"""9. fázis: adatkezelés és tárhely (gyökerek, törlés, megőrzés, áthelyezés, letöltés, export)."""
import hashlib
import io
import json
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from satrx.api.app import create_app
from satrx.core.config import PROJECT_ROOT, SdrConfig
from satrx.core.models import Artifact, Job, Recording
from satrx.recording import sigmf
from satrx.storage import manager as manager_mod
from satrx.storage import move as move_mod
from satrx.storage.export import ExportManager, ExportSpec
from satrx.storage.move import Cancelled, copy_verified
from tests.test_scheduler import _fetch, _settings

F0 = 145_800_000
NOW = datetime.now(timezone.utc)


@pytest.fixture
def ctx(tmp_path):
    s = _settings(tmp_path / "data")
    app = create_app(s, tle_fetcher=_fetch, startup_refresh=False, scheduler_enabled=False)
    with TestClient(app) as client:
        client.post("/api/satellites", json={"norad_id": 25544})
        yield client, app, tmp_path


def make_rec(app, name, size=10_000, processed=True, age_days=0.0, norad=25544, artifacts=1, art_size=2_000,
             protected=False, root=None):
    """Egy felvétel az adatbázisban és a lemezen (gyorsan, a pipeline futtatása nélkül)."""
    sm = app.state.storage
    root_info = sm.root("recordings", root) if root else sm.active("recordings")
    art_root = sm.active("artifacts")
    base = root_info.path / name
    with sigmf.SigMFWriter(base, "cu8", 250_000, F0) as w:
        w.write(np.zeros(size // 2, dtype=np.complex64))
    sigmf.orbit_path(base).write_text(json.dumps({"orbit_schema": 1, "track": None}))
    started = NOW - timedelta(days=age_days)
    with app.state.session_factory() as s:
        rec = Recording(path=name, sample_format="cu8", center_freq_hz=F0, sample_rate_hz=250_000, started_at=started,
                        ended_at=started + timedelta(seconds=10), size_bytes=size, norad_id=norad, root_id=root_info.id,
                        protected=protected, orbit_meta_path=f"{name}.orbit.json")
        s.add(rec)
        s.flush()
        job = Job(kind="processing", status="done" if processed else "failed", recording_id=rec.id)
        s.add(job)
        s.flush()
        for i in range(artifacts):
            rel = f"rec{rec.id}/job{job.id}/r{i}.png"
            p = art_root.path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * art_size)
            s.add(Artifact(job_id=job.id, recording_id=rec.id, norad_id=norad, kind="image", path=rel, meta={},
                           root_id=art_root.id, size_bytes=art_size))
        s.commit()
        return rec.id


def rec_row(app, rid):
    with app.state.session_factory() as s:
        r = s.get(Recording, rid)
        s.expunge(r)
        return r


def n_artifacts(app, rid=None):
    with app.state.session_factory() as s:
        q = s.query(Artifact)
        return (q.filter(Artifact.recording_id == rid) if rid else q).count()


def wait_move(app, timeout=30):
    app.state.mover.join(timeout)
    return app.state.mover.status()


# ---- gyökerek és ellenőrzés ------------------------------------------------------------------------------

def test_default_roots_exist_with_markers_and_are_reported(ctx):
    client, app, _ = ctx
    info = client.get("/api/storage").json()
    kinds = {r["kind"]: r for r in info["roots"]}
    assert set(kinds) == {"recordings", "artifacts"}
    assert all(r["active"] and r["available"] for r in kinds.values())
    assert (Path(kinds["recordings"]["path"]) / ".satrx-root").is_file()
    assert info["recordings_bytes"] == 0 and info["quota_bytes"] == 50 * 10**9 and info["move"]["state"] == "idle"
    # a már létező (gyökér nélküli, régi) sorok az alapgyökérhez tartoznak
    rid = make_rec(app, "legacy")
    with app.state.session_factory() as s:
        s.get(Recording, rid).root_id = None
        s.commit()
    assert client.get(f"/api/recordings/{rid}").json()["iq_available"] is True


def test_validate_path_rules(ctx, monkeypatch):
    client, app, tmp = ctx
    sm = app.state.storage
    v = lambda p: sm.validate_path(p)  # noqa: E731
    assert v("")["errors"] == ["path_empty"]
    assert v("relative/dir")["errors"] == ["path_not_absolute"]
    assert "path_is_filesystem_root" in v(str(Path(tmp.anchor)))["errors"]
    assert "path_is_home" in v(str(Path.home()))["errors"]
    assert "path_inside_application" in v(str(PROJECT_ROOT / "src" / "x"))["errors"]
    assert "path_inside_application" in v(str(PROJECT_ROOT / "web"))["errors"]
    f = tmp / "a_file"
    f.write_text("x")
    assert v(str(f))["errors"] == ["path_not_a_directory"]

    new = v(str(tmp / "brand" / "new"))
    assert new["ok"] and "path_will_be_created" in new["warnings"] and not new["exists"] and new["free_bytes"] > 0

    used = tmp / "used"
    used.mkdir()
    (used / "something.txt").write_text("x")
    assert "path_not_empty" in v(str(used))["warnings"] and v(str(used))["non_empty"] is True

    default = app.state.storage.default("recordings")
    again = v(str(default.path))
    assert again["ok"] and again["existing_root_id"] == default.id and "path_not_empty" not in again["warnings"]

    monkeypatch.setattr(manager_mod.shutil, "disk_usage", lambda p: type("U", (), {"free": 10 * 1024**2})())
    assert "path_disk_full" in v(str(tmp / "x"))["errors"]
    monkeypatch.setattr(manager_mod.shutil, "disk_usage", lambda p: type("U", (), {"free": 2 * 1024**3})())
    assert "path_low_space" in v(str(tmp / "x"))["warnings"]
    monkeypatch.undo()

    orig = Path.write_bytes
    monkeypatch.setattr(Path, "write_bytes", lambda self, data: (_ for _ in ()).throw(OSError("read-only"))
                        if ".satrx-write-test" in self.name else orig(self, data))
    assert "path_not_writable" in v(str(used))["errors"]


def test_validate_endpoint(ctx):
    client, _, tmp = ctx
    assert client.post("/api/storage/validate", json={"path": str(tmp / "ok")}).json()["ok"] is True
    assert client.post("/api/storage/validate", json={"path": "nope"}).json()["errors"] == ["path_not_absolute"]


def test_new_recordings_go_to_the_new_root_and_old_ones_stay_reachable(ctx):
    client, app, tmp = ctx
    old = make_rec(app, "old_one")
    new_dir = tmp / "usb_rec"
    r = client.put("/api/storage/roots", json={"recordings_path": str(new_dir)})
    assert r.status_code == 200, r.text
    assert (new_dir / ".satrx-root").is_file()
    active = next(x for x in r.json()["roots"] if x["kind"] == "recordings" and x["active"])
    assert Path(active["path"]) == new_dir

    assert client.post("/api/recorder/start", json={"center_freq_hz": F0, "sample_rate_hz": 250_000, "duration_s": 1}).status_code == 202
    for _ in range(100):
        if app.state.recorder.status().state == "idle":
            break
        time.sleep(0.05)
    new_rec = max(client.get("/api/recordings").json(), key=lambda x: x["id"])
    assert (new_dir / f"{new_rec['path']}.sigmf-data").is_file()
    assert client.get(f"/api/recordings/{new_rec['id']}").json()["iq_available"] is True
    detail = client.get(f"/api/recordings/{old}").json()                     # a régi felvétel a régi helyén érhető el
    assert detail["iq_available"] is True and detail["info"]["sample_rate_hz"] == 250_000
    assert client.get(f"/api/recordings/{old}/download?part=meta").status_code == 200
    roots = client.get("/api/storage").json()["roots"]
    assert len([r for r in roots if r["kind"] == "recordings"]) == 2


def test_changing_the_location_is_rejected_for_invalid_paths_and_while_busy(tmp_path):
    s = _settings(tmp_path / "data")
    s.sdr = SdrConfig(backend="mock", device_args={"realtime": True})
    with TestClient(create_app(s, tle_fetcher=_fetch, startup_refresh=False, scheduler_enabled=False)) as client:
        bad = client.put("/api/storage/roots", json={"recordings_path": "relative"})
        assert bad.status_code == 422 and bad.json()["detail"]["checks"]["recordings"]["errors"] == ["path_not_absolute"]
        assert client.put("/api/storage/roots", json={}).status_code == 422

        client.post("/api/recorder/start", json={"center_freq_hz": F0, "sample_rate_hz": 250_000, "duration_s": 30})
        locked = client.put("/api/storage/roots", json={"recordings_path": str(tmp_path / "other")})
        assert locked.status_code == 409 and "recording_in_progress" in locked.json()["detail"]["reasons"]
        assert client.post("/api/storage/move", json={"kind": "recordings"}).status_code == 409
        client.post("/api/recorder/stop")


def test_an_unavailable_drive_is_reported_and_blocks_new_recordings(ctx):
    client, app, tmp = ctx
    rid = make_rec(app, "on_usb")
    art_id = client.get("/api/artifacts").json()[0]["id"]
    root = app.state.storage.active("recordings")
    (root.path / ".satrx-root").unlink()                  # úgy tűnik, mintha a meghajtó nem lenne csatolva

    health = next(c for c in client.get("/api/health/detailed").json()["components"] if c["id"] == "storage")
    assert health["status"] == "error" and health["code"] == "storage_unavailable"
    assert str(root.path) in health["details"]["unavailable"]

    r = client.post("/api/recorder/start", json={"center_freq_hz": F0, "sample_rate_hz": 250_000, "duration_s": 1})
    assert r.status_code == 422 and "not available" in r.json()["detail"]
    detail = client.get(f"/api/recordings/{rid}").json()
    assert detail["storage_available"] is False and detail["iq_available"] is False
    assert client.get(f"/api/recordings/{rid}/download").status_code == 503
    assert client.get(f"/api/storage/recordings").json()["items"][0]["storage_available"] is False
    plan = client.post("/api/storage/delete", json={"recording_ids": [rid], "scope": "all", "dry_run": True}).json()
    assert plan["items"][0]["skipped"] == "storage_unavailable" and plan["freed_bytes"] == 0
    assert client.get(f"/api/artifacts/{art_id}/file").status_code == 200       # az eredmények gyökere külön áll

    import shutil
    shutil.rmtree(root.path)                                 # a mappa is eltűnt
    assert client.get(f"/api/recordings/{rid}").json()["storage_available"] is False


# ---- törlés -----------------------------------------------------------------------------------------------

def test_delete_dry_run_matches_the_real_outcome(ctx):
    client, app, _ = ctx
    a = make_rec(app, "a", size=40_000, artifacts=2, art_size=3_000)
    b = make_rec(app, "b", size=20_000, artifacts=1, art_size=1_000)
    sm = app.state.storage

    plan = client.post("/api/storage/delete", json={"recording_ids": [a, b], "scope": "iq_only", "dry_run": True}).json()
    assert plan["dry_run"] is True and plan["freed_bytes"] == 60_000 and plan["recordings"] == 2 and plan["artifacts"] == 0
    assert sigmf.data_path(sm.recording_base(rec_row(app, a))).exists()            # semmi nem törlődött

    done = client.post("/api/storage/delete", json={"recording_ids": [a], "scope": "iq_only"}).json()
    assert done["result"]["freed_bytes"] == 40_000 and done["result"]["recordings"] == 1
    assert not sigmf.data_path(sm.recording_base(rec_row(app, a))).exists()
    assert sigmf.meta_path(sm.recording_base(rec_row(app, a))).exists()            # a metaadat és a pályaadat megmarad
    assert sigmf.orbit_path(sm.recording_base(rec_row(app, a))).exists()
    assert rec_row(app, a).deleted is True and n_artifacts(app, a) == 2             # az eredmények megmaradnak
    detail = client.get(f"/api/recordings/{a}").json()
    assert detail["iq_available"] is False and detail["orbit"] is not None and len(detail["artifacts"]) == 2

    again = client.post("/api/storage/delete", json={"recording_ids": [a], "scope": "iq_only", "dry_run": True}).json()
    assert again["items"][0]["skipped"] == "no_iq" and again["freed_bytes"] == 0

    plan_all = client.post("/api/storage/delete", json={"recording_ids": [b], "scope": "all", "dry_run": True}).json()
    base_b = sm.recording_base(rec_row(app, b))
    meta_orbit = sigmf.meta_path(base_b).stat().st_size + sigmf.orbit_path(base_b).stat().st_size
    assert plan_all["freed_bytes"] == 20_000 + 1_000 + meta_orbit
    out = client.post("/api/storage/delete", json={"recording_ids": [b], "scope": "all"}).json()
    assert out["result"]["recordings"] == 1
    assert not list(sm.active("recordings").path.glob("b.*")) and n_artifacts(app, b) == 0
    assert not (sm.active("artifacts").path / f"rec{b}").exists()
    assert client.get(f"/api/recordings/{b}").status_code == 404


def test_protected_and_busy_and_missing_items_are_skipped(tmp_path):
    s = _settings(tmp_path / "data")
    s.sdr = SdrConfig(backend="mock", device_args={"realtime": True})
    with TestClient(create_app(s, tle_fetcher=_fetch, startup_refresh=False, scheduler_enabled=False)) as client:
        app = client.app
        client.post("/api/satellites", json={"norad_id": 25544})
        prot = make_rec(app, "prot", protected=True)
        plain = make_rec(app, "plain")
        r = client.post("/api/recorder/start", json={"center_freq_hz": F0, "sample_rate_hz": 250_000, "duration_s": 30})
        busy_id = r.json()["id"]

        plan = client.post("/api/storage/delete", json={"recording_ids": [prot, plain, busy_id, 9999], "scope": "all",
                                                        "dry_run": True}).json()
        reasons = {i["id"]: i["skipped"] for i in plan["items"]}
        assert reasons == {prot: "protected", plain: None, busy_id: "busy", 9999: "not_found"}
        assert plan["recordings"] == 1 and plan["skipped"] == 3
        assert client.delete(f"/api/recordings/{busy_id}").status_code == 409

        forced = client.post("/api/storage/delete", json={"recording_ids": [prot], "scope": "iq_only", "dry_run": True,
                                                          "include_protected": True}).json()
        assert forced["items"][0]["skipped"] is None
        client.post("/api/recorder/stop")


def test_artifact_only_deletion_and_no_double_counting(ctx):
    client, app, _ = ctx
    rid = make_rec(app, "x", artifacts=3, art_size=1_000)
    arts = [a["id"] for a in client.get("/api/artifacts", params={"recording_id": rid}).json()]
    one = client.post("/api/storage/delete", json={"artifact_ids": [arts[0]], "dry_run": True}).json()
    assert one["freed_bytes"] == 1_000 and one["artifacts"] == 1
    both = client.post("/api/storage/delete", json={"recording_ids": [rid], "artifact_ids": arts, "scope": "all",
                                                    "dry_run": True}).json()
    assert both["artifacts"] == 0 and both["recordings"] == 1                      # az eredmények a felvétellel együtt mennek
    assert client.post("/api/storage/delete", json={"artifact_ids": [arts[0]]}).json()["result"]["artifacts"] == 1
    assert n_artifacts(app, rid) == 2
    assert client.post("/api/storage/delete", json={}).status_code == 422
    assert client.post("/api/storage/delete", json={"recording_ids": [rid], "scope": "weird"}).status_code == 422


def test_deleting_writes_an_event_and_cleans_empty_job_directories(ctx):
    client, app, _ = ctx
    rid = make_rec(app, "e", artifacts=1)
    art = client.get("/api/artifacts").json()[0]["id"]
    root = app.state.storage.active("artifacts").path
    client.post("/api/storage/delete", json={"artifact_ids": [art]})
    assert not (root / f"rec{rid}").exists()                                        # üres rec/job mappák takarítva
    ev = next(e for e in client.get("/api/events").json() if e["code"] == "data_deleted")
    assert ev["level"] == "info" and ev["details"]["artifacts"] == 1 and ev["details"]["freed_bytes"] == 2_000


def test_protect_flag_and_listing_filters_and_sorting(ctx):
    client, app, _ = ctx
    from satrx.core.models import Satellite
    with app.state.session_factory() as sess:
        sess.add(Satellite(norad_id=57166, name="METEOR-M2 3"))
        sess.commit()
    old = make_rec(app, "old", size=5_000, age_days=10, processed=True, artifacts=1)
    big = make_rec(app, "big", size=90_000, age_days=2, processed=False, artifacts=0)
    mid = make_rec(app, "mid", size=30_000, age_days=5, processed=True, artifacts=2, art_size=5_000, norad=57166)
    assert client.patch(f"/api/recordings/{old}", json={"protected": True}).json() == {"id": old, "protected": True}
    assert client.patch("/api/recordings/9999", json={"protected": True}).status_code == 404

    def ids(**params):
        return [i["id"] for i in client.get("/api/storage/recordings", params=params).json()["items"]]

    assert ids() == [big, mid, old]                                                 # alap: legújabb elöl
    assert ids(sort="size") == [big, mid, old] and ids(sort="size", order="asc") == [old, mid, big]
    assert ids(sort="results") == [mid, old, big]
    assert ids(state="protected") == [old] and ids(state="unprocessed") == [big]
    assert ids(norad_id=57166) == [mid]
    assert ids(since=(NOW - timedelta(days=6)).isoformat()) == [big, mid]
    assert ids(limit=1, offset=1) == [mid]
    listing = client.get("/api/storage/recordings").json()
    assert listing["total"] == 3 and listing["iq_bytes"] == 125_000 and listing["artifact_bytes"] == 2_000 + 10_000
    first = next(i for i in listing["items"] if i["id"] == mid)
    assert first["artifact_count"] == 2 and first["processed"] is True and first["total_bytes"] == 40_000
    client.post("/api/storage/delete", json={"recording_ids": [big]})
    assert ids(state="without_iq") == [big] and ids(state="with_iq") == [mid, old]


# ---- megőrzés ------------------------------------------------------------------------------------------------

def test_age_retention_only_removes_processed_unprotected_idle_iq(ctx):
    client, app, _ = ctx
    app.state.settings.storage.iq_retention_days = 7
    old_done = make_rec(app, "old_done", age_days=9)
    old_unprocessed = make_rec(app, "old_unproc", age_days=9, processed=False)
    old_protected = make_rec(app, "old_prot", age_days=9, protected=True)
    recent = make_rec(app, "recent", age_days=2)
    plan = client.post("/api/storage/retention", json={}).json()                     # alapból száraz futtatás
    assert plan["dry_run"] is True and plan["recording_ids"] == [old_done] and plan["freed_bytes"] == 10_000
    assert rec_row(app, old_done).deleted is False

    real = client.post("/api/storage/retention", json={"dry_run": False}).json()
    assert real["aged_out"] == 1
    assert [rec_row(app, i).deleted for i in (old_done, old_unprocessed, old_protected, recent)] == [True, False, False, False]
    assert n_artifacts(app, old_done) == 1                                          # az eredmény megmarad
    ev = next(e for e in client.get("/api/events").json() if e["code"] == "retention_deleted")
    assert ev["details"]["aged_out"] == 1

    app.state.settings.storage.iq_retention_days = 0                                  # 0 = nincs korhatár
    assert client.post("/api/storage/retention", json={}).json()["recording_ids"] == []


def test_quota_retention_deletes_oldest_processed_first_then_unprocessed_as_a_last_resort(ctx):
    client, app, _ = ctx
    app.state.settings.storage.iq_retention_days = 0
    app.state.settings.storage.quota_gb = 0.00010                                    # 100 000 bájt
    p_old = make_rec(app, "p_old", size=30_000, age_days=8, artifacts=0)
    p_new = make_rec(app, "p_new", size=30_000, age_days=3, artifacts=0)
    unproc = make_rec(app, "unproc", size=30_000, age_days=9, processed=False, artifacts=0)
    prot = make_rec(app, "prot", size=30_000, age_days=10, protected=True, artifacts=0)    # 120 000 összesen

    plan = client.post("/api/storage/retention", json={"dry_run": True}).json()
    # a célszint a kvóta 90%-a (90 000): egy törlés (120 000 -> 90 000) elég, a feldolgozatlanhoz nem nyúl
    assert plan["recording_ids"] == [p_old] and plan["unprocessed_deleted"] == 0 and plan["freed_bytes"] == 30_000

    client.post("/api/storage/retention", json={"dry_run": False})
    assert [rec_row(app, i).deleted for i in (p_old, p_new, unproc, prot)] == [True, False, False, False]

    app.state.settings.storage.quota_gb = 0.00002                                    # most már a feldolgozott sem elég (20 000)
    last = client.post("/api/storage/retention", json={"dry_run": False}).json()
    assert last["over_quota"] == 1 and last["unprocessed_deleted"] == 1              # p_new, aztán a feldolgozatlan
    assert last["notes"] == ["quota_still_exceeded"]                                 # a védett marad: a kvóta még túllépve
    assert [rec_row(app, i).deleted for i in (p_new, unproc, prot)] == [True, True, False]
    events = {e["code"]: e for e in client.get("/api/events").json()}
    assert events["retention_unprocessed_deleted"]["level"] == "warning"


def test_retention_is_wired_into_the_scheduler_loop(ctx):
    client, app, _ = ctx
    app.state.settings.storage.iq_retention_days = 1
    rid = make_rec(app, "aged", age_days=3)
    assert app.state.scheduler.retention is not None
    app.state.scheduler.retention()
    assert rec_row(app, rid).deleted is True


def test_usage_includes_results_and_the_health_check_uses_it(ctx):
    client, app, _ = ctx
    make_rec(app, "u1", size=60_000, artifacts=2, art_size=20_000)                   # 60k IQ + 40k eredmény
    info = client.get("/api/storage").json()
    assert info["recordings_bytes"] == 60_000 and info["artifacts_bytes"] == 40_000 and info["total_bytes"] == 100_000
    assert info["recordings_count"] == 1 and info["artifacts_count"] == 2 and info["growth_7d_bytes"] == 100_000
    assert info["days_until_disk_full"] > 0 and info["quota_used_fraction"] < 0.001
    assert {r["kind"]: r["used_bytes"] for r in info["roots"]} == {"recordings": 60_000, "artifacts": 40_000}

    app.state.settings.storage.quota_gb = 0.00008                                    # 80 000: csak az eredményekkel együtt lépi túl
    storage = next(c for c in client.get("/api/health/detailed").json()["components"] if c["id"] == "storage")
    assert storage["code"] == "quota_exceeded" and storage["details"]["artifacts_gb"] == 0.0


# ---- áthelyezés ------------------------------------------------------------------------------------------------

def _two_recordings_and_a_new_root(client, app, tmp):
    a = make_rec(app, "mv_a", size=30_000, artifacts=1)
    b = make_rec(app, "mv_b", size=20_000, artifacts=2)
    new_dir = tmp / "ssd_rec"
    assert client.put("/api/storage/roots", json={"recordings_path": str(new_dir)}).status_code == 200
    return a, b, new_dir


def test_move_copies_verifies_switches_the_root_and_removes_the_source(ctx):
    client, app, tmp = ctx
    a, b, new_dir = _two_recordings_and_a_new_root(client, app, tmp)
    old_dir = app.state.storage.default("recordings").path
    before = hashlib.sha256((old_dir / "mv_a.sigmf-data").read_bytes()).hexdigest()

    r = client.post("/api/storage/move", json={"kind": "recordings"})
    assert r.status_code == 202 and r.json()["state"] == "running"
    st = wait_move(app)
    assert st["state"] == "done" and st["done_items"] == 2 and st["total_items"] == 2 and st["errors"] == []
    assert st["total_bytes"] > 50_000 and st["done_bytes"] == st["total_bytes"]

    active = app.state.storage.active("recordings")
    for rid, name in ((a, "mv_a"), (b, "mv_b")):
        assert rec_row(app, rid).root_id == active.id
        assert (new_dir / f"{name}.sigmf-data").is_file() and (new_dir / f"{name}.orbit.json").is_file()
        assert not (old_dir / f"{name}.sigmf-data").exists() and not (old_dir / f"{name}.sigmf-meta").exists()
    assert hashlib.sha256((new_dir / "mv_a.sigmf-data").read_bytes()).hexdigest() == before
    assert not list(new_dir.glob("*.part"))
    assert client.get(f"/api/recordings/{a}").json()["iq_available"] is True
    assert client.get(f"/api/recordings/{a}/download").content == (new_dir / "mv_a.sigmf-data").read_bytes()
    ev = next(e for e in client.get("/api/events").json() if e["code"] == "move_finished")
    assert ev["level"] == "info" and ev["details"]["moved"] == 2

    again = client.post("/api/storage/move", json={"kind": "recordings"}).json()      # nincs mit áthelyezni
    assert wait_move(app)["total_items"] == 0 and again["state"] in ("running", "done")


def test_artifacts_move_with_thumbnails_and_empty_directories_are_removed(ctx):
    client, app, tmp = ctx
    rid = make_rec(app, "art", artifacts=0)
    sm = app.state.storage
    old_root = sm.active("artifacts").path
    with app.state.session_factory() as s:
        job = s.query(Job).first()
        rel, thumb = f"rec{rid}/job{job.id}/p.png", f"rec{rid}/job{job.id}/p.thumb.jpg"
        (old_root / rel).parent.mkdir(parents=True, exist_ok=True)
        (old_root / rel).write_bytes(b"P" * 5_000)
        (old_root / thumb).write_bytes(b"T" * 700)
        s.add(Artifact(job_id=job.id, recording_id=rid, kind="image", path=rel, thumbnail_path=thumb, meta={},
                       root_id=sm.active("artifacts").id, size_bytes=5_700))
        s.commit()
    new_dir = tmp / "ssd_art"
    assert client.put("/api/storage/roots", json={"artifacts_path": str(new_dir)}).status_code == 200
    assert client.post("/api/storage/move", json={"kind": "artifacts"}).status_code == 202
    assert wait_move(app)["state"] == "done"
    assert (new_dir / rel).read_bytes() == b"P" * 5_000 and (new_dir / thumb).read_bytes() == b"T" * 700
    assert not (old_root / f"rec{rid}").exists()
    art = client.get("/api/artifacts").json()[0]
    assert client.get(f"/api/artifacts/{art['id']}/file").content == b"P" * 5_000
    assert client.get(f"/api/artifacts/{art['id']}/thumbnail").content == b"T" * 700
    assert client.get("/api/artifacts/{}/file?download=1".format(art["id"])).headers["content-disposition"].startswith("attachment")


def test_cancelled_move_leaves_the_rest_untouched_and_can_be_resumed(ctx, monkeypatch):
    client, app, tmp = ctx
    a, b, new_dir = _two_recordings_and_a_new_root(client, app, tmp)
    real = move_mod.copy_verified
    calls = {"n": 0}

    def flaky(src, dst, cancelled, progress=None):
        calls["n"] += 1
        if "mv_b" in src.name:
            raise Cancelled()
        return real(src, dst, cancelled, progress)

    monkeypatch.setattr(move_mod, "copy_verified", flaky)
    client.post("/api/storage/move", json={"kind": "recordings"})
    st = wait_move(app)
    assert st["state"] == "cancelled" and st["done_items"] == 1
    old_dir = app.state.storage.default("recordings").path
    assert rec_row(app, a).root_id == app.state.storage.active("recordings").id
    assert rec_row(app, b).root_id != app.state.storage.active("recordings").id         # a b felvétel érintetlen
    assert (old_dir / "mv_b.sigmf-data").is_file() and not list(new_dir.glob("mv_b.*"))   # nincs félig kész másolat
    assert client.get(f"/api/recordings/{b}").json()["iq_available"] is True

    monkeypatch.setattr(move_mod, "copy_verified", real)                              # folytatás: csak a hátralévő megy át
    client.post("/api/storage/move", json={"kind": "recordings"})
    st = wait_move(app)
    assert st["state"] == "done" and st["total_items"] == 1 and (new_dir / "mv_b.sigmf-data").is_file()


def test_a_failing_item_is_reported_and_does_not_stop_the_others(ctx, monkeypatch):
    client, app, tmp = ctx
    a, b, new_dir = _two_recordings_and_a_new_root(client, app, tmp)
    real = move_mod.copy_verified
    monkeypatch.setattr(move_mod, "copy_verified", lambda src, dst, c, p=None: (_ for _ in ()).throw(OSError("disk error"))
                        if "mv_a" in src.name else real(src, dst, c, p))
    client.post("/api/storage/move", json={"kind": "recordings"})
    st = wait_move(app)
    assert st["state"] == "done" and st["done_items"] == 1
    assert [e["id"] for e in st["errors"]] == [a] and "disk error" in st["errors"][0]["error"]
    assert (app.state.storage.default("recordings").path / "mv_a.sigmf-data").is_file()  # a hibás elem eredetije megvan
    assert rec_row(app, a).root_id != app.state.storage.active("recordings").id
    assert rec_row(app, b).root_id == app.state.storage.active("recordings").id


def test_move_fails_early_without_enough_space_and_skips_busy_recordings(ctx, monkeypatch):
    client, app, tmp = ctx
    a, b, new_dir = _two_recordings_and_a_new_root(client, app, tmp)
    monkeypatch.setattr(move_mod.shutil, "disk_usage", lambda p: type("U", (), {"free": 1000})())
    client.post("/api/storage/move", json={"kind": "recordings"})
    st = wait_move(app)
    assert st["state"] == "failed" and "Not enough free space" in st["error"]
    assert rec_row(app, a).root_id != app.state.storage.active("recordings").id
    monkeypatch.undo()

    monkeypatch.setattr(move_mod, "busy_ids", None, raising=False)
    app.state.mover._busy = lambda session: {a}                                       # az "a" felvételt éppen feldolgozzák
    client.post("/api/storage/move", json={"kind": "recordings"})
    st = wait_move(app)
    assert st["state"] == "done" and st["done_items"] == 1 and st["skipped"] == [{"type": "recordings", "id": a, "reason": "busy"}]


def test_copy_verified_unit(tmp_path, monkeypatch):
    src = tmp_path / "s.bin"
    src.write_bytes(np.random.default_rng(0).bytes(300_000))
    digest = copy_verified(src, tmp_path / "d" / "out.bin", lambda: False)
    assert digest == hashlib.sha256(src.read_bytes()).hexdigest() and (tmp_path / "d" / "out.bin").read_bytes() == src.read_bytes()

    with pytest.raises(Cancelled):
        copy_verified(src, tmp_path / "c.bin", lambda: True)
    assert not (tmp_path / "c.bin").exists() and not list(tmp_path.glob("*.part"))

    def corrupt(fd):                                      # a lemezre írás közben megsérül a másolat
        with (tmp_path / "x.bin.part").open("ab") as f:
            f.write(b"!")
    monkeypatch.setattr(move_mod.os, "fsync", corrupt)
    with pytest.raises(OSError, match="Checksum mismatch"):
        copy_verified(src, tmp_path / "x.bin", lambda: False)
    assert not (tmp_path / "x.bin").exists() and not (tmp_path / "x.bin.part").exists()


# ---- letöltés ---------------------------------------------------------------------------------------------------

def test_downloads_stream_files_and_support_ranges(ctx):
    client, app, _ = ctx
    rid = make_rec(app, "dl", size=100_000)
    base = app.state.storage.recording_base(rec_row(app, rid))
    full = client.get(f"/api/recordings/{rid}/download")
    assert full.status_code == 200 and full.content == sigmf.data_path(base).read_bytes()
    assert full.headers["content-disposition"].endswith('dl.sigmf-data"')
    part = client.get(f"/api/recordings/{rid}/download", headers={"Range": "bytes=10-19"})
    assert part.status_code == 206 and part.content == sigmf.data_path(base).read_bytes()[10:20]
    assert part.headers["content-range"] == "bytes 10-19/100000"
    assert json.loads(client.get(f"/api/recordings/{rid}/download?part=meta").content)["global"]["core:sample_rate"] == 250_000
    assert client.get(f"/api/recordings/{rid}/download?part=orbit").status_code == 200
    assert client.get(f"/api/recordings/{rid}/download?part=zip").status_code == 422

    client.post("/api/storage/delete", json={"recording_ids": [rid]})
    gone = client.get(f"/api/recordings/{rid}/download")
    assert gone.status_code == 404 and "deleted" in gone.json()["detail"]
    assert client.get(f"/api/recordings/{rid}/download?part=meta").status_code == 200   # a metaadat megmaradt
    assert client.get("/api/recordings/9999/download").status_code == 404


# ---- export -------------------------------------------------------------------------------------------------------

def _wait_export(client, export_id, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/exports/{export_id}").json()
        if job["state"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError("export did not finish")


def test_zip_export_contains_everything_with_a_verifiable_manifest(ctx):
    client, app, _ = ctx
    a = make_rec(app, "ex_a", size=30_000, artifacts=2)
    b = make_rec(app, "ex_b", size=10_000, artifacts=1)
    r = client.post("/api/exports", json={"recording_ids": [a, b]})
    assert r.status_code == 202
    job = _wait_export(client, r.json()["id"])
    assert job["state"] == "done" and job["done_bytes"] == job["total_bytes"] and job["files"] == 9    # 2 x (IQ, meta, orbit) + 3 eredmény
    dl = client.get(f"/api/exports/{job['id']}/download")
    assert dl.status_code == 200 and dl.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(dl.content))
    names = set(zf.namelist())
    assert {"ex_a/ex_a.sigmf-data", "ex_a/ex_a.sigmf-meta", "ex_a/ex_a.orbit.json", "ex_b/ex_b.sigmf-data", "MANIFEST.json"} <= names
    assert len([n for n in names if n.startswith("ex_a/results/")]) == 2
    assert all(i.compress_type == zipfile.ZIP_STORED for i in zf.infolist())          # az IQ nem tömörödik: nincs fölösleges CPU
    manifest = json.loads(zf.read("MANIFEST.json"))
    assert manifest["software"] == "satrx" and len(manifest["files"]) == len(names) - 1
    for entry in manifest["files"]:
        assert hashlib.sha256(zf.read(entry["name"])).hexdigest() == entry["sha256"] and len(zf.read(entry["name"])) == entry["size"]
    assert zf.testzip() is None

    # csak a metaadat és az eredmények (IQ nélkül), plusz önálló eredmény
    art = client.get("/api/artifacts", params={"recording_id": b}).json()[0]["id"]
    light = _wait_export(client, client.post("/api/exports", json={"recording_ids": [a], "artifact_ids": [art], "include_iq": False}).json()["id"])
    zf2 = zipfile.ZipFile(io.BytesIO(client.get(f"/api/exports/{light['id']}/download").content))
    assert "ex_a/ex_a.sigmf-data" not in zf2.namelist() and any(n.startswith("results/") for n in zf2.namelist())


def test_export_validation_errors_and_status_lookups(ctx):
    client, app, tmp = ctx
    rid = make_rec(app, "v1")
    assert client.post("/api/exports", json={}).status_code == 422
    assert client.post("/api/exports", json={"recording_ids": [rid], "mode": "directory"}).status_code == 422
    assert client.post("/api/exports", json={"recording_ids": [rid], "mode": "directory", "directory": "relative"}).status_code == 422
    assert client.post("/api/exports", json={"recording_ids": [rid], "move": True}).status_code == 422          # zip + move
    assert client.post("/api/exports", json={"recording_ids": [rid], "mode": "directory", "directory": str(tmp / "d"),
                                             "move": True, "include_iq": False}).status_code == 422
    assert client.get("/api/exports/nonexistent").status_code == 404
    assert client.get("/api/exports/nonexistent/download").status_code == 404
    assert client.post("/api/exports/nonexistent/cancel").status_code == 404
    empty = make_rec(app, "gone", artifacts=0)
    client.post("/api/storage/delete", json={"recording_ids": [empty], "scope": "all"})
    failed = _wait_export(client, client.post("/api/exports", json={"recording_ids": [empty]}).json()["id"])
    assert failed["state"] == "failed" and "no files" in failed["error"]


def test_directory_export_copies_with_manifest_and_move_deletes_only_verified_unprotected(ctx):
    client, app, tmp = ctx
    keep = make_rec(app, "keep", protected=True)
    go = make_rec(app, "go", size=12_000, artifacts=1)
    dest = tmp / "usb_export"
    job = _wait_export(client, client.post("/api/exports", json={
        "recording_ids": [keep, go], "mode": "directory", "directory": str(dest), "move": True}).json()["id"])
    assert job["state"] == "done" and Path(job["result"]) == dest
    assert (dest / "go" / "go.sigmf-data").stat().st_size == 12_000
    assert (dest / "keep" / "keep.sigmf-data").is_file() and not list(dest.rglob("*.part"))
    manifest = json.loads((dest / "MANIFEST.json").read_text())
    for entry in manifest["files"]:
        assert hashlib.sha256((dest / entry["name"]).read_bytes()).hexdigest() == entry["sha256"]
    assert job["moved"]["recordings"] == 1 and job["moved"]["skipped"] == 1             # a védett kimarad a törlésből
    assert client.get(f"/api/recordings/{go}").status_code == 404 and client.get(f"/api/recordings/{keep}").status_code == 200
    ev = next(e for e in client.get("/api/events").json() if e["code"] == "export_finished")
    assert ev["details"]["mode"] == "directory"


def test_export_can_be_cancelled_and_leaves_no_partial_file(ctx, monkeypatch):
    client, app, tmp = ctx
    rid = make_rec(app, "cancelme", size=500_000)
    exporter = ExportManager(app.state.settings, app.state.storage, app.state.session_factory, lambda s: set())
    exporter.dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("satrx.storage.export.CHUNK", 64 * 1024)
    job = exporter.submit(ExportSpec(recording_ids=[rid]))
    assert exporter.cancel(job["id"]) is True
    with pytest.raises(Cancelled):
        exporter._run(job["id"])
    assert not list(exporter.dir.glob("*.zip*"))                                        # sem kész, sem részleges csomag


def test_old_export_files_are_cleaned_up(ctx):
    client, app, _ = ctx
    exporter = app.state.exporter
    stale = exporter.dir / "satrx_export_old.zip"
    stale.write_bytes(b"PK")
    old = time.time() - 3 * 24 * 3600
    import os
    os.utime(stale, (old, old))
    rid = make_rec(app, "cl")
    _wait_export(client, client.post("/api/exports", json={"recording_ids": [rid]}).json()["id"])
    assert not stale.exists()


def test_new_storage_endpoints_require_the_token(tmp_path):
    s = _settings(tmp_path / "d")
    s.server.auth_token = "secret"
    with TestClient(create_app(s, tle_fetcher=_fetch, startup_refresh=False, scheduler_enabled=False)) as client:
        for method, url in (("get", "/api/storage"), ("post", "/api/storage/delete"), ("get", "/api/storage/recordings"),
                            ("post", "/api/exports"), ("get", "/api/recordings/1/download")):
            assert getattr(client, method)(url).status_code == 401, url
        assert client.get("/api/storage", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_artifact_retention_is_optional_and_never_touches_protected_recordings(ctx):
    client, app, _ = ctx
    old = make_rec(app, "old_results", age_days=3, artifacts=2, art_size=1_000)
    old_protected = make_rec(app, "old_protected", age_days=3, artifacts=1, art_size=1_000, protected=True)
    fresh = make_rec(app, "fresh_results", age_days=0.1, artifacts=1, art_size=1_000)
    with app.state.session_factory() as s:                      # az eredmények kora a létrehozásuk ideje
        for a in s.query(Artifact).filter(Artifact.recording_id.in_([old, old_protected])):
            a.created_at = NOW - timedelta(days=3)
        s.commit()

    app.state.settings.storage.iq_retention_days = 0
    assert client.post("/api/storage/retention", json={}).json()["artifacts_deleted"] == 0       # alapból kikapcsolva
    app.state.settings.storage.artifact_retention_days = 1

    plan = client.post("/api/storage/retention", json={"dry_run": True}).json()
    assert plan["artifacts_deleted"] == 2 and plan["freed_bytes"] == 2_000 and n_artifacts(app) == 4
    done = client.post("/api/storage/retention", json={"dry_run": False}).json()
    assert done["artifacts_deleted"] == 2
    assert n_artifacts(app, old) == 0 and n_artifacts(app, old_protected) == 1 and n_artifacts(app, fresh) == 1
    assert rec_row(app, old).deleted is False                                                      # a nyers IQ-hoz nem nyúl
    ev = next(e for e in client.get("/api/events").json() if e["code"] == "retention_deleted")
    assert ev["details"]["artifacts"] == 2
    assert client.get("/api/storage").json()["retention"]["artifact_retention_days"] == 1
