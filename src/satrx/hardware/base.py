from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import numpy as np


class SdrError(Exception):
    pass


@dataclass(frozen=True)
class SdrCaps:
    freq_min_hz: int
    freq_max_hz: int
    sample_rate_min_hz: int
    sample_rate_max_hz: int
    gain_range_db: tuple[float, float] | None = None
    antennas: tuple[str, ...] = field(default_factory=tuple)
    native_format: str = "cf32_le"  # a leggazdaságosabb tárolási formátum (SigMF datatype)

    def check(self, center_hz: int, sample_rate_hz: int) -> None:
        if not self.freq_min_hz <= center_hz <= self.freq_max_hz:
            raise SdrError(
                f"Center frequency {center_hz} Hz outside device range "
                f"{self.freq_min_hz}-{self.freq_max_hz} Hz"
            )
        if not self.sample_rate_min_hz <= sample_rate_hz <= self.sample_rate_max_hz:
            raise SdrError(
                f"Sample rate {sample_rate_hz} outside device range "
                f"{self.sample_rate_min_hz}-{self.sample_rate_max_hz}"
            )


class SdrDevice(ABC):
    """Hardverfüggetlen SDR felület. A minták mindig normalizált complex64 blokkok (-1..1)."""

    @abstractmethod
    def capabilities(self) -> SdrCaps: ...

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def configure(self, center_hz: int, sample_rate_hz: int, gain_db: float | None = None,
                  antenna: str | None = None) -> None:
        """gain_db=None: automatikus erősítés (AGC)."""

    @abstractmethod
    def retune(self, center_hz: int) -> None:
        """Középfrekvencia változtatása a minták megszakítása nélkül (Doppler-követés)."""

    @abstractmethod
    def read(self, num_samples: int) -> np.ndarray:
        """Pontosan num_samples complex64 mintát ad vissza (blokkol)."""

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> SdrDevice:
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
