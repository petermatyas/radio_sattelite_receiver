"""Az rtl_sdr / rtl_fm külső programok használata (hamis programmal, tests/fake_rtl.py)."""
import json
import os
import stat
import sys
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from satrx.api.app import create_app
from satrx.core.config import SdrConfig, Settings
from satrx.hardware.base import SdrError
from satrx.hardware.probe import probe_sdr
from satrx.hardware.rtlsdr_tool import RtlSdrToolDevice
from satrx.recording import sigmf
from tests.test_scheduler import _fetch

SCRIPT = Path(__file__).with_name("fake_rtl.py")


@pytest.fixture
def fake_rtl(tmp_path, monkeypatch):
    """Futtatható `rtl_sdr`, `rtl_fm` és `rtl_test` indítók a hamis programhoz."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    paths = {}
    for name in ("rtl_sdr", "rtl_fm"):
        if os.name == "nt":
            launcher = bin_dir / f"{name}.cmd"
            launcher.write_text(f'@echo off\r\n"{sys.executable}" "{SCRIPT}" %*\r\n')
        else:
            launcher = bin_dir / name
            launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{SCRIPT}" "$@"\n')
            launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
        paths[name] = launcher
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_RTL_LOG", str(log))
    monkeypatch.delenv("FAKE_RTL", raising=False)
    return SimpleNamespace(paths=paths, mode=lambda m: monkeypatch.setenv("FAKE_RTL", m),
                           calls=lambda: [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else [])


def _app(tmp_path, fake_rtl, backend="rtl_sdr", with_fm=True, fm_path=None, **device_args):
    s = Settings(data_dir=tmp_path / "data")
    s.sdr = SdrConfig(backend=backend, device_args=device_args)
    s.tools.rtl_sdr_path = str(fake_rtl.paths["rtl_sdr"])
    if with_fm:
        s.tools.rtl_fm_path = fm_path or str(fake_rtl.paths["rtl_fm"])
    return create_app(s, tle_fetcher=_fetch, startup_refresh=False, scheduler_enabled=False)


def _wait_idle(client, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if client.get("/api/recorder/status").json()["state"] == "idle":
            return
        time.sleep(0.1)
    raise AssertionError("recorder still busy")


def test_device_reads_normalised_iq_and_passes_the_arguments(fake_rtl):
    dev = RtlSdrToolDevice(command=[str(fake_rtl.paths["rtl_sdr"])], device_index=1, ppm=5)
    with dev:
        dev.configure(446_006_250, 250_000, gain_db=20)
        block = dev.read(25_000)
        assert block.dtype == np.complex64 and len(block) == 25_000 and np.abs(block).max() <= 1.0
        spec = np.abs(np.fft.fft(block))
        assert abs(np.fft.fftfreq(len(block), 1 / 250_000)[spec.argmax()] - 10_000) < 300      # a +10 kHz-es hang
    args = fake_rtl.calls()[0]
    assert args[args.index("-f") + 1] == "446006250" and args[args.index("-s") + 1] == "250000"
    assert args[args.index("-g") + 1] == "20" and args[args.index("-p") + 1] == "5" and args[args.index("-d") + 1] == "1"
    assert args[-1] == "-"


def test_automatic_gain_omits_the_gain_flag_and_retune_restarts_the_program(fake_rtl):
    dev = RtlSdrToolDevice(command=[str(fake_rtl.paths["rtl_sdr"])])
    with dev:
        dev.configure(137_100_000, 1_024_000, gain_db=None)
        dev.read(1000)
        dev.retune(137_100_500)
        assert len(dev.read(1000)) == 1000
    first, second = fake_rtl.calls()[:2]
    assert "-g" not in first and "-p" not in first
    assert second[second.index("-f") + 1] == "137100500"


def test_a_failing_program_gives_a_helpful_error(fake_rtl):
    fake_rtl.mode("fail")
    dev = RtlSdrToolDevice(command=[str(fake_rtl.paths["rtl_sdr"])])
    with dev:
        dev.configure(100_000_000, 250_000)
        with pytest.raises(SdrError) as exc:
            dev.read(1000)
    assert "usb_claim_interface" in str(exc.value) and "Zadig" in str(exc.value)


def test_missing_program_is_reported():
    dev = RtlSdrToolDevice(command=["definitely-not-an-rtl-sdr-program"])
    with pytest.raises(SdrError, match="Could not start rtl_sdr"):
        dev.configure(100_000_000, 250_000)


def test_probe_reports_a_missing_tool(tmp_path, monkeypatch):
    monkeypatch.setattr("satrx.tools.shutil.which", lambda name: None)
    monkeypatch.setattr("satrx.tools._rtl_locations", lambda name: [])
    s = Settings(data_dir=tmp_path)
    s.sdr = SdrConfig(backend="rtl_sdr")
    probe = probe_sdr(s)
    assert probe.status == "no_tool" and not probe.ok and not probe.simulated


def test_a_full_recording_runs_through_the_rtl_sdr_program(tmp_path, fake_rtl):
    with TestClient(_app(tmp_path, fake_rtl)) as client:
        r = client.post("/api/recorder/start", json={"center_freq_hz": 446_006_250, "sample_rate_hz": 250_000, "duration_s": 2,
                                                     "gain_db": 20, "bandwidth_hz": 12_500})
        assert r.status_code == 202, r.text
        _wait_idle(client)
        rec = client.get("/api/recordings").json()[0]
        assert rec["source"] == "rtl_sdr" and rec["size_bytes"] == 2 * 250_000 * 2
        base = tmp_path / "data" / "recordings" / rec["path"]
        info = sigmf.read_info(base)
        assert info.num_samples == 500_000 and info.extra["satrx:sdr_backend"] == "rtl_sdr"
        raw = np.fromfile(sigmf.data_path(base), dtype=np.uint8)
        assert abs(raw.astype(float).mean() - 127.5) < 3                                      # nem csupa nulla / zaj
        events = {e["code"]: e for e in client.get("/api/events").json()}
        assert events["recording_finished"]["details"]["samples"] == 500_000
    assert "-g" in fake_rtl.calls()[0]


def test_a_failed_program_becomes_a_failed_recording_with_an_error_event(tmp_path, fake_rtl):
    fake_rtl.mode("fail")
    with TestClient(_app(tmp_path, fake_rtl)) as client:
        client.post("/api/recorder/start", json={"center_freq_hz": 446_006_250, "sample_rate_hz": 250_000, "duration_s": 2})
        _wait_idle(client)
        failed = next(e for e in client.get("/api/events").json() if e["code"] == "recording_failed")
        assert failed["level"] == "error" and "rtl_sdr stopped" in failed["details"]["error"]


def test_rtl_fm_audio_goes_straight_to_the_gallery(tmp_path, fake_rtl):
    with TestClient(_app(tmp_path, fake_rtl)) as client:
        r = client.post("/api/recorder/start-fm", json={"center_freq_hz": 446_006_250, "duration_s": 2, "mode": "fm",
                                                         "bandwidth_hz": 12_500, "audio_rate_hz": 24_000, "gain_db": 20,
                                                         "squelch": 10, "label": "PMR 1"})
        assert r.status_code == 202, r.text
        _wait_idle(client)
        art = client.get("/api/artifacts", params={"kind": "audio"}).json()[0]
        assert art["meta"]["source"] == "rtl_fm" and art["meta"]["mode"] == "fm" and art["meta"]["bandwidth_hz"] == 12_500
        wav_path = tmp_path / "data" / "artifacts" / "rtl_fm"
        wavs = list(wav_path.glob("*.wav"))
        assert len(wavs) == 1 and "PMR-1" in wavs[0].name
        with wave.open(str(wavs[0])) as w:
            assert w.getframerate() == 24_000 and w.getnchannels() == 1 and w.getnframes() == 48_000
        assert client.get(f"/api/artifacts/{art['id']}/file").status_code == 200
        job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
        assert job["status"] == "done"
        codes = [e["code"] for e in client.get("/api/events").json()]
        assert "recording_started" in codes and "recording_finished" in codes
    args = next(a for a in fake_rtl.calls() if "-M" in a)
    assert args[args.index("-M") + 1] == "fm" and args[args.index("-s") + 1] == "12500"
    assert args[args.index("-l") + 1] == "10" and args[args.index("-g") + 1] == "20" and args[args.index("-r") + 1] == "24000"


def test_rtl_fm_is_refused_with_the_simulated_backend_or_a_missing_program(tmp_path, fake_rtl):
    with TestClient(_app(tmp_path / "a", fake_rtl, backend="mock")) as client:
        r = client.post("/api/recorder/start-fm", json={"center_freq_hz": 446_006_250, "duration_s": 2})
        assert r.status_code == 422 and "real RTL-SDR" in r.text
    with TestClient(_app(tmp_path / "b", fake_rtl, fm_path=str(tmp_path / "nope" / "rtl_fm"))) as client:
        r = client.post("/api/recorder/start-fm", json={"center_freq_hz": 446_006_250, "duration_s": 2})
        assert r.status_code == 422 and "rtl_fm" in r.text
