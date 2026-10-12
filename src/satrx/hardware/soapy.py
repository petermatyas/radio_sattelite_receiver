from __future__ import annotations

import numpy as np

from satrx.hardware.base import SdrCaps, SdrDevice, SdrError


class SoapyDevice(SdrDevice):
    """Általános SDR a SoapySDR Python kötésen át (Airspy, HackRF, SDRplay, RTL-SDR, ...)."""

    def __init__(self, args: str = ""):
        self.args = args  # pl. "driver=airspy" vagy "driver=rtlsdr,serial=00000001"
        self._sdr = None
        self._stream = None
        self._SoapySDR = None

    def _soapy(self):
        if self._SoapySDR is None:
            try:
                import SoapySDR
            except ImportError as exc:
                raise SdrError("SoapySDR Python bindings are not installed") from exc
            self._SoapySDR = SoapySDR
        return self._SoapySDR

    def capabilities(self) -> SdrCaps:
        if self._sdr is None:  # nyitás előtt konzervatív, szélessávú becslés
            return SdrCaps(1_000_000, 6_000_000_000, 100_000, 20_000_000, None, (), "cf32_le")
        S = self._soapy()
        freq = self._sdr.getFrequencyRange(S.SOAPY_SDR_RX, 0)
        rates = self._sdr.getSampleRateRange(S.SOAPY_SDR_RX, 0)
        gains = self._sdr.getGainRange(S.SOAPY_SDR_RX, 0)
        return SdrCaps(
            int(min(r.minimum() for r in freq)), int(max(r.maximum() for r in freq)),
            int(min(r.minimum() for r in rates)), int(max(r.maximum() for r in rates)),
            (gains.minimum(), gains.maximum()), tuple(self._sdr.listAntennas(S.SOAPY_SDR_RX, 0)), "cf32_le",
        )

    def open(self) -> None:
        S = self._soapy()
        try:
            self._sdr = S.Device(self.args)
        except Exception as exc:
            raise SdrError(f"Could not open SoapySDR device '{self.args}': {exc}") from exc

    def configure(self, center_hz, sample_rate_hz, gain_db=None, antenna=None) -> None:
        S = self._soapy()
        sdr = self._require_open()
        self.capabilities().check(center_hz, sample_rate_hz)
        if self._stream is not None:
            sdr.deactivateStream(self._stream)
            sdr.closeStream(self._stream)
        sdr.setSampleRate(S.SOAPY_SDR_RX, 0, sample_rate_hz)
        sdr.setFrequency(S.SOAPY_SDR_RX, 0, center_hz)
        if antenna:
            sdr.setAntenna(S.SOAPY_SDR_RX, 0, antenna)
        if gain_db is None:
            sdr.setGainMode(S.SOAPY_SDR_RX, 0, True)
        else:
            sdr.setGainMode(S.SOAPY_SDR_RX, 0, False)
            sdr.setGain(S.SOAPY_SDR_RX, 0, gain_db)
        self._stream = sdr.setupStream(S.SOAPY_SDR_RX, S.SOAPY_SDR_CF32)
        sdr.activateStream(self._stream)

    def retune(self, center_hz) -> None:
        self._require_open().setFrequency(self._soapy().SOAPY_SDR_RX, 0, center_hz)

    def read(self, num_samples: int) -> np.ndarray:
        sdr = self._require_open()
        if self._stream is None:
            raise SdrError("Device not configured")
        out = np.empty(num_samples, dtype=np.complex64)
        got = 0
        while got < num_samples:
            res = sdr.readStream(self._stream, [out[got:]], num_samples - got, timeoutUs=1_000_000)
            if res.ret < 0:
                raise SdrError(f"SoapySDR readStream error {res.ret}")
            got += res.ret
        return out

    def close(self) -> None:
        if self._sdr is not None:
            if self._stream is not None:
                self._sdr.deactivateStream(self._stream)
                self._sdr.closeStream(self._stream)
                self._stream = None
            self._sdr = None

    def _require_open(self):
        if self._sdr is None:
            raise SdrError("Device not open")
        return self._sdr
