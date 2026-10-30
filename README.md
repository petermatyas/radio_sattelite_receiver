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

## Az `rtl_sdr` és `rtl_fm` programok (RTL-SDR)

A valódi vételhez a satrx a külső `rtl_sdr` (nyers IQ) és `rtl_fm` (közvetlen hang) parancssori programokat is használhatja
(`sdr.backend: rtl_sdr`). Ezek nem szkriptek, hanem kész programok (`rtl_sdr.exe`, `rtl_fm.exe`, `rtl_test.exe`, a `rtlsdr.dll`-lel együtt).

**Honnan töltsd le**
* **Windows:** az rtl-sdr-blog kiadásai (a V3/V4 vevőkhöz is jó): <https://github.com/rtlsdrblog/rtl-sdr-blog/releases>.
  Töltsd le a Windowsra szóló zip-et (pl. `Release.zip`), csomagold ki egy rövid útvonalú mappába (pl. `C:\rtl-sdr`); a program az `x64` almappában van.
  Alternatíva: az Osmocom saját fordításai: <https://ftp.osmocom.org/binaries/windows/rtl-sdr/>.
* **WinUSB-illesztő (Windowson kell):** Zadig, <https://zadig.akeo.ie/>. Options → List All Devices → „Bulk-In, Interface (Interface 0)" → WinUSB → Replace Driver.
* **Linux / Raspberry Pi:** `sudo apt install rtl-sdr` (a telepítő szkript ezt is elvégzi). A forráskód: <https://osmocom.org/projects/rtl-sdr/wiki>.

**Beállítás**
1. Tedd az `x64` mappát a PATH-ba, vagy add meg az útvonalat a webes felületen (Beállítások → Külső programok), illetve a `config/local.yaml`-ben:
   ```yaml
   sdr:
     backend: rtl_sdr
   tools:
     rtl_sdr_path: C:\rtl-sdr\x64\rtl_sdr.exe
     rtl_fm_path: C:\rtl-sdr\x64\rtl_fm.exe
   ```
2. Ellenőrzés: `.venv\Scripts\python -m satrx doctor`. Közvetlenül a programmal: `rtl_test -t` (az eszköz látszik-e).

A linkek a leírás írásakor nem lettek újra megnyitva; ha az oldal változott, keresd az „rtl-sdr-blog releases" vagy „rtl-sdr windows binaries" kifejezésre.

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
