"""Hosszútávú (soak) teszt: a valódi szolgáltatást futtatja órákon át, folyamatos terheléssel, és figyeli az erőforrásokat.

    python scripts/soak_test.py --minutes 5                 # gyors próba
    python scripts/soak_test.py --minutes 480 --report soak.json   # éjszakai futás a Pi-n

Mit csinál:
  * külön folyamatban elindítja a szolgáltatást (mock SDR valós idejű ütemezéssel, ideiglenes adatkönyvtár, kis kvóta);
  * ciklusonként felvételt indít, feldolgoztatja (FM + waterfall), és közben lekérdezi az API-t;
  * egy SSE- és egy WebSocket-kliens folyamatosan csatlakozva marad (az élő nézet terhelése);
  * a kis kvóta miatt a megőrzés (törlés) is lefut;
  * 5 másodpercenként méri a memóriát (RSS), a szálakat, a nyitott fájlokat, a CPU-t és a lemezhasználatot.
A végén összegzi, és a küszöbök átlépésekor nem nulla kilépési kóddal tér vissza (memóriaszivárgás, szálszivárgás,
megszakadt ciklusok, hibaesemények).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def venv_python() -> str:
    for cand in (ROOT / ".venv" / "Scripts" / "python.exe", ROOT / ".venv" / "bin" / "python"):
        if cand.exists():
            return str(cand)
    return sys.executable


class Api:
    def __init__(self, base: str):
        self.base = base
        self.errors = 0

    def call(self, method: str, path: str, body=None, timeout: float = 60):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, method=method, data=data, headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            self.errors += 1
            return {"_http_error": exc.code, "_body": exc.read().decode(errors="replace")[:200]}
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            return {"_error": str(exc)}


class StreamClients:
    """SSE- és WebSocket-kliens a háttérben; számolják az üzeneteket és az üzenetek közötti legnagyobb szünetet."""

    def __init__(self, base: str, stop: threading.Event):
        self.base, self.stop = base, stop
        self.sse_messages = 0
        self.sse_max_gap = 0.0
        self.sse_reconnects = 0
        self.ws_rows = 0
        self.ws_reconnects = 0
        self.threads = [threading.Thread(target=self._sse, daemon=True), threading.Thread(target=self._ws, daemon=True)]

    def start(self) -> None:
        for t in self.threads:
            t.start()

    def _sse(self) -> None:
        while not self.stop.is_set():
            try:
                last = time.monotonic()
                with urllib.request.urlopen(self.base + "/api/stream?duration_s=120", timeout=30) as r:
                    for raw in r:
                        if self.stop.is_set():
                            return
                        if raw.startswith(b"event:"):
                            now = time.monotonic()
                            self.sse_max_gap = max(self.sse_max_gap, now - last)
                            last = now
                            self.sse_messages += 1
            except Exception:  # noqa: BLE001
                pass
            self.sse_reconnects += 1
            time.sleep(1)

    def _ws(self) -> None:
        from websockets.sync.client import connect
        url = self.base.replace("http", "ws", 1) + "/api/live/waterfall"
        while not self.stop.is_set():
            try:
                with connect(url, max_size=None) as ws:
                    while not self.stop.is_set():
                        try:
                            msg = ws.recv(timeout=2)
                        except TimeoutError:
                            continue
                        if isinstance(msg, bytes):
                            self.ws_rows += len(msg) // 1024
            except Exception:  # noqa: BLE001
                pass
            self.ws_reconnects += 1
            time.sleep(1)


def dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


_PROCS: dict[int, object] = {}


def sample_process(proc, data_dir: Path) -> dict:
    import psutil
    # a Process-objektumokat megőrizzük, különben a cpu_percent mindig 0-t adna (az első hívás referencia)
    procs = []
    for p in [proc] + proc.children(recursive=True):
        procs.append(_PROCS.setdefault(p.pid, p))
    rss = sum(p.memory_info().rss for p in procs if p.is_running())
    threads = sum(p.num_threads() for p in procs if p.is_running())
    handles = 0
    for p in procs:
        try:
            handles += p.num_fds() if hasattr(p, "num_fds") else p.num_handles()
        except psutil.Error:
            pass
    cpu = sum(p.cpu_percent(interval=None) for p in procs if p.is_running())
    return {"t": time.time(), "rss_mb": rss / 1e6, "threads": threads, "handles": handles, "cpu": cpu,
            "disk_mb": dir_size(data_dir) / 1e6}


def slope_per_hour(samples: list[dict], key: str) -> float:
    """Legkisebb négyzetes meredekség (egység/óra)."""
    if len(samples) < 3:
        return 0.0
    xs = [(s["t"] - samples[0]["t"]) / 3600 for s in samples]
    ys = [s[key] for s in samples]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=float, default=5)
    ap.add_argument("--port", type=int, default=8140)
    ap.add_argument("--cycle-s", type=float, default=12, help="két felvételi ciklus kezdete közötti idő")
    ap.add_argument("--record-s", type=float, default=4)
    ap.add_argument("--max-rss-growth-mb-per-hour", type=float, default=30)
    ap.add_argument("--quota-mb", type=float, default=60, help="kis kvóta: a megőrzés is lefusson")
    ap.add_argument("--artifact-retention-min", type=float, default=3, help="az eredmények törlése ennyi perc után (a soak gyors öregítése)")
    ap.add_argument("--max-thread-growth", type=int, default=6)
    ap.add_argument("--max-handle-growth", type=int, default=40)
    ap.add_argument("--report", type=Path)
    ap.add_argument("--keep-data", action="store_true")
    args = ap.parse_args(argv)

    try:
        import psutil
    except ImportError:
        print("A psutil hiányzik: pip install psutil  (vagy: pip install -e '.[soak]')")
        return 2

    data_dir = Path(tempfile.mkdtemp(prefix="satrx_soak_"))
    env = dict(os.environ, SATRX_DATA_DIR=str(data_dir), SATRX_STORAGE__QUOTA_GB=str(args.quota_mb / 1000), SATRX_STORAGE__IQ_RETENTION_DAYS="0",
               SATRX_STORAGE__ARTIFACT_RETENTION_DAYS=str(args.artifact_retention_min / 1440),
               SATRX_SERVER__HOST="127.0.0.1")
    server = subprocess.Popen([venv_python(), "-m", "satrx", "--port", str(args.port)], cwd=ROOT, env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{args.port}"
    api = Api(base)
    stop = threading.Event()
    samples: list[dict] = []
    cycles = {"ok": 0, "failed": 0}
    failures: list[str] = []
    ps = None
    try:
        for _ in range(120):
            if api.call("GET", "/api/health", timeout=2) and api.errors == 0:
                break
            api.errors = 0
            time.sleep(0.5)
        else:
            print("A szolgáltatás nem indult el.")
            return 2
        ps = psutil.Process(server.pid)
        api.errors = 0

        api.call("PUT", "/api/settings/observer", {"lat": 47.4979, "lon": 19.0402, "alt_m": 100})
        norad = None
        sat = api.call("POST", "/api/satellites", {"norad_id": 25544}, timeout=30)
        if sat and "norad_id" in sat:
            norad = 25544                                    # offline környezetben a felvételek műhold nélkül mennek
        pipelines = {p["name"]: p["id"] for p in api.call("GET", "/api/pipelines")}
        pipeline_id = pipelines["FM audio + waterfall"]
        api.errors = 0                                   # az előkészítés (pl. hálózatfüggő TLE-letöltés) hibái nem számítanak

        clients = StreamClients(base, stop)
        clients.start()
        for p in [ps] + ps.children(recursive=True):
            p.cpu_percent(interval=None)

        def monitor() -> None:
            while not stop.is_set():
                try:
                    samples.append(sample_process(ps, data_dir))
                except psutil.Error:
                    pass
                stop.wait(5)

        threading.Thread(target=monitor, daemon=True).start()

        deadline = time.monotonic() + args.minutes * 60
        n = 0
        print(f"Soak teszt: {args.minutes:g} perc, ciklus {args.cycle_s:g} s, adatkönyvtár {data_dir}")
        while time.monotonic() < deadline:
            started = time.monotonic()
            n += 1
            body = {"center_freq_hz": 145_800_000, "sample_rate_hz": 250_000, "duration_s": args.record_s}
            if norad:
                body["norad_id"] = norad
            rec = api.call("POST", "/api/recorder/start", body)
            if not rec or "id" not in rec:
                cycles["failed"] += 1
                failures.append(f"cycle {n}: start failed {rec}")
            else:
                ok = False
                while time.monotonic() - started < args.record_s + 30:
                    if api.call("GET", "/api/recorder/status")["state"] == "idle":
                        ok = True
                        break
                    time.sleep(0.5)
                job = api.call("POST", f"/api/recordings/{rec['id']}/process", {"pipeline_id": pipeline_id})
                if ok and job and "job_id" in job:
                    for _ in range(120):
                        status = api.call("GET", f"/api/jobs/{job['job_id']}")
                        if status and status.get("status") in ("done", "failed", "no_result"):
                            ok = status["status"] == "done"
                            break
                        time.sleep(0.5)
                    else:
                        ok = False
                else:
                    ok = False
                cycles["ok" if ok else "failed"] += 1
                if not ok:
                    failures.append(f"cycle {n}: recording {rec['id']} did not complete cleanly")
            for path in ("/api/health/detailed", "/api/events?limit=20", "/api/passes?hours=24", "/api/storage",
                         "/api/artifacts?limit=20", "/api/storage/recordings?limit=20", "/api/jobs?limit=10"):
                api.call("GET", path)
            if n % 5 == 0:
                api.call("POST", "/api/storage/retention", {"dry_run": True})
            elapsed = int(time.monotonic() - started)
            if n % 5 == 0 and samples:
                s = samples[-1]
                print(f"  [{n:3d}] ciklus kész ({cycles['ok']} ok / {cycles['failed']} hiba) RSS {s['rss_mb']:.0f} MB, szál {s['threads']}, "
                      f"handle {s['handles']}, lemez {s['disk_mb']:.0f} MB")
            time.sleep(max(0.0, args.cycle_s - (time.monotonic() - started)))

        # a megőrzés percenként fut: egy intervallumnyit várunk, hogy a pillanatnyi kvótatúllépés ne számítson hibának
        print("  várakozás a megőrzés egy futására (65 s)...")
        time.sleep(65)
        summary_events = api.call("GET", "/api/events/summary")
        events = api.call("GET", "/api/events?limit=500") or []
        deleted = [e for e in events if isinstance(e, dict) and e.get("code") == "retention_deleted"]
        health = api.call("GET", "/api/health/detailed")
    finally:
        stop.set()
        try:
            if server.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(server.pid), "/T", "/F"], capture_output=True)
                else:
                    server.terminate()
                    server.wait(timeout=15)
        except Exception:  # noqa: BLE001
            pass

    # ---- kiértékelés --------------------------------------------------------------------------------------------
    warm = samples[len(samples) // 2:] if len(samples) > 10 else samples                  # a második fél: a bemelegedés után
    rss_slope = slope_per_hour(warm, "rss_mb")
    thread_growth = (max(s["threads"] for s in warm) - min(s["threads"] for s in warm)) if warm else 0
    handle_growth = (warm[-1]["handles"] - warm[0]["handles"]) if warm else 0
    errors = (summary_events or {}).get("error", 0)
    report = {
        "minutes": args.minutes, "cycles": cycles, "api_errors": api.errors, "failures": failures[:10],
        "rss_mb": {"start": round(samples[0]["rss_mb"], 1) if samples else None, "end": round(samples[-1]["rss_mb"], 1) if samples else None,
                   "max": round(max(s["rss_mb"] for s in samples), 1) if samples else None,
                   "slope_mb_per_hour": round(rss_slope, 1)},
        "threads": {"min": min(s["threads"] for s in warm) if warm else None, "max": max(s["threads"] for s in warm) if warm else None},
        "handle_growth": handle_growth, "cpu_avg_percent": round(statistics.fmean(s["cpu"] for s in samples), 1) if samples else None,
        "disk_mb_end": round(samples[-1]["disk_mb"], 1) if samples else None,
        "retention_runs": len(deleted), "unacknowledged_error_events": errors,
        "sse": {"messages": clients.sse_messages, "max_gap_s": round(clients.sse_max_gap, 1), "reconnects": clients.sse_reconnects},
        "websocket": {"rows": clients.ws_rows, "reconnects": clients.ws_reconnects},
        "health": (health or {}).get("status"),
    }
    if health and health.get("status") != "ok":
        report["health_components"] = [{"id": c["id"], "status": c["status"], "code": c["code"]} for c in health["components"]
                                       if c["status"] != "ok"]
    problems = []
    if cycles["failed"]:
        problems.append(f"{cycles['failed']} ciklus hibás")
    if errors:
        problems.append(f"{errors} hibaesemény")
    conclusive_memory = args.minutes >= 30                                               # rövid futásból a meredekség nem következtethető ki
    if conclusive_memory and rss_slope > args.max_rss_growth_mb_per_hour:
        problems.append(f"memória-növekedés {rss_slope:.1f} MB/óra (> {args.max_rss_growth_mb_per_hour:g})")
    if thread_growth > args.max_thread_growth:
        problems.append(f"szálszám-ingadozás {thread_growth} (> {args.max_thread_growth})")
    if handle_growth > args.max_handle_growth:
        problems.append(f"nyitott fájlok növekedése {handle_growth} (> {args.max_handle_growth})")
    bad = [c for c in report.get("health_components", []) if c["status"] == "error"]
    if bad:
        problems.append("tartós hiba az állapotellenőrzésben: " + ", ".join(f"{c['id']}:{c['code']}" for c in bad))
    if clients.sse_max_gap > 15:
        problems.append(f"az SSE-folyam {clients.sse_max_gap:.0f} mp-re elhallgatott")
    report["memory_verdict"] = ("leak suspected" if conclusive_memory and rss_slope > args.max_rss_growth_mb_per_hour
                                else "stable" if conclusive_memory else "inconclusive (run at least 30 minutes)")
    report["verdict"] = "FAIL" if problems else "PASS"
    report["problems"] = problems
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.report:
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    if not args.keep_data:
        import shutil
        shutil.rmtree(data_dir, ignore_errors=True)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
