from __future__ import annotations

from datetime import datetime

import numpy as np
from skyfield.api import EarthSatellite, load, wgs84

from satrx.passes.predictor import Observer

C_KM_S = 299_792.458
_ts = load.timescale(builtin=True)


class DopplerTracker:
    """Egy nominális (a műhold által sugárzott) frekvencia Doppler-eltolt vételi értéke az idő függvényében."""

    def __init__(self, line1: str, line2: str, observer: Observer, nominal_hz: int, name: str = "sat"):
        self._sat = EarthSatellite(line1, line2, name, _ts)
        self._topos = wgs84.latlon(observer.lat, observer.lon, elevation_m=observer.alt_m)
        self.nominal_hz = nominal_hz

    def range_rate_km_s(self, t: datetime) -> float:
        """Pozitív: a műhold távolodik."""
        rel = (self._sat - self._topos).at(_ts.from_datetime(t))
        pos, vel = rel.position.km, rel.velocity.km_per_s
        return float(np.dot(pos, vel) / np.linalg.norm(pos))

    def freq_at(self, t: datetime) -> int:
        return round(self.nominal_hz * (1 - self.range_rate_km_s(t) / C_KM_S))
