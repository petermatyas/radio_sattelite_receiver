from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from datetime import timezone as dt_timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from satrx.api.deps import get_session, require_auth
from satrx.core.models import Antenna, Pipeline, RadioProfile, Satellite, TimedEvent, WatchRule
from satrx.scheduler.occurrences import get_zone, next_occurrences

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


def _commit(session: Session) -> None:
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, f"Conflict (duplicate name or referenced by other records): {exc.orig}")


def _validate_steps(request: Request, steps: list[dict]) -> None:
    try:
        request.app.state.runner.validate_steps(steps)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


def _recompute(request: Request) -> None:
    request.app.state.scheduler.recompute()


def _get(session: Session, model, id_: int):
    obj = session.get(model, id_)
    if obj is None:
        raise HTTPException(404, f"{model.__name__} not found")
    return obj


# ---- antennák -------------------------------------------------------------------------------

class AntennaIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    freq_min_hz: int = Field(ge=0, default=0)
    freq_max_hz: int = Field(gt=0, default=6_000_000_000)
    switch_port: str | None = None
    notes: str = ""


class AntennaOut(AntennaIn):
    model_config = ConfigDict(from_attributes=True)
    id: int


@router.get("/antennas", response_model=list[AntennaOut])
def list_antennas(session: Session = Depends(get_session)):
    return session.scalars(select(Antenna).order_by(Antenna.name)).all()


@router.post("/antennas", response_model=AntennaOut, status_code=201)
def create_antenna(body: AntennaIn, session: Session = Depends(get_session)):
    obj = Antenna(**body.model_dump())
    session.add(obj)
    _commit(session)
    return obj


@router.put("/antennas/{id_}", response_model=AntennaOut)
def update_antenna(id_: int, body: AntennaIn, session: Session = Depends(get_session)):
    obj = _get(session, Antenna, id_)
    for k, v in body.model_dump().items():
        setattr(obj, k, v)
    _commit(session)
    return obj


@router.delete("/antennas/{id_}", status_code=204)
def delete_antenna(id_: int, session: Session = Depends(get_session)):
    session.delete(_get(session, Antenna, id_))
    _commit(session)


# ---- rádióprofilok --------------------------------------------------------------------------

class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    center_freq_hz: int = Field(gt=0)
    bandwidth_hz: int = Field(gt=0)
    sample_rate_hz: int = Field(gt=0)
    gain_db: float | None = None
    modulation_hint: str = ""
    doppler_correction: bool = True
    antenna_id: int | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.bandwidth_hz > self.sample_rate_hz:
            raise ValueError("bandwidth_hz must not exceed sample_rate_hz")
        return self


class ProfileOut(ProfileIn):
    model_config = ConfigDict(from_attributes=True)
    id: int


@router.get("/profiles", response_model=list[ProfileOut])
def list_profiles(session: Session = Depends(get_session)):
    return session.scalars(select(RadioProfile).order_by(RadioProfile.name)).all()


@router.post("/profiles", response_model=ProfileOut, status_code=201)
def create_profile(body: ProfileIn, session: Session = Depends(get_session)):
    obj = RadioProfile(**body.model_dump())
    session.add(obj)
    _commit(session)
    return obj


@router.put("/profiles/{id_}", response_model=ProfileOut)
def update_profile(id_: int, body: ProfileIn, request: Request, session: Session = Depends(get_session)):
    obj = _get(session, RadioProfile, id_)
    for k, v in body.model_dump().items():
        setattr(obj, k, v)
    _commit(session)
    _recompute(request)
    return obj


@router.delete("/profiles/{id_}", status_code=204)
def delete_profile(id_: int, session: Session = Depends(get_session)):
    session.delete(_get(session, RadioProfile, id_))
    _commit(session)


# ---- pipeline-ok ----------------------------------------------------------------------------

class PipelineIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    mode: str = Field(min_length=1, max_length=40)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    keep_iq: bool = True


class PipelineOut(PipelineIn):
    model_config = ConfigDict(from_attributes=True)
    id: int


@router.get("/pipelines", response_model=list[PipelineOut])
def list_pipelines(session: Session = Depends(get_session)):
    return session.scalars(select(Pipeline).order_by(Pipeline.name)).all()


@router.post("/pipelines", response_model=PipelineOut, status_code=201)
def create_pipeline(body: PipelineIn, request: Request, session: Session = Depends(get_session)):
    _validate_steps(request, body.steps)
    obj = Pipeline(**body.model_dump())
    session.add(obj)
    _commit(session)
    return obj


@router.put("/pipelines/{id_}", response_model=PipelineOut)
def update_pipeline(id_: int, body: PipelineIn, request: Request, session: Session = Depends(get_session)):
    _validate_steps(request, body.steps)
    obj = _get(session, Pipeline, id_)
    for k, v in body.model_dump().items():
        setattr(obj, k, v)
    _commit(session)
    return obj


@router.delete("/pipelines/{id_}", status_code=204)
def delete_pipeline(id_: int, session: Session = Depends(get_session)):
    session.delete(_get(session, Pipeline, id_))
    _commit(session)


# ---- megfigyelési szabályok -----------------------------------------------------------------

class RuleIn(BaseModel):
    norad_id: int
    valid_from: datetime
    valid_to: datetime
    min_elevation_deg: float | None = Field(default=None, ge=0, le=90)
    priority: int = Field(default=0, ge=-100, le=100)
    profile_id: int
    pipeline_id: int
    allow_partial: bool = True
    enabled: bool = True

    @model_validator(mode="after")
    def _check(self):
        if self.valid_from.tzinfo is None or self.valid_to.tzinfo is None:
            raise ValueError("valid_from/valid_to must include a timezone (e.g. 2026-10-12T08:00:00Z)")
        if self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        return self


class RuleOut(RuleIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    satellite_name: str = ""


def _rule_out(session: Session, rule: WatchRule) -> RuleOut:
    sat = session.get(Satellite, rule.norad_id)
    out = RuleOut.model_validate(rule)
    out.satellite_name = sat.name if sat else ""
    return out


def _check_refs(session: Session, body: RuleIn) -> None:
    for model, id_ in ((Satellite, body.norad_id), (RadioProfile, body.profile_id), (Pipeline, body.pipeline_id)):
        if session.get(model, id_) is None:
            raise HTTPException(422, f"{model.__name__} {id_} does not exist")


@router.get("/rules", response_model=list[RuleOut])
def list_rules(session: Session = Depends(get_session)):
    rules = session.scalars(select(WatchRule).order_by(WatchRule.valid_from)).all()
    return [_rule_out(session, r) for r in rules]


@router.post("/rules", response_model=RuleOut, status_code=201)
def create_rule(body: RuleIn, request: Request, session: Session = Depends(get_session)):
    _check_refs(session, body)
    rule = WatchRule(**body.model_dump())
    session.add(rule)
    _commit(session)
    _recompute(request)
    return _rule_out(session, rule)


@router.put("/rules/{id_}", response_model=RuleOut)
def update_rule(id_: int, body: RuleIn, request: Request, session: Session = Depends(get_session)):
    rule = _get(session, WatchRule, id_)
    _check_refs(session, body)
    for k, v in body.model_dump().items():
        setattr(rule, k, v)
    _commit(session)
    _recompute(request)
    return _rule_out(session, rule)


@router.delete("/rules/{id_}", status_code=204)
def delete_rule(id_: int, request: Request, session: Session = Depends(get_session)):
    session.delete(_get(session, WatchRule, id_))
    _commit(session)
    _recompute(request)


# ---- időalapú események (NORAD ID nélkül) ----------------------------------------------------------------------------

class TimedEventIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    start_at: datetime
    duration_s: int = Field(ge=1, le=6 * 3600, description="egy előfordulás hossza, legfeljebb 6 óra")
    recurrence: Literal["none", "daily", "weekly"] = "none"
    weekdays: list[int] = Field(default_factory=list)
    repeat_until: datetime | None = None
    timezone: str = "UTC"
    profile_id: int
    pipeline_id: int
    priority: int = Field(default=0, ge=-100, le=100)
    allow_partial: bool = False
    notes: str = ""

    @model_validator(mode="after")
    def _check(self):
        if self.start_at.tzinfo is None or (self.repeat_until is not None and self.repeat_until.tzinfo is None):
            raise ValueError("start_at/repeat_until must include a timezone (e.g. 2026-10-12T18:00:00Z)")
        zone = get_zone(self.timezone)                      # ValueError: ismeretlen időzóna
        if any(d < 0 or d > 6 for d in self.weekdays):
            raise ValueError("weekdays must be between 0 (Monday) and 6 (Sunday)")
        self.weekdays = sorted(set(self.weekdays))
        if self.recurrence == "weekly" and not self.weekdays:
            self.weekdays = [self.start_at.astimezone(zone).weekday()]
        if self.recurrence != "weekly":
            self.weekdays = []
        if self.repeat_until is not None and self.repeat_until <= self.start_at:
            raise ValueError("repeat_until must be after start_at")
        if self.recurrence == "none":
            self.repeat_until = None
        return self


class TimedEventOut(TimedEventIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    next_occurrences: list[datetime] = Field(default_factory=list)
    estimated_bytes: int | None = None     # egy előfordulás nyers IQ-ja (cu8 becslés)
    profile_name: str = ""
    pipeline_name: str = ""


def _event_out(session: Session, ev: TimedEvent) -> TimedEventOut:
    out = TimedEventOut.model_validate(ev)
    profile = session.get(RadioProfile, ev.profile_id)
    pipeline = session.get(Pipeline, ev.pipeline_id)
    out.profile_name = profile.name if profile else ""
    out.pipeline_name = pipeline.name if pipeline else ""
    out.estimated_bytes = profile.sample_rate_hz * 2 * ev.duration_s if profile else None
    out.next_occurrences = next_occurrences(ev, datetime.now(dt_timezone.utc), 5)
    return out


def _check_event_refs(session: Session, body: TimedEventIn) -> None:
    for model, id_ in ((RadioProfile, body.profile_id), (Pipeline, body.pipeline_id)):
        if session.get(model, id_) is None:
            raise HTTPException(422, f"{model.__name__} {id_} does not exist")


@router.get("/timed-events", response_model=list[TimedEventOut])
def list_timed_events(session: Session = Depends(get_session)):
    return [_event_out(session, e) for e in session.scalars(select(TimedEvent).order_by(TimedEvent.start_at))]


@router.post("/timed-events/preview")
def preview_timed_event(body: TimedEventIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Az űrlap alapján a következő előfordulások (mentés nélkül), hogy a beállítás azonnal ellenőrizhető legyen."""
    _check_event_refs(session, body)
    ev = TimedEvent(**body.model_dump())
    profile = session.get(RadioProfile, body.profile_id)
    return {"next_occurrences": next_occurrences(ev, datetime.now(dt_timezone.utc), 8), "weekdays": body.weekdays,
            "estimated_bytes": profile.sample_rate_hz * 2 * body.duration_s}


@router.post("/timed-events", response_model=TimedEventOut, status_code=201)
def create_timed_event(body: TimedEventIn, request: Request, session: Session = Depends(get_session)):
    _check_event_refs(session, body)
    ev = TimedEvent(**body.model_dump())
    session.add(ev)
    _commit(session)
    _recompute(request)
    return _event_out(session, ev)


@router.put("/timed-events/{id_}", response_model=TimedEventOut)
def update_timed_event(id_: int, body: TimedEventIn, request: Request, session: Session = Depends(get_session)):
    ev = _get(session, TimedEvent, id_)
    _check_event_refs(session, body)
    for k, v in body.model_dump().items():
        setattr(ev, k, v)
    _commit(session)
    _recompute(request)
    return _event_out(session, ev)


@router.delete("/timed-events/{id_}", status_code=204)
def delete_timed_event(id_: int, request: Request, session: Session = Depends(get_session)):
    session.delete(_get(session, TimedEvent, id_))
    _commit(session)
    _recompute(request)
