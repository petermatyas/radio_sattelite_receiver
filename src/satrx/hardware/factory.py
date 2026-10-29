from __future__ import annotations

from satrx.core.config import SdrConfig, Settings
from satrx.hardware.base import SdrDevice, SdrError


def create_device(cfg: SdrConfig, settings: Settings | None = None) -> SdrDevice:
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
    if cfg.backend == "rtl_sdr":
        from satrx.hardware.rtlsdr_tool import RtlSdrToolDevice
        from satrx.tools import find_rtl_tool
        return RtlSdrToolDevice(command=find_rtl_tool("rtl_sdr", settings) if settings else None, **args)
    if cfg.backend == "soapy":
        from satrx.hardware.soapy import SoapyDevice
        return SoapyDevice(**args)
    raise SdrError(f"Unknown SDR backend: {cfg.backend}")
