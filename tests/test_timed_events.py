"""Időalapú (NORAD ID nélküli) események: előfordulások, API, ütemezés, végrehajtás, konzol-események."""
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from satrx.api.app import create_app
from satrx.core.models import Pass
from satrx.recording import sigmf
from satrx.scheduler.occurrences import get_zone, next_occurrences, occurrences
from tests.test_scheduler import T0, _fetch, _passes, _rule, _settings, _wait_idle, env  # noqa: F401

UTC = timezone.utc
BUD = "Europe/Budapest"


def ev(start, duration_s=1800, recurrence="none", weekdays=(), until=None, tz="UTC"):
    return SimpleNamespace(start_at=start, duration_s=duration_s, recurrence=recurrence, weekdays=list(weekdays),
                           repeat_until=until, timezone=tz)


def d(y, m, day, h=0, mi=0):
    return datetime(y, m, day, h, mi, tzinfo=UTC)


# ---- előfordulások -------------------------------------------------------------------------------------------------

def test_one_off_event_window_overlap():
    e = ev(d(2026, 10, 12, 10), 3600)
    assert occurrences(e, d(2026, 10, 12), d(2026, 10, 13)) == [(d(2026, 10, 12, 10), d(2026, 10, 12, 11))]
    assert occurrences(e, d(2026, 10, 12, 10, 30), d(2026, 10, 13)) == [(d(2026, 10, 12, 10), d(2026, 10, 12, 11))]   # már fut
    assert occurrences(e, d(2026, 10, 12, 11), d(2026, 10, 13)) == []                                                  # lejárt
    assert occurrences(e, d(2026, 10, 11), d(2026, 10, 12, 10)) == []                                                  # még nem kezdődött


def test_daily_event_keeps_the_local_wall_clock_across_daylight_saving_time():
    """Minden nap 18:00 helyi idő: a nyári időszámításra váltáskor (2026-03-29) UTC-ben egy órával korábbra kerül."""
    e = ev(d(2026, 3, 27, 17), 1800, "daily", tz=BUD)            # 18:00 CET = 17:00Z
    occ = occurrences(e, d(2026, 3, 27), d(2026, 3, 31))
    starts = [s for s, _ in occ]
    assert starts == [d(2026, 3, 27, 17), d(2026, 3, 28, 17), d(2026, 3, 29, 16), d(2026, 3, 30, 16)]
    assert all(s.astimezone(ZoneInfo(BUD)).hour == 18 for s in starts)
    assert all(e_ - s == timedelta(minutes=30) for s, e_ in occ)


def test_weekly_event_runs_on_the_selected_weekdays_only():
    e = ev(d(2026, 10, 12, 6), 600, "weekly", weekdays=[0, 2], tz="UTC")                # hétfő (2026-10-12) és szerda
    occ = occurrences(e, d(2026, 10, 12), d(2026, 10, 27))
    assert [s.date().isoformat() for s, _ in occ] == ["2026-10-12", "2026-10-14", "2026-10-19", "2026-10-21", "2026-10-26"]
    default_day = ev(d(2026, 10, 14, 6), 600, "weekly", weekdays=[], tz="UTC")           # nincs megadva: a kezdő nap
    assert {s.weekday() for s, _ in occurrences(default_day, d(2026, 10, 14), d(2026, 11, 20))} == {2}


def test_recurrence_respects_first_start_and_end_date():
    e = ev(d(2026, 10, 12, 8), 600, "daily", until=d(2026, 10, 14, 8), tz="UTC")
    occ = [s for s, _ in occurrences(e, d(2026, 10, 1), d(2026, 11, 1))]
    assert occ == [d(2026, 10, 12, 8), d(2026, 10, 13, 8), d(2026, 10, 14, 8)]          # nem előbb, nem később


def test_an_occurrence_crossing_midnight_is_found_from_the_next_day():
    e = ev(d(2026, 10, 12, 23, 30), 3600, "daily", tz="UTC")
    occ = occurrences(e, d(2026, 10, 13, 0, 10), d(2026, 10, 13, 6))
    assert [(s, e_) for s, e_ in occ] == [(d(2026, 10, 12, 23, 30), d(2026, 10, 13, 0, 30))]


def test_timezone_validation_and_next_occurrences():
    with pytest.raises(ValueError):
        get_zone("Mars/Olympus")
    e = ev(d(2026, 10, 12, 8), 600, "daily", tz="UTC")
    nxt = next_occurrences(e, d(2026, 10, 12, 9), 4)
    assert nxt == [d(2026, 10, 13, 8), d(2026, 10, 14, 8), d(2026, 10, 15, 8), d(2026, 10, 16, 8)]
    assert next_occurrences(ev(d(2026, 10, 12, 8), 60), d(2026, 10, 13), 3) == []        # az egyszeri már elmúlt


# ---- API ---------------------------------------------------------------------------------------------------------------

def body(**over):
    base = {"name": "Teszt jeladó", "start_at": (T0 + timedelta(hours=1)).isoformat(), "duration_s": 600,
            "profile_id": 1, "pipeline_id": 1}
    return {**base, **over}


def test_timed_event_crud_validation_and_output(env):
    client, _ = env
    r = client.post("/api/timed-events", json=body(recurrence="weekly", timezone=BUD))
    assert r.status_code == 201, r.text
    out = r.json()
    start_local = datetime.fromisoformat(out["start_at"].replace("Z", "+00:00")).astimezone(ZoneInfo(BUD))
    assert out["weekdays"] == [start_local.weekday()]                          # weekly + üres lista: a kezdő nap
    assert out["profile_name"].startswith("ISS") and out["pipeline_name"] == "Record only"
    assert out["estimated_bytes"] == 250_000 * 2 * 600 and out["id"] > 0

    upd = client.put(f"/api/timed-events/{out['id']}", json=body(recurrence="daily", repeat_until=(T0 + timedelta(days=9)).isoformat()))
    assert upd.json()["recurrence"] == "daily" and upd.json()["weekdays"] == []        # nem heti: nincs napválasztás
    once = client.put(f"/api/timed-events/{out['id']}", json=body(recurrence="none", repeat_until=(T0 + timedelta(days=9)).isoformat()))
    assert once.json()["repeat_until"] is None                                         # az egyszeri eseménynek nincs vége-dátuma
    assert [e["id"] for e in client.get("/api/timed-events").json()] == [out["id"]]
    assert client.delete(f"/api/timed-events/{out['id']}").status_code == 204
    assert client.get("/api/timed-events").json() == [] and client.delete(f"/api/timed-events/{out['id']}").status_code == 404


@pytest.mark.parametrize("over,why", [
    ({"start_at": "2026-10-12T09:00:00"}, "időzóna nélküli kezdet"),
    ({"timezone": "Mars/Olympus"}, "ismeretlen időzóna"),
    ({"duration_s": 0}, "nulla hossz"),
    ({"duration_s": 7 * 3600}, "6 óránál hosszabb"),
    ({"recurrence": "weekly", "weekdays": [7]}, "érvénytelen nap"),
    ({"recurrence": "yearly"}, "ismeretlen ismétlődés"),
    ({"recurrence": "daily", "repeat_until": (T0).isoformat()}, "a vége a kezdet előtt"),
    ({"profile_id": 999}, "nincs ilyen profil"),
    ({"pipeline_id": 999}, "nincs ilyen pipeline"),
    ({"name": ""}, "üres név"),
    ({"priority": 500}, "túl nagy prioritás"),
])
def test_timed_event_validation(env, over, why):
    client, _ = env
    assert client.post("/api/timed-events", json=body(**over)).status_code == 422, why
    assert client.get("/api/timed-events").json() == []


def test_preview_shows_the_next_occurrences_without_saving(env):
    client, _ = env
    start = datetime.now(UTC) + timedelta(hours=1)
    r = client.post("/api/timed-events/preview", json=body(start_at=start.isoformat(), recurrence="daily", timezone="UTC"))
    assert r.status_code == 200
    times = [datetime.fromisoformat(t.replace("Z", "+00:00")) for t in r.json()["next_occurrences"]]
    assert len(times) == 8 and times[1] - times[0] == timedelta(days=1)
    assert r.json()["estimated_bytes"] == 250_000 * 2 * 600
    assert client.get("/api/timed-events").json() == []                                # semmi nem mentődött


# ---- ütemezés -----------------------------------------------------------------------------------------------------------

def test_events_are_scheduled_even_without_an_observer_location(tmp_path):
    app = create_app(_settings(tmp_path / "d"), tle_fetcher=_fetch, startup_refresh=False, scheduler_enabled=False)
    with TestClient(app) as client:
        assert client.get("/api/settings/observer").json() is None                      # nincs megfigyelési pont
        r = client.post("/api/timed-events", json=body(start_at=(datetime.now(UTC) + timedelta(hours=2)).isoformat(),
                                                       recurrence="daily", timezone="UTC", name="Napi jeladó"))
        assert r.status_code == 201
        passes = client.get("/api/passes", params={"hours": 72}).json()
        assert len(passes) >= 2 and all(p["kind"] == "event" and p["norad_id"] is None for p in passes)
        assert {p["satellite_name"] for p in passes} == {"Napi jeladó"} and all(p["status"] == "planned" for p in passes)
        assert passes[0]["event_id"] == r.json()["id"] and passes[0]["max_elevation_deg"] == 0
        # a pontos időablak: puffer nélkül
        row = _passes(app)[0]
        assert row.rec_start == row.aos and row.rec_end == row.los and (row.los - row.aos).total_seconds() == 600


def test_disabling_or_deleting_an_event_removes_its_future_passes(env):
    client, app = env
    start = datetime.now(UTC) + timedelta(hours=3)
    event = client.post("/api/timed-events", json=body(start_at=start.isoformat(), name="E")).json()
    assert [p for p in _passes(app) if p.event_id == event["id"]]
    client.put(f"/api/timed-events/{event['id']}", json=body(start_at=start.isoformat(), name="E", enabled=False))
    assert not [p for p in _passes(app) if p.event_id == event["id"]]
    client.put(f"/api/timed-events/{event['id']}", json=body(start_at=start.isoformat(), name="E"))
    assert [p for p in _passes(app) if p.event_id == event["id"]]
    client.delete(f"/api/timed-events/{event['id']}")
    assert not [p for p in _passes(app) if p.event_id == event["id"]]


def test_event_and_satellite_pass_compete_by_priority(env):
    client, app = env
    _rule(client, pipeline_id=1, priority=0)
    app.state.scheduler.recompute(now=T0)
    sat_pass = next(p for p in _passes(app) if p.event_id is None)
    # az esemény pontosan az átvonulás idejére esik
    secs = int((sat_pass.los - sat_pass.aos).total_seconds())
    low = client.post("/api/timed-events", json=body(start_at=sat_pass.aos.isoformat(), duration_s=secs, priority=-10, name="Gyenge")).json()
    app.state.scheduler.recompute(now=T0)
    rows = {(p.event_id is not None): p for p in _passes(app) if p.aos == sat_pass.aos}
    assert rows[False].status == "planned" and rows[True].status == "skipped_conflict"       # a magasabb prioritású (átvonulás) nyer

    client.put(f"/api/timed-events/{low['id']}", json=body(start_at=sat_pass.aos.isoformat(), duration_s=secs, priority=50, name="Erős"))
    app.state.scheduler.recompute(now=T0)
    rows = {(p.event_id is not None): p for p in _passes(app) if p.aos == sat_pass.aos}
    assert rows[True].status == "planned" and rows[False].status == "skipped_conflict"        # most az esemény nyer


def test_partial_overlap_is_allowed_for_events_only_when_requested(env):
    client, app = env
    start = T0 + timedelta(hours=2)
    first = client.post("/api/timed-events", json=body(start_at=start.isoformat(), duration_s=1200, priority=5, name="Első")).json()
    for allow, expected in ((False, "skipped_conflict"), (True, "partial")):
        second = client.post("/api/timed-events", json=body(start_at=(start + timedelta(minutes=10)).isoformat(), duration_s=1200,
                                                            name=f"Második {allow}", allow_partial=allow)).json()
        app.state.scheduler.recompute(now=T0)
        row = next(p for p in _passes(app) if p.event_id == second["id"])
        assert row.status == expected
        if allow:
            assert row.rec_start == start + timedelta(minutes=20) and row.rec_end == start + timedelta(minutes=30)
        client.delete(f"/api/timed-events/{second['id']}")
    assert first["id"]


def test_an_event_with_an_unsupported_frequency_is_marked_unsupported(env):
    client, app = env
    pid = client.post("/api/profiles", json={"name": "Túl magas", "center_freq_hz": 5_000_000_000, "bandwidth_hz": 20_000,
                                              "sample_rate_hz": 250_000, "doppler_correction": False}).json()["id"]
    client.post("/api/timed-events", json=body(start_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(), profile_id=pid))
    assert [p.status for p in _passes(app) if p.event_id] == ["unsupported"]


# ---- végrehajtás és konzol-események -------------------------------------------------------------------------------------

def test_a_timed_event_is_recorded_processed_and_logged(env):
    client, app = env
    fm_audio = next(p["id"] for p in client.get("/api/pipelines").json() if p["name"] == "FM audio")
    event = client.post("/api/timed-events", json=body(start_at=(T0 + timedelta(hours=1)).isoformat(), duration_s=8,
                                                       pipeline_id=fm_audio, name="Földi jeladó 145.8")).json()
    sched = app.state.scheduler
    sched.recompute(now=T0)
    row = next(p for p in _passes(app) if p.event_id == event["id"])
    assert sched.tick(now=row.rec_start - timedelta(minutes=5)) is None                 # még korai
    assert sched.tick(now=row.rec_start + timedelta(seconds=1)) == row.id
    _wait_idle(app)
    assert app.state.executor.wait_idle(60)

    assert next(p for p in _passes(app) if p.id == row.id).status == "done"
    rec = client.get("/api/recordings").json()[0]
    assert rec["norad_id"] is None and rec["label"] == "Földi jeladó 145.8" and rec["source"] == "mock"
    assert "Foldi-jelad" in rec["path"] or "F-ldi-jelad" in rec["path"] or "jelad" in rec["path"]            # a fájlnévben a címke
    base = app.state.settings.recordings_path / rec["path"]
    info = sigmf.read_info(base)
    assert info.num_samples == 7 * 250_000 and len(info.captures) == 1                    # nincs Doppler-követés: nincs újrahangolás
    assert info.extra["satrx:event"] == "Földi jeladó 145.8"

    import json
    doc = json.loads(sigmf.orbit_path(base).read_text())
    assert doc["satellite"] is None and doc["track"] is None and doc["radio"]["doppler_tracking"] is False
    assert doc["pass"]["event_id"] == event["id"] and doc["observer"]["lat"] == pytest.approx(47.4979)

    detail = client.get(f"/api/recordings/{rec['id']}").json()
    assert detail["recording"]["satellite_name"] == "Földi jeladó 145.8"                 # a felület a címkét mutatja nevként
    assert client.get("/api/storage/recordings").json()["items"][0]["satellite_name"] == "Földi jeladó 145.8"
    audio = [a for a in detail["artifacts"] if a["kind"] == "audio"]
    assert audio and audio[0]["satellite_name"] == "Földi jeladó 145.8"

    # a konzol: indulás és vég a felvétel nevével, a feldolgozás indulása és vége
    log = {e["code"]: e for e in reversed(client.get("/api/events", params={"limit": 50}).json())}
    started, finished = log["recording_started"], log["recording_finished"]
    assert started["details"]["subject"] == "Földi jeladó 145.8" and started["details"]["center_mhz"] == 145.8
    assert started["details"]["duration_s"] == 7 and started["level"] == "info"
    assert finished["details"]["duration_s"] == 7.0 and finished["details"]["size_mb"] == 3.5
    assert log["pipeline_started"]["details"]["pipeline"] == "FM audio"
    assert log["pipeline_done"]["details"] == {"pipeline": "FM audio", "artifacts": 1}
    assert log["service_started"]["details"]["backend"] == "mock"
    ids = [e["id"] for e in client.get("/api/events", params={"limit": 50}).json()]
    assert ids == sorted(ids, reverse=True)                                              # időrendben kapható vissza


def test_a_missed_event_is_logged_with_its_name(env):
    client, app = env
    event = client.post("/api/timed-events", json=body(start_at=(T0 + timedelta(hours=1)).isoformat(), name="Elmaradt")).json()
    app.state.scheduler.recompute(now=T0)
    row = next(p for p in _passes(app) if p.event_id == event["id"])
    app.state.scheduler.tick(now=row.rec_end + timedelta(minutes=1))
    assert next(p for p in _passes(app) if p.id == row.id).status == "missed"
    ev_row = next(e for e in client.get("/api/events").json() if e["code"] == "pass_missed")
    assert ev_row["level"] == "warning" and ev_row["details"]["label"] == "Elmaradt" and ev_row["details"]["event_id"] == event["id"]


def test_next_event_shows_in_the_status_snapshot_and_the_health_check(env):
    client, app = env
    client.post("/api/timed-events", json=body(start_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(), name="Következő jeladó"))
    with client.stream("GET", "/api/stream?duration_s=0.3") as r:
        import json
        line = next(l for l in r.iter_lines() if l.startswith("data:"))
    nxt = json.loads(line[5:])["next_pass"]
    assert nxt["satellite"] == "Következő jeladó" and nxt["kind"] == "event"
    sched = next(c for c in client.get("/api/health/detailed").json()["components"] if c["id"] == "scheduler")
    assert sched["details"]["next_pass"]["satellite"] == "Következő jeladó"


def test_manual_recording_bandwidth_is_stored_and_used_by_the_demodulator(env):
    client, app = env
    r = client.post("/api/recorder/start", json={"center_freq_hz": 446_006_250, "sample_rate_hz": 250_000, "duration_s": 2,
                                                 "bandwidth_hz": 12_500})
    assert r.status_code == 202, r.text
    _wait_idle(app)
    rec = client.get("/api/recordings").json()[0]
    assert sigmf.read_info(app.state.settings.recordings_path / rec["path"]).extra["satrx:bandwidth_hz"] == 12_500
    job = client.post(f"/api/recordings/{rec['id']}/run", json={"steps": [{"processor": "audio_demod", "params": {"mode": "nfm"}}]}).json()
    assert app.state.executor.wait_idle(60)
    art = client.get(f"/api/jobs/{job['job_id']}").json()["artifacts"][0]
    assert art["meta"]["bandwidth_hz"] == 12_500
