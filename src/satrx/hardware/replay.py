from __future__ import annotations

from pathlib import Path

import numpy as np

from satrx.hardware.base import SdrCaps, SdrDevice, SdrError
from satrx.recording import sigmf


class FileReplayDevice(SdrDevice):
    """Meglévő SigMF felvételt játszik le SDR helyett (teszt, regresszió, hardver nélküli futtatás).

    A fájl végén EOF-nál SdrError-t dob (loop=True esetén körbefordul).
    A mintavételi ráta és a frekvencia a felvételből jön, a configure() ezeket nem írja felül.
    """

    def __init__(self, path: str | Path, loop: bool = False):
        self.base = Path(path)
        self.loop = loop
        self.info: sigmf.SigMFInfo | None = None
        self.center_hz = 0
        self._fh = None

    def capabilities(self) -> SdrCaps:
        info = self.info or sigmf.read_info(self.base)
        return SdrCaps(0, 6_000_000_000, info.sample_rate_hz, info.sample_rate_hz, None, ("RX",), info.fmt)

    def open(self) -> None:
        self.info = sigmf.read_info(self.base)
        self.center_hz = self.info.center_freq_hz
        self._fh = sigmf.data_path(self.base).open("rb")

    def configure(self, center_hz, sample_rate_hz, gain_db=None, antenna=None) -> None:
        if self.info is None:
            raise SdrError("Device not open")
        if sample_rate_hz != self.info.sample_rate_hz:
            raise SdrError(
                f"Replay file has sample rate {self.info.sample_rate_hz}, requested {sample_rate_hz}"
            )

    def retune(self, center_hz) -> None:
        self.center_hz = center_hz

    def read(self, num_samples: int) -> np.ndarray:
        if self._fh is None or self.info is None:
            raise SdrError("Device not open")
        bps = sigmf.bytes_per_sample(self.info.fmt)
        want = num_samples * bps
        raw = self._fh.read(want)
        if len(raw) < want and self.loop:
            self._fh.seek(0)
            raw += self._fh.read(want - len(raw))
        if len(raw) < want:
            raise SdrError("End of replay file")
        return sigmf.decode(raw, self.info.fmt)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
