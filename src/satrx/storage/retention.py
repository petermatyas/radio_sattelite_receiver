"""Automatikus megőrzés: az IQ megőrzési ideje és a kvóta érvényesítése (a legrégebbi, nem védett, feldolgozott felvételektől)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from satrx.core.config import Settings
from satrx.core.events import EventLog
from satrx.core.models import Artifact, Job, Recording
from satrx.storage.manager import StorageManager
from satrx.storage.service import execute_plan, plan_delete

log = logging.getLogger(__name__)
QUOTA_TARGET = 0.9   # a kvóta túllépésekor eddig takarítunk, hogy ne minden felvétel után fusson


@dataclass
class RetentionResult:
    aged_out: int = 0
    over_quota: int = 0
    unprocessed_deleted: int = 0
    artifacts_deleted: int = 0
    freed_bytes: int = 0
    recording_ids: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    dry_run: bool = False

    def to_dict(self) -> dict:
        return {"aged_out": self.aged_out, "over_quota": self.over_quota, "unprocessed_deleted": self.unprocessed_deleted,
                "artifacts_deleted": self.artifacts_deleted, "freed_bytes": self.freed_bytes, "recording_ids": self.recording_ids, "notes": self.notes,
                "dry_run": self.dry_run}


def processed_recording_ids(session: Session) -> set[int]:
    """Olyan felvételek, amelyekből már készült eredmény (a processing job sikeresen lefutott)."""
    return {r for (r,) in session.execute(select(Job.recording_id).where(
        Job.kind == "processing", Job.status == "done", Job.recording_id.is_not(None)))}


def apply_retention(session: Session, sm: StorageManager, settings: Settings, events: EventLog | None = None,
                    busy: set[int] = frozenset(), now: datetime | None = None, dry_run: bool = False) -> RetentionResult:
    """Megőrzési szabályok:
    1. az IQ-t a `storage.iq_retention_days` napnál régebbi, **feldolgozott**, nem védett felvételekről töröljük
       (az eredmények megmaradnak); 0 = nincs korhatár;
    2. ha az összes adat meghaladja a kvótát, a legrégebbi feldolgozott, nem védett felvételek IQ-ját töröljük,
       a kvóta 90%-áig; ha ez nem elég, végső esetben a feldolgozatlanokét is (figyelmeztetéssel).
    A védett és az éppen használt felvételhez nem nyúl. `dry_run`: csak megmutatja, mit törölne.
    """
    now = now or datetime.now(timezone.utc)
    result = RetentionResult(dry_run=dry_run)
    processed = processed_recording_ids(session)
    total = {"bytes": sm.total_bytes()}
    taken: set[int] = set()

    def candidates(only_processed: bool) -> list[Recording]:
        rows = session.scalars(select(Recording).where(Recording.deleted.is_(False), Recording.protected.is_(False))
                               .order_by(Recording.started_at)).all()
        return [r for r in rows if r.id not in busy and r.id not in taken and ((r.id in processed) == only_processed)]

    def drop(recs: list[Recording]) -> int:
        if not recs:
            return 0
        plan = plan_delete(session, sm, recording_ids=[r.id for r in recs], scope="iq_only", busy=busy)
        freed, count = plan.freed_bytes, plan.counts["recordings"]
        if not dry_run:
            outcome = execute_plan(session, sm, plan)
            freed, count = outcome["freed_bytes"], outcome["recordings"]
        total["bytes"] -= freed
        result.freed_bytes += freed
        ids = [i.id for i in plan.items if not i.skipped]
        result.recording_ids += ids
        taken.update(ids)
        return count

    days = settings.storage.iq_retention_days
    if days and days > 0:
        cutoff = now - timedelta(days=days)
        result.aged_out = drop([r for r in candidates(True) if (r.ended_at or r.started_at) < cutoff])

    adays = settings.storage.artifact_retention_days
    if adays and adays > 0:                       # opcionális: a régi eredmények törlése (a védett felvételeké sosem)
        cutoff = now - timedelta(days=adays)
        protected = {r for (r,) in session.execute(select(Recording.id).where(Recording.protected.is_(True)))}
        old = [aid for aid, rid in session.execute(select(Artifact.id, Artifact.recording_id).where(Artifact.created_at < cutoff))
               if rid not in protected and rid not in busy]
        if old:
            plan = plan_delete(session, sm, artifact_ids=old, busy=busy)
            freed, count = plan.freed_bytes, plan.counts["artifacts"]
            if not dry_run:
                outcome = execute_plan(session, sm, plan)
                freed, count = outcome["freed_bytes"], outcome["artifacts"]
            total["bytes"] -= freed
            result.freed_bytes += freed
            result.artifacts_deleted = count

    quota = settings.storage.quota_gb * 1e9
    if quota > 0 and total["bytes"] > quota:
        target = quota * QUOTA_TARGET
        for only_processed in (True, False):
            for rec in candidates(only_processed):
                if total["bytes"] <= target:
                    break
                n = drop([rec])
                if only_processed:
                    result.over_quota += n
                else:
                    result.unprocessed_deleted += n
        if total["bytes"] > quota:
            result.notes.append("quota_still_exceeded")

    deleted = result.aged_out + result.over_quota + result.unprocessed_deleted + result.artifacts_deleted
    if events is not None and not dry_run and deleted:
        events.emit("info", "storage", "retention_deleted", f"Retention deleted {deleted} item(s)",
                    {"aged_out": result.aged_out, "over_quota": result.over_quota, "artifacts": result.artifacts_deleted,
                     "freed_bytes": result.freed_bytes})
        if result.unprocessed_deleted:
            events.emit("warning", "storage", "retention_unprocessed_deleted",
                        f"The quota forced deleting the raw IQ of {result.unprocessed_deleted} unprocessed recording(s)",
                        {"count": result.unprocessed_deleted})
    if "quota_still_exceeded" in result.notes:
        log.warning("Storage quota is still exceeded after the retention run")
    return result
