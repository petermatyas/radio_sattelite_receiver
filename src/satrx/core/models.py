from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, TypeDecorator,
                        UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """SQLite nem tárol időzónát: mindig UTC-ként mentünk, és tz-aware UTC-ként olvasunk vissza."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[dict[str, Any]]: JSON}


class Satellite(Base):
    __tablename__ = "satellites"

    norad_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(120), default="")
    tle_line1: Mapped[str | None] = mapped_column(String(80))
    tle_line2: Mapped[str | None] = mapped_column(String(80))
    tle_epoch: Mapped[datetime | None] = mapped_column(UTCDateTime())
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    default_profile_id: Mapped[int | None] = mapped_column(ForeignKey("radio_profiles.id", ondelete="SET NULL"))  # a műhold frekvenciája
    default_pipeline_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipelines.id", ondelete="SET NULL", name="fk_satellites_default_pipeline_id"))

    rules: Mapped[list[WatchRule]] = relationship(back_populates="satellite", cascade="all, delete-orphan")


class SatelliteChannel(Base):
    """Egy műhold egy frekvenciája és módja; a mögötte lévő profilt és pipeline-t a rendszer tartja karban."""

    __tablename__ = "satellite_channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    norad_id: Mapped[int] = mapped_column(ForeignKey("satellites.norad_id", ondelete="CASCADE", name="fk_satellite_channels_norad_id"), index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    mode: Mapped[str] = mapped_column(String(16), default="fm")  # fm | sstv | lrpt | record
    center_freq_hz: Mapped[int] = mapped_column(Integer)
    bandwidth_hz: Mapped[int] = mapped_column(Integer)
    sample_rate_hz: Mapped[int] = mapped_column(Integer)
    gain_db: Mapped[float | None] = mapped_column(Float)
    doppler: Mapped[bool] = mapped_column(Boolean, default=True)
    keep_iq: Mapped[bool] = mapped_column(Boolean, default=True)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # a módhoz tartozó beállítások
    profile_id: Mapped[int | None] = mapped_column(Integer)      # a karbantartott rádióprofil és pipeline
    pipeline_id: Mapped[int | None] = mapped_column(Integer)


class Antenna(Base):
    __tablename__ = "antennas"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    freq_min_hz: Mapped[int] = mapped_column(Integer, default=0)
    freq_max_hz: Mapped[int] = mapped_column(Integer, default=6_000_000_000)
    switch_port: Mapped[str | None] = mapped_column(String(40))
    notes: Mapped[str] = mapped_column(Text, default="")


class RadioProfile(Base):
    __tablename__ = "radio_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    center_freq_hz: Mapped[int] = mapped_column(Integer)
    bandwidth_hz: Mapped[int] = mapped_column(Integer)
    sample_rate_hz: Mapped[int] = mapped_column(Integer)
    gain_db: Mapped[float | None] = mapped_column(Float)  # None = AGC
    modulation_hint: Mapped[str] = mapped_column(String(20), default="")
    doppler_correction: Mapped[bool] = mapped_column(Boolean, default=True)
    antenna_id: Mapped[int | None] = mapped_column(ForeignKey("antennas.id", ondelete="SET NULL"))


class Pipeline(Base):
    __tablename__ = "pipelines"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    mode: Mapped[str] = mapped_column(String(40))  # fm | sstv | lrpt | ...
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # [{processor, params}]
    keep_iq: Mapped[bool] = mapped_column(Boolean, default=True)


class WatchRule(Base):
    __tablename__ = "watch_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    norad_id: Mapped[int] = mapped_column(ForeignKey("satellites.norad_id", ondelete="CASCADE"))
    valid_from: Mapped[datetime] = mapped_column(UTCDateTime())
    valid_to: Mapped[datetime] = mapped_column(UTCDateTime())
    min_elevation_deg: Mapped[float | None] = mapped_column(Float)  # None = globális alapérték
    priority: Mapped[int] = mapped_column(Integer, default=0)
    profile_id: Mapped[int] = mapped_column(ForeignKey("radio_profiles.id"))
    pipeline_id: Mapped[int] = mapped_column(ForeignKey("pipelines.id"))
    allow_partial: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    satellite: Mapped[Satellite] = relationship(back_populates="rules")


class TimedEvent(Base):
    """Időalapú (műholdtól független) esemény: egy adott időablak felvétele, egyszer vagy ismétlődően.

    Nincs NORAD ID és pályaszámítás: a felvétel az ablak idejére a profil frekvenciáján fut (pl. földi jeladó, APRS,
    meteorszórás, rádiócsillagászat). Az ismétlődés a helyi falióra szerint értendő (az időzóna a nyári időszámítást követi).
    """

    __tablename__ = "timed_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    start_at: Mapped[datetime] = mapped_column(UTCDateTime())          # az első előfordulás kezdete
    duration_s: Mapped[int] = mapped_column(Integer)
    recurrence: Mapped[str] = mapped_column(String(10), default="none")  # none | daily | weekly
    weekdays: Mapped[list[int]] = mapped_column(JSON, default=list)       # weekly: 0 = hétfő ... 6 = vasárnap
    repeat_until: Mapped[datetime | None] = mapped_column(UTCDateTime())  # None: határozatlan ideig
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    profile_id: Mapped[int] = mapped_column(ForeignKey("radio_profiles.id"))
    pipeline_id: Mapped[int] = mapped_column(ForeignKey("pipelines.id"))
    priority: Mapped[int] = mapped_column(Integer, default=0)
    allow_partial: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str] = mapped_column(Text, default="")


class Pass(Base):
    __tablename__ = "passes"

    id: Mapped[int] = mapped_column(primary_key=True)
    norad_id: Mapped[int | None] = mapped_column(ForeignKey("satellites.norad_id", ondelete="CASCADE"))  # None: időalapú esemény
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("watch_rules.id", ondelete="SET NULL"))
    event_id: Mapped[int | None] = mapped_column(ForeignKey("timed_events.id", ondelete="SET NULL"))
    label: Mapped[str | None] = mapped_column(String(120))  # időalapú eseménynél az esemény neve
    aos: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    tca: Mapped[datetime] = mapped_column(UTCDateTime())
    los: Mapped[datetime] = mapped_column(UTCDateTime())
    max_elevation_deg: Mapped[float] = mapped_column(Float)
    rec_start: Mapped[datetime | None] = mapped_column(UTCDateTime())  # tervezett felvételi ablak
    rec_end: Mapped[datetime | None] = mapped_column(UTCDateTime())
    device: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24), default="planned")
    # planned | partial | skipped_conflict | unsupported | recording | processing | done | failed | missed


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    pass_id: Mapped[int | None] = mapped_column(ForeignKey("passes.id", ondelete="SET NULL"))
    pipeline_id: Mapped[int | None] = mapped_column(ForeignKey("pipelines.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(16), default="recording")  # recording | processing
    recording_id: Mapped[int | None] = mapped_column(ForeignKey("recordings.id", ondelete="CASCADE"))
    device: Mapped[str] = mapped_column(String(80), default="")  # SDR erőforrás azonosító
    status: Mapped[str] = mapped_column(String(24), default="pending")
    # recording: running | done | failed | stopped; processing: queued | running | done | no_result | failed
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error: Mapped[str | None] = mapped_column(Text)
    log: Mapped[str] = mapped_column(Text, default="")


class Recording(Base):
    __tablename__ = "recordings"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    norad_id: Mapped[int | None] = mapped_column(ForeignKey("satellites.norad_id", ondelete="SET NULL"))
    path: Mapped[str] = mapped_column(String(500))  # SigMF alapnév (.sigmf-data / .sigmf-meta)
    sample_format: Mapped[str] = mapped_column(String(10), default="cu8")
    center_freq_hz: Mapped[int] = mapped_column(Integer)
    sample_rate_hz: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)  # retenció törölte az IQ-t
    orbit_meta_path: Mapped[str | None] = mapped_column(String(500))  # .orbit.json, a recordings mappához képest
    root_id: Mapped[int | None] = mapped_column(ForeignKey("storage_roots.id", ondelete="SET NULL"))  # None = alapgyökér
    protected: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")  # a megőrzési szabály nem törli
    label: Mapped[str | None] = mapped_column(String(120))  # időalapú esemény neve (műhold nélküli felvételnél)
    source: Mapped[str | None] = mapped_column(String(16))  # az SDR-backend: mock | replay | rtlsdr | soapy (régi sornál None)


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    recording_id: Mapped[int | None] = mapped_column(ForeignKey("recordings.id", ondelete="CASCADE"))
    norad_id: Mapped[int | None] = mapped_column(ForeignKey("satellites.norad_id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(20))  # image | audio | waterfall | text
    path: Mapped[str] = mapped_column(String(500))
    thumbnail_path: Mapped[str | None] = mapped_column(String(500))
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    root_id: Mapped[int | None] = mapped_column(ForeignKey("storage_roots.id", ondelete="SET NULL"))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # fájl + bélyegkép


class Setting(Base):
    """Futásidőben a UI-ról módosítható beállítások (felülírják a YAML-t)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)


class JobStep(Base):
    """Egy pipeline-lépés állapota egy feldolgozó jobon belül."""

    __tablename__ = "job_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    idx: Mapped[int] = mapped_column(Integer)
    processor: Mapped[str] = mapped_column(String(80))
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    # pending | running | ok | no_result | failed | skipped
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error: Mapped[str | None] = mapped_column(Text)
    log: Mapped[str] = mapped_column(Text, default="")


class Event(Base):
    """Eseménynapló: a felületen megjelenő hibák, figyelmeztetések és fontos állapotváltások."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(10))  # info | warning | error
    source: Mapped[str] = mapped_column(String(20))  # scheduler | recorder | pipeline | tle | storage | system
    code: Mapped[str] = mapped_column(String(60))
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    pass_id: Mapped[int | None] = mapped_column(ForeignKey("passes.id", ondelete="SET NULL"))
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    recording_id: Mapped[int | None] = mapped_column(ForeignKey("recordings.id", ondelete="SET NULL"))
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)


class StorageRoot(Base):
    """Egy mentési gyökérmappa. Adatfajtánként (recordings | artifacts) pontosan egy aktív: oda kerül az új adat;
    a régi adat a saját gyökerében marad (vagy az áthelyezés job viszi át)."""

    __tablename__ = "storage_roots"
    __table_args__ = (UniqueConstraint("kind", "path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    path: Mapped[str] = mapped_column(String(500))
    label: Mapped[str] = mapped_column(String(80), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
