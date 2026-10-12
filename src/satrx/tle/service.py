from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from satrx.core.models import Satellite
from satrx.tle.client import Tle, TleError, fetch_tle

log = logging.getLogger(__name__)

TleFetcher = Callable[[int], Tle]


def apply_tle(sat: Satellite, tle: Tle) -> None:
    sat.name = tle.name
    sat.tle_line1 = tle.line1
    sat.tle_line2 = tle.line2
    sat.tle_epoch = tle.epoch


def add_satellite(session: Session, norad_id: int, fetcher: TleFetcher = fetch_tle) -> Satellite:
    if session.get(Satellite, norad_id) is not None:
        raise ValueError(f"Satellite {norad_id} already exists")
    sat = Satellite(norad_id=norad_id)
    apply_tle(sat, fetcher(norad_id))
    session.add(sat)
    session.commit()
    return sat


def refresh_tle(session: Session, sat: Satellite, fetcher: TleFetcher = fetch_tle) -> None:
    apply_tle(sat, fetcher(sat.norad_id))
    session.commit()


def refresh_stale(session: Session, max_age: timedelta = timedelta(hours=24),
                  fetcher: TleFetcher = fetch_tle, events=None) -> int:
    """Frissíti a régi TLE-jű műholdakat; egy hiba nem állítja meg a többit."""
    now = datetime.now(timezone.utc)
    updated = 0
    for sat in session.scalars(select(Satellite)):
        epoch = sat.tle_epoch
        if epoch is not None and now - epoch < max_age:
            continue
        try:
            refresh_tle(session, sat, fetcher)
            updated += 1
        except TleError as exc:
            log.warning("TLE refresh failed for %s: %s", sat.norad_id, exc)
            if events is not None:
                events.emit("warning", "tle", "tle_refresh_failed", f"TLE refresh failed for {sat.name}: {exc}",
                            {"norad_id": sat.norad_id, "error": str(exc)})
    return updated
