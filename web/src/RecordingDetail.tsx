import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, Artifact, artifactUrl, authUrl, RecordingDetailData, WaterfallMeta } from "./api";
import { fmt, mb, useAsync } from "./hooks";

interface Selection { t0: number; t1: number; f0: number; f1: number }

const MODES = ["nfm", "wfm", "am", "usb", "lsb"] as const;
const COLORMAPS = ["viridis", "heat", "jet", "gray"] as const;
const FFT_SIZES = [256, 512, 1024, 2048, 4096, 8192];

function WaterfallViewer({ art, onSelect }: { art: Artifact; onSelect: (s: Selection | undefined) => void }) {
  const { t } = useTranslation();
  const m = art.meta as WaterfallMeta;
  const imgRef = useRef<HTMLImageElement>(null);
  const [drag, setDrag] = useState<{ x0: number; y0: number; x1: number; y1: number }>();
  const [sel, setSel] = useState<Selection>();

  // a megjelenített kép pixeleiből a kép saját (natív) pixelei, majd idő/frekvencia
  const toNative = (e: React.MouseEvent) => {
    const img = imgRef.current!;
    const r = img.getBoundingClientRect();
    return { x: (e.clientX - r.left) * (img.naturalWidth / r.width), y: (e.clientY - r.top) * (img.naturalHeight / r.height) };
  };
  const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
  const toSel = (a: { x: number; y: number }, b: { x: number; y: number }): Selection => {
    const p = m.plot;
    const fx = (x: number) => m.f_lo_hz + (clamp(x, p.x, p.x + p.w) - p.x) / p.w * (m.f_hi_hz - m.f_lo_hz);
    const ty = (y: number) => m.t0_s + (clamp(y, p.y, p.y + p.h) - p.y) / p.h * (m.t1_s - m.t0_s);
    return { f0: Math.min(fx(a.x), fx(b.x)), f1: Math.max(fx(a.x), fx(b.x)), t0: Math.min(ty(a.y), ty(b.y)), t1: Math.max(ty(a.y), ty(b.y)) };
  };

  const pct = (x: number, y: number) => ({ left: `${x / m.width * 100}%`, top: `${y / m.height * 100}%` });
  const box = drag && {
    ...pct(Math.min(drag.x0, drag.x1), Math.min(drag.y0, drag.y1)),
    width: `${Math.abs(drag.x1 - drag.x0) / m.width * 100}%`, height: `${Math.abs(drag.y1 - drag.y0) / m.height * 100}%`,
  };

  return (
    <div className="viewer">
      <p className="muted">{t("selectHint")}</p>
      <div
        className="viewer-frame"
        onMouseDown={(e) => { const n = toNative(e); setDrag({ x0: n.x, y0: n.y, x1: n.x, y1: n.y }); setSel(undefined); onSelect(undefined); }}
        onMouseMove={(e) => { if (drag) { const n = toNative(e); setDrag({ ...drag, x1: n.x, y1: n.y }); } }}
        onMouseUp={() => {
          if (drag && Math.abs(drag.x1 - drag.x0) > 4 && Math.abs(drag.y1 - drag.y0) > 4) {
            const s = toSel({ x: drag.x0, y: drag.y0 }, { x: drag.x1, y: drag.y1 });
            setSel(s); onSelect(s);
          } else { setDrag(undefined); }
        }}
      >
        <img ref={imgRef} src={artifactUrl(art.id)} alt="waterfall" draggable={false} />
        {box && <div className="selection" style={box} />}
      </div>
      {sel && (
        <p className="muted">
          {t("selection")}: {sel.t0.toFixed(1)}–{sel.t1.toFixed(1)} s, {(sel.f0 / 1000).toFixed(1)}–{(sel.f1 / 1000).toFixed(1)} kHz
        </p>
      )}
    </div>
  );
}

export default function RecordingDetail({ id, onBack }: { id: number; onBack: () => void }) {
  const { t } = useTranslation();
  const detail = useAsync<RecordingDetailData>(() => api.recordingDetail(id));
  const [error, setError] = useState<string>();
  const [sel, setSel] = useState<Selection>();
  const [viewId, setViewId] = useState<number>();
  const [wf, setWf] = useState({ fft: "1024", cmap: "viridis", range: "45", start: "", dur: "", off: "", span: "", overlay: true });
  const [au, setAu] = useState({ mode: "nfm", off: "0", bw: "", follow: true, start: "", dur: "" });

  const busy = detail.data?.jobs.some((j) => j.status === "queued" || j.status === "running") ?? false;
  useEffect(() => {
    const timer = setInterval(detail.reload, busy ? 1000 : 5000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [busy]);

  const d = detail.data;
  const waterfalls = d?.artifacts.filter((a) => a.kind === "waterfall") ?? [];
  const viewer = waterfalls.find((a) => a.id === viewId) ?? waterfalls[0];
  const num = (v: string) => (v === "" ? undefined : Number(v));
  const lastJob = d?.jobs[0];

  const run = async (processor: string, params: Record<string, unknown>) => {
    try { await api.runSteps(id, [{ processor, params }]); setError(undefined); } catch (e) { setError((e as Error).message); }
    detail.reload();
  };
  const runWaterfall = () => run("waterfall", {
    fft_size: Number(wf.fft), colormap: wf.cmap, dynamic_range_db: Number(wf.range), overlay_doppler: wf.overlay,
    start_s: num(wf.start) ?? 0, duration_s: num(wf.dur) ?? null,
    center_offset_hz: (num(wf.off) ?? 0) * 1000, span_hz: wf.span === "" ? null : Number(wf.span) * 1000,
  });
  const runAudio = () => run("audio_demod", {
    mode: au.mode, offset_hz: (num(au.off) ?? 0) * 1000, follow_doppler: au.follow,
    bandwidth_hz: au.bw === "" ? null : Number(au.bw) * 1000, start_s: num(au.start) ?? 0, duration_s: num(au.dur) ?? null,
  });

  const fromSelection = (target: "audio" | "waterfall") => {
    if (!sel) return;
    const round = (v: number, n = 2) => String(Math.round(v * 10 ** n) / 10 ** n);
    if (target === "audio") {
      setAu({ ...au, off: round((sel.f0 + sel.f1) / 2000, 3), bw: round(Math.max(0.5, (sel.f1 - sel.f0) / 1000), 2),
        start: round(sel.t0), dur: round(sel.t1 - sel.t0), follow: false });
    } else {
      setWf({ ...wf, start: round(sel.t0), dur: round(sel.t1 - sel.t0), off: round((sel.f0 + sel.f1) / 2000, 3),
        span: round((sel.f1 - sel.f0) / 1000, 2) });
    }
  };

  if (!d) return <p>{detail.error ?? "…"}</p>;
  const orbit = d.orbit;
  const rec = d.recording;

  return (
    <>
      <div className="row">
        <button onClick={onBack}>← {t("back")}</button>
        <h2 style={{ margin: 0 }}>#{rec.id} {rec.satellite_name ?? t("manual")}</h2>
        {busy && <span className="st-running">{t("processingNow")}</span>}
      </div>

      <div className="cards">
        <div className="card">
          <h3>{t("kind_recording")}</h3>
          <p>{fmt(rec.started_at)}{(rec.source === "mock" || rec.source === "replay") && <span className="badge badge-sim" title={t("simulatedHint")}>{t("simulatedShort")}</span>}</p>
          {(rec.source === "mock" || rec.source === "replay") && <p className="hint">{t("simulatedHint")}</p>}
          <p className="muted">
            {(rec.center_freq_hz / 1e6).toFixed(4)} MHz · {rec.sample_rate_hz / 1e3} kS/s · {rec.sample_format} · {mb(rec.size_bytes)}
            {d.info && <> · {d.info.duration_s.toFixed(1)} s</>}
          </p>
          {d.info && d.info.retunes.length > 0 && <p className="muted">{t("retunes", { n: d.info.retunes.length })}</p>}
          {!d.iq_available && <p className="hint">{d.storage_available ? t("iqGone") : t("storageUnavailableHint")}</p>}
          <p className="actions">
            {d.iq_available && <a href={authUrl(`/api/recordings/${id}/download?part=iq`)}>⬇ {t("rawIq")}</a>}
            {d.storage_available && <a href={authUrl(`/api/recordings/${id}/download?part=meta`)}>⬇ meta</a>}
            {d.storage_available && d.orbit && <a href={authUrl(`/api/recordings/${id}/download?part=orbit`)}>⬇ orbit</a>}
            <label className="inline"><input type="checkbox" checked={rec.protected} onChange={(e) => api.protectRecording(id, e.target.checked).then(detail.reload)} /> 🔒 {t("protect")}</label>
          </p>
        </div>
        {orbit && (
          <div className="card">
            <h3>{t("orbitData")}</h3>
            {orbit.satellite && <p>{orbit.satellite.name} ({orbit.satellite.norad_id})</p>}
            {orbit.pass && <p className="muted">{t("maxEl")}: {orbit.pass.max_elevation_deg?.toFixed(1)}° · {fmt(orbit.pass.aos)} – {new Date(orbit.pass.los).toLocaleTimeString()}</p>}
            {orbit.satellite?.tle?.epoch && <p className="muted">TLE: {fmt(orbit.satellite.tle.epoch)}</p>}
            <p className="muted">{orbit.has_track ? t("trackPoints", { n: orbit.track_points }) : t("noTrack")}</p>
          </div>
        )}
      </div>

      {(error || lastJob?.status === "failed") && (
        <p className="err">{t("error")}: {error ?? lastJob?.error}</p>
      )}

      {d.iq_available && (
        <div className="forms">
          <form className="panel" onSubmit={(e) => { e.preventDefault(); runWaterfall(); }}>
            <h3>{t("makeWaterfall")}</h3>
            <div className="row">
              <label>FFT<select value={wf.fft} onChange={(e) => setWf({ ...wf, fft: e.target.value })}>{FFT_SIZES.map((n) => <option key={n}>{n}</option>)}</select></label>
              <label>{t("colormap")}<select value={wf.cmap} onChange={(e) => setWf({ ...wf, cmap: e.target.value })}>{COLORMAPS.map((c) => <option key={c}>{c}</option>)}</select></label>
              <label>{t("dynRange")}<input type="number" min="10" max="120" value={wf.range} onChange={(e) => setWf({ ...wf, range: e.target.value })} style={{ width: "5rem" }} /></label>
            </div>
            <div className="row">
              <label>{t("startS")}<input type="number" step="any" min="0" value={wf.start} onChange={(e) => setWf({ ...wf, start: e.target.value })} style={{ width: "6rem" }} /></label>
              <label>{t("durS")}<input type="number" step="any" min="0" value={wf.dur} onChange={(e) => setWf({ ...wf, dur: e.target.value })} style={{ width: "6rem" }} /></label>
              <label>{t("centerKhz")}<input type="number" step="any" value={wf.off} onChange={(e) => setWf({ ...wf, off: e.target.value })} style={{ width: "6rem" }} /></label>
              <label>{t("spanKhz")}<input type="number" step="any" min="0" value={wf.span} onChange={(e) => setWf({ ...wf, span: e.target.value })} style={{ width: "6rem" }} /></label>
              <label>{t("overlayDoppler")}<input type="checkbox" checked={wf.overlay} onChange={(e) => setWf({ ...wf, overlay: e.target.checked })} /></label>
            </div>
            <button type="submit" disabled={busy}>{t("generate")}</button>
          </form>

          <form className="panel" onSubmit={(e) => { e.preventDefault(); runAudio(); }}>
            <h3>{t("makeAudio")}</h3>
            <div className="row">
              <label>{t("mode")}<select value={au.mode} onChange={(e) => setAu({ ...au, mode: e.target.value })}>{MODES.map((m) => <option key={m} value={m}>{m.toUpperCase()}</option>)}</select></label>
              <label>{t("offsetKhz")}<input type="number" step="any" value={au.off} onChange={(e) => setAu({ ...au, off: e.target.value })} style={{ width: "6rem" }} /></label>
              <label>{t("bandwidthKhz")}<input type="number" step="any" min="0.5" value={au.bw} onChange={(e) => setAu({ ...au, bw: e.target.value })} style={{ width: "6rem" }} /></label>
              <label>{t("followDoppler")}<input type="checkbox" checked={au.follow} disabled={!orbit?.has_track} onChange={(e) => setAu({ ...au, follow: e.target.checked })} /></label>
            </div>
            <div className="row">
              <label>{t("startS")}<input type="number" step="any" min="0" value={au.start} onChange={(e) => setAu({ ...au, start: e.target.value })} style={{ width: "6rem" }} /></label>
              <label>{t("durS")}<input type="number" step="any" min="0" value={au.dur} onChange={(e) => setAu({ ...au, dur: e.target.value })} style={{ width: "6rem" }} /></label>
            </div>
            <button type="submit" disabled={busy}>{t("generate")}</button>
          </form>
        </div>
      )}

      {viewer && (
        <section>
          <h2>{t("waterfall")}</h2>
          {waterfalls.length > 1 && (
            <div className="row">
              {waterfalls.map((a) => <button key={a.id} className={a.id === viewer.id ? "tab active" : ""} onClick={() => setViewId(a.id)}>#{a.id}</button>)}
            </div>
          )}
          <WaterfallViewer key={viewer.id} art={viewer} onSelect={setSel} />
          {sel && d.iq_available && (
            <div className="row">
              <button onClick={() => { fromSelection("audio"); }}>{t("selToAudio")}</button>
              <button onClick={() => { fromSelection("waterfall"); }}>{t("selToZoom")}</button>
            </div>
          )}
        </section>
      )}

      <section>
        <h2>{t("results")}</h2>
        {d.artifacts.length === 0 && <p className="muted">{t("noResultsYet")}</p>}
        <div className="gallery">
          {d.artifacts.map((a) => (
            <figure key={a.id} className="tile">
              {a.kind === "audio"
                ? <audio controls preload="none" src={artifactUrl(a.id)} />
                : a.kind === "image"
                  ? <a href={artifactUrl(a.id)} target="_blank" rel="noreferrer"><img src={artifactUrl(a.id, true)} alt={`#${a.id}`} /></a>
                  : a.has_thumbnail ? <img src={artifactUrl(a.id, true)} alt={`#${a.id}`} onClick={() => setViewId(a.id)} /> : null}
              <figcaption>
                <span className="muted">{t(`akind_${a.kind}`, { defaultValue: a.kind })} #{a.id} · {fmt(a.created_at)}</span>
                {a.kind === "image" && a.meta.mode === "LRPT" && <p className="muted">LRPT {String(a.meta.product ?? "")} · {String(a.meta.bitrate ?? "")}</p>}
                {a.kind === "image" && a.meta.mode && a.meta.mode !== "LRPT" && <p className="muted">SSTV {String(a.meta.mode)} · {a.meta.lines_decoded}/{a.meta.lines_total}{a.meta.complete === false ? ` · ${t("partialImage")}` : ""}</p>}
                {a.kind === "audio" && <p className="muted">{String(a.meta.mode).toUpperCase()} · {a.meta.duration_s} s · {a.meta.start_s ?? 0} s…</p>}
                <button onClick={() => api.deleteArtifact(a.id).then(detail.reload)}>{t("remove")}</button>{" "}
                <a href={artifactUrl(a.id)} download>{t("download")}</a>
              </figcaption>
            </figure>
          ))}
        </div>
      </section>

      {d.jobs.length > 0 && (
        <section>
          <h2>{t("jobs")}</h2>
          <table>
            <thead><tr><th>#</th><th>{t("steps")}</th><th>{t("status")}</th><th>{t("started")}</th></tr></thead>
            <tbody>
              {d.jobs.map((j) => (
                <tr key={j.id}>
                  <td>{j.id}</td><td>{j.pipeline_name ?? t("adhoc")}</td>
                  <td className={`st-${j.status}`}>{t(`jst_${j.status}`, { defaultValue: j.status })}{j.error ? ` – ${j.error}` : ""}</td>
                  <td>{j.started_at ? fmt(j.started_at) : "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </>
  );
}
