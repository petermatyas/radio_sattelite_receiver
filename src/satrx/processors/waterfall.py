from __future__ import annotations

from typing import Literal

import numpy as np
from PIL import Image
from pydantic import BaseModel, Field, field_validator

from satrx.dsp.filters import DspError
from satrx.dsp.spectrum import COLORMAPS, compute_spectrogram, render_waterfall
from satrx.pipeline.base import ArtifactSpec, PipelineContext, Processor, ProcessorError


class WaterfallParams(BaseModel):
    fft_size: int = Field(1024, ge=128, le=16384, description="FFT méret (2 hatvány); nagyobb = finomabb frekvenciafelbontás")
    max_rows: int = Field(1200, ge=50, le=4000, description="A kép maximális magassága (sorok száma)")
    start_s: float = Field(0.0, ge=0, description="Kezdet a felvétel elejétől (mp)")
    duration_s: float | None = Field(None, gt=0, description="Hossz (mp); üres = a végéig")
    center_offset_hz: float = Field(0.0, description="A kivágás közepe a felvétel közép-frekvenciájához képest")
    span_hz: float | None = Field(None, gt=0, description="Kivágott sávszélesség (Hz); üres = a teljes sáv")
    colormap: Literal["viridis", "heat", "jet", "gray"] = "viridis"
    db_min: float | None = Field(None, description="Alsó szint dBFS-ben; üres = automatikus")
    db_max: float | None = Field(None, description="Felső szint dBFS-ben; üres = automatikus")
    dynamic_range_db: float = Field(45.0, gt=5, le=140, description="Dinamikatartomány automatikus szintnél")
    overlay_doppler: bool = Field(True, description="A jósolt Doppler-görbe rárajzolása (ha van pályaadat)")
    max_width: int = Field(2048, ge=256, le=8192, description="A kép maximális szélessége (oszlopok)")

    @field_validator("fft_size")
    @classmethod
    def _power_of_two(cls, v: int) -> int:
        if v & (v - 1):
            raise ValueError("fft_size must be a power of two")
        return v

    @field_validator("colormap")
    @classmethod
    def _known(cls, v: str) -> str:
        assert v in COLORMAPS
        return v


class Waterfall(Processor):
    name = "waterfall"
    title = "Waterfall (spectrogram) → PNG"
    params_schema = WaterfallParams

    def run(self, ctx: PipelineContext, params: WaterfallParams) -> list[ArtifactSpec]:
        info = ctx.info
        if info.num_samples == 0:
            raise ProcessorError("The recording is empty")
        last = {"pct": -1}

        def progress(frac: float) -> None:
            pct = int(frac * 10) * 10
            if pct != last["pct"]:
                last["pct"] = pct
                ctx.log(f"spectrogram {pct}%")

        try:
            spec = compute_spectrogram(
                ctx.base, fft_size=params.fft_size, max_rows=params.max_rows, start_s=params.start_s,
                duration_s=params.duration_s, center_offset_hz=params.center_offset_hz, span_hz=params.span_hz,
                max_width=params.max_width, progress=progress)
            curve = self._doppler(ctx) if params.overlay_doppler else None
            title = self._title(ctx)
            img, meta = render_waterfall(spec, colormap=params.colormap, db_min=params.db_min, db_max=params.db_max,
                                         dynamic_range_db=params.dynamic_range_db, doppler_curve=curve, title=title)
        except DspError as exc:
            raise ProcessorError(str(exc)) from exc

        index = len(list(ctx.workdir.glob("waterfall_*.png"))) + 1
        path = ctx.workdir / f"waterfall_{index}.png"
        # a zajos spektrum RGB-ben tömöríthetetlen: 256 színű palettával kb. harmad akkora fájl
        img.quantize(colors=256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE).save(path, optimize=True)
        ctx.log(f"wrote {path.name}: {img.width}x{img.height}, {meta['rows']} rows, "
                f"{meta['row_s'] * 1000:.0f} ms/row, {meta['bin_hz']:.0f} Hz/column")
        return [ArtifactSpec("waterfall", path, meta)]

    @staticmethod
    def _doppler(ctx: PipelineContext):
        track = (ctx.orbit or {}).get("track")
        if not track or not track.get("doppler_freq_hz"):
            return None
        return np.asarray(track["t_s"], float), np.asarray(track["doppler_freq_hz"], float)

    @staticmethod
    def _title(ctx: PipelineContext) -> str:
        sat = ((ctx.orbit or {}).get("satellite") or {}).get("name")
        start = ((ctx.orbit or {}).get("recording") or {}).get("start") or ""
        label = f"{ctx.info.center_freq_hz / 1e6:.4f} MHz"
        return " - ".join(x for x in (sat, label, start.replace("T", " ").rstrip("Z")[:19]) if x)
