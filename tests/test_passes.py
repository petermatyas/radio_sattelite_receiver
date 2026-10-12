from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from satrx.api.app import create_app
from satrx.core.config import Settings
from satrx.passes.predictor import Observer, predict_passes
from satrx.tle.client import Tle, TleError, parse_tle

ISS_TEXT = """ISS (ZARYA)
1 25544U 98067A   26284.82161096  .00007651  00000+0  14771-3 0  9996
2 25544  51.6314  82.9019 0006707 250.7442 109.2821 15.48831270589830
"""
BUDAPEST = Observer(47.4979, 19.0402, 100)
START = datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_tle():
    tle = parse_tle(ISS_TEXT, 25544)
    assert tle.name == "ISS (ZARYA)"
    assert tle.norad_id == 25544
    assert tle.epoch.date().isoformat() == "2026-10-11"
    with pytest.raises(TleError):
        parse_tle("No GP data found", 25544)
    with pytest.raises(TleError):
        parse_tle(ISS_TEXT, 12345)


def test_predict_passes_iss():
    tle = parse_tle(ISS_TEXT, 25544)
    passes = predict_passes(tle.line1, tle.line2, BUDAPEST, START, START + timedelta(days=2), 10.0)
    assert len(passes) >= 3
    for p in passes:
        assert p.aos < p.tca < p.los
        assert p.max_elevation_deg >= 10.0 - 0.01
        assert (p.los - p.aos) < timedelta(minutes=15)
    assert passes == sorted(passes, key=lambda p: p.aos)
    assert all(a.los < b.aos for a, b in zip(passes, passes[1:]))


def test_higher_min_elevation_gives_subset():
    tle = parse_tle(ISS_TEXT, 25544)
    end = START + timedelta(days=3)
    low = predict_passes(tle.line1, tle.line2, BUDAPEST, START, end, 5.0)
    high = predict_passes(tle.line1, tle.line2, BUDAPEST, START, end, 40.0)
    assert len(high) < len(low)
    assert all(p.max_elevation_deg >= 40 - 0.01 for p in high)


def test_pass_in_progress_at_slice_start_is_kept():
    tle = parse_tle(ISS_TEXT, 25544)
    full = predict_passes(tle.line1, tle.line2, BUDAPEST, START, START + timedelta(days=1), 10.0)[0]
    mid = full.tca
    cut = predict_passes(tle.line1, tle.line2, BUDAPEST, mid, mid + timedelta(hours=1), 10.0)
    assert cut[0].aos == mid
    assert abs((cut[0].los - full.los).total_seconds()) < 2


def _fake_fetcher(norad_id: int) -> Tle:
    if norad_id != 25544:
        raise TleError("No GP data found")
    return parse_tle(ISS_TEXT, norad_id)


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path), tle_fetcher=_fake_fetcher, startup_refresh=False)
    with TestClient(app) as c:
        yield c


def test_api_flow(client):
    assert client.get("/api/settings/observer").json() is None
    assert client.post("/api/passes/recompute").status_code == 409

    r = client.post("/api/satellites", json={"norad_id": 25544})
    assert r.status_code == 201 and r.json()["name"] == "ISS (ZARYA)"
    assert client.post("/api/satellites", json={"norad_id": 25544}).status_code == 409
    assert client.post("/api/satellites", json={"norad_id": 1}).status_code == 502

    assert client.put("/api/settings/observer", json={"lat": 47.5, "lon": 19.04, "alt_m": 100}).status_code == 200
    assert client.get("/api/status").json()["observer_set"] is True

    # átvonulás csak szabályból lesz; a seed adatok: profile 1, pipeline 1
    now = datetime.now(timezone.utc)
    rule = {"norad_id": 25544, "valid_from": now.isoformat(), "valid_to": (now + timedelta(days=5)).isoformat(),
            "profile_id": 1, "pipeline_id": 1}
    assert client.post("/api/rules", json=rule).status_code == 201
    passes = client.get("/api/passes", params={"hours": 48}).json()
    assert passes and passes[0]["satellite_name"] == "ISS (ZARYA)"

    assert client.patch("/api/satellites/25544", json={"enabled": False}).json()["enabled"] is False
    assert client.get("/api/passes").json() == []
    assert client.delete("/api/satellites/25544").status_code == 204
    assert client.get("/api/satellites").json() == []


def test_observer_validation(client):
    assert client.put("/api/settings/observer", json={"lat": 123, "lon": 0}).status_code == 422
