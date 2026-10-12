from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from satrx.hardware.base import SdrDevice
from satrx.recording.orbit import OrbitContext
from satrx.recording.sigmf import SigMFWriter

BLOCK_SAMPLES = 131072  # 256 többszöröse (RTL-SDR követelmény)


class FrequencyPlan(Protocol):
    """Idő -> vételi frekvencia (Doppler-követés)."""

    def freq_at(self, t: datetime) -> int: ...


@dataclass(frozen=True)
class RecordSpec:
    center_hz: int
    sample_rate_hz: int
    duration_s: float
    gain_db: float | None = None
    fmt: str = "cu8"
    antenna: str | None = None
    description: str = ""
    extra_meta: dict[str, Any] | None = None
    doppler: FrequencyPlan | None = None
    retune_step_hz: int = 500
    start_time: datetime | None = None  # a felvétel kezdete (Doppler-számításhoz); alap: most
    orbit: OrbitContext | None = None   # pályametaadat-pillanatkép az .orbit.json-hoz


@dataclass(frozen=True)
class RecordResult:
    num_samples: int
    size_bytes: int
    stopped_early: bool


def record(device: SdrDevice, spec: RecordSpec, base: Path, stop: threading.Event | None = None,
           on_block: Callable[[np.ndarray, int, int], None] | None = None) -> RecordResult:
    """Az eszközről spec.duration_s hosszú IQ-t ír SigMF fájlba.

    A hossz mintaszám alapú (nem falióra), így hardveren és mockon is ugyanúgy működik.
    on_block(samples, total_samples_so_far, center_hz): mellékág (élő waterfall); a hibái nem állítják le a felvételt.
    """
    stop = stop or threading.Event()
    start_time = spec.start_time or datetime.now(timezone.utc)
    current_hz = spec.doppler.freq_at(start_time) if spec.doppler else spec.center_hz
    device.configure(current_hz, spec.sample_rate_hz, spec.gain_db, spec.antenna)
    total = int(round(spec.duration_s * spec.sample_rate_hz))
    check_every = max(1, spec.sample_rate_hz // 2)  # félmásodpercenként
    next_check = check_every
    size = 0
    stopped_early = False
    with SigMFWriter(base, spec.fmt, spec.sample_rate_hz, current_hz,
                     extra_global=spec.extra_meta, description=spec.description) as writer:
        while writer.num_samples < total:
            if stop.is_set():
                stopped_early = True
                break
            if spec.doppler and writer.num_samples >= next_check:
                t = start_time + timedelta(seconds=writer.num_samples / spec.sample_rate_hz)
                wanted = spec.doppler.freq_at(t)
                if abs(wanted - current_hz) >= spec.retune_step_hz:
                    device.retune(wanted)
                    writer.mark_retune(wanted)
                    current_hz = wanted
                next_check = writer.num_samples + check_every
            n = min(BLOCK_SAMPLES, total - writer.num_samples)
            block = device.read(n)
            size += writer.write(block)
            if on_block is not None:
                try:
                    on_block(block, writer.num_samples, current_hz)
                except Exception:  # noqa: BLE001 - a mellékág nem ronthatja el a felvételt
                    on_block = None
        num = writer.num_samples
    return RecordResult(num, size, stopped_early)
