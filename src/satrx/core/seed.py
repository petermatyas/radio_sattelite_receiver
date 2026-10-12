from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from satrx.core.models import Pipeline, RadioProfile


def seed_defaults(session: Session) -> None:
    """Első indításkor alap profilok és a 'csak felvétel' pipeline (a további pipeline-ok a 4+. fázisban jönnek)."""
    existing = set(session.scalars(select(Pipeline.name)))
    if "Record only" not in existing:
        session.add(Pipeline(name="Record only", mode="record", steps=[], keep_iq=True))
    if "FM audio" not in existing:
        session.add(Pipeline(name="FM audio", mode="fm", keep_iq=True,
                             steps=[{"processor": "fm_demod", "params": {}}]))
    if "FM audio + waterfall" not in existing:
        session.add(Pipeline(name="FM audio + waterfall", mode="fm", keep_iq=True,
                             steps=[{"processor": "fm_demod", "params": {}},
                                    {"processor": "waterfall", "params": {}}]))
    if "ISS SSTV" not in existing:
        session.add(Pipeline(name="ISS SSTV", mode="sstv", keep_iq=True,
                             steps=[{"processor": "fm_demod", "params": {"audio_lowpass_hz": 3500}},
                                    {"processor": "sstv", "params": {}}]))
    if "Meteor LRPT" not in existing:
        # a nyers IQ nagy (~1,2 GB / 10 perc): siker után törlődik, hiba / nincs eredmény esetén megmarad az újrapróbáláshoz
        session.add(Pipeline(name="Meteor LRPT", mode="lrpt", keep_iq=False,
                             steps=[{"processor": "meteor_lrpt", "params": {}}]))
    if not session.scalar(select(func.count()).select_from(RadioProfile)):
        session.add_all([
            RadioProfile(name="ISS FM 145.800", center_freq_hz=145_800_000, bandwidth_hz=25_000,
                         sample_rate_hz=250_000, gain_db=None, modulation_hint="nfm", doppler_correction=True),
            RadioProfile(name="Meteor LRPT 137.900", center_freq_hz=137_900_000, bandwidth_hz=150_000,
                         sample_rate_hz=1_024_000, gain_db=None, modulation_hint="lrpt", doppler_correction=True),
            RadioProfile(name="Meteor LRPT 137.100", center_freq_hz=137_100_000, bandwidth_hz=150_000,
                         sample_rate_hz=1_024_000, gain_db=None, modulation_hint="lrpt", doppler_correction=True),
        ])
    session.commit()
