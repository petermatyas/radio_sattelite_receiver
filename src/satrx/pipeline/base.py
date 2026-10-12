from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from satrx.recording.sigmf import SigMFInfo


class ProcessorError(Exception):
    """A feldolgozás hibája (a lépés `failed`, a job megáll); az üzenet a UI-n megjelenik.

    A `code` (ha van) az eseménynaplóba kerül géppel olvasható kódként (a UI ebből fordít és javasol teendőt).
    """

    code: str | None = None


class ToolMissingError(ProcessorError):
    """Hiányzik egy külső program (pl. SatDump)."""

    code = "tool_missing"


class NoResult(Exception):
    """Nincs eredmény, de nem hiba (pl. nem sikerült dekódolni, mert a műhold adása ki volt kapcsolva).

    A lépés `no_result` állapotú lesz, a további lépések kimaradnak, a job nem hibás.
    """


@dataclass
class ArtifactSpec:
    kind: str  # image | audio | waterfall | text
    path: Path  # abszolút, vagy a workdir-hez képest relatív; az artifacts könyvtáron belül kell lennie
    meta: dict[str, Any] = field(default_factory=dict)
    thumbnail: Path | None = None  # képeknél automatikusan készül, ha nincs megadva


@dataclass
class PipelineContext:
    recording_id: int
    job_id: int
    norad_id: int | None
    base: Path  # a SigMF alapnév (.sigmf-data / .sigmf-meta)
    info: SigMFInfo
    orbit: dict[str, Any] | None  # a betöltött .orbit.json (TERV.md 7.1); hiányozhat
    workdir: Path  # a job kimeneti könyvtára
    state: dict[str, Any] = field(default_factory=dict)  # lépések közötti adatátadás
    tools: dict[str, Any] = field(default_factory=dict)  # feloldott külső eszközök, pl. {"satdump": ["/usr/bin/satdump"]}
    _log: Callable[[str], None] = lambda _msg: None

    def log(self, message: str) -> None:
        self._log(message)


class Processor(ABC):
    """Feldolgozó plugin. A `params_schema` alapján a UI űrlapot generál, és a lépés paramétereit ellenőrizzük."""

    name: ClassVar[str]
    title: ClassVar[str]
    params_schema: ClassVar[type[BaseModel]]

    @abstractmethod
    def run(self, ctx: PipelineContext, params: Any) -> list[ArtifactSpec]: ...
