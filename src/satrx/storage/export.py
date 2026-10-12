"""Exportálás: ZIP-csomag (letöltéshez) vagy másolás a szerveren megadott mappába (USB, hálózati meghajtó), MANIFEST-tel."""
from __future__ import annotations

import hashlib
import json
import logging
import queue
import shutil
import threading
import time
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from satrx.core.config import Settings
from satrx.core.events import EventLog
from satrx.core.models import Artifact, Recording
from satrx.recording import sigmf
from satrx.storage.manager import MIN_FREE_ERROR, StorageManager
from satrx.storage.move import CHUNK, Cancelled, copy_verified
from satrx.storage.service import execute_plan, plan_delete

log = logging.getLogger(__name__)
ZIP_TTL_S = 24 * 3600


@dataclass
class ExportSpec:
    recording_ids: list[int] = field(default_factory=list)
    artifact_ids: list[int] = field(default_factory=list)
    include_iq: bool = True
    include_artifacts: bool = True       # a kiválasztott felvételek eredményei is
    mode: str = "zip"                    # zip | directory
    directory: str | None = None
    move: bool = False                   # directory módban: sikeres, ellenőrzött másolás után törli az eredetit


class ExportManager:
    def __init__(self, settings: Settings, sm: StorageManager, session_factory: sessionmaker[Session],
                 busy_fn: Callable[[Session], set[int]], events: EventLog | None = None):
        self._settings, self._sm, self._sessions, self._busy, self._events = settings, sm, session_factory, busy_fn, events
        self.dir = settings.data_path / "exports"
        self._jobs: dict[str, dict[str, Any]] = {}
        self._specs: dict[str, ExportSpec] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # ---- életciklus -----------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._thread = threading.Thread(target=self._loop, name="export", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            for ev in self._cancel.values():
                ev.set()
            self._queue.put(None)
            self._thread.join(timeout=10)
            self._thread = None

    def wait_idle(self, timeout: float = 60) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                if all(j["state"] in ("done", "failed", "cancelled") for j in self._jobs.values()):
                    return True
            time.sleep(0.05)
        return False

    def submit(self, spec: ExportSpec) -> dict[str, Any]:
        if spec.mode not in ("zip", "directory"):
            raise ValueError("mode must be 'zip' or 'directory'")
        if spec.mode == "directory":
            check = self._sm.validate_path(spec.directory or "")
            if not check["ok"]:
                raise ValueError("Invalid destination: " + ", ".join(check["errors"]))
        if not spec.recording_ids and not spec.artifact_ids:
            raise ValueError("Nothing selected")
        if spec.move and spec.mode != "directory":
            raise ValueError("Moving is only possible when copying to a directory")
        if spec.move and spec.recording_ids and not (spec.include_iq and spec.include_artifacts):
            raise ValueError("Moving requires exporting the raw IQ and the results too, otherwise unexported data would be deleted")
        self._cleanup_old_zips()
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "state": "queued", "mode": spec.mode, "total_bytes": 0, "done_bytes": 0, "files": 0,
               "current": None, "error": None, "result": None, "moved": None,
               "created_at": datetime.now(timezone.utc).isoformat(), "finished_at": None}
        with self._lock:
            self._jobs[job_id], self._specs[job_id], self._cancel[job_id] = job, spec, threading.Event()
        self._queue.put(job_id)
        return dict(job)

    def status(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def cancel(self, job_id: str) -> bool:
        ev = self._cancel.get(job_id)
        if ev is None:
            return False
        ev.set()
        return True

    def zip_path(self, job_id: str) -> Path | None:
        job = self.status(job_id)
        if not job or job["state"] != "done" or job["mode"] != "zip":
            return None
        path = self.dir / job["result"]
        return path if path.is_file() else None

    def _set(self, job_id: str, **kw: Any) -> None:
        with self._lock:
            self._jobs[job_id].update(kw)

    def _cleanup_old_zips(self) -> None:
        if not self.dir.is_dir():
            return
        for p in self.dir.glob("*.zip"):
            try:
                if time.time() - p.stat().st_mtime > ZIP_TTL_S:
                    p.unlink()
            except OSError:
                pass

    # ---- végrehajtás ----------------------------------------------------------------------------------------------------

    def _gather(self, session: Session, spec: ExportSpec) -> list[tuple[str, Path, str]]:
        """(név az archívumban, forrásfájl, típus) listája."""
        files: list[tuple[str, Path, str]] = []
        seen_art: set[int] = set()
        for rid in spec.recording_ids:
            rec = session.get(Recording, rid)
            if rec is None:
                continue
            base = self._sm.recording_base(rec)
            parts = [(sigmf.meta_path(base), "meta"), (sigmf.orbit_path(base), "orbit")]
            if spec.include_iq:
                parts.insert(0, (sigmf.data_path(base), "iq"))
            files += [(f"{rec.path}/{p.name}", p, kind) for p, kind in parts if p.is_file()]
            if spec.include_artifacts:
                for art in session.scalars(select(Artifact).where(Artifact.recording_id == rid)):
                    seen_art.add(art.id)
                    files += self._artifact_entries(art, f"{rec.path}/results")
        for aid in spec.artifact_ids:
            art = session.get(Artifact, aid)
            if art is not None and art.id not in seen_art:
                files += self._artifact_entries(art, "results")
        return files

    def _artifact_entries(self, art: Artifact, folder: str) -> list[tuple[str, Path, str]]:
        path = self._sm.artifact_path(art)
        return [(f"{folder}/{art.id}_{path.name}", path, "artifact")] if path else []

    def _loop(self) -> None:
        while (job_id := self._queue.get()) is not None:
            try:
                self._run(job_id)
            except Cancelled:
                self._set(job_id, state="cancelled")
            except Exception as exc:  # noqa: BLE001
                log.exception("Export %s failed", job_id)
                self._set(job_id, state="failed", error=str(exc))
            finally:
                self._set(job_id, finished_at=datetime.now(timezone.utc).isoformat(), current=None)
                job = self.status(job_id)
                if self._events is not None and job["state"] in ("done", "failed"):
                    self._events.emit("error" if job["state"] == "failed" else "info", "storage", "export_finished",
                                      f"Export {job['state']}", {"mode": job["mode"], "files": job["files"],
                                                                  "bytes": job["done_bytes"], "error": job["error"]})

    def _run(self, job_id: str) -> None:
        spec, cancel = self._specs[job_id], self._cancel[job_id]
        self._set(job_id, state="running")
        with self._sessions() as session:
            files = self._gather(session, spec)
        total = sum(p.stat().st_size for _, p, _ in files)
        self._set(job_id, total_bytes=total, files=len(files))
        if not files:
            raise ValueError("There are no files to export (the selected items have no files)")
        dest = self.dir if spec.mode == "zip" else Path(spec.directory)
        dest.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(dest).free < total * 1.02 + MIN_FREE_ERROR:
            raise OSError(f"Not enough free space for the export (need {total / 1e9:.2f} GB)")

        def progress(n: int) -> None:
            with self._lock:
                self._jobs[job_id]["done_bytes"] += n

        manifest: list[dict[str, Any]] = []
        if spec.mode == "zip":
            name = f"satrx_export_{datetime.now():%Y%m%d_%H%M%S}_{job_id[:6]}.zip"
            part = self.dir / (name + ".part")
            try:
                with zipfile.ZipFile(part, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
                    for arc, src, _kind in files:
                        self._set(job_id, current=arc)
                        h = hashlib.sha256()
                        with src.open("rb") as fi, zf.open(arc, "w", force_zip64=True) as fo:
                            while chunk := fi.read(CHUNK):
                                if cancel.is_set():
                                    raise Cancelled()
                                h.update(chunk)
                                fo.write(chunk)
                                progress(len(chunk))
                        manifest.append({"name": arc, "size": src.stat().st_size, "sha256": h.hexdigest()})
                    zf.writestr("MANIFEST.json", self._manifest(manifest))
                part.replace(self.dir / name)
            except BaseException:
                part.unlink(missing_ok=True)
                raise
            self._set(job_id, state="done", result=name)
            return

        for arc, src, _kind in files:
            self._set(job_id, current=arc)
            digest = copy_verified(src, dest / arc, cancel.is_set, progress)
            manifest.append({"name": arc, "size": src.stat().st_size, "sha256": digest})
        (dest / "MANIFEST.json").write_text(self._manifest(manifest), encoding="utf-8")
        moved = None
        if spec.move:                     # a másolat ellenőrzött: az eredeti törölhető (védettet és foglaltat kihagyja)
            with self._sessions() as session:
                plan = plan_delete(session, self._sm, recording_ids=spec.recording_ids, artifact_ids=spec.artifact_ids,
                                   scope="all", busy=self._busy(session))
                moved = execute_plan(session, self._sm, plan)
        self._set(job_id, state="done", result=str(dest), moved=moved)

    @staticmethod
    def _manifest(entries: list[dict[str, Any]]) -> str:
        return json.dumps({"created": datetime.now(timezone.utc).isoformat(), "software": "satrx", "files": entries}, indent=2)
