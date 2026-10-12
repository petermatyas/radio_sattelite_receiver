from __future__ import annotations

import importlib.util
import logging
from importlib.metadata import entry_points
from pathlib import Path

from satrx.pipeline.base import Processor

log = logging.getLogger(__name__)


class ProcessorRegistry:
    def __init__(self) -> None:
        self._items: dict[str, type[Processor]] = {}
        self.load_errors: list[str] = []

    def register(self, cls: type[Processor]) -> type[Processor]:
        if cls.name in self._items and self._items[cls.name] is not cls:
            raise ValueError(f"Processor '{cls.name}' is already registered")
        self._items[cls.name] = cls
        return cls

    def get(self, name: str) -> type[Processor] | None:
        return self._items.get(name)

    def all(self) -> list[type[Processor]]:
        return sorted(self._items.values(), key=lambda c: c.name)


def load_plugins(registry: ProcessorRegistry, plugins_dir: Path | None) -> None:
    """Külső processzorok: `satrx.processors` entry point-ok és a plugins/ mappa *.py fájljai.

    Egy plugin fájlnak `register(registry)` függvényt kell tartalmaznia. A hibás plugin nem akadályozza az
    indulást; a hiba a `load_errors` listába kerül (az állapotellenőrzés megjeleníti).
    """
    for ep in entry_points(group="satrx.processors"):
        try:
            registry.register(ep.load())
        except Exception as exc:  # noqa: BLE001
            registry.load_errors.append(f"entry point {ep.name}: {exc}")
            log.exception("Could not load processor entry point %s", ep.name)

    if plugins_dir and plugins_dir.is_dir():
        for path in sorted(plugins_dir.glob("*.py")):
            try:
                spec = importlib.util.spec_from_file_location(f"satrx_plugin_{path.stem}", path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.register(registry)
            except Exception as exc:  # noqa: BLE001
                registry.load_errors.append(f"{path.name}: {exc}")
                log.exception("Could not load plugin %s", path)


def default_registry(plugins_dir: Path | None = None) -> ProcessorRegistry:
    from satrx.processors.audio_demod import AudioDemod
    from satrx.processors.fm_demod import FmDemod
    from satrx.processors.meteor_lrpt import MeteorLrpt
    from satrx.processors.sstv import Sstv
    from satrx.processors.waterfall import Waterfall

    registry = ProcessorRegistry()
    for cls in (FmDemod, AudioDemod, Waterfall, Sstv, MeteorLrpt):
        registry.register(cls)
    load_plugins(registry, plugins_dir)
    return registry
