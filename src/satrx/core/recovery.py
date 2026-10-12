"""Összeomlás / áramkimaradás utáni helyreállítás induláskor.

Tiszta leállításnál a felvételkezelő lezárja a futó felvételt, ezért ilyenkor nincs teendő. Hirtelen leállás után
(áramkimaradás a Pi-n, kill) viszont félbemaradt állapotok maradnak: "felvétel" státuszú átvonulás, "fut" jobok, lezáratlan
felvételek metaadat nélkül. Ezeket rendbe tesszük, a megmaradt IQ pedig így is feldolgozható.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from satrx.core.events import EventLog
from satrx.core.models import Job, Pass, Recording
from satrx.recording import sigmf
from satrx.storage.manager import StorageManager

log = logging.getLogger(__name__)
INTERRUPTED = "Interrupted by a restart or crash"


def _cleanup_part_files(roots: list[Path]) -> int:
    removed = 0
    for root in roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*.part"):
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def recover_after_crash(session_factory: sessionmaker[Session], storage: StorageManager, events: EventLog | None,
                        extra_part_dirs: list[Path] | None = None) -> dict[str, int]:
    """Visszaadja, mit javított; ha semmit, akkor a leállás tiszta volt."""
    now = datetime.now(timezone.utc)
    result = {"passes": 0, "jobs": 0, "recordings": 0, "repaired_meta": 0, "part_files": 0}
    with session_factory() as s:
        for p in s.scalars(select(Pass).where(Pass.status.in_(("recording", "processing")))):
            p.status = "failed"
            result["passes"] += 1
        for j in s.scalars(select(Job).where(Job.status.in_(("running", "queued", "pending")))):
            j.status, j.error, j.finished_at = "failed", INTERRUPTED, now
            result["jobs"] += 1
        for rec in s.scalars(select(Recording).where(Recording.ended_at.is_(None))):
            result["recordings"] += 1
            rec.ended_at = now
            try:
                base = storage.recording_base(rec)
                data = sigmf.data_path(base)
                if data.exists():
                    rec.size_bytes = data.stat().st_size
                    if not sigmf.meta_path(base).exists():
                        _write_meta(base, rec)
                        result["repaired_meta"] += 1
                else:
                    rec.deleted = True
            except OSError as exc:
                log.warning("Could not inspect recording %s: %s", rec.id, exc)
        s.commit()
    roots = [r.path for r in storage.roots()] + list(extra_part_dirs or [])
    result["part_files"] = _cleanup_part_files(roots)

    if any(result.values()) and events is not None:
        events.emit("warning", "system", "recovered_after_crash",
                    "The service was not stopped cleanly; unfinished work was marked and repaired", dict(result))
    return result


def _write_meta(base: Path, rec: Recording) -> None:
    """A lezáratlan felvétel SigMF metaadata (a felvétel közben nem készül el): a mintaszámot a fájlméret adja."""
    import json

    meta = {"global": {"core:datatype": rec.sample_format, "core:sample_rate": rec.sample_rate_hz, "core:version": sigmf.SIGMF_VERSION,
                       "core:recorder": "satrx", "core:description": "Recovered after an unclean shutdown"},
            "captures": [{"core:sample_start": 0, "core:frequency": rec.center_freq_hz}], "annotations": []}
    sigmf.meta_path(base).write_text(json.dumps(meta, indent=2), encoding="utf-8")
