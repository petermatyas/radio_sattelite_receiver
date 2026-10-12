from __future__ import annotations

import logging
import re
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from satrx.core.config import Settings
from satrx.core.events import EventLog
from satrx.core.models import Job, Recording, Satellite
from satrx.hardware.base import SdrDevice, SdrError
from satrx.hardware.factory import create_device
from satrx.recording import orbit as orbit_mod
from satrx.recording.live import LiveSpectrum
from satrx.recording import sigmf
from satrx.recording.recorder import RecordResult, RecordSpec, record
from satrx.storage.manager import StorageManager

log = logging.getLogger(__name__)

DEVICE_ID = "sdr0"


# on_finished(recording_id, job_id, pass_id, result, error) - a felvételi szálból hívódik
FinishedCallback = Callable[[int, int, int | None, RecordResult | None, str | None], None]


class BusyError(Exception):
    """Az SDR már foglalt."""


@dataclass
class RecorderStatus:
    state: str = "idle"  # idle | recording | stopping
    recording_id: int | None = None
    pass_id: int | None = None  # None: kézi felvétel
    started_at: datetime | None = None
    duration_s: float = 0.0
    elapsed_s: float = 0.0
    last_error: str | None = None


class RecordingManager:
    """Egy SDR eszköz kizárólagos használata (egyszerre egy felvétel).

    A scheduler (3. fázis) is ezen keresztül kér majd felvételt, így a kézi és az automatikus
    felvételek nem ütközhetnek.
    """

    def __init__(self, settings: Settings, session_factory: sessionmaker[Session],
                 device_factory: Callable[[], SdrDevice] | None = None, events: EventLog | None = None,
                 live: LiveSpectrum | None = None, storage: StorageManager | None = None):
        self._storage = storage or StorageManager(settings, session_factory)
        self._events = events
        self._live = live
        self._settings = settings
        self._sessions = session_factory
        self._device_factory = device_factory or (lambda: create_device(settings.sdr))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = RecorderStatus()

    def native_format(self) -> str:
        return self._device_factory().capabilities().native_format

    def status(self) -> RecorderStatus:
        with self._lock:
            return RecorderStatus(**self._status.__dict__)

    def capabilities(self):
        return self._device_factory().capabilities()

    def start(self, spec: RecordSpec, norad_id: int | None = None, pass_id: int | None = None,
              pipeline_id: int | None = None, on_finished: FinishedCallback | None = None,
              label: str | None = None) -> int:
        """Elindít egy felvételt a háttérben; a Recording azonosítóját adja. SdrError/BusyError dobható."""
        with self._lock:
            if self._status.state != "idle":
                raise BusyError("SDR is busy")
            device = self._device_factory()
            caps = device.capabilities()
            caps.check(spec.center_hz, spec.sample_rate_hz)
            root = self._storage.active("recordings")
            if not self._storage.available(root):
                raise SdrError(f"The recordings storage location is not available: {root.path}")
            recordings_dir = root.path
            need = spec.duration_s * spec.sample_rate_hz * sigmf.bytes_per_sample(spec.fmt)
            if shutil.disk_usage(recordings_dir).free < need * 1.1:
                raise SdrError(f"Not enough free disk space (need ~{need / 1e6:.0f} MB)")

            backend = self._settings.sdr.backend
            spec = replace(spec, extra_meta={**(spec.extra_meta or {}), "satrx:sdr_backend": backend})
            now = datetime.now(timezone.utc)
            slug = re.sub(r"[^A-Za-z0-9]+", "-", label or "").strip("-")[:30]
            name = f"{now:%Y%m%dT%H%M%SZ}_{norad_id or slug or 'manual'}_{spec.center_hz}"
            with self._sessions() as session:
                job = Job(pass_id=pass_id, pipeline_id=pipeline_id, device=DEVICE_ID, status="running", started_at=now)
                session.add(job)
                session.flush()
                rec = Recording(job_id=job.id, norad_id=norad_id, path=name, sample_format=spec.fmt,
                                center_freq_hz=spec.center_hz, sample_rate_hz=spec.sample_rate_hz,
                                started_at=now, root_id=root.id, source=backend, label=label)
                session.add(rec)
                session.commit()
                job_id, rec_id = job.id, rec.id

            self._stop.clear()
            self._status = RecorderStatus("recording", rec_id, pass_id, now, spec.duration_s, 0.0, None)
            if self._live is not None:
                self._live.begin(spec.sample_rate_hz, spec.center_hz, spec.description)
            with self._sessions() as session:
                sat = session.get(Satellite, norad_id) if norad_id is not None else None
                self._subject = (sat.name if sat else None) or label or "manual"
            if self._events is not None:
                mhz = round(spec.center_hz / 1e6, 4)
                self._events.emit("info", "recorder", "recording_started",
                                  f"Recording started: {self._subject}, {mhz} MHz, {spec.duration_s:.0f} s",
                                  {"subject": self._subject, "duration_s": round(spec.duration_s), "center_freq_hz": spec.center_hz,
                                   "center_mhz": mhz, "source": backend},
                                  pass_id=pass_id, job_id=job_id, recording_id=rec_id)
            self._thread = threading.Thread(
                target=self._run, args=(device, spec, recordings_dir / name, job_id, rec_id, pass_id, on_finished, spec.start_time or now), name="recorder", daemon=True
            )
            self._thread.start()
            return rec_id

    def stop(self) -> None:
        with self._lock:
            if self._status.state == "recording":
                self._status.state = "stopping"
                self._stop.set()

    def shutdown(self) -> None:
        self.stop()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _on_block(self, block, total_samples: int, center_hz: int, sample_rate: int) -> None:
        with self._lock:
            self._status.elapsed_s = total_samples / sample_rate
        if self._live is not None:
            self._live.push(block, sample_rate, center_hz)

    def _run(self, device: SdrDevice, spec: RecordSpec, base: Path, job_id: int, rec_id: int,
             pass_id: int | None, on_finished: FinishedCallback | None, start_time: datetime) -> None:
        error: str | None = None
        result = None
        try:
            with device:
                result = record(device, spec, base, self._stop,
                                on_block=lambda b, n, c: self._on_block(b, n, c, spec.sample_rate_hz))
        except Exception as exc:  # noqa: BLE001
            log.exception("Recording %s failed", rec_id)
            error = str(exc)
        finally:
            orbit_rel = self._write_orbit(spec, base, start_time, result, error)
            self._finish(job_id, rec_id, base, result, error, orbit_rel)
            self._emit_result(rec_id, job_id, pass_id, result, error, spec)
            if self._live is not None:
                self._live.end()
            if on_finished is not None:
                try:
                    on_finished(rec_id, job_id, pass_id, result, error)
                except Exception:  # noqa: BLE001
                    log.exception("on_finished callback failed")
            with self._lock:
                self._status = RecorderStatus(last_error=error)

    def _emit_result(self, rec_id: int, job_id: int, pass_id: int | None, result, error: str | None, spec: RecordSpec) -> None:
        if self._events is None:
            return
        ids = {"pass_id": pass_id, "job_id": job_id, "recording_id": rec_id}
        subject = getattr(self, "_subject", "manual")
        if error:
            self._events.emit("error", "recorder", "recording_failed", f"Recording failed: {error}",
                              {"error": error, "subject": subject}, **ids)
        elif result is not None:
            secs = round(result.num_samples / spec.sample_rate_hz, 1)
            details = {"subject": subject, "samples": result.num_samples, "size_bytes": result.size_bytes, "duration_s": secs,
                       "size_mb": round(result.size_bytes / 1e6, 1)}
            if result.stopped_early:
                self._events.emit("info", "recorder", "recording_stopped",
                                  f"Recording stopped early: {subject}, {secs} s", details, **ids)
            else:
                self._events.emit("info", "recorder", "recording_finished",
                                  f"Recording finished: {subject}, {secs} s, {details['size_mb']} MB", details, **ids)

    def _write_orbit(self, spec: RecordSpec, base, start_time: datetime, result, error: str | None) -> str | None:
        """A pályametaadat-fájl írása; a hibája nem érinti a felvételt."""
        try:
            doc = orbit_mod.build_orbit_doc(
                spec.orbit, base=base, start=start_time, planned_duration_s=spec.duration_s,
                sample_rate_hz=spec.sample_rate_hz, gain_db=spec.gain_db, fmt=spec.fmt,
                stopped_early=bool(result and result.stopped_early), error=error)
            doc["radio"]["backend"] = (spec.extra_meta or {}).get("satrx:sdr_backend")
            return orbit_mod.write_orbit_file(base, doc).name
        except Exception:  # noqa: BLE001
            log.exception("Could not write orbit metadata for %s", base.name)
            return None

    def _finish(self, job_id: int, rec_id: int, base, result, error: str | None,
                orbit_rel: str | None = None) -> None:
        now = datetime.now(timezone.utc)
        with self._sessions() as session:
            job, rec = session.get(Job, job_id), session.get(Recording, rec_id)
            job.finished_at = rec.ended_at = now
            job.error = error
            job.status = "failed" if error else ("stopped" if result and result.stopped_early else "done")
            data = sigmf.data_path(base)
            rec.size_bytes = data.stat().st_size if data.exists() else 0
            rec.orbit_meta_path = orbit_rel
            session.commit()
        log.info("Recording %s finished: %s", rec_id, job.status)
