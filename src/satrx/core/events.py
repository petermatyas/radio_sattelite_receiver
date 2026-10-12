from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from satrx.core.models import Event

log = logging.getLogger(__name__)

_LEVELS = {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}
MAX_EVENTS = 5000
MAX_AGE = timedelta(days=30)


class EventLog:
    """Az események egyetlen belépési pontja: adatbázisba ír (a UI ezt mutatja) és naplófájlba is.

    A `code` géppel olvasható (a UI ebből fordít és ad javaslatot), a `message` angol tartalék szöveg.
    """

    def __init__(self, session_factory: sessionmaker[Session]):
        self._sessions = session_factory

    def emit(self, level: str, source: str, code: str, message: str, details: dict[str, Any] | None = None,
             pass_id: int | None = None, job_id: int | None = None, recording_id: int | None = None) -> None:
        log.log(_LEVELS[level], "[%s] %s: %s", source, code, message)
        try:
            with self._sessions() as session:
                session.add(Event(level=level, source=source, code=code, message=message,
                                  details=details or {}, pass_id=pass_id, job_id=job_id,
                                  recording_id=recording_id))
                session.commit()
        except Exception:  # noqa: BLE001 - az eseménynapló hibája sosem állíthat meg felvételt
            log.exception("Could not persist event %s", code)

    def prune(self, now: datetime | None = None) -> int:
        """30 napnál régebbi és az 5000. utáni események törlése; a nem nyugtázott hibák megmaradnak."""
        now = now or datetime.now(timezone.utc)
        keep_unacked_errors = (Event.level == "error") & (Event.acknowledged.is_(False))
        with self._sessions() as session:
            removed = session.execute(delete(Event).where(Event.ts < now - MAX_AGE, ~keep_unacked_errors)).rowcount
            total = session.scalar(select(func.count()).select_from(Event)) or 0
            if total > MAX_EVENTS:
                cutoff = session.scalars(select(Event.id).order_by(Event.id.desc()).offset(MAX_EVENTS).limit(1)).first()
                if cutoff is not None:
                    removed += session.execute(delete(Event).where(Event.id <= cutoff, ~keep_unacked_errors)).rowcount
            session.commit()
            return removed
