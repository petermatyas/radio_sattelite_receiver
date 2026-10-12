"""Élő waterfall: a felvétel közben számolt spektrumsorokat WebSocketen küldjük a böngészőknek.

A számítás csak akkor fut, ha van feliratkozó (nincs néző = nincs FFT). A felvételi szál soha nem blokkolódik:
a sorok a kliensek korlátos sorába kerülnek, és ha egy kliens lemarad, a legrégebbi sorokat eldobjuk.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

import numpy as np

from satrx.dsp.spectrum import quantize_db, spectrum_rows

log = logging.getLogger(__name__)
QUEUE_SIZE = 200
DB_LO, DB_HI = -120.0, 0.0   # a bájtra képezés tartománya; a böngésző ebből állítja a megjelenítési szintet


class Subscriber:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=QUEUE_SIZE)


def _put(sub: Subscriber, item: tuple[str, Any]) -> None:
    if sub.queue.full():
        sub.queue.get_nowait()
    sub.queue.put_nowait(item)


class LiveSpectrum:
    def __init__(self, fft_size: int = 1024, rows_per_s: float = 10.0):
        self.fft_size = fft_size
        self.rows_per_s = rows_per_s
        self._lock = threading.Lock()
        self._subs: set[Subscriber] = set()
        self._header: dict[str, Any] | None = None
        self._center_hz: int | None = None

    @property
    def has_subscribers(self) -> bool:
        return bool(self._subs)

    @property
    def active(self) -> bool:
        return self._header is not None

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> Subscriber:
        sub = Subscriber(loop)
        with self._lock:
            self._subs.add(sub)
            header = self._header
        if header is not None:                 # menet közben csatlakozó néző azonnal megkapja a fejlécet
            sub.queue.put_nowait(("json", header))
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        with self._lock:
            self._subs.discard(sub)

    # ---- a felvételi szál hívja -------------------------------------------------------------------------

    def begin(self, sample_rate_hz: int, center_hz: int, label: str = "") -> None:
        header = {"type": "header", "bins": self.fft_size, "sample_rate_hz": sample_rate_hz,
                  "center_freq_hz": center_hz, "rows_per_s": self.rows_per_s, "db_lo": DB_LO, "db_hi": DB_HI,
                  "label": label}
        with self._lock:
            self._header, self._center_hz = header, center_hz
        self._broadcast(("json", header))

    def push(self, samples: np.ndarray, sample_rate_hz: int, center_hz: int) -> None:
        if not self._subs or self._header is None:
            return
        if self._center_hz is not None and abs(center_hz - self._center_hz) >= 500:   # Doppler-újrahangolás
            self._center_hz = center_hz
            self._broadcast(("json", {"type": "center", "center_freq_hz": center_hz}))
        n_rows = max(1, round(len(samples) / sample_rate_hz * self.rows_per_s))
        rows = spectrum_rows(samples, self.fft_size, n_rows)
        if len(rows):
            self._broadcast(("rows", quantize_db(rows, DB_LO, DB_HI).tobytes()))

    def end(self) -> None:
        with self._lock:
            self._header = self._center_hz = None
        self._broadcast(("json", {"type": "end"}))

    def _broadcast(self, item: tuple[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            try:
                sub.loop.call_soon_threadsafe(_put, sub, item)
            except RuntimeError:               # a kliens eseményhurka már lezárult
                self.unsubscribe(sub)
