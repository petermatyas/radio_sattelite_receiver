from __future__ import annotations

import logging
import queue
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from satrx.core.config import Settings
from satrx.core.events import EventLog
from satrx.core.models import Artifact, Job, JobStep, Pass, Pipeline, Recording
from satrx.pipeline.base import ArtifactSpec, NoResult, PipelineContext, ProcessorError
from satrx.pipeline.registry import ProcessorRegistry
from satrx.recording import sigmf
from satrx.recording.orbit import load_orbit
from satrx.storage.manager import StorageManager
from satrx.tools import find_satdump

log = logging.getLogger(__name__)
THUMB_SIZE = (256, 256)


def now() -> datetime:
    return datetime.now(timezone.utc)


class PipelineError(Exception):
    """A job nem hozható létre (hiányzó felvétel/pipeline stb.)."""


class PipelineRunner:
    def __init__(self, settings: Settings, session_factory: sessionmaker[Session], registry: ProcessorRegistry,
                 events: EventLog | None = None, storage: StorageManager | None = None):
        self._storage = storage or StorageManager(settings, session_factory)
        self._settings = settings
        self._sessions = session_factory
        self.registry = registry
        self._events = events

    # ---- job létrehozása ----------------------------------------------------------------------

    def validate_steps(self, steps: list[dict]) -> None:
        """ValueError, ha ismeretlen processzor vagy érvénytelen paraméter szerepel a lépések között."""
        for i, step in enumerate(steps):
            cls = self.registry.get(step.get("processor", ""))
            if cls is None:
                raise ValueError(f"Step {i + 1}: unknown processor '{step.get('processor')}'")
            try:
                cls.params_schema.model_validate(step.get("params") or {})
            except Exception as exc:  # pydantic.ValidationError
                raise ValueError(f"Step {i + 1} ({cls.name}): invalid parameters: {exc}") from exc

    def create_job(self, recording_id: int, pipeline_id: int | None = None, pass_id: int | None = None,
                   steps: list[dict] | None = None) -> int:
        """Feldolgozó job: vagy egy mentett pipeline (pipeline_id), vagy ad-hoc lépéslista (steps) alapján."""
        with self._sessions() as session:
            rec = session.get(Recording, recording_id)
            pipeline = session.get(Pipeline, pipeline_id) if pipeline_id is not None else None
            if rec is None or (pipeline_id is not None and pipeline is None):
                raise PipelineError("Recording or pipeline not found")
            if steps is None:
                steps = pipeline.steps if pipeline else []
            if rec.deleted or not sigmf.data_path(self._storage.recording_base(rec)).exists():
                raise PipelineError("The raw IQ recording no longer exists")
            if not steps:
                raise PipelineError("The pipeline has no steps")
            self.validate_steps(steps)
            job = Job(kind="processing", recording_id=recording_id, pipeline_id=pipeline_id, pass_id=pass_id,
                      status="queued")
            session.add(job)
            session.flush()
            for i, step in enumerate(steps):
                session.add(JobStep(job_id=job.id, idx=i, processor=step["processor"],
                                    params=step.get("params") or {}))
            if pass_id is not None and (p := session.get(Pass, pass_id)) is not None:
                p.status = "processing"
            session.commit()
            return job.id

    # ---- futtatás -----------------------------------------------------------------------------

    def run_job(self, job_id: int) -> None:
        with self._sessions() as session:
            job = session.get(Job, job_id)
            rec = session.get(Recording, job.recording_id)
            pipeline = session.get(Pipeline, job.pipeline_id) if job.pipeline_id else None
            job.status, job.started_at = "running", now()
            session.commit()
            base = self._storage.recording_base(rec)
            storage_ok = self._storage.recording_available(rec)
            ids = {"job_id": job_id, "recording_id": rec.id, "pass_id": job.pass_id}
            norad_id, keep_iq = rec.norad_id, (pipeline.keep_iq if pipeline else True)
            pipeline_name = pipeline.name if pipeline else "ad-hoc"
            step_ids = list(session.scalars(select(JobStep.id).where(JobStep.job_id == job_id).order_by(JobStep.idx)))

        self._emit("info", "pipeline_started", f"Processing started: {pipeline_name}", ids, {"pipeline": pipeline_name})
        art_root = self._storage.active("artifacts")
        if not storage_ok or not self._storage.available(art_root):
            msg = "The storage location is not available (is the drive connected?)"
            self._finish(job_id, "failed", msg)
            self._emit("error", "storage_unavailable", msg, ids)
            return
        try:
            info = sigmf.read_info(base)
        except (OSError, KeyError, ValueError) as exc:
            self._finish(job_id, "failed", f"Cannot read the recording: {exc}")
            self._emit("error", "iq_missing", f"Cannot read the recording: {exc}", ids)
            return
        workdir = art_root.path / f"rec{rec.id}" / f"job{job_id}"
        workdir.mkdir(parents=True, exist_ok=True)

        status, error = "done", None
        state: dict = {}  # a job lépései között közös (pl. az előző demodulátor hangfájlja)
        with self._sessions() as session:
            satdump = find_satdump(session, self._settings)
        tools = {"satdump": satdump.command}
        error_code: str | None = None
        for n, step_id in enumerate(step_ids):
            outcome, step_error, code = self._run_step(step_id, job_id, rec.id, norad_id, base, info, workdir, state, tools,
                                                       art_root)
            if outcome == "ok":
                continue
            status = "no_result" if outcome == "no_result" else "failed"
            error = step_error if outcome == "failed" else None
            error_code = code
            self._skip_rest(step_ids[n + 1:])
            break

        self._finish(job_id, status, error)
        if status == "failed":
            self._emit("error", error_code or "pipeline_failed", f"Processing failed: {error}", ids, {"error": error})
        elif status == "no_result":
            self._emit("info", "no_result", f"Processing finished without a decodable result: {pipeline_name}", ids,
                       {"pipeline": pipeline_name})
        else:
            with self._sessions() as session:
                n_art = session.scalar(select(func.count()).select_from(Artifact).where(Artifact.job_id == job_id)) or 0
            self._emit("info", "pipeline_done", f"Processing finished: {pipeline_name}, {n_art} result(s)", ids,
                       {"pipeline": pipeline_name, "artifacts": n_art})
            if not keep_iq:
                self._delete_iq(rec.id, base)

    def _run_step(self, step_id: int, job_id: int, rec_id: int, norad_id: int | None, base: Path,
                  info: sigmf.SigMFInfo, workdir: Path, state: dict, tools: dict,
                  art_root) -> tuple[str, str | None, str | None]:
        with self._sessions() as session:
            step = session.get(JobStep, step_id)
            step.status, step.started_at = "running", now()
            name, raw_params = step.processor, step.params
            session.commit()

        log_lines: list[str] = []
        outcome, error, code = "ok", None, None
        try:
            cls = self.registry.get(name)
            if cls is None:
                raise ProcessorError(f"Unknown processor '{name}'")
            params = cls.params_schema.model_validate(raw_params or {})
            ctx = PipelineContext(
                recording_id=rec_id, job_id=job_id, norad_id=norad_id, base=base, info=info,
                orbit=load_orbit(base), workdir=workdir, state=state, tools=tools, _log=log_lines.append)
            artifacts = cls().run(ctx, params)
            self._store_artifacts(artifacts, ctx, job_id, rec_id, norad_id, art_root)
        except NoResult as exc:
            outcome, error = "no_result", str(exc)
            log_lines.append(str(exc))
        except Exception as exc:  # noqa: BLE001 - minden hiba a UI-ra kerül, nem csak a ProcessorError
            outcome, error = "failed", str(exc) or exc.__class__.__name__
            code = getattr(exc, "code", None)
            if not isinstance(exc, ProcessorError):
                log_lines.append(traceback.format_exc())
                log.exception("Processor %s crashed", name)

        with self._sessions() as session:
            step = session.get(JobStep, step_id)
            step.status = outcome
            step.finished_at, step.error, step.log = now(), error, "\n".join(log_lines)
            session.commit()
        return outcome, error, code

    def _skip_rest(self, step_ids: list[int]) -> None:
        with self._sessions() as session:
            for sid in step_ids:
                session.get(JobStep, sid).status = "skipped"
            session.commit()

    def _store_artifacts(self, specs: list[ArtifactSpec], ctx: PipelineContext, job_id: int, rec_id: int,
                         norad_id: int | None, art_root) -> None:
        root = art_root.path.resolve()
        rows = []
        for spec in specs:
            path = (spec.path if spec.path.is_absolute() else ctx.workdir / spec.path).resolve()
            if root not in path.parents or not path.is_file():
                raise ProcessorError(f"Artifact path is invalid or missing: {spec.path}")
            thumb = spec.thumbnail
            if thumb is None and spec.kind in ("image", "waterfall"):
                thumb = self._make_thumbnail(path)
            rows.append(Artifact(job_id=job_id, recording_id=rec_id, norad_id=norad_id, kind=spec.kind,
                                 path=path.relative_to(root).as_posix(),
                                 thumbnail_path=thumb.resolve().relative_to(root).as_posix() if thumb else None,
                                 meta=spec.meta, root_id=art_root.id,
                                 size_bytes=path.stat().st_size + (thumb.stat().st_size if thumb and thumb.exists() else 0)))
        with self._sessions() as session:
            session.add_all(rows)
            session.commit()

    @staticmethod
    def _make_thumbnail(path: Path) -> Path | None:
        thumb = path.with_name(path.stem + ".thumb.jpg")
        try:
            with Image.open(path) as img:
                img = img.convert("RGB")
                img.thumbnail(THUMB_SIZE)
                img.save(thumb, "JPEG", quality=80)
            return thumb
        except Exception:  # noqa: BLE001 - miniatűr nélkül is használható az artifact
            log.warning("Could not create thumbnail for %s", path)
            return None

    def _finish(self, job_id: int, status: str, error: str | None) -> None:
        with self._sessions() as session:
            job = session.get(Job, job_id)
            job.status, job.error, job.finished_at = status, error, now()
            if job.pass_id is not None and (p := session.get(Pass, job.pass_id)) is not None:
                p.status = "failed" if status == "failed" else "done"
            session.commit()

    def _delete_iq(self, rec_id: int, base: Path) -> None:
        for p in (sigmf.data_path(base), sigmf.meta_path(base)):
            p.unlink(missing_ok=True)
        with self._sessions() as session:
            rec = session.get(Recording, rec_id)
            rec.deleted = True
            session.commit()

    def _emit(self, level: str, code: str, message: str, ids: dict, details: dict | None = None) -> None:
        if self._events is not None:
            self._events.emit(level, "pipeline", code, message, details, **ids)


class PipelineExecutor:
    """Egyetlen háttérszál, amely sorban futtatja a feldolgozó jobokat (az SDR közben szabad marad)."""

    def __init__(self, runner: PipelineRunner):
        self.runner = runner
        self._queue: queue.Queue[int | None] = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="pipeline", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(timeout=10)
            self._thread = None

    def submit_steps(self, recording_id: int, steps: list[dict], pass_id: int | None = None) -> int:
        """Ad-hoc feldolgozás (pl. utólagos waterfall/hang) mentett pipeline nélkül."""
        job_id = self.runner.create_job(recording_id, None, pass_id, steps)
        self._queue.put(job_id)
        return job_id

    def submit(self, recording_id: int, pipeline_id: int, pass_id: int | None = None) -> int:
        """Job létrehozása és sorba állítása; a job azonosítóját adja (PipelineError, ha nem indítható)."""
        job_id = self.runner.create_job(recording_id, pipeline_id, pass_id)
        self._queue.put(job_id)
        return job_id

    def wait_idle(self, timeout: float = 30) -> bool:
        done = threading.Event()
        threading.Thread(target=lambda: (self._queue.join(), done.set()), daemon=True).start()
        return done.wait(timeout)

    def _loop(self) -> None:
        while (job_id := self._queue.get()) is not None:
            try:
                self.runner.run_job(job_id)
            except Exception:  # noqa: BLE001
                log.exception("Pipeline job %s crashed", job_id)
                try:
                    self.runner._finish(job_id, "failed", "Internal error while running the pipeline")
                except Exception:  # noqa: BLE001
                    log.exception("Could not mark job %s as failed", job_id)
            finally:
                self._queue.task_done()
        self._queue.task_done()
