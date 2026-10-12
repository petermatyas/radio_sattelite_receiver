"""8. fázis: SSE állapotfolyam, élő waterfall (WebSocket), új felvételi események."""
import asyncio
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from satrx.api.app import create_app
from satrx.core.config import SdrConfig
from satrx.dsp.spectrum import quantize_db, spectrum_rows
from satrx.recording import live as live_mod
from satrx.recording.live import LiveSpectrum
from tests.test_scheduler import _fetch, _settings, env  # noqa: F401

SR = 250_000
F0 = 145_800_000


def _tone(n, hz, amp=0.5):
    return (amp * np.exp(2j * np.pi * hz * np.arange(n) / SR)).astype(np.complex64)


def _realtime_app(tmp_path, token=None):
    s = _settings(tmp_path)
    s.sdr = SdrConfig(backend="mock", device_args={"realtime": True})
    s.server.auth_token = token
    return create_app(s, tle_fetcher=_fetch, startup_refresh=False, scheduler_enabled=False)


# ---- spektrumsorok --------------------------------------------------------------------------------------

def test_spectrum_rows_level_position_and_row_count():
    rows = spectrum_rows(_tone(SR // 2, 20_000), 1024, 5)
    assert rows.shape == (5, 1024) and rows.dtype == np.float32
    peak = int(np.argmax(rows[0]))
    assert abs(peak - (512 + 20_000 / (SR / 1024))) <= 1                     # a közép-frekvencia a sor közepén van
    assert -9 < rows[0, peak] < -3                                           # 0,5 amplitúdó ~ -6 dBFS
    assert spectrum_rows(_tone(100, 1000), 1024, 3).shape == (0, 1024)       # túl rövid blokk: nincs sor
    assert spectrum_rows(_tone(2048, 1000), 1024, 10).shape[0] == 2          # nem lehet több sor, mint FFT


def test_live_rows_match_the_offline_spectrogram_level(tmp_path):
    """Az élő nézet és az utólagos waterfall ugyanazt a dBFS-skálát használja."""
    from satrx.dsp.spectrum import compute_spectrogram
    from satrx.recording import sigmf
    samples = _tone(SR, 20_000)
    with sigmf.SigMFWriter(tmp_path / "x", "cf32_le", SR, F0) as w:
        w.write(samples)
    offline = compute_spectrogram(tmp_path / "x", fft_size=1024, max_rows=10)
    live = spectrum_rows(samples, 1024, 10)
    assert float(offline.db.max()) == pytest.approx(float(live.max()), abs=0.5)


def test_quantize_db_range():
    q = quantize_db(np.array([-200.0, -120.0, -60.0, 0.0, 30.0]), -120, 0)
    assert q.dtype == np.uint8 and list(q) == [0, 0, 127, 255, 255]


# ---- hub ------------------------------------------------------------------------------------------------------

def test_hub_broadcast_late_subscriber_retune_and_end():
    async def main():
        hub = LiveSpectrum(fft_size=256, rows_per_s=10)
        loop = asyncio.get_running_loop()
        sub = hub.subscribe(loop)
        assert hub.has_subscribers and not hub.active

        await asyncio.to_thread(hub.begin, SR, F0, "test")
        kind, header = await asyncio.wait_for(sub.queue.get(), 2)
        assert kind == "json" and header["type"] == "header" and header["bins"] == 256 and header["center_freq_hz"] == F0

        await asyncio.to_thread(hub.push, _tone(131_072, 10_000), SR, F0)
        kind, rows = await asyncio.wait_for(sub.queue.get(), 2)
        n_rows = round(131_072 / SR * 10)
        assert kind == "rows" and len(rows) == n_rows * 256
        first = np.frombuffer(rows[:256], dtype=np.uint8)
        assert abs(int(np.argmax(first)) - (128 + 10_000 / (SR / 256))) <= 1

        late = hub.subscribe(loop)                       # menet közben csatlakozik: azonnal megkapja a fejlécet
        assert (await asyncio.wait_for(late.queue.get(), 2))[1]["type"] == "header"

        await asyncio.to_thread(hub.push, _tone(131_072, 10_000), SR, F0 + 2_000)     # Doppler-újrahangolás
        kinds = [(await asyncio.wait_for(sub.queue.get(), 2)) for _ in range(2)]
        assert kinds[0][1] == {"type": "center", "center_freq_hz": F0 + 2_000} and kinds[1][0] == "rows"

        await asyncio.to_thread(hub.end)
        assert (await asyncio.wait_for(sub.queue.get(), 2))[1] == {"type": "end"} and not hub.active
        hub.unsubscribe(sub)
        hub.unsubscribe(late)
        assert not hub.has_subscribers

    asyncio.run(main())


def test_hub_does_no_work_without_subscribers(monkeypatch):
    calls = []
    monkeypatch.setattr(live_mod, "spectrum_rows", lambda *a: calls.append(a) or np.zeros((1, 8), np.float32))
    hub = LiveSpectrum(256, 10)
    hub.begin(SR, F0)
    hub.push(_tone(131_072, 1000), SR, F0)
    assert calls == []                                    # nincs néző = nincs FFT


def test_slow_client_drops_the_oldest_rows_instead_of_blocking():
    async def main():
        hub = LiveSpectrum(64, 10)
        sub = hub.subscribe(asyncio.get_running_loop())
        await asyncio.to_thread(hub.begin, SR, F0)
        for i in range(live_mod.QUEUE_SIZE + 50):
            await asyncio.to_thread(hub.push, _tone(25_000, 1000 + i), SR, F0)
        await asyncio.sleep(0.2)
        assert sub.queue.qsize() == live_mod.QUEUE_SIZE        # korlátos: a felvételi szál nem torlódik

    asyncio.run(main())


# ---- WebSocket ------------------------------------------------------------------------------------------------

def test_websocket_streams_rows_during_a_recording_and_signals_the_end(tmp_path):
    with TestClient(_realtime_app(tmp_path)) as client:
        with client.websocket_connect("/api/live/waterfall") as ws:
            assert client.post("/api/recorder/start", json={"center_freq_hz": F0, "sample_rate_hz": SR,
                                                            "duration_s": 30}).status_code == 202
            header = ws.receive_json()
            assert header["type"] == "header" and header["bins"] == 1024 and header["sample_rate_hz"] == SR
            assert header["center_freq_hz"] == F0 and header["db_lo"] == -120 and header["db_hi"] == 0

            msg = ws.receive()
            while "bytes" not in msg or msg["bytes"] is None:
                msg = ws.receive()
            data = msg["bytes"]
            assert len(data) % 1024 == 0 and len(data) >= 1024
            row = np.frombuffer(data[:1024], dtype=np.uint8)
            tone_bin = 512 + 10_000 / (SR / 1024)               # a mock FM-vivője a közép +10 kHz-én
            assert 532 <= int(np.argmax(row)) <= 575 and abs(int(np.argmax(row)) - tone_bin) < 25
            assert row[600:].mean() < row.max() - 40            # zajpadló vs. jel

            assert client.post("/api/recorder/stop").status_code == 204
            for _ in range(200):
                msg = ws.receive()
                if msg.get("text") and json.loads(msg["text"]).get("type") == "end":
                    break
            else:
                raise AssertionError("no end message")


def test_websocket_late_joiner_gets_the_header(tmp_path):
    with TestClient(_realtime_app(tmp_path)) as client:
        client.post("/api/recorder/start", json={"center_freq_hz": F0, "sample_rate_hz": SR, "duration_s": 30})
        time.sleep(0.4)
        with client.websocket_connect("/api/live/waterfall") as ws:
            assert ws.receive_json()["type"] == "header"
        client.post("/api/recorder/stop")


def test_websocket_requires_the_token(tmp_path):
    with TestClient(_realtime_app(tmp_path, token="secret")) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/live/waterfall"):
                pass
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/live/waterfall?token=wrong"):
                pass
        with client.websocket_connect("/api/live/waterfall?token=secret"):
            pass


def test_no_fft_work_is_done_when_nobody_watches(tmp_path, monkeypatch):
    calls = []
    real = live_mod.spectrum_rows
    monkeypatch.setattr(live_mod, "spectrum_rows", lambda *a: calls.append(1) or real(*a))
    with TestClient(_realtime_app(tmp_path)) as client:
        client.post("/api/recorder/start", json={"center_freq_hz": F0, "sample_rate_hz": SR, "duration_s": 30})
        time.sleep(1.2)
        client.post("/api/recorder/stop")
    assert calls == []


# ---- SSE -----------------------------------------------------------------------------------------------------

def _read_sse(client, url, duration_s=2.5, during=None):
    """A folyam (event, adat) párjai; a `during` egy másik szálon fut a folyam közben."""
    out = []
    if during:
        threading.Thread(target=during, daemon=True).start()
    with client.stream("GET", f"{url}{'&' if '?' in url else '?'}duration_s={duration_s}&interval_s=0.3") as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        event = None
        for line in r.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:") and event:
                out.append((event, json.loads(line[5:])))
                event = None
    return out


def test_sse_state_snapshot_shape(env):
    client, app = env
    client.put("/api/settings/observer", json={"lat": 47.5, "lon": 19.0, "alt_m": 100})
    now = datetime.now(timezone.utc)
    client.post("/api/rules", json={"norad_id": 25544, "valid_from": now.isoformat(),
                                    "valid_to": (now + timedelta(days=5)).isoformat(), "profile_id": 1, "pipeline_id": 1})
    msgs = _read_sse(client, "/api/stream", duration_s=0.5)
    name, state = msgs[0]
    assert name == "state"
    assert state["recorder"]["state"] == "idle" and state["live_waterfall"] is False
    assert state["unacknowledged"] == {"error": 0, "warning": 0, "info": state["unacknowledged"]["info"]}
    assert state["next_pass"]["satellite"] == "ISS (ZARYA)" and state["next_pass"]["rec_start"].endswith("Z")
    assert state["processing_jobs"] == 0 and state["time"].endswith("Z")


def test_sse_pushes_new_events_within_a_second_and_updates_counters(env):
    client, app = env
    log = app.state.events
    msgs = _read_sse(client, "/api/stream", duration_s=3,
                     during=lambda: (time.sleep(0.8), log.emit("error", "storage", "disk_full", "disk full", {"x": 1})))
    events = [p for n, p in msgs if n == "event"]
    assert [e["code"] for e in events] == ["disk_full"] and events[0]["level"] == "error" and events[0]["details"] == {"x": 1}
    states = [p for n, p in msgs if n == "state"]
    assert states[0]["unacknowledged"]["error"] == 0 and states[-1]["unacknowledged"]["error"] == 1
    assert not [e for e in events if e["code"] != "disk_full"]            # a régi események nem ismétlődnek


def test_sse_reports_a_running_recording(tmp_path):
    with TestClient(_realtime_app(tmp_path)) as client:
        client.post("/api/recorder/start", json={"center_freq_hz": F0, "sample_rate_hz": SR, "duration_s": 30})
        time.sleep(0.8)
        msgs = _read_sse(client, "/api/stream", duration_s=0.4)
        client.post("/api/recorder/stop")
    rec = msgs[0][1]["recorder"]
    assert rec["state"] == "recording" and rec["duration_s"] == 30 and rec["elapsed_s"] > 0.3
    assert rec["recording"]["center_freq_hz"] == F0 and msgs[0][1]["live_waterfall"] is True


def test_sse_requires_the_token(tmp_path):
    with TestClient(_realtime_app(tmp_path, token="secret")) as client:
        assert client.get("/api/stream?duration_s=0.1").status_code == 401
        with client.stream("GET", "/api/stream?duration_s=0.1&token=secret") as r:
            assert r.status_code == 200


# ---- felvételi események -----------------------------------------------------------------------------------

def test_recording_start_and_finish_events(env):
    client, app = env
    client.post("/api/recorder/start", json={"center_freq_hz": F0, "sample_rate_hz": SR, "duration_s": 1})
    for _ in range(100):
        if app.state.recorder.status().state == "idle":
            break
        time.sleep(0.05)
    time.sleep(0.2)
    events = {e["code"]: e for e in client.get("/api/events").json()}
    assert events["recording_started"]["level"] == "info" and events["recording_started"]["recording_id"]
    done = events["recording_finished"]
    assert done["details"]["samples"] == SR and done["recording_id"] == events["recording_started"]["recording_id"]
