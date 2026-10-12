# Telepítés és üzemeltetés Raspberry Pi 5-ön

> **Állapot:** a telepítőszkriptek szintaxisát és szárazfutását, az üzemeltetési kódot (diagnosztika, összeomlás utáni
> helyreállítás, watchdog) és a hosszútávú tesztkeretet Windowson ellenőriztük. **Valódi Raspberry Pi-n és RTL-SDR
> hardverrel még nem futott.** Az első telepítésnél érdemes a `satrx doctor` kimenetét és a hosszútávú tesztet a Pi-n is
> lefuttatni (lásd lent), és az eltéréseket jelezni.

## 1. Mi kell hozzá

| Elem | Megjegyzés |
|------|------------|
| Raspberry Pi 5 (4 vagy 8 GB) + hivatalos 5 V / 5 A tápegység | az RTL-SDR és egy külső SSD árama miatt fontos a jó táp |
| Raspberry Pi OS **Lite 64-bit** (Bookworm vagy újabb) | Python 3.11+ kell |
| RTL-SDR (V3 / V4) + antenna | 137 MHz (Meteor) és 145,8 MHz (ISS) sávra; az antenna minősége többet számít, mint a szoftver |
| Külső SSD vagy USB-meghajtó (ajánlott) | az IQ felvételek nagyok (10 perc LRPT ≈ 1,2 GB); az SD-kártyát kíméli |
| Hálózat (Ethernet vagy Wi-Fi) | TLE-frissítéshez és a webes felülethez |

## 2. Az operációs rendszer előkészítése

1. Raspberry Pi Imager: *Raspberry Pi OS Lite (64-bit)*; a beállításokban add meg a gépnevet, a felhasználót, az SSH-t, a Wi-Fi-t és az **időzónát**.
2. Első indítás után: `sudo apt update && sudo apt full-upgrade -y && sudo reboot`.
3. Ellenőrizd az időt: `timedatectl` (az `System clock synchronized: yes` sornak igaznak kell lennie). Az átvonulások
   időzítése a pontos órától függ; a telepítő a `chrony`-t is beállítja.

## 3. Telepítés

### A) Kiadási csomaggal (ajánlott, nem kell Node.js a Pi-n)

A fejlesztő gépen (a webes felületet ott építjük meg):

```bash
python scripts/make_release.py              # dist/satrx-<verzió>.tar.gz
scp dist/satrx-*.tar.gz pi@raspberrypi.local:~
```

A Pi-n:

```bash
tar xzf satrx-*.tar.gz && cd satrx-*/
sudo ./scripts/install_pi.sh
```

### B) Forráskódból

```bash
git clone <a-tároló-címe> satrx && cd satrx
sudo ./scripts/install_pi.sh --build-web    # telepíti a Node.js-t, és megépíti a webes felületet
```

A telepítő (többször futtatható):

* telepíti a rendszercsomagokat (`rtl-sdr`, `librtlsdr0`, `chrony`, Python venv ...);
* létrehozza a `satrx` rendszerfelhasználót (a `plugdev` csoporttal);
* a programot az `/opt/satrx` alá másolja, virtuális környezetet és függőségeket telepít;
* létrehozza a `config/local.yaml` fájlt **véletlen API-tokennel**, `sdr.backend: rtlsdr` beállítással;
* beállítja az RTL-SDR-hez az udev-szabályt és a DVB kernelmodul tiltását (`deploy/`);
* telepíti és elindítja a `satrx` systemd szolgáltatást;
* lefuttatja a `satrx doctor` ellenőrzést, és kiírja a webes felület címét és a tokent.

Kapcsolók: `--update` (frissítés: a csomagokat és a konfigurációt nem bántja), `--dry-run` (csak kiírja a lépéseket),
`--no-systemd`, `--port`, `--dir`, `--data-dir`, `--user`, `--uninstall [--purge-data]`, `--satdump-deb URL`.

## 4. Első beállítás a webes felületen

1. Nyisd meg a kiírt címet (`http://<a-pi-címe>:8000`), a böngésző elsőre a tokent kéri.
2. **Beállítások:** add meg a megfigyelési pontot (koordináta). Enélkül nincs átvonulás-számítás.
3. **Műholdak:** vedd fel a NORAD ID-kat (ISS: 25544, Meteor-M2 3: 57166, Meteor-M2 4: 59051).
4. **Ütemezés:** hozz létre szabályt (műhold + időszak + profil + pipeline + prioritás). A profilok és pipeline-ok alapból megvannak.
5. Az **Áttekintés** oldal állapotkártyái mutatják, ha valami nem rendben van (SDR, idő, tárhely, SatDump).

## 5. Az RTL-SDR ellenőrzése

```bash
/opt/satrx/.venv/bin/python -m satrx doctor
```

A tipikus hibák: a `dvb_usb_rtl28xxu` modul foglalja az eszközt (a telepítő tiltja; azonnali hatáshoz
`sudo modprobe -r dvb_usb_rtl28xxu`), vagy hiányzik a jogosultság (az udev-szabály és a `plugdev` csoport kell;
USB-újracsatlakoztatás után él). A `rtl_test` paranccsal (a `rtl-sdr` csomagból) független ellenőrzés végezhető, de
a szolgáltatás futása közben az eszköz foglalt.

## 6. SatDump (a Meteor LRPT képekhez)

A SatDump külön program, csak az LRPT dekódoláshoz kell (az FM és az SSTV nélküle is működik).

1. Töltsd le a Raspberry Pi (arm64) csomagot a <https://github.com/SatDump/SatDump/releases> oldalról.
2. `sudo apt install ./satdump_*.deb` (vagy: `sudo ./scripts/install_pi.sh --update --satdump-deb <URL>`).
3. A `satrx doctor` és a webes Beállítások oldal mutatja, hogy megtalálta-e; ha nem a szokásos helyen van, add meg az útvonalát.

## 7. Külső meghajtó (SSD, USB-meghajtó)

1. Formázd **ext4**-re (a tulajdonos így beállítható), és dugd be.
2. Stabil, UUID-alapú csatolás újraindítás után is:

   ```bash
   lsblk -f                                              # megkeresni az eszközt, pl. /dev/sda1
   ./scripts/fstab_line.sh /dev/sda1 /mnt/satrx          # kiírja a javasolt /etc/fstab sort
   sudo ./scripts/fstab_line.sh /dev/sda1 /mnt/satrx --apply
   ```

   A sor `nofail` és `x-systemd.device-timeout` opciót kap: ha a meghajtó nincs a gépben, a Pi attól még elindul.
3. A webes felület **Tárhely** oldalán add meg a mappákat (pl. `/mnt/satrx/recordings` és `/mnt/satrx/artifacts`),
   az **Ellenőrzés** gomb előbb megmondja, használható-e. Az új adat ide kerül; a régi adat az **Áthelyezés** gombbal vihető át
   (ellenőrző összeggel, megszakíthatóan).

**Ha a meghajtó eltűnik** (kihúzott kábel, áramproblémák): a mentési mappa `.satrx-root` jelzőfájlja hiányzik, ezért a
rendszer **nem ír a rendszermeghajtóra**; az állapotellenőrzés `storage_unavailable` hibát jelez, az új felvételek nem
indulnak, a már mentett adatok pedig „nem elérhető" jelzést kapnak. A meghajtó visszatérésekor minden magától működik.

## 8. Üzemeltetés

```bash
systemctl status satrx            # állapot
sudo systemctl restart satrx      # újraindítás
journalctl -u satrx -f            # napló élőben
/opt/satrx/.venv/bin/python -m satrx doctor
sudo ./scripts/install_pi.sh --update   # frissítés egy újabb csomag kicsomagolása után
```

* **Watchdog:** a szolgáltatás `Type=notify` és `WatchdogSec=180` beállítással fut; ha a háttérszálak megakadnak, a systemd
  újraindítja. Hirtelen leállás (áramkimaradás) után induláskor a rendszer rendbe teszi a félbemaradt állapotokat
  (felvétel, feldolgozás, lezáratlan IQ metaadat), és `recovered_after_crash` figyelmeztetést ír az eseménynaplóba.
  A félbemaradt felvétel IQ-ja feldolgozható marad.
* **Tárhely:** a kvóta (alap 50 GB) és a megőrzési idő (a nyers IQ 7 nap) a `config/local.yaml` `storage:` részében állítható;
  az eredmények alapból nem törlődnek (`artifact_retention_days`). A **védett** felvételt a megőrzés sosem törli.
* **Biztonsági mentés:** az adatbázis és a konfiguráció a fontos, az IQ felvételek újratermelhetők nem:

  ```bash
  sudo -u satrx sqlite3 /var/lib/satrx/satrx.db ".backup '/mnt/satrx/backup-satrx.db'"
  sudo cp /opt/satrx/config/local.yaml /mnt/satrx/
  ```

  Visszaállításhoz állítsd le a szolgáltatást, másold vissza az adatbázist, és indítsd újra.

## 9. Hosszútávú teszt (érdemes a Pi-n is lefuttatni)

```bash
/opt/satrx/.venv/bin/pip install psutil
/opt/satrx/.venv/bin/python scripts/soak_test.py --minutes 480 --report soak.json   # egy éjszaka
```

A teszt külön folyamatban futtatja a szolgáltatást (mock SDR, ideiglenes adatkönyvtár), felvételeket indít és
feldolgoztat, közben SSE- és WebSocket-klienseket tart nyitva, és mér: memóriát (RSS), szálakat, nyitott fájlokat, CPU-t,
lemezhasználatot. `PASS`/`FAIL` ítéletet ad. A memóriaszivárgásról **30 percnél rövidebb futásból nem mond ítéletet**
(a bemelegedés elnyomja a trendet), ezért az éjszakai futás az értelmes.

## 10. Hibakeresés

| Tünet | Teendő |
|-------|--------|
| A webes felület nem érhető el | `systemctl status satrx`, `journalctl -u satrx -n 50`, `satrx doctor` (port, függőségek, web/dist) |
| „No RTL-SDR device found" | USB-kábel/táp; a `dvb_usb_rtl28xxu` tiltva van-e (`lsmod`); udev-szabály; `plugdev` csoport; újracsatlakoztatás |
| Az átvonulások időpontja rossz | `timedatectl`: az óra nincs szinkronban; a megfigyelési pont koordinátája; a TLE kora (Áttekintés) |
| „A mentési hely nem elérhető" | A külső meghajtó nincs csatolva: `lsblk`, `mount`, `/etc/fstab`; a kábel és a táp |
| Megtelt a lemez | Tárhely oldal: nyers IQ törlése, exportálás; a kvóta és a megőrzés beállítása |
| Elfelejtett API-token | `sudo grep auth_token /opt/satrx/config/local.yaml` |
| A Meteor képei hiányoznak | SatDump telepítve van-e (`doctor`); a műhold LRPT-adója bekapcsolt-e (`no_result` = nincs dekódolható jel) |

## 11. Biztonság

* Alapból **nem titkosított HTTP** a helyi hálózaton, API-tokennel. Ne tedd közvetlenül az internetre.
* Távoli eléréshez VPN (pl. WireGuard/Tailscale) vagy HTTPS-t végző reverse proxy (pl. Caddy) javasolt.
* Tűzfal: `sudo ufw allow from 192.168.0.0/16 to any port 8000` (a saját hálózatod szerint).
* A szolgáltatás nem root-ként fut, szigorított systemd-beállításokkal (lásd `deploy/satrx.service`).

## 12. Eltávolítás

```bash
sudo ./scripts/install_pi.sh --uninstall               # a szolgáltatás és az udev-szabály; az adatok megmaradnak
sudo ./scripts/install_pi.sh --uninstall --purge-data  # az adatokat is törli (/var/lib/satrx)
```
