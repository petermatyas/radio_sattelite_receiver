from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import satrx
from satrx.api.deps import require_auth
from satrx.api.config_routes import router as config_router
from satrx.api.channel_routes import router as channel_router
from satrx.api.ops_routes import router as ops_router
from satrx.api.recording_routes import router as recording_router
from satrx.api.routes import router
from satrx.api.storage_routes import router as storage_router
from satrx.api.stream_routes import router as stream_router, ws_router
from satrx.core.config import PROJECT_ROOT, Settings, get_settings
from satrx.core.db import make_engine, make_session_factory, run_migrations
from satrx import sdnotify
from satrx.core.events import EventLog
from satrx.core.recovery import recover_after_crash
from satrx.tools import apply_stored_tools
from satrx.core.logging import setup_logging
from satrx.core.seed import seed_defaults
from satrx.pipeline.registry import default_registry
from satrx.pipeline.runner import PipelineExecutor, PipelineRunner
from satrx.hardware.probe import CachedProbe
from satrx.recording.live import LiveSpectrum
from satrx.storage.export import ExportManager
from satrx.storage.manager import StorageManager
from satrx.storage.move import MoveManager
from satrx.storage.retention import apply_retention
from satrx.storage.service import busy_recording_ids
from satrx.recording.manager import RecordingManager
from satrx.passes.service import get_observer
from satrx.scheduler.service import Scheduler
from satrx.tle import service as tle_service
from satrx.tle.client import fetch_tle

log = logging.getLogger(__name__)
WEB_DIST = PROJECT_ROOT / "web" / "dist"


def _startup_refresh(app: FastAPI, settings: Settings) -> None:
    """Régi TLE-k frissítése és az átvonulások újraszámolása (háttérszálon)."""
    with app.state.session_factory() as session:
        tle_service.refresh_stale(session, fetcher=app.state.tle_fetcher, events=app.state.events)
        if get_observer(session, settings) is not None:
            log.info("Pass prediction: %d passes", app.state.scheduler.recompute())


def create_app(settings: Settings | None = None, tle_fetcher=fetch_tle, startup_refresh: bool = True,
               scheduler_enabled: bool = True) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.ensure_dirs()
        setup_logging(settings.data_path / "logs")
        run_migrations(settings.db_path)
        engine = make_engine(settings.db_path)
        app.state.settings = settings
        app.state.tle_fetcher = tle_fetcher
        app.state.engine = engine
        app.state.session_factory = make_session_factory(engine)
        app.state.events = events = EventLog(app.state.session_factory)
        app.state.storage = storage = StorageManager(settings, app.state.session_factory)
        storage.ensure_roots()
        with app.state.session_factory() as _s:
            apply_stored_tools(_s, settings)
        recover_after_crash(app.state.session_factory, storage, events,
                            extra_part_dirs=[settings.data_path / "exports"])
        app.state.registry = default_registry(PROJECT_ROOT / "plugins")
        app.state.runner = PipelineRunner(settings, app.state.session_factory, app.state.registry, events, storage=storage)
        app.state.executor = executor = PipelineExecutor(app.state.runner)
        app.state.live = LiveSpectrum()
        app.state.sdr_probe = CachedProbe(settings)
        app.state.recorder = RecordingManager(settings, app.state.session_factory, events=events,
                                              live=app.state.live, storage=storage)
        busy = lambda session: busy_recording_ids(session, app.state.recorder.status())  # noqa: E731
        app.state.mover = MoveManager(storage, app.state.session_factory, busy, events)
        app.state.exporter = ExportManager(settings, storage, app.state.session_factory, busy, events)
        app.state.exporter.start()
        app.state.scheduler = Scheduler(settings, app.state.session_factory, app.state.recorder,
                                        events=events, executor=executor)
        def run_retention() -> None:
            with app.state.session_factory() as session:
                apply_retention(session, storage, settings, events, busy=busy_recording_ids(session, app.state.recorder.status()))
        app.state.scheduler.retention = run_retention
        executor.start()
        events.prune()
        with app.state.session_factory() as session:
            seed_defaults(session)
        log.info("satrx %s elindult, adatkönyvtár: %s", satrx.__version__, settings.data_path)
        if not settings.observer.is_set:
            log.warning("Megfigyelési pont nincs megadva - a UI-n adható meg.")
        if scheduler_enabled:
            app.state.scheduler.start()
        sdnotify.ready(f"satrx {satrx.__version__}")
        events.emit("info", "system", "service_started", f"satrx {satrx.__version__} started (SDR: {settings.sdr.backend})",
                    {"version": satrx.__version__, "backend": settings.sdr.backend})
        task = asyncio.create_task(asyncio.to_thread(_startup_refresh, app, settings)) if startup_refresh else None
        yield
        sdnotify.stopping()
        app.state.scheduler.stop()
        app.state.mover.cancel()
        app.state.mover.join(10)
        app.state.exporter.stop()
        app.state.recorder.shutdown()
        app.state.executor.stop()
        if task is not None:
            await task
        engine.dispose()

    app = FastAPI(title="satrx", version=satrx.__version__, lifespan=lifespan)

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "version": satrx.__version__}

    @app.get("/api/status", dependencies=[Depends(require_auth)])
    def status(request: Request) -> dict:
        s: Settings = request.app.state.settings
        with request.app.state.session_factory() as session:
            observer_set = get_observer(session, s) is not None
        return {
            "version": satrx.__version__,
            "observer_set": observer_set,
            "sdr_backend": s.sdr.backend,
            "data_dir": str(s.data_path),
        }

    app.include_router(router)
    app.include_router(recording_router)
    app.include_router(config_router)
    app.include_router(channel_router)
    app.include_router(ops_router)
    app.include_router(storage_router)
    app.include_router(stream_router)
    app.include_router(ws_router)

    if (WEB_DIST / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            f = (WEB_DIST / path).resolve()
            if path and f.is_file() and WEB_DIST.resolve() in f.parents:
                return FileResponse(f)
            return FileResponse(WEB_DIST / "index.html")

    return app
