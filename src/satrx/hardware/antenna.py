from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger(__name__)


class AntennaSwitch(Protocol):
    def select(self, port: str | None) -> None:
        """Az adott kapcsolóporton lévő antennára vált (None: alapállás)."""


class NoopSwitch:
    """Nincs fizikai váltó; megjegyzi a kért portot. Később GPIO/USB-relé/hálózati megvalósítás váltja."""

    def __init__(self) -> None:
        self.selected: str | None = None

    def select(self, port: str | None) -> None:
        if port != self.selected:
            log.info("Antenna switch (noop): port %s", port)
        self.selected = port
