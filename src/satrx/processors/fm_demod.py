from __future__ import annotations

import math
import wave
from typing import Any

import numpy as np
from pydantic import BaseModel, Field
from scipy import signal

from satrx.dsp.filters import DecimatingFilter as _DecimatingFilter
from satrx.dsp.filters import DspError, Fir as _Fir, Iir as _Iir
from satrx.dsp.filters import best_decimation, stages as _stages
from satrx.pipeline.base import ArtifactSpec, PipelineContext, Processor, ProcessorError
from satrx.recording import sigmf

class FmDemodParams(BaseModel):
    carrier_offset_hz: float = Field(0.0, description="A jel eltolása a névleges (Doppler-követett) frekvenciától, "
                                                      "orbit adat nélkül a felvétel közép-frekvenciájától")
    follow_doppler: bool = Field(True, description="A Doppler-görbe követése az .orbit.json alapján")
    bandwidth_hz: float | None = Field(None, gt=1_000, le=100_000,
                                      description="Csatornaszűrő sávszélesség; üres = a felvétel sávszélessége, ennek hiányában 15 kHz")
    deviation_hz: float = Field(5_000, gt=500, description="FM deviáció (a hangerő normalizálásához)")
    highpass_hz: float = Field(30.0, ge=0, description="DC/frekvenciahiba eltávolítása (0 = ki)")
    audio_lowpass_hz: float | None = Field(5_000, gt=100, description="Hang aluláteresztő szűrő (None = ki)")
    de_emphasis_us: float = Field(0.0, ge=0, description="De-emphasis időállandó µs-ban (0 = ki)")
    squelch_db: float | None = Field(None, description="Zajzár a csatorna teljesítményére (dBFS), None = ki")


class FmDemod(Processor):
    name = "fm_demod"
    title = "FM demodulator (NFM) → WAV"
    params_schema = FmDemodParams

    def run(self, ctx: PipelineContext, params: FmDemodParams) -> list[ArtifactSpec]:
        info = ctx.info
        sr = info.sample_rate_hz
        if info.num_samples == 0:
            raise ProcessorError("The recording is empty")

        bw = params.bandwidth_hz or info.extra.get("satrx:bandwidth_hz") or 15_000
        bw = min(max(float(bw), 1_000.0), 100_000.0)
        try:
            decimation = best_decimation(sr, bw)
        except DspError as exc:
            raise ProcessorError(str(exc)) from exc
        fs = sr // decimation
        ctx.log(f"sample rate {sr} Hz, decimation {decimation} ({'x'.join(map(str, _stages(decimation)))}), "
                f"audio rate {fs} Hz")

        stages = [_DecimatingFilter(f) for f in _stages(decimation)]
        channel = _Fir(signal.firwin(101, min(0.99, bw / fs)))
        highpass = _Iir(*signal.butter(1, params.highpass_hz / (fs / 2), "high")) if params.highpass_hz > 0 else None
        lowpass = (_Fir(signal.firwin(101, min(0.99, params.audio_lowpass_hz / (fs / 2))), dtype=np.float64)
                   if params.audio_lowpass_hz else None)
        deemph = None
        if params.de_emphasis_us > 0:
            alpha = math.exp(-1.0 / (params.de_emphasis_us * 1e-6 * fs))
            deemph = _Iir(np.array([1 - alpha]), np.array([1.0, -alpha]))

        doppler = self._doppler_curve(ctx, params)
        ctx.log("following the Doppler curve from the orbit metadata" if doppler else
                "no Doppler curve used (fixed offset from the center frequency)")

        index = len([p for p in ctx.workdir.glob("fm_demod_*.wav")]) + 1
        out_path = ctx.workdir / f"fm_demod_{index}.wav"

        phase, prev = 0.0, np.complex128(1.0)
        sum_sq, peak, n_audio, open_samples, power_sum, power_n = 0.0, 0.0, 0, 0, 0.0, 0
        window = max(1, int(fs * 0.02))

        with wave.open(str(out_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(fs)
            for start, center_hz, block in sigmf.read_segments(ctx.base):
                delta = self._offset(doppler, params, start + len(block) / 2, sr, center_hz)
                n = np.arange(len(block))
                x = block.astype(np.complex128) * np.exp(-1j * (phase + 2 * np.pi * delta * n / sr))
                phase = (phase + 2 * np.pi * delta * len(block) / sr) % (2 * np.pi)

                for stage in stages:
                    x = stage.process(x)
                if len(x) == 0:
                    continue
                x = channel.process(x)

                power_sum += float(np.sum(np.abs(x) ** 2))
                power_n += len(x)
                z = np.concatenate(([prev], x))
                prev = x[-1]
                audio = np.angle(z[1:] * np.conj(z[:-1])) * fs / (2 * np.pi * params.deviation_hz)

                if params.squelch_db is not None:
                    mask = np.ones(len(x), dtype=bool)
                    for i in range(0, len(x), window):
                        seg = x[i:i + window]
                        if 10 * math.log10(float(np.mean(np.abs(seg) ** 2)) + 1e-20) < params.squelch_db:
                            mask[i:i + window] = False
                    open_samples += int(mask.sum())
                else:
                    mask = None
                    open_samples += len(x)

                if highpass is not None:
                    audio = highpass.process(audio)
                if lowpass is not None:
                    audio = lowpass.process(audio)
                if deemph is not None:
                    audio = deemph.process(audio)
                audio = np.clip(audio, -1.0, 1.0)
                if mask is not None:
                    audio = np.where(mask, audio, 0.0)

                sum_sq += float(np.sum(audio ** 2))
                peak = max(peak, float(np.max(np.abs(audio))))
                n_audio += len(audio)
                wav.writeframes((audio * 32767).astype("<i2").tobytes())

        if n_audio == 0:
            raise ProcessorError("No audio samples were produced")
        rms = math.sqrt(sum_sq / n_audio)
        meta: dict[str, Any] = {
            "sample_rate_hz": fs, "duration_s": round(n_audio / fs, 2),
            "rms_dbfs": round(20 * math.log10(rms + 1e-9), 1), "peak_dbfs": round(20 * math.log10(peak + 1e-9), 1),
            "channel_power_dbfs": round(10 * math.log10(power_sum / power_n + 1e-20), 1),
            "squelch_open_fraction": round(open_samples / n_audio, 3),
            "doppler_followed": doppler is not None, "bandwidth_hz": bw,
        }
        ctx.state["audio_path"], ctx.state["audio_rate_hz"] = out_path, fs
        ctx.log(f"wrote {out_path.name}: {meta['duration_s']} s, RMS {meta['rms_dbfs']} dBFS")
        return [ArtifactSpec("audio", out_path, meta)]

    @staticmethod
    def _doppler_curve(ctx: PipelineContext, params: FmDemodParams):
        """(t_s, vételi frekvencia Hz) tömbök az .orbit.json pálya-idősorából, ha használható."""
        track = (ctx.orbit or {}).get("track")
        if not params.follow_doppler or not track or not track.get("doppler_freq_hz"):
            return None
        return np.asarray(track["t_s"], float), np.asarray(track["doppler_freq_hz"], float)

    @staticmethod
    def _offset(doppler, params: FmDemodParams, mid_sample: float, sr: int, center_hz: int) -> float:
        """A jel eltolása a szegmens közép-frekvenciájától (Hz)."""
        if doppler is None:
            return params.carrier_offset_hz
        t_s, freq = doppler
        carrier = float(np.interp(mid_sample / sr, t_s, freq))
        return carrier + params.carrier_offset_hz - center_hz
