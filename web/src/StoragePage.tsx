import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, authUrl, DeletePlan, ExportJob, MoveStatus, RetentionResult, StorageItem, StorageList, StorageOverview, ValidationResult } from "./api";
import { fmt, fmtBytes, useAsync } from "./hooks";
import { useStream } from "./stream";

// ---- használat és kvóta ---------------------------------------------------------------------------------

function UsageSection({ info, onChanged }: { info: StorageOverview; onChanged: () => void }) {
  const { t } = useTranslation();
  const [plan, setPlan] = useState<RetentionResult>();
  const [error, setError] = useState<string>();
  const pct = Math.min(100, (info.quota_used_fraction ?? 0) * 100);
  const level = pct >= 100 ? "err" : pct >= 80 ? "warn" : "ok";

  const preview = async () => { try { setPlan(await api.retention(true)); setError(undefined); } catch (e) { setError((e as Error).message); } };
  const run = async () => {
    if (!window.confirm(t("confirmRetention"))) return;
    try { await api.retention(false); setPlan(undefined); onChanged(); } catch (e) { setError((e as Error).message); }
  };

  return (
    <section>
      <h2>{t("usage")}</h2>
      <div className={`quota quota-${level}`}>
        <div className="quota-bar"><span style={{ width: `${pct}%` }} /></div>
        <div>
          {fmtBytes(info.total_bytes)} / {fmtBytes(info.quota_bytes)} ({pct.toFixed(0)}%) – {t("recordingsShort")}: {fmtBytes(info.recordings_bytes)} ({info.recordings_count}),
          {" "}{t("resultsShort")}: {fmtBytes(info.artifacts_bytes)} ({info.artifacts_count})
        </div>
        <div className="muted">
          {t("growth7d")}: {fmtBytes(info.growth_7d_bytes)}
          {info.days_until_disk_full != null && info.days_until_disk_full < 3650 && <> · {t("daysUntilFull", { n: Math.round(info.days_until_disk_full) })}</>}
          {" · "}{t("retentionInfo", { days: info.retention.iq_retention_days, adays: info.retention.artifact_retention_days, quota: info.retention.quota_gb })}
        </div>
      </div>
      <div className="row">
        <button onClick={preview}>{t("retentionPreview")}</button>
        {plan && <button onClick={run} disabled={plan.recording_ids.length === 0}>{t("retentionRun")}</button>}
      </div>
      {error && <p className="err">{error}</p>}
      {plan && (
        <p className="muted">
          {plan.recording_ids.length === 0 ? t("retentionNothing")
            : t("retentionPlan", { n: plan.recording_ids.length, size: fmtBytes(plan.freed_bytes), aged: plan.aged_out, quota: plan.over_quota, unproc: plan.unprocessed_deleted })}
          {plan.artifacts_deleted > 0 && <> {t("retentionArtifacts", { n: plan.artifacts_deleted })}</>}
          {plan.notes.includes("quota_still_exceeded") && <span className="hint"> {t("quotaStillExceeded")}</span>}
        </p>
      )}
    </section>
  );
}

// ---- a mentés helye ---------------------------------------------------------------------------------------

function CheckResult({ r }: { r: ValidationResult }) {
  const { t } = useTranslation();
  return (
    <ul className="checks">
      {r.errors.map((c) => <li key={c} className="err">✖ {t(`pv_${c}`, { defaultValue: c })}</li>)}
      {r.warnings.map((c) => <li key={c} className="hint">⚠ {t(`pv_${c}`, { defaultValue: c })}</li>)}
      {r.ok && r.errors.length === 0 && <li className="st-done">✔ {t("pathOk")}{r.free_bytes != null && ` – ${t("freeSpace")}: ${fmtBytes(r.free_bytes)}`}</li>}
    </ul>
  );
}

function LocationSection({ info, onChanged }: { info: StorageOverview; onChanged: () => void }) {
  const { t } = useTranslation();
  const activeOf = (kind: string) => info.roots.find((r) => r.kind === kind && r.active);
  const [paths, setPaths] = useState({ recordings: "", artifacts: "" });
  const [checks, setChecks] = useState<Record<string, ValidationResult>>({});
  const [error, setError] = useState<string>();
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    setPaths({ recordings: activeOf("recordings")?.path ?? "", artifacts: activeOf("artifacts")?.path ?? "" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [info.roots.map((r) => `${r.id}${r.active}`).join()]);

  const check = async (kind: "recordings" | "artifacts") => {
    try { setChecks({ ...checks, [kind]: await api.validatePath(paths[kind]) }); setError(undefined); } catch (e) { setError((e as Error).message); }
  };
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    const body: { recordings_path?: string; artifacts_path?: string } = {};
    if (paths.recordings && paths.recordings !== activeOf("recordings")?.path) body.recordings_path = paths.recordings;
    if (paths.artifacts && paths.artifacts !== activeOf("artifacts")?.path) body.artifacts_path = paths.artifacts;
    if (!body.recordings_path && !body.artifacts_path) return;
    try {
      const out = await api.setRoots(body);
      setChecks(out.checks ?? {}); setSaved(true); setError(undefined); onChanged();
    } catch (err) { setError((err as Error).message); setSaved(false); }
  };

  const row = (kind: "recordings" | "artifacts", label: string) => (
    <div key={kind} className="loc">
      <label style={{ flex: 1, minWidth: "22rem" }}>
        {label}
        <input value={paths[kind]} onChange={(e) => { setPaths({ ...paths, [kind]: e.target.value }); setSaved(false); }} placeholder="/mnt/ssd/satrx/recordings" />
      </label>
      <button type="button" onClick={() => check(kind)}>{t("checkPath")}</button>
      {checks[kind] && <CheckResult r={checks[kind]} />}
    </div>
  );

  return (
    <section>
      <h2>{t("saveLocation")}</h2>
      <p className="muted">{t("saveLocationHelp")}</p>
      <form onSubmit={save}>
        {row("recordings", t("recordingsFolder"))}
        {row("artifacts", t("resultsFolder"))}
        <div className="row">
          <button type="submit">{t("save")}</button>
          {saved && <span className="st-done">{t("saved")}</span>}
        </div>
      </form>
      {error && <p className="err">{t("error")}: {error}</p>}
      <table>
        <thead><tr><th>{t("kind")}</th><th>{t("path")}</th><th>{t("used")}</th><th>{t("freeSpace")}</th><th>{t("status")}</th></tr></thead>
        <tbody>
          {info.roots.map((r) => (
            <tr key={r.id}>
              <td>{t(`root_${r.kind}`)}{r.active ? ` ★` : ""}</td><td><code>{r.path}</code></td>
              <td>{fmtBytes(r.used_bytes)}</td><td>{r.free_bytes != null ? fmtBytes(r.free_bytes) : "-"}</td>
              <td className={r.available ? "" : "err"}>{r.available ? t("available") : t("unavailable")}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <MoveSection info={info} onChanged={onChanged} />
    </section>
  );
}

function MoveSection({ info, onChanged }: { info: StorageOverview; onChanged: () => void }) {
  const { t } = useTranslation();
  const [st, setSt] = useState<MoveStatus>(info.move);
  const [error, setError] = useState<string>();
  const running = st.state === "running";
  useEffect(() => { setSt(info.move); }, [info.move]);
  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => api.moveStatus().then((s) => { setSt(s); if (s.state !== "running") onChanged(); }), 1000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running]);

  const outside = (kind: string) => info.roots.filter((r) => r.kind === kind && !r.active && r.used_bytes > 0);
  const start = async (kind: "recordings" | "artifacts") => {
    try { setSt(await api.startMove(kind)); setError(undefined); } catch (e) { setError((e as Error).message); }
  };
  const pct = st.total_bytes > 0 ? (st.done_bytes / st.total_bytes) * 100 : 0;
  return (
    <div>
      <h3>{t("moveExisting")}</h3>
      {(["recordings", "artifacts"] as const).map((kind) => outside(kind).length > 0 && (
        <p key={kind}>
          {t("moveHint", { kind: t(`root_${kind}`), size: fmtBytes(outside(kind).reduce((a, r) => a + r.used_bytes, 0)) })}{" "}
          <button disabled={running} onClick={() => start(kind)}>{t("moveStart")}</button>
        </p>
      ))}
      {outside("recordings").length === 0 && outside("artifacts").length === 0 && st.state === "idle" && <p className="muted">{t("nothingToMove")}</p>}
      {st.state !== "idle" && (
        <div className="panel">
          <strong>{t(`mv_${st.state}`)}</strong> {st.kind && `– ${t(`root_${st.kind}`)}`}
          <div className="progress wide"><span style={{ width: `${pct}%` }} /></div>
          <div className="muted">{st.done_items}/{st.total_items} {t("items")} · {fmtBytes(st.done_bytes)} / {fmtBytes(st.total_bytes)}{st.current ? ` · ${st.current}` : ""}</div>
          {running && <button onClick={() => api.cancelMove()}>{t("cancel")}</button>}
          {st.error && <p className="err">{st.error}</p>}
          {st.errors.length > 0 && <p className="err">{t("moveErrors", { n: st.errors.length })}: {st.errors.map((e) => `${e.type} #${e.id}: ${e.error}`).join("; ")}</p>}
          {st.skipped.length > 0 && <p className="hint">{t("moveSkipped", { n: st.skipped.length })}</p>}
        </div>
      )}
      {error && <p className="err">{error}</p>}
    </div>
  );
}

// ---- törlés megerősítése ---------------------------------------------------------------------------------------

function ConfirmDelete({ ids, scope, onClose, onDone }: { ids: number[]; scope: "iq_only" | "all"; onClose: () => void; onDone: () => void }) {
  const { t } = useTranslation();
  const [plan, setPlan] = useState<DeletePlan>();
  const [error, setError] = useState<string>();
  useEffect(() => {
    api.deleteData({ recording_ids: ids, scope, dry_run: true }).then(setPlan).catch((e) => setError(e.message));
  }, [ids, scope]);
  const skipped = plan?.items.filter((i) => i.skipped) ?? [];
  const run = async () => {
    try { await api.deleteData({ recording_ids: ids, scope }); onDone(); } catch (e) { setError((e as Error).message); }
  };
  return (
    <div className="modal" role="dialog">
      <div className="modal-card">
        <h3>{scope === "iq_only" ? t("deleteIqTitle") : t("deleteAllTitle")}</h3>
        <p>{scope === "iq_only" ? t("deleteIqHelp") : t("deleteAllHelp")}</p>
        {!plan && !error && <p className="muted">…</p>}
        {plan && (
          <p>
            <strong>{t("willDelete", { n: plan.recordings })}</strong> – {t("willFree", { size: fmtBytes(plan.freed_bytes) })}
            {skipped.length > 0 && <span className="hint"> {t("willSkip", { n: skipped.length })}: {[...new Set(skipped.map((i) => t(`skip_${i.skipped}`)))].join(", ")}</span>}
          </p>
        )}
        {error && <p className="err">{error}</p>}
        <div className="row">
          <button className="danger" disabled={!plan || plan.recordings === 0} onClick={run}>{t("delete")}</button>
          <button onClick={onClose}>{t("cancel")}</button>
        </div>
      </div>
    </div>
  );
}

// ---- exportok ----------------------------------------------------------------------------------------------------

function ExportPanel({ ids, onClose }: { ids: number[]; onClose: () => void }) {
  const { t } = useTranslation();
  const [mode, setMode] = useState<"zip" | "directory">("zip");
  const [directory, setDirectory] = useState("");
  const [includeIq, setIncludeIq] = useState(true);
  const [move, setMove] = useState(false);
  const [job, setJob] = useState<ExportJob>();
  const [error, setError] = useState<string>();
  const active = job && (job.state === "queued" || job.state === "running");
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => api.exportStatus(job!.id).then(setJob), 800);
    return () => clearInterval(id);
  }, [active, job]);

  const start = async () => {
    try {
      setJob(await api.createExport({ recording_ids: ids, mode, directory: mode === "directory" ? directory : null, include_iq: includeIq, move: mode === "directory" && move }));
      setError(undefined);
    } catch (e) { setError((e as Error).message); }
  };
  const pct = job && job.total_bytes > 0 ? (job.done_bytes / job.total_bytes) * 100 : 0;
  return (
    <div className="modal" role="dialog">
      <div className="modal-card">
        <h3>{t("exportTitle", { n: ids.length })}</h3>
        <div className="row">
          <label><input type="radio" checked={mode === "zip"} onChange={() => setMode("zip")} /> {t("exportZip")}</label>
          <label><input type="radio" checked={mode === "directory"} onChange={() => setMode("directory")} /> {t("exportDirectory")}</label>
        </div>
        {mode === "directory" && (
          <div>
            <label>{t("targetFolder")}<input value={directory} onChange={(e) => setDirectory(e.target.value)} placeholder="/mnt/usb/satrx_export" style={{ width: "100%" }} /></label>
            <label><input type="checkbox" checked={move} disabled={!includeIq} onChange={(e) => setMove(e.target.checked)} /> {t("exportMove")}</label>
            {move && <p className="hint">{t("exportMoveHelp")}</p>}
          </div>
        )}
        <label><input type="checkbox" checked={includeIq} onChange={(e) => { setIncludeIq(e.target.checked); if (!e.target.checked) setMove(false); }} /> {t("includeIq")}</label>
        {job && (
          <div className="panel">
            <strong>{t(`ex_${job.state}`)}</strong>
            <div className="progress wide"><span style={{ width: `${job.state === "done" ? 100 : pct}%` }} /></div>
            <div className="muted">{fmtBytes(job.done_bytes)} / {fmtBytes(job.total_bytes)} · {job.files} {t("files")}{job.current ? ` · ${job.current}` : ""}</div>
            {job.state === "done" && job.mode === "zip" && <a className="button" href={authUrl(`/api/exports/${job.id}/download`)}>⬇ {t("downloadZip")}</a>}
            {job.state === "done" && job.mode === "directory" && <p className="st-done">{t("copiedTo", { path: job.result })}{job.moved ? ` · ${t("movedCount", { n: job.moved.recordings })}` : ""}</p>}
            {job.error && <p className="err">{job.error}</p>}
            {active && <button onClick={() => api.cancelExport(job.id)}>{t("cancel")}</button>}
          </div>
        )}
        {error && <p className="err">{error}</p>}
        <div className="row">
          <button onClick={start} disabled={!!active || (mode === "directory" && !directory)}>{t("startExport")}</button>
          <button onClick={onClose}>{t("close")}</button>
        </div>
      </div>
    </div>
  );
}

// ---- felvételek listája -----------------------------------------------------------------------------------------

export default function StoragePage() {
  const { t } = useTranslation();
  const info = useAsync<StorageOverview>(() => api.storage());
  const [filters, setFilters] = useState({ state: "all", sort: "date", order: "desc" });
  const list = useAsync<StorageList>(() => api.storageRecordings(filters));
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [confirm, setConfirm] = useState<"iq_only" | "all">();
  const [exporting, setExporting] = useState(false);
  const { tick } = useStream();
  const reload = () => { info.reload(); list.reload(); };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(list.reload, [filters.state, filters.sort, filters.order]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(reload, [tick]);

  const items = list.data?.items ?? [];
  const allSelected = items.length > 0 && items.every((i) => selected.has(i.id));
  const ids = useMemo(() => [...selected], [selected]);
  const selectedItems = items.filter((i) => selected.has(i.id));
  const toggle = (id: number) => setSelected((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });

  const setProtected = async (flag: boolean) => {
    await Promise.all(ids.map((id) => api.protectRecording(id, flag)));
    reload();
  };

  return (
    <>
      {info.error && <p className="err">{info.error}</p>}
      {info.data && <UsageSection info={info.data} onChanged={reload} />}
      {info.data && <LocationSection info={info.data} onChanged={reload} />}

      <section>
        <h2>{t("storedData")}</h2>
        <div className="row">
          <label>{t("filter")}
            <select value={filters.state} onChange={(e) => setFilters({ ...filters, state: e.target.value })}>
              {["all", "with_iq", "without_iq", "protected", "unprocessed"].map((s) => <option key={s} value={s}>{t(`fs_${s}`)}</option>)}
            </select>
          </label>
          <label>{t("sortBy")}
            <select value={filters.sort} onChange={(e) => setFilters({ ...filters, sort: e.target.value })}>
              {["date", "size", "iq", "results"].map((s) => <option key={s} value={s}>{t(`sort_${s}`)}</option>)}
            </select>
          </label>
          <label>{t("order")}
            <select value={filters.order} onChange={(e) => setFilters({ ...filters, order: e.target.value })}>
              <option value="desc">↓</option><option value="asc">↑</option>
            </select>
          </label>
          {list.data && <span className="muted">{t("listTotals", { n: list.data.total, size: fmtBytes(list.data.total_bytes) })}</span>}
        </div>

        {selected.size > 0 && (
          <div className="bulkbar">
            <strong>{t("selectedCount", { n: selected.size })}</strong> ({fmtBytes(selectedItems.reduce((a, i) => a + i.total_bytes, 0))})
            <button onClick={() => setConfirm("iq_only")}>{t("deleteIq")}</button>
            <button onClick={() => setConfirm("all")}>{t("deleteEverything")}</button>
            <button onClick={() => setExporting(true)}>{t("exportAction")}</button>
            <button onClick={() => setProtected(true)}>🔒 {t("protect")}</button>
            <button onClick={() => setProtected(false)}>{t("unprotect")}</button>
            <button onClick={() => setSelected(new Set())}>{t("clearSelection")}</button>
          </div>
        )}

        <table>
          <thead>
            <tr>
              <th><input type="checkbox" checked={allSelected} onChange={() => setSelected(allSelected ? new Set() : new Set(items.map((i) => i.id)))} /></th>
              <th>{t("started")}</th><th>{t("satellite")}</th><th>{t("rawIq")}</th><th>{t("results")}</th><th>{t("status")}</th><th />
            </tr>
          </thead>
          <tbody>
            {items.map((i: StorageItem) => (
              <tr key={i.id} className={selected.has(i.id) ? "selected" : ""}>
                <td><input type="checkbox" checked={selected.has(i.id)} onChange={() => toggle(i.id)} /></td>
                <td>{fmt(i.started_at)}</td>
                <td>{i.satellite_name ?? t("manual")} <span className="muted">{(i.center_freq_hz / 1e6).toFixed(3)} MHz</span></td>
                <td>{i.iq_available ? fmtBytes(i.iq_bytes) : <span className="muted">{t("iqDeleted")}</span>}</td>
                <td>{i.artifact_count > 0 ? `${fmtBytes(i.artifact_bytes)} (${i.artifact_count})` : "-"}</td>
                <td>
                  {(i.source === "mock" || i.source === "replay") && <span className="badge badge-sim" title={t("simulatedHint")}>{t("simulatedShort")}</span>}
                  {i.protected && <span className="badge badge-warning">🔒 {t("protectedShort")}</span>}
                  {!i.processed && i.iq_available && <span className="badge badge-neutral">{t("unprocessed")}</span>}
                  {!i.storage_available && <span className="badge badge-error">{t("unavailable")}</span>}
                </td>
                <td className="actions">
                  {i.iq_available && i.storage_available && <a href={authUrl(`/api/recordings/${i.id}/download?part=iq`)} title={t("downloadIq")}>⬇ IQ</a>}
                  {i.storage_available && <a href={authUrl(`/api/recordings/${i.id}/download?part=meta`)}>meta</a>}
                  {i.storage_available && i.has_orbit && <a href={authUrl(`/api/recordings/${i.id}/download?part=orbit`)}>orbit</a>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {list.data && items.length === 0 && <p className="muted">{t("noRecordings")}</p>}
      </section>

      {confirm && <ConfirmDelete ids={ids} scope={confirm} onClose={() => setConfirm(undefined)}
        onDone={() => { setConfirm(undefined); setSelected(new Set()); reload(); }} />}
      {exporting && <ExportPanel ids={ids} onClose={() => setExporting(false)} />}
    </>
  );
}
