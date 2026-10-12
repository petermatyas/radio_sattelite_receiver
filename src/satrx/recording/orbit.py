"""Pályametaadatok a nyers felvétel mellé (`<alapnév>.orbit.json`, lásd TERV.md 7.1)."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
from skyfield.api import EarthSatellite, load, wgs84

import satrx
from satrx.passes.doppler import C_KM_S
from satrx.passes.predictor import Observer
from satrx.recording import sigmf

log = logging.getLogger(__name__)
ORBIT_SCHEMA = 1
TRACK_STEP_S = 1.0
_ts = load.timescale(builtin=True)


@dataclass(frozen=True)
class OrbitContext:
    """A felvétel indításakor ismert pálya- és átvonulásadatok pillanatképe (minden mező hiányozhat)."""
    nominal_hz: int
    norad_id: int | None = None
    name: str | None = None
    tle_line1: str | None = None
    tle_line2: str | None = None
    tle_epoch: datetime | None = None
    observer: Observer | None = None
    pass_info: dict[str, Any] | None = None  # pass_id, rule_id, aos, tca, los, max_elevation_deg, rec_start, rec_end
    profile_name: str | None = None
    doppler_tracking: bool = False

    @property
    def has_orbit(self) -> bool:
        return bool(self.tle_line1 and self.tle_line2 and self.observer)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


def compute_track(ctx: OrbitContext, start: datetime, duration_s: float, step_s: float = TRACK_STEP_S) -> dict[str, list]:
    """Oszlopos pálya-idősor a [start, start+duration_s] szakaszra."""
    sat = EarthSatellite(ctx.tle_line1, ctx.tle_line2, ctx.name or "sat", _ts)
    topos = wgs84.latlon(ctx.observer.lat, ctx.observer.lon, elevation_m=ctx.observer.alt_m)
    t_s = np.arange(0.0, duration_s + step_s / 2, step_s)
    times = _ts.from_datetimes([start + timedelta(seconds=float(x)) for x in t_s])
    rel = (sat - topos).at(times)
    alt, az, dist = rel.altaz()
    pos, vel = rel.position.km, rel.velocity.km_per_s
    rr = np.sum(pos * vel, axis=0) / np.linalg.norm(pos, axis=0)
    sub = wgs84.subpoint_of(sat.at(times))
    doppler = ctx.nominal_hz * (1 - rr / C_KM_S)

    def r(a, n):
        return np.round(a, n).tolist()

    return {
        "step_s": step_s, "t_s": r(t_s, 3), "az_deg": r(az.degrees, 2), "el_deg": r(alt.degrees, 2),
        "range_km": r(dist.km, 2), "range_rate_km_s": r(rr, 4), "doppler_freq_hz": np.round(doppler).astype(int).tolist(),
        "subpoint_lat_deg": r(sub.latitude.degrees, 3), "subpoint_lon_deg": r(sub.longitude.degrees, 3),
    }


def build_orbit_doc(ctx: OrbitContext | None, *, base: Path, start: datetime, planned_duration_s: float,
                    sample_rate_hz: int, gain_db: float | None, fmt: str, stopped_early: bool,
                    error: str | None) -> dict[str, Any]:
    """Az `.orbit.json` tartalma; a tényleges mintaszámot és az újrahangolásokat a SigMF fájlból olvassa vissza."""
    num_samples, captures = 0, []
    try:
        info = sigmf.read_info(base)
        num_samples, captures = info.num_samples, info.captures
    except (OSError, KeyError, ValueError):
        pass  # megszakadt felvételnél a meta hiányozhat; a doksi akkor is elkészül
    actual_s = num_samples / sample_rate_hz if sample_rate_hz else 0.0
    ctx = ctx or OrbitContext(nominal_hz=captures[0]["core:frequency"] if captures else 0)

    doc: dict[str, Any] = {
        "orbit_schema": ORBIT_SCHEMA,
        "software": {"name": "satrx", "version": satrx.__version__},
        "satellite": None,
        "observer": None,
        "pass": ctx.pass_info,
        "radio": {
            "profile": ctx.profile_name, "nominal_hz": ctx.nominal_hz,
            "initial_center_hz": captures[0]["core:frequency"] if captures else None,
            "sample_rate_hz": sample_rate_hz, "gain_db": gain_db, "format": fmt,
            "doppler_tracking": ctx.doppler_tracking,
            "retunes": [{"sample_start": c["core:sample_start"],
                         "t_s": round(c["core:sample_start"] / sample_rate_hz, 3), "freq_hz": c["core:frequency"]}
                        for c in captures[1:]],
        },
        "recording": {
            "start": _iso(start), "end": _iso(start + timedelta(seconds=actual_s)),
            "planned_duration_s": round(planned_duration_s, 3), "actual_duration_s": round(actual_s, 3),
            "num_samples": num_samples, "stopped_early": stopped_early, "error": error,
        },
        "track": None,
    }
    if ctx.norad_id is not None or ctx.tle_line1:
        doc["satellite"] = {
            "norad_id": ctx.norad_id, "name": ctx.name,
            "tle": {"line1": ctx.tle_line1, "line2": ctx.tle_line2, "epoch": _iso(ctx.tle_epoch)}
            if ctx.tle_line1 else None,
        }
    if ctx.observer:
        doc["observer"] = {"lat": ctx.observer.lat, "lon": ctx.observer.lon, "alt_m": ctx.observer.alt_m}
    if ctx.has_orbit and actual_s > 0:
        try:
            doc["track"] = compute_track(ctx, start, actual_s)
        except Exception:  # noqa: BLE001 - a pálya-idősor hibája nem veszejtheti el a többi metaadatot
            log.exception("Orbit track computation failed")
    return doc


def write_orbit_file(base: Path, doc: dict[str, Any]) -> Path:
    path = sigmf.orbit_path(base)
    path.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
    return path


def load_orbit(base: Path) -> dict[str, Any] | None:
    path = sigmf.orbit_path(base)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
