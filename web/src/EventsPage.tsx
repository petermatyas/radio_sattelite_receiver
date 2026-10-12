import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, AppEvent, Job, JobDetail } from "./api";
import { eventText, fmt, useAsync } from "./hooks";

function JobPanel({ id, onClose, onChanged }: { id: number; onClose: () => void; onChanged: () => void }) {
  const { t } = useTranslation();
  const job = useAsync<JobDetail>(() => api.job(id));
  const [error, setError] = useState<string>();
  useEffect(() => {
    const timer = setInterval(job.reload, 2000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const j = job.data;
  if (!j) return <p>{job.error ?? "…"}</p>;
  const retry = async () => {
    try { await api.retryJob(j.id); onChanged(); onClose(); } catch (e) { setError((e as Error).message); }
  };
  return (
    <div className="panel">
      <div className="row">
        <h3>#{j.id} – {t(`kind_${j.kind}`)} – <span className={`st-${j.status}`}>{t(`jst_${j.status}`, { defaultValue: j.status })}</span></h3>
        {j.kind === "processing" && <button onClick={retry}>{t("retry")}</button>}
        <button onClick={onClose}>{t("close")}</button>
      </div>
      {(error || j.error) && <p className="err">{error ?? j.error}</p>}
      <ol className="steps">
        {j.steps.map((s) => (
          <li key={s.idx} className={`step step-${s.status}`}>
            <strong>{s.processor}</strong> – {t(`sst_${s.status}`, { defaultValue: s.status })}
            {s.started_at && s.finished_at && (
              <span className="muted"> ({((+new Date(s.finished_at) - +new Date(s.started_at)) / 1000).toFixed(1)} s)</span>
            )}
            {s.error && <div className="err">{s.error}</div>}
            {s.log && <pre>{s.log}</pre>}
          </li>
        ))}
      </ol>
      {j.artifacts.length > 0 && <p>{t("artifacts")}: {j.artifacts.map((a) => `${t(`akind_${a.kind}`, { defaultValue: a.kind })} #${a.id}`).join(", ")}</p>}
    </div>
  );
}

export default function EventsPage() {
  const { t } = useTranslation();
  const [level, setLevel] = useState("");
  const [unacked, setUnacked] = useState(false);
  const [open, setOpen] = useState<number>();
  const events = useAsync<AppEvent[]>(() => api.events({ level: level || undefined, unacked, limit: 200 }));
  const jobs = useAsync<Job[]>(() => api.jobs(40));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(events.reload, [level, unacked]);
  useEffect(() => {
    const id = setInterval(() => { events.reload(); jobs.reload(); }, 5000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [level, unacked]);

  return (
    <>
      <section>
        <h2>{t("events")}</h2>
        <div className="row">
          <label>
            {t("level")}
            <select value={level} onChange={(e) => setLevel(e.target.value)}>
              <option value="">{t("all")}</option>
              <option value="error">{t("lvl_error")}</option>
              <option value="warning">{t("lvl_warning")}</option>
              <option value="info">{t("lvl_info")}</option>
            </select>
          </label>
          <label>
            {t("onlyUnacked")}
            <input type="checkbox" checked={unacked} onChange={(e) => setUnacked(e.target.checked)} />
          </label>
          <button onClick={() => api.ackAll().then(events.reload)}>{t("ackAll")}</button>
        </div>
        {events.error && <p className="err">{events.error}</p>}
        <table>
          <thead><tr><th>{t("time")}</th><th>{t("level")}</th><th>{t("source")}</th><th>{t("message")}</th><th /></tr></thead>
          <tbody>
            {events.data?.map((e) => (
              <tr key={e.id} className={e.acknowledged ? "acked" : ""}>
                <td>{fmt(e.ts)}</td>
                <td className={`lvl-${e.level}`}>{t(`lvl_${e.level}`)}</td>
                <td>{e.source}</td>
                <td>
                  {eventText(t, e)}
                  {e.job_id && <> <a href="#" onClick={(ev) => { ev.preventDefault(); setOpen(e.job_id!); }}>{t("job")} #{e.job_id}</a></>}
                </td>
                <td>{!e.acknowledged && <button onClick={() => api.ackEvent(e.id).then(events.reload)}>{t("ack")}</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {events.data?.length === 0 && <p className="muted">{t("noEvents")}</p>}
      </section>

      <section>
        <h2>{t("jobs")}</h2>
        {open !== undefined && <JobPanel key={open} id={open} onClose={() => setOpen(undefined)} onChanged={jobs.reload} />}
        <table>
          <thead><tr><th>#</th><th>{t("kind")}</th><th>{t("satellite")}</th><th>{t("pipeline")}</th><th>{t("status")}</th><th>{t("started")}</th></tr></thead>
          <tbody>
            {jobs.data?.map((j) => (
              <tr key={j.id} className="clickable" onClick={() => setOpen(j.id)}>
                <td>{j.id}</td><td>{t(`kind_${j.kind}`)}</td><td>{j.satellite_name ?? "-"}</td><td>{j.pipeline_name ?? "-"}</td>
                <td className={`st-${j.status}`}>{t(`jst_${j.status}`, { defaultValue: j.status })}</td>
                <td>{j.started_at ? fmt(j.started_at) : "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}
