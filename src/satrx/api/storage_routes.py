"""Adatkezelés: tárhely-használat, a mentés helyének módosítása, áthelyezés, törlés, védelem, letöltés, exportok."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from satrx.api.deps import get_session, get_settings_dep, require_auth
from satrx.core.config import Settings
from satrx.core.models import Artifact, Job, Recording, Satellite
from satrx.recording import sigmf
from satrx.storage.export import ExportSpec
from satrx.storage.retention import apply_retention, processed_recording_ids
from satrx.storage.service import busy_recording_ids, execute_plan, plan_delete

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


def _sm(request: Request):
    return request.app.state.storage


def _busy(request: Request, session: Session) -> set[int]:
    return busy_recording_ids(session, request.app.state.recorder.status())


def _blockers(request: Request, session: Session) -> list[str]:
    """Mi akadályozza a mentési hely módosítását / áthelyezést (felvétel, feldolgozás, folyamatban lévő áthelyezés)."""
    reasons = []
    if request.app.state.recorder.status().state != "idle":
        reasons.append("recording_in_progress")
    if session.scalar(select(func.count()).select_from(Job).where(
            Job.kind == "processing", Job.status.in_(("queued", "running")))):
        reasons.append("processing_in_progress")
    if request.app.state.mover.running:
        reasons.append("move_in_progress")
    return reasons


# ---- áttekintés és érvényesítés ------------------------------------------------------------------------------

@router.get("/storage")
def storage_overview(request: Request) -> dict[str, Any]:
    return {**_sm(request).usage(), "move": request.app.state.mover.status()}


class PathIn(BaseModel):
    path: str


@router.post("/storage/validate")
def validate_path(body: PathIn, request: Request) -> dict[str, Any]:
    return _sm(request).validate_path(body.path)


class RootsIn(BaseModel):
    recordings_path: str | None = None
    artifacts_path: str | None = None


@router.put("/storage/roots")
def set_roots(body: RootsIn, request: Request, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Az aktív mentési mappák módosítása: az új adat ide kerül, a régi a helyén marad (vagy áthelyezhető)."""
    if not body.recordings_path and not body.artifacts_path:
        raise HTTPException(422, "No path given")
    reasons = _blockers(request, session)
    if reasons:
        raise HTTPException(409, {"code": "storage_locked", "reasons": reasons})
    sm = _sm(request)
    checks = {k: sm.validate_path(v) for k, v in (("recordings", body.recordings_path), ("artifacts", body.artifacts_path)) if v}
    bad = {k: c for k, c in checks.items() if not c["ok"]}
    if bad:
        raise HTTPException(422, {"code": "invalid_path", "checks": bad})
    for kind, value in (("recordings", body.recordings_path), ("artifacts", body.artifacts_path)):
        if value:
            sm.register_root(kind, value)
    request.app.state.events.emit("info", "storage", "storage_root_changed", "The storage location was changed",
                                  {"recordings": body.recordings_path, "artifacts": body.artifacts_path})
    return {**sm.usage(), "move": request.app.state.mover.status(), "checks": checks}


# ---- áthelyezés -------------------------------------------------------------------------------------------------

class MoveIn(BaseModel):
    kind: Literal["recordings", "artifacts"]


@router.post("/storage/move", status_code=202)
def start_move(body: MoveIn, request: Request, session: Session = Depends(get_session)) -> dict[str, Any]:
    reasons = [r for r in _blockers(request, session) if r != "processing_in_progress"]
    if "recording_in_progress" in reasons or "move_in_progress" in reasons:
        raise HTTPException(409, {"code": "storage_locked", "reasons": reasons})
    try:
        return request.app.state.mover.start(body.kind)
    except ValueError as exc:
        raise HTTPException(409, {"code": "move_failed_to_start", "message": str(exc)})


@router.get("/storage/move")
def move_status(request: Request) -> dict[str, Any]:
    return request.app.state.mover.status()


@router.post("/storage/move/cancel", status_code=204)
def cancel_move(request: Request):
    request.app.state.mover.cancel()


# ---- lista és törlés --------------------------------------------------------------------------------------------

@router.get("/storage/recordings")
def storage_recordings(request: Request, norad_id: int | None = None, since: datetime | None = None,
                       until: datetime | None = None, state: Literal["all", "with_iq", "without_iq", "protected", "unprocessed"] = "all",
                       sort: Literal["date", "size", "iq", "results"] = "date", order: Literal["asc", "desc"] = "desc",
                       limit: int = 200, offset: int = 0, session: Session = Depends(get_session)) -> dict[str, Any]:
    sm = _sm(request)
    q = select(Recording)
    if norad_id is not None:
        q = q.where(Recording.norad_id == norad_id)
    if since:
        q = q.where(Recording.started_at >= since)
    if until:
        q = q.where(Recording.started_at <= until)
    recs = session.scalars(q).all()
    art = {rid: (n, b) for rid, n, b in session.execute(select(
        Artifact.recording_id, func.count(), func.coalesce(func.sum(Artifact.size_bytes), 0)).group_by(Artifact.recording_id))}
    processed = processed_recording_ids(session)
    names = dict(session.execute(select(Satellite.norad_id, Satellite.name)).all())
    avail: dict[int | None, bool] = {}
    items = []
    for r in recs:
        n_art, b_art = art.get(r.id, (0, 0))
        if r.root_id not in avail:
            avail[r.root_id] = sm.available(sm.root("recordings", r.root_id))
        iq = 0 if r.deleted else r.size_bytes
        items.append({"id": r.id, "started_at": r.started_at, "norad_id": r.norad_id, "satellite_name": names.get(r.norad_id) or r.label,
                      "center_freq_hz": r.center_freq_hz, "sample_rate_hz": r.sample_rate_hz, "iq_bytes": iq,
                      "iq_available": not r.deleted, "artifact_count": n_art, "artifact_bytes": b_art,
                      "total_bytes": iq + b_art, "protected": r.protected, "processed": r.id in processed,
                      "root_id": r.root_id, "storage_available": avail[r.root_id], "has_orbit": bool(r.orbit_meta_path), "source": r.source})
    flt = {"all": lambda i: True, "with_iq": lambda i: i["iq_available"], "without_iq": lambda i: not i["iq_available"],
           "protected": lambda i: i["protected"], "unprocessed": lambda i: not i["processed"] and i["iq_available"]}[state]
    items = [i for i in items if flt(i)]
    key = {"date": lambda i: i["started_at"], "size": lambda i: i["total_bytes"], "iq": lambda i: i["iq_bytes"],
           "results": lambda i: i["artifact_bytes"]}[sort]
    items.sort(key=key, reverse=(order == "desc"))
    return {"total": len(items), "total_bytes": sum(i["total_bytes"] for i in items),
            "iq_bytes": sum(i["iq_bytes"] for i in items), "artifact_bytes": sum(i["artifact_bytes"] for i in items),
            "items": items[offset: offset + min(limit, 1000)]}


class DeleteIn(BaseModel):
    recording_ids: list[int] = Field(default_factory=list)
    artifact_ids: list[int] = Field(default_factory=list)
    scope: Literal["iq_only", "all"] = "iq_only"
    dry_run: bool = False
    include_protected: bool = False


@router.post("/storage/delete")
def delete_data(body: DeleteIn, request: Request, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Törlés. `dry_run`: csak a tervet adja (mit és mennyi helyet szabadítana fel); a védett és az éppen használt elemet kihagyja."""
    if not body.recording_ids and not body.artifact_ids:
        raise HTTPException(422, "Nothing selected")
    sm = _sm(request)
    plan = plan_delete(session, sm, recording_ids=body.recording_ids, artifact_ids=body.artifact_ids, scope=body.scope,
                       include_protected=body.include_protected, busy=_busy(request, session))
    if body.dry_run:
        return {"dry_run": True, **plan.to_dict()}
    result = execute_plan(session, sm, plan)
    request.app.state.events.emit(
        "info", "storage", "data_deleted", f"Deleted {result['recordings']} recording(s) and {result['artifacts']} result(s)",
        {"scope": body.scope, **{k: v for k, v in result.items() if k != "errors"}, "errors": len(result["errors"])})
    return {"dry_run": False, **plan.to_dict(), "result": result}


class RetentionIn(BaseModel):
    dry_run: bool = True


@router.post("/storage/retention")
def run_retention(body: RetentionIn, request: Request, session: Session = Depends(get_session),
                  settings: Settings = Depends(get_settings_dep)) -> dict[str, Any]:
    """A megőrzési szabályok kézi futtatása (alapból száraz futtatás: csak megmutatja, mit törölne)."""
    result = apply_retention(session, _sm(request), settings, request.app.state.events, busy=_busy(request, session),
                             dry_run=body.dry_run)
    return result.to_dict()


class ProtectIn(BaseModel):
    protected: bool


@router.patch("/recordings/{recording_id}")
def protect_recording(recording_id: int, body: ProtectIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    rec = session.get(Recording, recording_id)
    if rec is None:
        raise HTTPException(404, "Recording not found")
    rec.protected = body.protected
    session.commit()
    return {"id": rec.id, "protected": rec.protected}


# ---- letöltés ------------------------------------------------------------------------------------------------------

@router.get("/recordings/{recording_id}/download")
def download_recording_part(recording_id: int, request: Request, part: Literal["iq", "meta", "orbit"] = "iq",
                            session: Session = Depends(get_session)):
    """Egy fájl streamelt letöltése (HTTP Range: a megszakadt letöltés folytatható)."""
    rec = session.get(Recording, recording_id)
    if rec is None:
        raise HTTPException(404, "Recording not found")
    sm = _sm(request)
    if not sm.recording_available(rec):
        raise HTTPException(503, "The storage location is not available")
    base = sm.recording_base(rec)
    path = {"iq": sigmf.data_path(base), "meta": sigmf.meta_path(base), "orbit": sigmf.orbit_path(base)}[part]
    if not path.is_file():
        raise HTTPException(404, "File not found (the raw IQ may have been deleted)" if part == "iq" else "File not found")
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)


# ---- exportok ---------------------------------------------------------------------------------------------------------

class ExportIn(BaseModel):
    recording_ids: list[int] = Field(default_factory=list)
    artifact_ids: list[int] = Field(default_factory=list)
    include_iq: bool = True
    include_artifacts: bool = True
    mode: Literal["zip", "directory"] = "zip"
    directory: str | None = None
    move: bool = False


@router.post("/exports", status_code=202)
def create_export(body: ExportIn, request: Request) -> dict[str, Any]:
    try:
        return request.app.state.exporter.submit(ExportSpec(**body.model_dump()))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.get("/exports/{export_id}")
def export_status(export_id: str, request: Request) -> dict[str, Any]:
    job = request.app.state.exporter.status(export_id)
    if job is None:
        raise HTTPException(404, "Export not found")
    return job


@router.post("/exports/{export_id}/cancel", status_code=204)
def export_cancel(export_id: str, request: Request):
    if not request.app.state.exporter.cancel(export_id):
        raise HTTPException(404, "Export not found")


@router.get("/exports/{export_id}/download")
def export_download(export_id: str, request: Request):
    path = request.app.state.exporter.zip_path(export_id)
    if path is None:
        raise HTTPException(404, "The export is not ready or no longer available")
    return FileResponse(path, media_type="application/zip", filename=path.name.removesuffix(".part"))
