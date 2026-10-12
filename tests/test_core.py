from datetime import datetime, timezone

from fastapi.testclient import TestClient

from satrx.api.app import create_app
from satrx.core.config import Settings, load_settings
from satrx.core.db import init_db, make_engine, make_session_factory
from satrx.core.models import Pipeline, RadioProfile, Satellite, WatchRule


def test_config_layering(tmp_path, monkeypatch):
    (tmp_path / "default.yaml").write_text("server:\n  port: 8000\nscheduling:\n  buffer_before_s: 60\n")
    (tmp_path / "local.yaml").write_text("server:\n  port: 9000\n")
    s = load_settings(tmp_path)
    assert s.server.port == 9000
    assert s.scheduling.buffer_before_s == 60

    monkeypatch.setenv("SATRX_SERVER__PORT", "9100")
    assert load_settings(tmp_path).server.port == 9100


def test_db_roundtrip():
    engine = make_engine(":memory:")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        s.add(Satellite(norad_id=25544, name="ISS"))
        s.add(RadioProfile(id=1, name="iss-fm", center_freq_hz=145_800_000, bandwidth_hz=25_000, sample_rate_hz=250_000))
        s.add(Pipeline(id=1, name="fm", mode="fm", steps=[{"processor": "fm_demod", "params": {}}]))
        s.add(
            WatchRule(
                norad_id=25544,
                valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
                valid_to=datetime(2026, 2, 1, tzinfo=timezone.utc),
                profile_id=1,
                pipeline_id=1,
                priority=5,
            )
        )
        s.commit()
    with factory() as s:
        rule = s.query(WatchRule).one()
        assert rule.satellite.name == "ISS"
        assert s.get(Pipeline, 1).steps[0]["processor"] == "fm_demod"


def _settings(tmp_path, token=None) -> Settings:
    s = Settings(data_dir=tmp_path)
    s.server.auth_token = token
    return s


def test_health_and_status(tmp_path):
    with TestClient(create_app(_settings(tmp_path), startup_refresh=False)) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        assert client.get("/api/status").json()["observer_set"] is False


def test_auth_token(tmp_path):
    with TestClient(create_app(_settings(tmp_path, token="secret"), startup_refresh=False)) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/status").status_code == 401
        assert client.get("/api/status", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_migrations_match_models(tmp_path):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from satrx.core.db import run_migrations
    from satrx.core.models import Base

    db = tmp_path / "m.db"
    run_migrations(db)
    engine = make_engine(db)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []
