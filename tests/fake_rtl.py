"""Hamis `rtl_sdr` / `rtl_fm` program: a valódi parancssori szerződést utánozza (a kimenet a stdout).

rtl_sdr: nyers 8 bites IQ, egy +10 kHz-es hanggal; rtl_fm (a `-M` kapcsolóról ismerhető fel): 16 bites mono PCM, 1 kHz-es szinusz.
Az argumentumokat egy JSON-naplóba írja (FAKE_RTL_LOG). FAKE_RTL=fail: hibaüzenet és 1-es kilépés.
"""
import json
import os
import sys
import time

import numpy as np

argv = sys.argv[1:]
log = os.environ.get("FAKE_RTL_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(argv) + "\n")

if os.environ.get("FAKE_RTL") == "fail":
    print("usb_claim_interface error -6", file=sys.stderr)
    sys.exit(1)


def opt(flag, default=None):
    return argv[argv.index(flag) + 1] if flag in argv else default


out = sys.stdout.buffer
try:
    if "-M" in argv:                                   # rtl_fm
        rate = int(opt("-r", "24000"))
        n = rate // 10
        t = 0
        while True:
            x = np.sin(2 * np.pi * 1000 * (np.arange(n) + t) / rate)
            out.write((x * 12000).astype("<i2").tobytes())
            out.flush()
            t += n
            time.sleep(0.01)
    else:                                              # rtl_sdr
        rate = int(opt("-s", "250000"))
        n = rate // 10
        t = 0
        while True:
            ph = 2 * np.pi * 10_000 * (np.arange(n) + t) / rate
            iq = np.empty(2 * n, dtype=np.uint8)
            iq[0::2] = np.clip(127.5 + 100 * np.cos(ph), 0, 255)
            iq[1::2] = np.clip(127.5 + 100 * np.sin(ph), 0, 255)
            out.write(iq.tobytes())
            out.flush()
            t += n
            time.sleep(0.005)
except (BrokenPipeError, OSError, ValueError):
    pass
