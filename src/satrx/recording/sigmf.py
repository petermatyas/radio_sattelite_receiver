from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from satrx.hardware.base import SdrError

SIGMF_VERSION = "1.0.0"
FORMATS = ("cu8", "ci16_le", "cf32_le")
_BYTES_PER_SAMPLE = {"cu8": 2, "ci16_le": 4, "cf32_le": 8}


def bytes_per_sample(fmt: str) -> int:
    return _BYTES_PER_SAMPLE[fmt]


def data_path(base: Path) -> Path:
    return base.with_name(base.name + ".sigmf-data")


def meta_path(base: Path) -> Path:
    return base.with_name(base.name + ".sigmf-meta")


def orbit_path(base: Path) -> Path:
    return base.with_name(base.name + ".orbit.json")


def encode(samples: np.ndarray, fmt: str) -> bytes:
    """complex64 (-1..1) -> az adott SigMF datatype nyers bájtjai."""
    if fmt == "cf32_le":
        return np.ascontiguousarray(samples, dtype="<c8").tobytes()
    inter = np.empty(samples.size * 2, dtype=np.float32)
    inter[0::2] = samples.real
    inter[1::2] = samples.imag
    if fmt == "cu8":
        return np.clip(np.rint(inter * 127.5 + 127.5), 0, 255).astype(np.uint8).tobytes()
    if fmt == "ci16_le":
        return np.clip(np.rint(inter * 32767), -32768, 32767).astype("<i2").tobytes()
    raise SdrError(f"Unsupported sample format: {fmt}")


def decode(raw: bytes, fmt: str) -> np.ndarray:
    if fmt == "cf32_le":
        return np.frombuffer(raw, dtype="<c8").astype(np.complex64)
    if fmt == "cu8":
        inter = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5) / 127.5
    elif fmt == "ci16_le":
        inter = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32767
    else:
        raise SdrError(f"Unsupported sample format: {fmt}")
    return (inter[0::2] + 1j * inter[1::2]).astype(np.complex64)


class SigMFWriter:
    """IQ felvétel írása SigMF formátumban (.sigmf-data + .sigmf-meta)."""

    def __init__(self, base: Path, fmt: str, sample_rate_hz: int, center_freq_hz: int,
                 extra_global: dict[str, Any] | None = None, description: str = ""):
        if fmt not in FORMATS:
            raise SdrError(f"Unsupported sample format: {fmt}")
        self.base, self.fmt = base, fmt
        self.sample_rate_hz, self.center_freq_hz = sample_rate_hz, center_freq_hz
        self.num_samples = 0
        self._start = datetime.now(timezone.utc)
        self._global = {
            "core:datatype": fmt,
            "core:sample_rate": sample_rate_hz,
            "core:version": SIGMF_VERSION,
            "core:recorder": "satrx",
            "core:description": description,
            **(extra_global or {}),
        }
        self._captures: list[dict[str, Any]] = [self._capture(0, center_freq_hz)]
        base.parent.mkdir(parents=True, exist_ok=True)
        self._fh = data_path(base).open("wb")

    def _capture(self, start_sample: int, freq_hz: int) -> dict[str, Any]:
        return {
            "core:sample_start": start_sample,
            "core:frequency": freq_hz,
            "core:datetime": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    def mark_retune(self, freq_hz: int) -> None:
        """Új capture szegmens a frekvenciaváltás helyén (Doppler-követés)."""
        if freq_hz != self._captures[-1]["core:frequency"]:
            self._captures.append(self._capture(self.num_samples, freq_hz))

    def write(self, samples: np.ndarray) -> int:
        raw = encode(samples, self.fmt)
        self._fh.write(raw)
        self.num_samples += samples.size
        return len(raw)

    def close(self) -> None:
        if self._fh.closed:
            return
        self._fh.close()
        meta = {"global": self._global, "captures": self._captures, "annotations": []}
        meta_path(self.base).write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def __enter__(self) -> SigMFWriter:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


@dataclass(frozen=True)
class SigMFInfo:
    fmt: str
    sample_rate_hz: int
    center_freq_hz: int
    num_samples: int
    captures: list[dict[str, Any]]
    extra: dict[str, Any]


def read_info(base: Path) -> SigMFInfo:
    meta = json.loads(meta_path(base).read_text(encoding="utf-8"))
    g = meta["global"]
    fmt = g["core:datatype"]
    size = data_path(base).stat().st_size
    return SigMFInfo(
        fmt=fmt,
        sample_rate_hz=int(g["core:sample_rate"]),
        center_freq_hz=int(meta["captures"][0]["core:frequency"]),
        num_samples=size // bytes_per_sample(fmt),
        captures=meta["captures"],
        extra={k: v for k, v in g.items() if not k.startswith("core:")},
    )


def read_blocks(base: Path, block_samples: int = 262144) -> Iterator[np.ndarray]:
    info = read_info(base)
    block_bytes = block_samples * bytes_per_sample(info.fmt)
    with data_path(base).open("rb") as fh:
        while raw := fh.read(block_bytes):
            usable = len(raw) - len(raw) % bytes_per_sample(info.fmt)
            if usable:
                yield decode(raw[:usable], info.fmt)


def read_segments(base: Path, block_samples: int = 262144, start_sample: int = 0,
                  end_sample: int | None = None) -> Iterator[tuple[int, int, np.ndarray]]:
    """(első minta indexe, a szegmens közép-frekvenciája Hz-ben, minták) a [start_sample, end_sample) tartományra.

    A blokkok az újrahangolások (captures) határán szétvágva jönnek, így egy blokkon belül a közép-frekvencia
    állandó. A fájlban előre ugrik (seek), nem olvassa végig a kihagyott részt.
    """
    info = read_info(base)
    end = info.num_samples if end_sample is None else min(end_sample, info.num_samples)
    starts = sorted((int(c["core:sample_start"]), int(c["core:frequency"])) for c in info.captures)
    bounds = [st for st, _ in starts[1:]] + [info.num_samples]
    bps = bytes_per_sample(info.fmt)
    with data_path(base).open("rb") as fh:
        for (seg_start, freq), seg_end in zip(starts, bounds):
            pos, hi = max(seg_start, start_sample), min(seg_end, end)
            if pos >= hi:
                continue
            fh.seek(pos * bps)
            while pos < hi:
                raw = fh.read(min(block_samples, hi - pos) * bps)
                if len(raw) < bps:
                    return
                samples = decode(raw[: len(raw) - len(raw) % bps], info.fmt)
                yield pos, freq, samples
                pos += len(samples)
