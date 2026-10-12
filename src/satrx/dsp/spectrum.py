"""Spektrogram (waterfall) számítás és renderelés felvételekből; a színezés az élő waterfallal közös lesz."""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from satrx.dsp.filters import DspError
from satrx.recording import sigmf

# ---- színtérképek --------------------------------------------------------------------------------

_STOPS: dict[str, list[tuple[int, int, int]]] = {
    "viridis": [(68, 1, 84), (72, 40, 120), (62, 74, 137), (49, 104, 142), (38, 130, 142), (31, 158, 137),
                (53, 183, 121), (110, 206, 88), (181, 222, 43), (253, 231, 37)],
    "heat": [(0, 0, 0), (120, 0, 0), (230, 40, 0), (255, 170, 0), (255, 255, 120), (255, 255, 255)],
    "jet": [(0, 0, 128), (0, 0, 255), (0, 255, 255), (255, 255, 0), (255, 0, 0), (128, 0, 0)],
    "gray": [(0, 0, 0), (255, 255, 255)],
}
COLORMAPS = tuple(_STOPS)


def colormap_lut(name: str) -> np.ndarray:
    """256 x 3 uint8 keresőtábla."""
    stops = np.asarray(_STOPS[name], dtype=float)
    x = np.linspace(0, 1, len(stops))
    xi = np.linspace(0, 1, 256)
    return np.stack([np.interp(xi, x, stops[:, c]) for c in range(3)], axis=1).round().astype(np.uint8)


# ---- spektrogram ---------------------------------------------------------------------------------

@dataclass
class Spectrogram:
    db: np.ndarray            # (sorok, oszlopok) dBFS
    ref_center_hz: int        # a frekvenciatengely nullpontja (a felvétel kezdeti közép-frekvenciája)
    f_lo_hz: float            # a bal / jobb szél a ref_center-hez képest (Hz)
    f_hi_hz: float
    t0_s: float               # az első / utolsó sor ideje a felvétel kezdetétől
    t1_s: float
    bin_hz: float             # egy oszlop szélessége (pooling után)
    fft_size: int
    ffts_per_row: int

    @property
    def rows(self) -> int:
        return self.db.shape[0]

    @property
    def row_s(self) -> float:
        return (self.t1_s - self.t0_s) / max(1, self.rows)


def compute_spectrogram(base, *, fft_size: int = 1024, max_rows: int = 1200, start_s: float = 0.0,
                        duration_s: float | None = None, center_offset_hz: float = 0.0, span_hz: float | None = None,
                        max_width: int = 2048, progress: Callable[[float], None] | None = None) -> Spectrogram:
    """Átlagolt (Welch-típusú) spektrogram. A sorok a felvétel kezdetétől eltelt idő szerint rendezettek.

    A közép-frekvencia változásai (Doppler-követés) a frekvenciatengelyen kompenzálva vannak: a jel a
    felvétel kezdeti közép-frekvenciájához mért helyén marad (a szélső néhány bin körbefordulhat).
    """
    info = sigmf.read_info(base)
    sr = info.sample_rate_hz
    s0 = int(round(start_s * sr))
    s1 = info.num_samples if duration_s is None else min(info.num_samples, s0 + int(round(duration_s * sr)))
    n_ffts = (s1 - s0) // fft_size
    if s0 < 0 or n_ffts < 1:
        raise DspError("The selected time range is too short for the chosen FFT size")

    ffts_per_row = max(1, math.ceil(n_ffts / max_rows))
    window = np.hanning(fft_size).astype(np.float32)
    norm = float(np.sum(window)) ** 2
    bin_hz = sr / fft_size
    ref = info.center_freq_hz

    # frekvencia-kivágás (a bin-ek a ref-hez viszonyítva: (k - N/2) * bin_hz)
    lo_bin, hi_bin = 0, fft_size
    if span_hz is not None:
        lo_bin = max(0, int(math.floor(fft_size / 2 + (center_offset_hz - span_hz / 2) / bin_hz)))
        hi_bin = min(fft_size, int(math.ceil(fft_size / 2 + (center_offset_hz + span_hz / 2) / bin_hz)))
        if hi_bin - lo_bin < 8:
            raise DspError("The selected frequency span is too narrow")
    width = hi_bin - lo_bin
    pool = max(1, math.ceil(width / max_width))

    rows: list[np.ndarray] = []
    acc = np.zeros(fft_size, dtype=np.float64)
    count = 0
    done_ffts = 0
    block = max(fft_size, (262144 // fft_size) * fft_size)
    leftover = np.empty(0, dtype=np.complex64)
    last_center = None

    def emit() -> None:
        nonlocal acc, count
        power = acc / count / norm
        rows.append(10 * np.log10(power[lo_bin:hi_bin] + 1e-20).astype(np.float32))
        acc[:] = 0
        count = 0

    for pos, center, samples in sigmf.read_segments(base, block, s0, s0 + n_ffts * fft_size):
        if center != last_center:
            leftover = np.empty(0, dtype=np.complex64)  # a szegmens határán a maradék eldobása
            last_center = center
        data = np.concatenate([leftover, samples]) if leftover.size else samples
        usable = (len(data) // fft_size) * fft_size
        leftover = data[usable:]
        if usable == 0:
            continue
        frames = data[:usable].reshape(-1, fft_size) * window
        power = np.fft.fftshift(np.abs(np.fft.fft(frames, axis=1)) ** 2, axes=1)
        shift = int(round((center - ref) / bin_hz))
        if shift:
            power = np.roll(power, shift, axis=1)
        i = 0
        while i < len(power):
            take = min(ffts_per_row - count, len(power) - i)
            acc += power[i:i + take].sum(axis=0)
            count += take
            i += take
            if count == ffts_per_row:
                emit()
        done_ffts += len(power)
        if progress:
            progress(min(1.0, done_ffts / n_ffts))

    if count > 0:
        emit()  # a záró, rövidebb sor is bekerül
    if not rows:
        raise DspError("No spectrum rows could be computed")
    db = np.vstack(rows)
    if pool > 1:
        trim = (db.shape[1] // pool) * pool
        db = db[:, :trim].reshape(db.shape[0], -1, pool).max(axis=2)  # max-pool: a keskeny jelek megmaradnak

    f_lo = (lo_bin - fft_size / 2) * bin_hz
    f_hi = f_lo + db.shape[1] * pool * bin_hz
    return Spectrogram(db=db, ref_center_hz=ref, f_lo_hz=f_lo, f_hi_hz=f_hi, t0_s=s0 / sr,
                       t1_s=s0 / sr + done_ffts * fft_size / sr, bin_hz=bin_hz * pool,
                       fft_size=fft_size, ffts_per_row=ffts_per_row)


# ---- renderelés ----------------------------------------------------------------------------------

def nice_ticks(lo: float, hi: float, target: int = 8) -> list[float]:
    """Szép (1-2-5 lépésű) osztásközök a [lo, hi] tartományban."""
    span = hi - lo
    if span <= 0:
        return [lo]
    raw = span / max(1, target)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)
    first = math.ceil(lo / step) * step
    return [first + i * step for i in range(int((hi - first) / step + 1e-9) + 1)]


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # régi Pillow: nem skálázható alapbetűtípus
        return ImageFont.load_default()


def _fmt_freq(hz: float) -> str:
    return f"{hz / 1000:+.1f}" if abs(hz) >= 1 else "0"


def auto_range(db: np.ndarray, dynamic_range_db: float) -> tuple[float, float]:
    floor = float(np.percentile(db, 5))
    return floor - 3.0, floor - 3.0 + dynamic_range_db


def render_waterfall(spec: Spectrogram, *, colormap: str = "viridis", db_min: float | None = None,
                     db_max: float | None = None, dynamic_range_db: float = 45.0,
                     doppler_curve: tuple[np.ndarray, np.ndarray] | None = None,
                     title: str = "") -> tuple[Image.Image, dict[str, Any]]:
    """PNG-be való kép tengelyekkel; a visszaadott meta a kijelöléshez szükséges geometriát tartalmazza."""
    if colormap not in _STOPS:
        raise DspError(f"Unknown colormap '{colormap}'")
    if db_min is None or db_max is None:
        lo, hi = auto_range(spec.db, dynamic_range_db)
        db_min = lo if db_min is None else db_min
        db_max = hi if db_max is None else db_max
    if db_max <= db_min:
        raise DspError("db_max must be greater than db_min")

    norm = np.clip((spec.db - db_min) / (db_max - db_min), 0, 1)
    pixels = colormap_lut(colormap)[(norm * 255).astype(np.uint8)]
    rows, cols = pixels.shape[:2]
    vscale = max(1, min(4, 300 // max(1, rows)))  # kevés sor esetén függőleges nagyítás
    if vscale > 1:
        pixels = np.repeat(pixels, vscale, axis=0)
    plot_h, plot_w = pixels.shape[:2]

    left, top, right, bottom, bar = 78, 34, 24, 44, 46
    img = Image.new("RGB", (left + plot_w + bar + right, top + plot_h + bottom), (18, 18, 22))
    img.paste(Image.fromarray(pixels, "RGB"), (left, top))
    draw = ImageDraw.Draw(img)
    font, small = _font(14), _font(12)
    white, grey = (235, 235, 235), (120, 120, 130)

    def x_of(f_hz: float) -> float:
        return left + (f_hz - spec.f_lo_hz) / (spec.f_hi_hz - spec.f_lo_hz) * plot_w

    def y_of(t_s: float) -> float:
        return top + (t_s - spec.t0_s) / (spec.t1_s - spec.t0_s) * plot_h

    draw.text((left, 8), title or f"{spec.ref_center_hz / 1e6:.4f} MHz", fill=white, font=font)
    draw.text((left + plot_w - 150, 8), f"FFT {spec.fft_size}, {spec.row_s * 1000:.0f} ms/row", fill=grey, font=small)

    for f in nice_ticks(spec.f_lo_hz, spec.f_hi_hz, max(4, plot_w // 110)):
        x = x_of(f)
        draw.line([(x, top), (x, top + plot_h)], fill=(60, 60, 70))
        draw.line([(x, top + plot_h), (x, top + plot_h + 5)], fill=white)
        draw.text((x, top + plot_h + 8), _fmt_freq(f), fill=white, font=small, anchor="mt")
    draw.text((left + plot_w / 2, top + plot_h + 28), f"kHz (center {spec.ref_center_hz / 1e6:.4f} MHz)",
              fill=grey, font=small, anchor="mt")
    for t in nice_ticks(spec.t0_s, spec.t1_s, max(4, plot_h // 60)):
        y = y_of(t)
        draw.line([(left - 5, y), (left, y)], fill=white)
        draw.text((left - 8, y), f"{t:.0f} s" if t == int(t) else f"{t:.1f} s", fill=white, font=small, anchor="rm")

    overlay = False
    if doppler_curve is not None:
        t_s, freq = doppler_curve
        keep = (t_s >= spec.t0_s) & (t_s <= spec.t1_s)
        pts = [(x_of(f - spec.ref_center_hz), y_of(t)) for t, f in zip(t_s[keep], freq[keep])]
        pts = [p for p in pts if left <= p[0] <= left + plot_w]
        if len(pts) >= 2:
            draw.line(pts, fill=(255, 60, 200), width=1)
            draw.text((pts[0][0] + 6, pts[0][1] + 2), "Doppler", fill=(255, 120, 220), font=small)
            overlay = True

    bar_x = left + plot_w + 12
    lut = colormap_lut(colormap)
    for i in range(plot_h):
        draw.line([(bar_x, top + i), (bar_x + 10, top + i)], fill=tuple(int(c) for c in lut[255 - int(i / plot_h * 255)]))
    draw.text((bar_x + 14, top), f"{db_max:.0f}", fill=white, font=small, anchor="lt")
    draw.text((bar_x + 14, top + plot_h), f"{db_min:.0f} dB", fill=white, font=small, anchor="lb")

    meta = {
        "fft_size": spec.fft_size, "rows": rows, "row_s": round(spec.row_s, 4),
        "bin_hz": round(spec.bin_hz, 2), "colormap": colormap, "db_min": round(db_min, 1), "db_max": round(db_max, 1),
        "ref_center_hz": spec.ref_center_hz, "f_lo_hz": round(spec.f_lo_hz, 1), "f_hi_hz": round(spec.f_hi_hz, 1),
        "t0_s": round(spec.t0_s, 3), "t1_s": round(spec.t1_s, 3), "doppler_overlay": overlay,
        "plot": {"x": left, "y": top, "w": plot_w, "h": plot_h}, "width": img.width, "height": img.height,
    }
    return img, meta


# ---- gyors sorok az élő nézethez --------------------------------------------------------------------------

_WINDOWS: dict[int, tuple[np.ndarray, float]] = {}


def spectrum_rows(samples: np.ndarray, fft_size: int, n_rows: int) -> np.ndarray:
    """A mintákból n_rows darab átlagolt spektrumsor (dBFS, a közép-frekvencia a sor közepén).

    Ugyanazt az ablakot és normálást használja, mint a compute_spectrogram, így a két nézet szintje egyezik.
    """
    if fft_size not in _WINDOWS:
        w = np.hanning(fft_size).astype(np.float32)
        _WINDOWS[fft_size] = (w, float(np.sum(w)) ** 2)
    window, norm = _WINDOWS[fft_size]
    usable = (len(samples) // fft_size) * fft_size
    if usable == 0:
        return np.empty((0, fft_size), dtype=np.float32)
    frames = samples[:usable].reshape(-1, fft_size) * window
    power = np.fft.fftshift(np.abs(np.fft.fft(frames, axis=1)) ** 2, axes=1)
    groups = np.array_split(power, min(n_rows, len(power)), axis=0)
    rows = np.stack([g.mean(axis=0) for g in groups])
    return (10 * np.log10(rows / norm + 1e-20)).astype(np.float32)


def quantize_db(db: np.ndarray, lo: float = -120.0, hi: float = 0.0) -> np.ndarray:
    """dBFS -> uint8 (lo -> 0, hi -> 255)."""
    return np.clip((db - lo) / (hi - lo) * 255.0, 0, 255).astype(np.uint8)
