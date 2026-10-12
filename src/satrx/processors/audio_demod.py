from __future__ import annotations

import math
import wave
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, Field
from scipy import signal

from satrx.dsp.filters import DecimatorChain, DspError, Fir, Iir, plan_decimation
from satrx.pipeline.base import ArtifactSpec, PipelineContext, Processor, ProcessorError
from satrx.recording import sigmf

# mód -> (sávszélesség, deviáció, felüláteresztő, hang-aluláteresztő, de-emphasis µs)
_DEFAULTS = {
    "nfm": (15_000.0, 5_000.0, 30.0, 5_000.0, 0.0),
    "wfm": (180_000.0, 75_000.0, 20.0, 15_000.0, 50.0),
    "am": (10_000.0, 0.0, 50.0, 5_000.0, 0.0),
    "usb": (2_700.0, 0.0, 100.0, 2_700.0, 0.0),
    "lsb": (2_700.0, 0.0, 100.0, 2_700.0, 0.0),
}


class AudioDemodParams(BaseModel):
    mode: Literal["nfm", "wfm", "am", "usb", "lsb"] = "nfm"
    offset_hz: float = Field(0.0, description="A jel frekvenciája: Doppler-követésnél a Doppler-görbéhez, "
                                              "különben a felvétel kezdeti közép-frekvenciájához képest (Hz)")
    follow_doppler: bool = Field(True, description="A Doppler-görbe követése az .orbit.json alapján")
    bandwidth_hz: float | None = Field(None, gt=500, le=250_000, description="Csatorna-sávszélesség; üres = módtól függő")
    deviation_hz: float | None = Field(None, gt=500, description="FM deviáció; üres = módtól függő")
    highpass_hz: float | None = Field(None, ge=0, description="Felüláteresztő (0 = ki); üres = módtól függő")
    audio_lowpass_hz: float | None = Field(None, ge=0, description="Hang aluláteresztő (0 = ki); üres = módtól függő")
    de_emphasis_us: float | None = Field(None, ge=0, description="De-emphasis µs (0 = ki); üres = módtól függő")
    squelch_db: float | None = Field(None, description="Zajzár a csatorna teljesítményére (dBFS)")
    agc: bool = Field(True, description="Automatikus hangerő-szabályzás AM/SSB módban")
    start_s: float = Field(0.0, ge=0, description="Kezdet a felvétel elejétől (mp)")
    duration_s: float | None = Field(None, gt=0, description="Hossz (mp); üres = a végéig")


def _resolve(p: AudioDemodParams, rec_bw: float | None = None) -> dict[str, float]:
    bw, dev, hp, lp, de = _DEFAULTS[p.mode]
    if rec_bw and p.mode in ("nfm", "wfm", "am"):     # a felvételnél megadott csatornaszélesség az alap
        bw = min(max(float(rec_bw), 600.0), 250_000.0)
    return {
        "bw": p.bandwidth_hz or bw, "dev": p.deviation_hz or dev,
        "hp": hp if p.highpass_hz is None else p.highpass_hz,
        "lp": lp if p.audio_lowpass_hz is None else p.audio_lowpass_hz,
        "de": de if p.de_emphasis_us is None else p.de_emphasis_us,
    }


class AudioDemod(Processor):
    name = "audio_demod"
    title = "Audio demodulator (NFM / WFM / AM / USB / LSB) → WAV"
    params_schema = AudioDemodParams

    def run(self, ctx: PipelineContext, params: AudioDemodParams) -> list[ArtifactSpec]:
        info = ctx.info
        sr = info.sample_rate_hz
        if info.num_samples == 0:
            raise ProcessorError("The recording is empty")
        r = _resolve(params, info.extra.get("satrx:bandwidth_hz"))
        try:
            d1, da = plan_decimation(sr, r["bw"])
        except DspError as exc:
            raise ProcessorError(str(exc)) from exc
        fs_if, fs_a = sr // d1, sr // (d1 * da)
        ctx.log(f"mode {params.mode}, channel {r['bw']:.0f} Hz, decimation {d1}x{da}, IF {fs_if} Hz, audio {fs_a} Hz")

        s0 = int(round(params.start_s * sr))
        s1 = info.num_samples if params.duration_s is None else min(info.num_samples, s0 + int(params.duration_s * sr))
        if s0 >= s1:
            raise ProcessorError("The selected time range is empty")

        front = DecimatorChain(d1)
        channel = Fir(self._channel_taps(params.mode, r["bw"], fs_if))
        back = DecimatorChain(da, dtype=np.float64) if da > 1 else None
        highpass = Iir(*signal.butter(1, r["hp"] / (fs_a / 2), "high")) if r["hp"] > 0 else None
        lowpass = (Fir(signal.firwin(101, min(0.99, r["lp"] / (fs_a / 2))), dtype=np.float64)
                   if 0 < r["lp"] < fs_a / 2 else None)
        deemph = None
        if r["de"] > 0:
            alpha = math.exp(-1.0 / (r["de"] * 1e-6 * fs_a))
            deemph = Iir(np.array([1 - alpha]), np.array([1.0, -alpha]))
        agc = Iir(np.array([1 - math.exp(-1 / (0.5 * fs_a))]), np.array([1.0, -math.exp(-1 / (0.5 * fs_a))])) \
            if params.agc and params.mode in ("am", "usb", "lsb") else None

        doppler = self._doppler(ctx) if params.follow_doppler else None
        ctx.log("following the Doppler curve" if doppler else "fixed frequency (no Doppler curve)")

        index = len(list(ctx.workdir.glob("audio_*.wav"))) + 1
        out_path = ctx.workdir / f"audio_{params.mode}_{index}.wav"
        phase, prev = 0.0, np.complex128(1.0)
        sum_sq = peak = power_sum = 0.0
        n_audio = power_n = open_n = 0
        window = max(1, int(fs_if * 0.02))

        with wave.open(str(out_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(fs_a)
            for start, center_hz, block in sigmf.read_segments(ctx.base, start_sample=s0, end_sample=s1):
                target = (float(np.interp((start + len(block) / 2) / sr, *doppler)) if doppler else info.center_freq_hz)
                delta = target + params.offset_hz - center_hz
                n = np.arange(len(block))
                x = block.astype(np.complex128) * np.exp(-1j * (phase + 2 * np.pi * delta * n / sr))
                phase = (phase + 2 * np.pi * delta * len(block) / sr) % (2 * np.pi)

                x = front.process(x)
                if len(x) == 0:
                    continue
                x = channel.process(x)
                power_sum += float(np.sum(np.abs(x) ** 2))
                power_n += len(x)

                if params.mode in ("nfm", "wfm"):
                    z = np.concatenate(([prev], x))
                    prev = x[-1]
                    audio = np.angle(z[1:] * np.conj(z[:-1])) * fs_if / (2 * np.pi * r["dev"])
                elif params.mode == "am":
                    audio = np.abs(x)
                else:  # usb / lsb: a szűrt jel egyoldalas, a valós rész a hang
                    audio = x.real * 2

                if params.squelch_db is not None:
                    mask = np.ones(len(x), dtype=bool)
                    for i in range(0, len(x), window):
                        seg = x[i:i + window]
                        if 10 * math.log10(float(np.mean(np.abs(seg) ** 2)) + 1e-20) < params.squelch_db:
                            mask[i:i + window] = False
                    open_n += int(mask.sum())
                    audio = np.where(mask, audio, 0.0)
                else:
                    open_n += len(x)

                if back is not None:
                    audio = back.process(audio)
                    if len(audio) == 0:
                        continue
                if highpass is not None:
                    audio = highpass.process(audio)
                if lowpass is not None:
                    audio = lowpass.process(audio)
                if deemph is not None:
                    audio = deemph.process(audio)
                if agc is not None:
                    env = agc.process(np.abs(audio))
                    audio = audio * np.minimum(0.25 / np.maximum(env, 1e-4), 200.0)
                audio = np.clip(audio, -1.0, 1.0)

                sum_sq += float(np.sum(audio ** 2))
                peak = max(peak, float(np.max(np.abs(audio))))
                n_audio += len(audio)
                wav.writeframes((audio * 32767).astype("<i2").tobytes())

        if n_audio == 0:
            raise ProcessorError("No audio samples were produced")
        rms = math.sqrt(sum_sq / n_audio)
        meta: dict[str, Any] = {
            "mode": params.mode, "sample_rate_hz": fs_a, "duration_s": round(n_audio / fs_a, 2),
            "start_s": params.start_s, "offset_hz": params.offset_hz, "bandwidth_hz": r["bw"],
            "rms_dbfs": round(20 * math.log10(rms + 1e-9), 1), "peak_dbfs": round(20 * math.log10(peak + 1e-9), 1),
            "channel_power_dbfs": round(10 * math.log10(power_sum / max(power_n, 1) + 1e-20), 1),
            "squelch_open_fraction": round(open_n / max(power_n, 1), 3), "doppler_followed": doppler is not None,
        }
        ctx.state["audio_path"], ctx.state["audio_rate_hz"] = out_path, fs_a
        ctx.log(f"wrote {out_path.name}: {meta['duration_s']} s, RMS {meta['rms_dbfs']} dBFS")
        return [ArtifactSpec("audio", out_path, meta)]

    @staticmethod
    def _channel_taps(mode: str, bw: float, fs: float) -> np.ndarray:
        numtaps = int(max(101, min(801, 8 * fs / bw))) | 1
        taps = signal.firwin(numtaps, min(0.99, bw / fs))
        if mode in ("usb", "lsb"):
            n = np.arange(numtaps) - (numtaps - 1) / 2
            taps = taps * np.exp(1j * 2 * np.pi * (1 if mode == "usb" else -1) * (bw / 2) / fs * n)
        return taps

    @staticmethod
    def _doppler(ctx: PipelineContext):
        track = (ctx.orbit or {}).get("track")
        if not track or not track.get("doppler_freq_hz"):
            return None
        return np.asarray(track["t_s"], float), np.asarray(track["doppler_freq_hz"], float)
