"""Adatok törlése: előre kiszámolt terv (száraz futtatás), végrehajtás védelmekkel; közös az API és a megőrzési szabály számára."""
from __future__ import annotations

import logging
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from satrx.core.models import Artifact, Job, Recording
from satrx.recording import sigmf
from satrx.storage.manager import StorageManager

log = logging.getLogger(__name__)


@dataclass
class DeleteItem:
    type: str                 # recording | artifact
    id: int
    action: str               # delete_iq | delete_all | delete_artifact
    bytes: int = 0
    skipped: str | None = None  # not_found | busy | protected | no_iq | storage_unavailable


@dataclass
class DeletePlan:
    items: list[DeleteItem] = field(default_factory=list)

    @property
    def freed_bytes(self) -> int:
        return sum(i.bytes for i in self.items if not i.skipped)

    @property
    def counts(self) -> dict[str, int]:
        done = [i for i in self.items if not i.skipped]
        return {"recordings": sum(i.type == "recording" for i in done), "artifacts": sum(i.type == "artifact" for i in done),
                "skipped": sum(1 for i in self.items if i.skipped)}

    def to_dict(self) -> dict:
        return {"items": [asdict(i) for i in self.items], "freed_bytes": self.freed_bytes, **self.counts}


def busy_recording_ids(session: Session, recorder_status) -> set[int]:
    """Amelyik felvételt épp írnak vagy feldolgoznak (a fájljait nem szabad törölni vagy mozgatni)."""
    busy = {r for (r,) in session.execute(select(Job.recording_id).where(
        Job.kind == "processing", Job.status.in_(("queued", "running")), Job.recording_id.is_not(None)))}
    if recorder_status is not None and recorder_status.state != "idle" and recorder_status.recording_id is not None:
        busy.add(recorder_status.recording_id)
    return busy


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _recording_files(sm: StorageManager, rec: Recording) -> list[Path]:
    base = sm.recording_base(rec)
    return [sigmf.data_path(base), sigmf.meta_path(base), sigmf.orbit_path(base)]


def _artifact_files(sm: StorageManager, art: Artifact) -> list[Path]:
    root = sm.root("artifacts", art.root_id).path
    return [root / rel for rel in (art.path, art.thumbnail_path) if rel]


def plan_delete(session: Session, sm: StorageManager, *, recording_ids=(), artifact_ids=(), scope: str = "iq_only",
                include_protected: bool = False, busy: set[int] = frozenset()) -> DeletePlan:
    """Mit törölnénk és mennyi helyet szabadítana fel (a törlés előtt megjeleníthető)."""
    if scope not in ("iq_only", "all"):
        raise ValueError("scope must be 'iq_only' or 'all'")
    plan = DeletePlan()
    covered_artifacts: set[int] = set()
    for rid in dict.fromkeys(recording_ids):
        rec = session.get(Recording, rid)
        if rec is None:
            plan.items.append(DeleteItem("recording", rid, "delete_all", skipped="not_found"))
            continue
        action = "delete_iq" if scope == "iq_only" else "delete_all"
        item = DeleteItem("recording", rid, action)
        if rid in busy:
            item.skipped = "busy"
        elif rec.protected and not include_protected:
            item.skipped = "protected"
        elif not sm.recording_available(rec):
            item.skipped = "storage_unavailable"
        elif scope == "iq_only":
            if rec.deleted:
                item.skipped = "no_iq"
            else:
                item.bytes = rec.size_bytes
        else:
            item.bytes = (0 if rec.deleted else rec.size_bytes) + sum(
                _file_size(p) for p in _recording_files(sm, rec)[1:])
            arts = session.scalars(select(Artifact).where(Artifact.recording_id == rid)).all()
            item.bytes += sum(a.size_bytes for a in arts)
            covered_artifacts.update(a.id for a in arts)
        plan.items.append(item)
    for aid in dict.fromkeys(artifact_ids):
        if aid in covered_artifacts:
            continue
        art = session.get(Artifact, aid)
        if art is None:
            plan.items.append(DeleteItem("artifact", aid, "delete_artifact", skipped="not_found"))
        elif art.recording_id in busy:
            plan.items.append(DeleteItem("artifact", aid, "delete_artifact", skipped="busy"))
        elif not sm.available(sm.root("artifacts", art.root_id)):
            plan.items.append(DeleteItem("artifact", aid, "delete_artifact", skipped="storage_unavailable"))
        else:
            plan.items.append(DeleteItem("artifact", aid, "delete_artifact", bytes=art.size_bytes))
    return plan


def _unlink(path: Path) -> None:
    path.unlink(missing_ok=True)


def delete_artifact_files(sm: StorageManager, art: Artifact) -> None:
    for p in _artifact_files(sm, art):
        _unlink(p)
        parent = p.parent
        # a job üres kimeneti könyvtárának takarítása (rec<id>/job<id>), a gyökérig nem megyünk fel
        try:
            root = sm.root("artifacts", art.root_id).path
            while parent != root and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        except OSError:
            pass


def execute_plan(session: Session, sm: StorageManager, plan: DeletePlan) -> dict:
    """A terv végrehajtása; a kihagyott elemekről és a hibákról jelentést ad. Csak nyilvántartott fájlokat töröl."""
    freed, done, errors = 0, {"recordings": 0, "artifacts": 0}, []
    for item in plan.items:
        if item.skipped:
            continue
        try:
            if item.type == "artifact":
                art = session.get(Artifact, item.id)
                delete_artifact_files(sm, art)
                session.delete(art)
                done["artifacts"] += 1
            else:
                rec = session.get(Recording, item.id)
                if item.action == "delete_iq":
                    _unlink(sigmf.data_path(sm.recording_base(rec)))
                    rec.deleted = True
                else:
                    for p in _recording_files(sm, rec):
                        _unlink(p)
                    arts = session.scalars(select(Artifact).where(Artifact.recording_id == rec.id)).all()
                    for art in arts:
                        delete_artifact_files(sm, art)
                    for root in {sm.root("artifacts", a.root_id).path for a in arts}:
                        shutil.rmtree(root / f"rec{rec.id}", ignore_errors=True)
                    session.delete(rec)
                done["recordings"] += 1
            session.commit()
            freed += item.bytes
        except OSError as exc:
            session.rollback()
            errors.append({"type": item.type, "id": item.id, "error": str(exc)})
            log.warning("Could not delete %s %s: %s", item.type, item.id, exc)
    return {"freed_bytes": freed, **done, "errors": errors, "skipped": plan.counts["skipped"]}


def delete_recording(session: Session, sm: StorageManager, rec: Recording) -> None:
    """Egy felvétel teljes törlése (régi API-hívásokhoz): IQ, metaadat, pályaadat, eredmények."""
    execute_plan(session, sm, plan_delete(session, sm, recording_ids=[rec.id], scope="all", include_protected=True))


def delete_artifact(session: Session, sm: StorageManager, art: Artifact) -> None:
    execute_plan(session, sm, plan_delete(session, sm, artifact_ids=[art.id]))


def artifacts_size(session: Session, recording_id: int) -> int:
    return int(session.scalar(select(func.coalesce(func.sum(Artifact.size_bytes), 0)).where(
        Artifact.recording_id == recording_id)) or 0)
