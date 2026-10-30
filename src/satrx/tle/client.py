from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger(__name__)

CELESTRAK_URL = "https://celestrak.org/NORAD/elements/gp.php"


class TleError(Exception):
    pass


@dataclass(frozen=True)
class Tle:
    name: str
    line1: str
    line2: str

    @property
    def norad_id(self) -> int:
        return int(self.line1[2:7])

    @property
    def epoch(self) -> datetime:
        year = int(self.line1[18:20])
        year += 2000 if year < 57 else 1900
        day_of_year = float(self.line1[20:32])
        return datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=day_of_year - 1)


def parse_tle(text: str, norad_id: int) -> Tle:
    """CelesTrak 3 soros (név + 2 TLE sor) válasz feldolgozása."""
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 3 or not lines[1].startswith("1 ") or not lines[2].startswith("2 "):
        raise TleError(f"No TLE data for NORAD {norad_id}: {text.strip()[:80]!r}")
    tle = Tle(name=lines[0].strip(), line1=lines[1], line2=lines[2])
    if tle.norad_id != norad_id:
        raise TleError(f"Requested NORAD {norad_id}, got {tle.norad_id}")
    return tle


def fetch_tle(norad_id: int, client: httpx.Client | None = None) -> Tle:
    own = client is None
    client = client or httpx.Client(timeout=15)
    try:
        resp = client.get(CELESTRAK_URL, params={"CATNR": norad_id, "FORMAT": "tle"})
        resp.raise_for_status()
        return parse_tle(resp.text, norad_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            # a CelesTrak "No GP data found" válasza: nincs friss pályaadat (nem követett, lebomlott vagy nem létező objektum)
            raise TleError(f"CelesTrak has no current orbital data for NORAD {norad_id} (the satellite may have decayed, "
                           "is no longer tracked, or the ID is wrong)") from exc
        raise TleError(f"TLE download failed for NORAD {norad_id}: HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise TleError(f"TLE download failed for NORAD {norad_id}: {exc}") from exc
    finally:
        if own:
            client.close()
