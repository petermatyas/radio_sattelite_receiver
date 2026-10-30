from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from satrx.api.deps import get_session, get_settings_dep, require_auth
from satrx.core.config import Settings
from satrx.core.models import Pass, Pipeline, RadioProfile, Satellite, SatelliteChannel, WatchRule
from satrx.passes.predictor import predict_passes
from satrx.passes.service import get_observer, set_observer
from satrx.scheduler.service import Scheduler
from satrx.tle import service as tle_service
from satrx.tle.client import TleError

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


class SatelliteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    norad_id: int
    name: str
    tle_epoch: datetime | None
    enabled: bool


class SatelliteIn(BaseModel):
    norad_id: int = Field(gt=0)


class SatelliteRefreshOut(SatelliteOut):
    changed: bool                         # az új TLE epochája eltér a korábbitól
    previous_epoch: datetime | None


class SatellitePatch(BaseModel):
    enabled: bool


class PassOut(BaseModel):
    id: int
    norad_id: int | None
    satellite_name: str                  # időalapú eseménynél az esemény neve
    kind: str = "pass"                  # pass | event
    single: bool = False                # egyetlen átvonulásra szóló (az idővonalról beütemezett) szabályból; visszavonható
    rule_id: int | None
    event_id: int | None = None
    aos: datetime
    tca: datetime
    los: datetime
    rec_start: datetime | None
    rec_end: datetime | None
    max_elevation_deg: float
    status: str


class ObserverIO(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    alt_m: float = 0.0


def _fetcher(request: Request):
    return request.app.state.tle_fetcher


def scheduler_of(request: Request) -> Scheduler:
    return request.app.state.scheduler


def _get_sat(session: Session, norad_id: int) -> Satellite:
    sat = session.get(Satellite, norad_id)
    if sat is None:
        raise HTTPException(404, "Satellite not found")
    return sat


@router.get("/satellites", response_model=list[SatelliteOut])
def list_satellites(session: Session = Depends(get_session)):
    return session.scalars(select(Satellite).order_by(Satellite.name)).all()


@router.post("/satellites", response_model=SatelliteOut, status_code=201)
def add_satellite(body: SatelliteIn, request: Request, session: Session = Depends(get_session)):
    try:
        sat = tle_service.add_satellite(session, body.norad_id, _fetcher(request))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except TleError as exc:
        raise HTTPException(502, str(exc))
    scheduler_of(request).recompute()
    return sat


@router.patch("/satellites/{norad_id}", response_model=SatelliteOut)
def patch_satellite(norad_id: int, body: SatellitePatch, request: Request, session: Session = Depends(get_session)):
    sat = _get_sat(session, norad_id)
    sat.enabled = body.enabled
    session.commit()
    scheduler_of(request).recompute()
    return sat


@router.delete("/satellites/{norad_id}", status_code=204)
def delete_satellite(norad_id: int, request: Request, session: Session = Depends(get_session)):
    sat = _get_sat(session, norad_id)
    owned = [(c.profile_id, c.pipeline_id) for c in session.scalars(select(SatelliteChannel).where(SatelliteChannel.norad_id == norad_id))]
    session.delete(sat)
    session.flush()
    for profile_id, pipeline_id in owned:          # a csatornák karbantartott profiljai és pipeline-jai is törlődnek
        for model, id_ in ((RadioProfile, profile_id), (Pipeline, pipeline_id)):
            obj = session.get(model, id_)
            if obj is not None:
                session.delete(obj)
    session.commit()
    scheduler_of(request).recompute()


@router.post("/satellites/refresh-tle")
def refresh_all_tle(request: Request, session: Session = Depends(get_session)):
    """Az összes műhold TLE-jének frissítése; egy hiba nem állítja meg a többit."""
    results = []
    for sat in session.scalars(select(Satellite).order_by(Satellite.name)).all():
        previous = sat.tle_epoch
        try:
            tle_service.refresh_tle(session, sat, _fetcher(request))
            results.append({"norad_id": sat.norad_id, "name": sat.name, "changed": sat.tle_epoch != previous, "error": None})
        except TleError as exc:
            session.rollback()
            results.append({"norad_id": sat.norad_id, "name": sat.name, "changed": False, "error": str(exc)})
    scheduler_of(request).recompute()
    return {"results": results, "updated": sum(r["changed"] for r in results), "failed": sum(r["error"] is not None for r in results),
            "total": len(results)}


@router.post("/satellites/{norad_id}/refresh-tle", response_model=SatelliteRefreshOut)
def refresh_tle(norad_id: int, request: Request, session: Session = Depends(get_session)):
    sat = _get_sat(session, norad_id)
    previous = sat.tle_epoch
    try:
        tle_service.refresh_tle(session, sat, _fetcher(request))
    except TleError as exc:
        raise HTTPException(502, str(exc))
    scheduler_of(request).recompute()
    return SatelliteRefreshOut.model_validate({"norad_id": sat.norad_id, "name": sat.name, "tle_epoch": sat.tle_epoch,
                                               "enabled": sat.enabled, "changed": sat.tle_epoch != previous, "previous_epoch": previous})


@router.get("/settings/observer", response_model=ObserverIO | None)
def read_observer(session: Session = Depends(get_session), settings: Settings = Depends(get_settings_dep)):
    obs = get_observer(session, settings)
    return ObserverIO(lat=obs.lat, lon=obs.lon, alt_m=obs.alt_m) if obs else None


@router.put("/settings/observer", response_model=ObserverIO)
def write_observer(body: ObserverIO, request: Request, session: Session = Depends(get_session)):
    set_observer(session, body.lat, body.lon, body.alt_m)
    scheduler_of(request).recompute()
    return body


@router.get("/passes", response_model=list[PassOut])
def list_passes(hours: int = 48, unscheduled: bool = False, session: Session = Depends(get_session),
                settings: Settings = Depends(get_settings_dep)):
    now = datetime.now(timezone.utc)
    rows = session.execute(
        select(Pass, Satellite.name).outerjoin(Satellite, Satellite.norad_id == Pass.norad_id)
        .where(Pass.los >= now - timedelta(hours=1), Pass.aos <= now + timedelta(hours=hours))
        .order_by(Pass.aos)
    ).all()
    single_rules = set(session.scalars(select(WatchRule.id).where(WatchRule.single_pass)))
    out = [PassOut(id=p.id, norad_id=p.norad_id, satellite_name=name or p.label or "-", kind="event" if p.event_id else "pass",
                   single=p.rule_id in single_rules, rule_id=p.rule_id, event_id=p.event_id, aos=p.aos, tca=p.tca,
                   los=p.los, rec_start=p.rec_start, rec_end=p.rec_end,
                   max_elevation_deg=p.max_elevation_deg, status=p.status)
           for p, name in rows]
    if unscheduled:
        out += _unscheduled_passes(session, settings, now, hours, rows)
        out.sort(key=lambda p: p.aos)
    return out


def _unscheduled_passes(session: Session, settings: Settings, now: datetime, hours: int, scheduled) -> list[PassOut]:
    """A TLE-vel rendelkező műholdak átvonulásai, amelyekhez nincs ütemezett felvétel (tájékoztató, nem kerül felvételre).

    Azonosító: negatív szám (nem adatbázis-sor), a műholdból és az AOS percéből képzett, így újratöltéskor sem változik. Az ütemezett átvonulással átfedő előrejelzést kihagyjuk.
    """
    observer = get_observer(session, settings)
    if observer is None:
        return []
    start, end = now - timedelta(hours=1), now + timedelta(hours=hours)
    out: list[PassOut] = []
    for sat in session.scalars(select(Satellite).order_by(Satellite.norad_id)):
        if not (sat.tle_line1 and sat.tle_line2):
            continue
        taken = [(p.aos, p.los) for p, _ in scheduled if p.norad_id == sat.norad_id]
        try:
            predicted = predict_passes(sat.tle_line1, sat.tle_line2, observer, start, end,
                                       settings.scheduling.default_min_elevation_deg, sat.name)
        except Exception:  # noqa: BLE001 - egy hibás TLE nem vihet el az egész listát
            continue
        for p in predicted:
            if any(a < p.los and p.aos < b for a, b in taken):
                continue
            out.append(PassOut(id=-(sat.norad_id * 1_000_000 + int(p.aos.timestamp() // 60) % 1_000_000), norad_id=sat.norad_id, satellite_name=sat.name, kind="pass", rule_id=None,
                               aos=p.aos, tca=p.tca, los=p.los, rec_start=None, rec_end=None,
                               max_elevation_deg=p.max_elevation_deg, status="unscheduled"))
    return out


@router.post("/passes/recompute")
def recompute(request: Request, session: Session = Depends(get_session),
              settings: Settings = Depends(get_settings_dep)):
    if get_observer(session, settings) is None:
        raise HTTPException(409, "Observer location is not set")
    return {"count": scheduler_of(request).recompute()}
