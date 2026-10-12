from __future__ import annotations

from satrx.core.config import SdrConfig
from satrx.hardware.base import SdrDevice, SdrError


def create_device(cfg: SdrConfig) -> SdrDevice:
    """Új (még nem megnyitott) eszközpéldány a konfiguráció alapján."""
    args = dict(cfg.device_args)
    if cfg.backend == "mock":
        from satrx.hardware.mock import MockDevice
        return MockDevice(**args)
    if cfg.backend == "replay":
        from satrx.hardware.replay import FileReplayDevice
        return FileReplayDevice(**args)
    if cfg.backend == "rtlsdr":
        from satrx.hardware.rtlsdr import RtlSdrDevice
        return RtlSdrDevice(**args)
    if cfg.backend == "soapy":
        from satrx.hardware.soapy import SoapyDevice
        return SoapyDevice(**args)
    raise SdrError(f"Unknown SDR backend: {cfg.backend}")
