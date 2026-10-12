"""Hamis `satdump` a processzor tesztelésére: a valódi program parancssori szerződését utánozza.

Az argumentumokat egy JSON-naplóba írja (FAKE_SATDUMP_LOG), így a teszt ellenőrizheti a pontos hívást.
Viselkedés a FAKE_SATDUMP környezeti változóból:
  ok            képek + kompozit + köztes fájlok, 0-s kilépés (alap)
  none          nem ír képet, 0-s kilépés (nincs dekódolható jel)
  crash         hibaüzenet és 3-as kilépés
  slow          sokáig fut (időkorlát-teszt)
  legacy_only   az `pipeline` alparancsot nem ismeri (régi SatDump)
  new_only      a régi stílust nem ismeri
  only_80k      csak a *_80k pipeline-nal ad képet
"""
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

mode = os.environ.get("FAKE_SATDUMP", "ok")
argv = sys.argv[1:]
log = os.environ.get("FAKE_SATDUMP_LOG")
if log:
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps(argv) + "\n")

new_style = bool(argv) and argv[0] == "pipeline"
if mode == "legacy_only" and new_style:
    print("Unknown command: pipeline")
    sys.exit(1)
if mode == "new_only" and not new_style:
    print("Usage: satdump pipeline [pipeline_id] [input_level] [input_file] [output]")
    sys.exit(1)
args = argv[1:] if new_style else argv
if len(args) < 4:
    print("not enough arguments")
    sys.exit(2)
pipeline, level, input_file, outdir = args[0], args[1], args[2], Path(args[3])
opts = args[4:]
assert level == "baseband"
if not Path(input_file).is_file():
    print(f"input file not found: {input_file}")
    sys.exit(4)
outdir.mkdir(parents=True, exist_ok=True)

print(f"Starting {pipeline}")
for i in range(3):
    print(f"Progress {i * 30}%\rProgress {i * 30 + 10}%")
if mode == "crash":
    print("Segmentation fault while demodulating", file=sys.stderr)
    sys.exit(3)
if mode == "slow":
    time.sleep(30)
if mode == "none" or (mode == "only_80k" and not pipeline.endswith("_80k")):
    print("No valid frames decoded")
    sys.exit(0)


def strip(name: str, base: int):
    y, x = np.mgrid[0:240, 0:1568]
    arr = (np.sin(x / (30 + base)) * 80 + 128 + y % 7).clip(0, 255).astype(np.uint8)
    Image.fromarray(arr, "L").save(outdir / "MSU-MR" / name)


(outdir / "MSU-MR").mkdir(exist_ok=True)
for ch in (1, 2, 3):
    strip(f"MSU-MR-{ch}.png", ch)
rgb = np.stack([np.full((240, 1568), v, dtype=np.uint8) for v in (200, 120, 60)], axis=-1)
Image.fromarray(rgb, "RGB").save(outdir / "MSU-MR" / "MSU-MR-RGB-123.png")
Image.fromarray(np.zeros((4, 1568), dtype=np.uint8), "L").save(outdir / "MSU-MR" / "MSU-MR-tiny.png")  # túl alacsony
(outdir / "dataset.json").write_text(json.dumps({"satellite": "METEOR-M2 3", "products": ["MSU-MR"]}))
(outdir / "meteor_m2-x_lrpt.cadu").write_bytes(b"\0" * 300_000)
(outdir / "MSU-MR" / "msu_mr.soft").write_bytes(b"\0" * 200_000)
print("Done")
