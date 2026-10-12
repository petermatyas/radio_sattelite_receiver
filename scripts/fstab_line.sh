#!/usr/bin/env bash
# Stabil (UUID-alapú) fstab-sor egy külső meghajtóhoz (SSD, USB-meghajtó), hogy újraindítás után is ugyanoda csatolódjon.
#
#   ./scripts/fstab_line.sh /dev/sda1 /mnt/satrx            # csak kiírja a javasolt sort
#   sudo ./scripts/fstab_line.sh /dev/sda1 /mnt/satrx --apply [--owner satrx]
#   ./scripts/fstab_line.sh --uuid 1234-ABCD --type ext4 /mnt/satrx   # blkid nélkül (próba)
#
# A sor `nofail` és `x-systemd.device-timeout` opciót kap: ha a meghajtó nincs a gépben, a Pi attól még elindul
# (a satrx ilyenkor hibát jelez, és nem ír a rendszermeghajtóra: a mentési mappa jelzőfájlja hiányzik).
# Ext4 ajánlott: ott a tulajdonos beállítható (--owner). NTFS/exFAT/FAT esetén a tulajdonost a csatolási opciók adják.
set -euo pipefail

DEVICE=""; MOUNTPOINT=""; UUID=""; FSTYPE=""; APPLY=0; OWNER="satrx"; DRY_RUN=0

usage() { sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
die() { printf 'xx  %s\n' "$*" >&2; exit 1; }

while (( $# )); do
    case "$1" in
        --apply) APPLY=1; shift ;;
        --owner) OWNER="$2"; shift 2 ;;
        --uuid) UUID="$2"; shift 2 ;;
        --type) FSTYPE="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) die "Ismeretlen kapcsoló: $1" ;;
        *) if [[ -z "$DEVICE" && -z "$UUID" && "$1" == /dev/* ]]; then DEVICE="$1"; else MOUNTPOINT="$1"; fi; shift ;;
    esac
done

[[ -n "$MOUNTPOINT" ]] || { usage; die "Hiányzik a csatolási pont (pl. /mnt/satrx)."; }
[[ "$MOUNTPOINT" == /* ]] || die "A csatolási pont abszolút útvonal legyen."

if [[ -z "$UUID" ]]; then
    [[ -n "$DEVICE" ]] || die "Add meg az eszközt (pl. /dev/sda1) vagy a --uuid értéket."
    command -v blkid >/dev/null || die "A blkid nem található."
    eval "$(blkid -s UUID -s TYPE -o export "$DEVICE")" || die "A $DEVICE nem olvasható (root kell?)."
    UUID="${UUID:-}"; FSTYPE="${TYPE:-$FSTYPE}"
fi
[[ -n "$UUID" ]] || die "Nem sikerült az UUID-t megállapítani."
[[ -n "$FSTYPE" ]] || die "Nem sikerült a fájlrendszer típusát megállapítani (add meg: --type ext4)."

OPTIONS="defaults,nofail,x-systemd.device-timeout=10"
case "$FSTYPE" in
    ntfs|ntfs3|exfat|vfat) OPTIONS="$OPTIONS,uid=$OWNER,gid=$OWNER,umask=007" ;;
esac
LINE="UUID=$UUID $MOUNTPOINT $FSTYPE $OPTIONS 0 2"

echo "Javasolt /etc/fstab sor:"
echo "    $LINE"
if (( ! APPLY )); then
    echo
    if [[ -n "$DEVICE" ]]; then echo "Alkalmazás: sudo $0 $DEVICE $MOUNTPOINT --apply"
    else echo "Alkalmazás: sudo $0 --uuid $UUID --type $FSTYPE $MOUNTPOINT --apply"; fi
    echo "Utána a webes felület Tárhely oldalán add meg a mappát (pl. $MOUNTPOINT/recordings)."
    exit 0
fi

run() { if (( DRY_RUN )); then printf '+ %s\n' "$*"; else "$@"; fi; }
if (( ! DRY_RUN )); then [[ $EUID -eq 0 ]] || die "Az --apply root jogosultságot kér (sudo)."; fi

if (( ! DRY_RUN )) && grep -q "UUID=$UUID" /etc/fstab 2>/dev/null; then
    echo "Az UUID már szerepel az /etc/fstab fájlban, nem módosítom."
else
    run cp /etc/fstab "/etc/fstab.satrx-backup-$(date +%Y%m%d%H%M%S)"
    if (( DRY_RUN )); then printf '+ append to /etc/fstab: %s\n' "$LINE"; else printf '%s\n' "$LINE" >> /etc/fstab; fi
fi
run mkdir -p "$MOUNTPOINT"
run mount "$MOUNTPOINT"
case "$FSTYPE" in
    ntfs|ntfs3|exfat|vfat) ;;
    *) run chown "$OWNER:$OWNER" "$MOUNTPOINT" ;;
esac
echo "Kész: $MOUNTPOINT csatolva. A satrx Tárhely oldalán add meg a felvételek mappáját (pl. $MOUNTPOINT/recordings)."
