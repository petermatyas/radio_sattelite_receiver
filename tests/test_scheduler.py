import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from satrx.api.app import create_app
from satrx.core.config import SdrConfig, Settings
from satrx.passes.doppler import DopplerTracker
from satrx.passes.predictor import Observer, predict_passes
from satrx.recording import sigmf
from satrx.recording.recorder import RecordSpec, record
from satrx.hardware.mock import MockDevice
from satrx.scheduler.planner import Candidate, Fixed, plan
from satrx.tle.client import Tle, TleError, parse_tle

T0 = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
ISS_TEXT = """ISS (ZARYA)
1 25544U 98067A   26284.82161096  .00007651  00000+0  14771-3 0  9996
2 25544  51.6314  82.9019 0006707 250.7442 109.2821 15.48831270589830
"""
BUDAPEST = Observer(47.4979, 19.0402, 100)


def cand(key, start_min, dur_min=10, prio=0, el=45.0, partial=True, resources=("sdr0",), buf=60):
    aos = T0 + timedelta(minutes=start_min)
    return Candidate(key, aos, aos + timedelta(minutes=dur_min), el, prio, partial, buf, buf, resources)


# ---- planner ------------------------------------------------------------------------------

def test_planner_non_overlapping_all_planned():
    d = plan([cand(1, 0), cand(2, 30)], ["sdr0"])
    assert d[1].status == d[2].status == "planned"
    assert d[1].rec_start == T0 - timedelta(seconds=60)


def test_planner_higher_priority_wins_and_loser_gets_partial_tail():
    # a 2. átvonulás 5 perccel a 1. vége előtt kezdődik -> csak a vége használható
    d = plan([cand(1, 0, prio=5), cand(2, 5, prio=0)], ["sdr0"])
    assert d[1].status == "planned"
    assert d[2].status == "partial"
    assert d[2].rec_start == d[1].rec_end  # közvetlenül az 1. után kezdődik
    assert d[2].rec_end == T0 + timedelta(minutes=5 + 10, seconds=60)


def test_planner_partial_threshold():
    # 1. átvonulás 0-10 perc (nagyobb prioritás); a 2. csak a 10. perc után kap helyet
    long_tail = plan([cand(1, 0, 10, prio=5, buf=0), cand(2, 9.5, 10, prio=0, buf=0)], ["sdr0"], min_partial_s=60)
    assert long_tail[2].status == "partial"          # 9,5 perc hasznos jel marad
    assert long_tail[2].rec_start == T0 + timedelta(minutes=10)
    short_tail = plan([cand(1, 0, 10, prio=5, buf=0), cand(2, 0.5, 10, prio=0, buf=0)], ["sdr0"], min_partial_s=60)
    assert short_tail[2].status == "skipped_conflict"  # csak 30 mp marad a 60 mp-es küszöb alatt


def test_planner_no_partial_when_disallowed():
    d = plan([cand(1, 0, prio=5), cand(2, 5, partial=False)], ["sdr0"])
    assert d[2].status == "skipped_conflict"


def test_planner_score_uses_elevation_as_tiebreak():
    d = plan([cand(1, 0, el=20), cand(2, 0, el=70)], ["sdr0"])
    assert d[2].status == "planned" and d[1].status != "planned"


def test_planner_two_resources_run_in_parallel():
    d = plan([cand(1, 0, resources=("a", "b")), cand(2, 0, resources=("a", "b"))], ["a", "b"])
    assert {d[1].resource, d[2].resource} == {"a", "b"}
    assert d[1].status == d[2].status == "planned"


def test_planner_unsupported_and_fixed_busy():
    assert plan([cand(1, 0, resources=())], ["sdr0"])[1].status == "unsupported"
    busy = Fixed("sdr0", T0 - timedelta(minutes=5), T0 + timedelta(minutes=20))
    assert plan([cand(1, 0, partial=False)], ["sdr0"], [busy])[1].status == "skipped_conflict"


# ---- doppler ------------------------------------------------------------------------------

def test_doppler_shape_over_pass():
    tle = parse_tle(ISS_TEXT, 25544)
    p = predict_passes(tle.line1, tle.line2, BUDAPEST, T0 - timedelta(hours=8), T0 + timedelta(hours=16), 5.0)[0]
    trk = DopplerTracker(tle.line1, tle.line2, BUDAPEST, 145_800_000)
    start, mid, end = (trk.freq_at(t) - 145_800_000 for t in (p.aos, p.tca, p.los))
    assert start > 2_000 and end < -2_000          # közeledik: magasabb, távolodik: alacsonyabb frekvencia
    assert abs(mid) < 1_000
    assert start < 4_000 and end > -4_000          # VHF-n az ISS max. kb. +-3.5 kHz


class StepPlan:
    """Mintaszámmal arányosan emelkedő frekvencia a retune ellenőrzéséhez."""
    def __init__(self, t0):
        self.t0 = t0

    def freq_at(self, t):
        return 145_800_000 + int((t - self.t0).total_seconds() * 1000)


def test_recorder_retunes_and_marks_captures(tmp_path):
    t0 = datetime.now(timezone.utc)
    spec = RecordSpec(145_800_000, 250_000, 3.0, fmt="cu8", doppler=StepPlan(t0), retune_step_hz=500, start_time=t0)
    dev = MockDevice(realtime=False)
    with dev:
        record(dev, spec, tmp_path / "d")
    info = sigmf.read_info(tmp_path / "d")
    assert len(info.captures) >= 4
    freqs = [c["core:frequency"] for c in info.captures]
    assert freqs == sorted(freqs) and freqs[-1] > freqs[0]
    assert dev.center_hz == freqs[-1]


# ---- integráció ----------------------------------------------------------------------------

def _fetch(norad_id: int) -> Tle:
    if norad_id != 25544:
        raise TleError("No GP data found")
    return parse_tle(ISS_TEXT, norad_id)


def _settings(tmp_path, realtime=False) -> Settings:
    s = Settings(data_dir=tmp_path)
    s.sdr = SdrConfig(backend="mock", device_args={"realtime": realtime})
    s.scheduling.buffer_before_s = 0
    s.scheduling.buffer_after_s = 0
    return s


@pytest.fixture
def env(tmp_path):
    app = create_app(_settings(tmp_path), tle_fetcher=_fetch, startup_refresh=False, scheduler_enabled=False)
    with TestClient(app) as client:
        client.put("/api/settings/observer", json={"lat": 47.4979, "lon": 19.0402, "alt_m": 100})
        assert client.post("/api/satellites", json={"norad_id": 25544}).status_code == 201
        yield client, app


def _rule(client, **over):
    body = {"norad_id": 25544, "valid_from": T0.isoformat(), "valid_to": (T0 + timedelta(days=2)).isoformat(),
            "profile_id": 1, "pipeline_id": 1, "min_elevation_deg": 10, **over}
    r = client.post("/api/rules", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _passes(app):
    from sqlalchemy import select
    from satrx.core.models import Pass
    with app.state.session_factory() as s:
        return list(s.scalars(select(Pass).order_by(Pass.aos)))


def _wait_idle(app, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if app.state.recorder.status().state == "idle":
            return
        time.sleep(0.05)
    raise AssertionError("recorder still busy")


def test_recompute_from_rules_and_window_limits(env):
    client, app = env
    rule = _rule(client, valid_to=(T0 + timedelta(hours=12)).isoformat())
    app.state.scheduler.recompute(now=T0)
    passes = _passes(app)
    assert passes and all(p.rule_id == rule["id"] and p.status == "planned" for p in passes)
    assert all(T0 <= p.aos and p.los <= T0 + timedelta(hours=12) for p in passes)
    assert all(p.rec_start == p.aos and p.rec_end == p.los for p in passes)  # puffer 0
    assert all(p.device == "sdr0" for p in passes)


def test_overlapping_rules_for_same_satellite_higher_priority_wins(env):
    client, app = env
    low = _rule(client, priority=0)
    high = _rule(client, priority=5)
    app.state.scheduler.recompute(now=T0)
    passes = _passes(app)
    assert passes and {p.rule_id for p in passes} == {high["id"]}
    assert low["id"] != high["id"]


def test_tick_records_pass_with_doppler_and_marks_done(env, tmp_path):
    client, app = env
    rule = _rule(client)
    sched = app.state.scheduler
    sched.recompute(now=T0)
    p = _passes(app)[0]

    assert sched.tick(now=p.rec_start - timedelta(minutes=10)) is None  # még korai
    started = sched.tick(now=p.rec_end - timedelta(seconds=10))
    assert started == p.id
    _wait_idle(app)

    done = next(x for x in _passes(app) if x.id == p.id)
    assert done.status == "done"
    rec = client.get("/api/recordings").json()[0]
    info = sigmf.read_info(tmp_path / "recordings" / rec["path"])
    assert info.extra["satrx:norad_id"] == 25544 and info.extra["satrx:pass_id"] == p.id
    assert info.num_samples == 10 * 250_000
    # a Doppler-eltolás miatt a felvétel közepe a nominális frekvencia körül, de nem pontosan azon van
    assert abs(info.center_freq_hz - 145_800_000) < 4_000
    assert rule["id"] == done.rule_id

    # a futott átvonulást egy újraszámolás nem veszi fel újra
    sched.recompute(now=p.rec_end - timedelta(seconds=5))
    assert [x.status for x in _passes(app) if x.id == p.id] == ["done"]


def test_missed_pass_is_marked(env):
    client, app = env
    _rule(client)
    app.state.scheduler.recompute(now=T0)
    p = _passes(app)[0]
    assert app.state.scheduler.tick(now=p.rec_end + timedelta(minutes=1)) is None
    assert next(x for x in _passes(app) if x.id == p.id).status == "missed"


def test_scheduled_pass_preempts_manual_recording(tmp_path):
    app = create_app(_settings(tmp_path, realtime=True), tle_fetcher=_fetch, startup_refresh=False,
                     scheduler_enabled=False)
    with TestClient(app) as client:
        client.put("/api/settings/observer", json={"lat": 47.4979, "lon": 19.0402, "alt_m": 100})
        client.post("/api/satellites", json={"norad_id": 25544})
        _rule(client)
        sched = app.state.scheduler
        sched.recompute(now=T0)
        p = _passes(app)[0]

        r = client.post("/api/recorder/start",
                        json={"center_freq_hz": 145_800_000, "sample_rate_hz": 250_000, "duration_s": 60})
        assert r.status_code == 202
        assert sched.tick(now=p.rec_end - timedelta(seconds=10)) is None  # leállítást kér, még nem indít
        _wait_idle(app)
        manual = client.get("/api/recordings").json()[0]
        assert manual["size_bytes"] < 60 * 250_000 * 2

        assert sched.tick(now=p.rec_end - timedelta(seconds=10)) == p.id
        app.state.recorder.stop()
        _wait_idle(app)


# ---- konfigurációs API -----------------------------------------------------------------------

def test_config_api_validation(env):
    client, _ = env
    assert [p["name"] for p in client.get("/api/profiles").json()][0].startswith("ISS")
    assert {p["name"] for p in client.get("/api/pipelines").json()} >= {"Record only", "FM audio", "FM audio + waterfall", "ISS SSTV"}

    bad = {"name": "x", "center_freq_hz": 100_000_000, "bandwidth_hz": 500_000, "sample_rate_hz": 250_000}
    assert client.post("/api/profiles", json=bad).status_code == 422
    ok = client.post("/api/profiles", json={**bad, "bandwidth_hz": 20_000})
    assert ok.status_code == 201
    assert client.post("/api/profiles", json={**bad, "bandwidth_hz": 20_000}).status_code == 409  # név egyedi

    base = {"norad_id": 25544, "valid_from": T0.isoformat(), "valid_to": (T0 + timedelta(days=1)).isoformat(),
            "profile_id": ok.json()["id"], "pipeline_id": 1}
    assert client.post("/api/rules", json={**base, "valid_from": "2026-10-12T08:00:00"}).status_code == 422
    assert client.post("/api/rules", json={**base, "valid_to": T0.isoformat()}).status_code == 422
    assert client.post("/api/rules", json={**base, "norad_id": 999}).status_code == 422
    assert client.post("/api/rules", json={**base, "profile_id": 9999}).status_code == 422

    rule = client.post("/api/rules", json=base).json()
    assert rule["satellite_name"] == "ISS (ZARYA)"
    assert client.delete(f"/api/profiles/{ok.json()['id']}").status_code == 409  # szabály hivatkozik rá
    assert client.put(f"/api/rules/{rule['id']}", json={**base, "priority": 7}).json()["priority"] == 7
    assert client.delete(f"/api/rules/{rule['id']}").status_code == 204
    assert client.delete(f"/api/profiles/{ok.json()['id']}").status_code == 204


def test_antenna_crud(env):
    client, _ = env
    a = client.post("/api/antennas", json={"name": "QFH 137", "freq_min_hz": 136_000_000,
                                           "freq_max_hz": 138_000_000, "switch_port": "1"})
    assert a.status_code == 201
    assert client.get("/api/antennas").json()[0]["switch_port"] == "1"
    assert client.delete(f"/api/antennas/{a.json()['id']}").status_code == 204
