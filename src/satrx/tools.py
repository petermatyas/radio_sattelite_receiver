"""Külső eszközök (SatDump) keresése: beállított útvonal (UI/YAML) -> PATH -> szokásos telepítési helyek."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from satrx.core.config import Settings
from satrx.core.models import Setting

TOOLS_KEY = "tools"


@dataclass(frozen=True)
class ToolInfo:
    command: list[str] | None   # a futtatandó parancs (első eleme a program)
    source: str                 # setting | config | path | default | none | invalid
    configured: str | None

    @property
    def found(self) -> bool:
        return self.command is not None


def _default_locations() -> list[Path]:
    if os.name == "nt":
        roots = [os.environ.get(v) for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
        return [Path(r) / "SatDump" / "satdump.exe" for r in roots if r] + [Path("C:/SatDump/satdump.exe")]
    return [Path(p) for p in ("/usr/bin/satdump", "/usr/local/bin/satdump", "/opt/satdump/satdump",
                              "/opt/SatDump/satdump", str(Path.home() / "SatDump" / "build" / "satdump"))]


def _resolve(value: str) -> list[str] | None:
    path = Path(value).expanduser()
    if path.is_file():
        return [str(path)]
    found = shutil.which(value)
    return [found] if found else None


def configured_satdump(session: Session | None, settings: Settings) -> tuple[str | None, str]:
    """(beállított érték, forrás): a UI-n mentett érték felülírja a YAML-t."""
    if session is not None:
        stored = session.get(Setting, TOOLS_KEY)
        if stored is not None and stored.value and stored.value.get("satdump_path"):
            return stored.value["satdump_path"], "setting"
    if settings.tools.satdump_path:
        return settings.tools.satdump_path, "config"
    return None, "none"


def find_satdump(session: Session | None, settings: Settings) -> ToolInfo:
    value, source = configured_satdump(session, settings)
    if value:
        command = _resolve(value)
        return ToolInfo(command, source if command else "invalid", value)
    on_path = shutil.which("satdump")
    if on_path:
        return ToolInfo([on_path], "path", None)
    for candidate in _default_locations():
        if candidate.is_file():
            return ToolInfo([str(candidate)], "default", None)
    return ToolInfo(None, "none", None)


RTL_KEYS = ("rtl_sdr_path", "rtl_fm_path")


def save_tool_paths(session: Session, values: dict[str, str | None]) -> None:
    """A megadott útvonalak mentése (a többi megmarad)."""
    stored = session.get(Setting, TOOLS_KEY)
    merged = {**(stored.value if stored is not None and stored.value else {}), **{k: v or None for k, v in values.items()}}
    session.merge(Setting(key=TOOLS_KEY, value=merged))
    session.commit()


def save_satdump_path(session: Session, value: str | None) -> None:
    save_tool_paths(session, {"satdump_path": value})


def apply_stored_tools(session: Session, settings: Settings) -> None:
    """A felületen mentett rtl_sdr / rtl_fm útvonalak felülírják a YAML-t (az eszközréteg a `settings.tools`-t olvassa)."""
    stored = session.get(Setting, TOOLS_KEY)
    if stored is None or not stored.value:
        return
    for key in RTL_KEYS:
        if key in stored.value:
            setattr(settings.tools, key, stored.value[key])


def _rtl_locations(name: str) -> list[Path]:
    exe = f"{name}.exe" if os.name == "nt" else name
    if os.name == "nt":
        roots = [os.environ.get(v) for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "USERPROFILE")]
        dirs = [Path(r) / sub for r in roots if r for sub in ("rtl-sdr", "rtl-sdr-blog", "rtlsdr")]
        dirs += [Path("C:/rtl-sdr"), Path("C:/rtl-sdr-release/x64"), Path("C:/rtlsdr")]
        return [d / exe for d in dirs] + [d / "x64" / exe for d in dirs]
    return [Path("/usr/bin") / exe, Path("/usr/local/bin") / exe]


def find_rtl_tool(name: str, settings: Settings) -> list[str] | None:
    """Az `rtl_sdr` / `rtl_fm` program keresése: beállított útvonal (tools.<name>_path) -> PATH -> szokásos helyek."""
    configured = getattr(settings.tools, f"{name}_path", None)
    if configured:
        return _resolve(configured)
    found = shutil.which(name)
    if found:
        return [found]
    for path in _rtl_locations(name):
        if path.is_file():
            return [str(path)]
    return None
