"""systemd értesítések (sd_notify) függőség nélkül: READY, STATUS és a watchdog-életjel.

Ha a szolgáltatás `Type=notify` és `WatchdogSec=` beállítással fut, a systemd újraindítja, ha az életjelek elmaradnak
(például a háttérszálak megakadnak). Más környezetben (Windows, kézi indítás) minden hívás hatástalan.
"""
from __future__ import annotations

import logging
import os
import socket
import time

log = logging.getLogger(__name__)
_last_ping = 0.0


def notify(message: str) -> bool:
    """Üzenet küldése a systemd-nek (NOTIFY_SOCKET); False, ha nincs socket vagy a küldés nem sikerült."""
    path = os.environ.get("NOTIFY_SOCKET")
    if not path or not hasattr(socket, "AF_UNIX"):
        return False
    if path.startswith("@"):                 # absztrakt socket
        path = "\0" + path[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(path)
            sock.sendall(message.encode("utf-8"))
        return True
    except OSError as exc:
        log.debug("sd_notify failed: %s", exc)
        return False


def watchdog_interval() -> float | None:
    """Az életjelek javasolt gyakorisága másodpercben (a watchdog időkorlátjának fele), vagy None, ha nincs watchdog."""
    usec = os.environ.get("WATCHDOG_USEC")
    if not usec or not usec.isdigit() or int(usec) <= 0:
        return None
    return int(usec) / 1e6 / 2


def watchdog_ping(now: float | None = None) -> bool:
    """Életjel a watchdognak, legfeljebb a javasolt gyakorisággal; a háttérszál ciklusából hívjuk."""
    global _last_ping
    interval = watchdog_interval()
    if interval is None:
        return False
    now = time.monotonic() if now is None else now
    if now - _last_ping < interval:
        return False
    _last_ping = now
    return notify("WATCHDOG=1")


def ready(status: str = "") -> bool:
    return notify("READY=1" + (f"\nSTATUS={status}" if status else ""))


def stopping() -> bool:
    return notify("STOPPING=1")


def reset_for_tests() -> None:
    global _last_ping
    _last_ping = 0.0
