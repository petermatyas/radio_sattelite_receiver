import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, Artifact, artifactUrl, authUrl, ExportJob } from "./api";
import { fmt, fmtBytes, useAsync } from "./hooks";

function MetaLine({ a }: { a: Artifact }) {
  const { t } = useTranslation();
  const m = a.meta;
  const parts: string[] = [];
  if (m.duration_s !== undefined) parts.push(`${m.duration_s} s`);
  if (m.rms_dbfs !== undefined) parts.push(`RMS ${m.rms_dbfs} dBFS`);
  if (m.sample_rate_hz !== undefined) parts.push(`${(m.sample_rate_hz / 1000).toFixed(1)} kHz`);
  if (m.width !== undefined && m.height !== undefined) parts.push(`${m.width}×${m.height}`);
  if (m.doppler_followed) parts.push(t("dopplerFollowed"));
  if (a.kind === "image" && m.mode === "LRPT") {
    parts.unshift(`LRPT ${m.instrument ?? ""} ${m.product ?? ""}`.trim());
    if (m.bitrate) parts.push(String(m.bitrate));
    if (m.product_kind) parts.push(t(`pk_${m.product_kind}`, { defaultValue: String(m.product_kind) }));
  } else if (a.kind === "image" && m.mode) {
    parts.unshift(`SSTV ${m.mode}`);
    if (m.lines_total) parts.push(t("sstvLines", { done: m.lines_decoded, total: m.lines_total }));
  }
  return (
    <p className="muted">
      {parts.join(" · ")}
      {a.kind === "image" && m.complete === false && <span className="badge badge-warning">{t("partialImage")}</span>}
    </p>
  );
}

export default function GalleryPage() {
  const { t } = useTranslation();
  const [kind, setKind] = useState("");
  const [viewing, setViewing] = useState<Artifact>();
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [zip, setZip] = useState<ExportJob>();
  const [msg, setMsg] = useState<string>();
  const toggle = (id: number) => setSelected((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  useEffect(() => {
    if (!zip || (zip.state !== "queued" && zip.state !== "running")) return;
    const timer = setInterval(() => api.exportStatus(zip.id).then(setZip), 800);
    return () => clearInterval(timer);
  }, [zip]);
  const items = useAsync<Artifact[]>(() => api.artifacts(kind || undefined));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(items.reload, [kind]);

  const removeSelected = async () => {
    const ids = [...selected];
    try {
      const plan = await api.deleteData({ artifact_ids: ids, dry_run: true });
      if (!window.confirm(t("confirmDeleteArtifacts", { n: plan.artifacts, size: fmtBytes(plan.freed_bytes) }))) return;
      await api.deleteData({ artifact_ids: ids });
      setSelected(new Set()); setMsg(undefined); items.reload();
    } catch (e) { setMsg((e as Error).message); }
  };
  const zipSelected = async () => {
    try { setZip(await api.createExport({ artifact_ids: [...selected], mode: "zip" })); setMsg(undefined); } catch (e) { setMsg((e as Error).message); }
  };

  return (
    <>
      {selected.size > 0 && (
        <div className="bulkbar">
          <strong>{t("selectedCount", { n: selected.size })}</strong>
          <button onClick={removeSelected}>{t("remove")}</button>
          <button onClick={zipSelected}>ZIP</button>
          <button onClick={() => setSelected(new Set())}>{t("clearSelection")}</button>
          {zip && <span>{t(`ex_${zip.state}`)}{zip.state === "done" && <> <a className="button" href={authUrl(`/api/exports/${zip.id}/download`)}>⬇ {t("downloadZip")}</a></>}{zip.error && <span className="err"> {zip.error}</span>}</span>}
        </div>
      )}
      {msg && <p className="err">{msg}</p>}
      <div className="row">
        <label>
          {t("kindFilter")}
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="">{t("all")}</option>
            {["image", "audio", "waterfall", "text"].map((k) => <option key={k} value={k}>{t(`akind_${k}`)}</option>)}
          </select>
        </label>
      </div>
      {items.error && <p className="err">{items.error}</p>}
      {items.data?.length === 0 && <p className="muted">{t("galleryEmpty")}</p>}
      <div className="gallery">
        {items.data?.map((a) => (
          <figure key={a.id} className={`tile ${selected.has(a.id) ? "selected" : ""}`}>
            <input type="checkbox" className="pick" checked={selected.has(a.id)} onChange={() => toggle(a.id)} aria-label={`#${a.id}`} />
            {a.has_thumbnail
              ? <img src={artifactUrl(a.id, true)} alt={`#${a.id}`} onClick={() => setViewing(a)} />
              : a.kind === "audio"
                ? <audio controls preload="none" src={artifactUrl(a.id)} />
                : <a href={artifactUrl(a.id)} target="_blank" rel="noreferrer">{t("download")}</a>}
            <figcaption>
              <strong>{a.satellite_name ?? t("manual")}</strong> <span className="muted">{t(`akind_${a.kind}`, { defaultValue: a.kind })}</span>
              <br /><span className="muted">{fmt(a.created_at)}</span>
              <MetaLine a={a} />
              <button onClick={() => window.confirm(t("confirmDeleteArtifact")) && api.deleteArtifact(a.id).then(items.reload)}>{t("remove")}</button>{" "}
              <a href={authUrl(`/api/artifacts/${a.id}/file?download=1`)}>⬇</a>
            </figcaption>
          </figure>
        ))}
      </div>
      {viewing && (
        <div className="lightbox" onClick={() => setViewing(undefined)}>
          <img src={artifactUrl(viewing.id)} alt={`#${viewing.id}`} />
        </div>
      )}
    </>
  );
}
