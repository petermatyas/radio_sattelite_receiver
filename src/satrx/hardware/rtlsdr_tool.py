from __future__ import annotations

import collections
import subprocess
import threading

import numpy as np

from satrx.hardware.base import SdrCaps, SdrDevice, SdrError


class RtlSdrToolDevice(SdrDevice):
    """RTL-SDR az `rtl_sdr` parancssori programmal: a program a nyers 8 bites IQ-t a kimenetére írja, mi onnan olvassuk.

    Nem kell hozzá pyrtlsdr; elég az `rtl_sdr` (és a hozzá tartozó librtlsdr + WinUSB/udev illesztő).
    Újrahangolás (Doppler) a program újraindításával történik, ami rövid (~0,2 s) mintakiesést okoz; fix frekvenciás
    felvételnél (PMR, FM, időzített események) ez nem fordul elő.
    """

    def __init__(self, command: list[str] | None = None, device_index: int = 0, ppm: int = 0):
        self.command = command or ["rtl_sdr"]
        self.device_index = device_index
        self.ppm = ppm
        self._proc: subprocess.Popen | None = None
        self._stderr: collections.deque[str] = collections.deque(maxlen=12)
        self._cfg: tuple[int, int, float | None] | None = None

    def capabilities(self) -> SdrCaps:
        return SdrCaps(24_000_000, 1_766_000_000, 225_001, 3_200_000, (0.0, 49.6), ("RX",), "cu8")

    def open(self) -> None:
        pass  # a program a configure()-nál indul (a frekvencia és a mintavétel az indítási paraméter)

    def configure(self, center_hz, sample_rate_hz, gain_db=None, antenna=None) -> None:
        self.capabilities().check(center_hz, sample_rate_hz)
        self._cfg = (int(center_hz), int(sample_rate_hz), gain_db)
        self._start()

    def retune(self, center_hz) -> None:
        if self._cfg is None:
            raise SdrError("Device not configured")
        self._cfg = (int(center_hz), self._cfg[1], self._cfg[2])
        self._stop()
        self._start()

    def read(self, num_samples: int) -> np.ndarray:
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise SdrError("rtl_sdr is not running")
        want = num_samples * 2
        data = proc.stdout.read(want)
        if len(data) < want:
            self._raise_exit(proc)
        raw = np.frombuffer(data, dtype=np.uint8)
        iq = (raw.astype(np.float32) - 127.5) / 127.5
        return (iq[0::2] + 1j * iq[1::2]).astype(np.complex64)

    def close(self) -> None:
        self._stop()

    # ---- belső --------------------------------------------------------------------------------------------------

    def _start(self) -> None:
        assert self._cfg is not None
        freq, rate, gain = self._cfg
        cmd = [*self.command, "-f", str(freq), "-s", str(rate), "-d", str(self.device_index)]
        if gain is not None:
            cmd += ["-g", f"{gain:g}"]            # gain nélkül az rtl_sdr automatikus erősítést használ
        if self.ppm:
            cmd += ["-p", str(self.ppm)]
        cmd.append("-")                           # kimenet: stdout
        self._stderr.clear()
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1 << 20)
        except OSError as exc:
            raise SdrError(f"Could not start rtl_sdr ({self.command[0]}): {exc}. Install the rtl-sdr tools or set "
                           "tools.rtl_sdr_path in config/local.yaml") from exc
        threading.Thread(target=self._drain_stderr, args=(self._proc,), daemon=True).start()

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        for line in iter(proc.stderr.readline, b""):
            self._stderr.append(line.decode("utf-8", "replace").rstrip())

    def _stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        for stream in (proc.stdout, proc.stderr):
            if stream:
                stream.close()

    def _raise_exit(self, proc: subprocess.Popen) -> None:
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        text = " | ".join(l for l in self._stderr if l)
        hint = ""
        if "usb_claim_interface" in text or "Failed to open" in text or "No supported devices" in text:
            hint = (" - the RTL-SDR cannot be opened: close other SDR programs; on Windows install the WinUSB driver "
                    "with Zadig, on Linux the udev rule")
        raise SdrError(f"rtl_sdr stopped (exit code {proc.returncode}): {text or 'no message'}{hint}")
