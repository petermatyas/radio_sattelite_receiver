"""Időalapú (NORAD ID nélküli) események: előfordulások, API, ütemezés, végrehajtás, konzol-események."""
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from satrx.api.app import create_app
from satrx.core.config import SdrConfig, Settings
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


def test_tle_refresh_reports_whether_anything_changed(env):
    client, _ = env
    same = client.post("/api/satellites/25544/refresh-tle").json()
    assert same["changed"] is False and same["previous_epoch"] == same["tle_epoch"]


def test_unscheduled_passes_are_listed_only_on_request_and_never_double_listed(env):
    client, app = env
    now = datetime.now(UTC)
    from tests.test_scheduler import ISS_TEXT  # noqa: F401
    plain = client.get("/api/passes", params={"hours": 168}).json()
    assert plain == []                                                        # szabály nélkül nincs ütemezett átvonulás
    extra = client.get("/api/passes", params={"hours": 168, "unscheduled": True}).json()
    assert extra and all(p["status"] == "unscheduled" and p["id"] < 0 and p["norad_id"] == 25544 for p in extra)
    assert [p["aos"] for p in extra] == sorted(p["aos"] for p in extra)
    # szabály felvétele: az ütemezett átvonulások nem szerepelnek még egyszer ütemezetlenként
    _rule(client, valid_from=now.isoformat(), valid_to=(now + timedelta(days=7)).isoformat())
    app.state.scheduler.recompute()
    both = client.get("/api/passes", params={"hours": 168, "unscheduled": True}).json()
    sched = [p for p in both if p["status"] != "unscheduled"]
    assert sched and not [u for u in both if u["status"] == "unscheduled"
                          and any(s["aos"] < u["los"] and u["aos"] < s["los"] for s in sched)]


def test_a_rule_without_profile_and_pipeline_uses_the_satellites_defaults(env):
    client, _ = env
    body = {"norad_id": 25544, "valid_from": T0.isoformat(), "valid_to": (T0 + timedelta(days=2)).isoformat()}
    r = client.post("/api/rules", json=body)
    assert r.status_code == 422 and "default" in r.text                       # nincs mit átvenni
    assert client.patch("/api/satellites/25544", json={"default_profile_id": 999}).status_code == 422
    sat = client.patch("/api/satellites/25544", json={"default_profile_id": 2, "default_pipeline_id": 1}).json()
    assert sat["default_profile_id"] == 2 and sat["default_pipeline_id"] == 1 and sat["enabled"] is True
    rule = client.post("/api/rules", json=body).json()
    assert rule["profile_id"] == 2 and rule["pipeline_id"] == 1
    # a kifejezetten megadott érték erősebb, az enabled átállítása nem törli az alapértékeket
    assert client.post("/api/rules", json={**body, "profile_id": 1}).json()["profile_id"] == 1
    assert client.patch("/api/satellites/25544", json={"enabled": False}).json()["default_profile_id"] == 2


def test_satellite_channels_manage_the_profile_and_pipeline(env):
    client, _ = env
    opts = client.get("/api/channels/options").json()
    assert "Robot36" in opts["sstv_modes"] and set(opts["modes"]) == {"fm", "sstv", "lrpt", "record"}
    r = client.post("/api/satellites/25544/channels", json={"center_freq_hz": 145_800_000, "mode": "sstv",
                                                            "settings": {"sstv_mode": "Robot36", "min_lines": 20}, "gain_db": 30})
    assert r.status_code == 201, r.text
    ch = r.json()
    assert ch["name"].startswith("ISS") and ch["bandwidth_hz"] == 25_000 and ch["sample_rate_hz"] == 250_000 and ch["doppler"] is True
    profile = next(p for p in client.get("/api/profiles").json() if p["id"] == ch["profile_id"])
    pipeline = next(p for p in client.get("/api/pipelines").json() if p["id"] == ch["pipeline_id"])
    assert profile["center_freq_hz"] == 145_800_000 and profile["gain_db"] == 30 and pipeline["mode"] == "sstv"
    assert [s["processor"] for s in pipeline["steps"]] == ["fm_demod", "sstv"]
    assert pipeline["steps"][1]["params"] == {"mode": "Robot36", "min_lines": 20}
    assert pipeline["steps"][0]["params"]["bandwidth_hz"] == 25_000
    # a pipeline lépései érvényesek a processzorok sémája szerint (a futtató ugyanígy ellenőrzi)
    assert client.post("/api/rules", json={"norad_id": 25544, "valid_from": T0.isoformat(), "valid_to": (T0 + timedelta(days=2)).isoformat(),
                                           "channel_id": ch["id"]}).json()["profile_id"] == ch["profile_id"]
    # módosítás: a profil és a pipeline helyben frissül (a szabály követi)
    upd = client.put(f"/api/channels/{ch['id']}", json={"center_freq_hz": 145_990_000, "mode": "fm", "name": "ISS hang",
                                                        "settings": {"squelch_db": -40, "waterfall": True}}).json()
    assert upd["profile_id"] == ch["profile_id"] and upd["name"] == "ISS hang"
    pipeline = next(p for p in client.get("/api/pipelines").json() if p["id"] == ch["pipeline_id"])
    assert [s["processor"] for s in pipeline["steps"]] == ["fm_demod", "waterfall"] and pipeline["steps"][0]["params"]["squelch_db"] == -40
    # használatban lévő csatorna nem törölhető, a szabály nélküli igen
    assert client.delete(f"/api/channels/{ch['id']}").status_code == 409
    other = client.post("/api/satellites/25544/channels", json={"center_freq_hz": 137_100_000, "mode": "lrpt"}).json()
    assert other["bandwidth_hz"] == 150_000 and other["keep_iq"] is False
    n_profiles = len(client.get("/api/profiles").json())
    assert client.delete(f"/api/channels/{other['id']}").status_code == 204
    assert len(client.get("/api/profiles").json()) == n_profiles - 1


def test_channel_validation_and_rule_fallback_to_the_first_channel(env):
    client, _ = env
    bad = [{"center_freq_hz": 145_800_000, "mode": "sstv", "settings": {"sstv_mode": "nope"}},
           {"center_freq_hz": 145_800_000, "mode": "lrpt", "settings": {"lrpt_pipeline": "x"}},
           {"center_freq_hz": 145_800_000, "settings": {"unknown": 1}},
           {"center_freq_hz": 145_800_000, "bandwidth_hz": 900_000},
           {"center_freq_hz": 145_800_000, "mode": "ssb"}]
    for body in bad:
        assert client.post("/api/satellites/25544/channels", json=body).status_code == 422, body
    assert client.post("/api/satellites/1/channels", json={"center_freq_hz": 1}).status_code == 404
    first = client.post("/api/satellites/25544/channels", json={"center_freq_hz": 145_800_000, "mode": "fm"}).json()
    client.post("/api/satellites/25544/channels", json={"center_freq_hz": 145_990_000, "mode": "record"})
    rule = client.post("/api/rules", json={"norad_id": 25544, "valid_from": T0.isoformat(),
                                           "valid_to": (T0 + timedelta(days=2)).isoformat()}).json()
    assert rule["profile_id"] == first["profile_id"] and rule["pipeline_id"] == first["pipeline_id"]
    assert client.post("/api/rules", json={"norad_id": 25544, "valid_from": T0.isoformat(), "valid_to": (T0 + timedelta(days=2)).isoformat(),
                                           "channel_id": 99999}).status_code == 422
    # a műhold törlése a karbantartott profilokat és pipeline-okat is törli
    before = len(client.get("/api/profiles").json())
    assert client.delete("/api/satellites/25544").status_code == 204
    assert len(client.get("/api/profiles").json()) == before - 2 and client.get("/api/channels").json() == []


def test_refresh_all_tles_continues_after_a_failure(env):
    client, app = env
    from satrx.tle.client import TleError
    from satrx.core.models import Satellite
    with app.state.session_factory() as session:                 # a tesztadat-forrás csak az ISS-t ismeri: a második műholdat közvetlenül vesszük fel
        session.add(Satellite(norad_id=57166, name="TESZT"))
        session.commit()
    real = app.state.tle_fetcher

    def flaky(norad_id):
        if norad_id == 57166:
            raise TleError("boom")
        return real(norad_id)

    app.state.tle_fetcher = flaky
    out = client.post("/api/satellites/refresh-tle").json()
    assert out["total"] == 2 and out["failed"] == 1 and out["updated"] == 0
    assert {r["norad_id"]: r["error"] for r in out["results"]} == {25544: None, 57166: "boom"}


def test_celestrak_404_gives_a_clear_message():
    import httpx
    from satrx.tle.client import TleError, fetch_tle
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(404, text="No GP data found")))
    with pytest.raises(TleError, match="no current orbital data for NORAD 40931"):
        fetch_tle(40931, client)


def test_one_predicted_pass_can_be_scheduled_with_a_chosen_channel(env):
    client, app = env
    ch1 = client.post("/api/satellites/25544/channels", json={"center_freq_hz": 145_800_000, "mode": "fm"}).json()
    ch2 = client.post("/api/satellites/25544/channels", json={"center_freq_hz": 145_800_000, "mode": "sstv",
                                                              "settings": {"sstv_mode": "Robot36"}}).json()
    passes = client.get("/api/passes", params={"hours": 72, "unscheduled": True}).json()
    assert len(passes) >= 3
    target = passes[1]
    r = client.post("/api/passes/schedule", json={"norad_id": 25544, "aos": target["aos"], "los": target["los"], "channel_id": ch2["id"]})
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "planned"
    after = client.get("/api/passes", params={"hours": 72, "unscheduled": True}).json()
    planned = [p for p in after if p["status"] != "unscheduled"]
    iso = lambda x: datetime.fromisoformat(x.replace("Z", "+00:00"))  # noqa: E731
    assert len(planned) == 1 and abs((iso(planned[0]["aos"]) - iso(target["aos"])).total_seconds()) < 5   # csak ez az egy átvonulás lett ütemezve
    assert len([p for p in after if p["status"] == "unscheduled"]) == len(passes) - 1  # a többi továbbra is ütemezetlen
    rule = next(x for x in client.get("/api/rules").json() if x["id"] == r.json()["rule_id"])
    assert rule["profile_id"] == ch2["profile_id"] and rule["pipeline_id"] == ch2["pipeline_id"] and rule["profile_id"] != ch1["profile_id"]
    # érvénytelen kérések
    assert client.post("/api/passes/schedule", json={"norad_id": 25544, "aos": target["los"], "los": target["aos"]}).status_code == 422
    assert client.post("/api/passes/schedule", json={"norad_id": 25544, "aos": target["aos"], "los": target["los"], "channel_id": 9999}).status_code == 422
    # olyan időablak, ahol nincs átvonulás: nem marad árva szabály
    n_rules = len(client.get("/api/rules").json())
    gap = passes[0]
    from datetime import datetime as dt
    mid = dt.fromisoformat(gap["los"].replace("Z", "+00:00")) + timedelta(minutes=20)
    assert client.post("/api/passes/schedule", json={"norad_id": 25544, "aos": mid.isoformat(), "los": (mid + timedelta(minutes=5)).isoformat()}).status_code == 409
    assert len(client.get("/api/rules").json()) == n_rules


def test_review_fixes_managed_profiles_and_stable_ids(env):
    client, _ = env
    ch = client.post("/api/satellites/25544/channels", json={"center_freq_hz": 145_800_000, "mode": "fm"}).json()
    # a karbantartott profil / pipeline nem törölhető a Profilok / Pipeline-ok oldalról
    assert client.delete(f"/api/profiles/{ch['profile_id']}").status_code == 409
    assert client.delete(f"/api/pipelines/{ch['pipeline_id']}").status_code == 409
    # időpontos esemény használja a csatorna profilját: a műhold törlése 409, nem 500
    ev = client.post("/api/timed-events", json={"name": "x", "start_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(), "duration_s": 60,
                                                "profile_id": ch["profile_id"], "pipeline_id": ch["pipeline_id"]})
    assert ev.status_code == 201
    assert client.delete("/api/satellites/25544").status_code == 409
    client.delete(f"/api/timed-events/{ev.json()['id']}")
    # az ütemezetlen átvonulások azonosítója nem függ a lista hosszától
    long = {p["aos"][:16]: p["id"] for p in client.get("/api/passes", params={"hours": 72, "unscheduled": True}).json()}
    short = {p["aos"][:16]: p["id"] for p in client.get("/api/passes", params={"hours": 24, "unscheduled": True}).json()}
    assert short and all(long.get(k) == v for k, v in short.items()) and len(set(long.values())) == len(long)


def test_tool_paths_must_have_the_expected_program_name(tmp_path):
    import shutil
    other = tmp_path / "notepad.exe"
    shutil.copy(sys.executable, other)
    s = Settings(data_dir=tmp_path / "d")
    with TestClient(create_app(s, tle_fetcher=_fetch, startup_refresh=False, scheduler_enabled=False)) as client:
        r = client.put("/api/settings/tools", json={"rtl_fm_path": str(other)})
        assert r.status_code == 422 and "rtl_fm" in r.text


def test_rtl_sdr_backend_does_not_retune_during_a_pass():
    from satrx.scheduler.service import Scheduler
    s = Settings()
    s.sdr = SdrConfig(backend="rtl_sdr")
    assert Scheduler._retune_step(SimpleNamespace(_settings=s)) == 10**9
    s.sdr = SdrConfig(backend="rtlsdr")
    assert Scheduler._retune_step(SimpleNamespace(_settings=s)) == s.scheduling.retune_step_hz
