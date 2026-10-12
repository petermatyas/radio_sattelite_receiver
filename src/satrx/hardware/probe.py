"""Az SDR-eszköz állapotának lekérdezése megnyitás nélkül (a felület és a diagnosztika használja).

Az eredmény az „állapotkód": ebből fordít és ad javaslatot a felület (hc_sdr_<kód>, hint_sdr_<kód>).
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from satrx.core.config import Settings

SIMULATED_BACKENDS = ("mock", "replay")


@dataclass
class SdrProbe:
    backend: str
    status: str                  # ok | mock | replay | no_package | no_library | no_driver | not_found | probe_failed | unknown_backend
    message: str = ""
    devices: list[dict[str, Any]] = field(default_factory=list)

    @property
    def simulated(self) -> bool:
        return self.backend in SIMULATED_BACKENDS

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "mock", "replay")

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "simulated": self.simulated}


def _probe_rtlsdr() -> SdrProbe:
    try:
        from rtlsdr import RtlSdr
    except ImportError:
        return SdrProbe("rtlsdr", "no_package", "The pyrtlsdr package is not installed")
    except OSError as exc:       # a librtlsdr könyvtár (DLL / .so) nem tölthető be
        return SdrProbe("rtlsdr", "no_library", f"librtlsdr could not be loaded: {exc}")
    try:
        serials = list(RtlSdr.get_device_serial_addresses())
    except Exception as exc:  # noqa: BLE001
        text = f"{type(exc).__name__}: {exc}"
        # Windowson a hiányzó WinUSB-illesztő tipikus jele: az eszköz látszik, de nem olvasható (LIBUSB_ERROR_*)
        if "LIBUSB" in text.upper() or "usb" in text.lower():
            return SdrProbe("rtlsdr", "no_driver", text)
        return SdrProbe("rtlsdr", "probe_failed", text)
    if not serials:
        return SdrProbe("rtlsdr", "not_found", "No RTL-SDR device was found")
    return SdrProbe("rtlsdr", "ok", f"{len(serials)} RTL-SDR device(s) found",
                    [{"index": i, "serial": s} for i, s in enumerate(serials)])


def _probe_soapy() -> SdrProbe:
    try:
        import SoapySDR
    except ImportError:
        return SdrProbe("soapy", "no_package", "The SoapySDR Python bindings are not installed")
    try:
        found = list(SoapySDR.Device.enumerate(""))
    except Exception as exc:  # noqa: BLE001
        return SdrProbe("soapy", "probe_failed", str(exc))
    if not found:
        return SdrProbe("soapy", "not_found", "No SoapySDR device was found")
    return SdrProbe("soapy", "ok", f"{len(found)} SoapySDR device(s) found",
                    [{"index": i, "serial": str(d.get("serial", "")), "label": str(d.get("label", ""))} for i, d in enumerate(found)])


def probe_sdr(settings: Settings) -> SdrProbe:
    backend = settings.sdr.backend
    if backend == "mock":
        return SdrProbe("mock", "mock", "Simulated SDR: the signals are synthetic, not real")
    if backend == "replay":
        return SdrProbe("replay", "replay", "Recorded files are played back instead of a real SDR")
    if backend == "rtlsdr":
        return _probe_rtlsdr()
    if backend == "soapy":
        return _probe_soapy()
    return SdrProbe(backend, "unknown_backend", f"Unknown SDR backend: {backend}")


class CachedProbe:
    """A felsorolás lassú lehet (USB), ezért néhány másodpercig gyorsítótárazzuk."""

    def __init__(self, settings: Settings, ttl_s: float = 10.0):
        self._settings, self._ttl = settings, ttl_s
        self._lock = threading.Lock()
        self._cached: tuple[float, SdrProbe] | None = None

    def get(self, refresh: bool = False) -> SdrProbe:
        with self._lock:
            now = time.monotonic()
            if refresh or self._cached is None or now - self._cached[0] > self._ttl:
                self._cached = (now, probe_sdr(self._settings))
            return self._cached[1]
