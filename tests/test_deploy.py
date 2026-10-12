"""10. fázis: telepítési fájlok (systemd, udev, szkriptek, kiadási csomag, konfigurációs példa)."""
import configparser
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

from satrx.core.config import PROJECT_ROOT, Settings, load_settings

DEPLOY = PROJECT_ROOT / "deploy"
SCRIPTS = PROJECT_ROOT / "scripts"


def find_bash() -> str | None:
    """Működő bash (a Windowson a WSL-indító System32\\bash.exe disztribúció nélkül nem használható)."""
    for cand in [shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"]:
        if not cand or not Path(cand).exists():
            continue
        try:
            out = subprocess.run([cand, "-c", "echo ok"], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0 and out.stdout.strip() == "ok":
            return cand
    return None


BASH = find_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="nincs használható bash")


def bash(script: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([BASH, str(script), *args], capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")


# ---- systemd-egység, udev, modul-tiltólista ------------------------------------------------------------------------

def test_systemd_unit_is_a_watchdog_supervised_notify_service():
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read(DEPLOY / "satrx.service", encoding="utf-8")
    svc = cp["Service"]
    assert svc["Type"] == "notify" and svc["NotifyAccess"] == "main"
    assert int(svc["WatchdogSec"]) >= 60                                    # az életjel a tick (2 s) és a megőrzés miatt bőven belefér
    assert svc["Restart"] == "on-failure" and int(svc["TimeoutStopSec"]) >= 30
    assert svc["ExecStart"].endswith("/.venv/bin/python -m satrx") and svc["User"] == "@USER@"
    assert "SATRX_DATA_DIR=@DATA_DIR@" in svc["Environment"]
    assert svc["NoNewPrivileges"] == "true" and svc["ProtectSystem"] == "full"
    assert svc["SupplementaryGroups"] == "plugdev"
    assert "network-online.target" in cp["Unit"]["After"] and "time-sync.target" in cp["Unit"]["After"]
    assert cp["Install"]["WantedBy"] == "multi-user.target"
    placeholders = set(re.findall(r"@[A-Z_]+@", (DEPLOY / "satrx.service").read_text(encoding="utf-8")))
    assert placeholders == {"@USER@", "@INSTALL_DIR@", "@DATA_DIR@"}        # csak ezeket tölti ki a telepítő


def test_watchdog_interval_matches_the_unit():
    """A WatchdogSec feléről küldünk életjelet; a háttérszál 2 mp-enként jár, így a tartalék nagy."""
    from satrx import sdnotify
    from satrx.scheduler.service import TICK_S
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.read(DEPLOY / "satrx.service", encoding="utf-8")
    import os
    os.environ["WATCHDOG_USEC"] = str(int(cp["Service"]["WatchdogSec"]) * 1_000_000)
    try:
        assert sdnotify.watchdog_interval() == int(cp["Service"]["WatchdogSec"]) / 2
        assert TICK_S * 10 < sdnotify.watchdog_interval()
    finally:
        del os.environ["WATCHDOG_USEC"]


def test_udev_rule_and_module_blacklist():
    rules = (DEPLOY / "99-satrx-rtlsdr.rules").read_text(encoding="utf-8")
    assert '0bda' in rules and '2838' in rules and '2832' in rules and 'GROUP="plugdev"' in rules and 'MODE="0660"' in rules
    black = (DEPLOY / "blacklist-rtlsdr.conf").read_text(encoding="utf-8")
    assert "blacklist dvb_usb_rtl28xxu" in black


def test_the_shipped_files_satisfy_the_doctor_checks(tmp_path):
    """A mellékelt tiltólistát és udev-szabályt a diagnosztika is felismeri (egy kamu gyökérkönyvtárban telepítve)."""
    from satrx import doctor
    (tmp_path / "etc/modprobe.d").mkdir(parents=True)
    (tmp_path / "etc/udev/rules.d").mkdir(parents=True)
    shutil.copy(DEPLOY / "blacklist-rtlsdr.conf", tmp_path / "etc/modprobe.d/blacklist-rtlsdr.conf")
    shutil.copy(DEPLOY / "99-satrx-rtlsdr.rules", tmp_path / "etc/udev/rules.d/99-satrx-rtlsdr.rules")
    checks = {c.id: c for c in doctor.check_rtlsdr_linux("Linux", lambda a: (0, "usbcore 1 0"), tmp_path)}
    assert checks["dvb_module"].status == doctor.OK and checks["udev"].status == doctor.OK


# ---- konfigurációs példa -----------------------------------------------------------------------------------------------

def test_config_example_loads_into_settings(tmp_path):
    (tmp_path / "default.yaml").write_text((PROJECT_ROOT / "config" / "default.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "local.yaml").write_text((PROJECT_ROOT / "config" / "local.yaml.example").read_text(encoding="utf-8"), encoding="utf-8")
    s = load_settings(tmp_path)
    assert s.sdr.backend == "rtlsdr" and s.server.port == 8000 and s.storage.artifact_retention_days == 0
    assert s.observer.is_set and s.storage.iq_retention_days == 7


def test_the_config_generated_by_the_installer_is_valid():
    """A telepítő által írt YAML (a szkript heredoc-ja) érvényes konfiguráció."""
    script = (SCRIPTS / "install_pi.sh").read_text(encoding="utf-8")
    body = re.search(r"<<EOF\n(.*?)\nEOF", script, flags=re.S).group(1)
    rendered = body.replace("$DATA_DIR", "/var/lib/satrx").replace("$PORT", "8000").replace("$TOKEN", "tok3n")
    cfg = yaml.safe_load(rendered)
    s = Settings(**cfg)
    assert s.sdr.backend == "rtlsdr" and s.server.auth_token == "tok3n" and s.server.host == "0.0.0.0"
    assert str(s.data_dir).replace("\\", "/") == "/var/lib/satrx"


# ---- Linux telepítő ----------------------------------------------------------------------------------------------------

@needs_bash
@pytest.mark.parametrize("name", ["install_pi.sh", "fstab_line.sh"])
def test_shell_scripts_have_valid_syntax(name):
    out = subprocess.run([BASH, "-n", str(SCRIPTS / name)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


@needs_bash
def test_install_dry_run_lists_every_step_and_changes_nothing(tmp_path):
    out = bash(SCRIPTS / "install_pi.sh", "--dry-run", "--dir", str(tmp_path / "opt"), "--data-dir", str(tmp_path / "var"), "--port", "8123")
    assert out.returncode == 0, out.stderr
    text = out.stdout
    for expected in ("apt-get install -y", "rtl-sdr", "chrony", "useradd --system", "--groups plugdev", "python3 -m venv",
                     'install -e', "[rtlsdr]", "99-satrx-rtlsdr.rules", "blacklist-rtlsdr.conf", "udevadm control --reload-rules",
                     "modprobe -r dvb_usb_rtl28xxu", "render deploy/satrx.service", "systemctl enable satrx", "systemctl restart satrx",
                     "satrx doctor", "http://", ":8123", "<generált-token>"):
        assert expected in text, expected
    assert not (tmp_path / "opt").exists() and not (tmp_path / "var").exists()           # a szárazfutás nem hoz létre semmit
    steps = [l for l in text.splitlines() if l.startswith("+ ")]
    order = [next(i for i, l in enumerate(steps) if key in l) for key in
             ("apt-get install", "useradd", "rsync -a", "-m venv", "pip install -e", "write ", "udevadm control", "systemctl enable")]
    assert order == sorted(order)                                                          # a lépések sorrendje logikus


@needs_bash
def test_install_flags_change_the_plan(tmp_path):
    base = ["--dry-run", "--dir", str(tmp_path / "o"), "--data-dir", str(tmp_path / "d")]
    update = bash(SCRIPTS / "install_pi.sh", *base, "--update").stdout
    assert "apt-get" not in update and "systemctl restart satrx" in update                  # frissítésnél nincs csomagtelepítés
    nosd = bash(SCRIPTS / "install_pi.sh", *base, "--no-systemd").stdout
    assert "systemctl enable satrx" not in nosd and "--no-systemd" in nosd
    web = bash(SCRIPTS / "install_pi.sh", *base, "--build-web").stdout
    assert "npm install" in web and "nodejs" in web
    deb = bash(SCRIPTS / "install_pi.sh", *base, "--satdump-deb", "https://example.org/satdump.deb").stdout
    assert "curl -fL -o /tmp/satdump.deb https://example.org/satdump.deb" in deb and "apt-get install -y /tmp/satdump.deb" in deb
    un = bash(SCRIPTS / "install_pi.sh", *base, "--uninstall").stdout
    assert "systemctl disable --now satrx" in un and "rm -rf" not in un                     # adat nélkül nem töröl
    purge = bash(SCRIPTS / "install_pi.sh", *base, "--uninstall", "--purge-data").stdout
    assert f"rm -rf {tmp_path / 'd'}".replace("\\", "/") in purge.replace("\\", "/")
    assert bash(SCRIPTS / "install_pi.sh", "--bogus").returncode != 0
    helped = bash(SCRIPTS / "install_pi.sh", "--help")
    assert helped.returncode == 0 and "sudo ./scripts/install_pi.sh" in helped.stdout and "--dry-run" in helped.stdout


@needs_bash
def test_install_refuses_to_run_without_root_or_off_linux():
    out = bash(SCRIPTS / "install_pi.sh")                                                   # nincs --dry-run
    assert out.returncode != 0 and ("Linux" in out.stderr or "root" in out.stderr.lower())


@needs_bash
def test_fstab_helper_prints_a_stable_uuid_based_line():
    out = bash(SCRIPTS / "fstab_line.sh", "--uuid", "1234-ABCD", "--type", "ext4", "/mnt/satrx")
    assert out.returncode == 0
    assert "UUID=1234-ABCD /mnt/satrx ext4 defaults,nofail,x-systemd.device-timeout=10 0 2" in out.stdout
    exfat = bash(SCRIPTS / "fstab_line.sh", "--uuid", "AAAA-BBBB", "--type", "exfat", "/mnt/usb")
    assert "uid=satrx,gid=satrx,umask=007" in exfat.stdout                                  # nem POSIX fájlrendszeren csatolási opcióval
    assert "uid=" not in out.stdout
    assert bash(SCRIPTS / "fstab_line.sh", "--uuid", "1", "--type", "ext4", "relative/dir").returncode != 0
    assert bash(SCRIPTS / "fstab_line.sh", "/dev/sda1").returncode != 0                    # hiányzik a csatolási pont
    applied = bash(SCRIPTS / "fstab_line.sh", "--uuid", "1234-ABCD", "--type", "ext4", "/mnt/satrx", "--apply", "--dry-run")
    assert applied.returncode == 0 and "cp /etc/fstab /etc/fstab.satrx-backup-" in applied.stdout     # módosítás előtt mentés
    assert "chown satrx:satrx /mnt/satrx" in applied.stdout and "mount /mnt/satrx" in applied.stdout
    assert "mount" not in out.stdout.split("Javasolt")[1].split("Alkalmazás")[0]            # alapból nem módosít


# ---- Windows telepítő ------------------------------------------------------------------------------------------------------

POWERSHELL = shutil.which("powershell")
needs_powershell = pytest.mark.skipif(sys.platform != "win32" or POWERSHELL is None, reason="Windows PowerShell kell")


def test_windows_installer_is_saved_with_a_bom_for_powershell_5():
    """A BOM nélküli UTF-8 fájlt a Windows PowerShell 5.1 ANSI-ként olvasná, és az ékezetes szövegek elromlanának."""
    assert (SCRIPTS / "install_windows.ps1").read_bytes().startswith(b"\xef\xbb\xbf")


@needs_powershell
def test_windows_installer_parses_and_dry_runs(tmp_path):
    parse = subprocess.run([POWERSHELL, "-NoProfile", "-Command",
                            "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                            f"'{SCRIPTS / 'install_windows.ps1'}',[ref]$null,[ref]$e); if($e){{$e|%{{$_.Message}}; exit 1}}"],
                           capture_output=True, text=True, timeout=60)
    assert parse.returncode == 0, parse.stdout
    # tiszta, ideiglenes projektmásolatban futtatjuk: a szkript a saját szülőmappáját tekinti a projektnek, így a teszt nem
    # függ attól, hogy a fejlesztő gépén van-e már config/local.yaml vagy web/dist
    proj = tmp_path / "proj"
    (proj / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPTS / "install_windows.ps1", proj / "scripts" / "install_windows.ps1")
    run = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(proj / "scripts" / "install_windows.ps1"),
                          "-DryRun", "-Backend", "rtlsdr", "-SkipDoctor"], capture_output=True, text=True, timeout=60,
                         encoding="utf-8", errors="replace")
    assert run.returncode == 0, run.stderr
    assert "-m venv .venv" in run.stdout and 'install -e ".[rtlsdr]"' in run.stdout and "backend: rtlsdr" in run.stdout
    assert "run_satrx.cmd" in run.stdout and "Zadig" in run.stdout


# ---- kiadási csomag ----------------------------------------------------------------------------------------------------------

def test_release_archive_contains_the_app_and_the_built_web_but_no_junk(tmp_path):
    if not (PROJECT_ROOT / "web" / "dist" / "index.html").is_file():
        pytest.skip("a webes felület nincs megépítve")
    out = subprocess.run([sys.executable, str(SCRIPTS / "make_release.py"), "--no-build", "--out", str(tmp_path)],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    archive = next(tmp_path.glob("satrx-*.tar.gz"))
    ver = archive.name.removeprefix("satrx-").removesuffix(".tar.gz")
    with tarfile.open(archive) as tar:
        names = tar.getnames()
        modes = {m.name: m.mode for m in tar.getmembers()}
    root = f"satrx-{ver}/"
    for needed in ("pyproject.toml", "src/satrx/__init__.py", "src/satrx/api/app.py", "src/satrx/migrations/versions/0004_storage_roots_protection.py",
                   "web/dist/index.html", "config/default.yaml", "config/local.yaml.example", "deploy/satrx.service",
                   "deploy/99-satrx-rtlsdr.rules", "scripts/install_pi.sh", "scripts/install_windows.ps1", "docs/RASPBERRY_PI.md", "README.md"):
        assert root + needed in names, needed
    assert any(n.startswith(root + "web/dist/assets/") for n in names)
    assert not [n for n in names if any(f in n for f in ("__pycache__", ".venv", "node_modules", ".pytest_cache"))]
    assert root + "config/local.yaml" not in names and not [n for n in names if n.startswith(root + "data/")]
    assert not [n for n in names if n.endswith((".pyc", ".db", ".sigmf-data"))]
    assert modes[root + "scripts/install_pi.sh"] & 0o111                                      # futtatható
    assert modes[root + "scripts/fstab_line.sh"] & 0o111
    assert str(archive) in out.stdout                                                         # a kimenet megmondja, hol a csomag


def test_release_version_matches_the_package():
    import importlib.util
    spec = importlib.util.spec_from_file_location("make_release", SCRIPTS / "make_release.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from satrx import __version__
    assert mod.version() == __version__
