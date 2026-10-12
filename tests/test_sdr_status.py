"""Az SDR-állapot láthatósága: backend, szimulált jelzés, eszközfelismerés, illesztőprogram-hibák."""
import json
import sys
import time
import types

import pytest
from fastapi.testclient import TestClient

from satrx import doctor
from satrx.api.app import create_app
from satrx.core.config import SdrConfig, Settings
from satrx.hardware import probe as probe_mod
from satrx.hardware.probe import CachedProbe, probe_sdr
from satrx.recording import sigmf
from tests.test_scheduler import _fetch, _settings

F0 = 145_800_000


class LibUSBError(Exception):
    """Pontosan az a hiba, amit a pyrtlsdr a hiányzó WinUSB-illesztővel dob."""


USER_ERROR = LibUSBError('<LIBUSB_ERROR_NOT_FOUND (-5): Entity not found> "while reading USB strings (device 0)"')


def fake_rtlsdr(monkeypatch, serials=None, error=None, import_error=None):
    mod = types.ModuleType("rtlsdr")
    if import_error is not None:
        def getattr_(name):
            raise import_error
        mod.__getattr__ = getattr_
    else:
        class RtlSdr:
            @staticmethod
            def get_device_serial_addresses():
                if error:
                    raise error
                return serials or []
        mod.RtlSdr = RtlSdr
    monkeypatch.setitem(sys.modules, "rtlsdr", mod)


def settings_for(backend, tmp_path=None):
    s = Settings(data_dir=tmp_path) if tmp_path else Settings()
    s.sdr = SdrConfig(backend=backend)
    return s


# ---- próba ---------------------------------------------------------------------------------------------------------

def test_probe_distinguishes_every_situation(monkeypatch):
    assert probe_sdr(settings_for("mock")).status == "mock" and probe_sdr(settings_for("mock")).simulated is True
    assert probe_sdr(settings_for("replay")).simulated is True
    odd = types.SimpleNamespace(sdr=types.SimpleNamespace(backend="banana"))          # a konfiguráció ezt már betöltéskor elutasítja
    assert probe_sdr(odd).status == "unknown_backend"

    s = settings_for("rtlsdr")
    monkeypatch.setitem(sys.modules, "rtlsdr", None)
    assert probe_sdr(s).status == "no_package"
    fake_rtlsdr(monkeypatch, import_error=OSError("error loading librtlsdr.dll"))
    p = probe_sdr(s)
    assert p.status == "no_library" and "librtlsdr" in p.message
    fake_rtlsdr(monkeypatch, error=USER_ERROR)                                   # a felhasználó esete
    p = probe_sdr(s)
    assert p.status == "no_driver" and "LIBUSB_ERROR_NOT_FOUND" in p.message and not p.ok
    fake_rtlsdr(monkeypatch, error=RuntimeError("boom"))
    assert probe_sdr(s).status == "probe_failed"
    fake_rtlsdr(monkeypatch, serials=[])
    assert probe_sdr(s).status == "not_found"
    fake_rtlsdr(monkeypatch, serials=["00000001", "00000002"])
    p = probe_sdr(s)
    assert p.status == "ok" and p.ok and not p.simulated and p.devices == [{"index": 0, "serial": "00000001"}, {"index": 1, "serial": "00000002"}]


def test_probe_cache_and_refresh(monkeypatch):
    calls = []
    monkeypatch.setattr(probe_mod, "probe_sdr", lambda s: calls.append(1) or probe_mod.SdrProbe("mock", "mock"))
    cached = CachedProbe(Settings(), ttl_s=60)
    cached.get(), cached.get()
    assert len(calls) == 1
    cached.get(refresh=True)
    assert len(calls) == 2


# ---- API és állapotellenőrzés -------------------------------------------------------------------------------------

def make_app(tmp_path, backend="mock", realtime=False):
    s = _settings(tmp_path / "d")
    s.sdr = SdrConfig(backend=backend, device_args={"realtime": realtime} if backend == "mock" else {})
    return create_app(s, tle_fetcher=_fetch, startup_refresh=False, scheduler_enabled=False)


def sdr_component(client):
    return next(c for c in client.get("/api/health/detailed").json()["components"] if c["id"] == "sdr")


def test_mock_backend_is_flagged_everywhere(tmp_path):
    with TestClient(make_app(tmp_path)) as client:
        info = client.get("/api/sdr").json()
        assert info["backend"] == "mock" and info["simulated"] is True and info["probe"]["status"] == "mock"
        comp = sdr_component(client)
        assert comp["status"] == "warning" and comp["code"] == "sdr_mock" and comp["details"]["simulated"] is True
        assert "synthetic" in comp["message"]
        with client.stream("GET", "/api/stream?duration_s=0.3") as r:
            line = next(l for l in r.iter_lines() if l.startswith("data:"))
        assert json.loads(line[5:])["sdr"] == {"backend": "mock", "simulated": True}


def test_recordings_remember_which_backend_made_them(tmp_path):
    with TestClient(make_app(tmp_path)) as client:
        client.post("/api/recorder/start", json={"center_freq_hz": F0, "sample_rate_hz": 250_000, "duration_s": 1})
        for _ in range(100):
            if client.app.state.recorder.status().state == "idle":
                break
            time.sleep(0.05)
        rec = client.get("/api/recordings").json()[0]
        assert rec["source"] == "mock"
        assert client.get(f"/api/recordings/{rec['id']}").json()["recording"]["source"] == "mock"
        assert client.get("/api/storage/recordings").json()["items"][0]["source"] == "mock"
        base = client.app.state.settings.recordings_path / rec["path"]
        assert sigmf.read_info(base).extra["satrx:sdr_backend"] == "mock"
        assert json.loads(sigmf.orbit_path(base).read_text())["radio"]["backend"] == "mock"


def test_a_real_backend_reports_its_device_state(tmp_path, monkeypatch):
    with TestClient(make_app(tmp_path, backend="rtlsdr")) as client:
        fake_rtlsdr(monkeypatch, error=USER_ERROR)
        client.get("/api/sdr?refresh=true")
        comp = sdr_component(client)
        assert comp["status"] == "error" and comp["code"] == "sdr_no_driver" and "LIBUSB" in comp["message"]
        assert client.get("/api/health/detailed").json()["status"] == "error"

        fake_rtlsdr(monkeypatch, serials=[])
        client.get("/api/sdr?refresh=true")
        assert sdr_component(client)["code"] == "sdr_not_found"

        fake_rtlsdr(monkeypatch, serials=["00000001"])
        info = client.get("/api/sdr?refresh=true").json()
        assert info["simulated"] is False and info["probe"]["devices"] == [{"index": 0, "serial": "00000001"}]
        comp = sdr_component(client)
        assert comp["status"] == "ok" and comp["code"] == "sdr_idle" and comp["details"]["devices"][0]["serial"] == "00000001"


def test_the_device_is_not_probed_while_it_is_recording(tmp_path, monkeypatch):
    calls = []
    with TestClient(make_app(tmp_path, realtime=True)) as client:
        monkeypatch.setattr(client.app.state.sdr_probe, "get", lambda refresh=False: calls.append(1))
        client.post("/api/recorder/start", json={"center_freq_hz": F0, "sample_rate_hz": 250_000, "duration_s": 30})
        info = client.get("/api/sdr").json()
        assert info["recorder_state"] == "recording" and info["probe"] is None
        assert sdr_component(client)["code"] == "sdr_recording" and calls == []
        client.post("/api/recorder/stop")


def test_a_failed_open_explains_the_driver_problem(tmp_path, monkeypatch):
    class Failing:
        def __init__(self, device_index=0):
            raise USER_ERROR
    mod = types.ModuleType("rtlsdr")
    mod.RtlSdr = Failing
    monkeypatch.setitem(sys.modules, "rtlsdr", mod)
    with TestClient(make_app(tmp_path, backend="rtlsdr")) as client:
        r = client.post("/api/recorder/start", json={"center_freq_hz": F0, "sample_rate_hz": 250_000, "duration_s": 2})
        assert r.status_code == 202
        for _ in range(100):
            if client.app.state.recorder.status().state == "idle":
                break
            time.sleep(0.05)
        time.sleep(0.2)
        ev = next(e for e in client.get("/api/events").json() if e["code"] == "recording_failed")
        assert "WinUSB" in ev["message"] and "Zadig" in ev["message"] and ev["level"] == "error"
        rec = client.get("/api/recordings").json()[0]
        assert rec["source"] == "rtlsdr" and rec["size_bytes"] == 0


# ---- doctor ---------------------------------------------------------------------------------------------------------

USER_PNP = ("OK|USB|USB\\VID_0BDA&PID_2838\\00000001\n"
            "Error||USB\\VID_0BDA&PID_2838&MI_00\\6&38A180A5&0&0000\n"
            "Error||USB\\VID_0BDA&PID_2838&MI_01\\6&38A180A5&0&0001")


def test_doctor_recognizes_the_missing_windows_driver(monkeypatch):
    run = lambda out: (lambda args: (0, out))  # noqa: E731
    rtl, mock = settings_for("rtlsdr"), settings_for("mock")
    c = doctor.check_rtlsdr_windows(rtl, "Windows", run(USER_PNP))
    assert c.status == doctor.FAIL and "no working driver" in c.message and "Zadig" in c.hint and "Interface 0" in c.hint
    assert doctor.check_rtlsdr_windows(mock, "Windows", run(USER_PNP)).status == doctor.WARN       # mockkal csak figyelmeztetés

    good = ("OK|USB|USB\\VID_0BDA&PID_2838\\00000001\nOK|USBDevice|USB\\VID_0BDA&PID_2838&MI_00\\6&1&0&0000\n"
            "OK|Camera|USB\\VID_0BDA&PID_2838&MI_01\\6&1&0&0001")
    assert doctor.check_rtlsdr_windows(rtl, "Windows", run(good)).status == doctor.OK
    absent = doctor.check_rtlsdr_windows(rtl, "Windows", run(""))
    assert absent.status == doctor.WARN and "No RTL-SDR" in absent.message
    assert doctor.check_rtlsdr_windows(mock, "Windows", run("")).status == doctor.INFO
    assert doctor.check_rtlsdr_windows(rtl, "Windows", lambda a: (127, "")).status == doctor.INFO
    assert doctor.check_rtlsdr_windows(rtl, "Linux", run(USER_PNP)) is None


def test_doctor_sdr_check_gives_the_driver_hint_for_the_users_error(monkeypatch):
    fake_rtlsdr(monkeypatch, error=USER_ERROR)
    c = doctor.check_sdr(settings_for("rtlsdr"))
    assert c.status == doctor.FAIL and "not accessible" in c.message and "WinUSB" in c.hint and "Zadig" in c.hint
    fake_rtlsdr(monkeypatch, serials=["x"])
    assert doctor.check_sdr(settings_for("rtlsdr")).status == doctor.OK
