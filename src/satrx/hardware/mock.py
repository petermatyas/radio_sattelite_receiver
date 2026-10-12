from __future__ import annotations

import time

import numpy as np

from satrx.hardware.base import SdrCaps, SdrDevice, SdrError


class MockDevice(SdrDevice):
    """Szintetikus jelforrás SDR nélküli fejlesztéshez/teszteléshez.

    Zaj + a középfrekvenciától `tone_offset_hz`-re lévő, 1 kHz-es hanggal modulált FM vivő.
    A fázis blokkok között folytonos, `retune` hatására az eltolás a vivőn nem ugrik.
    """

    def __init__(self, realtime: bool = True, tone_offset_hz: float = 10_000.0, seed: int = 0,
                 noise_sigma: float = 0.05):
        self.realtime = realtime
        self.tone_offset_hz = tone_offset_hz
        self.noise_sigma = noise_sigma
        self._rng = np.random.default_rng(seed)
        self._opened = False
        self.center_hz = 0
        self.sample_rate_hz = 0
        self.gain_db: float | None = None
        self._n = 0  # eddig kiadott minták (a fázishoz)
        self._carrier_phase = 0.0
        self._next_ready = 0.0

    def capabilities(self) -> SdrCaps:
        return SdrCaps(24_000_000, 1_766_000_000, 225_000, 3_200_000, (0.0, 49.6), ("RX",), "cu8")

    def open(self) -> None:
        self._opened = True

    def configure(self, center_hz, sample_rate_hz, gain_db=None, antenna=None) -> None:
        self._require_open()
        self.capabilities().check(center_hz, sample_rate_hz)
        self.center_hz, self.sample_rate_hz, self.gain_db = center_hz, sample_rate_hz, gain_db
        self._next_ready = time.monotonic()

    def retune(self, center_hz) -> None:
        self._require_open()
        self.center_hz = center_hz

    def read(self, num_samples: int) -> np.ndarray:
        self._require_open()
        if not self.sample_rate_hz:
            raise SdrError("Device not configured")
        if self.realtime:
            self._next_ready += num_samples / self.sample_rate_hz
            delay = self._next_ready - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        t = (self._n + np.arange(num_samples)) / self.sample_rate_hz
        audio = np.sin(2 * np.pi * 1000 * t)
        inst_freq = self.tone_offset_hz + 3000 * audio
        phase = self._carrier_phase + 2 * np.pi * np.cumsum(inst_freq) / self.sample_rate_hz
        self._carrier_phase = float(phase[-1] % (2 * np.pi))
        self._n += num_samples
        noise = self.noise_sigma * (
            self._rng.standard_normal(num_samples) + 1j * self._rng.standard_normal(num_samples)
        )
        x = 0.5 * np.exp(1j * phase) + noise
        out = np.empty(num_samples, dtype=np.complex64)
        out.real = np.clip(x.real, -1, 1)
        out.imag = np.clip(x.imag, -1, 1)
        return out

    def close(self) -> None:
        self._opened = False

    def _require_open(self) -> None:
        if not self._opened:
            raise SdrError("Device not open")
