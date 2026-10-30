from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from satrx.core.config import Settings
from satrx.core.models import Pass, RadioProfile, Satellite, Setting, TimedEvent, WatchRule
from satrx.hardware.base import SdrCaps, SdrError
from satrx.passes.predictor import Observer, predict_passes
from satrx.scheduler.occurrences import occurrences
from satrx.scheduler.planner import Candidate, Fixed, plan

log = logging.getLogger(__name__)

OBSERVER_KEY = "observer"
REPLACEABLE = ("planned", "partial", "skipped_conflict", "unsupported")
KEPT = ("recording", "processing", "done", "failed", "missed")


def get_observer(session: Session, settings: Settings) -> Observer | None:
    """Az UI-ról mentett helyszín felülírja a YAML-t."""
    stored = session.get(Setting, OBSERVER_KEY)
    if stored is not None and stored.value:
        v = stored.value
        return Observer(v["lat"], v["lon"], v.get("alt_m") or 0.0)
    if settings.observer.is_set:
        return Observer(settings.observer.lat, settings.observer.lon, settings.observer.alt_m or 0.0)
    return None


def set_observer(session: Session, lat: float, lon: float, alt_m: float) -> Observer:
    session.merge(Setting(key=OBSERVER_KEY, value={"lat": lat, "lon": lon, "alt_m": alt_m}))
    session.commit()
    return Observer(lat, lon, alt_m)


def _compatible_resources(profile: RadioProfile, resources: dict[str, SdrCaps]) -> tuple[str, ...]:
    ok = []
    for name, caps in resources.items():
        try:
            caps.check(profile.center_freq_hz, profile.sample_rate_hz)
            ok.append(name)
        except SdrError:
            pass
    return tuple(ok)


def recompute_passes(session: Session, settings: Settings, resources: dict[str, SdrCaps],
                     now: datetime | None = None) -> int:
    """A jövőbeli felvételek újraszámolása a műholdas szabályokból és az időalapú eseményekből, ütközésfeloldással.

    A műholdas szabályokhoz megfigyelési pont kell (átvonulás-számítás); az időalapú események (NORAD ID nélkül)
    enélkül is ütemeződnek. A már lefutott/futó (KEPT) elemeket nem bántja; a futó felvételek foglaltnak számítanak.
    """
    observer = get_observer(session, settings)
    if observer is None:
        log.warning("Observer not set: satellite passes are not predicted (timed events still are)")
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(hours=settings.scheduling.horizon_hours)
    sched = settings.scheduling

    session.execute(delete(WatchRule).where(WatchRule.single_pass, WatchRule.valid_to < now - timedelta(days=7)))   # lejárt egyedi szabályok
    session.execute(delete(Pass).where(Pass.status.in_(REPLACEABLE), Pass.los >= now))
    kept = session.scalars(select(Pass).where(Pass.status.in_(KEPT), Pass.los >= now - timedelta(hours=1))).all()
    fixed = [Fixed(p.device or "", p.rec_start, p.rec_end) for p in kept
             if p.status == "recording" and p.rec_start and p.rec_end and p.rec_end > now]

    # 1. műholdas jelöltek: szabályonként átvonulások a szabály érvényességi ablakán belül
    raw: list[tuple[WatchRule, object]] = []
    rules = session.scalars(select(WatchRule).where(WatchRule.enabled, WatchRule.valid_to > now)).all() if observer else []
    for rule in rules:
        sat = session.get(Satellite, rule.norad_id)
        profile = session.get(RadioProfile, rule.profile_id)
        if sat is None or not sat.enabled or not (sat.tle_line1 and sat.tle_line2) or profile is None:
            continue
        start, end = max(now, rule.valid_from), min(horizon, rule.valid_to)
        if start >= end:
            continue
        min_el = rule.min_elevation_deg if rule.min_elevation_deg is not None else sched.default_min_elevation_deg
        for p in predict_passes(sat.tle_line1, sat.tle_line2, observer, start, end, min_el, sat.name):
            if any(k.norad_id == sat.norad_id and k.aos < p.los and p.aos < k.los for k in kept):
                continue  # ezt az átvonulást már felvettük/felvesszük
            raw.append((rule, p))

    # ugyanazt a műholdat több szabály is kérheti: a nagyobb prioritású nyer
    raw.sort(key=lambda rp: (-rp[0].priority, rp[0].id, rp[1].aos))
    chosen: list[tuple[WatchRule, object]] = []
    for rule, p in raw:
        if not any(r.norad_id == rule.norad_id and c.aos < p.los and p.aos < c.los for r, c in chosen):
            chosen.append((rule, p))

    candidates: list[Candidate] = []
    rows: list[dict] = []
    for rule, p in chosen:
        profile = session.get(RadioProfile, rule.profile_id)
        candidates.append(Candidate(
            key=len(candidates), aos=p.aos, los=p.los, max_elevation_deg=p.max_elevation_deg, priority=rule.priority,
            allow_partial=rule.allow_partial and sched.allow_partial_recording,
            buffer_before_s=sched.buffer_before_s, buffer_after_s=sched.buffer_after_s,
            resources=_compatible_resources(profile, resources)))
        rows.append(dict(norad_id=rule.norad_id, rule_id=rule.id, aos=p.aos, tca=p.tca, los=p.los,
                         max_elevation_deg=p.max_elevation_deg))

    # 2. időalapú események: pontos időablak, puffer nélkül, pályaszámítás nélkül
    for ev in session.scalars(select(TimedEvent).where(TimedEvent.enabled)):
        profile = session.get(RadioProfile, ev.profile_id)
        if profile is None:
            continue
        try:
            slots = occurrences(ev, now, horizon)
        except ValueError:
            log.warning("Timed event %s has an invalid time zone: %s", ev.id, ev.timezone)
            continue
        for s_, e_ in slots:
            if any(k.event_id == ev.id and k.aos < e_ and s_ < k.los for k in kept):
                continue
            candidates.append(Candidate(
                key=len(candidates), aos=s_, los=e_, max_elevation_deg=0.0, priority=ev.priority,
                allow_partial=ev.allow_partial and sched.allow_partial_recording, buffer_before_s=0, buffer_after_s=0,
                resources=_compatible_resources(profile, resources)))
            rows.append(dict(norad_id=None, event_id=ev.id, label=ev.name, aos=s_, tca=s_ + (e_ - s_) / 2, los=e_,
                             max_elevation_deg=0.0))

    decisions = plan(candidates, list(resources), fixed, sched.min_partial_s)
    for i, row in enumerate(rows):
        d = decisions[i]
        session.add(Pass(status=d.status, device=d.resource, rec_start=d.rec_start, rec_end=d.rec_end, **row))
    session.commit()
    return len(rows)
