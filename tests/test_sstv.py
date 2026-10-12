"""6. fázis: SSTV dekóder és processzor."""
import io
import wave
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from satrx.dsp.filters import DspError
from satrx.dsp.sstv import decode_sstv, find_vis, FreqTrack
from satrx.dsp.sstv_modes import BY_NAME, BY_VIS, MODES
from satrx.pipeline.base import NoResult, PipelineContext, ProcessorError
from satrx.processors.sstv import Sstv, SstvParams
from satrx.recording import sigmf
from tests.sstv_synth import blurred_error, gradient_image, mean_abs_error, synth
from tests.test_scheduler import env  # noqa: F401

FS = 12_000


def _img(name):
    m = BY_NAME[name]
    return gradient_image(m.width, m.height), m


# ---- módtáblázat ---------------------------------------------------------------------------------

def test_mode_table_is_consistent():
    assert len({m.vis for m in MODES}) == len(MODES) and len({m.name for m in MODES}) == len(MODES)
    for m in MODES:
        assert m.last_end_ms <= m.period_ms + 1e-6, m.name          # a csatornák elférnek a sorperiódusban
        assert m.height % m.rows_per_period == 0
        assert BY_VIS[m.vis] is m
    # ismert teljes képidők (a szabvány szerint), másodpercben
    for name, secs in {"Robot36": 36, "Robot72": 72, "MartinM1": 114.3, "MartinM2": 58.1, "ScottieS1": 109.6,
                       "ScottieS2": 71.1, "PD50": 49.7, "PD90": 89.99, "PD120": 126.1, "PD180": 187.0,
                       "PD240": 248.0, "PD290": 288.7}.items():
        assert BY_NAME[name].duration_s == pytest.approx(secs, rel=0.01), name
    # VIS-kódok a pysstv-vel és a slowrx-szel egyezően
    assert (BY_NAME["PD180"].vis, BY_NAME["PD240"].vis, BY_NAME["Robot36"].vis, BY_NAME["PD120"].vis) == (96, 97, 8, 95)


# ---- dekódolás: minden mód ------------------------------------------------------------------------

@pytest.mark.parametrize("name", [m.name for m in MODES])
def test_roundtrip_every_mode(name):
    img, m = _img(name)
    out = decode_sstv(synth([(img, m)], FS), FS)
    assert len(out) == 1
    d = out[0]
    assert d.mode.name == name and d.vis_detected and d.image.size == (m.width, m.height)
    assert d.lines_decoded == m.height and d.sync_fraction > 0.95
    assert mean_abs_error(d.image, img) < 4.0
    assert abs(d.freq_offset_hz) < 5 and abs(d.drift_ppm) < 50


def test_sample_rate_independence():
    img, m = _img("Robot36")
    for fs in (8000, 22050, 48000, 51200):
        d = decode_sstv(synth([(img, m)], fs), fs)[0]
        assert mean_abs_error(d.image, img) < 4.0, fs


# ---- romlott jelek ------------------------------------------------------------------------------------

def test_noise_tolerance():
    img, m = _img("PD90")
    d = decode_sstv(synth([(img, m)], FS, noise=0.35, seed=3), FS)[0]      # kb. 10 dB SNR a sávban
    assert d.lines_decoded == m.height and d.sync_fraction > 0.9
    assert blurred_error(d.image, img) < 12            # a pixelenkénti zaj kiátlagolódik, a kép felismerhető marad
    clean = decode_sstv(synth([(img, m)], FS, noise=0.08, seed=3), FS)[0]
    assert mean_abs_error(clean.image, img) < 12


@pytest.mark.parametrize("offset", [-180.0, 90.0, 180.0])
def test_frequency_offset_is_estimated_and_removed(offset):
    img, m = _img("PD90")
    d = decode_sstv(synth([(img, m)], FS, offset_hz=offset), FS)[0]
    assert d.freq_offset_hz == pytest.approx(offset, abs=8)
    assert mean_abs_error(d.image, img) < 4.0


@pytest.mark.parametrize("ppm", [-1500.0, 600.0, 2500.0])
def test_clock_drift_is_corrected(ppm):
    """A hangkártya / Doppler okozta időskála-eltérés ferdülést okozna: a szinkronillesztés kiegyenlíti."""
    img, m = _img("PD120")
    d = decode_sstv(synth([(img, m)], FS, drift_ppm=ppm), FS)[0]
    assert d.lines_decoded == m.height
    assert d.drift_ppm == pytest.approx(-ppm, abs=250)      # gyorsabb óra = rövidebb sorperiódus
    assert mean_abs_error(d.image, img) < 6.0


def test_doppler_like_quadratic_drift():
    """A sebesség előjelet vált az átvonulás alatt: az időeltolódás másodfokú."""
    img, m = _img("PD120")
    clean = synth([(img, m)], FS)
    t = np.arange(len(clean), dtype=np.float64)
    warped = t * (1 + 2e-5) + 12.0 * (t / len(t)) ** 2            # lineáris + másodfokú tag (a végén ~1 ms)
    d = decode_sstv(np.interp(warped, t, clean).astype(np.float32), FS)[0]
    assert d.lines_decoded >= m.height - 4 and mean_abs_error(d.image, img, rows=d.lines_decoded) < 6.0


def test_without_vis_the_mode_is_inferred_from_sync_spacing():
    for name in ("Robot36", "MartinM1", "ScottieS1", "PD90"):
        img, m = _img(name)
        out = decode_sstv(synth([(img, m)], FS, with_vis=False), FS)
        assert len(out) == 1 and out[0].mode.name == name and out[0].vis_detected is False, name
        assert mean_abs_error(out[0].image, img) < 8.0, name


def test_recording_that_starts_in_the_middle_of_an_image():
    """ISS-átvonulásnál gyakori: a jel a kép közben kezdődik, a VIS-fejléc kimarad."""
    img, m = _img("PD120")
    audio = synth([(img, m)], FS)
    cut = int(len(audio) * 0.4)
    out = decode_sstv(audio[cut:], FS)
    assert len(out) == 1 and out[0].mode.name == "PD120" and not out[0].vis_detected
    assert 0.5 * m.height < out[0].lines_decoded <= m.height * 0.62
    # a dekódolt rész a kép megfelelő sávjával egyezik (a kép tetejét nem kapjuk meg: a vízszintes tartalom stimmel)
    rows = out[0].lines_decoded
    start_row = m.height - rows
    ref = img.resize((m.width, m.height))
    assert mean_abs_error(out[0].image, ref.crop((0, start_row, m.width, m.height))) < 12


def test_signal_lost_midway_gives_partial_image():
    img, m = _img("PD120")
    audio = synth([(img, m)], FS)
    out = decode_sstv(audio[: int(len(audio) * 0.55)], FS)
    assert len(out) == 1 and out[0].vis_detected
    assert 0.4 * m.height < out[0].lines_decoded < 0.6 * m.height
    assert mean_abs_error(out[0].image, img, rows=out[0].lines_decoded) < 4.0


def test_trailing_noise_after_the_image_does_not_add_lines():
    img, m = _img("PD90")
    audio = synth([(img, m)], FS)
    noise = np.random.default_rng(1).standard_normal(FS * 20).astype(np.float32) * 0.5
    out = decode_sstv(np.concatenate([audio, noise]), FS)
    assert len(out) == 1 and out[0].lines_decoded == m.height


def test_two_images_in_one_recording_with_different_modes():
    a, ma = _img("Robot36")
    b, mb = _img("MartinM2")
    out = decode_sstv(synth([(a, ma), (b, mb)], FS), FS)
    assert [d.mode.name for d in out] == ["Robot36", "MartinM2"]
    assert out[0].start_s < out[0].end_s <= out[1].start_s + 1
    assert mean_abs_error(out[0].image, a) < 4 and mean_abs_error(out[1].image, b) < 4


def test_forced_mode_overrides_the_vis_code():
    img, m = _img("PD90")
    out = decode_sstv(synth([(img, m)], FS), FS, mode="PD90")
    assert out and out[0].mode.name == "PD90"
    with pytest.raises(DspError):
        decode_sstv(synth([(img, m)], FS), FS, mode="NoSuchMode")


def test_pure_noise_and_plain_tones_give_no_image():
    rng = np.random.default_rng(5)
    assert decode_sstv(rng.standard_normal(FS * 60).astype(np.float32) * 0.3, FS) == []
    t = np.arange(FS * 30) / FS
    assert decode_sstv(np.sin(2 * np.pi * 1500 * t).astype(np.float32), FS) == []
    assert decode_sstv(np.sin(2 * np.pi * 1900 * t).astype(np.float32), FS) == []      # csak vezérhang, nincs VIS
    with pytest.raises(DspError):
        decode_sstv(np.zeros(100, dtype=np.float32), FS)


def test_vis_parity_error_is_rejected():
    img, m = _img("PD90")
    audio = synth([(img, m)], FS)
    ft = FreqTrack(audio, FS)
    assert [h.code for h in find_vis(ft)] == [m.vis]
    # egy bit átbillentése a paritást elrontja: a fejléc nem érvényes
    from tests.sstv_synth import vis_header
    from satrx.dsp.sstv_modes import FREQ_VIS_ONE, FREQ_VIS_ZERO
    bad = vis_header(FS, m.vis).copy()
    bit0 = int(0.3 * FS) + int(0.01 * FS) + int(0.3 * FS) + int(0.03 * FS)
    seg = slice(bit0, bit0 + int(0.03 * FS))
    bad[seg] = FREQ_VIS_ZERO if bad[bit0 + 5] == FREQ_VIS_ONE else FREQ_VIS_ONE
    broken = np.sin(2 * np.pi * np.cumsum(np.concatenate([np.full(FS // 2, 1500.0), bad, np.full(FS, 1500.0)])) / FS)
    assert find_vis(FreqTrack(broken.astype(np.float32), FS)) == []


def test_independent_encoder_pysstv_is_decoded():
    """Független megvalósítás (pysstv) kimenetét dekódoljuk: a módtáblázat nem csak a saját kódolómmal konzisztens."""
    color = pytest.importorskip("pysstv.color")
    for name, limit in (("Robot36", 6), ("MartinM1", 3), ("PD90", 4), ("PD120", 4)):
        cls = getattr(color, name)
        img = gradient_image(cls.WIDTH, cls.HEIGHT)
        audio = np.fromiter(cls(img, FS, 16).gen_samples(), dtype=np.float64)
        audio = (audio / 32768.0 if np.abs(audio).max() > 2 else audio).astype(np.float32)
        out = decode_sstv(audio, FS)
        assert len(out) == 1 and out[0].mode.name == name and out[0].lines_decoded == out[0].lines_total
        assert mean_abs_error(out[0].image, img) < limit, name


# ---- processzor -------------------------------------------------------------------------------------------

def _write_wav(path: Path, audio: np.ndarray, fs: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(fs)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())


def _ctx(tmp_path, audio=None, fs=FS):
    base = tmp_path / "rec"
    with sigmf.SigMFWriter(base, "cu8", 250_000, 145_800_000) as w:
        w.write(np.zeros(1000, dtype=np.complex64))
    wd = tmp_path / "out"
    wd.mkdir(exist_ok=True)
    ctx = PipelineContext(recording_id=1, job_id=1, norad_id=25544, base=base, info=sigmf.read_info(base),
                          orbit=None, workdir=wd)
    if audio is not None:
        wav = wd / "fm_demod_1.wav"
        _write_wav(wav, audio, fs)
        ctx.state["audio_path"] = wav
    return ctx


def test_processor_writes_png_artifacts_with_metadata(tmp_path):
    a, ma = _img("PD90")
    b, mb = _img("Robot36")
    ctx = _ctx(tmp_path, synth([(a, ma), (b, mb)], FS))
    arts = Sstv().run(ctx, SstvParams())
    assert [x.kind for x in arts] == ["image", "image"]
    assert [x.meta["mode"] for x in arts] == ["PD90", "Robot36"]
    first = arts[0]
    assert first.path.suffix == ".png" and first.path.name == "sstv_1.png" and arts[1].path.name == "sstv_2.png"
    assert Image.open(first.path).size == (320, 256)
    assert first.meta["lines_decoded"] == 256 and first.meta["complete"] is True and first.meta["vis_detected"] is True
    assert first.meta["sync_fraction"] > 0.95 and first.meta["width"] == 320 and first.meta["height"] == 256
    assert first.meta["image_start_s"] < arts[1].meta["image_start_s"]


def test_processor_partial_image_is_marked_incomplete(tmp_path):
    img, m = _img("PD120")
    audio = synth([(img, m)], FS)
    (art,) = Sstv().run(_ctx(tmp_path, audio[: int(len(audio) * 0.5)]), SstvParams())
    assert art.meta["complete"] is False and art.meta["lines_decoded"] < art.meta["lines_total"]


def test_processor_no_signal_is_not_an_error(tmp_path):
    noise = np.random.default_rng(2).standard_normal(FS * 30).astype(np.float32) * 0.3
    with pytest.raises(NoResult, match="No SSTV"):
        Sstv().run(_ctx(tmp_path, noise), SstvParams())


def test_processor_params_and_audio_requirements(tmp_path):
    with pytest.raises(ValueError):
        SstvParams(mode="Banana")
    with pytest.raises(ProcessorError, match="short"):
        Sstv().run(_ctx(tmp_path, np.zeros(100, dtype=np.float32)), SstvParams())
    img, m = _img("PD90")
    arts = Sstv().run(_ctx(tmp_path, synth([(img, m)], FS)), SstvParams(mode="PD90"))
    assert arts[0].meta["mode"] == "PD90"


# ---- teljes lánc: FM-modulált IQ -> demodulátor -> SSTV -> galéria ----------------------------------------

def _store_sstv_iq(app, name: str, audio: np.ndarray, audio_fs: int, sr: int = 250_000, carrier_hz: float = 8_000.0,
                   deviation_hz: float = 3_000.0, snr_noise: float = 0.0) -> int:
    """Az SSTV-hangot FM-modulálja egy IQ-felvételbe (cu8), és beírja az adatbázisba; a Recording azonosítóját adja."""
    from datetime import datetime, timezone
    from satrx.core.models import Recording

    up = sr // audio_fs
    base = app.state.settings.recordings_path / name
    rng = np.random.default_rng(4)
    phase = 0.0
    with sigmf.SigMFWriter(base, "cu8", sr, 145_800_000) as w:
        for a in range(0, len(audio), audio_fs * 4):
            chunk = np.repeat(audio[a: a + audio_fs * 4], up).astype(np.float64)
            inst = carrier_hz + deviation_hz * chunk
            ph = phase + 2 * np.pi * np.cumsum(inst) / sr
            phase = float(ph[-1] % (2 * np.pi))
            iq = 0.5 * np.exp(1j * ph)
            if snr_noise:
                iq = iq + snr_noise * (rng.standard_normal(len(iq)) + 1j * rng.standard_normal(len(iq)))
            w.write(iq.astype(np.complex64))
    with app.state.session_factory() as s:
        rec = Recording(path=name, sample_format="cu8", center_freq_hz=145_800_000, sample_rate_hz=sr,
                        started_at=datetime.now(timezone.utc), ended_at=datetime.now(timezone.utc),
                        size_bytes=sigmf.data_path(base).stat().st_size)
        s.add(rec)
        s.commit()
        return rec.id


def test_full_chain_iq_to_gallery_image(env):
    client, app = env
    img, m = _img("Robot36")
    rec_id = _store_sstv_iq(app, "sstv_iq", synth([(img, m)], 50_000, lead_s=1.0), 50_000)

    r = client.post(f"/api/recordings/{rec_id}/run", json={"steps": [
        {"processor": "fm_demod", "params": {"carrier_offset_hz": 8000, "deviation_hz": 3000, "audio_lowpass_hz": 3500}},
        {"processor": "sstv", "params": {}}]})
    assert r.status_code == 202, r.text
    assert app.state.executor.wait_idle(120)
    job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "done", job
    assert [(s["processor"], s["status"]) for s in job["steps"]] == [("fm_demod", "ok"), ("sstv", "ok")]
    assert "using the audio of the previous step" in job["steps"][1]["log"]          # a lépések állapota közös
    assert "internal NFM" not in job["steps"][1]["log"]

    images = [a for a in job["artifacts"] if a["kind"] == "image"]
    assert len(images) == 1 and images[0]["has_thumbnail"] is True
    meta = images[0]["meta"]
    assert meta["mode"] == "Robot36" and meta["vis_detected"] is True and meta["complete"] is True
    assert (meta["width"], meta["height"]) == (320, 240)

    png = client.get(f"/api/artifacts/{images[0]['id']}/file")
    assert png.headers["content-type"] == "image/png"
    decoded = Image.open(io.BytesIO(png.content))
    assert decoded.size == (320, 240) and mean_abs_error(decoded, img) < 8       # FM-láncon át is felismerhető
    assert client.get(f"/api/artifacts/{images[0]['id']}/thumbnail").headers["content-type"] == "image/jpeg"
    assert client.get("/api/artifacts", params={"kind": "image"}).json()[0]["meta"]["mode"] == "Robot36"


def test_full_chain_with_noisy_iq_and_the_seeded_pipeline(env):
    client, app = env
    img, m = _img("PD90")
    rec_id = _store_sstv_iq(app, "sstv_noisy", synth([(img, m)], 50_000, lead_s=2.0), 50_000, snr_noise=0.35)
    pipelines = {p["name"]: p for p in client.get("/api/pipelines").json()}
    steps = [dict(s) for s in pipelines["ISS SSTV"]["steps"]]
    steps[0]["params"] = {**steps[0]["params"], "carrier_offset_hz": 8000, "deviation_hz": 3000}
    r = client.post(f"/api/recordings/{rec_id}/run", json={"steps": steps})
    assert r.status_code == 202, r.text
    assert app.state.executor.wait_idle(240)
    job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "done", [(s["processor"], s["status"], s["log"]) for s in job["steps"]]
    meta = next(a for a in job["artifacts"] if a["kind"] == "image")["meta"]
    assert meta["mode"] == "PD90" and meta["lines_decoded"] == 256 and meta["sync_fraction"] > 0.9


def test_sstv_without_a_previous_demodulator_step_uses_the_internal_one(env):
    client, app = env
    img, m = _img("Robot36")
    rec_id = _store_sstv_iq(app, "sstv_solo", synth([(img, m)], 50_000), 50_000)
    r = client.post(f"/api/recordings/{rec_id}/run", json={"steps": [
        {"processor": "sstv", "params": {"offset_hz": 8000, "follow_doppler": False}}]})
    assert app.state.executor.wait_idle(120)
    job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "done" and job["artifacts"][0]["meta"]["mode"] == "Robot36"
    assert "internal NFM demodulator" in job["steps"][0]["log"]
    assert len(job["artifacts"]) == 1          # a belső demodulátor ideiglenes hangja nem kerül a galériába


def test_no_sstv_in_a_recording_is_a_neutral_result_not_an_error(env):
    client, app = env
    rng = np.random.default_rng(9)
    rec_id = _store_sstv_iq(app, "sstv_none", (rng.standard_normal(50_000 * 20) * 0.3).astype(np.float32), 50_000)
    r = client.post(f"/api/recordings/{rec_id}/run", json={"steps": [
        {"processor": "fm_demod", "params": {"carrier_offset_hz": 8000, "deviation_hz": 3000}}, {"processor": "sstv"}]})
    assert app.state.executor.wait_idle(120)
    job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "no_result" and job["error"] is None
    assert [s["status"] for s in job["steps"]] == ["ok", "no_result"]
    events = {e["code"]: e["level"] for e in client.get("/api/events").json()}
    assert events["no_result"] == "info" and "pipeline_failed" not in events
