"""Valós idejű csatornák: SSE (állapot + új események) és WebSocket (élő waterfall)."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from satrx.api.deps import require_auth
from satrx.core.models import Event, Job, Pass, Recording, Satellite

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])
ws_router = APIRouter(prefix="/api")  # a WebSocket hitelesítését kézzel végezzük


def _json_default(o: Any) -> str:
    if isinstance(o, datetime):
        return o.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    raise TypeError(type(o))


def _sse(name: str, payload: Any) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, default=_json_default)}\n\n"


def _event_dict(e: Event) -> dict[str, Any]:
    return {"id": e.id, "ts": e.ts, "level": e.level, "source": e.source, "code": e.code, "message": e.message,
            "details": e.details, "pass_id": e.pass_id, "job_id": e.job_id, "recording_id": e.recording_id,
            "acknowledged": e.acknowledged}


def snapshot(app) -> dict[str, Any]:
    """A fejléc állapotsorához szükséges, olcsó pillanatkép (a teljes állapotellenőrzés külön végpont)."""
    st = app.state
    rec = st.recorder.status()
    now = datetime.now(timezone.utc)
    with st.session_factory() as s:
        counts = dict(s.execute(select(Event.level, func.count()).where(Event.acknowledged.is_(False))
                                .group_by(Event.level)).all())
        active_jobs = s.scalar(select(func.count()).select_from(Job).where(
            Job.kind == "processing", Job.status.in_(("queued", "running")))) or 0
        nxt = s.scalars(select(Pass).where(Pass.status.in_(("planned", "partial")), Pass.rec_start > now)
                        .order_by(Pass.rec_start)).first()
        next_pass = None
        if nxt is not None:
            sat = s.get(Satellite, nxt.norad_id) if nxt.norad_id is not None else None
            next_pass = {"pass_id": nxt.id, "satellite": sat.name if sat else (nxt.label or "-"),
                         "kind": "event" if nxt.event_id else "pass",
                         "rec_start": nxt.rec_start, "max_elevation_deg": nxt.max_elevation_deg}
        recording = None
        if rec.state != "idle" and rec.recording_id is not None:
            row = s.get(Recording, rec.recording_id)
            sat = s.get(Satellite, row.norad_id) if row and row.norad_id else None
            recording = {"id": rec.recording_id, "pass_id": rec.pass_id, "satellite": sat.name if sat else None,
                         "center_freq_hz": row.center_freq_hz if row else None}
    return {
        "time": now,
        "recorder": {"state": rec.state, "elapsed_s": rec.elapsed_s, "duration_s": rec.duration_s,
                     "last_error": rec.last_error, "recording": recording},
        "next_pass": next_pass,
        "processing_jobs": active_jobs,
        "unacknowledged": {"error": counts.get("error", 0), "warning": counts.get("warning", 0),
                           "info": counts.get("info", 0)},
        "live_waterfall": st.live.active,
        "sdr": {"backend": st.settings.sdr.backend, "simulated": st.settings.sdr.backend in ("mock", "replay")},
    }


@router.get("/stream")
async def stream(request: Request, duration_s: float = 0.0, interval_s: float = 1.0):
    """Server-Sent Events: `state` (másodpercenként) és `event` (új eseménynapló-bejegyzés).

    duration_s > 0: ennyi idő után a kapcsolat lezárul (a kliens újracsatlakozik); 0 = amíg a kliens él.
    """
    app = request.app
    interval = max(0.2, min(interval_s, 10.0))

    def newest_id() -> int:
        with app.state.session_factory() as s:
            return s.scalar(select(func.coalesce(func.max(Event.id), 0))) or 0

    def fresh_events(after: int) -> list[Event]:
        with app.state.session_factory() as s:
            rows = list(s.scalars(select(Event).where(Event.id > after).order_by(Event.id).limit(50)))
            s.expunge_all()
            return rows

    async def gen():
        last_id = await asyncio.to_thread(newest_id)
        started, last_ping = time.monotonic(), time.monotonic()
        yield "retry: 3000\n\n"
        while True:
            if await request.is_disconnected():
                return
            try:
                yield _sse("state", await asyncio.to_thread(snapshot, app))
                for e in await asyncio.to_thread(fresh_events, last_id):
                    last_id = max(last_id, e.id)
                    yield _sse("event", _event_dict(e))
            except Exception:  # noqa: BLE001 - egy hibás pillanatkép nem szakíthatja meg a folyamot
                log.exception("SSE snapshot failed")
            if duration_s and time.monotonic() - started >= duration_s:
                return
            if time.monotonic() - last_ping > 15:
                last_ping = time.monotonic()
                yield ": ping\n\n"
            await asyncio.sleep(interval)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@ws_router.websocket("/live/waterfall")
async def live_waterfall(ws: WebSocket) -> None:
    """Élő waterfall: JSON fejléc/állapotüzenetek és bináris spektrumsorok (uint8, `bins` bájt soronként)."""
    token = ws.app.state.settings.server.auth_token
    if token is not None:
        supplied = ws.query_params.get("token") or ""
        auth = ws.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = supplied or auth[7:]
        if not secrets.compare_digest(supplied, token):
            await ws.close(code=1008)
            return
    await ws.accept()
    hub = ws.app.state.live
    sub = hub.subscribe(asyncio.get_running_loop())

    async def sender() -> None:
        while True:
            kind, payload = await sub.queue.get()
            if kind == "json":
                await ws.send_json(payload)
            else:
                await ws.send_bytes(payload)

    async def receiver() -> None:  # a kliens lezárását ezen keresztül vesszük észre
        while True:
            await ws.receive_text()

    tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for t in tasks:
            t.cancel()
        hub.unsubscribe(sub)
