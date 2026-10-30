from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from satrx.api.deps import get_session, get_settings_dep, require_auth
from satrx.core.config import Settings
from satrx.core.models import Pass, Satellite
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
    session.delete(_get_sat(session, norad_id))
    session.commit()
    scheduler_of(request).recompute()


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
def list_passes(hours: int = 48, session: Session = Depends(get_session)):
    now = datetime.now(timezone.utc)
    rows = session.execute(
        select(Pass, Satellite.name).outerjoin(Satellite, Satellite.norad_id == Pass.norad_id)
        .where(Pass.los >= now - timedelta(hours=1), Pass.aos <= now + timedelta(hours=hours))
        .order_by(Pass.aos)
    ).all()
    return [PassOut(id=p.id, norad_id=p.norad_id, satellite_name=name or p.label or "-", kind="event" if p.event_id else "pass",
                    rule_id=p.rule_id, event_id=p.event_id, aos=p.aos, tca=p.tca,
                    los=p.los, rec_start=p.rec_start, rec_end=p.rec_end,
                    max_elevation_deg=p.max_elevation_deg, status=p.status)
            for p, name in rows]


@router.post("/passes/recompute")
def recompute(request: Request, session: Session = Depends(get_session),
              settings: Settings = Depends(get_settings_dep)):
    if get_observer(session, settings) is None:
        raise HTTPException(409, "Observer location is not set")
    return {"count": scheduler_of(request).recompute()}
