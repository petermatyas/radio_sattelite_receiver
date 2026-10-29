from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = PROJECT_ROOT / "config"


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    auth_token: str | None = None


class ObserverConfig(BaseModel):
    lat: float | None = None
    lon: float | None = None
    alt_m: float | None = None

    @property
    def is_set(self) -> bool:
        return self.lat is not None and self.lon is not None


class SchedulingConfig(BaseModel):
    default_min_elevation_deg: float = 10
    buffer_before_s: int = 60
    buffer_after_s: int = 60
    allow_partial_recording: bool = True
    min_partial_s: int = 60         # ennél rövidebb részleges felvételt nem érdemes indítani
    prep_s: int = 30                # ennyivel az ablak kezdete előtt indul az SDR előkészítése
    horizon_hours: int = 48
    retune_step_hz: int = 500       # Doppler-követés: ekkora eltérésnél hangol újra


class StorageConfig(BaseModel):
    quota_gb: float = 50
    iq_retention_days: int = 7
    artifact_retention_days: float = 0   # 0 = az eredményeket (képek, hangok) nem törli automatikusan


class SdrConfig(BaseModel):
    backend: Literal["mock", "replay", "rtlsdr", "rtl_sdr", "soapy"] = "mock"
    device_args: dict[str, Any] = Field(default_factory=dict)


class ToolsConfig(BaseModel):
    satdump_path: str | None = None
    rtl_sdr_path: str | None = None   # az rtl_sdr program (backend: rtl_sdr); üres = PATH
    rtl_fm_path: str | None = None    # az rtl_fm program (közvetlen hangfelvétel); üres = PATH


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SATRX_", env_nested_delimiter="__")

    data_dir: Path = Path("data")
    server: ServerConfig = Field(default_factory=ServerConfig)
    observer: ObserverConfig = Field(default_factory=ObserverConfig)
    scheduling: SchedulingConfig = Field(default_factory=SchedulingConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    sdr: SdrConfig = Field(default_factory=SdrConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, *args, **kwargs):
        # a környezeti változók felülírják a YAML-ból érkező (init) értékeket
        return env_settings, init_settings

    @property
    def data_path(self) -> Path:
        return self.data_dir if self.data_dir.is_absolute() else PROJECT_ROOT / self.data_dir

    @property
    def db_path(self) -> Path:
        return self.data_path / "satrx.db"

    @property
    def recordings_path(self) -> Path:
        return self.data_path / "recordings"

    @property
    def artifacts_path(self) -> Path:
        return self.data_path / "artifacts"

    @property
    def tle_cache_path(self) -> Path:
        return self.data_path / "tle"

    def ensure_dirs(self) -> None:
        for p in (self.data_path, self.recordings_path, self.artifacts_path, self.tle_cache_path):
            p.mkdir(parents=True, exist_ok=True)


def _deep_merge(base: dict, override: dict) -> dict:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings(config_dir: Path | None = None) -> Settings:
    """default.yaml < local.yaml < SATRX_* környezeti változók."""
    config_dir = config_dir or CONFIG_DIR
    merged = _deep_merge(_load_yaml(config_dir / "default.yaml"), _load_yaml(config_dir / "local.yaml"))
    return Settings(**merged)


@lru_cache
def get_settings() -> Settings:
    return load_settings()
