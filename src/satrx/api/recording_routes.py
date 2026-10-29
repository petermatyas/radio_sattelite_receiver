from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from satrx.api.deps import get_session, get_settings_dep, require_auth
from satrx.passes.service import get_observer
from satrx.recording.orbit import OrbitContext
from satrx.storage import service as storage_service
from satrx.core.config import Settings
from satrx.core.models import Recording, Satellite
from satrx.hardware.base import SdrError
from satrx.recording.manager import BusyError, RecordingManager
from satrx.recording.recorder import RecordSpec

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


class StartRecording(BaseModel):
    center_freq_hz: int = Field(gt=0)
    sample_rate_hz: int = Field(gt=0)
    duration_s: float = Field(gt=0, le=3600)
    gain_db: float | None = None
    bandwidth_hz: float | None = Field(None, gt=0, description="csatorna-sávszélesség: a feldolgozás alapértelmezése")
    format: Literal["cu8", "ci16_le", "cf32_le"] | None = None  # None: az eszköz natív formátuma
    norad_id: int | None = None


class StartFmRecording(BaseModel):
    center_freq_hz: int = Field(gt=0)
    duration_s: float = Field(gt=0, le=3600)
    mode: Literal["fm", "wbfm", "am", "usb", "lsb"] = "fm"
    bandwidth_hz: int = Field(12_500, ge=1_000, le=250_000, description="az rtl_fm demodulátorának sávszélessége (-s)")
    audio_rate_hz: int = Field(24_000, ge=8_000, le=48_000)
    gain_db: float | None = None
    squelch: int = Field(0, ge=0, le=500)
    label: str | None = Field(None, max_length=60)


class RecorderStatusOut(BaseModel):
    state: str
    recording_id: int | None
    started_at: datetime | None
    duration_s: float
    elapsed_s: float
    last_error: str | None


class RecordingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    norad_id: int | None
    path: str
    sample_format: str
    center_freq_hz: int
    sample_rate_hz: int
    started_at: datetime
    ended_at: datetime | None
    size_bytes: int
    deleted: bool
    orbit_meta_path: str | None
    source: str | None = None
    label: str | None = None


def _manager(request: Request) -> RecordingManager:
    return request.app.state.recorder


@router.get("/recorder/status", response_model=RecorderStatusOut)
def recorder_status(request: Request):
    return _manager(request).status()


@router.post("/recorder/start-fm", status_code=202)
def recorder_start_fm(body: StartFmRecording, request: Request, settings: Settings = Depends(get_settings_dep)):
    """Közvetlen hangfelvétel az rtl_fm programmal: a WAV az eredmények (Galéria) közé kerül."""
    from satrx.recording.rtlfm import FmSpec
    args = settings.sdr.device_args if isinstance(settings.sdr.device_args, dict) else {}
    try:
        job_id = _manager(request).start_fm(
            FmSpec(body.center_freq_hz, body.duration_s, body.mode, body.bandwidth_hz, body.audio_rate_hz, body.gain_db,
                   body.squelch, int(args.get("ppm", 0)), int(args.get("device_index", 0))),
            label=body.label)
    except BusyError as exc:
        raise HTTPException(409, str(exc))
    except SdrError as exc:
        raise HTTPException(422, str(exc))
    return {"job_id": job_id}


@router.post("/recorder/start", response_model=RecordingOut, status_code=202)
def recorder_start(body: StartRecording, request: Request, session: Session = Depends(get_session),
                   settings: Settings = Depends(get_settings_dep)):
    manager = _manager(request)
    try:
        fmt = body.format or manager.native_format()
        rec_id = manager.start(
            RecordSpec(body.center_freq_hz, body.sample_rate_hz, body.duration_s, body.gain_db, fmt,
                       description="Manual recording", orbit=_manual_orbit(session, settings, body),
                       extra_meta={"satrx:bandwidth_hz": body.bandwidth_hz} if body.bandwidth_hz else None),
            norad_id=body.norad_id,
        )
    except BusyError as exc:
        raise HTTPException(409, str(exc))
    except SdrError as exc:
        raise HTTPException(422, str(exc))
    return session.get(Recording, rec_id)


@router.post("/recorder/stop", status_code=204)
def recorder_stop(request: Request):
    _manager(request).stop()


@router.get("/recordings", response_model=list[RecordingOut])
def list_recordings(limit: int = 200, session: Session = Depends(get_session)):
    rows = session.scalars(select(Recording).order_by(Recording.started_at.desc()).limit(limit)).all()
    return rows


@router.delete("/recordings/{recording_id}", status_code=204)
def delete_recording(recording_id: int, request: Request, session: Session = Depends(get_session),
                     settings: Settings = Depends(get_settings_dep)):
    rec = session.get(Recording, recording_id)
    if rec is None:
        raise HTTPException(404, "Recording not found")
    status = _manager(request).status()
    if status.state != "idle" and status.recording_id == recording_id:
        raise HTTPException(409, "Recording is in progress")
    storage_service.delete_recording(session, request.app.state.storage, rec)



def _manual_orbit(session: Session, settings: Settings, body: StartRecording) -> OrbitContext:
    """Kézi felvételnél is mentjük a pályametaadatot, ha a műhold és a helyszín ismert (Doppler-követés nincs)."""
    sat = session.get(Satellite, body.norad_id) if body.norad_id else None
    return OrbitContext(
        nominal_hz=body.center_freq_hz, norad_id=body.norad_id, name=sat.name if sat else None,
        tle_line1=sat.tle_line1 if sat else None, tle_line2=sat.tle_line2 if sat else None,
        tle_epoch=sat.tle_epoch if sat else None,
        observer=get_observer(session, settings) if sat else None, profile_name="manual")
