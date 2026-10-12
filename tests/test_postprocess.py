"""5. fázis: waterfall és hang utólag a mentett felvételekből."""
import io
import wave
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from satrx.dsp import spectrum
from satrx.dsp.filters import DspError, plan_decimation
from satrx.pipeline.base import PipelineContext, ProcessorError
from satrx.processors.audio_demod import AudioDemod, AudioDemodParams
from satrx.processors.waterfall import Waterfall, WaterfallParams
from satrx.recording import sigmf
from tests.test_pipeline import F0, SR, _manual_recording, _run, _wav_peak_hz, papp  # noqa: F401
from tests.test_scheduler import env  # noqa: F401  (a papp fixture függősége)

CENTER = 100_000_000


def _write(base: Path, samples: np.ndarray, sr: int = SR, center: int = CENTER) -> Path:
    with sigmf.SigMFWriter(base, "cf32_le", sr, center) as w:
        w.write(samples.astype(np.complex64))
    return base


def _noise(n, sigma=0.01, seed=1):
    rng = np.random.default_rng(seed)
    return sigma * (rng.standard_normal(n) + 1j * rng.standard_normal(n))


def _ctx(base, tmp_path, orbit=None):
    wd = tmp_path / "out"
    wd.mkdir(exist_ok=True)
    return PipelineContext(recording_id=1, job_id=1, norad_id=None, base=base, info=sigmf.read_info(base),
                           orbit=orbit, workdir=wd)


def _peak_bin_freq(spec, row):
    cols = spec.db.shape[1]
    k = int(np.argmax(spec.db[row]))
    return spec.f_lo_hz + (k + 0.5) * (spec.f_hi_hz - spec.f_lo_hz) / cols


# ---- spektrogram -----------------------------------------------------------------------------

def test_spectrogram_finds_tone_and_axes(tmp_path):
    n = SR * 2
    t = np.arange(n) / SR
    base = _write(tmp_path / "tone", 0.5 * np.exp(2j * np.pi * 20_000 * t) + _noise(n))
    spec = spectrum.compute_spectrogram(base, fft_size=1024, max_rows=100)
    assert spec.ref_center_hz == CENTER
    assert spec.f_lo_hz == pytest.approx(-SR / 2) and spec.f_hi_hz == pytest.approx(SR / 2)
    assert spec.t0_s == 0 and spec.t1_s == pytest.approx(2.0, abs=0.02)
    assert 90 <= spec.rows <= 100
    assert all(abs(_peak_bin_freq(spec, r) - 20_000) < 400 for r in (0, spec.rows // 2, spec.rows - 1))
    # dBFS: egy 0,5 amplitúdójú komplex tónus kb. -6 dB
    assert -9 < spec.db[0].max() < -3


def test_spectrogram_time_axis_follows_a_chirp(tmp_path):
    n = SR * 4
    t = np.arange(n) / SR
    f_inst = -30_000 + 15_000 * t                       # -30 kHz -> +30 kHz
    base = _write(tmp_path / "chirp", 0.5 * np.exp(2j * np.pi * np.cumsum(f_inst) / SR) + _noise(n))
    spec = spectrum.compute_spectrogram(base, fft_size=1024, max_rows=80)
    first, last = _peak_bin_freq(spec, 2), _peak_bin_freq(spec, spec.rows - 3)
    assert first < -20_000 and last > 20_000
    freqs = [_peak_bin_freq(spec, r) for r in range(0, spec.rows, 8)]
    assert freqs == sorted(freqs)                         # a frekvencia az idővel monoton nő


def test_spectrogram_time_and_frequency_crop(tmp_path):
    n = SR * 4
    t = np.arange(n) / SR
    base = _write(tmp_path / "crop", 0.5 * np.exp(2j * np.pi * 20_000 * t) + _noise(n))
    spec = spectrum.compute_spectrogram(base, fft_size=1024, max_rows=50, start_s=1.0, duration_s=1.0,
                                        center_offset_hz=20_000, span_hz=20_000)
    assert spec.t0_s == pytest.approx(1.0) and spec.t1_s == pytest.approx(2.0, abs=0.02)
    assert spec.f_lo_hz == pytest.approx(10_000, abs=300) and spec.f_hi_hz == pytest.approx(30_000, abs=300)
    assert abs(_peak_bin_freq(spec, 5) - 20_000) < 300
    with pytest.raises(DspError):
        spectrum.compute_spectrogram(base, fft_size=1024, start_s=3.9999, duration_s=0.00001)


def test_spectrogram_compensates_retunes(tmp_path):
    """Doppler-követés: a közép-frekvencia változik, a jel abszolút frekvenciája nem."""
    half = SR * 2
    t = np.arange(half) / SR
    base = tmp_path / "retune"
    with sigmf.SigMFWriter(base, "cf32_le", SR, CENTER) as w:
        w.write((0.5 * np.exp(2j * np.pi * 20_000 * t) + _noise(half)).astype(np.complex64))
        w.mark_retune(CENTER + 5_000)                     # a közép 5 kHz-cel feljebb: a jel 15 kHz-re kerül
        w.write((0.5 * np.exp(2j * np.pi * 15_000 * t) + _noise(half, seed=2)).astype(np.complex64))
    spec = spectrum.compute_spectrogram(base, fft_size=1024, max_rows=40)
    top, bottom = _peak_bin_freq(spec, 3), _peak_bin_freq(spec, spec.rows - 3)
    assert abs(top - 20_000) < 400 and abs(bottom - 20_000) < 400


def test_max_width_pools_columns(tmp_path):
    n = SR
    t = np.arange(n) / SR
    base = _write(tmp_path / "pool", 0.5 * np.exp(2j * np.pi * 20_000 * t) + _noise(n))
    spec = spectrum.compute_spectrogram(base, fft_size=4096, max_rows=20, max_width=1024)
    assert spec.db.shape[1] == 1024 and spec.bin_hz == pytest.approx(SR / 1024, rel=0.01)
    assert abs(_peak_bin_freq(spec, 2) - 20_000) < 600    # a max-pooling megőrzi a keskeny jelet


def test_nice_ticks_and_colormaps():
    assert spectrum.nice_ticks(0, 10, 5) == [0, 2, 4, 6, 8, 10]
    assert spectrum.nice_ticks(-125_000, 125_000, 8)[0] == -100_000
    for name in spectrum.COLORMAPS:
        lut = spectrum.colormap_lut(name)
        assert lut.shape == (256, 3) and lut.dtype == np.uint8
    assert tuple(spectrum.colormap_lut("gray")[0]) == (0, 0, 0) and tuple(spectrum.colormap_lut("gray")[255]) == (255,) * 3


def test_render_geometry_overlay_and_validation(tmp_path):
    n = SR * 2
    t = np.arange(n) / SR
    base = _write(tmp_path / "r", 0.5 * np.exp(2j * np.pi * 20_000 * t) + _noise(n))
    spec = spectrum.compute_spectrogram(base, fft_size=1024, max_rows=100)
    curve = (np.array([0.0, 1.0, 2.0]), np.array([CENTER - 30_000.0, CENTER, CENTER + 30_000.0]))
    img, meta = spectrum.render_waterfall(spec, colormap="heat", doppler_curve=curve, title="test")
    assert (img.width, img.height) == (meta["width"], meta["height"])
    p = meta["plot"]
    assert p["x"] + p["w"] < img.width and p["y"] + p["h"] < img.height and p["w"] == spec.db.shape[1]
    assert meta["doppler_overlay"] is True and meta["f_lo_hz"] < 0 < meta["f_hi_hz"]
    assert meta["db_max"] - meta["db_min"] == pytest.approx(45.0)
    # a tónus (+20 kHz) helye a képen a meta geometriájából visszaszámolható
    x = p["x"] + (20_000 - meta["f_lo_hz"]) / (meta["f_hi_hz"] - meta["f_lo_hz"]) * p["w"]
    column = np.asarray(img.convert("L"))[p["y"] + p["h"] // 2, int(x) - 6:int(x) + 7]
    assert column.max() > np.asarray(img.convert("L"))[p["y"] + p["h"] // 2, p["x"] + 5:p["x"] + 30].max()
    with pytest.raises(DspError):
        spectrum.render_waterfall(spec, colormap="nope")
    with pytest.raises(DspError):
        spectrum.render_waterfall(spec, db_min=-10, db_max=-20)
    _, none = spectrum.render_waterfall(spec, doppler_curve=None)
    assert none["doppler_overlay"] is False


def test_waterfall_processor_writes_png_with_meta(tmp_path):
    n = SR * 2
    t = np.arange(n) / SR
    base = _write(tmp_path / "w", 0.5 * np.exp(2j * np.pi * 20_000 * t) + _noise(n))
    orbit = {"satellite": {"name": "TESTSAT"}, "track": {"t_s": [0, 2], "doppler_freq_hz": [CENTER + 1, CENTER + 2]}}
    (art,) = Waterfall().run(_ctx(base, tmp_path, orbit), WaterfallParams(colormap="jet", max_rows=200))
    assert art.kind == "waterfall" and art.path.suffix == ".png"
    with Image.open(art.path) as img:
        assert img.size == (art.meta["width"], art.meta["height"])
    assert art.meta["colormap"] == "jet" and art.meta["ref_center_hz"] == CENTER
    with pytest.raises(ValueError):
        WaterfallParams(fft_size=1000)
    with pytest.raises(ProcessorError):
        Waterfall().run(_ctx(base, tmp_path), WaterfallParams(start_s=1.99999, duration_s=0.00001))


# ---- hang-demodulátor -----------------------------------------------------------------------------

def test_plan_decimation():
    assert plan_decimation(250_000, 15_000) == (5, 1)         # NFM: közvetlenül ~50 kHz-re
    assert plan_decimation(250_000, 180_000) == (1, 5)        # WFM: az IF 250 kHz, majd hang-decimálás
    d1, da = plan_decimation(1_024_000, 180_000)
    assert 1_024_000 / d1 >= 216_000 and 20 <= d1 * da <= 22
    with pytest.raises(DspError):
        plan_decimation(100_000, 180_000)


def _demod(base, tmp_path, **kw):
    (art,) = AudioDemod().run(_ctx(base, tmp_path), AudioDemodParams(**kw))
    return art, _wav_peak_hz(art.path)


def test_audio_demod_nfm_offset_and_time_crop(tmp_path):
    n = SR * 3
    t = np.arange(n) / SR
    phase = 2 * np.pi * 10_000 * t + (3000 / 1000) * (-np.cos(2 * np.pi * 1000 * t))   # 1 kHz tónus, 3 kHz hub
    base = _write(tmp_path / "nfm", 0.5 * np.exp(1j * phase) + _noise(n))
    art, (peak, rate) = _demod(base, tmp_path, mode="nfm", offset_hz=10_000, follow_doppler=False)
    assert abs(peak - 1000) < 20 and rate == 50_000 and art.meta["mode"] == "nfm"
    assert art.meta["duration_s"] == pytest.approx(3.0, abs=0.05)

    art2, (peak2, _) = _demod(base, tmp_path, mode="nfm", offset_hz=10_000, follow_doppler=False,
                              start_s=1.0, duration_s=0.5)
    assert art2.meta["duration_s"] == pytest.approx(0.5, abs=0.03) and abs(peak2 - 1000) < 40
    assert art2.path != art.path                                       # nem írja felül az előző kimenetet


def test_audio_demod_wfm(tmp_path):
    n = SR * 2
    t = np.arange(n) / SR
    phase = 2 * np.pi * 0 * t + (50_000 / 1000) * (-np.cos(2 * np.pi * 1000 * t))      # 50 kHz hub, 1 kHz hang
    base = _write(tmp_path / "wfm", 0.5 * np.exp(1j * phase) + _noise(n))
    art, (peak, rate) = _demod(base, tmp_path, mode="wfm", follow_doppler=False)
    assert abs(peak - 1000) < 25 and rate == 50_000
    assert art.meta["bandwidth_hz"] == 180_000 and art.meta["rms_dbfs"] > -25


def test_audio_demod_am(tmp_path):
    n = SR * 2
    t = np.arange(n) / SR
    carrier = 0.5 * (1 + 0.6 * np.sin(2 * np.pi * 800 * t)) * np.exp(2j * np.pi * 20_000 * t)
    base = _write(tmp_path / "am", carrier + _noise(n))
    art, (peak, _) = _demod(base, tmp_path, mode="am", offset_hz=20_000, follow_doppler=False)
    assert abs(peak - 800) < 20 and art.meta["rms_dbfs"] > -30        # az AGC felhozza a hangerőt


@pytest.mark.parametrize("mode,sign,expect_audible", [("usb", +1, True), ("lsb", -1, True)])
def test_audio_demod_ssb_selects_the_right_sideband(tmp_path, mode, sign, expect_audible):
    n = SR * 2
    t = np.arange(n) / SR
    carrier_hz = 15_000
    tone = 0.4 * np.exp(2j * np.pi * (carrier_hz + sign * 1200) * t)                  # a vivőtől 1200 Hz-re
    base = _write(tmp_path / mode, tone + _noise(n))
    art, (peak, _) = _demod(base, tmp_path, mode=mode, offset_hz=carrier_hz, follow_doppler=False)
    assert abs(peak - 1200) < 25
    # a másik oldalsáv a szűrőn kívül marad: sokkal halkabb
    other = "lsb" if mode == "usb" else "usb"
    art_other, _ = _demod(base, tmp_path, mode=other, offset_hz=carrier_hz, follow_doppler=False, agc=False)
    art_same, _ = _demod(base, tmp_path, mode=mode, offset_hz=carrier_hz, follow_doppler=False, agc=False)
    assert art_same.meta["rms_dbfs"] - art_other.meta["rms_dbfs"] > 15


def test_audio_demod_follows_doppler_and_validates(tmp_path):
    n = SR * 2
    t = np.arange(n) / SR
    base = _write(tmp_path / "dop", 0.5 * np.exp(2j * np.pi * (10_000 * t + 3 * -np.cos(2 * np.pi * 1000 * t))) + _noise(n))
    orbit = {"track": {"t_s": [0.0, 5.0], "doppler_freq_hz": [CENTER + 10_000, CENTER + 10_000]}}
    (art,) = AudioDemod().run(_ctx(base, tmp_path, orbit), AudioDemodParams(mode="nfm"))
    assert art.meta["doppler_followed"] is True and abs(_wav_peak_hz(art.path)[0] - 1000) < 20
    with pytest.raises(ProcessorError, match="empty"):
        AudioDemod().run(_ctx(base, tmp_path), AudioDemodParams(start_s=3.0))
    low = _write(tmp_path / "low", _noise(50_000, seed=3), sr=100_000)
    with pytest.raises(ProcessorError):
        AudioDemod().run(_ctx(low, tmp_path), AudioDemodParams(mode="wfm"))


# ---- API: ad-hoc jobok és a felvétel részletei ----------------------------------------------------------

def test_api_run_waterfall_and_audio_on_stored_recording(papp):
    client, app = papp
    rec_id = _manual_recording(client, app)

    wf = _run_steps(client, app, rec_id, [{"processor": "waterfall", "params": {"fft_size": 512, "colormap": "heat"}}])
    assert wf["status"] == "done" and wf["pipeline_id"] is None and wf["kind"] == "processing"
    (art,) = wf["artifacts"]
    assert art["kind"] == "waterfall" and art["has_thumbnail"] and art["meta"]["colormap"] == "heat"
    png = client.get(f"/api/artifacts/{art['id']}/file")
    assert png.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(png.content)).size == (art["meta"]["width"], art["meta"]["height"])

    au = _run_steps(client, app, rec_id, [{"processor": "audio_demod",
                                           "params": {"mode": "nfm", "offset_hz": 10_000, "follow_doppler": False,
                                                      "start_s": 0.5, "duration_s": 1.0}}])
    assert au["status"] == "done" and au["artifacts"][0]["kind"] == "audio"
    assert au["artifacts"][0]["meta"]["duration_s"] == pytest.approx(1.0, abs=0.05)
    with wave.open(io.BytesIO(client.get(f"/api/artifacts/{au['artifacts'][0]['id']}/file").content)) as w:
        assert w.getframerate() == 50_000

    detail = client.get(f"/api/recordings/{rec_id}").json()
    assert detail["iq_available"] is True and detail["info"]["duration_s"] == pytest.approx(2.0)
    assert detail["info"]["sample_rate_hz"] == SR and detail["info"]["center_freq_hz"] == F0
    assert {a["kind"] for a in detail["artifacts"]} == {"waterfall", "audio"}
    assert {j["id"] for j in detail["jobs"]} >= {wf["id"], au["id"]}
    assert detail["recording"]["id"] == rec_id and detail["orbit"]["radio"]["sample_rate_hz"] == SR

    retry = client.post(f"/api/jobs/{wf['id']}/retry")                  # az ad-hoc job is újrafuttatható
    assert retry.status_code == 202 and retry.json()["job_id"] not in (wf["id"], au["id"])
    app.state.executor.wait_idle(30)
    again = client.get(f"/api/jobs/{retry.json()['job_id']}").json()
    assert again["status"] == "done" and again["steps"][0]["params"]["colormap"] == "heat"


def test_api_run_validation_and_errors(papp):
    client, app = papp
    rec_id = _manual_recording(client, app)
    url = f"/api/recordings/{rec_id}/run"
    assert client.post(url, json={"steps": [{"processor": "nope"}]}).status_code == 422
    assert client.post(url, json={"steps": [{"processor": "waterfall", "params": {"fft_size": 1000}}]}).status_code == 422
    assert client.post(url, json={"steps": [{"processor": "waterfall", "params": {"colormap": "rainbow"}}]}).status_code == 422
    assert client.post(url, json={"steps": []}).status_code == 409
    assert client.post("/api/recordings/999/run", json={"steps": [{"processor": "waterfall"}]}).status_code == 409
    assert client.get("/api/recordings/999").status_code == 404

    bad = _run_steps(client, app, rec_id, [{"processor": "waterfall", "params": {"start_s": 50}}])
    assert bad["status"] == "failed" and "too short" in bad["error"]


def test_recording_detail_after_iq_removed(papp):
    client, app = papp
    rec_id = _manual_recording(client, app)
    pid = client.post("/api/pipelines", json={"name": "wf-clean", "mode": "t", "keep_iq": False,
                                              "steps": [{"processor": "waterfall", "params": {}}]}).json()["id"]
    _run(client, app, rec_id, pid)
    detail = client.get(f"/api/recordings/{rec_id}").json()
    assert detail["iq_available"] is False and detail["info"] is None and detail["orbit"] is not None
    assert client.post(f"/api/recordings/{rec_id}/run", json={"steps": [{"processor": "waterfall"}]}).status_code == 409


def test_seeded_fm_waterfall_pipeline_produces_both_results(papp):
    client, app = papp
    pid = next(p["id"] for p in client.get("/api/pipelines").json() if p["name"] == "FM audio + waterfall")
    job = _run(client, app, _manual_recording(client, app), pid)
    assert job["status"] == "done" and [s["processor"] for s in job["steps"]] == ["fm_demod", "waterfall"]
    assert {a["kind"] for a in job["artifacts"]} == {"audio", "waterfall"}


def _run_steps(client, app, rec_id, steps):
    r = client.post(f"/api/recordings/{rec_id}/run", json={"steps": steps})
    assert r.status_code == 202, r.text
    assert app.state.executor.wait_idle(60)
    return client.get(f"/api/jobs/{r.json()['job_id']}").json()
