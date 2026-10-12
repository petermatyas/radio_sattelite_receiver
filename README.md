# satrx: automatikus műholdvevő

SDR-rel (RTL-SDR, később más is) műholdak jeleinek automatikus vétele, feldolgozása és megjelenítése webes felületen.
Windowson fejleszthető és tesztelhető, Raspberry Pi 5-ön (Raspberry Pi OS) üzemeltethető.

**Mit tud**

* **Ütemezés:** NORAD ID-k és időszakok megadása, prioritás, ütközésfeloldás (egy SDR), Doppler-követés, antenna- és sávkezelés.
* **Felvétel:** nyers IQ SigMF formátumban, a pályametaadattal (TLE, átvonulás, Doppler-idősor) együtt.
* **Feldolgozás (bővíthető pipeline-ok):** FM-demodulátor, SSTV (14 mód), Meteor-M LRPT (SatDump), utólagos waterfall és hang (NFM/WFM/AM/USB/LSB).
* **Webes felület:** áttekintés és állapot, idővonal, élő waterfall, galéria, pipeline-szerkesztő, tárhelykezelés, hibák és események.
* **Üzemeltetés:** systemd szolgáltatás watchdoggal, összeomlás utáni helyreállítás, adatmegőrzés, külső meghajtó, `satrx doctor`.

A részletes terv és a fázisonkénti állapot: [TERV.md](TERV.md). Raspberry Pi: [docs/RASPBERRY_PI.md](docs/RASPBERRY_PI.md).

## Gyors indítás (Windows, fejlesztéshez)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1        # -Backend rtlsdr a valódi eszközhöz
run_satrx.cmd                                                               # majd: http://localhost:8000
```

Kézzel, bármilyen rendszeren (Python 3.11+, a webes felülethez Node.js):

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"      # Windows: .venv\Scripts\pip
cd web && npm install && npm run build && cd ..
.venv/bin/python -m satrx                                      # http://localhost:8000
.venv/bin/python -m satrx doctor                               # a környezet ellenőrzése
```

Alapból a `mock` SDR fut (hardver nélkül is kipróbálható); a valódi eszközhöz `sdr.backend: rtlsdr`
a `config/local.yaml` fájlban (példa: `config/local.yaml.example`).

## Raspberry Pi

```bash
python scripts/make_release.py                 # a fejlesztő gépen: kiadási csomag a megépített webes felülettel
sudo ./scripts/install_pi.sh                   # a Pi-n, a kicsomagolt csomagból
```

## Tesztek

```bash
.venv/bin/python -m pytest                     # minden teszt (hardver és internet nélkül)
.venv/bin/python scripts/soak_test.py --minutes 5      # hosszútávú teszt (éjszakai futáshoz: --minutes 480)
```

## Szerkezet

| Mappa | Tartalom |
|-------|----------|
| `src/satrx/` | a szolgáltatás (API, ütemező, SDR-réteg, felvétel, pipeline, processzorok, tárhely) |
| `web/` | a React + TypeScript webes felület |
| `config/` | alapértelmezett és példa konfiguráció |
| `deploy/` | systemd-egység, udev-szabály, kernelmodul-tiltólista |
| `scripts/` | telepítők, kiadási csomag, fstab-segéd, hosszútávú teszt |
| `plugins/` | saját feldolgozók (egy `register(registry)` függvénnyel) |
| `tests/` | automatikus tesztek |
