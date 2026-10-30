from __future__ import annotations

import mimetypes
import shutil
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from satrx.api.deps import get_session, get_settings_dep, require_auth
from satrx.core.config import Settings
from satrx.core.models import (Artifact, Event, Job, JobStep, Pass, Pipeline, Recording, Satellite, WatchRule)
from satrx.passes.service import get_observer
from satrx.recording import sigmf
from satrx.recording.orbit import load_orbit
from satrx.pipeline.runner import PipelineError
from satrx.storage import service as storage_service
from satrx.tools import RTL_KEYS, apply_stored_tools, configured_satdump, find_rtl_tool, find_satdump, save_tool_paths

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])
_UNACKED = Event.acknowledged.is_(False)


def _state(request: Request):
    return request.app.state


# ---- processzorok ---------------------------------------------------------------------------

@router.get("/processors")
def list_processors(request: Request) -> list[dict[str, Any]]:
    return [{"name": c.name, "title": c.title, "params_schema": c.params_schema.model_json_schema()}
            for c in _state(request).registry.all()]


# ---- feldolgozás indítása ---------------------------------------------------------------------

class ProcessRequest(BaseModel):
    pipeline_id: int


@router.post("/recordings/{recording_id}/process", status_code=202)
def process_recording(recording_id: int, body: ProcessRequest, request: Request) -> dict[str, int]:
    try:
        return {"job_id": _state(request).executor.submit(recording_id, body.pipeline_id)}
    except PipelineError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


class StepIn(BaseModel):
    processor: str
    params: dict[str, Any] = {}


class RunRequest(BaseModel):
    steps: list[StepIn]


@router.post("/recordings/{recording_id}/run", status_code=202)
def run_steps(recording_id: int, body: RunRequest, request: Request) -> dict[str, int]:
    """Ad-hoc feldolgozás egy mentett felvételen (pl. waterfall vagy hang generálása)."""
    steps = [s.model_dump() for s in body.steps]
    try:
        return {"job_id": _state(request).executor.submit_steps(recording_id, steps)}
    except PipelineError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.get("/recordings/{recording_id}")
def recording_detail(recording_id: int, request: Request, session: Session = Depends(get_session),
                     settings: Settings = Depends(get_settings_dep)) -> dict[str, Any]:
    """A felvétel részletei: IQ-adatok, pályametaadat-összefoglaló, a belőle készült eredmények és jobok."""
    rec = session.get(Recording, recording_id)
    if rec is None:
        raise HTTPException(404, "Recording not found")
    sm = request.app.state.storage
    base = sm.recording_base(rec)
    storage_ok = sm.recording_available(rec)
    info = None
    if storage_ok and not rec.deleted and sigmf.data_path(base).exists():
        i = sigmf.read_info(base)
        info = {"format": i.fmt, "sample_rate_hz": i.sample_rate_hz, "center_freq_hz": i.center_freq_hz,
                "num_samples": i.num_samples, "duration_s": round(i.num_samples / i.sample_rate_hz, 3),
                "retunes": [{"t_s": round(c["core:sample_start"] / i.sample_rate_hz, 3), "freq_hz": c["core:frequency"]}
                            for c in i.captures[1:]]}
    orbit = load_orbit(base) if storage_ok else None
    orbit_summary = None
    if orbit:
        track = orbit.get("track")
        orbit_summary = {"satellite": orbit.get("satellite"), "observer": orbit.get("observer"),
                         "pass": orbit.get("pass"), "radio": orbit.get("radio"), "recording": orbit.get("recording"),
                         "has_track": bool(track), "track_points": len(track["t_s"]) if track else 0,
                         "max_elevation_deg": max(track["el_deg"]) if track else None}
    names = _names(session)
    arts = session.scalars(select(Artifact).where(Artifact.recording_id == recording_id)
                           .order_by(Artifact.id.desc())).all()
    jobs = session.scalars(select(Job).where(Job.recording_id == recording_id).order_by(Job.id.desc()).limit(20)).all()
    return {"recording": {"id": rec.id, "norad_id": rec.norad_id, "satellite_name": names.get(rec.norad_id) or rec.label,
                          "path": rec.path, "sample_format": rec.sample_format, "center_freq_hz": rec.center_freq_hz,
                          "sample_rate_hz": rec.sample_rate_hz, "started_at": rec.started_at,
                          "ended_at": rec.ended_at, "size_bytes": rec.size_bytes, "deleted": rec.deleted,
                          "protected": rec.protected, "source": rec.source},
            "iq_available": info is not None, "storage_available": storage_ok, "info": info, "orbit": orbit_summary,
            "artifacts": [_artifact_out(a, names) for a in arts],
            "jobs": [_job_out(session, j, names) for j in jobs]}


# ---- jobok ----------------------------------------------------------------------------------

class StepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    idx: int
    processor: str
    params: dict[str, Any]
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    log: str


class ArtifactOut(BaseModel):
    id: int
    job_id: int | None
    recording_id: int | None
    norad_id: int | None
    satellite_name: str | None = None
    kind: str
    has_thumbnail: bool
    meta: dict[str, Any]
    created_at: datetime


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    kind: str
    status: str
    pass_id: int | None
    recording_id: int | None
    pipeline_id: int | None
    pipeline_name: str | None = None
    satellite_name: str | None = None
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None


class JobDetail(JobOut):
    steps: list[StepOut]
    artifacts: list[ArtifactOut]


def _artifact_out(a: Artifact, names: dict[int, str]) -> ArtifactOut:
    return ArtifactOut(id=a.id, job_id=a.job_id, recording_id=a.recording_id, norad_id=a.norad_id,
                       satellite_name=names.get(a.norad_id) or names.get(("rec", a.recording_id)), kind=a.kind, has_thumbnail=bool(a.thumbnail_path),
                       meta=a.meta, created_at=a.created_at)


def _names(session: Session) -> dict:
    """norad_id -> műholdnév; a műhold nélküli (időalapú esemény) felvételekhez ("rec", id) -> az esemény neve."""
    names: dict = dict(session.execute(select(Satellite.norad_id, Satellite.name)).all())
    names.update({("rec", rid): label for rid, label in session.execute(
        select(Recording.id, Recording.label).where(Recording.label.is_not(None)))})
    return names


def _job_out(session: Session, job: Job, names: dict[int, str]) -> JobOut:
    out = JobOut.model_validate(job)
    pipeline = session.get(Pipeline, job.pipeline_id) if job.pipeline_id else None
    out.pipeline_name = pipeline.name if pipeline else None
    rec = session.get(Recording, job.recording_id) if job.recording_id else None
    if rec is None:
        rec = session.scalars(select(Recording).where(Recording.job_id == job.id)).first()
    out.satellite_name = (names.get(rec.norad_id) or names.get(("rec", rec.id))) if rec else None
    return out


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(limit: int = 50, kind: str | None = None, session: Session = Depends(get_session)):
    q = select(Job).order_by(Job.id.desc()).limit(limit)
    if kind:
        q = q.where(Job.kind == kind)
    names = _names(session)
    return [_job_out(session, j, names) for j in session.scalars(q)]


@router.get("/jobs/{job_id}", response_model=JobDetail)
def job_detail(job_id: int, session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    names = _names(session)
    steps = session.scalars(select(JobStep).where(JobStep.job_id == job_id).order_by(JobStep.idx)).all()
    arts = session.scalars(select(Artifact).where(Artifact.job_id == job_id).order_by(Artifact.id)).all()
    return JobDetail(**_job_out(session, job, names).model_dump(),
                     steps=[StepOut.model_validate(s) for s in steps],
                     artifacts=[_artifact_out(a, names) for a in arts])


@router.post("/jobs/{job_id}/retry", status_code=202)
def retry_job(job_id: int, request: Request, session: Session = Depends(get_session)) -> dict[str, int]:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.kind != "processing" or job.recording_id is None:
        raise HTTPException(409, "Only processing jobs can be retried")
    executor = _state(request).executor
    try:
        if job.pipeline_id is not None:
            return {"job_id": executor.submit(job.recording_id, job.pipeline_id, job.pass_id)}
        steps = session.scalars(select(JobStep).where(JobStep.job_id == job_id).order_by(JobStep.idx)).all()
        return {"job_id": executor.submit_steps(job.recording_id, [{"processor": s.processor, "params": s.params}
                                                                   for s in steps], job.pass_id)}
    except PipelineError as exc:
        raise HTTPException(409, str(exc))


# ---- események ------------------------------------------------------------------------------

class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    ts: datetime
    level: str
    source: str
    code: str
    message: str
    details: dict[str, Any]
    pass_id: int | None
    job_id: int | None
    recording_id: int | None
    acknowledged: bool


@router.get("/events", response_model=list[EventOut])
def list_events(level: str | None = None, source: str | None = None, unacked: bool = False,
                since: datetime | None = None, limit: int = 100, session: Session = Depends(get_session)):
    q = select(Event).order_by(Event.id.desc()).limit(min(limit, 1000))
    if level:
        q = q.where(Event.level == level)
    if source:
        q = q.where(Event.source == source)
    if unacked:
        q = q.where(_UNACKED)
    if since:
        q = q.where(Event.ts >= since)
    return session.scalars(q).all()


@router.get("/events/summary")
def events_summary(session: Session = Depends(get_session)) -> dict[str, int]:
    rows = dict(session.execute(select(Event.level, func.count()).where(_UNACKED).group_by(Event.level)).all())
    return {"error": rows.get("error", 0), "warning": rows.get("warning", 0), "info": rows.get("info", 0)}


@router.post("/events/{event_id}/ack", status_code=204)
def ack_event(event_id: int, session: Session = Depends(get_session)):
    ev = session.get(Event, event_id)
    if ev is None:
        raise HTTPException(404, "Event not found")
    ev.acknowledged = True
    session.commit()


@router.post("/events/ack-all")
def ack_all(session: Session = Depends(get_session)) -> dict[str, int]:
    count = 0
    for ev in session.scalars(select(Event).where(_UNACKED)):
        ev.acknowledged = True
        count += 1
    session.commit()
    return {"acknowledged": count}


# ---- artifactok (galéria) -------------------------------------------------------------------

@router.get("/artifacts", response_model=list[ArtifactOut])
def list_artifacts(kind: str | None = None, norad_id: int | None = None, recording_id: int | None = None,
                   limit: int = 100, offset: int = 0, session: Session = Depends(get_session)):
    q = select(Artifact).order_by(Artifact.created_at.desc(), Artifact.id.desc()).offset(offset).limit(min(limit, 500))
    if kind:
        q = q.where(Artifact.kind == kind)
    if norad_id is not None:
        q = q.where(Artifact.norad_id == norad_id)
    if recording_id is not None:
        q = q.where(Artifact.recording_id == recording_id)
    names = _names(session)
    return [_artifact_out(a, names) for a in session.scalars(q)]


def _artifact_file(request: Request, session: Session, artifact_id: int, thumbnail: bool,
                   download: bool = False) -> FileResponse:
    art = session.get(Artifact, artifact_id)
    sm = request.app.state.storage
    if art is not None and not sm.available(sm.root("artifacts", art.root_id)):
        raise HTTPException(503, "The storage location is not available")
    path = sm.artifact_path(art, thumbnail) if art else None
    if path is None:
        raise HTTPException(404, "File not found")
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                        filename=f"satrx_{artifact_id}_{path.name}" if download else None)


@router.get("/artifacts/{artifact_id}/file")
def artifact_file(artifact_id: int, request: Request, download: bool = False, session: Session = Depends(get_session)):
    return _artifact_file(request, session, artifact_id, False, download)


@router.get("/artifacts/{artifact_id}/thumbnail")
def artifact_thumbnail(artifact_id: int, request: Request, session: Session = Depends(get_session)):
    return _artifact_file(request, session, artifact_id, True)


@router.delete("/artifacts/{artifact_id}", status_code=204)
def delete_artifact(artifact_id: int, request: Request, session: Session = Depends(get_session)):
    art = session.get(Artifact, artifact_id)
    if art is None:
        raise HTTPException(404, "Artifact not found")
    storage_service.delete_artifact(session, request.app.state.storage, art)


# ---- külső eszközök -------------------------------------------------------------------------------

class ToolsIn(BaseModel):
    satdump_path: str | None = None
    rtl_sdr_path: str | None = None
    rtl_fm_path: str | None = None


def _tools_out(session: Session, settings: Settings) -> dict[str, Any]:
    info = find_satdump(session, settings)
    configured, source = configured_satdump(session, settings)
    out: dict[str, Any] = {"satdump_path": configured, "configured_from": source,
                           "satdump": {"found": info.found, "path": info.command[0] if info.command else None, "source": info.source}}
    for name in ("rtl_sdr", "rtl_fm"):
        command = find_rtl_tool(name, settings)
        out[f"{name}_path"] = getattr(settings.tools, f"{name}_path")
        out[name] = {"found": command is not None, "path": command[0] if command else None}
    return out


@router.get("/settings/tools")
def read_tools(session: Session = Depends(get_session), settings: Settings = Depends(get_settings_dep)):
    return _tools_out(session, settings)


@router.put("/settings/tools")
def write_tools(body: ToolsIn, session: Session = Depends(get_session), settings: Settings = Depends(get_settings_dep)):
    import shutil
    from pathlib import Path

    changes: dict[str, str | None] = {}
    for key in ("satdump_path", *RTL_KEYS):
        if key not in body.model_fields_set:
            continue                                       # a nem küldött útvonal változatlan marad
        value = (getattr(body, key) or "").strip() or None
        if value is not None and not Path(value).expanduser().is_file() and shutil.which(value) is None:
            raise HTTPException(422, f"No such executable: {value}")
        changes[key] = value
    save_tool_paths(session, changes)
    apply_stored_tools(session, settings)
    return _tools_out(session, settings)


# ---- SDR-állapot ---------------------------------------------------------------------------------

@router.get("/sdr")
def sdr_status(request: Request, refresh: bool = False) -> dict[str, Any]:
    """Melyik SDR-backend fut, szimulált-e, és látja-e a rendszer az eszközt (megnyitás nélkül)."""
    st = _state(request)
    rec = st.recorder.status()
    probe = st.sdr_probe.get(refresh=refresh) if rec.state == "idle" else None   # felvétel közben az eszköz foglalt
    return {"backend": st.settings.sdr.backend, "simulated": st.settings.sdr.backend in ("mock", "replay"),
            "recorder_state": rec.state, "probe": probe.to_dict() if probe else None}


# ---- állapotellenőrzés ----------------------------------------------------------------------

_SEVERITY = {"ok": 0, "warning": 1, "error": 2}


def _component(cid: str, status: str, code: str, message: str, details: dict | None = None) -> dict[str, Any]:
    return {"id": cid, "status": status, "code": code, "message": message, "details": details or {}}


@router.get("/health/detailed")
def health_detailed(request: Request, session: Session = Depends(get_session),
                    settings: Settings = Depends(get_settings_dep)) -> dict[str, Any]:
    st = _state(request)
    now = datetime.now(timezone.utc)
    components: list[dict[str, Any]] = []

    rec = st.recorder.status()
    if rec.last_error:
        components.append(_component("sdr", "error", "sdr_error", f"Last recording failed: {rec.last_error}",
                                     {"backend": settings.sdr.backend, "error": rec.last_error}))
    elif rec.state != "idle":
        components.append(_component("sdr", "ok", "sdr_" + rec.state, f"SDR {rec.state}",
                                     {"backend": settings.sdr.backend, "state": rec.state, "simulated": settings.sdr.backend in ("mock", "replay"),
                                      "elapsed_s": rec.elapsed_s, "duration_s": rec.duration_s}))
    else:
        probe = st.sdr_probe.get()
        details = {"backend": probe.backend, "simulated": probe.simulated, "state": "idle", "devices": probe.devices,
                   "probe_message": probe.message}
        if probe.status in ("mock", "replay"):
            components.append(_component("sdr", "warning", f"sdr_{probe.status}", probe.message, details))
        elif probe.status == "ok":
            components.append(_component("sdr", "ok", "sdr_idle", "SDR idle", details))
        else:
            components.append(_component("sdr", "error", f"sdr_{probe.status}", probe.message, details))

    nxt = session.scalars(select(Pass).where(Pass.status.in_(("planned", "partial")), Pass.rec_start > now - timedelta(seconds=1))
                          .order_by(Pass.rec_start)).first()
    nxt_info = None
    if nxt is not None:
        sat = session.get(Satellite, nxt.norad_id) if nxt.norad_id is not None else None
        name = sat.name if sat else (nxt.label or "-")
        nxt_info = {"pass_id": nxt.id, "satellite": name, "rec_start": nxt.rec_start.isoformat(),
                    "seconds_until": int((nxt.rec_start - now).total_seconds())}
    if st.scheduler.is_running:
        components.append(_component("scheduler", "ok", "scheduler_running", "Scheduler running", {"next_pass": nxt_info}))
    else:
        components.append(_component("scheduler", "warning", "scheduler_stopped", "Scheduler is not running",
                                     {"next_pass": nxt_info}))

    sats = session.scalars(select(Satellite).where(Satellite.enabled)).all()
    epochs = [s.tle_epoch for s in sats if s.tle_epoch]
    if not sats:
        components.append(_component("tle", "warning", "no_satellites", "No satellites are enabled"))
    elif epochs and now - min(epochs) > timedelta(hours=48):
        age_h = int((now - min(epochs)).total_seconds() // 3600)
        components.append(_component("tle", "warning", "tle_stale", f"Oldest TLE is {age_h} hours old", {"age_h": age_h}))
    else:
        components.append(_component("tle", "ok", "tle_ok", "TLEs are up to date", {"satellites": len(sats)}))

    components.append(_storage_component(st.storage, settings))

    problems = []
    if get_observer(session, settings) is None:
        problems.append(("observer_missing", "The observer location is not set"))
    if not session.scalar(select(func.count()).select_from(WatchRule).where(WatchRule.enabled, WatchRule.valid_to > now)):
        problems.append(("no_rules", "There are no active watch rules"))
    # csak akkor figyelmeztetünk, ha egy aktív szabály ténylegesen SatDumpot igénylő pipeline-t használ
    active_pipelines = session.scalars(select(Pipeline).join(WatchRule, WatchRule.pipeline_id == Pipeline.id)
                                       .where(WatchRule.enabled, WatchRule.valid_to > now)).all()
    uses_satdump = any(step.get("processor") == "meteor_lrpt" for p in active_pipelines for step in p.steps)
    if uses_satdump and not find_satdump(session, settings).found:
        problems.append(("satdump_missing", "SatDump was not found (needed by the Meteor LRPT pipeline)"))
    for err in st.registry.load_errors:
        problems.append(("plugin_load_failed", f"Plugin failed to load: {err}"))
    if problems:
        components.append(_component("config", "warning", problems[0][0], "; ".join(m for _, m in problems),
                                     {"problems": [c for c, _ in problems]}))
    else:
        components.append(_component("config", "ok", "config_ok", "Configuration is complete"))

    overall = max((c["status"] for c in components), key=lambda s: _SEVERITY[s])
    return {"status": overall, "components": components, "time": now.isoformat()}


def _storage_component(sm, settings: Settings) -> dict[str, Any]:
    usage = sm.usage()
    roots = [r for r in usage["roots"] if r["active"]]
    gb = lambda b: round(b / 1e9, 2)  # noqa: E731
    active_free = [r["free_bytes"] for r in roots if r["free_bytes"] is not None]
    free = min(active_free) if active_free else 0
    details = {"free_gb": gb(free), "used_gb": gb(usage["total_bytes"]), "quota_gb": settings.storage.quota_gb,
               "recordings_gb": gb(usage["recordings_bytes"]), "artifacts_gb": gb(usage["artifacts_bytes"]),
               "roots": [{"kind": r["kind"], "path": r["path"], "available": r["available"]} for r in roots]}
    missing = [r for r in roots if not r["available"]]
    if missing:
        details["unavailable"] = [r["path"] for r in missing]
        return _component("storage", "error", "storage_unavailable", "The storage location is not available", details)
    quota = settings.storage.quota_gb * 1e9
    used = usage["total_bytes"]
    if free < 0.5e9:
        return _component("storage", "error", "disk_full", "Less than 0.5 GB free disk space", details)
    if used > quota:
        return _component("storage", "error", "quota_exceeded", "The stored data exceed the storage quota", details)
    if used > 0.8 * quota or free < 2e9:
        return _component("storage", "warning", "storage_low", "Storage is running low", details)
    return _component("storage", "ok", "storage_ok", "Storage is fine", details)
