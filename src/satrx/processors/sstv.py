from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from satrx.dsp.filters import DspError
from satrx.dsp.sstv import decode_sstv, read_wav
from satrx.dsp.sstv_modes import MODE_NAMES
from satrx.pipeline.base import ArtifactSpec, NoResult, PipelineContext, Processor, ProcessorError


class SstvParams(BaseModel):
    mode: Literal[("auto", *MODE_NAMES)] = Field("auto", description="SSTV dekódolási mód; 'auto' = a VIS-kódból (vagy a szinkron-távolságból) felismerve")
    min_lines: int = Field(8, ge=2, description="Ennél kevesebb sorból álló képet eldob")
    offset_hz: float = Field(0.0, description="Csak ha nincs előző demodulátor-lépés: a belső FM-demodulátor eltolása")
    follow_doppler: bool = Field(True, description="Csak a belső FM-demodulátorhoz: Doppler-követés")


class Sstv(Processor):
    name = "sstv"
    title = "SSTV decoder → PNG"
    params_schema = SstvParams

    def run(self, ctx: PipelineContext, params: SstvParams) -> list[ArtifactSpec]:
        audio_path, temporary = ctx.state.get("audio_path"), False
        if audio_path is not None:
            ctx.log(f"using the audio of the previous step: {audio_path.name}")
        else:
            # önállóan (pl. utólagos feldolgozásnál) is használható: belső NFM-demodulálás
            from satrx.processors.audio_demod import AudioDemod, AudioDemodParams
            ctx.log("no audio from a previous step: running an internal NFM demodulator")
            (art,) = AudioDemod().run(ctx, AudioDemodParams(mode="nfm", offset_hz=params.offset_hz,
                                                           follow_doppler=params.follow_doppler,
                                                           audio_lowpass_hz=3500))
            audio_path, temporary = art.path, True
        try:
            audio, fs = read_wav(audio_path)
            ctx.log(f"audio: {len(audio) / fs:.1f} s at {fs} Hz")
            images = decode_sstv(audio, fs, params.mode, params.min_lines, ctx.log)
        except DspError as exc:
            raise ProcessorError(str(exc)) from exc
        finally:
            if temporary:
                audio_path.unlink(missing_ok=True)

        if not images:
            raise NoResult("No SSTV signal found in the audio (no VIS header and no regular sync pattern)")

        base_index = len(list(ctx.workdir.glob("sstv_*.png")))
        artifacts = []
        for i, d in enumerate(images, start=1):
            path = ctx.workdir / f"sstv_{base_index + i}.png"
            d.image.save(path, optimize=True)
            meta = {
                "mode": d.mode.name, "vis_code": d.mode.vis, "width": d.image.width, "height": d.image.height,
                "lines_decoded": d.lines_decoded, "lines_total": d.lines_total,
                "complete": d.lines_decoded >= d.lines_total, "vis_detected": d.vis_detected,
                "freq_offset_hz": round(d.freq_offset_hz, 1), "sync_fraction": round(d.sync_fraction, 3),
                "sync_count": d.sync_count, "drift_ppm": round(d.drift_ppm), "image_start_s": round(d.start_s, 1),
                "image_end_s": round(d.end_s, 1), "audio_rate_hz": fs,
            }
            ctx.log(f"{path.name}: {d.mode.name} {d.image.width}x{d.image.height}, {d.lines_decoded}/{d.lines_total} lines, "
                    f"sync {d.sync_fraction:.0%}, offset {d.freq_offset_hz:+.0f} Hz")
            artifacts.append(ArtifactSpec("image", path, meta))
        return artifacts
