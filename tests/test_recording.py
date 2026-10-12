import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from satrx.api.app import create_app
from satrx.core.config import SdrConfig, Settings
from satrx.hardware.base import SdrError
from satrx.hardware.factory import create_device
from satrx.hardware.mock import MockDevice
from satrx.hardware.replay import FileReplayDevice
from satrx.recording import sigmf
from satrx.recording.recorder import RecordSpec, record

SR = 250_000
FREQ = 145_800_000


def _record_mock(tmp_path, fmt="cu8", duration=0.5, name="rec"):
    base = tmp_path / name
    with MockDevice(realtime=False) as dev:
        result = record(dev, RecordSpec(FREQ, SR, duration, fmt=fmt), base)
    return base, result


@pytest.mark.parametrize("fmt,tol", [("cf32_le", 1e-6), ("ci16_le", 1e-4), ("cu8", 1 / 127.5)])
def test_encode_decode_roundtrip(fmt, tol):
    rng = np.random.default_rng(1)
    x = (rng.uniform(-1, 1, 1000) + 1j * rng.uniform(-1, 1, 1000)).astype(np.complex64)
    y = sigmf.decode(sigmf.encode(x, fmt), fmt)
    assert y.shape == x.shape
    assert np.max(np.abs(y - x)) <= tol * 1.5


@pytest.mark.parametrize("fmt", sigmf.FORMATS)
def test_record_writes_valid_sigmf(tmp_path, fmt):
    base, result = _record_mock(tmp_path, fmt)
    assert result.num_samples == SR // 2 and not result.stopped_early
    info = sigmf.read_info(base)
    assert (info.fmt, info.sample_rate_hz, info.center_freq_hz) == (fmt, SR, FREQ)
    assert info.num_samples == result.num_samples
    assert result.size_bytes == info.num_samples * sigmf.bytes_per_sample(fmt)
    blocks = list(sigmf.read_blocks(base, block_samples=50_000))
    assert sum(len(b) for b in blocks) == info.num_samples


def test_mock_signal_has_carrier_at_offset(tmp_path):
    base, _ = _record_mock(tmp_path, "cf32_le", 0.2)
    iq = np.concatenate(list(sigmf.read_blocks(base)))
    power = np.abs(np.fft.fftshift(np.fft.fft(iq * np.hanning(len(iq))))) ** 2
    freqs = np.fft.fftshift(np.fft.fftfreq(len(iq), 1 / SR))
    # az FM oldalvonalak miatt a csúcs nem pont a vivőn van; a teljesítménysúlyozott közép igen
    assert abs(np.sum(freqs * power) / np.sum(power) - 10_000) < 500


def test_stop_event_ends_early(tmp_path):
    import threading

    stop = threading.Event()
    stop.set()
    with MockDevice(realtime=False) as dev:
        result = record(dev, RecordSpec(FREQ, SR, 10, fmt="cu8"), tmp_path / "x", stop)
    assert result.stopped_early and result.num_samples == 0
    assert sigmf.meta_path(tmp_path / "x").exists()


def test_replay_device(tmp_path):
    base, _ = _record_mock(tmp_path, "ci16_le", 0.2)
    with FileReplayDevice(base) as dev:
        dev.configure(FREQ, SR)
        first = dev.read(1000)
        assert len(first) == 1000
        with pytest.raises(SdrError):
            dev.configure(FREQ, SR * 2)
        with pytest.raises(SdrError):
            dev.read(10 * SR)  # EOF


def test_capability_check_rejects_out_of_range():
    with MockDevice() as dev:
        with pytest.raises(SdrError):
            dev.configure(5_000_000, SR)
        with pytest.raises(SdrError):
            dev.configure(FREQ, 10_000_000)


def test_factory_rejects_missing_driver():
    with pytest.raises(SdrError):
        with create_device(SdrConfig(backend="soapy")):
            pass  # SoapySDR nincs telepítve a tesztkörnyezetben


def _settings(tmp_path) -> Settings:
    s = Settings(data_dir=tmp_path)
    s.sdr = SdrConfig(backend="mock", device_args={"realtime": False})
    return s


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(_settings(tmp_path), startup_refresh=False)) as c:
        yield c


def _wait_idle(client, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get("/api/recorder/status").json()
        if st["state"] == "idle":
            return st
        time.sleep(0.05)
    raise AssertionError("recorder did not finish")


def test_api_manual_recording(client, tmp_path):
    body = {"center_freq_hz": FREQ, "sample_rate_hz": SR, "duration_s": 2}
    r = client.post("/api/recorder/start", json=body)
    assert r.status_code == 202, r.text
    rec_id = r.json()["id"]
    assert r.json()["started_at"].endswith("Z") or "+" in r.json()["started_at"]
    _wait_idle(client)

    recs = client.get("/api/recordings").json()
    assert [x["id"] for x in recs] == [rec_id]
    assert recs[0]["size_bytes"] == 2 * SR * 2  # cu8: 2 bájt / minta
    assert (tmp_path / "recordings" / (recs[0]["path"] + ".sigmf-data")).exists()

    assert client.delete(f"/api/recordings/{rec_id}").status_code == 204
    assert client.get("/api/recordings").json() == []
    assert [p.name for p in (tmp_path / "recordings").iterdir() if p.name != ".satrx-root"] == []


def test_api_validation_and_busy(tmp_path):
    s = _settings(tmp_path)
    s.sdr = SdrConfig(backend="mock", device_args={"realtime": True})  # valós idejű: marad foglalt
    with TestClient(create_app(s, startup_refresh=False)) as client:
        bad = {"center_freq_hz": 5_000_000, "sample_rate_hz": SR, "duration_s": 1}
        assert client.post("/api/recorder/start", json=bad).status_code == 422

        ok = {"center_freq_hz": FREQ, "sample_rate_hz": SR, "duration_s": 30}
        assert client.post("/api/recorder/start", json=ok).status_code == 202
        assert client.post("/api/recorder/start", json=ok).status_code == 409
        assert client.get("/api/recorder/status").json()["state"] == "recording"
        rec_id = client.get("/api/recordings").json()[0]["id"]
        assert client.delete(f"/api/recordings/{rec_id}").status_code == 409

        assert client.post("/api/recorder/stop").status_code == 204
        _wait_idle(client)
        rec = client.get("/api/recordings").json()[0]
        assert 0 < rec["size_bytes"] < 30 * SR * 2
