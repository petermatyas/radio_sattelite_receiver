# Műhold vevő – megvalósítási terv

Automatizált műholdjel-vétel SDR-rel, webes felülettel, bővíthető feldolgozó (pipeline) rendszerrel.

- **Fejlesztői/teszt környezet:** Windows 11 PC
- **Éles környezet:** Raspberry Pi 5, Raspberry Pi OS (64 bites, Debian alapú)
- **Első hardver:** RTL-SDR (később cserélhető)
- **Követelmény:** Windows + Linux kompatibilitás ugyanabból a kódbázisból

---

## 1. Célok és követelmények

| # | Követelmény |
|---|-------------|
| 1 | Web UI-n megadható, hogy mely NORAD ID-jú műholdat figyelje a rendszer |
| 2 | Időablakok: „dátumtól – dátumig" figyelés műholdanként |
| 3 | Műholdanként megadható a mód és az utána futó pipeline |
| 4 | Prioritás: ütköző átvonulásoknál (egy SDR) eldönthető, melyik nyer |
| 5 | Frekvenciasávok/antennák definiálhatók (későbbi antennaváltóhoz) |
| 6 | Ütemező („üzemező"), ami az átvonulások idején automatikusan elindítja a pipeline-t |
| 7 | Az SDR rögzíti a jelet, majd a kiválasztott mód szerint feldolgozás |
| 8 | Bővíthető feldolgozó lista; alapból: **FM**, **SSTV**, **Meteor M (LRPT) képek** |
| 9 | Eredmények mentése, **galéria** a böngészőben |
| 10 | Mentett IQ felvételből utólag generálható **hang** és **vízesés (waterfall)** diagram |
| 11 | Web alapú felület |
| 12 | SDR hardver absztrakció (RTL-SDR → bármi más) |
| 13 | **Hibák és állapotok láthatóak a felületen**: felügyelet nélküli üzemben sem kell naplófájlt olvasni (lásd 10.1) |
| 14 | **Adatkezelés a felületről**: a lementett adatok megtekinthetők, törölhetők, letölthetők/exportálhatók, és a mentés helye módosítható (lásd 10.2) |
| 15 | **Időzített események NORAD ID nélkül** (lásd 10.3): fix időablakú felvétel egy profil frekvenciáján, egyszeri vagy napi/heti ismétlődéssel, helyi időzónában |
| 16 | **Átlátható felület** (lásd 10): kevés menü; a főoldalon konzolszerű élő napló a történésekről és a hibákról |

---

## 2. Technológiai döntések (javaslat)

| Réteg | Választás | Indok |
|-------|-----------|-------|
| Nyelv (backend) | **Python 3.11+** | Windows/Linux/ARM egyaránt, kiváló SDR/DSP ökoszisztéma (numpy, scipy) |
| Web keretrendszer | **FastAPI** + Uvicorn | async, automatikus OpenAPI, WebSocket/SSE az élő állapothoz |
| Adatbázis | **SQLite** (SQLAlchemy + Alembic) | nulla konfig, Pi-n és Windowson is jó |
| Ütemezés | Saját scheduler-loop (lásd 5. fejezet), nem cron | az átvonulás-számítás és ütközésfeloldás egyedi logika |
| SDR absztrakció | **SoapySDR** (elsődleges) + natív `pyrtlsdr` fallback | SoapySDR lefedi az RTL-SDR-t, Airspy-t, HackRF-et, SDRplay-t stb. |
| Pályaszámítás | **Skyfield** (vagy `sgp4`) | átvonulás-előrejelzés, azimut/elevation, Doppler |
| TLE forrás | **CelesTrak** (GP/TLE) NORAD ID alapján, napi frissítés, cache-elve | ingyenes, NORAD ID-val lekérdezhető |
| Frontend | **React + Vite + TypeScript** (statikusan kiszolgálva a FastAPI-ból) | Pi-n nem kell Node futásidőben, csak build-kor |
| Külső dekóderek | **SatDump** (CLI) a Meteor LRPT-hez; Python/külső dekóder SSTV-hez | kész, bizonyított, aarch64 + Windows build is van |
| Csomagolás | `pyproject.toml`, `uv`/`pip`, opcionális Docker (csak Linux/Pi) | |

> **Megjegyzés a Windowsra:** SoapySDR + RTL-SDR driver Windowson körülményesebb (Zadig/WinUSB driver, SoapySDR build). Ezért a hardver réteg két backendet kap: `pyrtlsdr` (egyszerű telepítés) és `soapy`. Konfigurációból választható.

---

## 3. Architektúra

```
┌────────────────────────────── Web UI (React) ──────────────────────────────┐
│ Műholdak │ Ütemezés/Naptár │ Átvonulások │ Élő állapot │ Galéria │ Felvételek│
└──────────────▲───────────────────────────────────────────▲─────────────────┘
               │ REST + WebSocket/SSE                      │ statikus fájlok
┌──────────────┴───────────────────────────────────────────┴─────────────────┐
│                           FastAPI szolgáltatás                              │
│  API réteg ─ Szolgáltatások ─ Repository (SQLite) ─ Fájltár (data/)         │
└───────┬────────────────────────────────────────────────────────────────────┘
        │ (ugyanabban a folyamatban vagy külön worker folyamatban)
┌───────▼──────────┐   ┌──────────────────┐   ┌─────────────────────────────┐
│ TLE kezelő       │   │ Pass Predictor   │   │ Scheduler / Orchestrator    │
│ (CelesTrak)      │──▶│ (Skyfield)       │──▶│ ütközésfeloldás, job queue  │
└──────────────────┘   └──────────────────┘   └──────────────┬──────────────┘
                                                             │ job indul
                           ┌─────────────────────────────────▼──────────────┐
                           │ Pipeline Runner                                │
                           │  1. Recorder (SDR → IQ fájl, Doppler-követés)  │
                           │  2. Processor plugin (FM / SSTV / LRPT / …)    │
                           │  3. Artifact mentés (kép, hang, meta) → DB     │
                           └─────────────────────────────────┬──────────────┘
                                                             │
                                          ┌──────────────────▼─────────────┐
                                          │ SDR Hardver Absztrakció (HAL)  │
                                          │ RtlSdr │ Soapy │ (jövőbeli)    │
                                          └────────────────────────────────┘
```

### Fő szabály: a **felvétel és a feldolgozás szétválik**
Az SDR mindig nyers IQ-t ment lemezre (a nyers felvétel a „forrás igazság"). A feldolgozás ezután (vagy részben közben) fut. Így:
- az SDR azonnal felszabadul a következő átvonuláshoz,
- a felvételből utólag bármikor újragenerálható hang/waterfall/kép,
- hibás dekódolás után újrafuttatható más beállításokkal.

Streaming-feldolgozás (élő demoduláció) opcionális kiegészítés, ha később kell élő hang a böngészőben.

---

## 4. Adatmodell (SQLite)

- **satellites** – `norad_id` (PK), `name`, `tle_line1/2`, `tle_epoch`, `enabled`, `default_profile_id`
- **radio_profiles** – frekvencia, sávszélesség, mintavételezési ráta, gain, módulációs hint, antenna ref., Doppler be/ki
- **antennas** – `id`, `name`, `freq_min_hz`, `freq_max_hz`, `switch_port` (később antennaváltóhoz), `notes`
- **watch_rules** (az „időpontokhoz bejelölés") – `satellite_id`, `valid_from`, `valid_to`, `min_elevation_deg`, `priority`, `profile_id`, `pipeline_id`, `enabled`, opcionális ismétlődés
- **pipelines** – `id`, `name`, `mode` (fm / sstv / lrpt / …), `steps` (JSON: processor plugin neve + paraméterek), `keep_iq` (megtartás szabály)
- **passes** – kiszámolt átvonulások: `satellite`, `aos`, `los`, `tca`, `max_elevation`, `status` (planned / skipped_conflict / recording / processing / done / failed)
- **jobs** – egy átvonuláshoz tartozó futás: állapot, naplók, hibák, időbélyegek
- **recordings** – IQ fájl elérési út, formátum (SigMF), center freq, sample rate, kezdet/vég, méret, kapcsolódó job
- **artifacts** – kimenetek (kép, WAV, PNG waterfall, szöveg): típus, fájl, bélyegkép, metaadat (JSON), kapcsolódó recording/job
- **settings** – megfigyelő helyszín (lat/lon/magasság), tárhely limitek, SDR beállítások

---

## 5. Ütemező (Scheduler / „üzemező")

### Működés
1. **Pass Predictor**: az aktív `watch_rules` alapján előre kiszámolja az átvonulásokat (pl. következő 48 óra), a `min_elevation` szűrővel. Újraszámol TLE-frissítéskor vagy szabálymódosításkor.
2. **Ütközésfeloldás** (egy SDR = egyszerre egy felvétel; később több eszköz esetén több „erőforrás"):
   - átfedő átvonulások közül a **magasabb prioritású** nyer,
   - azonos prioritásnál: magasabb max. elevation / hosszabb használható időtartam nyer,
   - opcionálisan: a vesztes átvonulás **részlegesen** felvehető, ha az átfedés csak az elejét/végét érinti (konfigurálható: engedélyezett / tiltott),
   - frekvencia/antenna kompatibilitás: ha két műhold ugyanabba az SDR sávszélességbe esik, **egy felvétellel mindkettő** felvehető (opcionális optimalizáció, 2. fázis),
   - a kiesett átvonulás `skipped_conflict` státuszt kap és látszik a UI-n.
3. **Job indítás**: AOS − előkészítési idő (pl. 30 mp) → SDR megnyitás, antenna kapcsolás (ha van), Doppler-követés indul; LOS + puffer → leállás.
4. **Robusztusság**: újraindítás után a tervezett átvonulások újraszámolódnak az adatbázisból; elmulasztott jobok jelölve; SDR hiba esetén retry/log; lemezhely ellenőrzés indulás előtt.
5. **Szimulációs / „dry-run" mód**: valódi SDR nélkül is fut (felvétel helyett meglévő IQ fájl lejátszása) – ez a Windows teszteléshez kulcsfontosságú.

### Prioritás-logika (példa)
```
score = rule.priority * 1000 + max_elevation_deg * 10 + duration_s / 60
```
A legnagyobb score nyer egy átfedő csoportban. A szabály mindig felülbírálható a UI-n manuális „kényszerített" kijelöléssel.

---

## 6. SDR hardver absztrakció (HAL)

```python
class SdrDevice(Protocol):
    def open(self, args: dict) -> None: ...
    def configure(self, center_hz, sample_rate, gain, bandwidth=None, antenna=None) -> None: ...
    def retune(self, center_hz) -> None: ...          # Doppler-követéshez
    def stream(self) -> Iterator[np.ndarray]: ...      # complex64 / int16 blokkok
    def capabilities(self) -> SdrCaps: ...             # freq range, max SR, gain range, antennák
    def close(self) -> None: ...
```

- Implementációk: `RtlSdrDevice` (pyrtlsdr), `SoapyDevice` (SoapySDR), `FileReplayDevice` (teszt/szimuláció), `MockDevice` (szintetikus jel generátor).
- A `capabilities()` alapján a UI/scheduler ellenőrzi, hogy a profil futtatható-e az adott eszközön.
- **Antenna / frekvenciasáv réteg**: `antennas` tábla + `AntennaSwitch` interfész (`select(port)`); jelenleg `NoopSwitch`, később GPIO / USB-relé / hálózati váltó implementáció. A scheduler a profil frekvenciája alapján választ antennát.

---

## 7. Felvétel formátum

- **SigMF** (`.sigmf-data` + `.sigmf-meta`) – szabványos, tartalmazza a közép-frekvenciát, mintavételi rátát, időbélyeget, műhold ID-t.
- Mintaformátum: `ci16_le` (RTL-SDR natív 8 bit → `cu8` is megengedett, helytakarékos); 2.4 MS/s cu8 ≈ 4.8 MB/s ≈ 290 MB/perc – a mintavételi rátát profilonként érdemes lecsökkenteni (pl. LRPT-hez 1–2 MS/s, FM/SSTV-hez ~250 kS/s decimálás után).
- Opcionális tömörítés / megtartási szabály: pl. a feldolgozás után az IQ törlése vagy N napig tárolása; lemezkvóta.

### 7.1 Pályametaadatok a nyers felvétel mellé

Minden nyers felvétel mellé a felvétel befejezésekor (hiba vagy megszakítás esetén is) pályametaadat-fájl kerül, így a felvétel önmagában, a TLE későbbi frissülése után is értelmezhető és újrafeldolgozható.

- **Fájl:** `<alapnév>.orbit.json` a `.sigmf-data` és `.sigmf-meta` mellett; a törléssel, exporttal és átmozgatással együtt kezelendő hármas (négyes) fájlcsoport.
- **Tartalma:**
  - **Műhold:** NORAD ID, név, a felvételkor érvényes **TLE** (mindkét sor + epoch + letöltés ideje), a TLE forrása.
  - **Megfigyelő:** lat / lon / magasság a felvétel idején.
  - **Átvonulás:** AOS, TCA, LOS, max. elevációs szög, TCA-kori azimut; a tervezett és a tényleges felvételi ablak (részleges felvételnél látszik, mi maradt ki és miért).
  - **Pálya-idősor** (pl. 1 mp-es lépésekkel, a felvételi ablak teljes hosszára): idő, azimut, elevációs szög, távolság, relatív sebesség (range rate), nominális és Doppler-korrigált frekvencia, (opcionálisan) a műhold szub-szatellita pontja.
  - **Rádió:** profil neve, nominális frekvencia, közép-frekvencia, mintavételi ráta, erősítés, antenna; az újrahangolások ideje és frekvenciája (megegyezik a SigMF `captures` szegmensekkel).
  - **Származás:** szabály/pipeline azonosító, a szoftver verziója, séma-verzió (`orbit_schema: 1`).
- **SigMF kapcsolat:** a kulcsadatok (NORAD ID, műholdnév, átvonulás-azonosító, TLE epoch) a `.sigmf-meta` `global` részében is szerepelnek `satrx:*` kulccsal, a max. elevációs szög és a Doppler-újrahangolások pedig `annotations` / `captures` bejegyzésként; a teljes idősor csak az `.orbit.json`-ban van, hogy a SigMF meta kicsi maradjon.
- **Adatbázis:** a `recordings` tábla hivatkozik a fájlra (`orbit_meta_path`), és az átvonulás fő adatai (AOS/LOS/max. elevációs szög, TLE epoch) a lekérdezhetőség miatt oszlopként is megvannak.
- **Felhasználása:**
  - a feldolgozók (`PipelineContext.orbit`) a Doppler-görbét és a TLE-t használják: pontos Doppler-kompenzáció utófeldolgozásban, a SatDump LRPT-képek földrajzi vetítése és időbélyegzése;
  - a waterfall diagram Doppler-görbe / elevációs overlay-t rajzolhat a felvétel mellé (9. fejezet);
  - a galéria/felvétel részletei megmutatják az átvonulás adatait (elevációs diagram, irány), az újrafeldolgozás ugyanazt a TLE-t használja, nem a legfrissebbet;
  - hibakereséshez: „a felvétel jelgyenge volt, mert az átvonulás csak 12° max. elevációjú volt".
- **Hiányzó TLE/megfigyelő** (pl. kézi felvétel): a fájl akkor is létrejön a rendelkezésre álló adatokkal (rádióparaméterek, idő), a hiányzó részek `null` értékűek.
- **Megvalósítás:** a Recorder az indítás pillanatában pillanatképet készít (TLE, megfigyelő, profil), a lezáráskor kiegészíti a tényleges idővel és az újrahangolásokkal, majd írja a fájlt; a hiba mellé eseményt is logol (10.1), ha az írás nem sikerül, de az IQ felvételt ez nem szakítja meg.

---

## 8. Pipeline / feldolgozó plugin rendszer

### Plugin interfész
```python
class Processor(ABC):
    name: str                  # "fm_demod", "sstv", "meteor_lrpt"
    input_kind: str            # "iq" | "audio" | "image" ...
    params_schema: type[BaseModel]   # a UI ebből generál űrlapot

    def run(self, ctx: PipelineContext, params) -> list[Artifact]: ...
```

- A `PipelineContext` tartalmazza a felvétel útvonalát és SigMF metaadatait, valamint a betöltött pályametaadatot (`ctx.orbit`, lásd 7.1: TLE, megfigyelő, átvonulás, Doppler-idősor).
- Plugin felfedezés: Python **entry points** (`satrx.processors`) vagy `plugins/` mappa – új feldolgozó a mag módosítása nélkül adható hozzá.
- Pipeline = lépések láncolata (YAML/JSON), pl. `record → fm_demod → sstv_decode → save_gallery`.
- A `params_schema` (Pydantic) alapján a webes felület automatikusan űrlapot épít.
- Minden lépés naplóz, haladást jelent (SSE), hibánál a job `failed` lesz, az IQ felvétel megmarad az újrafuttatáshoz.

### Alap processzorok

**1. FM demodulátor** (`fm_demod`)
- Csatorna kiválasztás (NCO frekvenciaeltolás + Doppler), low-pass szűrés, decimálás, FM (NFM/WFM) demoduláció, de-emphasis, squelch → **WAV** kimenet.
- Python (numpy/scipy), nincs külső függőség.

**2. SSTV** (`sstv`)
- Tipikus eset: ISS SSTV események (145.800 MHz, NFM, PD120/PD180/Robot36 stb.).
- Lánc: `fm_demod` → audio → SSTV dekóder → **PNG**.
- Dekóder: kezdetben beépülő külső/Python megoldás (pl. `sstv` Python csomag vagy slowrx-szerű dekóder); mód auto-detektálás a VIS kódból.

**3. Meteor-M LRPT** (`meteor_lrpt`)
- Frekvencia: ~137.1 / 137.9 MHz, QPSK, 72 kbps.
- Megvalósítás: **SatDump CLI** hívása a rögzített IQ-n (`satdump meteor_m2-x_lrpt baseband ...`), kimenet: csatornánkénti PNG + kompozitok. Alternatíva: `meteor_demod` + `meteor_decoder`.
- A processzor wrapper: megkeresi a SatDump binárist (konfigurálható út), futtatja, begyűjti a képeket artifactként.
- Fontos: a Meteor műholdak LRPT adása időnként ki van kapcsolva / műholdállapottól függ – a rendszernek ezt „nem sikerült dekódolni" állapotként kezelnie, nem hibaként.

**4. Mindig elérhető segédlépések:** `iq_to_audio`, `waterfall`, `save_gallery`, `cleanup_iq`.

### Későbbi bővítési lehetőségek
NOAA APT, Meteor LRPT további típusai, AIS, ADS-B, CW/telemetria (pl. cubesat beacon), AX.25/APRS (ISS digipeater), GOES HRIT (más hardver igényli).

---

## 9. Utólagos feldolgozás (hang + vízesés)

A felvételek oldalon bármely IQ felvételhez:
- **Waterfall PNG**: STFT (numpy/scipy), állítható FFT méret, átfedés, dinamikatartomány, colormap; nagy felvételnél blokkonkénti feldolgozás + decimálás (memóriakímélő). Időtengely + frekvencia tengely feliratozva, esetleg Doppler-görbe overlay.
- **Hang (WAV)**: a felhasználó megadja a frekvencia-eltolást, sávszélességet és a demodulációt (NFM / WFM / AM / USB / LSB) → szerveroldali demoduláció → WAV, lejátszás a böngészőben.
- **Interaktív kijelölés (2. fázis)**: a waterfall-on egérrel kijelölt időtartam/frekvencia sáv alapján hang generálás.
- Mindkettő háttér-jobként fut, az eredmény artifactként elmentődik és megjelenik a galériában.

---

## 10. Webes felület

**Hét főmenü** (korábban tíz; a mélylinkek megmaradtak, a régi `#schedule`, `#pipelines`, `#satellites` az új helyükre irányít):

| Menü | Tartalom |
|------|----------|
| **Áttekintés** | Rendszerállapot-kártyák (SDR, ütemező, TLE, tárhely), következő felvétel, élő waterfall, **konzol** (lásd 10.4), legutóbbi jobok |
| **Átvonulások** | Három almenüvel. **Menetrend:** az előrejelzett átvonulások és időzített események idővonala/listája, ütközések, a státusz oka. **Műholdak:** NORAD ID hozzáadása, TLE, engedélyezés. **Ütemezés:** megfigyelési szabályok, **időzített események** (10.3), rádióprofilok, pipeline-ok és szerkesztőjük |
| **Felvételek** | IQ felvételek, részletek, „Waterfall generálása", „Hang generálása", újrafeldolgozás, törlés; kézi felvétel |
| **Galéria** | Képek/hangok/waterfall-ok szűrhetően, bélyegkép rács, részletes nézet metaadattal |
| **Tárhely** | Használat, szűrés, törlés, export, a mentés helyének módosítása (10.2) |
| **Események** | Szűrhető eseménylista (szint, forrás, időszak), nyugtázás, job-részletek lépésenkénti idővonallal és naplóval, „Újrafuttatás" |
| **Beállítások** | Helyszín (lat/lon), SatDump útvonal |

- Valós idejű frissítés: **WebSocket/SSE**.
- Egyfelhasználós rendszerrel indulunk; opcionális egyszerű jelszó/token (LAN-on kívüli eléréshez), később felhasználókezelés.
- Reszponzív, hogy telefonról is nézhető legyen.

### 10.1 Hibák és állapotok megjelenítése

**Elv:** minden, ami felügyelet nélkül elromolhat vagy érdemes tudni, a felületen is látszik – érthető szöveggel és javasolt teendővel, nem nyers kivétellel.

**Mit mutatunk**

| Állapot / hiba | Forrás | Hol látszik |
|----------------|--------|-------------|
| Átvonulás: tervezett / részleges / kimaradt (ütközés) / nem támogatott / felvétel / feldolgozás / kész / hiba / elmulasztva | `passes.status` (megvan) | Átvonulások oldal, színes jelvény + ok |
| SDR: szabad / felvétel / leállítás, utolsó hiba (eszköz nem található, driver, USB-hiba) | `RecorderStatus` (megvan) | Fejléc állapotsor, Dashboard |
| Pipeline: lépésenként várakozik / fut / kész / hibás, időtartam, napló, hibaüzenet | `jobs` + új `job_steps` (4. fázis) | Job-részletek, Felvételek oldal |
| Ütemező: következő átvonulás visszaszámlálóval, megszakított kézi felvétel, elmulasztott átvonulás oka (pl. a szerver nem futott) | Scheduler | Dashboard, Események |
| TLE kora / sikertelen frissítés | TLE szolgáltatás | Műholdak oldal (figyelmeztetés, ha > 48 óra), Események |
| Hiányzó beállítás: megfigyelési pont, SatDump útvonal, nincs szabály | Rendszerellenőrzés | Dashboard figyelmeztető sáv |
| Tárhely: szabad hely, kvóta, IQ törlés a megőrzési szabály miatt, „nincs elég hely a felvételhez" | Storage | Dashboard, Események |
| Dekódolási eredmény: „nem sikerült dekódolni" (nem hiba, pl. a Meteor adása ki van kapcsolva) | Processzorok | Galéria/Felvételek: semleges jelzés, nem piros hiba |

**Eseménynapló (új `events` tábla)**
- Mezők: `ts`, `level` (info / warning / error), `source` (scheduler / recorder / pipeline / tle / storage / system), `code` (géppel olvasható, pl. `sdr_not_found`, `disk_full`, `pass_missed`), `message` (a `code` alapján fordított, olvasható szöveg HU/EN), `details` (JSON), opcionális `pass_id` / `job_id` / `recording_id`, `acknowledged`.
- Egyetlen belépési pont (`emit_event(...)`), amit a scheduler, a recorder, a pipeline-futtató és a TLE/storage szolgáltatás használ; a nem kezelt kivételek is ide kerülnek (a részletek a naplófájlban).
- Megőrzés: 30 nap vagy 5000 sor (amelyik kevesebb); a nem nyugtázott `error` szintű események nem törlődnek.

**API**
- `GET /api/health/detailed` – komponensenként `ok | warning | error` + üzenet (SDR, ütemező, TLE, tárhely, konfiguráció).
- `GET /api/events?level=&source=&since=` és `POST /api/events/{id}/ack`.
- `GET /api/jobs/{id}` – lépések, naplóvég, hiba; `POST /api/jobs/{id}/retry` (a nyers IQ-ból újrafuttatás).
- `GET /api/stream` (SSE) – állapot-pillanatképek és új események valós időben (a 17. fejezet waterfall WebSocketjétől független).

**UI elemek**
- **Állapotsor a fejlécben** minden oldalon: SDR állapot, folyamatban lévő felvétel/feldolgozás, következő átvonulás visszaszámlálója, piros jelvény a nem nyugtázott hibák számával.
- **Toast értesítés** új `error`/`warning` eseményekről (nyitott böngészőben); kattintásra az Események oldalra visz.
- **Hibaüzenet + javaslat**: a `code` alapján rövid teendő is megjelenik (pl. `sdr_not_found` → „Ellenőrizd az USB-t és a drivert", `disk_full` → „Töröld a régi felvételeket vagy emeld a kvótát").
- **Üres állapotok magyarázata**: ha nincs átvonulás, a felület megmondja, miért (nincs helyszín / nincs szabály / minden szabály lejárt / nincs TLE).

**Elfogadási kritérium:** egy felügyelet nélküli üzem alatt bekövetkezett hiba (hiányzó SDR, megtelt lemez, elhasalt dekódolás, elmulasztott átvonulás) néhány másodpercen belül, naplófájl olvasása nélkül, érthető okkal látszik a felületen. Tesztek: minden `code` kiváltása mockolt hibával, és az esemény megjelenése az API-ban.

**Meglévő alapok (0–3. fázis):** átvonulás-státusz és felvételi ablak a UI-n, `Job.status/error`, `RecorderStatus.last_error`, a kézi felvétel hibái a Felvételek oldalon. Hiányzik: központi eseménynapló, rendszerszintű állapotellenőrzés, állapotsor, SSE és a job-részletek.

### 10.2 Adatkezelés: megtekintés, törlés, letöltés, a mentés helyének módosítása

**Cél:** a lementett adatokat (nyers felvételek, eredmények) a felületen meg lehessen nézni, törölni, letölteni/exportálni, és be lehessen állítani, hogy hova mentsen a rendszer, anélkül hogy fájlokat kellene keresni a lemezen.

**1. Megtekintés (új „Tárhely" oldal és bővített Felvételek/Galéria)**
- Lista felvételenként és eredményenként: méret, dátum, műhold, típus, állapot (pl. „nyers IQ törölve, az eredmények megvannak", „védett"). Szűrés (műhold, dátum, típus, állapot) és rendezés méret szerint, hogy a legnagyobb helyfoglalók gyorsan látszanak.
- Összesítő: a felvételek és az eredmények összmérete, kvótasáv (mennyi a beállított kvótából), szabad lemezhely a célmeghajtón, az utolsó 7 nap növekedése, becsült hátralévő idő a megtelésig.
- Előnézet törlés előtt: ugyanaz a részletes nézet, mint a Felvétel-részletek (waterfall, hang, kép), így látszik, mit törölnénk.

**2. Törlés**
- Egyenként és **tömegesen** (kijelölés a listában vagy a galériában). A megerősítő ablak megmutatja, hány elem és **mennyi hely** szabadul fel.
- Két hatókör: **csak a nyers IQ törlése** (az eredmények, képek, hangok, waterfallok és a pályametaadat megmaradnak: ez a leggyakoribb, helyet spórol) és **mindent** (az eredményekkel és a metaadattal együtt). Eredmény önmagában is törölhető.
- **Védett** jelölés: a védett felvételt a kézi tömeges törlés is megkérdezi, az automatikus megőrzési szabály pedig nem törli.
- Biztonság: csak az adatbázisban nyilvántartott fájlokat töröljük, soha nem „a mappát"; futó felvétel vagy feldolgozás alatt álló felvétel nem törölhető (a felület megmondja, miért).
- A törlések eseménynaplóba kerülnek (`data_deleted`: mennyi elem, mennyi hely), az automatikus törlések külön kóddal (`retention_deleted`).
- **Automatikus megőrzés** ugyanezt a törlőkódot használja: az IQ megőrzési ideje (alap: 7 nap) és a kvóta (alap: 50 GB) szerint a legrégebbi, nem védett, már feldolgozott felvételek IQ-ját törli; a nem feldolgozott vagy hibás felvétel IQ-ját csak végső esetben (kvóta teljes túllépése), és erről figyelmeztetést ír.

**3. Letöltés / mentés**
- Egyedi fájlok letöltése: nyers IQ (`.sigmf-data` + `.sigmf-meta`), `.orbit.json`, WAV, PNG. A nagy fájl **streamelve** töltődik (nem kerül a memóriába), megszakadt letöltés folytatható (HTTP Range).
- **Exportcsomag (ZIP)** egy vagy több felvételre/eredményre: IQ + metaadat + pályaadat + eredmények, tömörítés nélkül az IQ-nál (úgysem tömörödik), `MANIFEST.json` az ellenőrző összegekkel. Nagy csomagnál háttérjob készíti, a haladás látszik, és a kész fájl letölthető.
- **Másolás a szerveren megadott mappába** (pl. USB-meghajtó, hálózati meghajtó): exportjob célmappával; az ellenőrző összeg alapján igazolja a másolatot, és kérésre utána törli az eredetit („áthelyezés").

**4. A mentés helyének módosítása**
- A Beállítások / Tárhely oldalon külön megadható a **felvételek** (nyers IQ) és az **eredmények** (képek, hangok) gyökérmappája, például a Pi-n egy külső SSD-re vagy USB-meghajtóra. (Az adatbázis és a naplók helye az `data_dir`, ez csak a konfigurációs fájlban/környezeti változóval állítható, mert indulás előtt kell.)
- **Ellenőrzés mentés előtt:** a mappa létezik vagy létrehozható, írható, van elég szabad hely, abszolút elérési út, Windowson és Linuxon is érvényes forma, nem az alkalmazás saját forráskönyvtára; ha a mappa nem üres, figyelmeztet. A felület a próbaírás eredményét mutatja.
- **Modell:** egy `storage_roots` tábla (azonosító, útvonal, címke, felvétel/eredmény, aktív). A `recordings` és `artifacts` sor megjegyzi, melyik gyökérben van a fájl; az **új** adat mindig az aktív gyökérbe kerül. A régi adat két módon kezelhető: (a) marad a régi helyen, és onnan továbbra is elérhető (több gyökér egyszerre), vagy (b) **áthelyezés háttérjobként**: másolás, ellenőrző összeg, csak utána a régi törlése; haladás látszik, megszakítható és folytatható, hiba esetén az eredeti érintetlen marad.
- **Védelem:** futó felvétel vagy feldolgozás alatt a gyökér nem módosítható és áthelyezés nem indítható (a felület megmondja, mi tartja foglalva).
- **Meghajtó eltűnése** (kihúzott USB, nem csatolt hálózati meghajtó): az állapotellenőrzés `storage_unavailable` hibát jelez, új felvétel nem indul, az érintett átvonulások `missed` státuszt és eseményt kapnak, a már mentett adatok megjelenítése pedig nem omlik össze (a hiányzó fájl „nem elérhető" jelzést kap). Pi-n tipp a dokumentációban: stabil csatolás UUID-val az `fstab`-ban és `nofail` opció.
- A felvételi útvonalak a `settings` táblában tárolódnak (a YAML csak alapérték), így a UI-ról módosíthatók újraindítás nélkül.

**API (terv):** `GET /api/storage` (használat, gyökerek, kvóta, növekedés), `PUT /api/storage/roots` (ellenőrzéssel), `POST /api/storage/move` (áthelyezés job), `POST /api/storage/delete` (`recording_ids`, `artifact_ids`, `scope: iq_only | all`, `dry_run` a felszabaduló hely előnézetéhez), `PATCH /api/recordings/{id}` (`protected`), `GET /api/recordings/{id}/download?part=iq|meta|orbit|zip`, `POST /api/exports` (célmappa vagy ZIP, job).

**Elfogadási kritérium:** a felületen látszik, mi mennyi helyet foglal; egy kijelölt felvétel IQ-ja törölhető úgy, hogy az eredményei megmaradnak, és a törlés előtt a felület helyesen előre jelzi a felszabaduló helyet; egy felvétel letölthető teljes csomagként; a mentés helye átállítható, az új felvételek ott jelennek meg, a régieket pedig áthelyezi vagy tovább eléri a rendszer; kihúzott meghajtó mellett a hiba a felületen látszik. Tesztek: törlési hatókörök és védelem, `dry_run` pontossága, útvonal-ellenőrzés (nem létező, nem írható, relatív, tele van), áthelyezés megszakadás után, több gyökér olvasása, letöltés és ZIP tartalma/összege.

**Meglévő alapok:** a felvétel és az artifact törlése (`DELETE /api/recordings/{id}`, `DELETE /api/artifacts/{id}`), a `recordings.deleted` jelző, a kvóta és megőrzési idő beállítás és a kvótaellenőrzés az állapotellenőrzésben, a fájlok letöltése az eredményekhez. Hiányzik: az IQ külön törlése az eredmények megtartásával, tömeges törlés, védett jelölés, exportcsomag, a mentési hely módosítása és az automatikus megőrzés.

---

### 10.3 Időzített események (NORAD ID nélkül)

Nem minden érdekes jel műholdról jön (földi jeladó, rendszeres adás, ISS-en kívüli esemény): ezekhez nincs szükség műholdra, TLE-re és megfigyelési pontra.
- **Adatmodell:** `timed_events` (név, aktív, kezdet, hossz, ismétlődés `none|daily|weekly`, napok, ismétlés vége, **időzóna**, profil, pipeline, prioritás, részleges felvétel). A tervezett előfordulások a `passes` táblába kerülnek `norad_id = NULL`, `event_id` és `label` mezőkkel, így az ütemező, az ütközésfeloldás, a felvétel és a pipeline változatlanul működik.
- **Idő:** az előfordulások a megadott időzóna falióráját követik, így egy „minden nap 18:00" esemény a nyári időszámításváltás után is 18:00-kor indul (UTC-ben eltolódik). Az időzónához Windowson a `tzdata` csomag kell (függőségként szerepel).
- **Végrehajtás:** az időablak pontos (puffer nélkül), nincs Doppler-követés és pályaadat; a felvétel neve a címke, a `.orbit.json` `satellite: null`, de tartalmazza az esemény adatait és a megfigyelőt (ha van). Magasabb prioritású átvonulás vagy esemény elnyomhatja (vagy részlegesen megrövidítheti, ha engedélyezett) a másikat.
- **API:** `GET/POST /api/timed-events`, `PUT/DELETE /api/timed-events/{id}`, `POST /api/timed-events/preview` (a következő 8 időpont és a becsült nyers méret mentés nélkül). Ellenőrzések: időzónás kezdet, ismert IANA időzóna, 1 mp – 6 óra hossz, napok 0–6, a vége a kezdet után, létező profil/pipeline.
- **UI:** az Átvonulások → Ütemezés oldalon űrlap élő előnézettel (a mező a kiválasztott időzóna falióráját veszi), lista a következő időpontokkal, szerkesztés, ki/bekapcsolás, törlés; az idővonalon és a listában ⏱ jelöli őket.

### 10.4 Főoldali konzol

- Konzolszerű (monospace, sötét) napló: a legújabb sor alul, automatikus görgetés (ha a felhasználó feljebb görget, nem ugrik vissza); szűrő: minden / csak hibák és figyelmeztetések; szünet; törlés; „mind nyugtáz".
- Forrása az eseménynapló: induláskor az utolsó 200 bejegyzés, utána az SSE `event` üzenetei.
- Tartalma: a szolgáltatás indulása (`service_started`), **a felvétel indulása és vége** a nevével, frekvenciával, hosszal, mérettel (`recording_started/finished/stopped/failed`), a feldolgozás indulása és vége az eredmények számával (`pipeline_started/pipeline_done/no_result`), valamint minden hiba és figyelmeztetés a javasolt teendővel (10.1). A korábbi „aktív hibák" lista megszűnt, mert a konzol ezt is mutatja.

## 11. Fájlstruktúra

```
radio_sattelite_receiver/
├─ pyproject.toml
├─ TERV.md
├─ config/ (default.yaml)
├─ src/satrx/
│  ├─ api/            # FastAPI routerek, WebSocket/SSE
│  ├─ core/           # config, logging, db, modellek
│  ├─ tle/            # CelesTrak kliens, cache
│  ├─ passes/         # Skyfield, előrejelzés, Doppler
│  ├─ scheduler/      # ütemező, ütközésfeloldás, job queue
│  ├─ hardware/       # SdrDevice, rtlsdr, soapy, replay, mock, antenna switch
│  ├─ recording/      # SigMF írás/olvasás
│  ├─ pipeline/       # runner, kontextus, plugin loader
│  ├─ processors/     # fm_demod, sstv, meteor_lrpt, waterfall, iq_to_audio
│  └─ storage/        # fájltár, bélyegképek, megtartási szabályok
├─ web/               # React frontend (Vite)
├─ plugins/           # külső/saját processzorok
├─ tests/
├─ scripts/           # install_windows.ps1, install_pi.sh
└─ data/              # (gitignore) db, recordings, artifacts, tle cache
```

---

## 12. Platformkezelés (Windows ↔ Raspberry Pi 5)

- Kód: tiszta Python, `pathlib`, nincs OS-specifikus hívás a magban; OS-függő részek (driver, külső binárisok) a `hardware/` és `processors/` rétegben, konfigurációból.
- **Windows (teszt):** Zadig WinUSB driver az RTL-SDR-hez, `pyrtlsdr` + `librtlsdr.dll`; SatDump Windows build. Mock/Replay eszközzel SDR nélkül is fejleszthető.
- **Raspberry Pi 5:** `apt install librtlsdr0 rtl-sdr soapysdr-module-rtlsdr`, blacklist `dvb_usb_rtl28xxu` kernel modul, udev szabály, **systemd service** az automatikus induláshoz, SatDump aarch64 build/csomag. RTL-SDR 2.4 MS/s a Pi 5-nek nem gond; a dekódolás (LRPT) időben elfér.
- Időszinkron fontos: NTP/chrony a Pi-n (az átvonulási idők pontossága miatt), Windowson a rendszeridő.
- Telepítő szkriptek mindkét platformra.

---

## 13. Tesztelés

- **Unit:** pass-előrejelzés (ismert TLE + ismert átvonulás), ütközésfeloldás (táblázatos tesztesetek), SigMF írás/olvasás, FM demod szintetikus jelen.
- **Integráció:** `MockDevice` / `FileReplayDevice` + teljes pipeline futtatás; ismert LRPT / SSTV minta-IQ felvétel a regressziós teszthez (CI-ben).
- **Idő-szimuláció:** a scheduler órája injektálható, így napokat lehet percek alatt lejátszani.
- **Kézi hardveres teszt:** ISS (25544) FM/SSTV átvonulás, Meteor átvonulás Windowson, majd Pi-n.

---

## 14. Ütemterv (fázisok)

| Fázis | Tartalom | Eredmény |
|-------|----------|----------|
| **0. Alap** | Repo, config, DB séma, logging, projektváz | Fut az üres szolgáltatás |
| **1. Pályaszámítás** | TLE letöltés, pass predictor, API + minimális UI lista | Látható a következő átvonulás |
| **2. HAL + felvétel** | SdrDevice, RTL-SDR, Mock/Replay, SigMF, kézi felvétel indítás | Kézzel indított IQ felvétel |
| **3. Scheduler** | Szabályok, prioritás, ütközésfeloldás, automatikus job indítás, Doppler | Automatikus felvétel |
| **4. Pipeline + FM** | **Pályametaadat a felvétel mellé (`.orbit.json`, 7.1)**, plugin keret, FM demod, artifact tárolás, galéria v1; **eseménynapló (`events`), `job_steps`, job-részletek oldal, `/api/health/detailed`** | Első automatikus hang a galériában; a hibák és a pipeline-lépések állapota látszik a UI-n |
| **5. Waterfall + hang utólag** | Felvétel oldal, generálási jobok | Utólagos elemzés |
| **6. SSTV** | SSTV processzor, galéria képmegjelenítés | ISS SSTV kép |
| **7. Meteor LRPT** | SatDump wrapper, kompozitok | Meteor képek |
| **8. UI csiszolás** | Naptár nézet, pipeline szerkesztő, élő állapot; **SSE állapotsor, toast értesítések, hibaüzenet-javaslatok, nyugtázás** | Teljes használható UI |
| **9. Adatkezelés és tárhely** | Tárhely oldal (használat, kvótasáv, szűrés/rendezés), IQ külön törlése, tömeges törlés, védett jelölés, letöltés és ZIP-export, mentési hely módosítása (`storage_roots`, áthelyezés job), automatikus megőrzés (kvóta, IQ megőrzési idő), `storage_unavailable` hiba | Az adatok a felületről kezelhetők, a mentés helye állítható, a lemez nem telik meg észrevétlenül |
| **10. Pi telepítés** | systemd, install script (SatDump ellenőrzés/telepítés), külső meghajtó csatolása, hosszútávú teszt | Éles üzem |
| **11. Bővítések** | Antennaváltó, multi-SDR, egy felvétel több műholdra, további dekóderek | |

---

## 15. Kockázatok és megjegyzések

- **Egy SDR – egyszerre egy sáv:** a 137 MHz-es (Meteor) és 145.8 MHz-es (ISS) sávok egy 2.4 MS/s ablakon belül nem férnek el együtt (~8.7 MHz távolság) → ütközésfeloldás szükséges.
- **Doppler:** VHF-n ±3 kHz körüli eltolódás; NFM-hez és LRPT-hez a követés/utánhangolás vagy szoftveres korrekció (utófeldolgozásban IQ-n) szükséges.
- **Antenna minősége** (V-dipól/turnstile/QFH 137 MHz-re, LNA, szűrő) a dekódolás sikerét jobban meghatározza, mint a szoftver.
- **Tárhely:** a nyers IQ gyorsan nő → megtartási szabályok és kvóta kötelező.
- **Műholdak állapota:** a Meteor LRPT adás és az ISS SSTV események nem folyamatosak; az ISS SSTV eseményekre naptár-alapú szabályok javasoltak.
- **Licenc:** SatDump GPLv3 – külső processzként hívjuk, így a saját kód licence szabadon választható.
- **Jogi:** csak vétel – nincs adás.

---

## 16. Tisztázott döntések

| # | Téma | Döntés | Hatás a tervre |
|---|------|--------|----------------|
| 1 | Helyszín | Még nem ismert | Beállításokban megadható (lat/lon/magasság); amíg nincs megadva, az átvonulás-számítás nem indul, a UI figyelmeztet |
| 2 | Antenna | Még nem ismert | Antenna/sáv réteg megvan, de induláskor egy „alap" szélessávú antenna profil, váltó nélkül |
| 3 | Első műholdak | ISS FM, ISS SSTV (25544), Meteor-M2-3 / M2-4 LRPT (57166 / 59051) | NOAA APT kimarad az első körből (későbbi plugin) |
| 4 | Frontend | React + TypeScript | Node csak fejlesztéshez/build-hez kell |
| 5 | LRPT dekóder | SatDump külső bináris | Elérési út konfigurálható, telepítő szkript letölti/ellenőrzi |
| 6 | SDR szám | Később több is lehet | A scheduler **erőforrás-alapú** (több eszköz = több sáv), már az első verziótól |
| 7 | Ütközés | Részleges felvétel engedélyezett | Szabályonként tiltható; a részleges felvétel státusza a UI-n látszik |
| 8 | Élő nézet | **Élő waterfall** vétel közben | Új feladat: WebSocket spektrum-streamelés (decimált FFT sorok a szerverről), lásd 17. fejezet. Élő hang nem kell |
| 9 | Hozzáférés | Helyi hálózat + egyszerű jelszó/token | Egy közös jelszó/API token, nincs felhasználókezelés |
| 10 | Tárhely | Még nem ismert | Alapértelmezett kvóta 50 GB, IQ megőrzés 7 nap, mindkettő állítható |
| 11 | Elevációs szög / puffer | Min. elevációs szög 5–10° (alap: 10°, szabályonként állítható), puffer 60 mp AOS előtt és LOS után | Alacsony elevációnál gyengébb a jel, a pass-lista szűrhető max. elevációra |
| 12 | Nyelv | Magyar + angol | i18n a React-ben (pl. `react-i18next`) az első naptól |
| 13 | Jövőbeli SDR | Még nem ismert | SoapySDR alapú HAL marad, nincs eszközspecifikus feltételezés |
| 14 | Hibák/állapotok | Látszódjanak a felületen | Eseménynapló, állapotsor és job-részletek, lásd 10.1 |
| 15 | Pályametaadat | Minden nyers felvétel mellé mentődik | `.orbit.json` TLE-vel, megfigyelővel, átvonulás- és Doppler-idősorral, lásd 7.1 |
| 16 | Adatkezelés | A mentett adatok megtekinthetők, törölhetők, letölthetők; a mentés helye módosítható | Tárhely oldal, IQ-only törlés, ZIP-export, `storage_roots` és áthelyezés job, automatikus megőrzés, lásd 10.2 |

## 17. Kiegészítés: élő waterfall

- A Recorder minden IQ blokkból mellékágon (az írást nem blokkolva) FFT-t számol (pl. 1024–2048 pont, ~10 sor/mp), dB-ben, uint8-ra kvantálva.
- A szerver WebSocketen küldi a sorokat a bejelentkezett klienseknek; ha senki nem néz, nincs FFT-számítás.
- A frontend `<canvas>`-ra rajzol görgetve, colormap-pel; Pi 5-ön a terhelés elhanyagolható.
- A tárolt felvételekből generált waterfall (9. fejezet) ugyanazt a színezést és FFT-kódot használja.

## 18. Állapot és következő lépés

- **Kész:** 0. (alap), 1. (pályaszámítás, TLE, minimális UI), 2. (SDR-réteg, SigMF, kézi felvétel), 3. (ütemező, szabályok, ütközésfeloldás, Doppler), 4. (pályametaadat `.orbit.json`, pipeline-keret, FM demodulátor, artifact-tárolás, galéria v1, eseménynapló, job-lépések, állapotellenőrzés) fázis.
- **5. fázis kész:** utólagos waterfall (`waterfall` processzor: FFT-méret, időtartomány- és frekvencia-kivágás, színtérkép, dinamikatartomány, Doppler-overlay, tengelyek) és hang (`audio_demod`: NFM / WFM / AM / USB / LSB, Doppler-követés a pályametaadatból, időtartomány) ad-hoc jobként a Felvétel-részletek oldalon, kijelöléssel a waterfallon. A spektrum- és színtérkép-kód a `dsp/spectrum.py`-ban van, ezt használja majd az élő waterfall is.
- **6. fázis kész:** SSTV-dekóder (`sstv` processzor, `dsp/sstv.py`): 14 mód (Robot36/72, Martin M1/M2, Scottie S1/S2/DX, PD50/90/120/160/180/240/290), VIS-felismerés, VIS nélküli módfelismerés a szinkron-távolságból, robusztus szinkronkövetés (csúszás- és Doppler-korrekció), több kép egy felvételből, részleges képek. A galéria megjeleníti a módot és a sorok számát. Új alap pipeline: „ISS SSTV" (`fm_demod` → `sstv`).
  - **Ellenőrzés:** a módtáblázatot a független `pysstv` kódoló kimenetével vetettük össze (Robot36, Martin M1/M2, Scottie S1/S2/DX, PD90–PD290: mind helyesen dekódolódik, ez javította a PD180/PD240 VIS-kódját is). **Nem volt független ellenőrzés** a PD50-re és a Robot72-re (nincs a pysstv-ben); ezek a szabvány szerinti időzítést követik. Valódi ISS-felvétellel még nem teszteltük.
  - **Javított hiba:** a pipeline lépései nem osztották meg a `ctx.state`-et (a 4. fázis óta); mostantól egy jobon belül közös.
- **7. fázis kész (SatDump nélkül tesztelve):** `meteor_lrpt` processzor ([meteor_lrpt.py](src/satrx/processors/meteor_lrpt.py)): a nyers IQ-t a SatDump külső programmal dekódolja, a kimeneti képeket (kompozitok elöl, utána a csatornák) a galériába teszi, a köztes fájlokat (CADU, soft szimbólumok) törli. Támogatja a SatDump új (`satdump pipeline ...`) és régi parancssori stílusát (automatikus tartalékkal), a 72k és 80k LRPT pipeline-t (auto: előbb 72k, kép nélkül 80k), időkorlátot és érthető hibákat. A SatDump útvonala a Beállítások oldalon vagy `tools.satdump_path`-ként adható meg; az állapotellenőrzés figyelmeztet, ha egy aktív szabály LRPT-t kér, de nincs SatDump. Új alap pipeline: „Meteor LRPT" (siker után törli az IQ-t, nincs eredmény / hiba esetén megtartja).
  - **Nem ellenőrzött:** a gépen nincs SatDump, ezért a hívást egy **hamis `satdump`** ellen teszteltük (tests/fake_satdump.py), amely a dokumentált parancssori szerződést utánozza. A valódi program kimeneti fájlneveit (pl. `MSU-MR/…png`) és a `--baseband_format` elfogadott értékeit (cu8 / cs16 / cf32; régi verzión w8 / i16 / f32) a dokumentáció alapján vettük, valódi felvétellel és SatDump-pal még ellenőrizni kell. A lépés naplója tartalmazza a pontos parancssort és a SatDump kimenetének végét, ez segít az eltérések kijavításában (`baseband_format`, `cli_style`, `extra_args` paraméterekkel felülbírálható).
  - **Tárhely:** egy 10 perces LRPT-felvétel 1,024 MS/s-on kb. 1,2 GB; az IQ-megőrzési idő és a kvóta automatikus érvényesítése még nincs megírva (9. fázis környéke).
- **8. fázis kész:**
  - **SSE** (`GET /api/stream`): másodpercenként `state` pillanatkép (SDR, felvétel előrehaladása, következő átvonulás, futó feldolgozások, nyugtázatlan hibák) és `event` az új eseménynapló-bejegyzésekről (legfeljebb ~1 mp késéssel). A böngésző `EventSource`-a a tokent a `?token=` paraméterrel adja át.
  - **Állapotsor** minden oldalon (SDR/felvétel folyamatjelzővel, visszaszámláló a következő felvételig, feldolgozások, hibagomb, kapcsolatjelző) és **toast értesítések** (hibák és figyelmeztetések a magyar/angol tanáccsal, a kattintás az Események oldalra visz; a sikeres feldolgozás/felvétel rövid ideig látszik).
  - **Élő waterfall** (`/api/live/waterfall`, WebSocket): a felvétel közben ~10 sor/mp, a számítás csak akkor fut, ha van néző; a sorok bájtosak, a színezés a böngészőben állítható (szintek, színtérkép); a Doppler-újrahangolást jelzi, menet közben csatlakozó néző azonnal megkapja a fejlécet, lemaradó kliensnél a legrégebbi sorokat eldobja (a felvétel nem lassul). Az FFT-kód és a dBFS-skála közös az utólagos waterfallal.
  - **Idővonal-nézet** az Átvonulások oldalon (műholdanként egy sáv, a felvételi ablak, állapotszínek, ütközések, 12 órától 7 napig), **pipeline-szerkesztő** (lépések hozzáadása/sorrendje/törlése, a paraméterűrlap a processzor JSON-sémájából épül, másolás, törlés), mélylinkek a fülekhez (`#passes`).
  - Új események: `recording_started`, `recording_finished`.
  - **Ellenőrzés:** a valós idejű működést tényleges böngészőben (Edge, DevTools-protokollal) is kipróbáltuk: élő waterfall, állapotsor, toast, jelvény, idővonal és szerkesztő; a konzolban nem volt hiba. Automatikus frontend-tesztek (pl. vitest) még nincsenek.
- **9. fázis kész (adatkezelés és tárhely, 10.2):**
  - **Tárhely oldal:** használat kvótasávval (felvételek + eredmények), növekedés és becsült megtelés, gyökerenkénti foglalt/szabad hely és elérhetőség; a tárolt felvételek listája szűréssel (van/nincs nyers IQ, védett, feldolgozatlan) és rendezéssel (dátum, méret, IQ, eredmények), kijelöléssel.
  - **Törlés:** nyers IQ külön (az eredmények, a metaadat és a pályaadat megmarad) vagy mindent; tömegesen; **száraz futtatással** (a megerősítő ablak előre mutatja a felszabaduló helyet és a kimaradókat: védett, használatban lévő, nincs IQ, elérhetetlen tárhely); csak nyilvántartott fájlokat töröl; **védett** jelölés; a Galériában az eredmények kijelölése, törlése és ZIP-je.
  - **Automatikus megőrzés** (`storage/retention.py`, percenként a háttérszálban + kézi előnézet/futtatás): az IQ megőrzési ideje (feldolgozott, nem védett felvételekre) és a kvóta (legrégebbi feldolgozottól, a kvóta 90%-áig; végső esetben a feldolgozatlan is, figyelmeztetéssel); a védett és használatban lévő felvételt sosem törli.
  - **A mentés helye módosítható:** felvételek és eredmények gyökérmappája külön, a UI-ról, újraindítás nélkül; ellenőrzés mentés előtt (abszolút út, nem a gyökér/saját mappa/alkalmazás könyvtára, létezik vagy létrehozható, írható, szabad hely, nem üres figyelmeztetés). Az új adat az aktív gyökérbe kerül, a régi a helyén marad és elérhető (`storage_roots`, `root_id`). **Áthelyezés job**: ellenőrző összeggel (SHA-256) másol, csak ezután állítja át az adatbázist és törli a forrást; megszakítható és folytatható, a hibás elem eredetije érintetlen marad, a használatban lévőt kihagyja, szabad helyet előre ellenőriz. Felvétel/feldolgozás közben a hely nem módosítható.
  - **Elérhetőség:** minden gyökérben `.satrx-root` jelzőfájl, így egy ki nem csatolt meghajtó üres csatolási pontját nem tekintjük elérhetőnek (nem kezd a rendszermeghajtóra írni); az állapotellenőrzés `storage_unavailable` hibát jelez, új felvétel nem indul, az érintett fájlok elérése 503.
  - **Letöltés és export:** streamelt letöltés HTTP Range-gel (IQ, meta, pályaadat, eredmény); exportjob: ZIP (tömörítetlen, `MANIFEST.json` ellenőrző összegekkel) vagy másolás a szerveren megadott mappába, opcionális **áthelyezéssel** (csak az ellenőrzött másolat után, a védettet kihagyva); a régi ZIP-ek 24 óra után törlődnek.
  - **Eltérések a tervtől:** az elérhetetlen tárhely miatt kimaradó átvonulás `failed` státuszt és `recording_start_failed` eseményt kap (nem `missed`); az exportjobok állapota memóriában él (újraindítás után elvész, a kész ZIP-ek a lemezen maradnak); a mentési útvonalak a `storage_roots` táblában vannak (nem a `settings` táblában).
  - **Nem ellenőrzött:** kihúzott/újracsatolt valódi USB-meghajtó és hálózati meghajtó (a jelzőfájl eltűnését szimuláltuk); Linuxos útvonalak és jogosultságok a Pi-n.
- **10. fázis kész (Pi telepítés és üzemeltetés), valódi Pi nélkül tesztelve:**
  - **Telepítés:** `scripts/install_pi.sh` (Raspberry Pi OS / Debian / Ubuntu, idempotens): rendszercsomagok, `satrx` rendszerfelhasználó, program az `/opt/satrx` alá, virtuális környezet, **véletlen API-tokenes** konfiguráció, RTL-SDR udev-szabály és a DVB-kernelmodul tiltása, chrony, systemd szolgáltatás, SatDump ellenőrzés (`--satdump-deb URL`), `satrx doctor`. Kapcsolók: `--update`, `--dry-run`, `--no-systemd`, `--build-web`, `--uninstall [--purge-data]`. Windowsra `scripts/install_windows.ps1` (`-DryRun`, `-Backend`). `scripts/make_release.py`: kiadási csomag a megépített webes felülettel (a Pi-n nem kell Node.js). `scripts/fstab_line.sh`: UUID-alapú, `nofail` fstab-sor külső meghajtóhoz (`--apply` mentéssel).
  - **systemd:** `Type=notify` + `WatchdogSec=180`; a háttérszál életjelet küld (`sdnotify.py`, függőség nélkül), megakadás esetén a systemd újraindítja; szigorított beállítások (nem root, `NoNewPrivileges`, `ProtectSystem=full`), a külső meghajtók írhatók.
  - **Összeomlás utáni helyreállítás** (`core/recovery.py`, minden induláskor): a félbemaradt „felvétel/feldolgozás" átvonulások és jobok `failed` státuszt kapnak, a lezáratlan IQ-hoz pótlódik a SigMF metaadat (a megmaradt felvétel feldolgozható), a `.part` maradékok törlődnek, `recovered_after_crash` figyelmeztetés kerül az eseménynaplóba. SQLite: WAL + `synchronous=NORMAL` + `busy_timeout` (kíméli az SD-kártyát).
  - **Diagnosztika:** `python -m satrx doctor [--json]`: Python, függőségek, web build, konfiguráció (token, megfigyelő), adatkönyvtár, tárhelygyökerek elérhetősége, SDR (a futó szolgáltatással nem ütközik: csak felsorol), SatDump, óraszinkron (Linux), DVB-modul és udev (Linux), port, szolgáltatás állapota; nem nulla kilépési kód hiba esetén.
  - **Hosszútávú teszt** (`scripts/soak_test.py`): a valódi szolgáltatást futtatja külön folyamatban, ciklusonként felvétel + feldolgozás, SSE- és WebSocket-kliensek nyitva, kis kvóta (a megőrzés is lefut); mér memóriát, szálakat, nyitott fájlokat, CPU-t, lemezt; PASS/FAIL ítélet. **A memóriaszivárgásról 30 percnél rövidebb futásból nem mond ítéletet.**
  - **A hosszútávú teszt által feltárt hiány, javítva:** a kvóta az eredményeket is számolta, de az automatikus megőrzés csak a nyers IQ-t törölte, így az eredmények felhalmozódása tartós `quota_exceeded` hibát okozott volna. Új, opcionális `storage.artifact_retention_days` (alap 0 = nem töröl; a védett felvételek eredményeit sosem törli).
  - **Mérés (Windows, mock SDR, 5 perc, 30 ciklus):** 30/30 ciklus hibátlan, 0 API-hiba, szálak 59–61, nyitott fájlok növekedése −3, átlagos CPU ~5%, az SSE legfeljebb 1 mp-re hallgatott el, a megőrzés háromszor lefutott, az állapot a végén a kvótán belül maradt. A memóriáról a futás túl rövid volt ítélethez.
  - **Nem ellenőrzött:** minden valódi Raspberry Pi-n és Linuxon (a szkripteket Windowson Git Bash-sel szintaxis- és szárazfutás-szinten, a PowerShell-szkriptet Windowson szintén szárazfutással teszteltük; a Docker/WSL nem volt használható); a systemd-egység betöltése; az `sd_notify` valódi systemd ellen (Windowson a valódi Unix-socketes teszt kimarad, a protokollt hamis sockettel teszteltük); RTL-SDR hardver; az `install_pi.sh` apt-lépései és a SatDump arm64 csomag; hosszú (órás) memóriamérés.
  - **Dokumentáció:** `README.md`, `docs/RASPBERRY_PI.md` (telepítés, SatDump, külső meghajtó, üzemeltetés, biztonsági mentés, hibakeresés, biztonság).
- **Első valódi hardveres próba (Windows, RTL-SDR) tanulságai és javítások:** a felhasználó bedugta az RTL-SDR-t, de a felület nem mutatta, hogy csatlakozott-e, és az első „átvonulás" nem tűnt valósnak. Ok: az alapértelmezett `sdr.backend: mock` szintetikus jelet adott, ami sehol nem volt jelezve; ráadásul a Windowsnak nem volt működő (WinUSB) illesztője az eszközhöz (a `pyrtlsdr` `LIBUSB_ERROR_NOT_FOUND` hibát adott), és a `pyrtlsdr` sem volt telepítve. Javítások: `GET /api/sdr` és az SDR állapotkártya (backend, felismert eszközök, újrakeresés gomb; hibakódok: `sdr_mock`, `sdr_no_package`, `sdr_no_library`, `sdr_no_driver`, `sdr_not_found`); **állandó „SZIMULÁLT SDR" jelzés az állapotsorban**; a felvételek megjegyzik a forrásukat (`recordings.source`, SigMF `satrx:sdr_backend`, `orbit.json`), és a listákban „szimulált" jelvényt kapnak; `satrx doctor` felismeri a hiányzó Windows-illesztőt (Zadig-útmutatóval); a felvétel indításakor a megnyitási hiba magyarázza a teendőt; `pip install -e ".[rtlsdr]"` Windowson a DLL-eket is hozza (`pyrtlsdr[lib]`). **Nem ellenőrzött:** tényleges vétel valódi RTL-SDR-rel (a WinUSB-illesztő telepítése a felhasználó teendője, Zadig, rendszergazdai jog).
- **Időzített események, menüegyszerűsítés és konzol (a felhasználó két új kérése) kész:**
  - A 10.3 és 10.4 fejezet szerint: `timed_events` tábla (0006-os migráció), `scheduler/occurrences.py`, a tervező és az ütemező kiterjesztése, API és UI; a felvételi/feldolgozási események részletes adatokat kapnak; a felület hét főmenüre egyszerűsödött (Átvonulások almenükkel), a főoldalon konzol van.
  - **Tesztek:** 267 automatikus teszt zöld (köztük 27 új, `tests/test_timed_events.py`: előfordulások, nyári időszámítás 2026-ban, heti napok, ismétlés vége, API-ellenőrzések, megfigyelési pont nélküli ütemezés, prioritás és részleges átfedés, felvétel–feldolgozás–konzolesemények végig). A tesztek **egy hibát találtak**: az ismétlődő esemény első előfordulása elveszett, ha a kezdet tört másodpercet tartalmazott (javítva).
  - **Böngészőben ellenőrizve** (Edge, mock SDR): a menük, az időzített események űrlapja és listája, a konzol az élő felvétel indulásával; konzolhiba nem volt. A felvétel végét és a feldolgozást a konzolon nem néztük meg szemmel, csak az API-tesztek fedik.
  - **Nem ellenőrzött:** frontend-egységtesztek nincsenek; a nyári időszámítás-váltás a böngészői időzóna-átváltásnál (`zonedToIso`) csak a kód átnézésével; a Linux/Pi, a Docker és a WSL továbbra sem lett kipróbálva.
- **`rtl_sdr` és `rtl_fm` programok használata (a felhasználó kérésére):**
  - **`sdr.backend: rtl_sdr`** ([rtlsdr_tool.py](src/satrx/hardware/rtlsdr_tool.py)): a nyers IQ rögzítését a külső `rtl_sdr` program végzi (`-f -s -g -d -p -`, a kimenet a stdout), a satrx a csövön olvassa és SigMF-be írja; pyrtlsdr nem kell. Gain megadása nélkül a program automatikus erősítést használ. Újrahangolás (Doppler) a program újraindításával történik (~0,2 s mintakiesés), fix frekvenciánál nincs hatása. A program útvonala: `tools.rtl_sdr_path` -> PATH -> szokásos telepítési helyek. A régi `rtlsdr` (pyrtlsdr) backend megmaradt.
  - **Közvetlen hangfelvétel az `rtl_fm`-mel** (`POST /api/recorder/start-fm`, a Felvételek oldalon „Hang közvetlenül (rtl_fm)"): a demodulálást (fm / wbfm / am / usb / lsb), a sávszélességet (`-s`), a zajzárat (`-l`) és az erősítést a program végzi, a kimenet WAV az eredmények (Galéria) közé kerül, **nyers IQ nélkül** (így utólagos újrafeldolgozás, waterfall és pályametaadat sincs hozzá). Csak valódi SDR-rel működik (mock/replay esetén 422).
  - Az SDR-állapot (`sdr_no_tool` hibakód), a `satrx doctor` és a konfigurációs példa ismeri az új backendet.
  - **Ellenőrzés:** hamis `rtl_sdr`/`rtl_fm` programmal (tests/fake_rtl.py, 9 teszt): argumentumok, normalizálás, újraindítás újrahangoláskor, hibaüzenetek, teljes felvétel és WAV-felvétel, események. **Nem ellenőrzött:** a valódi `rtl_sdr`/`rtl_fm` és az RTL-SDR hardver (a gépen a programok nincsenek telepítve); az `rtl_test` kimenetének értelmezése a valódi programmal.
- **Felület-kiegészítések az `rtl_sdr` bevezetése után:**
  - **Külső programok útvonala a Beállítások oldalon** (rtl_sdr, rtl_fm, SatDump): mentés után azonnal érvényes, az adatbázisban él és felülírja a YAML-t; csak a várt nevű program adható meg (`rtl_sdr`, `rtl_fm`, `satdump`), így a felületről nem indítható tetszőleges program.
  - **Átvonulások → Ütemezés** négy külön részre bontva: Műholdas szabályok, Időpontos felvételek, Rádióprofilok, Pipeline-ok.
  - **TLE:** a frissítés megmondja, hogy volt-e új adat (a forrás gyakran ugyanazt adja), megjelenik a TLE kora, van „Összes TLE frissítése" gomb (hibatűrő), a CelesTrak 404-e érthető üzenet.
  - **Műhold frekvenciák és módok („csatornák", `satellite_channels`, 0008-as migráció):** műholdanként több frekvencia, mód (FM hang / SSTV kép / Meteor LRPT / csak felvétel) és módhoz tartozó beállítás (SSTV dekódolási mód pl. Robot36, LRPT típus, zajzár, waterfall). Mögötte karbantartott rádióprofil és pipeline; a szabály a csatornát választja (üresen az első).
  - **Menetrend:** az idővonalon az ütemezetlen (TLE-vel rendelkező) átvonulások is látszanak (szaggatott szürke, kapcsolható), és egy kiválasztott átvonulás a műhold csatornái közül választva egyetlen kattintással beütemezhető (`POST /api/passes/schedule`: egy átvonulásra szóló rövid érvényességű szabály).
  - **Kód-review (2026-10-30) javításai:** a csatorna profilja/pipeline-ja nem törölhető a Profilok/Pipeline-ok oldalról (409); a műhold törlése 409-et ad 500 helyett, ha egy időpontos esemény használja a frekvenciáit; az ütemezetlen átvonulások azonosítója stabil (korábban a lista sorszáma volt: újratöltéskor elcsúszhatott a kijelölés); `rtl_sdr` backendnél nincs menet közbeni Doppler-újrahangolás (az újrahangolás a program újraindítása, ~0,2–1 s mintakiesés ~12 mp-enként); a `doctor` Windows USB-ellenőrzése az `rtl_sdr` backendre is él.
  - **Ismert hiányosságok / javaslatok:** (1) az `rtl_test` eszközfelsorolás a felvétel indulásával ütközhet (a health-lekérdezés ~6 s-ig foglalhatja az eszközt) – a próbát a felvételkezelő foglaltsági zárjával kell védeni; (2) az egyszeri átvonulás-beütemezések szabályként halmozódnak – lejárat után automatikus takarítás kell; (3) nincs ütemezés-visszavonás az idővonalról; (4) a nyári időszámítás „hiányzó" (pl. 02:30 márciusban) és „kétszeres" (októberi) helyi időpontjára nincs teszt; (5) a tesztek egy része a valódi mai naphoz kötött (kétszer is elromlott a naptár múlásával): óra-injektálás vagy fagyasztás kellene; (6) nincs frontend-teszt (vitest/Playwright) és nincs CI; (7) kézi TLE / Space-Track forrás a CelesTrak által nem követett műholdakhoz; (8) a `satellites.default_profile_id/default_pipeline_id` a felületről kikerült, a csatornák váltották – a mezők törölhetők; (9) egy rövid „első indítás" varázsló (megfigyelési pont, SDR-ellenőrzés, első műhold és frekvencia) csökkentené a belépési nehézséget; (10) több SDR/USB-eszköz kiválasztása a felületről.
- **Következő:** a tényleges Raspberry Pi-s kipróbálás (telepítés, `satrx doctor`, éjszakai `soak_test.py`, valódi RTL-SDR és antenna), majd a 11. fázis (bővítések: antennaváltó, több SDR, egy felvétel több műholdra, további dekóderek). A tárhelykezelést érdemes a Pi-s telepítés (10. fázis) előtt megcsinálni, mert egy 10 perces LRPT-felvétel kb. 1,2 GB.
- **4. fázis eltérések a tervtől:** a `recordings` tábla csak az `.orbit.json` útvonalát tárolja (az átvonulás fő adatai nem külön oszlopok, mert a `passes` táblából lekérdezhetők); a frissítés SSE helyett egyelőre 5–10 mp-es lekérdezéssel történik (az SSE a 8. fázisban jön); az FM demodulátor keskenysávú (NFM), a szélessávú FM későbbi bővítés.
- A „még nem tudom" pontok (helyszín, antenna, tárhely) a Beállítások oldalon később kitölthetők.
