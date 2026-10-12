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


def save_satdump_path(session: Session, value: str | None) -> None:
    session.merge(Setting(key=TOOLS_KEY, value={"satdump_path": value or None}))
    session.commit()
