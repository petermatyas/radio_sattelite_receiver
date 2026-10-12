"""7. fázis: Meteor LRPT dekódolás a SatDump külső programmal (hamis satdump-pal)."""
import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from satrx.pipeline.base import NoResult, PipelineContext, ProcessorError, ToolMissingError
from satrx.processors.meteor_lrpt import MeteorLrpt, MeteorLrptParams
from satrx.recording import sigmf
from tests.test_scheduler import T0, _rule, env  # noqa: F401

SR = 1_024_000
SCRIPT = Path(__file__).with_name("fake_satdump.py")


@pytest.fixture
def fake_satdump(tmp_path, monkeypatch):
    """Futtatható `satdump` indító (Windows: .cmd, egyébként shell-szkript) a hamis programhoz."""
    if os.name == "nt":
        launcher = tmp_path / "bin" / "satdump.cmd"
        launcher.parent.mkdir()
        launcher.write_text(f'@echo off\r\n"{sys.executable}" "{SCRIPT}" %*\r\n')
    else:
        launcher = tmp_path / "bin" / "satdump"
        launcher.parent.mkdir()
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{SCRIPT}" "$@"\n')
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_SATDUMP_LOG", str(log))
    monkeypatch.delenv("FAKE_SATDUMP", raising=False)
    return SimpleNamespace(path=launcher, mode=lambda m: monkeypatch.setenv("FAKE_SATDUMP", m),
                           calls=lambda: [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else [])


def _iq(base: Path, fmt: str = "cu8", seconds: float = 0.5) -> Path:
    rng = np.random.default_rng(0)
    n = int(SR * seconds)
    with sigmf.SigMFWriter(base, fmt, SR, 137_900_000) as w:
        w.write((0.1 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))).astype(np.complex64))
    return base


def _ctx(tmp_path, tools, fmt="cu8", name="rec"):
    base = _iq(tmp_path / name, fmt)
    wd = tmp_path / "out"
    wd.mkdir(exist_ok=True)
    return PipelineContext(recording_id=1, job_id=1, norad_id=57166, base=base, info=sigmf.read_info(base),
                           orbit=None, workdir=wd, tools=tools)


def _run(ctx, **params):
    return MeteorLrpt().run(ctx, MeteorLrptParams(**params))


# ---- a hívás és a kimenet -----------------------------------------------------------------------------

def test_new_style_command_line_and_images(tmp_path, fake_satdump):
    ctx = _ctx(tmp_path, {"satdump": [str(fake_satdump.path)]})
    arts = _run(ctx)
    (call,) = fake_satdump.calls()
    assert call[:3] == ["pipeline", "meteor_m2-x_lrpt", "baseband"]
    assert call[3].endswith("rec.sigmf-data") and Path(call[3]).is_file()
    assert call[4] == str(ctx.workdir / "satdump_1")
    assert call[5:] == ["--samplerate", str(SR), "--baseband_format", "cu8"]

    # kompozit elöl, csatornák utána; a túl alacsony kép és a köztes fájlok kimaradnak
    names = [a.path.name for a in arts]
    assert names == ["MSU-MR-RGB-123.png", "MSU-MR-1.png", "MSU-MR-2.png", "MSU-MR-3.png"]
    assert {a.kind for a in arts} == {"image"}
    m = arts[0].meta
    assert m["product_kind"] == "composite" and m["instrument"] == "MSU-MR" and m["mode"] == "LRPT"
    assert m["bitrate"] == "72k" and m["cli_style"] == "new" and (m["width"], m["height"]) == (1568, 240)
    assert [a.meta["product_kind"] for a in arts[1:]] == ["channel"] * 3
    out = ctx.workdir / "satdump_1"
    assert not list(out.rglob("*.cadu")) and not list(out.rglob("*.soft"))      # köztes fájlok törölve
    assert (out / "dataset.json").exists()                                      # a metaadat megmarad


def test_keep_intermediate_and_extra_arguments(tmp_path, fake_satdump):
    ctx = _ctx(tmp_path, {"satdump": [str(fake_satdump.path)]})
    _run(ctx, keep_intermediate=True, fill_missing=True, extra_args=["--dc_block"], pipeline="meteor_m2-x_lrpt")
    assert list((ctx.workdir / "satdump_1").rglob("*.cadu"))
    assert fake_satdump.calls()[0][-2:] == ["--fill_missing", "--dc_block"]


@pytest.mark.parametrize("fmt,expected", [("cu8", "cu8"), ("ci16_le", "cs16"), ("cf32_le", "cf32")])
def test_baseband_format_follows_the_recording_format(tmp_path, fake_satdump, fmt, expected):
    _run(_ctx(tmp_path, {"satdump": [str(fake_satdump.path)]}, fmt=fmt))
    assert fake_satdump.calls()[0][fake_satdump.calls()[0].index("--baseband_format") + 1] == expected


def test_explicit_baseband_format_override(tmp_path, fake_satdump):
    _run(_ctx(tmp_path, {"satdump": [str(fake_satdump.path)]}), baseband_format="cs8")
    assert fake_satdump.calls()[0][-1] == "cs8"


# ---- a SatDump két parancssori stílusa ------------------------------------------------------------------

def test_auto_style_falls_back_to_the_legacy_command_line(tmp_path, fake_satdump):
    fake_satdump.mode("legacy_only")
    ctx = _ctx(tmp_path, {"satdump": [str(fake_satdump.path)]})
    arts = _run(ctx)
    calls = fake_satdump.calls()
    assert [c[0] for c in calls] == ["pipeline", "meteor_m2-x_lrpt"]                # előbb új, aztán régi stílus
    legacy = calls[1]
    assert legacy[1] == "baseband" and legacy[-1] == "w8"                           # a régi formátumnevek
    assert arts and arts[0].meta["cli_style"] == "legacy"
    assert not (ctx.workdir / "satdump_1").exists()                                 # a sikertelen kísérlet kimenete törölve


def test_auto_style_keeps_the_new_style_when_it_works(tmp_path, fake_satdump):
    fake_satdump.mode("new_only")
    arts = _run(_ctx(tmp_path, {"satdump": [str(fake_satdump.path)]}))
    assert [c[0] for c in fake_satdump.calls()] == ["pipeline"] and arts[0].meta["cli_style"] == "new"


def test_forced_legacy_style(tmp_path, fake_satdump):
    _run(_ctx(tmp_path, {"satdump": [str(fake_satdump.path)]}), cli_style="legacy")
    assert [c[0] for c in fake_satdump.calls()] == ["meteor_m2-x_lrpt"]


# ---- bitráta ----------------------------------------------------------------------------------------------

def test_auto_pipeline_tries_72k_then_80k(tmp_path, fake_satdump):
    fake_satdump.mode("only_80k")
    ctx = _ctx(tmp_path, {"satdump": [str(fake_satdump.path)]})
    arts = _run(ctx)
    pipelines = [c[1] for c in fake_satdump.calls()]
    assert pipelines == ["meteor_m2-x_lrpt", "meteor_m2-x_lrpt_80k"]
    assert arts[0].meta["bitrate"] == "80k" and arts[0].meta["satdump_pipeline"] == "meteor_m2-x_lrpt_80k"


def test_explicit_pipeline_is_not_retried(tmp_path, fake_satdump):
    fake_satdump.mode("only_80k")
    with pytest.raises(NoResult):
        _run(_ctx(tmp_path, {"satdump": [str(fake_satdump.path)]}), pipeline="meteor_m2-x_lrpt")
    assert len(fake_satdump.calls()) == 1


# ---- hibaágak -----------------------------------------------------------------------------------------------

def test_missing_satdump_is_a_tool_error_with_a_code(tmp_path):
    with pytest.raises(ToolMissingError) as exc:
        _run(_ctx(tmp_path, {"satdump": None}))
    assert exc.value.code == "tool_missing" and "Settings" in str(exc.value)


def test_no_decodable_signal_is_a_neutral_result(tmp_path, fake_satdump):
    fake_satdump.mode("none")
    with pytest.raises(NoResult, match="no LRPT images"):
        _run(_ctx(tmp_path, {"satdump": [str(fake_satdump.path)]}))
    assert len(fake_satdump.calls()) == 2                                           # 72k és 80k is próbálva


def test_crash_reports_exit_code_and_the_last_output(tmp_path, fake_satdump):
    fake_satdump.mode("crash")
    with pytest.raises(ProcessorError, match=r"exit code 3.*Segmentation fault") as exc:
        _run(_ctx(tmp_path, {"satdump": [str(fake_satdump.path)]}), cli_style="new", pipeline="meteor_m2-x_lrpt")
    assert not isinstance(exc.value, ToolMissingError)


def test_timeout_stops_the_process(tmp_path, fake_satdump):
    import time
    fake_satdump.mode("slow")
    t0 = time.time()
    with pytest.raises(ProcessorError, match="did not finish within 5 s"):
        _run(_ctx(tmp_path, {"satdump": [str(fake_satdump.path)]}), timeout_s=5, cli_style="new",
             pipeline="meteor_m2-x_lrpt")
    assert time.time() - t0 < 20


def test_unstartable_program_is_reported(tmp_path):
    bad = tmp_path / "not_a_program"
    bad.write_text("not executable")
    with pytest.raises(ProcessorError, match="Could not start SatDump"):
        _run(_ctx(tmp_path, {"satdump": [str(bad)]}), cli_style="new", pipeline="meteor_m2-x_lrpt")


def test_param_validation():
    with pytest.raises(ValueError):
        MeteorLrptParams(pipeline="meteor_m1")
    with pytest.raises(ValueError):
        MeteorLrptParams(timeout_s=1)


# ---- API: eszköz-beállítás, állapotellenőrzés, teljes lánc ----------------------------------------------------

def _store_iq(app, name, fmt="cu8"):
    from satrx.core.models import Recording
    base = _iq(app.state.settings.recordings_path / name, fmt)
    with app.state.session_factory() as s:
        rec = Recording(path=name, sample_format=fmt, center_freq_hz=137_900_000, sample_rate_hz=SR, norad_id=25544,
                        started_at=datetime.now(timezone.utc), ended_at=datetime.now(timezone.utc),
                        size_bytes=sigmf.data_path(base).stat().st_size)
        s.add(rec)
        s.commit()
        return rec.id


def _pipeline_id(client, name):
    return next(p["id"] for p in client.get("/api/pipelines").json() if p["name"] == name)


def test_tools_settings_api(env, fake_satdump):
    client, _ = env
    start = client.get("/api/settings/tools").json()
    assert start["satdump"]["found"] is False and start["satdump_path"] is None

    assert client.put("/api/settings/tools", json={"satdump_path": "/no/such/satdump"}).status_code == 422
    ok = client.put("/api/settings/tools", json={"satdump_path": str(fake_satdump.path)}).json()
    assert ok["satdump"]["found"] is True and ok["satdump"]["path"] == str(fake_satdump.path)
    assert ok["configured_from"] == "setting"
    assert client.get("/api/settings/tools").json()["satdump_path"] == str(fake_satdump.path)

    cleared = client.put("/api/settings/tools", json={"satdump_path": ""}).json()
    assert cleared["satdump_path"] is None and cleared["satdump"]["found"] is False


def test_health_warns_only_when_an_active_rule_needs_satdump(env, fake_satdump):
    client, _ = env
    config = lambda: next(c for c in client.get("/api/health/detailed").json()["components"] if c["id"] == "config")  # noqa: E731
    assert "satdump_missing" not in config()["details"].get("problems", [])           # a seed pipeline még nincs használatban
    _rule(client, pipeline_id=_pipeline_id(client, "Meteor LRPT"))
    assert "satdump_missing" in config()["details"]["problems"]
    client.put("/api/settings/tools", json={"satdump_path": str(fake_satdump.path)})
    assert "satdump_missing" not in config()["details"].get("problems", [])


def test_full_chain_through_the_seeded_pipeline(env, fake_satdump):
    client, app = env
    client.put("/api/settings/tools", json={"satdump_path": str(fake_satdump.path)})
    rec_id = _store_iq(app, "lrpt_ok")
    r = client.post(f"/api/recordings/{rec_id}/process", json={"pipeline_id": _pipeline_id(client, "Meteor LRPT")})
    assert r.status_code == 202, r.text
    assert app.state.executor.wait_idle(120)
    job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "done", [(s["status"], s["error"], s["log"][-300:]) for s in job["steps"]]
    assert "$ " in job["steps"][0]["log"] and "--baseband_format cu8" in job["steps"][0]["log"]
    assert len(job["artifacts"]) == 4 and all(a["has_thumbnail"] for a in job["artifacts"])
    assert job["artifacts"][0]["meta"]["product_kind"] in {"composite", "channel"}

    gallery = client.get("/api/artifacts", params={"kind": "image"}).json()
    assert {a["meta"]["mode"] for a in gallery} == {"LRPT"}
    png = client.get(f"/api/artifacts/{gallery[0]['id']}/file")
    assert png.headers["content-type"] == "image/png"

    rec = client.get("/api/recordings").json()[0]
    assert rec["deleted"] is True                                                  # keep_iq=False: siker után törölve
    assert not sigmf.data_path(app.state.settings.recordings_path / "lrpt_ok").exists()


def test_no_result_keeps_the_raw_recording_for_a_retry(env, fake_satdump):
    client, app = env
    fake_satdump.mode("none")
    client.put("/api/settings/tools", json={"satdump_path": str(fake_satdump.path)})
    rec_id = _store_iq(app, "lrpt_none")
    r = client.post(f"/api/recordings/{rec_id}/process", json={"pipeline_id": _pipeline_id(client, "Meteor LRPT")})
    assert app.state.executor.wait_idle(120)
    job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "no_result" and job["error"] is None
    assert client.get("/api/recordings").json()[0]["deleted"] is False             # az IQ megmaradt
    assert sigmf.data_path(app.state.settings.recordings_path / "lrpt_none").exists()


def test_missing_tool_raises_a_dedicated_event(env):
    client, app = env
    rec_id = _store_iq(app, "lrpt_notool")
    r = client.post(f"/api/recordings/{rec_id}/process", json={"pipeline_id": _pipeline_id(client, "Meteor LRPT")})
    assert app.state.executor.wait_idle(60)
    job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "failed" and "SatDump was not found" in job["error"]
    events = client.get("/api/events", params={"level": "error"}).json()
    assert any(e["code"] == "tool_missing" and e["job_id"] == job["id"] for e in events)
    assert client.get("/api/recordings").json()[0]["deleted"] is False


def test_satdump_path_from_the_yaml_config(tmp_path, fake_satdump):
    from fastapi.testclient import TestClient
    from satrx.api.app import create_app
    from tests.test_scheduler import _fetch, _settings

    s = _settings(tmp_path / "d")
    s.tools.satdump_path = str(fake_satdump.path)
    with TestClient(create_app(s, tle_fetcher=_fetch, startup_refresh=False, scheduler_enabled=False)) as client:
        info = client.get("/api/settings/tools").json()
        assert info["configured_from"] == "config" and info["satdump"]["found"] is True
