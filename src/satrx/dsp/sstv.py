"""SSTV dekóder (a demodulált hangból képet): VIS-felismerés, szinkronkövetés (csúszás-korrekció), több mód.

Működés röviden:
1. A hangból pillanatnyi frekvencia készül (sáváteresztő szűrő + analitikus jel fázisváltozása).
2. VIS-fejlécek keresése (1900 Hz vezérhang, 1200 Hz szünet, bitek) -> mód és a kép kezdete.
   Ha nincs VIS (a vétel a kép közben kezdődött), a szinkron-impulzusok távolságából következtet a módra.
3. A szinkron-impulzusokra robusztus (kiugrókat elhagyó) polinom illesztése adja a sorok pontos helyét,
   így a hangkártya-/Doppler-eltérésből adódó ferdülés kiegyenlítődik.
4. A pixelek az időablakukon belüli átlagfrekvenciából készülnek; a frekvenciahibát a szinkron-impulzusokból becsüljük.
"""
from __future__ import annotations

import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage, signal

from satrx.dsp.filters import DspError
from satrx.dsp.sstv_modes import BY_NAME, BY_VIS, FREQ_BLACK, FREQ_LEADER, FREQ_SYNC, MODES, Mode


@dataclass
class VisHit:
    code: int
    header_start: int   # a vezérhang kezdete (minta)
    image_start: int    # az első sor kezdete (minta)
    offset_hz: float    # a vezérhangból mért frekvenciaeltolás


@dataclass
class DecodedImage:
    image: Image.Image
    mode: Mode
    start_s: float
    end_s: float
    lines_decoded: int
    lines_total: int
    vis_detected: bool
    freq_offset_hz: float
    sync_fraction: float          # a talált szinkron-impulzusok aránya a vártakhoz képest
    sync_count: int
    drift_ppm: float              # a sorperiódus eltérése a névlegestől
    notes: list[str] = field(default_factory=list)


# ---- bemenet --------------------------------------------------------------------------------------

def read_wav(path: Path | str) -> tuple[np.ndarray, int]:
    with wave.open(str(path)) as w:
        if w.getsampwidth() != 2:
            raise DspError("Only 16-bit WAV files are supported")
        rate, channels = w.getframerate(), w.getnchannels()
        raw = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        raw = raw.reshape(-1, channels).mean(axis=1)
    return raw, rate


class FreqTrack:
    """Pillanatnyi frekvencia és annak kumulatív összege (gyors ablak-átlagokhoz)."""

    def __init__(self, audio: np.ndarray, fs: int):
        self.fs = fs
        self.n = len(audio)
        sos = signal.butter(4, [800.0, min(2900.0, fs / 2 * 0.9)], "band", fs=fs, output="sos")
        filtered = signal.sosfiltfilt(sos, audio.astype(np.float64))
        inst = np.empty(self.n, dtype=np.float64)
        chunk, pad = 1 << 20, 4096
        prev = None
        for a in range(0, self.n, chunk):
            lo, hi = max(0, a - pad), min(self.n, a + chunk + pad)
            z = signal.hilbert(filtered[lo:hi])
            d = np.angle(z[1:] * np.conj(z[:-1])) * fs / (2 * np.pi)
            d = np.concatenate(([d[0]], d))
            inst[a:min(self.n, a + chunk)] = d[a - lo: a - lo + min(chunk, self.n - a)]
        self.inst = inst
        self.cs = np.concatenate(([0.0], np.cumsum(inst)))

    def mean(self, start: np.ndarray | float, end: np.ndarray | float) -> np.ndarray:
        """Átlagfrekvencia a [start, end) mintaablakokban (tömb is lehet)."""
        a = np.clip(np.round(start).astype(np.int64), 0, self.n - 1)
        b = np.clip(np.round(end).astype(np.int64), 1, self.n)
        b = np.maximum(b, a + 1)
        return (self.cs[b] - self.cs[a]) / (b - a)

    def ms(self, ms: float) -> float:
        return ms * self.fs / 1000.0


# ---- VIS -------------------------------------------------------------------------------------------

def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Egybefüggő True szakaszok [kezdet, vég) indexei."""
    if not mask.any():
        return []
    edges = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)))


def _close(mask: np.ndarray, gap: int) -> np.ndarray:
    """A rövid (<= gap) lyukak befoltozása egy logikai maszkban (zajos 1 ms-os átlagok miatt)."""
    return ndimage.binary_closing(mask, structure=np.ones(gap + 1, dtype=bool))


def _flat(f: np.ndarray, lo: int, hi: int, block: int = 20, limit: float = 90.0) -> bool:
    """Egyenletes-e a frekvencia (20 ms-os blokkátlagok szórása); a képtartalom (átmenetek) nem az."""
    seg = f[lo:hi]
    n = (len(seg) // block) * block
    return n >= block * 3 and float(np.std(seg[:n].reshape(-1, block).mean(axis=1))) < limit


def find_vis(ft: FreqTrack) -> list[VisHit]:
    """VIS-fejlécek: 300 ms 1900 Hz, 10 ms 1200 Hz, 300 ms 1900 Hz, start bit, 8 bit (paritással), stop bit."""
    fl = max(1, round(ft.fs * 0.001))                   # 1 ms-os keretek
    frames = ft.n // fl
    if frames < 700:
        return []
    f = (ft.cs[fl: frames * fl + 1: fl] - ft.cs[0: frames * fl: fl]) / fl
    wide = _close(np.abs(f - FREQ_LEADER) < 300, 8)    # tágan keresünk: hangfrekvencia-eltolás és zaj miatt
    hits: list[VisHit] = []
    skip_until = 0
    for a, b in _runs(wide):
        if b - a < 120 or a < skip_until:
            continue
        lead_off = float(np.median(f[a + 10:b - 10])) - FREQ_LEADER
        if abs(lead_off) > 250:
            continue
        if not _flat(f, a + 10, b - 10):
            continue
        leader = _close(np.abs(f - FREQ_LEADER - lead_off) < 170, 8)
        low = _close(f < 1550 + lead_off, 2)             # a 1200 Hz körüli szünet
        # szünet: 3-30 ms, a vezérhang vége körül
        brk = next(((x, y) for x, y in _runs(low[max(0, b - 10): b + 20]) if 3 <= y - x <= 30), None)
        if brk is None:
            continue
        k = max(0, b - 10) + brk[1]                      # a szünet utáni első keret
        run2 = next(((x, y) for x, y in _runs(leader[k - 1: k + 400]) if x <= 4), None)
        if run2 is None or run2[1] - run2[0] < 100:
            continue
        end2 = k - 1 + run2[1]
        if not _flat(f, k + 8, end2 - 8):
            continue
        offset = float(np.median(f[k + 8:k - 1 + run2[1] - 8])) - FREQ_LEADER
        # a start bit pontos kezdete: az első minta, ahol a (0,5 ms-es) átlag 1550 Hz alá esik
        s0, s1 = (end2 - 4) * fl, min(ft.n - 1, (end2 + 8) * fl)
        w = max(3, int(ft.ms(0.5)))
        sm = (ft.cs[s0 + w: s1 + 1] - ft.cs[s0: s1 + 1 - w]) / w
        below = np.flatnonzero(sm < 1550 + offset)
        if below.size == 0:
            continue
        t_start = s0 + int(below[0]) + w // 2
        bit_ms = 30.0
        end_needed = t_start + ft.ms(bit_ms * 10)
        if end_needed >= ft.n:
            continue
        start_bit = float(ft.mean(t_start + ft.ms(6), t_start + ft.ms(24)))
        if start_bit > 1400 + offset:
            continue
        bits = []
        for j in range(8):
            c = t_start + ft.ms(bit_ms * (j + 1) + 15)
            freq = float(ft.mean(c - ft.ms(9), c + ft.ms(9))) - offset
            bits.append(1 if freq < 1200 else 0)
        if sum(bits) % 2 != 0:
            continue                                                  # a paritás nem stimmel
        code = sum(bit << j for j, bit in enumerate(bits[:7]))
        if code not in BY_VIS:
            continue                                                  # nem támogatott / véletlen kód
        hits.append(VisHit(code, a * fl, int(round(end_needed)), offset))
        skip_until = int(end_needed / fl)
    return hits


# ---- szinkron-impulzusok ---------------------------------------------------------------------------------

def find_sync_runs(ft: FreqTrack, offset_hz: float = 0.0, min_ms: float = 3.0, max_ms: float = 30.0) -> np.ndarray:
    """A 1200 Hz körüli szakaszok közepei (minta) a megadott hossz-tartományban."""
    w = max(3, int(ft.ms(0.5)))
    sm = (ft.cs[w:] - ft.cs[:-w]) / w
    mask = sm < (FREQ_SYNC + FREQ_BLACK) / 2 + offset_hz
    out = [(a + b) / 2 + w / 2 for a, b in _runs(mask) if ft.ms(min_ms) <= b - a <= ft.ms(max_ms)]
    return np.asarray(out, dtype=np.float64)


def first_regular_sync(syncs: np.ndarray, period: float) -> float:
    """VIS nélkül: az a szinkron, amelyhez a legtöbb másik illeszkedik a periódus-rácson (a kiugró zajt kihagyja)."""
    best_score, best_first = -1, float(syncs[0])
    for s0 in syncs[: min(len(syncs), 400)]:
        d = (syncs - s0) / period
        fits = np.abs(d - np.round(d)) < 0.05
        score = int(fits.sum())
        if score > best_score:
            best_score, best_first = score, float(syncs[fits].min())
    return best_first


def infer_mode(ft: FreqTrack, syncs: np.ndarray) -> Mode | None:
    """VIS nélkül: a szinkron-impulzusok távolságából és szélességéből."""
    if len(syncs) < 6:
        return None
    gaps = np.diff(syncs) * 1000 / ft.fs
    best: tuple[int, Mode] | None = None
    for mode in MODES:
        near = np.abs(gaps - mode.period_ms) < 0.03 * mode.period_ms
        count = int(near.sum())
        if count >= 5 and (best is None or count > best[0]):
            best = (count, mode)
    return best[1] if best else None


# ---- kép dekódolása ----------------------------------------------------------------------------------------

def _fit_anchors(syncs: np.ndarray, expected0: float, period: float, max_lines: int) -> tuple[np.poly1d, np.ndarray, np.ndarray] | None:
    """Szinkronok hozzárendelése sorokhoz és robusztus polinomillesztés (sor-index -> idő).

    Visszaadja: (illesztett polinom, a belső (hozzárendelt) szinkronok sor-indexe, ideje).
    """
    tol = 0.15
    a, b = expected0, period
    ks = np.array([], dtype=np.float64)
    ts = np.array([], dtype=np.float64)
    coef = None
    for it in range(6):
        k = np.round((syncs - a) / b)
        resid = syncs - (a + k * b)
        ok = (k >= 0) & (k < max_lines) & (np.abs(resid) < tol * period)
        # soronként a legjobb (legkisebb eltérésű) szinkron
        order = np.lexsort((np.abs(resid), k))
        seen, keep = set(), []
        for i in order:
            if ok[i] and k[i] not in seen:
                seen.add(k[i])
                keep.append(i)
        if len(keep) < 3:
            return None
        ks, ts = k[keep], syncs[keep]
        deg = 2 if len(ks) >= 60 else 1
        for _ in range(3):  # kiugrók elhagyása
            coef = np.polyfit(ks, ts, deg)
            res = ts - np.polyval(coef, ks)
            lim = max(3 * 1.4826 * np.median(np.abs(res - np.median(res))), 0.002 * period)
            inl = np.abs(res) <= lim
            if inl.all() or inl.sum() < 3:
                break
            ks, ts = ks[inl], ts[inl]
        poly = np.poly1d(coef)
        a, b = float(poly(0)), float(poly(1) - poly(0))
        tol = 0.05
    return poly, ks, ts


def _pixel_means(ft: FreqTrack, anchors: np.ndarray, channel, ) -> np.ndarray:
    """(sorok x pixelek) átlagfrekvencia egy csatornára."""
    n = channel.pixels
    p = np.arange(n)
    step = ft.ms(channel.scan_ms) / n
    base = anchors[:, None] + ft.ms(channel.start_ms) + p[None, :] * step
    return ft.mean(base, base + step)


def _to_value(freq: np.ndarray, offset_hz: float) -> np.ndarray:
    return np.clip((freq - offset_hz - FREQ_BLACK) / 800.0 * 255.0, 0, 255)


def _ycrcb_to_rgb(y, cr, cb) -> np.ndarray:
    r = (100 * y + 140 * cr - 17850) / 100
    g = (100 * y - 71 * cr - 33 * cb + 13260) / 100
    b = (100 * y + 178 * cb - 22695) / 100
    return np.clip(np.stack([r, g, b], axis=-1), 0, 255).astype(np.uint8)


def decode_image(ft: FreqTrack, mode: Mode, search_start: float, search_end: float, all_syncs: np.ndarray,
                 offset0: float, vis_detected: bool) -> DecodedImage | None:
    period = ft.ms(mode.period_ms)
    syncs = all_syncs[(all_syncs >= search_start - period * 0.5) & (all_syncs <= search_end)]
    expected0 = search_start + ft.ms(mode.first_anchor_ms + mode.sync_ms / 2)
    max_lines = mode.periods
    fit = _fit_anchors(syncs, expected0, period, max_lines)
    if fit is None:
        return None
    poly, ks, ts = fit
    n_inlier = len(ks)

    # frekvenciahiba: a belső szinkron-impulzusok közepének átlaga a 1200 Hz-hez képest
    centers = ts
    means = ft.mean(centers - ft.ms(mode.sync_ms * 0.3), centers + ft.ms(mode.sync_ms * 0.3))
    offset = float(np.median(means)) - FREQ_SYNC
    if vis_detected or abs(offset) > 250:
        offset = offset0  # VIS: a 600 ms-os vezérhangból mért eltolás pontosabb a rövid szinkron-impulzusokénál

    last_k = int(ks.max())
    k_end = min(max_lines, last_k + 3)
    kk = np.arange(k_end)
    anchors = poly(kk) - ft.ms(mode.sync_ms / 2)                      # a szinkron kezdete (a közepéből)
    last_sample_needed = anchors + ft.ms(mode.last_end_ms)
    kk = kk[last_sample_needed <= ft.n + ft.ms(3)]
    anchors = anchors[: len(kk)]
    if len(kk) < 3:
        return None

    vals = {c.plane: _to_value(_pixel_means(ft, anchors, c), offset) for c in mode.channels}
    rows = len(kk) * mode.rows_per_period
    if mode.kind == "rgb":
        rgb = np.stack([vals["R"], vals["G"], vals["B"]], axis=-1).astype(np.uint8)
    elif mode.kind == "pd":
        h = len(kk)
        rgb = np.empty((2 * h, mode.width, 3), dtype=np.uint8)
        rgb[0::2] = _ycrcb_to_rgb(vals["Y0"], vals["Cr"], vals["Cb"])
        rgb[1::2] = _ycrcb_to_rgb(vals["Y1"], vals["Cr"], vals["Cb"])
    elif mode.kind == "robot72":
        up = lambda a: np.repeat(a, 2, axis=1)  # noqa: E731
        rgb = _ycrcb_to_rgb(vals["Y"], up(vals["Cr"]), up(vals["Cb"]))
    else:  # robot36: felváltva R-Y és B-Y; az elválasztó tónus mondja meg, melyik
        sep = ft.mean(anchors + ft.ms(mode.chroma_sep_ms + 0.5), anchors + ft.ms(mode.chroma_sep_ms + 4.0)) - offset
        is_cr = sep < 1900
        c = np.repeat(vals["C"], 2, axis=1)
        cr = np.full_like(vals["Y"], 128.0)
        cb = np.full_like(vals["Y"], 128.0)
        h = len(kk)
        for i in range(h):
            if is_cr[i]:
                cr[i] = c[i]
                j = i + 1 if i + 1 < h and not is_cr[i + 1] else (i - 1 if i >= 1 and not is_cr[i - 1] else None)
                if j is not None:
                    cb[i] = c[j]
            else:
                cb[i] = c[i]
                j = i - 1 if i >= 1 and is_cr[i - 1] else (i + 1 if i + 1 < h and is_cr[i + 1] else None)
                if j is not None:
                    cr[i] = c[j]
        rgb = _ycrcb_to_rgb(vals["Y"], cr, cb)

    expected_lines = int(np.sum(poly(np.arange(max_lines)) + ft.ms(mode.last_end_ms) <= ft.n + ft.ms(3)))
    fraction = n_inlier / max(1, min(max_lines, expected_lines)) if expected_lines else 0.0
    drift_ppm = (float(poly(1) - poly(0)) / period - 1.0) * 1e6
    start = float(poly(0)) - ft.ms(mode.sync_ms / 2) - ft.ms(mode.first_anchor_ms)
    return DecodedImage(Image.fromarray(rgb[:rows], "RGB"), mode, start / ft.fs, (anchors[-1] + ft.ms(mode.period_ms)) / ft.fs,
                        rows, mode.height, vis_detected, offset, min(1.0, fraction), n_inlier, drift_ppm)


def decode_sstv(audio: np.ndarray, fs: int, mode: str = "auto", min_lines: int = 8,
                log: Callable[[str], None] = lambda _m: None) -> list[DecodedImage]:
    """Az összes dekódolható SSTV-kép a hangfelvételből (egy hívásban több is lehet)."""
    if mode != "auto" and mode not in BY_NAME:
        raise DspError(f"Unknown SSTV mode '{mode}'")
    if len(audio) < fs * 2:
        raise DspError("The audio is too short for SSTV")
    ft = FreqTrack(audio, fs)
    hits = find_vis(ft)
    log(f"VIS headers found: {len(hits)}" + (": " + ", ".join(
        f"{BY_VIS[h.code].name if h.code in BY_VIS else 'unknown(' + str(h.code) + ')'} at {h.image_start / fs:.1f}s" for h in hits) if hits else ""))

    images: list[DecodedImage] = []
    syncs_cache: dict[float, np.ndarray] = {}

    def syncs_for(offset: float) -> np.ndarray:
        key = round(offset / 25)
        if key not in syncs_cache:
            syncs_cache[key] = find_sync_runs(ft, key * 25.0)
        return syncs_cache[key]

    if hits:
        covered_until = 0
        for i, h in enumerate(hits):
            if h.header_start < covered_until:
                continue                                              # egy már dekódolt kép belsejébe esik
            m = BY_NAME[mode] if mode != "auto" else BY_VIS.get(h.code)
            if m is None:
                log(f"skipping unknown VIS code {h.code}")
                continue
            # a következő kép fejléce vet véget ennek (a túl korán jövő találat a kép belsejében van: hamis)
            min_end = h.image_start + 0.9 * m.duration_s * fs
            nxt = next((x for x in hits[i + 1:] if x.header_start >= min_end), None)
            end = nxt.header_start if nxt else ft.n
            img = decode_image(ft, m, h.image_start, end, syncs_for(h.offset_hz), h.offset_hz, True)
            if img is None or img.sync_count < 5 or img.sync_fraction < 0.12 or img.lines_decoded < min_lines:
                log(f"{m.name} at {h.image_start / fs:.1f}s: too few sync pulses, skipped")
                continue
            images.append(img)
            covered_until = int(img.end_s * fs)
        return images

    best: tuple[int, float, np.ndarray, Mode | None] | None = None
    for off in (0.0, -100.0, 100.0, -200.0, 200.0):
        syncs = syncs_for(off)
        m = BY_NAME[mode] if mode != "auto" else infer_mode(ft, syncs)
        if m is None:
            continue
        gaps = np.diff(syncs) * 1000 / fs
        count = int((np.abs(gaps - m.period_ms) < 0.03 * m.period_ms).sum())
        if best is None or count > best[0]:
            best = (count, off, syncs, m)
        if mode != "auto" or count >= 0.5 * (len(syncs) - 1):
            break
    if best is None:
        syncs, m, blind_offset = syncs_for(0.0), None, 0.0
    else:
        _, blind_offset, syncs, m = best
    if m is None:
        log(f"no VIS header and no regular sync pattern ({len(syncs)} sync-like pulses)")
        return []
    log(f"no VIS header; mode inferred from the sync spacing: {m.name}")
    first = first_regular_sync(syncs, ft.ms(m.period_ms))
    img = decode_image(ft, m, first - ft.ms(m.first_anchor_ms + m.sync_ms / 2), ft.n, syncs, blind_offset, False)
    if img is None or img.sync_count < 10 or img.sync_fraction < 0.35 or img.lines_decoded < min_lines:
        log("the sync pattern is too weak: no image")
        return []
    return [img]
