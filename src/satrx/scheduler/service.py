from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from satrx import sdnotify
from satrx.core.config import Settings
from satrx.core.events import EventLog
from satrx.core.models import Antenna, Job, Pass, Pipeline, RadioProfile, Satellite, TimedEvent, WatchRule
from satrx.hardware.antenna import AntennaSwitch, NoopSwitch
from satrx.hardware.base import SdrCaps, SdrError
from satrx.passes.doppler import DopplerTracker
from satrx.passes.service import get_observer, recompute_passes
from satrx.recording.manager import DEVICE_ID, BusyError, RecordingManager
from satrx.pipeline.runner import PipelineError, PipelineExecutor
from satrx.recording.orbit import OrbitContext
from satrx.recording.recorder import RecordResult, RecordSpec

log = logging.getLogger(__name__)

TICK_S = 2.0
RECOMPUTE_EVERY_S = 1800
MIN_REMAINING_S = 5  # ennél rövidebb maradék ablakot nem indítunk el

# recorded_callback(recording_id, pass_id): a sikeres felvétel után (a 4. fázisban innen indul a pipeline)
RecordedCallback = Callable[[int, int], None]


class Scheduler:
    def __init__(self, settings: Settings, session_factory: sessionmaker[Session], recorder: RecordingManager,
                 antenna_switch: AntennaSwitch | None = None, events: EventLog | None = None,
                 executor: PipelineExecutor | None = None):
        self._events = events
        self._executor = executor
        self._settings = settings
        self._sessions = session_factory
        self._recorder = recorder
        self._switch = antenna_switch or NoopSwitch()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.on_recorded: list[RecordedCallback] = []
        self.retention: Callable[[], None] | None = None  # percenként hívódik (adatmegőrzés)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def resources(self) -> dict[str, SdrCaps]:
        return {DEVICE_ID: self._recorder.capabilities()}

    def recompute(self, now: datetime | None = None) -> int:
        with self._lock, self._sessions() as session:
            return recompute_passes(session, self._settings, self.resources(), now)

    # ---- futtatás -------------------------------------------------------------------------

    def tick(self, now: datetime | None = None) -> int | None:
        """Egy ütemezési lépés; az elindított átvonulás azonosítóját adja (vagy None)."""
        now = now or datetime.now(timezone.utc)
        sched = self._settings.scheduling
        with self._lock, self._sessions() as session:
            for p in session.scalars(select(Pass).where(
                    Pass.status.in_(("planned", "partial")),
                    Pass.rec_end <= now + timedelta(seconds=MIN_REMAINING_S))):
                p.status = "missed"
                self._emit("warning", "pass_missed", "The pass was not recorded", p,
                           {"norad_id": p.norad_id, "event_id": p.event_id, "label": p.label, "aos": p.aos.isoformat()})
            session.commit()

            due = session.scalars(
                select(Pass).where(Pass.status.in_(("planned", "partial")),
                                   Pass.rec_start <= now + timedelta(seconds=sched.prep_s))
                .order_by(Pass.rec_start)).first()
            if due is None:
                return None

            state = self._recorder.status()
            if state.state != "idle":
                if state.state == "recording" and state.pass_id is None:
                    log.info("Scheduled pass %s is due, stopping manual recording", due.id)
                    self._recorder.stop()
                return None
            if due.rec_start > now:
                return None  # még nem kezdődik az ablak
            return self._start_pass(session, due, now)

    def _start_pass(self, session: Session, p: Pass, now: datetime) -> int | None:
        if p.event_id is not None:
            return self._start_event(session, p, now)
        rule = session.get(WatchRule, p.rule_id) if p.rule_id else None
        sat = session.get(Satellite, p.norad_id) if p.norad_id is not None else None
        profile = session.get(RadioProfile, rule.profile_id) if rule else None
        pipeline = session.get(Pipeline, rule.pipeline_id) if rule else None
        observer = get_observer(session, self._settings)
        if None in (rule, sat, profile, pipeline, observer) or not (sat.tle_line1 and sat.tle_line2):
            return self._config_missing(session, p, "its rule, satellite, profile or pipeline is missing")

        doppler = (DopplerTracker(sat.tle_line1, sat.tle_line2, observer, profile.center_freq_hz, sat.name)
                   if profile.doppler_correction else None)
        spec = RecordSpec(
            center_hz=profile.center_freq_hz, sample_rate_hz=profile.sample_rate_hz,
            duration_s=(p.rec_end - now).total_seconds(), gain_db=profile.gain_db,
            fmt=self._recorder.native_format(), antenna=None,
            description=f"{sat.name} pass", doppler=doppler,
            retune_step_hz=self._settings.scheduling.retune_step_hz, start_time=now,
            orbit=self._orbit_context(sat, observer, profile, p, rule),
            extra_meta={"satrx:norad_id": sat.norad_id, "satrx:satellite": sat.name, "satrx:pass_id": p.id,
                        "satrx:max_elevation_deg": p.max_elevation_deg, "satrx:profile": profile.name},
        )
        return self._launch(session, p, spec, profile, pipeline, norad_id=sat.norad_id, label=sat.name)

    def _start_event(self, session: Session, p: Pass, now: datetime) -> int | None:
        """Időalapú esemény: a profil frekvenciáján, a megadott időablakban; nincs műhold, Doppler és pályaadat."""
        ev = session.get(TimedEvent, p.event_id)
        profile = session.get(RadioProfile, ev.profile_id) if ev else None
        pipeline = session.get(Pipeline, ev.pipeline_id) if ev else None
        if None in (ev, profile, pipeline):
            return self._config_missing(session, p, "its event, profile or pipeline is missing")
        iso = lambda d: d.isoformat().replace("+00:00", "Z") if d else None  # noqa: E731
        orbit = OrbitContext(
            nominal_hz=profile.center_freq_hz, name=ev.name, observer=get_observer(session, self._settings),
            profile_name=profile.name, doppler_tracking=False,
            pass_info={"pass_id": p.id, "event_id": ev.id, "event_name": ev.name, "aos": iso(p.aos), "los": iso(p.los),
                       "planned_rec_start": iso(p.rec_start), "planned_rec_end": iso(p.rec_end),
                       "partial": p.status == "partial"})
        spec = RecordSpec(
            center_hz=profile.center_freq_hz, sample_rate_hz=profile.sample_rate_hz,
            duration_s=(p.rec_end - now).total_seconds(), gain_db=profile.gain_db,
            fmt=self._recorder.native_format(), antenna=None, description=f"{ev.name} (timed event)", doppler=None,
            start_time=now, orbit=orbit,
            extra_meta={"satrx:event_id": ev.id, "satrx:event": ev.name, "satrx:pass_id": p.id, "satrx:profile": profile.name})
        return self._launch(session, p, spec, profile, pipeline, norad_id=None, label=ev.name)

    def _config_missing(self, session: Session, p: Pass, what: str) -> None:
        log.error("Pass %s cannot be started: %s", p.id, what)
        p.status = "failed"
        session.commit()
        self._emit("error", "pass_config_missing", f"The recording cannot start: {what}", p)
        return None

    def _launch(self, session: Session, p: Pass, spec: RecordSpec, profile: RadioProfile, pipeline: Pipeline,
                norad_id: int | None, label: str) -> int | None:
        antenna = session.get(Antenna, profile.antenna_id) if profile.antenna_id else None
        original_status = p.status
        p.status = "recording"
        session.commit()
        try:
            self._switch.select(antenna.switch_port if antenna else None)
            self._recorder.start(spec, norad_id=norad_id, pass_id=p.id, pipeline_id=pipeline.id,
                                 on_finished=self._on_finished, label=label if norad_id is None else None)
        except BusyError:
            p.status = original_status
            session.commit()
            return None
        except (SdrError, OSError) as exc:
            log.error("Pass %s: could not start recording: %s", p.id, exc)
            p.status = "failed"
            session.commit()
            self._emit("error", "recording_start_failed", f"Could not start the recording: {exc}", p,
                       {"error": str(exc)})
            return None
        log.info("Pass %s (%s) recording started", p.id, label)
        return p.id

    def _on_finished(self, rec_id: int, job_id: int, pass_id: int | None, result: RecordResult | None,
                     error: str | None) -> None:
        if pass_id is None:
            return
        ok = error is None and result is not None and result.num_samples > 0
        with self._lock, self._sessions() as session:
            p = session.get(Pass, pass_id)
            if p is None:
                return
            p.status = "done" if ok else "failed"
            session.commit()
            pipeline = session.get(Pipeline, session.get(Job, job_id).pipeline_id or 0)
            has_steps = ok and pipeline is not None and bool(pipeline.steps)
            pipeline_id = pipeline.id if pipeline else None
        if ok:
            if has_steps and self._executor is not None:
                try:
                    self._executor.submit(rec_id, pipeline_id, pass_id)
                except PipelineError as exc:
                    with self._sessions() as session:
                        session.get(Pass, pass_id).status = "failed"
                        session.commit()
                    if self._events:
                        self._events.emit("error", "pipeline", "pipeline_start_failed", str(exc),
                                          {"error": str(exc)}, pass_id=pass_id, recording_id=rec_id)
            for cb in self.on_recorded:
                try:
                    cb(rec_id, pass_id)
                except Exception:  # noqa: BLE001
                    log.exception("on_recorded callback failed")

    @staticmethod
    def _orbit_context(sat: Satellite, observer, profile: RadioProfile, p: Pass, rule: WatchRule) -> OrbitContext:
        iso = lambda d: d.isoformat().replace("+00:00", "Z") if d else None  # noqa: E731
        return OrbitContext(
            nominal_hz=profile.center_freq_hz, norad_id=sat.norad_id, name=sat.name,
            tle_line1=sat.tle_line1, tle_line2=sat.tle_line2, tle_epoch=sat.tle_epoch, observer=observer,
            profile_name=profile.name, doppler_tracking=profile.doppler_correction,
            pass_info={"pass_id": p.id, "rule_id": rule.id, "aos": iso(p.aos), "tca": iso(p.tca), "los": iso(p.los),
                       "max_elevation_deg": p.max_elevation_deg, "planned_rec_start": iso(p.rec_start),
                       "planned_rec_end": iso(p.rec_end), "partial": p.status == "partial"},
        )

    def _emit(self, level: str, code: str, message: str, p: Pass, details: dict | None = None) -> None:
        if self._events is not None:
            self._events.emit(level, "scheduler", code, message, details, pass_id=p.id)

    # ---- háttérszál -------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def _loop(self) -> None:
        next_recompute = time.monotonic() + RECOMPUTE_EVERY_S  # az első számítást az induló TLE-frissítés végzi
        next_retention = time.monotonic() + 30
        while not self._stop.wait(TICK_S):
            sdnotify.watchdog_ping()
            try:
                if time.monotonic() >= next_recompute:
                    self.recompute()
                    if self._events is not None:
                        self._events.prune()
                    next_recompute = time.monotonic() + RECOMPUTE_EVERY_S
                self.tick()
                if self.retention is not None and time.monotonic() >= next_retention:
                    next_retention = time.monotonic() + 60
                    self.retention()
            except Exception:  # noqa: BLE001
                log.exception("Scheduler tick failed")
