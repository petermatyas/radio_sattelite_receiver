"""Időalapú események előfordulásai egy időablakban (ismétlődés, helyi időzóna, nyári időszámítás). Tiszta függvények."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_OCCURRENCES = 1000
RECURRENCES = ("none", "daily", "weekly")


class EventLike(Protocol):
    start_at: datetime
    duration_s: int
    recurrence: str
    weekdays: list[int]
    repeat_until: datetime | None
    timezone: str


def get_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(f"Unknown time zone: {name}") from exc


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def occurrences(ev: EventLike, window_start: datetime, window_end: datetime) -> list[tuple[datetime, datetime]]:
    """Az esemény (kezdet, vég) párjai UTC-ben, amelyek átfedik a [window_start, window_end) ablakot.

    Az ismétlődés a helyi falióra szerint értendő: egy "minden nap 18:00" esemény nyári időszámításváltás után is
    18:00-kor indul helyi idő szerint (UTC-ben egy órával eltolódik).
    """
    start = _utc(ev.start_at)
    window_start, window_end = _utc(window_start), _utc(window_end)
    dur = timedelta(seconds=ev.duration_s)
    until = _utc(ev.repeat_until) if ev.repeat_until else None

    def overlaps(s: datetime, e: datetime) -> bool:
        return e > window_start and s < window_end

    if ev.recurrence == "none":
        return [(start, start + dur)] if overlaps(start, start + dur) else []

    tz = get_zone(ev.timezone)
    first_local = start.astimezone(tz)
    weekdays = set(ev.weekdays) if ev.recurrence == "weekly" and ev.weekdays else {first_local.weekday()}
    # a kezdő napot egy nappal korábbra húzzuk: a már futó, éjfélen átnyúló előfordulás is számít
    day: date = max(first_local.date(), (window_start - dur).astimezone(tz).date() - timedelta(days=1))
    last_day = window_end.astimezone(tz).date() + timedelta(days=1)
    out: list[tuple[datetime, datetime]] = []
    while day <= last_day and len(out) < MAX_OCCURRENCES:
        if ev.recurrence == "daily" or day.weekday() in weekdays:
            local = datetime(day.year, day.month, day.day, first_local.hour, first_local.minute, first_local.second,
                             first_local.microsecond, tzinfo=tz)
            s = local.astimezone(timezone.utc)
            if s >= start and (until is None or s <= until) and overlaps(s, s + dur):
                out.append((s, s + dur))
        day += timedelta(days=1)
    return out


def next_occurrences(ev: EventLike, after: datetime, count: int = 5) -> list[datetime]:
    """A következő `count` kezdési időpont (UTC) az `after` után (előnézethez)."""
    found: list[datetime] = []
    window = timedelta(days=14)
    cursor = _utc(after)
    for _ in range(30):                      # legfeljebb ~1 év előre
        for s, _e in occurrences(ev, cursor, cursor + window):
            if s >= after and s not in found:
                found.append(s)
        if len(found) >= count or ev.recurrence == "none":
            break
        cursor += window
    return sorted(found)[:count]
