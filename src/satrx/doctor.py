"""`satrx doctor`: a környezet ellenőrzése telepítés után és hibakereséskor (Windows és Linux/Raspberry Pi).

Nem módosít semmit, és nem nyit meg SDR-eszközt (a futó szolgáltatással nem ütközik): az eszközöket csak felsorolja.
"""
from __future__ import annotations

import importlib
import json
import os
import platform
import shutil
import socket
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from satrx.core.config import PROJECT_ROOT, Settings
from satrx.tools import find_satdump

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
Runner = Callable[[list[str]], tuple[int, str]]


@dataclass
class Check:
    id: str
    status: str
    message: str
    hint: str = ""


def _run(args: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return p.returncode, (p.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return 127, ""


def check_python() -> Check:
    v = sys.version_info
    if v < (3, 11):
        return Check("python", FAIL, f"Python {v.major}.{v.minor} is too old", "Python 3.11 or newer is required")
    return Check("python", OK, f"Python {v.major}.{v.minor}.{v.micro}")


def check_platform(system: str | None = None) -> Check:
    system = system or platform.system()
    detail = f"{system} {platform.machine()}"
    model = Path("/proc/device-tree/model")
    if system == "Linux" and model.exists():
        try:
            detail += f" ({model.read_text(errors='ignore').strip(chr(0))})"
        except OSError:
            pass
    return Check("platform", INFO, detail)


def check_dependencies() -> Check:
    missing = []
    for mod in ("numpy", "scipy", "PIL", "skyfield", "fastapi", "uvicorn", "sqlalchemy", "alembic", "httpx", "yaml"):
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        return Check("dependencies", FAIL, "Missing Python packages: " + ", ".join(missing),
                     "Run: pip install -e .  (in the project's virtual environment)")
    return Check("dependencies", OK, "All required Python packages are installed")


def check_web_build() -> Check:
    if (PROJECT_ROOT / "web" / "dist" / "index.html").is_file():
        return Check("web", OK, "The web interface is built")
    return Check("web", FAIL, "The web interface is not built (web/dist is missing)",
                 "Run 'npm install && npm run build' in the web folder, or use a release package that includes web/dist")


def check_config(settings: Settings) -> list[Check]:
    out = []
    if not settings.observer.is_set:
        out.append(Check("observer", WARN, "The observer location is not set in the config file",
                         "It can be entered on the Settings page of the web interface"))
    if settings.server.auth_token is None:
        exposed = settings.server.host not in ("127.0.0.1", "localhost", "::1")
        out.append(Check("auth", WARN if exposed else INFO,
                         "No API token is set: anyone on the network can use the web interface" if exposed
                         else "No API token is set (the server only listens on localhost)",
                         "Set server.auth_token in config/local.yaml" if exposed else ""))
    else:
        out.append(Check("auth", OK, "An API token is set"))
    return out


def check_data_dir(settings: Settings) -> Check:
    path = settings.data_path
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".satrx-doctor-probe"
        probe.write_bytes(b"x")
        probe.unlink()
    except OSError as exc:
        return Check("data_dir", FAIL, f"The data directory is not writable: {path} ({exc})",
                     "Check the directory's owner and permissions (it must be writable by the service user)")
    free = shutil.disk_usage(path).free
    msg = f"Data directory {path} is writable, {free / 1e9:.1f} GB free"
    if free < 2e9:
        return Check("data_dir", WARN, msg, "Less than 2 GB free: use a larger drive (see the Storage page)")
    return Check("data_dir", OK, msg)


def check_storage_roots(settings: Settings) -> list[Check]:
    db = settings.db_path
    if not db.is_file():
        return [Check("storage", INFO, "No database yet (it is created on the first start)")]
    out = []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        rows = con.execute("SELECT kind, path, active FROM storage_roots ORDER BY id").fetchall()
        con.close()
    except sqlite3.Error:
        return [Check("storage", INFO, "The storage locations are not registered yet")]
    for kind, path, active in rows:
        p = Path(path)
        ok = p.is_dir() and (p / ".satrx-root").is_file()
        if ok:
            out.append(Check(f"storage_{kind}", OK if active else INFO, f"{kind} location {path} is available"
                             + ("" if active else " (not active)")))
        elif active:
            out.append(Check(f"storage_{kind}", FAIL, f"The active {kind} location is not available: {path}",
                             "Is the external drive connected and mounted? Recording is blocked until it is back"))
        else:
            out.append(Check(f"storage_{kind}", WARN, f"An older {kind} location is not available: {path}",
                             "Data stored there cannot be opened until the drive is back"))
    return out


_SDR_HINTS = {
    "no_package": "Run: pip install -e '.[rtlsdr]' (Linux also: apt install librtlsdr0 rtl-sdr)",
    "no_library": "Linux: apt install librtlsdr0. Windows: pip install 'pyrtlsdr[lib]' (bundles the DLLs) or put rtlsdr.dll in PATH",
    "no_driver": "The device is visible but cannot be opened. Windows: install the WinUSB driver with Zadig for the "
                 "'Bulk-In, Interface (Interface 0)' entry; Linux: install the udev rule and blacklist the DVB module; "
                 "close other SDR programs that may hold the device",
    "not_found": "Check the USB connection; Linux: the dvb_usb_rtl28xxu module must be blacklisted and the udev rule installed; "
                 "Windows: install the WinUSB driver with Zadig",
    "no_tool": "Install the rtl-sdr tools (Windows: https://github.com/rtlsdrblog/rtl-sdr-blog/releases, unzip and add the x64 folder to PATH or set tools.rtl_sdr_path; Linux: apt install rtl-sdr)",
    "probe_failed": "Check the driver and the USB connection",
    "unknown_backend": "Use one of: mock, replay, rtlsdr, rtl_sdr, soapy",
}


def check_sdr(settings: Settings) -> Check:
    from satrx.hardware.probe import probe_sdr

    probe = probe_sdr(settings)
    if probe.status == "mock":
        return Check("sdr", WARN, "The SDR backend is 'mock': no real hardware is used",
                     "Set sdr.backend: rtlsdr in config/local.yaml to receive real signals")
    if probe.status == "replay":
        return Check("sdr", INFO, "The SDR backend is 'replay' (files are played back)")
    if probe.status == "ok":
        return Check("sdr", OK, probe.message)
    text = {"no_driver": f"The SDR is not accessible ({probe.message})"}.get(probe.status, probe.message)
    return Check("sdr", FAIL, text, _SDR_HINTS.get(probe.status, ""))


_PNP_PS = ("Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | Where-Object { $_.InstanceId -match 'VID_0BDA&PID_283[28]' } | "
           "ForEach-Object { $_.Status + '|' + $_.Class + '|' + $_.InstanceId }")


def check_rtlsdr_windows(settings: Settings, system: str | None = None, run: Runner = _run) -> Check | None:
    """Windows: az RTL-SDR USB-eszköz látszik-e, és van-e működő (WinUSB) illesztője az interfészeinek."""
    if (system or platform.system()) != "Windows":
        return None
    code, out = run(["powershell", "-NoProfile", "-Command", _PNP_PS])
    if code == 127:
        return Check("usb_windows", INFO, "The USB devices could not be listed (PowerShell is not available)")
    rows = [line.split("|", 2) for line in out.splitlines() if line.count("|") >= 2]
    if not rows:
        return Check("usb_windows", WARN if settings.sdr.backend == "rtlsdr" else INFO,
                     "No RTL-SDR (Realtek 0bda:2838 / 2832) USB device is connected to this computer",
                     "Plug the dongle in (preferably directly, not through a hub)")
    interfaces = [r for r in rows if "&MI_" in r[2]]
    broken = [r for r in (interfaces or rows) if r[0].strip().upper() not in ("OK",)]
    if broken:
        level = FAIL if settings.sdr.backend == "rtlsdr" else WARN
        return Check("usb_windows", level,
                     "The RTL-SDR is connected, but Windows has no working driver for it (" + ", ".join(
                         sorted({f"{r[1].strip() or 'device'}: {r[0].strip()}" for r in broken})) + ")",
                     "Install the WinUSB driver with Zadig (https://zadig.akeo.ie): Options > List All Devices, choose "
                     "'Bulk-In, Interface (Interface 0)', driver WinUSB, then Replace Driver. Do not change Interface 1")
    return Check("usb_windows", OK, f"The RTL-SDR is connected and its USB driver is working ({len(rows)} entries)")


def check_satdump(settings: Settings) -> Check:
    info = find_satdump(None, settings)
    if info.found:
        return Check("satdump", OK, f"SatDump found: {info.command[0]} ({info.source})")
    return Check("satdump", WARN, "SatDump was not found (needed only for Meteor LRPT decoding)",
                 "Install SatDump and set tools.satdump_path in config/local.yaml or on the Settings page")


def check_time_sync(system: str | None = None, run: Runner = _run) -> Check:
    system = system or platform.system()
    if system != "Linux":
        return Check("time_sync", INFO, "The system clock was not checked (only checked on Linux)",
                     "Pass predictions need an accurate clock: keep automatic time sync enabled")
    code, out = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    if code == 127:
        return Check("time_sync", WARN, "timedatectl is not available: the clock sync could not be checked")
    if out.strip().lower() == "yes":
        return Check("time_sync", OK, "The system clock is synchronized (NTP)")
    return Check("time_sync", WARN, "The system clock is NOT synchronized",
                 "Pass times depend on an accurate clock. Enable NTP: sudo timedatectl set-ntp true (and install chrony)")


def check_rtlsdr_linux(system: str | None = None, run: Runner = _run, root: Path = Path("/")) -> list[Check]:
    if (system or platform.system()) != "Linux":
        return []
    out = []
    blacklisted = False
    for conf in (root / "etc/modprobe.d").glob("*.conf") if (root / "etc/modprobe.d").is_dir() else []:
        try:
            if "blacklist dvb_usb_rtl28xxu" in conf.read_text(errors="ignore"):
                blacklisted = True
        except OSError:
            pass
    code, mods = run(["lsmod"])
    loaded = "dvb_usb_rtl28xxu" in mods
    if loaded:
        out.append(Check("dvb_module", FAIL, "The dvb_usb_rtl28xxu kernel module is loaded and blocks the RTL-SDR",
                         "Blacklist it (deploy/blacklist-rtlsdr.conf) and run: sudo modprobe -r dvb_usb_rtl28xxu"))
    elif not blacklisted:
        out.append(Check("dvb_module", WARN, "The dvb_usb_rtl28xxu kernel module is not blacklisted",
                         "It may grab the dongle after a reboot: install deploy/blacklist-rtlsdr.conf into /etc/modprobe.d/"))
    else:
        out.append(Check("dvb_module", OK, "The DVB kernel module is blacklisted"))
    rules = [p for d in ("etc/udev/rules.d", "lib/udev/rules.d", "usr/lib/udev/rules.d") if (root / d).is_dir()
             for p in (root / d).glob("*") if "rtl" in p.name.lower() or "satrx" in p.name.lower()]
    if rules:
        out.append(Check("udev", OK, "A udev rule for the RTL-SDR is installed"))
    else:
        out.append(Check("udev", WARN, "No udev rule for the RTL-SDR was found",
                         "Without it only root can use the dongle: install deploy/99-satrx-rtlsdr.rules"))
    return out


def check_port(settings: Settings) -> Check:
    """Kapcsolódással ellenőrzünk (a bind Windowson megbízhatatlan, ha a másik folyamat SO_REUSEADDR-t használ)."""
    port = settings.server.port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        in_use = s.connect_ex(("127.0.0.1", port)) == 0
    if in_use:
        return Check("port", INFO, f"Port {port} is in use (the service is probably already running)")
    return Check("port", OK, f"Port {port} is free")


def check_service(system: str | None = None, run: Runner = _run) -> Check | None:
    if (system or platform.system()) != "Linux" or shutil.which("systemctl") is None:
        return None
    _, active = run(["systemctl", "is-active", "satrx"])
    _, enabled = run(["systemctl", "is-enabled", "satrx"])
    if active.strip() == "active":
        return Check("service", OK, f"The satrx service is active ({enabled.strip() or 'unknown'} at boot)")
    return Check("service", INFO, f"The satrx service is {active.strip() or 'not installed'}",
                 "Start it with: sudo systemctl enable --now satrx")


def run_all(settings: Settings) -> list[Check]:
    checks = [check_python(), check_platform(), check_dependencies(), check_web_build(), *check_config(settings),
              check_data_dir(settings), *check_storage_roots(settings), check_sdr(settings), check_satdump(settings),
              check_time_sync(), *check_rtlsdr_linux(), check_port(settings)]
    usb = check_rtlsdr_windows(settings)
    if usb:
        checks.append(usb)
    svc = check_service()
    if svc:
        checks.append(svc)
    return checks


_TAG = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", INFO: "[info]"}


def format_report(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        lines.append(f"{_TAG[c.status]} {c.message}")
        if c.hint and c.status in (WARN, FAIL):
            lines.append(f"       -> {c.hint}")
    fails = sum(c.status == FAIL for c in checks)
    warns = sum(c.status == WARN for c in checks)
    lines.append("")
    lines.append(f"{len(checks)} checks: {fails} failed, {warns} warning(s)." if fails or warns else
                 f"{len(checks)} checks: everything is fine.")
    return "\n".join(lines)


def main(argv: list[str], settings: Settings | None = None) -> int:
    from satrx.core.config import get_settings

    settings = settings or get_settings()
    checks = run_all(settings)
    if "--json" in argv:
        print(json.dumps([asdict(c) for c in checks], indent=2))
    else:
        print(format_report(checks))
    return 1 if any(c.status == FAIL for c in checks) else 0
