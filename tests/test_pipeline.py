import json
import time
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from satrx.api.app import create_app
from satrx.core.config import Settings
from satrx.core.events import EventLog
from satrx.core.models import Event
from satrx.hardware.mock import MockDevice
from satrx.pipeline.base import ArtifactSpec, NoResult, PipelineContext, Processor, ProcessorError
from satrx.pipeline.registry import ProcessorRegistry, load_plugins
from satrx.processors.fm_demod import FmDemod, FmDemodParams
from satrx.recording import orbit, sigmf
from satrx.recording.recorder import RecordSpec, record
from tests.test_scheduler import (BUDAPEST, ISS_TEXT, T0, StepPlan, _fetch, _passes, _rule,  # noqa: F401
                                  _settings, _wait_idle, env)

from pydantic import BaseModel

SR = 250_000
F0 = 145_800_000


# ---- segédek ------------------------------------------------------------------------------------

def _record_mock(tmp_path, name="rec", duration=2.0, doppler=None):
    base = tmp_path / name
    t0 = datetime.now(timezone.utc)
    spec = RecordSpec(F0, SR, duration, fmt="cf32_le", start_time=t0,
                      doppler=doppler(t0) if doppler else None)
    with MockDevice(realtime=False) as dev:
        record(dev, spec, base)
    return base


def _ctx(base, workdir, orbit_doc=None):
    workdir.mkdir(parents=True, exist_ok=True)
    return PipelineContext(recording_id=1, job_id=1, norad_id=None, base=base, info=sigmf.read_info(base),
                           orbit=orbit_doc, workdir=workdir)


def _wav_peak_hz(path: Path) -> tuple[float, int]:
    with wave.open(str(path)) as w:
        rate = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(float)
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.fft.rfftfreq(len(x), 1 / rate)[np.argmax(spec)]), rate


# ---- FM demodulátor -----------------------------------------------------------------------------

def test_fm_demod_recovers_the_1khz_tone(tmp_path):
    base = _record_mock(tmp_path)
    ctx = _ctx(base, tmp_path / "out")
    (art,) = FmDemod().run(ctx, FmDemodParams(carrier_offset_hz=10_000))
    peak_hz, rate = _wav_peak_hz(art.path)
    assert rate == 50_000                       # 250 kS/s / 5
    assert abs(peak_hz - 1000) < 20
    assert -12 < art.meta["rms_dbfs"] < -3      # 3 kHz hub / 5 kHz deviáció ~ 0,6 csúcs
    assert art.meta["duration_s"] == pytest.approx(2.0, abs=0.05)
    assert ctx.state["audio_path"] == art.path


def test_fm_demod_follows_doppler_curve_from_orbit_metadata(tmp_path):
    base = _record_mock(tmp_path)
    # a mock jele a közép-frekvencia +10 kHz-én van; a pálya-idősor ezt a vivőt jelöli
    doc = {"track": {"t_s": [0.0, 10.0], "doppler_freq_hz": [F0 + 10_000, F0 + 10_000]}}
    (art,) = FmDemod().run(_ctx(base, tmp_path / "o", doc), FmDemodParams())
    assert abs(_wav_peak_hz(art.path)[0] - 1000) < 20 and art.meta["doppler_followed"] is True
    # ugyanez követés nélkül (offset 0) a csatornán kívülre esik: a hang nem a 1 kHz-es tónus
    (off,) = FmDemod().run(_ctx(base, tmp_path / "o2"), FmDemodParams())
    assert off.meta["rms_dbfs"] != art.meta["rms_dbfs"] and off.meta["doppler_followed"] is False


def test_fm_demod_rejects_too_low_sample_rate(tmp_path):
    base = tmp_path / "low"
    with sigmf.SigMFWriter(base, "cu8", 100_000, F0) as w:      # 100 kS/s < 2 x 60 kHz csatorna
        w.write(np.zeros(50_000, dtype=np.complex64))
    with pytest.raises(ProcessorError, match="decimation"):
        FmDemod().run(_ctx(base, tmp_path / "o"), FmDemodParams(bandwidth_hz=60_000))


def test_read_segments_split_at_retunes(tmp_path):
    t0 = datetime.now(timezone.utc)
    base = tmp_path / "seg"
    spec = RecordSpec(F0, SR, 3.0, fmt="cu8", doppler=StepPlan(t0), start_time=t0)
    with MockDevice(realtime=False) as dev:
        record(dev, spec, base)
    info = sigmf.read_info(base)
    segs = list(sigmf.read_segments(base, block_samples=100_000))
    assert sum(len(s[2]) for s in segs) == info.num_samples
    assert [s[0] for s in segs] == list(np.cumsum([0] + [len(s[2]) for s in segs[:-1]]))
    starts = {c["core:sample_start"]: c["core:frequency"] for c in info.captures}
    for first, freq, _ in segs:
        assert freq == [f for st, f in sorted(starts.items()) if st <= first][-1]


# ---- pályametaadat -----------------------------------------------------------------------------

def test_orbit_track_matches_predictor(tmp_path):
    from satrx.passes.predictor import predict_passes
    from satrx.tle.client import parse_tle

    tle = parse_tle(ISS_TEXT, 25544)
    p = predict_passes(tle.line1, tle.line2, BUDAPEST, T0, T0 + timedelta(days=1), 10.0)[0]
    ctx = orbit.OrbitContext(nominal_hz=F0, norad_id=25544, name="ISS", tle_line1=tle.line1, tle_line2=tle.line2,
                             tle_epoch=tle.epoch, observer=BUDAPEST)
    dur = (p.los - p.aos).total_seconds()
    track = orbit.compute_track(ctx, p.aos, dur)
    assert len(track["t_s"]) == int(dur) + 1 or len(track["t_s"]) == int(dur) + 2
    i = int(round((p.tca - p.aos).total_seconds()))
    assert track["el_deg"][i] == pytest.approx(p.max_elevation_deg, abs=0.7)
    assert track["el_deg"][0] == pytest.approx(10.0, abs=0.5)
    assert track["doppler_freq_hz"][0] > F0 > track["doppler_freq_hz"][-1]
    assert max(track["range_rate_km_s"]) > 0 > min(track["range_rate_km_s"])
    assert {"az_deg", "range_km", "subpoint_lat_deg", "subpoint_lon_deg"} <= set(track)


def test_orbit_doc_without_orbit_data_has_nulls(tmp_path):
    base = _record_mock(tmp_path, "manual", 1.0)
    doc = orbit.build_orbit_doc(None, base=base, start=datetime.now(timezone.utc), planned_duration_s=1.0,
                                sample_rate_hz=SR, gain_db=None, fmt="cf32_le", stopped_early=False, error=None)
    assert doc["orbit_schema"] == 1 and doc["satellite"] is None and doc["track"] is None
    assert doc["recording"]["num_samples"] == SR and doc["radio"]["initial_center_hz"] == F0


def test_scheduled_recording_saves_orbit_file(env, tmp_path):
    client, app = env
    _rule(client, pipeline_id=1)
    sched = app.state.scheduler
    sched.recompute(now=T0)
    p = _passes(app)[0]
    assert sched.tick(now=p.rec_end - timedelta(seconds=10)) == p.id
    _wait_idle(app)

    rec = client.get("/api/recordings").json()[0]
    assert rec["orbit_meta_path"] and rec["orbit_meta_path"].endswith(".orbit.json")
    doc = json.loads((app.state.settings.recordings_path / rec["orbit_meta_path"]).read_text())
    assert doc["satellite"]["norad_id"] == 25544 and doc["satellite"]["tle"]["line1"].startswith("1 25544")
    assert doc["satellite"]["tle"]["epoch"].startswith("2026-10-11")
    assert doc["observer"]["lat"] == pytest.approx(47.4979)
    assert doc["pass"]["pass_id"] == p.id and doc["pass"]["max_elevation_deg"] == pytest.approx(p.max_elevation_deg)
    assert doc["pass"]["partial"] is False
    assert doc["radio"]["nominal_hz"] == F0 and doc["radio"]["doppler_tracking"] is True
    assert doc["recording"]["actual_duration_s"] == pytest.approx(10.0) and doc["recording"]["error"] is None
    track = doc["track"]
    assert len(track["t_s"]) == 11 and len(track["doppler_freq_hz"]) == 11
    # a SigMF metában is ott a kulcsadat
    meta = sigmf.read_info(app.state.settings.recordings_path / rec["path"])
    assert meta.extra["satrx:pass_id"] == p.id


def test_manual_recording_with_known_satellite_saves_track(env):
    client, app = env
    r = client.post("/api/recorder/start", json={"center_freq_hz": F0, "sample_rate_hz": SR, "duration_s": 2,
                                                 "norad_id": 25544})
    assert r.status_code == 202, r.text
    _wait_idle(app)
    rec = client.get("/api/recordings").json()[0]
    doc = json.loads((app.state.settings.recordings_path / rec["orbit_meta_path"]).read_text())
    assert doc["satellite"]["norad_id"] == 25544 and doc["track"] is not None
    assert doc["radio"]["profile"] == "manual" and doc["radio"]["doppler_tracking"] is False


# ---- plugin-keret ------------------------------------------------------------------------------

class _Params(BaseModel):
    fail: bool = False


class Boom(Processor):
    name, title, params_schema = "boom", "Always fails", _Params

    def run(self, ctx, params):
        ctx.log("about to explode")
        raise ProcessorError("boom happened")


class Nothing(Processor):
    name, title, params_schema = "nothing", "No decode", _Params

    def run(self, ctx, params):
        raise NoResult("the satellite was not transmitting")


class Crash(Processor):
    name, title, params_schema = "crash", "Unexpected error", _Params

    def run(self, ctx, params):
        return {}["missing"]


class MakeImage(Processor):
    name, title, params_schema = "make_image", "Dummy image", _Params

    def run(self, ctx, params):
        path = ctx.workdir / "pic.png"
        Image.new("RGB", (640, 480), (200, 30, 30)).save(path)
        return [ArtifactSpec("image", path, {"width": 640})]


class Escape(Processor):
    name, title, params_schema = "escape", "Writes outside the artifact dir", _Params

    def run(self, ctx, params):
        evil = ctx.base.parent / "evil.txt"
        evil.write_text("x")
        return [ArtifactSpec("text", evil)]


def _pipeline(client, name, steps, keep_iq=True):
    r = client.post("/api/pipelines", json={"name": name, "mode": "test", "steps": steps, "keep_iq": keep_iq})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
def papp(env):
    client, app = env
    for cls in (Boom, Nothing, Crash, MakeImage, Escape):
        app.state.registry.register(cls)
    return client, app


def _manual_recording(client, app):
    assert client.post("/api/recorder/start", json={"center_freq_hz": F0, "sample_rate_hz": SR,
                                                    "duration_s": 2}).status_code == 202
    _wait_idle(app)
    return client.get("/api/recordings").json()[0]["id"]


def _run(client, app, rec_id, pipeline_id):
    r = client.post(f"/api/recordings/{rec_id}/process", json={"pipeline_id": pipeline_id})
    assert r.status_code == 202, r.text
    assert app.state.executor.wait_idle(30)
    return client.get(f"/api/jobs/{r.json()['job_id']}").json()


def test_pipeline_validation_on_create(papp):
    client, _ = papp
    bad = {"name": "x", "mode": "t", "steps": [{"processor": "nope"}]}
    assert client.post("/api/pipelines", json=bad).status_code == 422
    bad = {"name": "x", "mode": "t", "steps": [{"processor": "fm_demod", "params": {"bandwidth_hz": -5}}]}
    assert client.post("/api/pipelines", json=bad).status_code == 422
    procs = {p["name"]: p for p in client.get("/api/processors").json()}
    assert "bandwidth_hz" in procs["fm_demod"]["params_schema"]["properties"]


def test_fm_pipeline_end_to_end_and_gallery(papp):
    client, app = papp
    rec_id = _manual_recording(client, app)
    pid = _pipeline(client, "fm10k", [{"processor": "fm_demod", "params": {"carrier_offset_hz": 10000}}])
    job = _run(client, app, rec_id, pid)

    assert job["status"] == "done" and job["kind"] == "processing" and job["recording_id"] == rec_id
    assert [(s["processor"], s["status"]) for s in job["steps"]] == [("fm_demod", "ok")]
    assert "audio rate 50000" in job["steps"][0]["log"]
    (art,) = job["artifacts"]
    assert art["kind"] == "audio" and art["has_thumbnail"] is False

    gallery = client.get("/api/artifacts", params={"kind": "audio"}).json()
    assert [a["id"] for a in gallery] == [art["id"]]
    f = client.get(f"/api/artifacts/{art['id']}/file")
    assert f.status_code == 200 and f.headers["content-type"].startswith("audio/")
    assert f.content[:4] == b"RIFF"
    assert client.get(f"/api/artifacts/{art['id']}/thumbnail").status_code == 404
    assert any(e["code"] == "pipeline_done" for e in client.get("/api/events").json())

    assert client.delete(f"/api/artifacts/{art['id']}").status_code == 204
    assert client.get("/api/artifacts").json() == []


def test_image_artifact_gets_thumbnail(papp):
    client, app = papp
    job = _run(client, app, _manual_recording(client, app), _pipeline(client, "img", [{"processor": "make_image"}]))
    (art,) = job["artifacts"]
    assert art["kind"] == "image" and art["has_thumbnail"] is True
    thumb = client.get(f"/api/artifacts/{art['id']}/thumbnail")
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"
    import io
    assert max(Image.open(io.BytesIO(thumb.content)).size) <= 256


def test_failed_step_is_visible_with_reason_and_skips_rest(papp):
    client, app = papp
    rec_id = _manual_recording(client, app)
    pid = _pipeline(client, "failing", [{"processor": "make_image"}, {"processor": "boom"}, {"processor": "make_image"}])
    job = _run(client, app, rec_id, pid)

    assert job["status"] == "failed" and job["error"] == "boom happened"
    assert [s["status"] for s in job["steps"]] == ["ok", "failed", "skipped"]
    assert job["steps"][1]["error"] == "boom happened" and "about to explode" in job["steps"][1]["log"]
    ev = [e for e in client.get("/api/events", params={"level": "error"}).json() if e["code"] == "pipeline_failed"]
    assert ev and ev[0]["job_id"] == job["id"] and ev[0]["recording_id"] == rec_id
    assert client.get("/api/events/summary").json()["error"] >= 1


def test_unexpected_exception_is_captured_with_traceback(papp):
    client, app = papp
    job = _run(client, app, _manual_recording(client, app), _pipeline(client, "crashing", [{"processor": "crash"}]))
    assert job["status"] == "failed" and "KeyError" in job["steps"][0]["log"]


def test_no_result_is_not_an_error(papp):
    client, app = papp
    job = _run(client, app, _manual_recording(client, app),
               _pipeline(client, "silent", [{"processor": "nothing"}, {"processor": "make_image"}]))
    assert job["status"] == "no_result" and job["error"] is None
    assert [s["status"] for s in job["steps"]] == ["no_result", "skipped"]
    codes = {e["code"]: e["level"] for e in client.get("/api/events").json()}
    assert codes["no_result"] == "info" and "pipeline_failed" not in codes


def test_artifact_must_stay_inside_artifact_dir(papp):
    client, app = papp
    job = _run(client, app, _manual_recording(client, app), _pipeline(client, "esc", [{"processor": "escape"}]))
    assert job["status"] == "failed" and "invalid" in job["error"]


def test_retry_and_process_errors(papp):
    client, app = papp
    rec_id = _manual_recording(client, app)
    pid = _pipeline(client, "failing2", [{"processor": "boom"}])
    job = _run(client, app, rec_id, pid)
    r = client.post(f"/api/jobs/{job['id']}/retry")
    assert r.status_code == 202 and r.json()["job_id"] != job["id"]
    app.state.executor.wait_idle(30)
    assert client.post(f"/api/recordings/{rec_id}/process", json={"pipeline_id": 1}).status_code == 409  # nincs lépés
    rec_job = client.get("/api/jobs", params={"kind": "recording"}).json()[0]
    assert client.post(f"/api/jobs/{rec_job['id']}/retry").status_code == 409
    assert client.post("/api/recordings/999/process", json={"pipeline_id": pid}).status_code == 409


def test_keep_iq_false_deletes_raw_data_but_keeps_orbit(papp):
    client, app = papp
    rec_id = _manual_recording(client, app)
    pid = _pipeline(client, "cleanup", [{"processor": "make_image"}], keep_iq=False)
    assert _run(client, app, rec_id, pid)["status"] == "done"
    rec = client.get("/api/recordings").json()[0]
    base = app.state.settings.recordings_path / rec["path"]
    assert rec["deleted"] is True and not sigmf.data_path(base).exists()
    assert sigmf.orbit_path(base).exists()
    assert client.post(f"/api/recordings/{rec_id}/process", json={"pipeline_id": pid}).status_code == 409


def test_deleting_recording_removes_all_files_and_artifacts(papp):
    client, app = papp
    rec_id = _manual_recording(client, app)
    _run(client, app, rec_id, _pipeline(client, "img2", [{"processor": "make_image"}]))
    root = app.state.settings
    assert client.delete(f"/api/recordings/{rec_id}").status_code == 204
    assert [p.name for p in root.recordings_path.iterdir() if p.name != ".satrx-root"] == []
    assert not (root.artifacts_path / f"rec{rec_id}").exists()
    assert client.get("/api/artifacts").json() == []


def test_scheduled_pass_runs_pipeline_and_ends_done(papp):
    client, app = papp
    pid = _pipeline(client, "sched-img", [{"processor": "make_image"}])
    _rule(client, pipeline_id=pid)
    sched = app.state.scheduler
    sched.recompute(now=T0)
    p = _passes(app)[0]
    assert sched.tick(now=p.rec_end - timedelta(seconds=10)) == p.id
    _wait_idle(app)
    assert app.state.executor.wait_idle(30)

    assert next(x for x in _passes(app) if x.id == p.id).status == "done"
    jobs = client.get("/api/jobs").json()
    assert {j["kind"] for j in jobs} == {"recording", "processing"}
    proc = next(j for j in jobs if j["kind"] == "processing")
    assert proc["status"] == "done" and proc["pass_id"] == p.id and proc["satellite_name"] == "ISS (ZARYA)"
    assert client.get("/api/artifacts").json()[0]["satellite_name"] == "ISS (ZARYA)"


def test_scheduled_pass_with_failing_pipeline_marks_pass_failed(papp):
    client, app = papp
    _rule(client, pipeline_id=_pipeline(client, "sched-boom", [{"processor": "boom"}]))
    sched = app.state.scheduler
    sched.recompute(now=T0)
    p = _passes(app)[0]
    sched.tick(now=p.rec_end - timedelta(seconds=10))
    _wait_idle(app)
    app.state.executor.wait_idle(30)
    assert next(x for x in _passes(app) if x.id == p.id).status == "failed"


def test_plugin_directory_loading(tmp_path):
    (tmp_path / "good.py").write_text(
        "from pydantic import BaseModel\nfrom satrx.pipeline.base import Processor\n"
        "class P(BaseModel): pass\n"
        "class Good(Processor):\n    name='good'; title='Good'; params_schema=P\n    def run(self, ctx, params): return []\n"
        "def register(registry): registry.register(Good)\n")
    (tmp_path / "broken.py").write_text("raise RuntimeError('syntax of life')\n")
    (tmp_path / "noregister.py").write_text("x = 1\n")
    reg = ProcessorRegistry()
    load_plugins(reg, tmp_path)
    assert reg.get("good") is not None
    assert len(reg.load_errors) == 2 and any("broken.py" in e for e in reg.load_errors)


# ---- események, állapotellenőrzés, hozzáférés ------------------------------------------------------

def test_recording_failure_is_reported_in_events_and_health(env):
    client, app = env

    class Broken(MockDevice):
        def read(self, n):
            raise RuntimeError("USB device disconnected")

    app.state.recorder._device_factory = lambda: Broken(realtime=False)
    assert client.post("/api/recorder/start", json={"center_freq_hz": F0, "sample_rate_hz": SR,
                                                    "duration_s": 5}).status_code == 202
    _wait_idle(app)
    time.sleep(0.2)
    ev = next(e for e in client.get("/api/events").json() if e["code"] == "recording_failed")
    assert ev["level"] == "error" and "USB device disconnected" in ev["message"] and ev["recording_id"]
    health = client.get("/api/health/detailed").json()
    sdr = next(c for c in health["components"] if c["id"] == "sdr")
    assert sdr["status"] == "error" and "USB device disconnected" in sdr["message"] and health["status"] == "error"
    job = client.get("/api/jobs").json()[0]
    assert job["status"] == "failed" and "USB device disconnected" in job["error"]


def test_missed_pass_emits_warning(env):
    client, app = env
    _rule(client)
    app.state.scheduler.recompute(now=T0)
    p = _passes(app)[0]
    app.state.scheduler.tick(now=p.rec_end + timedelta(minutes=1))
    ev = next(e for e in client.get("/api/events").json() if e["code"] == "pass_missed")
    assert ev["level"] == "warning" and ev["pass_id"] == p.id and ev["source"] == "scheduler"


def test_event_ack_summary_and_filters(env):
    client, app = env
    log: EventLog = app.state.events
    base_info = client.get("/api/events/summary").json()["info"]                 # pl. a "service_started" a konzolhoz
    log.emit("error", "storage", "disk_full", "disk full")
    log.emit("warning", "tle", "tle_refresh_failed", "tle failed")
    log.emit("info", "system", "hello", "hi")
    assert client.get("/api/events/summary").json() == {"error": 1, "warning": 1, "info": base_info + 1}
    assert [e["code"] for e in client.get("/api/events", params={"level": "error"}).json()] == ["disk_full"]
    assert [e["code"] for e in client.get("/api/events", params={"source": "tle"}).json()] == ["tle_refresh_failed"]
    err = client.get("/api/events", params={"level": "error"}).json()[0]
    assert client.post(f"/api/events/{err['id']}/ack").status_code == 204
    assert client.get("/api/events/summary").json()["error"] == 0
    assert len(client.get("/api/events", params={"unacked": True}).json()) == 2 + base_info
    assert client.post("/api/events/ack-all").json() == {"acknowledged": 2 + base_info}
    assert client.post("/api/events/9999/ack").status_code == 404


def test_event_pruning_keeps_unacknowledged_errors(env):
    _, app = env
    log: EventLog = app.state.events
    old = datetime.now(timezone.utc) - timedelta(days=40)
    with app.state.session_factory() as s:
        s.add_all([Event(ts=old, level="info", source="system", code="old_info", message="x"),
                   Event(ts=old, level="error", source="system", code="old_err_unacked", message="x"),
                   Event(ts=old, level="error", source="system", code="old_err_acked", message="x", acknowledged=True)])
        s.commit()
    assert log.prune() == 2
    with app.state.session_factory() as s:
        codes = {e.code for e in s.query(Event)}
    assert "old_err_unacked" in codes and not codes & {"old_info", "old_err_acked"}


def test_health_detailed_components(tmp_path):
    app = create_app(_settings(tmp_path), tle_fetcher=_fetch, startup_refresh=False, scheduler_enabled=False)
    with TestClient(app) as client:
        h = client.get("/api/health/detailed").json()
        comps = {c["id"]: c for c in h["components"]}
        assert set(comps) == {"sdr", "scheduler", "tle", "storage", "config"}
        assert comps["sdr"]["status"] == "warning" and comps["sdr"]["code"] == "sdr_mock"      # a szimulált SDR látszik
        assert comps["sdr"]["details"]["simulated"] is True and comps["storage"]["status"] == "ok"
        assert comps["scheduler"]["code"] == "scheduler_stopped"      # ebben a tesztben ki van kapcsolva
        assert comps["tle"]["code"] == "no_satellites"
        assert comps["config"]["code"] == "observer_missing" and "no_rules" in comps["config"]["details"]["problems"]
        assert h["status"] == "warning"

        client.put("/api/settings/observer", json={"lat": 47.5, "lon": 19.0, "alt_m": 100})
        client.post("/api/satellites", json={"norad_id": 25544})
        _rule(client, valid_to=(datetime.now(timezone.utc) + timedelta(days=30)).isoformat())   # a mai naphoz képest aktív
        h = client.get("/api/health/detailed").json()
        comps = {c["id"]: c for c in h["components"]}
        # a tesztadat TLE-je rögzített korú: a valódi mai naphoz képest elavulhat, ez itt nem a vizsgált dolog
        assert comps["tle"]["code"] != "no_satellites" and comps["config"]["status"] == "ok"


def test_health_reports_stale_tle(env):
    client, app = env
    from satrx.core.models import Satellite
    with app.state.session_factory() as s:
        s.get(Satellite, 25544).tle_epoch = datetime.now(timezone.utc) - timedelta(days=5)
        s.commit()
    tle = next(c for c in client.get("/api/health/detailed").json()["components"] if c["id"] == "tle")
    assert tle["status"] == "warning" and tle["code"] == "tle_stale" and tle["details"]["age_h"] >= 119


def test_artifact_files_accept_query_token(tmp_path):
    s = _settings(tmp_path)
    s.server.auth_token = "secret"
    app = create_app(s, tle_fetcher=_fetch, startup_refresh=False, scheduler_enabled=False)
    with TestClient(app) as client:
        h = {"Authorization": "Bearer secret"}
        assert client.get("/api/artifacts").status_code == 401
        assert client.get("/api/artifacts", headers=h).status_code == 200
        assert client.get("/api/artifacts/1/file").status_code == 401
        assert client.get("/api/artifacts/1/file", params={"token": "wrong"}).status_code == 401
        assert client.get("/api/artifacts/1/file", params={"token": "secret"}).status_code == 404  # auth ok, nincs fájl


class Producer(Processor):
    name, title, params_schema = "state_producer", "Writes to ctx.state", _Params

    def run(self, ctx, params):
        ctx.state["token"] = f"job{ctx.job_id}"
        return []


class Consumer(Processor):
    name, title, params_schema = "state_consumer", "Reads ctx.state", _Params

    def run(self, ctx, params):
        ctx.log(f"token={ctx.state.get('token')}")
        if ctx.state.get("token") != f"job{ctx.job_id}":
            raise ProcessorError("the step state was not shared")
        return []


def test_steps_of_a_job_share_ctx_state(papp):
    client, app = papp
    for cls in (Producer, Consumer):
        app.state.registry.register(cls)
    job = _run(client, app, _manual_recording(client, app),
               _pipeline(client, "stateful", [{"processor": "state_producer"}, {"processor": "state_consumer"}]))
    assert job["status"] == "done" and job["steps"][1]["log"] == f"token=job{job['id']}"
    other = _run(client, app, _manual_recording(client, app),
                 _pipeline(client, "stateful2", [{"processor": "state_consumer"}]))
    assert other["status"] == "failed"          # külön job állapota nem szivárog át
