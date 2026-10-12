"""Meteor-M LRPT dekódolás a SatDump külső programmal (a nyers IQ-ból képek és kompozitok).

A SatDump parancssora verziónként eltér:
  új (1.2+):  satdump pipeline <pipeline> baseband <fájl> <kimenet> --samplerate N --baseband_format cu8|cs16|cf32
  régi:       satdump <pipeline> baseband <fájl> <kimenet> --samplerate N --baseband_format w8|i16|f32
A `cli_style: auto` az újjal próbálkozik, és hiba esetén a régivel. A pontos parancssor a lépés naplójában látszik.
"""
from __future__ import annotations

import collections
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Literal

from PIL import Image
from pydantic import BaseModel, Field

from satrx.pipeline.base import ArtifactSpec, NoResult, PipelineContext, Processor, ProcessorError, ToolMissingError

PIPELINES = ("meteor_m2-x_lrpt", "meteor_m2-x_lrpt_80k")
_FORMATS = {
    "new": {"cu8": "cu8", "ci16_le": "cs16", "cf32_le": "cf32"},
    "legacy": {"cu8": "w8", "ci16_le": "i16", "cf32_le": "f32"},
}
_INTERMEDIATE = {".soft", ".cadu", ".frm", ".bin", ".raw", ".s16", ".f32", ".cu8", ".cs16", ".cf32", ".wav"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}


class MeteorLrptParams(BaseModel):
    pipeline: Literal["auto", "meteor_m2-x_lrpt", "meteor_m2-x_lrpt_80k"] = Field(
        "auto", description="72 kbps vagy 80 kbps LRPT; 'auto': előbb 72k, ha nincs kép, akkor 80k")
    cli_style: Literal["auto", "new", "legacy"] = Field("auto", description="A SatDump parancssori stílusa")
    baseband_format: str | None = Field(None, description="A --baseband_format értéke; üres = a felvétel típusából")
    fill_missing: bool = Field(False, description="--fill_missing: a hiányzó sorok feltöltése (ha a verzió támogatja)")
    timeout_s: int = Field(3600, ge=5, description="A SatDump legfeljebb ennyi ideig futhat")
    keep_intermediate: bool = Field(False, description="A köztes fájlok (CADU, soft szimbólumok) megtartása")
    max_images: int = Field(60, ge=1, le=500)
    min_image_height: int = Field(16, ge=1, description="Ennél alacsonyabb képeket eldob")
    extra_args: list[str] = Field(default_factory=list, description="További SatDump argumentumok")


def _run_command(cmd: list[str], timeout_s: int, on_line) -> int:
    """A parancs futtatása a kimenet sorainak folyamatos olvasásával és időkorláttal."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=flags)
    except OSError as exc:
        raise ProcessorError(f"Could not start SatDump ({cmd[0]}): {exc}") from exc
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            line = lines.get(timeout=0.5)
        except queue.Empty:
            line = ""
        if line is None:
            break
        if line:
            on_line(line.rstrip())
        if time.monotonic() > deadline:
            proc.kill()
            proc.wait()
            raise ProcessorError(f"SatDump did not finish within {timeout_s} s and was stopped")
    return proc.wait()


def _classify(stem: str) -> str:
    low = stem.lower()
    if "rgb" in low or "composite" in low or "false" in low or "natural" in low:
        return "composite"
    if re.search(r"(^|[-_ ])(ch)?\d{1,2}$", low):
        return "channel"
    return "product"


def _collect_images(outdir: Path, params: MeteorLrptParams) -> list[tuple[Path, int, int]]:
    found = []
    for path in sorted(outdir.rglob("*")):
        if path.suffix.lower() not in _IMAGE_SUFFIXES or not path.is_file() or path.stat().st_size == 0:
            continue
        try:
            with Image.open(path) as img:
                w, h = img.size
        except Exception:  # noqa: BLE001 - sérült kép
            continue
        if h >= params.min_image_height and w >= 16:
            found.append((path, w, h))
    order = {"composite": 0, "product": 1, "channel": 2}
    found.sort(key=lambda t: (order[_classify(t[0].stem)], str(t[0])))
    return found[: params.max_images]


class MeteorLrpt(Processor):
    name = "meteor_lrpt"
    title = "Meteor-M LRPT decoder (SatDump) → images + composites"
    params_schema = MeteorLrptParams

    def run(self, ctx: PipelineContext, params: MeteorLrptParams) -> list[ArtifactSpec]:
        satdump = ctx.tools.get("satdump")
        if not satdump:
            raise ToolMissingError(
                "SatDump was not found. Install it and set its path in the Settings page "
                "(or in config/local.yaml: tools.satdump_path).")
        info = ctx.info
        if info.num_samples == 0:
            raise ProcessorError("The recording is empty")
        data_file = Path(str(ctx.base) + ".sigmf-data")
        pipelines = PIPELINES if params.pipeline == "auto" else (params.pipeline,)
        styles = ["new", "legacy"] if params.cli_style == "auto" else [params.cli_style]

        tail: collections.deque[str] = collections.deque(maxlen=60)
        exit_codes: list[int] = []
        attempt = 0
        working_style: str | None = None
        for pipeline in pipelines:
            for style in ([working_style] if working_style else styles):
                attempt += 1
                outdir = ctx.workdir / f"satdump_{attempt}"
                shutil.rmtree(outdir, ignore_errors=True)
                outdir.mkdir(parents=True)
                cmd = self._command(satdump, style, pipeline, data_file, outdir, info, params)
                ctx.log("$ " + " ".join(cmd))
                tail.clear()
                code = _run_command(cmd, params.timeout_s, tail.append)
                exit_codes.append(code)
                images = _collect_images(outdir, params)
                ctx.log(f"exit code {code}, {len(images)} image(s) found")
                if images:
                    ctx.log("\n".join(list(tail)[-15:]))
                    return self._artifacts(ctx, outdir, images, pipeline, style, params)
                if code == 0:
                    working_style = style      # a stílus rendben van, csak nincs dekódolható jel
                    break
                shutil.rmtree(outdir, ignore_errors=True)
            # ugyanazt a pipeline-t más stílussal csak hiba esetén próbáljuk újra

        last = "\n".join(list(tail)[-12:])
        ctx.log(last)
        if any(c != 0 for c in exit_codes) and not any(c == 0 for c in exit_codes):
            raise ProcessorError(f"SatDump failed (exit code {exit_codes[-1]}). Last output: {last[-600:]}")
        raise NoResult("SatDump decoded no LRPT images (the satellite's LRPT transmitter may have been off, "
                       "or the signal was too weak)")

    @staticmethod
    def _command(satdump: list[str], style: str, pipeline: str, data_file: Path, outdir: Path, info,
                 params: MeteorLrptParams) -> list[str]:
        fmt = params.baseband_format or _FORMATS[style].get(info.fmt)
        if fmt is None:
            raise ProcessorError(f"Unsupported sample format for SatDump: {info.fmt}")
        head = [*satdump, "pipeline", pipeline] if style == "new" else [*satdump, pipeline]
        cmd = [*head, "baseband", str(data_file), str(outdir), "--samplerate", str(info.sample_rate_hz),
               "--baseband_format", fmt]
        if params.fill_missing:
            cmd.append("--fill_missing")
        return cmd + list(params.extra_args)

    @staticmethod
    def _artifacts(ctx: PipelineContext, outdir: Path, images, pipeline: str, style: str,
                   params: MeteorLrptParams) -> list[ArtifactSpec]:
        specs = []
        for path, w, h in images:
            rel = path.relative_to(outdir)
            instrument = rel.parts[0] if len(rel.parts) > 1 else ""
            specs.append(ArtifactSpec("image", path, {
                "source": "meteor_lrpt", "instrument": instrument, "product": path.stem,
                "product_kind": _classify(path.stem), "width": w, "height": h,
                "satdump_pipeline": pipeline, "bitrate": "80k" if pipeline.endswith("80k") else "72k",
                "cli_style": style, "mode": "LRPT",
            }))
        if not params.keep_intermediate:
            keep = {p for p, _, _ in images}
            freed = 0
            for path in outdir.rglob("*"):
                if path.is_file() and path not in keep and path.suffix.lower() in _INTERMEDIATE:
                    freed += path.stat().st_size
                    path.unlink()
            if freed:
                ctx.log(f"removed {freed / 1e6:.0f} MB of intermediate files")
        ctx.log(f"{len(specs)} image(s): " + ", ".join(s.path.name for s in specs[:8]) + (" ..." if len(specs) > 8 else ""))
        return specs
