"""Mentési gyökérmappák kezelése: hol van egy felvétel/eredmény fájlja, elérhető-e a meghajtó, új gyökér érvényesítése.

Elérhetőség-jelző: minden gyökérmappában egy `.satrx-root` fájl van. Ha a meghajtó nincs csatolva (USB kihúzva,
hálózati meghajtó leesett), a csatolási pont üres mappaként létezik tovább a rendszermeghajtón; jelző nélkül ezt
nem tekintjük elérhetőnek, így a felvétel nem kezd a rendszermeghajtóra írni.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from satrx.core.config import PROJECT_ROOT, Settings
from satrx.core.models import Artifact, Recording, StorageRoot

log = logging.getLogger(__name__)
MARKER = ".satrx-root"
KINDS = ("recordings", "artifacts")
MIN_FREE_ERROR = 100 * 1024 * 1024
MIN_FREE_WARN = 5 * 1024**3


class StorageError(Exception):
    pass


@dataclass(frozen=True)
class RootInfo:
    id: int
    kind: str
    path: Path
    label: str
    active: bool


def _norm(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


class StorageManager:
    def __init__(self, settings: Settings, session_factory: sessionmaker[Session]):
        self._settings = settings
        self._sessions = session_factory
        self._lock = threading.RLock()

    # ---- gyökerek -------------------------------------------------------------------------------------------

    def ensure_roots(self) -> None:
        """Indításkor: az alapgyökerek létrehozása (a konfigurált mappákból) és a régi sorok hozzárendelése."""
        defaults = {"recordings": self._settings.recordings_path, "artifacts": self._settings.artifacts_path}
        with self._sessions() as s:
            for kind, path in defaults.items():
                if s.scalar(select(func.count()).select_from(StorageRoot).where(StorageRoot.kind == kind)):
                    continue
                s.add(StorageRoot(kind=kind, path=str(path), label="default", active=True))
            s.commit()
        for root in self.roots():
            if root.path.is_dir() or root.label == "default":
                self._write_marker(root)

    def roots(self, kind: str | None = None) -> list[RootInfo]:
        with self._sessions() as s:
            q = select(StorageRoot).order_by(StorageRoot.id)
            if kind:
                q = q.where(StorageRoot.kind == kind)
            return [RootInfo(r.id, r.kind, Path(r.path), r.label, r.active) for r in s.scalars(q)]

    def active(self, kind: str) -> RootInfo:
        roots = self.roots(kind)
        for r in roots:
            if r.active:
                return r
        raise StorageError(f"No active {kind} storage root")

    def default(self, kind: str) -> RootInfo:
        return self.roots(kind)[0]

    def root(self, kind: str, root_id: int | None) -> RootInfo:
        """A megadott gyökér; None = az alapgyökér (a régi, még hozzárendelés nélküli sorok)."""
        if root_id is None:
            return self.default(kind)
        for r in self.roots(kind):
            if r.id == root_id:
                return r
        raise StorageError(f"Unknown storage root {root_id}")

    def available(self, root: RootInfo) -> bool:
        try:
            return root.path.is_dir() and (root.path / MARKER).is_file()
        except OSError:
            return False

    def _write_marker(self, root: RootInfo) -> None:
        try:
            root.path.mkdir(parents=True, exist_ok=True)
            marker = root.path / MARKER
            if not marker.exists():
                marker.write_text(json.dumps({"app": "satrx", "kind": root.kind, "label": root.label}), encoding="utf-8")
        except OSError as exc:
            log.warning("Could not write the storage marker in %s: %s", root.path, exc)

    def register_root(self, kind: str, path: str | Path, label: str = "", activate: bool = True) -> RootInfo:
        """Új (vagy már ismert) gyökér felvétele; a mappát és a jelzőfájlt létrehozza."""
        result = self.validate_path(str(path))
        if not result["ok"]:
            raise StorageError("; ".join(result["errors"]))
        path = Path(os.path.abspath(str(path)))
        with self._lock, self._sessions() as s:
            row = next((r for r in s.scalars(select(StorageRoot).where(StorageRoot.kind == kind))
                        if _norm(r.path) == _norm(path)), None)
            if row is None:
                row = StorageRoot(kind=kind, path=str(path), label=label or path.name)
                s.add(row)
                s.flush()
            if activate:
                for other in s.scalars(select(StorageRoot).where(StorageRoot.kind == kind)):
                    other.active = other.id == row.id
            s.commit()
            info = RootInfo(row.id, row.kind, Path(row.path), row.label, row.active)
        self._write_marker(info)
        return info

    # ---- útvonalak ---------------------------------------------------------------------------------------------

    def recording_base(self, rec: Recording) -> Path:
        return self.root("recordings", rec.root_id).path / rec.path

    def recording_available(self, rec: Recording) -> bool:
        return self.available(self.root("recordings", rec.root_id))

    def artifact_path(self, art: Artifact, thumbnail: bool = False) -> Path | None:
        """Az artifact (vagy bélyegképe) abszolút útja a gyökerén belül; None, ha nincs fájl vagy kiszökne a gyökérből."""
        rel = art.thumbnail_path if thumbnail else art.path
        if not rel:
            return None
        root = self.root("artifacts", art.root_id)
        top = root.path.resolve()
        path = (top / rel).resolve()
        return path if top in path.parents and path.is_file() else None

    # ---- érvényesítés ---------------------------------------------------------------------------------------------

    def validate_path(self, value: str) -> dict[str, Any]:
        """Egy leendő mentési mappa ellenőrzése (nem módosít semmit, csak próbaírást végez)."""
        errors: list[str] = []
        warnings: list[str] = []
        info: dict[str, Any] = {"path": value, "exists": False, "writable": False, "free_bytes": None,
                                "non_empty": False, "existing_root_id": None}
        value = (value or "").strip()
        if not value:
            errors.append("path_empty")
            return {"ok": False, "errors": errors, "warnings": warnings, **info}
        if not Path(value).is_absolute():
            errors.append("path_not_absolute")
            return {"ok": False, "errors": errors, "warnings": warnings, **info}
        path = Path(os.path.abspath(value))
        info["path"] = str(path)

        if path.parent == path:
            errors.append("path_is_filesystem_root")
        if _norm(path) == _norm(Path.home()):
            errors.append("path_is_home")
        for guarded in (PROJECT_ROOT / "src", PROJECT_ROOT / "web", PROJECT_ROOT / "tests"):
            if _norm(path) == _norm(guarded) or _norm(guarded) in {_norm(p) for p in path.parents}:
                errors.append("path_inside_application")
        if errors:
            return {"ok": False, "errors": errors, "warnings": warnings, **info}

        if path.exists():
            info["exists"] = True
            if not path.is_dir():
                errors.append("path_not_a_directory")
                return {"ok": False, "errors": errors, "warnings": warnings, **info}
            info["non_empty"] = any(p.name != MARKER for p in path.iterdir())
            if info["non_empty"] and not (path / MARKER).exists():
                warnings.append("path_not_empty")
            probe_dir = path
        else:
            ancestor = next((p for p in path.parents if p.exists()), None)
            if ancestor is None or not ancestor.is_dir():
                errors.append("path_parent_missing")
                return {"ok": False, "errors": errors, "warnings": warnings, **info}
            probe_dir = ancestor
            warnings.append("path_will_be_created")

        probe = probe_dir / f".satrx-write-test-{os.getpid()}"
        try:
            probe.write_bytes(b"x")
            probe.unlink()
            info["writable"] = True
        except OSError:
            errors.append("path_not_writable")
        try:
            free = shutil.disk_usage(probe_dir).free
            info["free_bytes"] = free
            if free < MIN_FREE_ERROR:
                errors.append("path_disk_full")
            elif free < MIN_FREE_WARN:
                warnings.append("path_low_space")
        except OSError:
            warnings.append("path_space_unknown")

        for r in self.roots():
            if _norm(r.path) == _norm(path):
                info["existing_root_id"] = r.id
        return {"ok": not errors, "errors": errors, "warnings": warnings, **info}

    # ---- használat ---------------------------------------------------------------------------------------------------

    def usage(self, now: datetime | None = None) -> dict[str, Any]:
        """Foglalt hely az adatbázisból (gyors; a fájlrendszert nem járja be) gyökerenként és összesen."""
        now = now or datetime.now(timezone.utc)
        week = now - timedelta(days=7)
        with self._sessions() as s:
            rec_by_root = dict(s.execute(select(func.coalesce(Recording.root_id, 0), func.coalesce(func.sum(Recording.size_bytes), 0))
                                         .where(Recording.deleted.is_(False)).group_by(Recording.root_id)).all())
            art_by_root = dict(s.execute(select(func.coalesce(Artifact.root_id, 0), func.coalesce(func.sum(Artifact.size_bytes), 0))
                                         .group_by(Artifact.root_id)).all())
            rec_count = s.scalar(select(func.count()).select_from(Recording).where(Recording.deleted.is_(False))) or 0
            art_count = s.scalar(select(func.count()).select_from(Artifact)) or 0
            growth = (s.scalar(select(func.coalesce(func.sum(Recording.size_bytes), 0)).where(Recording.started_at >= week)) or 0) + \
                     (s.scalar(select(func.coalesce(func.sum(Artifact.size_bytes), 0)).where(Artifact.created_at >= week)) or 0)
        defaults = {k: self.default(k).id for k in KINDS}
        roots = []
        for r in self.roots():
            used_map = rec_by_root if r.kind == "recordings" else art_by_root
            used = used_map.get(r.id, 0) + (used_map.get(0, 0) if r.id == defaults[r.kind] else 0)
            ok = self.available(r)
            free = None
            if ok:
                try:
                    free = shutil.disk_usage(r.path).free
                except OSError:
                    pass
            roots.append({"id": r.id, "kind": r.kind, "path": str(r.path), "label": r.label, "active": r.active,
                          "available": ok, "used_bytes": used, "free_bytes": free})
        rec_total = sum(rec_by_root.values())
        art_total = sum(art_by_root.values())
        quota = self._settings.storage.quota_gb * 1e9
        eta = None
        free_active = next((x["free_bytes"] for x in roots if x["kind"] == "recordings" and x["active"]), None)
        if growth > 0 and free_active is not None:
            eta = round(free_active / (growth / 7), 1)
        return {"roots": roots, "recordings_bytes": rec_total, "artifacts_bytes": art_total,
                "total_bytes": rec_total + art_total, "recordings_count": rec_count, "artifacts_count": art_count,
                "quota_bytes": int(quota), "quota_used_fraction": round((rec_total + art_total) / quota, 4) if quota else None,
                "growth_7d_bytes": growth, "days_until_disk_full": eta,
                "retention": {"iq_retention_days": self._settings.storage.iq_retention_days,
                              "artifact_retention_days": self._settings.storage.artifact_retention_days,
                              "quota_gb": self._settings.storage.quota_gb}}

    def total_bytes(self) -> int:
        with self._sessions() as s:
            return int((s.scalar(select(func.coalesce(func.sum(Recording.size_bytes), 0)).where(Recording.deleted.is_(False))) or 0)
                       + (s.scalar(select(func.coalesce(func.sum(Artifact.size_bytes), 0))) or 0))
