"""Műholdankénti frekvenciák és módok („csatornák"): egy helyen adható meg a frekvencia, a mód és a módhoz tartozó beállítások.

Egy csatorna mögött a rendszer egy rádióprofilt és egy pipeline-t tart karban (a csatorna módosításakor ezek is frissülnek),
így az ütemező, a szabályok és a felvételkezelés változatlanul profillal és pipeline-nal dolgoznak.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from satrx.api.deps import get_session, require_auth
from satrx.core.models import Pipeline, RadioProfile, Satellite, SatelliteChannel, TimedEvent, WatchRule
from satrx.dsp.sstv_modes import MODE_NAMES

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])

MODE_DEFAULTS: dict[str, dict[str, Any]] = {
    "fm": {"bandwidth_hz": 25_000, "sample_rate_hz": 250_000, "doppler": True, "keep_iq": True},
    "sstv": {"bandwidth_hz": 25_000, "sample_rate_hz": 250_000, "doppler": True, "keep_iq": True},
    "lrpt": {"bandwidth_hz": 150_000, "sample_rate_hz": 1_024_000, "doppler": True, "keep_iq": False},
    "record": {"bandwidth_hz": 25_000, "sample_rate_hz": 250_000, "doppler": True, "keep_iq": True},
}
LRPT_PIPELINES = ("auto", "meteor_m2-x_lrpt", "meteor_m2-x_lrpt_80k")


class ChannelIn(BaseModel):
    name: str = Field("", max_length=60)                       # üres = automatikus név
    center_freq_hz: int = Field(gt=0)
    mode: Literal["fm", "sstv", "lrpt", "record"] = "fm"
    bandwidth_hz: int | None = Field(None, gt=0)               # üres = a mód alapértéke
    sample_rate_hz: int | None = Field(None, gt=0)
    gain_db: float | None = None                               # üres = automatikus erősítés
    doppler: bool | None = None
    keep_iq: bool | None = None
    # módhoz tartozó beállítások: fm: squelch_db, audio_lowpass_hz, waterfall; sstv: sstv_mode, min_lines; lrpt: lrpt_pipeline
    settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("settings")
    @classmethod
    def _known_settings(cls, v: dict[str, Any]) -> dict[str, Any]:
        allowed = {"squelch_db", "audio_lowpass_hz", "waterfall", "sstv_mode", "min_lines", "lrpt_pipeline"}
        unknown = set(v) - allowed
        if unknown:
            raise ValueError(f"unknown settings: {', '.join(sorted(unknown))}")
        if "sstv_mode" in v and v["sstv_mode"] not in ("auto", *MODE_NAMES):
            raise ValueError(f"sstv_mode must be 'auto' or one of {', '.join(MODE_NAMES)}")
        if "lrpt_pipeline" in v and v["lrpt_pipeline"] not in LRPT_PIPELINES:
            raise ValueError(f"lrpt_pipeline must be one of {', '.join(LRPT_PIPELINES)}")
        return v


class ChannelOut(ChannelIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    norad_id: int
    name: str
    bandwidth_hz: int
    sample_rate_hz: int
    doppler: bool
    keep_iq: bool
    profile_id: int
    pipeline_id: int


def pipeline_steps(ch: SatelliteChannel) -> list[dict[str, Any]]:
    s = ch.settings or {}
    fm_bw = float(min(max(ch.bandwidth_hz, 1_000), 100_000))
    if ch.mode == "fm":
        params: dict[str, Any] = {"bandwidth_hz": fm_bw}
        for key in ("squelch_db", "audio_lowpass_hz"):
            if s.get(key) is not None:
                params[key] = s[key]
        steps = [{"processor": "fm_demod", "params": params}]
        if s.get("waterfall"):
            steps.append({"processor": "waterfall", "params": {}})
        return steps
    if ch.mode == "sstv":
        return [{"processor": "fm_demod", "params": {"bandwidth_hz": fm_bw, "audio_lowpass_hz": 3500}},
                {"processor": "sstv", "params": {"mode": s.get("sstv_mode", "auto"), **({"min_lines": int(s["min_lines"])} if s.get("min_lines") else {})}}]
    if ch.mode == "lrpt":
        return [{"processor": "meteor_lrpt", "params": {"pipeline": s.get("lrpt_pipeline", "auto")}}]
    return []


def _auto_name(sat: Satellite, ch: SatelliteChannel) -> str:
    return f"{sat.name or sat.norad_id} {ch.center_freq_hz / 1e6:.4f} MHz {ch.mode.upper()}"


def _sync(session: Session, sat: Satellite, ch: SatelliteChannel) -> None:
    """A csatornához tartozó profil és pipeline létrehozása / frissítése."""
    if not ch.name:
        ch.name = _auto_name(sat, ch)
    tag = f"{ch.name} [{sat.norad_id}#{ch.id}]"
    profile = session.get(RadioProfile, ch.profile_id) if ch.profile_id else None
    if profile is None:
        profile = RadioProfile(name=tag, center_freq_hz=ch.center_freq_hz, bandwidth_hz=ch.bandwidth_hz, sample_rate_hz=ch.sample_rate_hz)
        session.add(profile)
    profile.name, profile.center_freq_hz, profile.bandwidth_hz = tag, ch.center_freq_hz, ch.bandwidth_hz
    profile.sample_rate_hz, profile.gain_db, profile.doppler_correction = ch.sample_rate_hz, ch.gain_db, ch.doppler
    profile.modulation_hint = {"fm": "nfm", "sstv": "nfm", "lrpt": "lrpt", "record": ""}[ch.mode]
    pipeline = session.get(Pipeline, ch.pipeline_id) if ch.pipeline_id else None
    if pipeline is None:
        pipeline = Pipeline(name=tag, mode=ch.mode, steps=[], keep_iq=ch.keep_iq)
        session.add(pipeline)
    pipeline.name, pipeline.mode, pipeline.steps, pipeline.keep_iq = tag, ch.mode, pipeline_steps(ch), ch.keep_iq
    session.flush()
    ch.profile_id, ch.pipeline_id = profile.id, pipeline.id


def _apply(ch: SatelliteChannel, body: ChannelIn) -> None:
    d = MODE_DEFAULTS[body.mode]
    ch.name, ch.mode, ch.center_freq_hz, ch.gain_db = body.name.strip(), body.mode, body.center_freq_hz, body.gain_db
    ch.bandwidth_hz = body.bandwidth_hz or d["bandwidth_hz"]
    ch.sample_rate_hz = body.sample_rate_hz or d["sample_rate_hz"]
    ch.doppler = d["doppler"] if body.doppler is None else body.doppler
    ch.keep_iq = d["keep_iq"] if body.keep_iq is None else body.keep_iq
    ch.settings = body.settings
    if ch.bandwidth_hz > ch.sample_rate_hz:
        raise HTTPException(422, "bandwidth_hz must not exceed sample_rate_hz")


def _sat(session: Session, norad_id: int) -> Satellite:
    sat = session.get(Satellite, norad_id)
    if sat is None:
        raise HTTPException(404, "Satellite not found")
    return sat


@router.get("/channels/options")
def channel_options() -> dict[str, Any]:
    return {"modes": list(MODE_DEFAULTS), "defaults": MODE_DEFAULTS, "sstv_modes": list(MODE_NAMES), "lrpt_pipelines": list(LRPT_PIPELINES)}


@router.get("/satellites/{norad_id}/channels", response_model=list[ChannelOut])
def list_channels(norad_id: int, session: Session = Depends(get_session)):
    _sat(session, norad_id)
    return session.scalars(select(SatelliteChannel).where(SatelliteChannel.norad_id == norad_id).order_by(SatelliteChannel.id)).all()


@router.get("/channels", response_model=list[ChannelOut])
def all_channels(session: Session = Depends(get_session)):
    return session.scalars(select(SatelliteChannel).order_by(SatelliteChannel.norad_id, SatelliteChannel.id)).all()


@router.post("/satellites/{norad_id}/channels", response_model=ChannelOut, status_code=201)
def create_channel(norad_id: int, body: ChannelIn, session: Session = Depends(get_session)):
    sat = _sat(session, norad_id)
    ch = SatelliteChannel(norad_id=norad_id)
    _apply(ch, body)
    session.add(ch)
    session.flush()
    _sync(session, sat, ch)
    session.commit()
    return ch


@router.put("/channels/{channel_id}", response_model=ChannelOut)
def update_channel(channel_id: int, body: ChannelIn, request: Request, session: Session = Depends(get_session)):
    ch = session.get(SatelliteChannel, channel_id)
    if ch is None:
        raise HTTPException(404, "Channel not found")
    _apply(ch, body)
    _sync(session, _sat(session, ch.norad_id), ch)
    session.commit()
    request.app.state.scheduler.recompute()              # a frekvencia / mód változása az ütemezett felvételeket is érinti
    return ch


@router.delete("/channels/{channel_id}", status_code=204)
def delete_channel(channel_id: int, request: Request, session: Session = Depends(get_session)):
    ch = session.get(SatelliteChannel, channel_id)
    if ch is None:
        raise HTTPException(404, "Channel not found")
    used = session.scalar(select(func.count()).select_from(WatchRule).where(
        (WatchRule.profile_id == ch.profile_id) | (WatchRule.pipeline_id == ch.pipeline_id))) or 0
    used += session.scalar(select(func.count()).select_from(TimedEvent).where(
        (TimedEvent.profile_id == ch.profile_id) | (TimedEvent.pipeline_id == ch.pipeline_id))) or 0
    if used:
        raise HTTPException(409, f"The channel is used by {used} rule(s)/event(s): delete those first")
    profile, pipeline = session.get(RadioProfile, ch.profile_id), session.get(Pipeline, ch.pipeline_id)
    session.delete(ch)
    session.flush()
    for obj in (profile, pipeline):
        if obj is not None:
            session.delete(obj)
    session.commit()
    request.app.state.scheduler.recompute()
