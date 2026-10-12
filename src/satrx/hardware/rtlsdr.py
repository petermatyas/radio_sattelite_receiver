from __future__ import annotations

import numpy as np

from satrx.hardware.base import SdrCaps, SdrDevice, SdrError


class RtlSdrDevice(SdrDevice):
    """RTL-SDR a pyrtlsdr csomaggal (Windows: librtlsdr.dll + Zadig/WinUSB driver kell)."""

    def __init__(self, device_index: int = 0, ppm: int = 0):
        self.device_index = device_index
        self.ppm = ppm
        self._sdr = None

    def capabilities(self) -> SdrCaps:
        # R820T/R820T2 hangoló: ~24-1766 MHz; 2.4 MS/s felett veszteséges
        return SdrCaps(24_000_000, 1_766_000_000, 225_001, 3_200_000, (0.0, 49.6), ("RX",), "cu8")

    def open(self) -> None:
        try:
            from rtlsdr import RtlSdr
        except ImportError as exc:
            raise SdrError("pyrtlsdr is not installed (pip install 'satrx[rtlsdr]')") from exc
        try:
            self._sdr = RtlSdr(device_index=self.device_index)
        except Exception as exc:  # librtlsdr / USB hibák
            text = str(exc)
            hint = ""
            if "LIBUSB" in f"{type(exc).__name__} {text}".upper():
                hint = (" - the USB device cannot be opened: on Windows install the WinUSB driver with Zadig "
                        "(Bulk-In, Interface 0); on Linux install the udev rule and blacklist the DVB module; "
                        "close other SDR programs")
            raise SdrError(f"Could not open RTL-SDR #{self.device_index}: {text}{hint}") from exc
        if self.ppm:
            self._sdr.freq_correction = self.ppm

    def configure(self, center_hz, sample_rate_hz, gain_db=None, antenna=None) -> None:
        sdr = self._require_open()
        self.capabilities().check(center_hz, sample_rate_hz)
        sdr.sample_rate = sample_rate_hz
        sdr.center_freq = center_hz
        sdr.gain = "auto" if gain_db is None else gain_db

    def retune(self, center_hz) -> None:
        self._require_open().center_freq = center_hz

    def read(self, num_samples: int) -> np.ndarray:
        sdr = self._require_open()
        try:
            return np.asarray(sdr.read_samples(num_samples), dtype=np.complex64)
        except Exception as exc:
            raise SdrError(f"RTL-SDR read failed: {exc}") from exc

    def close(self) -> None:
        if self._sdr is not None:
            self._sdr.close()
            self._sdr = None

    def _require_open(self):
        if self._sdr is None:
            raise SdrError("Device not open")
        return self._sdr
