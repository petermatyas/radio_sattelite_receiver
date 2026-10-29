import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, Observer, Pass, Pipeline, RecorderStatus, Recording, Satellite, ToolsInfo } from "./api";
import { fmt, mb, useAsync } from "./hooks";
import DashboardPage from "./DashboardPage";
import EventsPage from "./EventsPage";
import GalleryPage from "./GalleryPage";
import RecordingDetail from "./RecordingDetail";
import LiveWaterfall from "./LiveWaterfall";
import PassTimeline from "./PassTimeline";
import SchedulePage from "./SchedulePage";
import StoragePage from "./StoragePage";
import { StatusBar, Toasts } from "./StatusBar";
import { useStream } from "./stream";

type Tab = "dashboard" | "passes" | "recordings" | "gallery" | "storage" | "events" | "settings";
type Sub = "plan" | "satellites" | "schedule";

/** Régi mélylinkek: az ütemezés, a pipeline-ok és a műholdak az Átvonulások alá kerültek. */
const LEGACY: Record<string, [Tab, Sub?]> = { schedule: ["passes", "schedule"], pipelines: ["passes", "schedule"], satellites: ["passes", "satellites"] };

function PassesPlan() {
  const { t } = useTranslation();
  const [hours, setHours] = useState(48);
  const [view, setView] = useState<"timeline" | "table">("timeline");
  const passes = useAsync<Pass[]>(() => api.passes(hours));
  const observer = useAsync<Observer | null>(() => api.observer());
  const { tick } = useStream();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(passes.reload, [hours, tick]);
  useEffect(() => {
    const id = setInterval(passes.reload, 30000);
    return () => clearInterval(id);
  }, [passes.reload]);
  return (
    <>
      {observer.data === null && <div className="warn">{t("observerMissing")}</div>}
      {passes.error && <p className="err">{passes.error}</p>}
      <div className="row">
        <label>
          {t("range")}
          <select value={hours} onChange={(e) => setHours(Number(e.target.value))}>
            <option value={12}>12 {t("hoursShort")}</option><option value={24}>24 {t("hoursShort")}</option>
            <option value={48}>48 {t("hoursShort")}</option><option value={168}>7 {t("daysShort")}</option>
          </select>
        </label>
        <button className={`tab ${view === "timeline" ? "active" : ""}`} onClick={() => setView("timeline")}>{t("timeline")}</button>
        <button className={`tab ${view === "table" ? "active" : ""}`} onClick={() => setView("table")}>{t("table")}</button>
      </div>
      {view === "timeline" && <PassTimeline passes={passes.data ?? []} hours={hours} />}
      {view === "table" && <table>
        <thead>
          <tr>
            <th>{t("satellite")}</th><th>{t("aos")}</th><th>{t("tca")}</th>
            <th>{t("los")}</th><th>{t("maxEl")}</th><th>{t("recWindow")}</th><th>{t("status")}</th>
          </tr>
        </thead>
        <tbody>
          {passes.data?.map((p) => (
            <tr key={p.id}>
              <td>{p.kind === "event" ? `⏱ ${p.satellite_name}` : `${p.satellite_name} (${p.norad_id})`}</td>
              <td>{fmt(p.aos)}</td><td>{fmt(p.tca)}</td><td>{fmt(p.los)}</td>
              <td>{p.max_elevation_deg.toFixed(1)}°</td>
              <td>{p.rec_start && p.rec_end ? `${new Date(p.rec_start).toLocaleTimeString()} – ${new Date(p.rec_end).toLocaleTimeString()}` : "-"}</td>
              <td className={`st-${p.status}`}>{t(`st_${p.status}`)}</td>
            </tr>
          ))}
        </tbody>
      </table>}
      {view === "table" && passes.data?.length === 0 && <p>{t("noPasses")}</p>}
    </>
  );
}

const SUBS: Sub[] = ["plan", "satellites", "schedule"];

/** Átvonulások: menetrend, műholdak és ütemezés (szabályok, időzített események, profilok, pipeline-ok) egy helyen. */
function PassesPage({ sub, onSub }: { sub: Sub; onSub: (s: Sub) => void }) {
  const { t } = useTranslation();
  return (
    <>
      <div className="row subtabs">
        {SUBS.map((s) => <button key={s} className={`tab ${sub === s ? "active" : ""}`} onClick={() => onSub(s)}>{t(`sub_${s}`)}</button>)}
      </div>
      {sub === "plan" && <PassesPlan />}
      {sub === "satellites" && <SatellitesPage />}
      {sub === "schedule" && <SchedulePage />}
    </>
  );
}

function RecordingsPage() {
  const { t } = useTranslation();
  const recs = useAsync<Recording[]>(() => api.recordings());
  const status = useAsync<RecorderStatus>(() => api.recorderStatus());
  const [form, setForm] = useState({ freq: "145.800", rate: "250", bw: "", duration: "30", gain: "", kind: "iq", fmMode: "fm", squelch: "0" });
  const [error, setError] = useState<string>();
  const pipelines = useAsync<Pipeline[]>(() => api.pipelines());
  const [pipelineId, setPipelineId] = useState("");
  const [info, setInfo] = useState<string>();
  const [detailId, setDetailId] = useState<number>();
  const busy = status.data !== undefined && status.data.state !== "idle";
  const runnable = pipelines.data?.filter((p) => p.steps.length > 0) ?? [];
  const process = async (recId: number) => {
    try {
      const r = await api.processRecording(recId, Number(pipelineId || runnable[0]?.id));
      setInfo(t("processStarted", { id: r.job_id }));
      setError(undefined);
    } catch (e) { setError((e as Error).message); }
  };

  useEffect(() => {
    const id = setInterval(() => { status.reload(); recs.reload(); }, busy ? 1000 : 5000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [busy]);

  const start = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      if (form.kind === "fm") {
        await api.startFmRecording({
          center_freq_hz: Math.round(Number(form.freq) * 1e6), duration_s: Number(form.duration), mode: form.fmMode,
          bandwidth_hz: Math.round(Number(form.bw || "12.5") * 1e3), gain_db: form.gain === "" ? null : Number(form.gain),
          squelch: Number(form.squelch) || 0,
        });
        setError(undefined);
        status.reload();
        return;
      }
      await api.startRecording({
        center_freq_hz: Math.round(Number(form.freq) * 1e6),
        sample_rate_hz: Math.round(Number(form.rate) * 1e3),
        duration_s: Number(form.duration),
        gain_db: form.gain === "" ? null : Number(form.gain),
        bandwidth_hz: form.bw === "" ? null : Math.round(Number(form.bw) * 1e3),
      });
      setError(undefined);
    } catch (err) { setError((err as Error).message); }
    status.reload(); recs.reload();
  };

  const field = (key: keyof typeof form, label: string, required = true) => (
    <label>
      {label}
      <input type="number" step="any" required={required} value={form[key]} onChange={(e) => setForm({ ...form, [key]: e.target.value })} />
    </label>
  );

  if (detailId !== undefined) return <RecordingDetail id={detailId} onBack={() => { setDetailId(undefined); recs.reload(); }} />;

  return (
    <>
      <form className="row" onSubmit={start}>
        <label>{t("recKind")}
          <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}>
            <option value="iq">{t("recKindIq")}</option><option value="fm">{t("recKindFm")}</option>
          </select>
        </label>
        {field("freq", t("freqMhz"))}{form.kind === "iq" && field("rate", t("sampleRate"))}{field("bw", t("bandwidthKhz"), false)}
        {form.kind === "fm" && <>
          <label>{t("demodMode")}
            <select value={form.fmMode} onChange={(e) => setForm({ ...form, fmMode: e.target.value })}>
              {["fm", "wbfm", "am", "usb", "lsb"].map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
          </label>
          {field("squelch", t("squelch"), false)}
        </>}
        {field("duration", t("duration"))}{field("gain", t("gain"), false)}
        <button type="submit" disabled={busy}>{t("startRec")}</button>
        {busy && <button type="button" onClick={() => api.stopRecording().then(status.reload)}>{t("stopRec")}</button>}
      </form>
      <p>
        {busy && status.data
          ? `${t("recording")}: ${status.data.elapsed_s.toFixed(0)} / ${status.data.duration_s} s`
          : t("idle")}
      </p>
      {busy && <LiveWaterfall />}
      {(error || recs.error || status.data?.last_error) && (
        <p className="err">{t("error")}: {error ?? recs.error ?? status.data?.last_error}</p>
      )}
      {runnable.length > 0 && (
        <div className="row">
          <label>
            {t("pipeline")}
            <select value={pipelineId} onChange={(e) => setPipelineId(e.target.value)}>
              {runnable.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </label>
          {info && <span>{info}</span>}
        </div>
      )}
      <table>
        <thead>
          <tr><th>{t("started")}</th><th>{t("freqMhz")}</th><th>{t("sampleRate")}</th><th>{t("format")}</th><th>{t("size")}</th><th /></tr>
        </thead>
        <tbody>
          {recs.data?.map((r) => (
            <tr key={r.id}>
              <td>{fmt(r.started_at)}{(r.source === "mock" || r.source === "replay") && <span className="badge badge-sim" title={t("simulatedHint")}>{t("simulatedShort")}</span>}</td>
              <td>{(r.center_freq_hz / 1e6).toFixed(3)}</td>
              <td>{r.sample_rate_hz / 1e3}</td>
              <td>{r.sample_format}</td>
              <td>{mb(r.size_bytes)}</td>
              <td>
                <button onClick={() => setDetailId(r.id)}>{t("details")}</button>{" "}
                {!r.deleted && runnable.length > 0 && <button onClick={() => process(r.id)}>{t("process")}</button>}{" "}
                <button
                  disabled={status.data?.recording_id === r.id}
                  onClick={() => window.confirm(t("confirmDelete")) && api.deleteRecording(r.id).then(recs.reload).catch((e) => setError(e.message))}
                >
                  {t("remove")}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {recs.data?.length === 0 && <p>{t("noRecordings")}</p>}
    </>
  );
}

function SatellitesPage() {
  const { t } = useTranslation();
  const sats = useAsync<Satellite[]>(() => api.satellites());
  const [noradId, setNoradId] = useState("");
  const [error, setError] = useState<string>();

  const run = async (fn: () => Promise<unknown>): Promise<boolean> => {
    let ok = true;
    try { await fn(); setError(undefined); } catch (e) { setError((e as Error).message); ok = false; }
    sats.reload();
    return ok;
  };

  return (
    <>
      <form
        className="row"
        onSubmit={async (e) => {
          e.preventDefault();
          if (await run(() => api.addSatellite(Number(noradId)))) setNoradId("");
        }}
      >
        <label>
          {t("noradId")}
          <input value={noradId} onChange={(e) => setNoradId(e.target.value)} inputMode="numeric" pattern="[0-9]+" required />
        </label>
        <button type="submit">{t("add")}</button>
      </form>
      {(error || sats.error) && <p className="err">{t("error")}: {error ?? sats.error}</p>}
      <table>
        <thead>
          <tr>
            <th>{t("satellite")}</th><th>{t("noradId")}</th><th>{t("tleEpoch")}</th><th>{t("enabled")}</th><th />
          </tr>
        </thead>
        <tbody>
          {sats.data?.map((s) => (
            <tr key={s.norad_id}>
              <td>{s.name}</td>
              <td>{s.norad_id}</td>
              <td>{s.tle_epoch ? fmt(s.tle_epoch) : "-"}</td>
              <td>
                <input type="checkbox" checked={s.enabled} onChange={(e) => run(() => api.patchSatellite(s.norad_id, e.target.checked))} />
              </td>
              <td>
                <button onClick={() => run(() => api.refreshTle(s.norad_id))}>{t("refreshTle")}</button>{" "}
                <button onClick={() => run(() => api.deleteSatellite(s.norad_id))}>{t("remove")}</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

function ToolsSection() {
  const { t } = useTranslation();
  const [value, setValue] = useState("");
  const [info, setInfo] = useState<ToolsInfo>();
  const [error, setError] = useState<string>();
  useEffect(() => {
    api.tools().then((i) => { setInfo(i); setValue(i.satdump_path ?? ""); }).catch((e) => setError(e.message));
  }, []);
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    try { setInfo(await api.setTools(value.trim() || null)); setError(undefined); } catch (err) { setError((err as Error).message); }
  };
  return (
    <section>
      <h2>{t("tools")}</h2>
      <form className="row" onSubmit={save}>
        <label style={{ flex: 1, minWidth: "20rem" }}>
          {t("satdumpPath")}
          <input value={value} onChange={(e) => setValue(e.target.value)} placeholder={t("satdumpPlaceholder")} />
        </label>
        <button type="submit">{t("save")}</button>
      </form>
      {error && <p className="err">{t("error")}: {error}</p>}
      {info && (
        <p className={info.satdump.found ? "" : "hint"}>
          {info.satdump.found
            ? t("satdumpFound", { path: info.satdump.path, source: t(`src_${info.satdump.source}`, { defaultValue: info.satdump.source }) })
            : t("satdumpNotFound")}
        </p>
      )}
    </section>
  );
}

function SettingsPage() {
  const { t } = useTranslation();
  const [form, setForm] = useState({ lat: "", lon: "", alt_m: "0" });
  const [msg, setMsg] = useState<string>();

  useEffect(() => {
    api.observer().then((o) => {
      if (o) setForm({ lat: String(o.lat), lon: String(o.lon), alt_m: String(o.alt_m) });
    });
  }, []);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await api.setObserver({ lat: Number(form.lat), lon: Number(form.lon), alt_m: Number(form.alt_m) });
      setMsg(t("saved"));
    } catch (err) {
      setMsg(`${t("error")}: ${(err as Error).message}`);
    }
  };

  const field = (key: keyof typeof form, label: string) => (
    <label>
      {label}
      <input type="number" step="any" required value={form[key]} onChange={(e) => setForm({ ...form, [key]: e.target.value })} />
    </label>
  );

  return (
    <form className="row" onSubmit={save}>
      <h2 style={{ width: "100%", fontSize: "1rem" }}>{t("observer")}</h2>
      {field("lat", t("lat"))}
      {field("lon", t("lon"))}
      {field("alt_m", t("alt"))}
      <button type="submit">{t("save")}</button>
      {msg && <span>{msg}</span>}
    </form>
  );
}

export default function App() {
  const { t, i18n } = useTranslation();
  const tabs: Tab[] = ["dashboard", "passes", "recordings", "gallery", "storage", "events", "settings"];
  // az aktív fül az URL-ben (#passes/schedule): mélylink, és a frissítés után is ugyanott marad
  const fromHash = (): { tab: Tab; sub: Sub } => {
    const [main, sub] = location.hash.slice(1).split("/");
    const legacy = LEGACY[main];
    if (legacy) return { tab: legacy[0], sub: legacy[1] ?? "plan" };
    return { tab: tabs.includes(main as Tab) ? (main as Tab) : "dashboard", sub: SUBS.includes(sub as Sub) ? (sub as Sub) : "plan" };
  };
  const [loc, setLoc] = useState(fromHash);
  const go = (tab: Tab, sub: Sub = "plan") => {
    setLoc({ tab, sub });
    history.replaceState(null, "", tab === "passes" && sub !== "plan" ? `#passes/${sub}` : `#${tab}`);
  };
  useEffect(() => {
    const onHash = () => setLoc(fromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const { tab, sub } = loc;
  const { state: live } = useStream();
  const errors = live?.unacknowledged.error ?? 0;
  const problems = errors + (live?.unacknowledged.warning ?? 0);
  return (
    <>
      <header>
        <h1>satrx</h1>
        {tabs.map((id) => (
          <button key={id} className={`tab ${tab === id ? "active" : ""}`} onClick={() => go(id)}>
            {t(id)}
            {id === "events" && problems > 0 && (
              <span className={`badge ${errors > 0 ? "badge-error" : "badge-warning"}`}>{problems}</span>
            )}
          </button>
        ))}
        <span className="spacer" />
        <select
          value={i18n.language}
          onChange={(e) => { localStorage.setItem("lang", e.target.value); i18n.changeLanguage(e.target.value); }}
        >
          <option value="hu">HU</option>
          <option value="en">EN</option>
        </select>
      </header>
      <StatusBar onOpenEvents={() => go("events")} />
      <main>
        {tab === "dashboard" && <DashboardPage />}
        {tab === "passes" && <PassesPage sub={sub} onSub={(x) => go("passes", x)} />}
        {tab === "recordings" && <RecordingsPage />}
        {tab === "gallery" && <GalleryPage />}
        {tab === "storage" && <StoragePage />}
        {tab === "events" && <EventsPage />}
        {tab === "settings" && <><SettingsPage /><ToolsSection /></>}
      </main>
      <Toasts onOpenEvents={() => go("events")} />
    </>
  );
}
