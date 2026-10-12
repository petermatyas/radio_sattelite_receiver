from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from skyfield.api import EarthSatellite, load, wgs84

_ts = load.timescale(builtin=True)


@dataclass(frozen=True)
class Observer:
    lat: float
    lon: float
    alt_m: float = 0.0


@dataclass(frozen=True)
class PassPrediction:
    aos: datetime
    tca: datetime
    los: datetime
    max_elevation_deg: float


def _to_utc(t) -> datetime:
    return t.utc_datetime().astimezone(timezone.utc)


def predict_passes(line1: str, line2: str, observer: Observer, start: datetime, end: datetime,
                   min_elevation_deg: float = 10.0, name: str = "sat") -> list[PassPrediction]:
    """Átvonulások [start, end) között, amelyek max. elevációja >= min_elevation_deg.

    AOS/LOS az 'min_elevation_deg' küszöb átlépése (nem a horizont), így a felvételi
    ablak közvetlenül használható. A szelet elején/végén félbevágott átvonulást is visszaadjuk.
    """
    sat = EarthSatellite(line1, line2, name, _ts)
    topos = wgs84.latlon(observer.lat, observer.lon, elevation_m=observer.alt_m)
    t0, t1 = _ts.from_datetime(start), _ts.from_datetime(end)
    times, events = sat.find_events(topos, t0, t1, altitude_degrees=min_elevation_deg)

    def elevation(t) -> float:
        return float((sat - topos).at(t).altaz()[0].degrees)

    def build(cur: dict, fallback_aos, fallback_los) -> PassPrediction:
        aos = cur.get(0, fallback_aos)
        los = cur.get(2, fallback_los)
        culm = cur.get(1, aos)  # félbevágott átvonulásnál a csúcs a szeleten kívül lehet
        return PassPrediction(_to_utc(aos), _to_utc(culm), _to_utc(los), elevation(culm))

    passes: list[PassPrediction] = []
    cur: dict[int, object] = {}
    for t, ev in zip(times, events):
        cur[int(ev)] = t
        if ev == 2:
            passes.append(build(cur, t0, t1))
            cur = {}
    if cur:  # a szelet végén még tart az átvonulás
        passes.append(build(cur, t0, t1))
    return passes
