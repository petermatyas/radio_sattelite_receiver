"""Közvetlen hangfelvétel az `rtl_fm` programmal (a demodulálást a program végzi, nyers IQ nem marad)."""
from __future__ import annotations

import collections
import subprocess
import threading
import wave
from dataclasses import dataclass
from pathlib import Path

from satrx.hardware.base import SdrError

MODES = ("fm", "wbfm", "am", "usb", "lsb", "raw")


@dataclass(frozen=True)
class FmSpec:
    center_hz: int
    duration_s: float
    mode: str = "fm"
    bandwidth_hz: int = 12_500       # az rtl_fm -s értéke: a demodulátor bemeneti sávszélessége
    audio_rate_hz: int = 24_000      # az rtl_fm -r értéke: a WAV mintavétele
    gain_db: float | None = None
    squelch: int = 0                 # az rtl_fm -l értéke (0 = nyitott zajzár)
    ppm: int = 0
    device_index: int = 0


@dataclass(frozen=True)
class FmResult:
    seconds: float
    size_bytes: int
    stopped_early: bool


def build_command(command: list[str], spec: FmSpec) -> list[str]:
    if spec.mode not in MODES:
        raise SdrError(f"Unsupported rtl_fm mode: {spec.mode}")
    cmd = [*command, "-f", str(spec.center_hz), "-M", spec.mode, "-s", str(spec.bandwidth_hz),
           "-r", str(spec.audio_rate_hz), "-d", str(spec.device_index)]
    if spec.gain_db is not None:
        cmd += ["-g", f"{spec.gain_db:g}"]
    if spec.squelch:
        cmd += ["-l", str(spec.squelch)]
    if spec.ppm:
        cmd += ["-p", str(spec.ppm)]
    cmd.append("-")                      # 16 bites mono PCM a kimenetre
    return cmd


def record_fm(command: list[str], spec: FmSpec, out: Path, stop: threading.Event,
              on_progress=None) -> FmResult:
    """Az rtl_fm hangját `spec.duration_s` másodpercig WAV-ba írja. A hossz a kapott minták száma szerint számít."""
    cmd = build_command(command, spec)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1 << 16)
    except OSError as exc:
        raise SdrError(f"Could not start rtl_fm ({command[0]}): {exc}. Install the rtl-sdr tools or set "
                       "tools.rtl_fm_path in config/local.yaml") from exc
    errors: collections.deque[str] = collections.deque(maxlen=12)

    def drain() -> None:
        for line in iter(proc.stderr.readline, b""):
            errors.append(line.decode("utf-8", "replace").rstrip())

    threading.Thread(target=drain, daemon=True).start()
    total = int(round(spec.duration_s * spec.audio_rate_hz)) * 2          # bájt (s16 mono)
    got = 0
    stopped_early = False
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with wave.open(str(out), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(spec.audio_rate_hz)
            while got < total:
                if stop.is_set():
                    stopped_early = True
                    break
                chunk = proc.stdout.read(min(8192, total - got))
                if not chunk:
                    break
                wav.writeframes(chunk)
                got += len(chunk)
                if on_progress:
                    on_progress(got / 2 / spec.audio_rate_hz)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    if got == 0 and not stopped_early:
        text = " | ".join(l for l in errors if l)
        raise SdrError(f"rtl_fm produced no audio (exit code {proc.returncode}): {text or 'no message'}")
    return FmResult(got / 2 / spec.audio_rate_hz, out.stat().st_size, stopped_early)


__all__ = ["FmSpec", "FmResult", "MODES", "build_command", "record_fm"]
