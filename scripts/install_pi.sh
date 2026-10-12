#!/usr/bin/env bash
# satrx telepítő Raspberry Pi OS (Bookworm+), Debian és Ubuntu rendszerre. Többször futtatható (idempotens).
#
#   sudo ./scripts/install_pi.sh                 # telepítés /opt/satrx alá, systemd szolgáltatással
#   sudo ./scripts/install_pi.sh --update        # frissítés: a csomagokat kihagyja, a beállításokat megtartja
#   sudo ./scripts/install_pi.sh --dry-run       # csak kiírja, mit tenne (root és Linux nélkül is futtatható)
#
# Beállítások: --user NAME (satrx) --dir PATH (/opt/satrx) --data-dir PATH (/var/lib/satrx) --port N (8000)
#              --no-systemd  --build-web  --satdump-deb URL  --uninstall  --purge-data
set -euo pipefail

SERVICE_USER="satrx"
INSTALL_DIR="/opt/satrx"
DATA_DIR="/var/lib/satrx"
PORT=8000
NO_SYSTEMD=0
BUILD_WEB=0
SATDUMP_DEB=""
DRY_RUN=0
UPDATE=0
UNINSTALL=0
PURGE_DATA=0
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

# Parancs futtatása; --dry-run esetén csak kiírja.
run()    { if (( DRY_RUN )); then printf '+ %s\n' "$*"; else "$@"; fi; }
# Mint a run, de a hibát nem veszi komolyan (pl. modprobe -r, ha a modul nincs betöltve).
run_ok() { if (( DRY_RUN )); then printf '+ %s   (hiba esetén folytatja)\n' "$*"; else "$@" || true; fi; }
# Fájl írása a standard bemenetről: write_file <út> <mód>
write_file() {
    if (( DRY_RUN )); then printf '+ write %s (mode %s)\n' "$1" "$2"; cat >/dev/null
    else cat > "$1"; chmod "$2" "$1"; fi
}

while (( $# )); do
    case "$1" in
        --user) SERVICE_USER="$2"; shift 2 ;;
        --dir) INSTALL_DIR="$2"; shift 2 ;;
        --data-dir) DATA_DIR="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --no-systemd) NO_SYSTEMD=1; shift ;;
        --build-web) BUILD_WEB=1; shift ;;
        --satdump-deb) SATDUMP_DEB="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --update) UPDATE=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        --purge-data) PURGE_DATA=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "Ismeretlen kapcsoló: $1 (lásd --help)" ;;
    esac
done

if (( ! DRY_RUN )); then
    [[ "$(uname -s)" == "Linux" ]] || die "Ez a szkript Linuxra (Raspberry Pi OS / Debian / Ubuntu) készült. Windowsra: scripts/install_windows.ps1"
    [[ $EUID -eq 0 ]] || die "Root jogosultság kell: sudo $0 $*"
    command -v apt-get >/dev/null || die "Csak az apt-alapú rendszereket támogatja (Raspberry Pi OS, Debian, Ubuntu)."
fi

# ---- eltávolítás ------------------------------------------------------------------------------------------------
if (( UNINSTALL )); then
    log "Szolgáltatás leállítása és eltávolítása"
    run_ok systemctl disable --now satrx
    run rm -f /etc/systemd/system/satrx.service
    run_ok systemctl daemon-reload
    run rm -f /etc/udev/rules.d/99-satrx-rtlsdr.rules
    if (( PURGE_DATA )); then
        log "Az adatok törlése: $DATA_DIR"
        run rm -rf "$DATA_DIR"
    else
        log "Az adatok megmaradtak: $DATA_DIR (törlés: --uninstall --purge-data)"
    fi
    log "A programkönyvtár ($INSTALL_DIR), a felhasználó ($SERVICE_USER) és a modul-tiltólista megmaradt; kézzel törölhető."
    exit 0
fi

# ---- 1. csomagok ----------------------------------------------------------------------------------------------------
if (( ! UPDATE )); then
    log "Rendszercsomagok telepítése"
    run apt-get update
    run apt-get install -y --no-install-recommends python3 python3-venv python3-pip rtl-sdr librtlsdr0 \
        libusb-1.0-0 chrony curl ca-certificates rsync
fi
if (( ! DRY_RUN )); then
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
        || die "Python 3.11 vagy újabb kell (Raspberry Pi OS Bookworm vagy újabb)."
fi

# ---- 2. felhasználó ---------------------------------------------------------------------------------------------------
log "Szolgáltatás-felhasználó: $SERVICE_USER"
if (( DRY_RUN )) || ! getent group plugdev >/dev/null; then run_ok groupadd --system plugdev; fi
if (( DRY_RUN )) || ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    run useradd --system --home-dir "$INSTALL_DIR" --no-create-home --shell /usr/sbin/nologin \
        --groups plugdev "$SERVICE_USER"
else
    run_ok usermod -aG plugdev "$SERVICE_USER"
fi

# ---- 3. program -----------------------------------------------------------------------------------------------------------
if [[ "$SRC_DIR" != "$INSTALL_DIR" ]]; then
    log "Program másolása: $SRC_DIR -> $INSTALL_DIR"
    run mkdir -p "$INSTALL_DIR"
    run rsync -a --delete \
        --exclude '/.git' --exclude '/.venv' --exclude '/data' --exclude '__pycache__' --exclude '*.egg-info' \
        --exclude '/web/node_modules' --exclude '/config/local.yaml' --exclude '/tests' \
        "$SRC_DIR/" "$INSTALL_DIR/"
fi

# ---- 4. webes felület --------------------------------------------------------------------------------------------------------
if [[ ! -f "$INSTALL_DIR/web/dist/index.html" ]]; then
    if (( BUILD_WEB )); then
        log "A webes felület építése (Node.js)"
        run apt-get install -y --no-install-recommends nodejs npm
        run bash -c "cd '$INSTALL_DIR/web' && npm install --no-audit --no-fund && npm run build"
    else
        warn "Hiányzik a web/dist (a felület nincs megépítve)."
        warn "Használj kiadási csomagot (scripts/make_release.py), vagy futtasd újra --build-web kapcsolóval."
        (( DRY_RUN )) || die "A webes felület nélkül a szolgáltatás használhatatlan."
    fi
fi

# ---- 5. Python környezet -------------------------------------------------------------------------------------------------------
log "Python virtuális környezet és függőségek"
run python3 -m venv "$INSTALL_DIR/.venv"
run "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
# szerkeszthető telepítés: a program a konfigurációt és a web/dist mappát a saját könyvtárából olvassa
run "$INSTALL_DIR/.venv/bin/pip" install -e "$INSTALL_DIR[rtlsdr]"

# ---- 6. konfiguráció ---------------------------------------------------------------------------------------------------------------
run mkdir -p "$INSTALL_DIR/config" "$DATA_DIR"
if [[ -f "$INSTALL_DIR/config/local.yaml" ]]; then
    log "A meglévő konfiguráció megmarad: $INSTALL_DIR/config/local.yaml"
    TOKEN="(a meglévő konfigurációban)"
else
    log "Konfiguráció létrehozása (véletlen API-tokennel)"
    if (( DRY_RUN )); then TOKEN="<generált-token>"
    else TOKEN="$("$INSTALL_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(24))')"; fi
    write_file "$INSTALL_DIR/config/local.yaml" 640 <<EOF
# A telepítő hozta létre; szabadon szerkeszthető (példa: config/local.yaml.example)
data_dir: $DATA_DIR
server:
  host: 0.0.0.0
  port: $PORT
  auth_token: "$TOKEN"
sdr:
  backend: rtlsdr
EOF
fi
run chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
run chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
run chmod 750 "$INSTALL_DIR/config"

# ---- 7. RTL-SDR: udev-szabály és a DVB-modul tiltása -----------------------------------------------------------------------------------
log "RTL-SDR beállítás (udev-szabály, kernelmodul tiltása)"
run install -m 644 "$INSTALL_DIR/deploy/99-satrx-rtlsdr.rules" /etc/udev/rules.d/99-satrx-rtlsdr.rules
run install -m 644 "$INSTALL_DIR/deploy/blacklist-rtlsdr.conf" /etc/modprobe.d/blacklist-rtlsdr.conf
run_ok udevadm control --reload-rules
run_ok udevadm trigger
run_ok modprobe -r dvb_usb_rtl28xxu

# ---- 8. pontos idő ------------------------------------------------------------------------------------------------------------------------
log "Pontos idő (az átvonulások időzítése miatt fontos)"
run_ok systemctl enable --now chrony
run_ok timedatectl set-ntp true

# ---- 9. systemd -----------------------------------------------------------------------------------------------------------------------------
if (( NO_SYSTEMD )); then
    log "A systemd szolgáltatás kimarad (--no-systemd). Kézi indítás: sudo -u $SERVICE_USER $INSTALL_DIR/.venv/bin/python -m satrx"
else
    log "systemd szolgáltatás telepítése és indítása"
    if (( DRY_RUN )); then
        printf '+ render deploy/satrx.service -> /etc/systemd/system/satrx.service (USER=%s INSTALL_DIR=%s DATA_DIR=%s)\n' \
            "$SERVICE_USER" "$INSTALL_DIR" "$DATA_DIR"
    else
        sed -e "s|@USER@|$SERVICE_USER|g" -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" -e "s|@DATA_DIR@|$DATA_DIR|g" \
            "$INSTALL_DIR/deploy/satrx.service" > /etc/systemd/system/satrx.service
        chmod 644 /etc/systemd/system/satrx.service
    fi
    run systemctl daemon-reload
    run systemctl enable satrx
    run systemctl restart satrx
fi

# ---- 10. SatDump ------------------------------------------------------------------------------------------------------------------------------
if [[ -n "$SATDUMP_DEB" ]]; then
    log "SatDump telepítése: $SATDUMP_DEB"
    run curl -fL -o /tmp/satdump.deb "$SATDUMP_DEB"
    run apt-get install -y /tmp/satdump.deb
elif (( ! DRY_RUN )) && ! command -v satdump >/dev/null; then
    warn "A SatDump nincs telepítve (csak a Meteor LRPT dekódoláshoz kell)."
    warn "Telepítés: töltsd le a Raspberry Pi (arm64) csomagot a https://github.com/SatDump/SatDump/releases oldalról,"
    warn "majd: sudo ./scripts/install_pi.sh --update --satdump-deb <letöltési-url>  (vagy sudo apt install ./satdump.deb)"
fi

# ---- 11. ellenőrzés ----------------------------------------------------------------------------------------------------------------------------
log "Ellenőrzés (satrx doctor)"
if (( DRY_RUN )); then
    printf '+ sudo -u %s %s/.venv/bin/python -m satrx doctor\n' "$SERVICE_USER" "$INSTALL_DIR"
else
    sudo -u "$SERVICE_USER" env SATRX_DATA_DIR="$DATA_DIR" "$INSTALL_DIR/.venv/bin/python" -m satrx doctor || true
fi

HOST_IP="$( (hostname -I 2>/dev/null || echo localhost) | awk '{print $1}')"
echo
log "Kész."
echo "    Webes felület:  http://$HOST_IP:$PORT"
echo "    API-token:      $TOKEN   (a böngésző elsőre kéri; a konfigurációban: $INSTALL_DIR/config/local.yaml)"
echo "    Állapot:        systemctl status satrx      Napló: journalctl -u satrx -f"
echo "    Külső meghajtó: lásd docs/RASPBERRY_PI.md és scripts/fstab_line.sh"
