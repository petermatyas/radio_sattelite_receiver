"""Átvonulások ütközésfeloldása erőforrásokra (SDR eszközökre). Tiszta, I/O nélküli logika."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Candidate:
    key: int
    aos: datetime
    los: datetime
    max_elevation_deg: float
    priority: int
    allow_partial: bool
    buffer_before_s: int
    buffer_after_s: int
    resources: tuple[str, ...]  # az eszközök, amelyek a profilt tudják venni; üres = nem támogatott

    @property
    def window(self) -> tuple[datetime, datetime]:
        return (self.aos - timedelta(seconds=self.buffer_before_s),
                self.los + timedelta(seconds=self.buffer_after_s))

    @property
    def score(self) -> float:
        return self.priority * 1000 + self.max_elevation_deg * 10 + (self.los - self.aos).total_seconds() / 60


@dataclass(frozen=True)
class Fixed:
    """Már foglalt idősáv (pl. épp futó felvétel)."""
    resource: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Decision:
    status: str  # planned | partial | skipped_conflict | unsupported
    resource: str | None = None
    rec_start: datetime | None = None
    rec_end: datetime | None = None


def _free_segments(window: tuple[datetime, datetime], busy: list[tuple[datetime, datetime]]):
    start, end = window
    cursor = start
    for b_start, b_end in sorted(busy):
        if b_end <= cursor or b_start >= end:
            continue
        if b_start > cursor:
            yield cursor, b_start
        cursor = max(cursor, b_end)
    if cursor < end:
        yield cursor, end


def _overlap_s(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> float:
    return max(0.0, (min(a[1], b[1]) - max(a[0], b[0])).total_seconds())


def plan(candidates: list[Candidate], resources: list[str], fixed: list[Fixed] | None = None,
         min_partial_s: float = 60) -> dict[int, Decision]:
    """Mohó kiosztás pontszám szerint: a legmagasabb pontszámú átvonulás kapja meg először az eszközt.

    - teljes ablak elfér valamelyik kompatibilis eszközön -> planned
    - különben, ha engedélyezett, a legnagyobb szabad szelet (legalább min_partial_s hasznos jelidővel) -> partial
    - különben skipped_conflict; ha nincs kompatibilis eszköz: unsupported
    """
    busy: dict[str, list[tuple[datetime, datetime]]] = {r: [] for r in resources}
    for f in fixed or []:
        busy.setdefault(f.resource, []).append((f.start, f.end))

    result: dict[int, Decision] = {}
    for c in sorted(candidates, key=lambda c: (-c.score, c.aos, c.key)):
        usable = [r for r in c.resources if r in busy]
        if not usable:
            result[c.key] = Decision("unsupported")
            continue
        window = c.window

        full = next((r for r in usable if next(_free_segments(window, busy[r]), None) == window), None)
        if full is not None:
            busy[full].append(window)
            result[c.key] = Decision("planned", full, *window)
            continue

        best: tuple[float, str, tuple[datetime, datetime]] | None = None
        if c.allow_partial:
            for r in usable:
                for seg in _free_segments(window, busy[r]):
                    useful = _overlap_s(seg, (c.aos, c.los))
                    if useful >= min_partial_s and (best is None or useful > best[0]):
                        best = (useful, r, seg)
        if best is None:
            result[c.key] = Decision("skipped_conflict")
        else:
            _, r, seg = best
            busy[r].append(seg)
            result[c.key] = Decision("partial", r, *seg)
    return result
