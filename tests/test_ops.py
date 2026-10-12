"""10. fázis: üzemeltetési kód (sd_notify, összeomlás utáni helyreállítás, doctor, SQLite beállítások)."""
import json
import os
import socket
import sys
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient

from satrx import doctor, sdnotify
from satrx.api.app import create_app
from satrx.core.config import Settings
from satrx.core.db import make_engine
from satrx.core.models import Job, Pass, Recording
from satrx.recording import sigmf
from tests.test_scheduler import _fetch, _settings
from tests.test_storage import make_rec, rec_row

NOW = datetime.now(timezone.utc)


# ---- sd_notify ------------------------------------------------------------------------------------------------

class FakeSocket:
    sent: list[bytes] = []

    def __init__(self, *a):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def connect(self, path):
        FakeSocket.path = path

    def sendall(self, data):
        FakeSocket.sent.append(data)


@pytest.fixture
def fake_notify(monkeypatch):
    FakeSocket.sent = []
    monkeypatch.setattr(sdnotify.socket, "socket", FakeSocket)
    monkeypatch.setattr(sdnotify.socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(sdnotify.socket, "SOCK_DGRAM", 2, raising=False)
    sdnotify.reset_for_tests()
    return FakeSocket


def test_notify_is_a_noop_without_a_notify_socket(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    assert sdnotify.notify("READY=1") is False and sdnotify.ready() is False
    assert sdnotify.watchdog_interval() is None and sdnotify.watchdog_ping() is False


def test_ready_stopping_and_abstract_socket_path(fake_notify, monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", "@/org/freedesktop/systemd/notify")
    assert sdnotify.ready("satrx 0.1") is True
    assert fake_notify.sent[-1] == b"READY=1\nSTATUS=satrx 0.1" and fake_notify.path == "\0/org/freedesktop/systemd/notify"
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/systemd/notify")
    assert sdnotify.stopping() is True and fake_notify.sent[-1] == b"STOPPING=1" and fake_notify.path == "/run/systemd/notify"


def test_watchdog_pings_at_half_the_timeout_and_are_throttled(fake_notify, monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/systemd/notify")
    monkeypatch.setenv("WATCHDOG_USEC", "120000000")                      # 120 s -> 60 s-onként
    assert sdnotify.watchdog_interval() == 60.0
    assert sdnotify.watchdog_ping(now=1000.0) is True
    assert sdnotify.watchdog_ping(now=1030.0) is False                       # túl korán
    assert sdnotify.watchdog_ping(now=1061.0) is True
    assert fake_notify.sent == [b"WATCHDOG=1", b"WATCHDOG=1"]
    monkeypatch.setenv("WATCHDOG_USEC", "garbage")
    assert sdnotify.watchdog_interval() is None


def test_notify_survives_a_dead_socket(monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", "/nonexistent/path/notify")
    assert sdnotify.notify("READY=1") is False                               # nem dobhat kivételt


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX") or os.name == "nt", reason="Unix datagram socket")
def test_notify_over_a_real_unix_socket(tmp_path, monkeypatch):
    path = str(tmp_path / "notify.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(path)
    server.settimeout(2)
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    assert sdnotify.ready("hello") is True
    assert server.recv(100) == b"READY=1\nSTATUS=hello"
    server.close()


def test_the_scheduler_loop_feeds_the_watchdog(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sdnotify, "watchdog_ping", lambda *a: calls.append(1) or True)
    from satrx.scheduler import service as sched
    monkeypatch.setattr(sched, "TICK_S", 0.05)
    app = create_app(_settings(tmp_path / "d"), tle_fetcher=_fetch, startup_refresh=False, scheduler_enabled=True)
    with TestClient(app):
        time.sleep(0.4)
    assert len(calls) >= 3


# ---- helyreállítás ------------------------------------------------------------------------------------------------

def _crash_state(app):
    """Egy hirtelen leállás után hátrahagyott állapot: futó felvétel lezáratlan IQ-val, megszakadt feldolgozás."""
    sm = app.state.storage
    rec_id = make_rec(app, "crashed", size=40_000, processed=False, artifacts=0)
    base = sm.recording_base(rec_row(app, rec_id))
    sigmf.meta_path(base).unlink()                                             # a metaadat a lezáráskor készül: nincs
    with app.state.session_factory() as s:
        rec = s.get(Recording, rec_id)
        rec.ended_at, rec.size_bytes = None, 0
        job = s.query(Job).filter(Job.recording_id == rec_id).first()
        job.status = "running"
        s.add(Pass(norad_id=25544, aos=NOW, tca=NOW, los=NOW, max_elevation_deg=40, status="recording"))
        s.add(Pass(norad_id=25544, aos=NOW, tca=NOW, los=NOW, max_elevation_deg=40, status="processing"))
        s.add(Pass(norad_id=25544, aos=NOW + timedelta(hours=1), tca=NOW, los=NOW, max_elevation_deg=40, status="planned"))
        s.add(Job(kind="processing", status="queued", recording_id=rec_id))
        s.commit()
    (sm.active("recordings").path / "copy.sigmf-data.part").write_bytes(b"x")   # félbemaradt másolat
    (app.state.settings.data_path / "exports").mkdir(exist_ok=True)
    (app.state.settings.data_path / "exports" / "x.zip.part").write_bytes(b"x")
    return rec_id


def test_recovery_after_an_unclean_shutdown(tmp_path):
    settings = _settings(tmp_path / "d")
    with TestClient(create_app(settings, tle_fetcher=_fetch, startup_refresh=False, scheduler_enabled=False)) as first:
        first.post("/api/satellites", json={"norad_id": 25544})
        rec_id = _crash_state(first.app)
        base = first.app.state.storage.recording_base(rec_row(first.app, rec_id))

    # új indulás ugyanazzal az adatkönyvtárral = "újraindítás a leállás után"
    with TestClient(create_app(settings, tle_fetcher=_fetch, startup_refresh=False, scheduler_enabled=False)) as client:
        app = client.app
        with app.state.session_factory() as s:
            statuses = sorted(p.status for p in s.query(Pass))
            jobs = {j.status: j.error for j in s.query(Job)}
            rec = s.get(Recording, rec_id)
        assert statuses == ["failed", "failed", "planned"]                       # a tervezett átvonulás érintetlen
        assert set(jobs) == {"failed", "done"} or set(jobs) == {"failed"}
        assert any(e and "Interrupted" in e for e in jobs.values())
        assert rec.ended_at is not None and rec.size_bytes == 40_000
        # a lezáratlan IQ-hoz pótolt metaadat: a felvétel újra olvasható és feldolgozható
        info = sigmf.read_info(base)
        assert info.sample_rate_hz == 250_000 and info.num_samples == 20_000 and info.center_freq_hz == 145_800_000
        assert not list(app.state.storage.active("recordings").path.glob("*.part"))
        assert not list((settings.data_path / "exports").glob("*.part"))
        ev = next(e for e in client.get("/api/events").json() if e["code"] == "recovered_after_crash")
        assert ev["level"] == "warning" and ev["details"]["passes"] == 2 and ev["details"]["repaired_meta"] == 1
        assert ev["details"]["part_files"] == 2 and ev["details"]["recordings"] == 1
        # a helyreállított felvételt a felület fel tudja dolgozni
        pid = next(p["id"] for p in client.get("/api/pipelines").json() if p["name"] == "FM audio")
        r = client.post(f"/api/recordings/{rec_id}/process", json={"pipeline_id": pid})
        assert r.status_code == 202


def test_a_clean_start_changes_nothing_and_emits_no_event(tmp_path):
    with TestClient(create_app(_settings(tmp_path / "d"), tle_fetcher=_fetch, startup_refresh=False, scheduler_enabled=False)) as client:
        assert not [e for e in client.get("/api/events").json() if e["code"] == "recovered_after_crash"]


def test_a_missing_iq_file_of_an_unfinished_recording_is_marked_deleted(tmp_path):
    settings = _settings(tmp_path / "d")
    with TestClient(create_app(settings, tle_fetcher=_fetch, startup_refresh=False, scheduler_enabled=False)) as first:
        first.post("/api/satellites", json={"norad_id": 25544})
        rid = make_rec(first.app, "lost", artifacts=0)
        base = first.app.state.storage.recording_base(rec_row(first.app, rid))
        sigmf.data_path(base).unlink()
        with first.app.state.session_factory() as s:
            s.get(Recording, rid).ended_at = None
            s.commit()
    with TestClient(create_app(settings, tle_fetcher=_fetch, startup_refresh=False, scheduler_enabled=False)) as client:
        assert rec_row(client.app, rid).deleted is True


# ---- SQLite beállítások ---------------------------------------------------------------------------------------------

def test_sqlite_uses_wal_normal_sync_and_a_busy_timeout(tmp_path):
    engine = make_engine(tmp_path / "x.db")
    with engine.connect() as c:
        assert c.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert c.exec_driver_sql("PRAGMA synchronous").scalar() == 1             # NORMAL
        assert c.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


# ---- doctor ------------------------------------------------------------------------------------------------------------

def _by_id(checks):
    out = {}
    for c in checks:
        out[c.id] = c
    return out


def test_doctor_basics_on_a_fresh_setup(tmp_path):
    s = Settings(data_dir=tmp_path / "data")
    checks = _by_id(doctor.run_all(s))
    assert checks["python"].status == doctor.OK and checks["dependencies"].status == doctor.OK
    assert checks["data_dir"].status == doctor.OK and "writable" in checks["data_dir"].message
    assert checks["sdr"].status == doctor.WARN and "mock" in checks["sdr"].message
    assert checks["observer"].status == doctor.WARN
    assert checks["auth"].status == doctor.WARN                                  # 0.0.0.0-n token nélkül: figyelmeztetés
    assert checks["storage"].status == doctor.INFO                                # még nincs adatbázis
    assert checks["satdump"].status in (doctor.OK, doctor.WARN)


def test_doctor_auth_levels(tmp_path):
    s = Settings(data_dir=tmp_path)
    s.server.host = "127.0.0.1"
    assert doctor.check_config(s)[-1].status == doctor.INFO
    s.server.host, s.server.auth_token = "0.0.0.0", "secret"
    assert doctor.check_config(s)[-1].status == doctor.OK


def test_doctor_reports_storage_roots_from_the_database(tmp_path):
    settings = _settings(tmp_path / "d")
    with TestClient(create_app(settings, tle_fetcher=_fetch, startup_refresh=False, scheduler_enabled=False)) as client:
        client.app.state.storage.register_root("recordings", tmp_path / "usb")
    ok = _by_id(doctor.check_storage_roots(settings))
    assert ok["storage_recordings"].status == doctor.OK
    (tmp_path / "usb" / ".satrx-root").unlink()                                   # a meghajtó "eltűnt"
    checks = doctor.check_storage_roots(settings)
    active_missing = [c for c in checks if c.id == "storage_recordings" and c.status == doctor.FAIL]
    assert len(active_missing) == 1 and "not available" in active_missing[0].message and active_missing[0].hint


def test_doctor_data_dir_failure(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    c = doctor.check_data_dir(Settings(data_dir=blocker / "sub"))
    assert c.status == doctor.FAIL and "not writable" in c.message


def test_doctor_time_sync_parsing():
    assert doctor.check_time_sync("Linux", lambda a: (0, "yes")).status == doctor.OK
    bad = doctor.check_time_sync("Linux", lambda a: (0, "no"))
    assert bad.status == doctor.WARN and "NOT synchronized" in bad.message and "set-ntp" in bad.hint
    assert doctor.check_time_sync("Linux", lambda a: (127, "")).status == doctor.WARN
    assert doctor.check_time_sync("Windows").status == doctor.INFO


def test_doctor_linux_rtlsdr_checks(tmp_path):
    (tmp_path / "etc/modprobe.d").mkdir(parents=True)
    (tmp_path / "etc/udev/rules.d").mkdir(parents=True)

    def run(args, loaded=False):
        return (0, "dvb_usb_rtl28xxu 28672 0\nusbcore 300 1" if loaded else "usbcore 300 1")

    none = _by_id(doctor.check_rtlsdr_linux("Linux", run, tmp_path))
    assert none["dvb_module"].status == doctor.WARN and none["udev"].status == doctor.WARN

    (tmp_path / "etc/modprobe.d/rtl.conf").write_text("blacklist dvb_usb_rtl28xxu\n")
    (tmp_path / "etc/udev/rules.d/99-satrx-rtlsdr.rules").write_text("x")
    good = _by_id(doctor.check_rtlsdr_linux("Linux", run, tmp_path))
    assert good["dvb_module"].status == doctor.OK and good["udev"].status == doctor.OK

    loaded = _by_id(doctor.check_rtlsdr_linux("Linux", lambda a: run(a, loaded=True), tmp_path))
    assert loaded["dvb_module"].status == doctor.FAIL and "modprobe -r" in loaded["dvb_module"].hint
    assert doctor.check_rtlsdr_linux("Windows") == []


def test_doctor_sdr_backend_variants(monkeypatch):
    s = Settings()
    s.sdr.backend = "replay"
    assert doctor.check_sdr(s).status == doctor.INFO
    s.sdr.backend = "rtlsdr"
    monkeypatch.setitem(sys.modules, "rtlsdr", None)                             # nincs telepítve
    c = doctor.check_sdr(s)
    assert c.status == doctor.FAIL and "pyrtlsdr" in c.message

    class FakeRtl:
        devices = []

        @staticmethod
        def get_device_serial_addresses():
            return FakeRtl.devices

    monkeypatch.setitem(sys.modules, "rtlsdr", type(sys)("rtlsdr"))
    sys.modules["rtlsdr"].RtlSdr = FakeRtl
    assert doctor.check_sdr(s).status == doctor.FAIL and "No RTL-SDR" in doctor.check_sdr(s).message
    FakeRtl.devices = ["00000001"]
    assert doctor.check_sdr(s).status == doctor.OK
    s.sdr.backend = "banana"
    assert doctor.check_sdr(s).status == doctor.FAIL


def test_doctor_satdump_and_port(tmp_path):
    s = Settings(data_dir=tmp_path)
    s.tools.satdump_path = str(tmp_path / "missing")
    assert doctor.check_satdump(s).status == doctor.WARN
    s.tools.satdump_path = sys.executable
    assert doctor.check_satdump(s).status == doctor.OK
    with socket.socket() as srv:                                                   # a port foglalt: nincs hiba, csak tájékoztatás
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        port = srv.getsockname()[1]
        s.server.port = port
        assert doctor.check_port(s).status == doctor.INFO
    assert doctor.check_port(s).status == doctor.OK                                # lezárva: újra szabad


def test_doctor_cli_exit_code_and_json(tmp_path, capsys):
    s = Settings(data_dir=tmp_path / "data")
    assert doctor.main(["--json"], s) in (0, 1)
    data = json.loads(capsys.readouterr().out)
    assert {c["id"] for c in data} >= {"python", "dependencies", "sdr", "data_dir"}
    assert all(set(c) == {"id", "status", "message", "hint"} for c in data)
    code = doctor.main([], s)
    out = capsys.readouterr().out
    assert "[ OK ]" in out and "checks:" in out and code in (0, 1)
    s.data_dir = tmp_path / "file" / "x"
    (tmp_path / "file").write_text("x")
    assert doctor.main([], s) == 1                                                  # FAIL -> nem nulla kilépési kód


def test_main_dispatches_the_doctor_subcommand(tmp_path, monkeypatch, capsys):
    from satrx import __main__ as entry
    monkeypatch.setattr(doctor, "main", lambda argv, settings=None: 3)
    with pytest.raises(SystemExit) as exc:
        entry.main(["doctor", "--json"])
    assert exc.value.code == 3
