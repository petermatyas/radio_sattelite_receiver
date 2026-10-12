"""Áthelyezés az aktív gyökérbe: ellenőrzött másolás, majd az adatbázis átállítása, és csak utána a forrás törlése.

Tetszőleges ponton megszakítható és újraindítható: egy elem csak akkor „költözik át", ha minden fájlja ellenőrzötten
a célon van; addig az eredeti érintetlen. A megszakított másolat `.part` fájlja törlődik.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from satrx.core.events import EventLog
from satrx.core.models import Artifact, Recording
from satrx.recording import sigmf
from satrx.storage.manager import MIN_FREE_ERROR, StorageManager

log = logging.getLogger(__name__)
CHUNK = 8 * 1024 * 1024


class Cancelled(Exception):
    pass


def copy_verified(src: Path, dst: Path, cancelled: Callable[[], bool], progress: Callable[[int], None] | None = None) -> str:
    """Másolás ellenőrző összeggel: a forrást másolás közben hash-eli, a másolatot visszaolvassa és összeveti.

    A SHA-256 összeget adja vissza. Hiba vagy megszakítás esetén a részleges célfájl törlődik.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    try:
        h = hashlib.sha256()
        with src.open("rb") as fi, part.open("wb") as fo:
            while chunk := fi.read(CHUNK):
                if cancelled():
                    raise Cancelled()
                h.update(chunk)
                fo.write(chunk)
                if progress:
                    progress(len(chunk))
            fo.flush()
            os.fsync(fo.fileno())
        check = hashlib.sha256()
        with part.open("rb") as f:
            while chunk := f.read(CHUNK):
                if cancelled():
                    raise Cancelled()
                check.update(chunk)
        if check.hexdigest() != h.hexdigest():
            raise OSError(f"Checksum mismatch after copying {src.name}")
        os.replace(part, dst)
        return h.hexdigest()
    except BaseException:
        part.unlink(missing_ok=True)
        raise


@dataclass
class _Item:
    type: str
    id: int
    src_root: Path
    files: list[str]          # a gyökérhez viszonyított fájlnevek
    size: int


class MoveManager:
    def __init__(self, sm: StorageManager, session_factory: sessionmaker[Session],
                 busy_fn: Callable[[Session], set[int]], events: EventLog | None = None):
        self._sm, self._sessions, self._busy, self._events = sm, session_factory, busy_fn, events
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {"state": "idle", "kind": None, "total_items": 0, "done_items": 0, "total_bytes": 0,
                                       "done_bytes": 0, "current": None, "skipped": [], "errors": [], "error": None,
                                       "started_at": None, "finished_at": None}

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state, skipped=list(self._state.get("skipped", [])), errors=list(self._state.get("errors", [])))

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, kind: str) -> dict[str, Any]:
        with self._lock:
            if self.running:
                raise ValueError("A move is already in progress")
            if kind not in ("recordings", "artifacts"):
                raise ValueError("kind must be 'recordings' or 'artifacts'")
            active = self._sm.active(kind)
            if not self._sm.available(active):
                raise ValueError("The active storage location is not available")
            self._cancel.clear()
            self._state = {"state": "running", "kind": kind, "total_items": 0, "done_items": 0, "total_bytes": 0,
                           "done_bytes": 0, "current": None, "skipped": [], "errors": [], "error": None,
                           "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None}
            self._thread = threading.Thread(target=self._run, args=(kind,), name="storage-move", daemon=True)
            self._thread.start()
        return self.status()

    def cancel(self) -> None:
        self._cancel.set()

    def join(self, timeout: float = 30) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _set(self, **kw: Any) -> None:
        with self._lock:
            self._state.update(kw)

    def _add(self, key: str, value: Any) -> None:
        with self._lock:
            self._state[key].append(value)

    # ---- szál -----------------------------------------------------------------------------------------------------

    def _collect(self, session: Session, kind: str, active_id: int) -> list[_Item]:
        items: list[_Item] = []
        default_id = self._sm.default(kind).id
        model = Recording if kind == "recordings" else Artifact
        for obj in session.query(model).all():
            effective = obj.root_id if obj.root_id is not None else default_id
            if effective == active_id:
                continue
            src_root = self._sm.root(kind, obj.root_id).path
            if kind == "recordings":
                base = Path(obj.path)
                names = [sigmf.data_path(base).name, sigmf.meta_path(base).name, sigmf.orbit_path(base).name]
            else:
                names = [n for n in (obj.path, obj.thumbnail_path) if n]
            files = [n for n in names if (src_root / n).is_file()]
            items.append(_Item(kind, obj.id, src_root, files, sum((src_root / n).stat().st_size for n in files)))
        return items

    def _run(self, kind: str) -> None:
        moved = 0
        try:
            with self._sessions() as session:
                active = self._sm.active(kind)
                items = self._collect(session, kind, active.id)
                total = sum(i.size for i in items)
                self._set(total_items=len(items), total_bytes=total)
                free = shutil.disk_usage(active.path).free
                if total * 1.05 + MIN_FREE_ERROR > free:
                    raise OSError(f"Not enough free space at the destination (need {total / 1e9:.1f} GB, free {free / 1e9:.1f} GB)")
                busy = self._busy(session)
                for item in items:
                    if self._cancel.is_set():
                        break
                    if item.type == "recordings" and item.id in busy:
                        self._add("skipped", {"type": item.type, "id": item.id, "reason": "busy"})
                        continue
                    self._set(current=f"{item.type} #{item.id}")
                    try:
                        self._move_item(session, kind, item, active.id, active.path)
                        moved += 1
                        self._set(done_items=self.status()["done_items"] + 1)
                    except Cancelled:
                        self._cancel.set()      # a megszakítás oka mindig megszakítás: így az állapot is „cancelled"
                        break
                    except (OSError, ValueError) as exc:
                        self._add("errors", {"type": item.type, "id": item.id, "error": str(exc)})
                        log.warning("Move of %s %s failed: %s", item.type, item.id, exc)
            state = "cancelled" if self._cancel.is_set() else ("failed" if self.status()["errors"] and not moved else "done")
            self._set(state=state, current=None)
        except Exception as exc:  # noqa: BLE001
            log.exception("Storage move failed")
            self._set(state="failed", error=str(exc), current=None)
        finally:
            self._set(finished_at=datetime.now(timezone.utc).isoformat())
            st = self.status()
            if self._events is not None:
                level = "error" if st["state"] == "failed" else "info"
                self._events.emit(level, "storage", "move_finished", f"Storage move {st['state']}: {moved} item(s) moved",
                                  {"kind": kind, "state": st["state"], "moved": moved, "errors": len(st["errors"]),
                                   "skipped": len(st["skipped"]), "bytes": st["done_bytes"]})

    def _move_item(self, session: Session, kind: str, item: _Item, dest_id: int, dest_root: Path) -> None:
        copied: list[Path] = []
        try:
            for name in item.files:
                dst = dest_root / name
                copy_verified(item.src_root / name, dst, self._cancel.is_set,
                              lambda n: self._set(done_bytes=self.status()["done_bytes"] + n))
                copied.append(dst)
        except BaseException:
            for dst in copied:                # az elem nem költözött át: a félig kész másolatokat eltávolítjuk
                dst.unlink(missing_ok=True)
            raise
        model = Recording if kind == "recordings" else Artifact
        obj = session.get(model, item.id)
        obj.root_id = dest_id
        session.commit()                       # innentől az új helyről érhető el; csak ezután törlünk
        for name in item.files:
            (item.src_root / name).unlink(missing_ok=True)
        for name in item.files:                # üres, korábbi mappák takarítása (rec<id>/job<id>)
            parent = (item.src_root / name).parent
            while parent != item.src_root and parent.is_dir() and not any(parent.iterdir()):
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
