"""Kiadási csomag (tar.gz) készítése, benne a megépített webes felülettel: a Pi-n így nem kell Node.js.

    python scripts/make_release.py              # megépíti a webes felületet, és dist/satrx-<verzió>.tar.gz készül
    python scripts/make_release.py --no-build   # a meglévő web/dist használata
    python scripts/make_release.py --out C:\\temp

A csomag kicsomagolva a telepítőszkripttel telepíthető: sudo ./scripts/install_pi.sh
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INCLUDE = ["src", "config/default.yaml", "config/local.yaml.example", "deploy", "scripts", "docs", "web/dist", "web/package.json",
           "plugins", "pyproject.toml", "alembic.ini", "README.md", "TERV.md"]
EXCLUDE_PARTS = {"__pycache__", ".venv", "node_modules", ".pytest_cache", "data"}
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".egg-info")


def version() -> str:
    text = (ROOT / "src" / "satrx" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else "0.0.0"


def build_web() -> None:
    npm = shutil.which("npm")
    if npm is None:
        sys.exit("Nincs npm: építsd meg a webes felületet kézzel, vagy használd a --no-build kapcsolót.")
    subprocess.run([npm, "install", "--no-audit", "--no-fund"], cwd=ROOT / "web", check=True)
    subprocess.run([npm, "run", "build"], cwd=ROOT / "web", check=True)


def files() -> list[Path]:
    out = []
    for item in INCLUDE:
        path = ROOT / item
        if not path.exists():
            continue
        candidates = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        for p in candidates:
            parts = set(p.relative_to(ROOT).parts)
            if parts & EXCLUDE_PARTS or p.name.endswith(EXCLUDE_SUFFIXES) or any(x.endswith(".egg-info") for x in parts):
                continue
            out.append(p)
    return out


def make_archive(out_dir: Path) -> Path:
    if not (ROOT / "web" / "dist" / "index.html").is_file():
        sys.exit("A web/dist hiányzik: futtasd build nélkül ne; használd az alapértelmezett (építő) módot.")
    ver = version()
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"satrx-{ver}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for p in files():
            info = tar.gettarinfo(str(p), arcname=f"satrx-{ver}/{p.relative_to(ROOT).as_posix()}")
            if p.suffix in (".sh", ".py") and p.parent.name == "scripts":
                info.mode = 0o755                                   # a telepítő szkriptek futtathatók legyenek
            with p.open("rb") as fh:
                tar.addfile(info, fh)
    return archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-build", action="store_true", help="ne építse meg a webes felületet")
    parser.add_argument("--out", type=Path, default=ROOT / "dist", help="a kimeneti mappa (alap: dist)")
    args = parser.parse_args(argv)
    if not args.no_build:
        build_web()
    archive = make_archive(args.out)
    print(f"Elkészült: {archive} ({archive.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
