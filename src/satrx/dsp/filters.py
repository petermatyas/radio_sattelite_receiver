"""Közös, állapottartó (streamelhető) szűrők és decimálás-tervezés a processzorokhoz."""
from __future__ import annotations

import numpy as np
from scipy import signal

TARGET_AUDIO_RATE = 48_000


class DspError(Exception):
    pass


def divisors_near(sample_rate: int, max_divisor: int, target_rate: float = TARGET_AUDIO_RATE) -> list[int]:
    return sorted((d for d in range(1, max_divisor + 1) if sample_rate % d == 0),
                  key=lambda d: abs(sample_rate / d - target_rate))


def best_decimation(sample_rate: int, bandwidth_hz: float) -> int:
    """A mintavételi ráta egész osztói közül az, amelyik 48 kHz-hez legközelebbi hangmintavételt ad."""
    divisors = divisors_near(sample_rate, int(sample_rate // (2 * bandwidth_hz)))
    if not divisors:
        raise DspError(
            f"Sample rate {sample_rate} Hz has no integer decimation that keeps a {bandwidth_hz:.0f} Hz channel")
    return divisors[0]


def plan_decimation(sample_rate: int, bandwidth_hz: float, min_ratio: float = 1.2) -> tuple[int, int]:
    """(csatorna-decimálás, hang-decimálás). A végső hangráta (>= 24 kHz) a lehető legközelebb van 48 kHz-hez;
    a köztes (IF) ráta, amelyen demodulálunk, legalább min_ratio x a sávszélesség (a széles WFM-hez is elég)."""
    if sample_rate < min_ratio * bandwidth_hz:
        raise DspError(
            f"Sample rate {sample_rate} Hz is too low for a {bandwidth_hz:.0f} Hz wide channel (decimation)")
    totals = divisors_near(sample_rate, int(sample_rate // 24_000))
    if not totals:
        raise DspError(f"Sample rate {sample_rate} Hz has no integer decimation to an audio rate (decimation)")
    total = totals[0]
    d1 = max(d for d in range(1, total + 1) if total % d == 0 and sample_rate / d >= min_ratio * bandwidth_hz)
    return d1, total // d1


def stages(decimation: int) -> list[int]:
    """Az össz-decimálás felbontása kis (<= 10) lépcsőkre; így a szűrők rövidek maradnak."""
    primes, n, p = [], decimation, 2
    while n > 1:
        while n % p == 0:
            primes.append(p)
            n //= p
        p += 1
    out, cur = [], 1
    for q in sorted(primes, reverse=True):
        if cur * q > 10 and cur > 1:
            out.append(cur)
            cur = 1
        cur *= q
    if cur > 1:
        out.append(cur)
    return out or [1]


class DecimatingFilter:
    """Állapottartó FIR aluláteresztő + decimálás (komplex vagy valós mintákra)."""

    def __init__(self, factor: int, dtype=np.complex128):
        self.factor = factor
        self.taps = signal.firwin(20 * factor + 1, 0.8 / factor) if factor > 1 else None
        self.zi = np.zeros(len(self.taps) - 1, dtype=dtype) if self.taps is not None else None
        self.consumed = 0

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.taps is None:
            return x
        y, self.zi = signal.lfilter(self.taps, 1.0, x, zi=self.zi)
        first = (-self.consumed) % self.factor
        self.consumed += len(y)
        return y[first::self.factor]


class DecimatorChain:
    def __init__(self, decimation: int, dtype=np.complex128):
        self.filters = [DecimatingFilter(f, dtype) for f in stages(decimation)]
        self.factors = stages(decimation)

    def process(self, x: np.ndarray) -> np.ndarray:
        for f in self.filters:
            x = f.process(x)
            if len(x) == 0:
                break
        return x


class Fir:
    def __init__(self, taps: np.ndarray, dtype=np.complex128):
        self.taps, self.zi = taps, np.zeros(len(taps) - 1, dtype=dtype)

    def process(self, x: np.ndarray) -> np.ndarray:
        y, self.zi = signal.lfilter(self.taps, 1.0, x, zi=self.zi)
        return y


class Iir:
    def __init__(self, b, a):
        self.b, self.a, self.zi = b, a, None

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.zi is None:
            self.zi = signal.lfilter_zi(self.b, self.a) * x[0]  # kezdeti lépcső nélkül induljon
        y, self.zi = signal.lfilter(self.b, self.a, x, zi=self.zi)
        return y
