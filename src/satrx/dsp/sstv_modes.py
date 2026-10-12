"""SSTV módok időzítési táblázata (idők ms-ban a sorszinkron-impulzus KEZDETÉHEZ képest).

Szinkron: 1200 Hz, fekete: 1500 Hz, fehér: 2300 Hz; egy pixel frekvenciája 1500 + 800 * érték / 255.
A Scottie módokban a szinkron a sor közepén van (a kék és a piros csatorna között), ezért az ott a horgony,
és a zöld/kék csatorna kezdete negatív.
"""
from __future__ import annotations

from dataclasses import dataclass

FREQ_SYNC, FREQ_BLACK, FREQ_WHITE = 1200.0, 1500.0, 2300.0
FREQ_LEADER, FREQ_VIS_ONE, FREQ_VIS_ZERO = 1900.0, 1100.0, 1300.0


@dataclass(frozen=True)
class Channel:
    plane: str        # R G B (RGB módok) | Y Cr Cb | Y0 Cr Cb Y1 (PD) | Y C (Robot36: C = R-Y vagy B-Y)
    start_ms: float   # a szinkron kezdetéhez képest
    scan_ms: float
    pixels: int


@dataclass(frozen=True)
class Mode:
    name: str
    vis: int
    width: int
    height: int
    sync_ms: float
    period_ms: float          # két szinkron távolsága (PD: két sor együtt)
    kind: str                 # rgb | pd | robot36 | robot72
    channels: tuple[Channel, ...]
    rows_per_period: int = 1
    chroma_sep_ms: float | None = None  # Robot36: az elválasztó tónus helye (1500 Hz = R-Y, 2300 Hz = B-Y)

    @property
    def periods(self) -> int:
        return self.height // self.rows_per_period

    @property
    def first_anchor_ms(self) -> float:
        """A kép első sorának kezdete és a horgony (szinkron) távolsága."""
        return max(0.0, -min(c.start_ms for c in self.channels))

    @property
    def last_end_ms(self) -> float:
        return max(c.start_ms + c.scan_ms for c in self.channels)

    @property
    def duration_s(self) -> float:
        return self.periods * self.period_ms / 1000


def _martin(name, vis, scan):
    gap, sync = 0.572, 4.862
    g = sync + gap
    b = g + scan + gap
    r = b + scan + gap
    return Mode(name, vis, 320, 256, sync, sync + gap + 3 * (scan + gap), "rgb",
                (Channel("G", g, scan, 320), Channel("B", b, scan, 320), Channel("R", r, scan, 320)))


def _scottie(name, vis, scan):
    gap, sync = 1.5, 9.0
    return Mode(name, vis, 320, 256, sync, 3 * scan + 3 * gap + sync, "rgb",
                (Channel("G", -(2 * scan + gap), scan, 320), Channel("B", -scan, scan, 320),
                 Channel("R", sync + gap, scan, 320)))


def _pd(name, vis, width, height, scan):
    sync, porch = 20.0, 2.08
    s = sync + porch
    return Mode(name, vis, width, height, sync, s + 4 * scan, "pd",
                (Channel("Y0", s, scan, width), Channel("Cr", s + scan, scan, width),
                 Channel("Cb", s + 2 * scan, scan, width), Channel("Y1", s + 3 * scan, scan, width)),
                rows_per_period=2)


MODES: tuple[Mode, ...] = (
    Mode("Robot36", 8, 320, 240, 9.0, 150.0, "robot36",
         (Channel("Y", 12.0, 88.0, 320), Channel("C", 106.0, 44.0, 160)), chroma_sep_ms=100.0),
    Mode("Robot72", 12, 320, 240, 9.0, 300.0, "robot72",
         (Channel("Y", 12.0, 138.0, 320), Channel("Cr", 156.0, 69.0, 160), Channel("Cb", 231.0, 69.0, 160))),
    _martin("MartinM1", 44, 146.432),
    _martin("MartinM2", 40, 73.216),
    _scottie("ScottieS1", 60, 138.24),
    _scottie("ScottieS2", 56, 88.064),
    _scottie("ScottieDX", 76, 345.6),
    _pd("PD50", 93, 320, 256, 91.52),
    _pd("PD90", 99, 320, 256, 170.24),
    _pd("PD120", 95, 640, 496, 121.6),
    _pd("PD160", 98, 512, 400, 195.584),
    _pd("PD180", 96, 640, 496, 183.04),
    _pd("PD240", 97, 640, 496, 244.48),
    _pd("PD290", 94, 800, 616, 228.8),
)
BY_VIS = {m.vis: m for m in MODES}
BY_NAME = {m.name: m for m in MODES}
MODE_NAMES = tuple(BY_NAME)
