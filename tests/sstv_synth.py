"""Szintetikus SSTV-jel (kódoló) a dekóder teszteléséhez. Csak vételi szoftver része a tesztnek, nem a terméknek."""
from __future__ import annotations

import numpy as np
from PIL import Image

from satrx.dsp.sstv_modes import (FREQ_BLACK, FREQ_LEADER, FREQ_SYNC, FREQ_VIS_ONE, FREQ_VIS_ZERO, FREQ_WHITE, Mode)


def gradient_image(width: int, height: int) -> Image.Image:
    """Sima átmenetek és nagy színes foltok (a kroma-részmintavételezés itt nem okoz nagy hibát)."""
    y, x = np.mgrid[0:height, 0:width]
    img = np.zeros((height, width, 3), dtype=np.float64)
    img[..., 0] = 255 * x / width
    img[..., 1] = 255 * y / height
    img[..., 2] = 255 * (1 - x / width) * 0.7 + 40
    img[height // 8: height // 3, width // 8: width // 3] = (230, 40, 40)
    img[height // 2: height * 7 // 8, width // 2: width * 3 // 4] = (30, 200, 70)
    img[height // 8: height // 4, width * 5 // 8: width * 7 // 8] = (250, 250, 250)
    return Image.fromarray(img.clip(0, 255).astype(np.uint8), "RGB")


def _freq(v: np.ndarray) -> np.ndarray:
    return FREQ_BLACK + (FREQ_WHITE - FREQ_BLACK) * np.asarray(v, dtype=np.float64) / 255.0


def _planes(img: Image.Image, mode: Mode) -> dict[str, np.ndarray]:
    rgb = np.asarray(img.convert("RGB").resize((mode.width, mode.height)), dtype=np.float64)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cr = np.clip(128 + (r - y) / 1.4, 0, 255)
    cb = np.clip(128 + (b - y) / 1.78, 0, 255)
    return {"R": r, "G": g, "B": b, "Y": y, "Cr": cr, "Cb": cb}


def _put(freq: np.ndarray, start: float, values: np.ndarray, duration_samples: float) -> None:
    """Pixelfrekvenciák elhelyezése a [start, start + duration) mintatartományban."""
    n = int(round(duration_samples))
    s = int(round(start))
    bounds = np.round(np.arange(len(values) + 1) * n / len(values)).astype(int)
    idx = np.repeat(np.arange(len(values)), np.diff(bounds))
    freq[s: s + n] = values[idx]


def vis_header(fs: int, code: int) -> np.ndarray:
    ms = fs / 1000.0
    parts = [np.full(int(300 * ms), FREQ_LEADER), np.full(int(10 * ms), FREQ_SYNC), np.full(int(300 * ms), FREQ_LEADER),
             np.full(int(30 * ms), FREQ_SYNC)]
    bits = [(code >> j) & 1 for j in range(7)]
    bits.append(sum(bits) % 2)
    parts += [np.full(int(30 * ms), FREQ_VIS_ONE if b else FREQ_VIS_ZERO) for b in bits]
    parts.append(np.full(int(30 * ms), FREQ_SYNC))
    return np.concatenate(parts)


def image_freqs(img: Image.Image, mode: Mode, fs: int) -> np.ndarray:
    """A kép sorai (VIS nélkül) a névleges időzítéssel; az első sor horgonya a 0. mintánál + first_anchor."""
    ms = fs / 1000.0
    period = mode.period_ms * ms
    first = mode.first_anchor_ms * ms
    total = int(first + (mode.periods + 1) * period)
    freq = np.full(total, FREQ_BLACK)
    planes = _planes(img, mode)
    for k in range(mode.periods):
        a = first + k * period
        freq[int(round(a)): int(round(a + mode.sync_ms * ms))] = FREQ_SYNC
        for ch in mode.channels:
            t0 = a + ch.start_ms * ms
            if mode.kind == "rgb":
                vals = planes[ch.plane][k]
            elif mode.kind == "pd":
                r0, r1 = 2 * k, 2 * k + 1
                vals = {"Y0": planes["Y"][r0], "Y1": planes["Y"][r1],
                        "Cr": (planes["Cr"][r0] + planes["Cr"][r1]) / 2,
                        "Cb": (planes["Cb"][r0] + planes["Cb"][r1]) / 2}[ch.plane]
            elif mode.kind == "robot72":
                p = planes[ch.plane][k]
                vals = p if ch.plane == "Y" else (p[0::2] + p[1::2]) / 2
            else:  # robot36
                if ch.plane == "Y":
                    vals = planes["Y"][k]
                else:
                    src = planes["Cr"] if k % 2 == 0 else planes["Cb"]
                    vals = (src[k][0::2] + src[k][1::2]) / 2
                    sep = t0 - 6.0 * ms
                    freq[int(round(sep)): int(round(sep + 4.5 * ms))] = FREQ_BLACK if k % 2 == 0 else FREQ_WHITE
                    freq[int(round(sep + 4.5 * ms)): int(round(t0))] = FREQ_LEADER
            _put(freq, t0, _freq(vals), ch.scan_ms * ms)
    return freq


def synth(images: list[tuple[Image.Image, Mode]], fs: int = 12000, with_vis: bool = True, lead_s: float = 0.4,
          tail_s: float = 0.6, noise: float = 0.0, offset_hz: float = 0.0, drift_ppm: float = 0.0,
          seed: int = 0) -> np.ndarray:
    parts = [np.full(int(lead_s * fs), FREQ_BLACK)]
    for img, mode in images:
        if with_vis:
            parts.append(vis_header(fs, mode.vis))
        parts.append(image_freqs(img, mode, fs))
        parts.append(np.full(int(0.3 * fs), FREQ_BLACK))
    parts.append(np.full(int(tail_s * fs), FREQ_BLACK))
    freq = np.concatenate(parts) + offset_hz
    sig = np.sin(2 * np.pi * np.cumsum(freq) / fs)
    if drift_ppm:  # a vevő órája eltér: az idő-tengely nyújtása
        t = np.arange(len(sig)) * (1 + drift_ppm * 1e-6)
        sig = np.interp(t, np.arange(len(sig)), sig)
    if noise:
        sig = sig + noise * np.random.default_rng(seed).standard_normal(len(sig))
    return sig.astype(np.float32)


def mean_abs_error(a: Image.Image, b: Image.Image, rows: int | None = None) -> float:
    """Átlagos pixelhiba az első sorokra; a referencia csak szélességben igazodik (a magasság nem nyúlik)."""
    ref = b.convert("RGB")
    if ref.width != a.width:
        ref = ref.resize((a.width, round(ref.height * a.width / ref.width)))
    x, y = np.asarray(a.convert("RGB"), dtype=float), np.asarray(ref, dtype=float)
    n = min(len(x), len(y)) if rows is None else min(rows, len(x), len(y))
    return float(np.mean(np.abs(x[:n] - y[:n])))


def blurred_error(a: Image.Image, b: Image.Image, block: int = 8) -> float:
    """Hiba blokkátlagolás után (a pixelenkénti zajt kiátlagolja, a képszerkezet hibáját nem)."""
    def reduce(img):
        arr = np.asarray(img.convert("RGB"), dtype=float)
        h, w = (arr.shape[0] // block) * block, (arr.shape[1] // block) * block
        return arr[:h, :w].reshape(h // block, block, w // block, block, 3).mean(axis=(1, 3))
    x, y = reduce(a), reduce(b.resize(a.size))
    return float(np.mean(np.abs(x - y)))
