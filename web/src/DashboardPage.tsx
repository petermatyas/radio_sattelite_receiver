import { useEffect } from "react";
import { useTranslation } from "react-i18next";
import { api, Health, HealthComponent, Job } from "./api";
import ConsoleLog from "./ConsoleLog";
import { fmt, useAsync } from "./hooks";
import LiveWaterfall from "./LiveWaterfall";
import { useStream } from "./stream";

function Card({ c, onRescan }: { c: HealthComponent; onRescan?: () => void }) {
  const { t } = useTranslation();
  const devices: { index: number; serial: string; label?: string }[] = c.details?.devices ?? [];
  const next = c.details?.next_pass;
  const hint = t(`hint_${c.code}`, { defaultValue: "" });
  return (
    <div className={`card card-${c.status}`}>
      <h3>{t(`comp_${c.id}`)}</h3>
      <p>
        <span className={`dot dot-${c.status}`} />
        {t(`hc_${c.code}`, { defaultValue: c.message, ...c.details })}
      </p>
      {hint && c.status !== "ok" && <p className="hint">{hint}</p>}
      {c.id === "sdr" && c.details?.backend && (
        <p className="muted">
          {t("sdrBackend")}: <strong>{c.details.backend}</strong>
          {devices.length > 0 && <> · {t("sdrDevices")}: {devices.map((d) => d.label || d.serial || `#${d.index}`).join(", ")}</>}
        </p>
      )}
      {c.id === "sdr" && onRescan && c.details?.state !== "recording" && <button onClick={onRescan}>{t("rescan")}</button>}
      {c.id === "scheduler" && next && (
        <p className="muted">
          {t("nextPass")}: {next.kind === "event" ? "⏱ " : ""}{next.satellite} – {fmt(next.rec_start)} ({t("inMinutes", { n: Math.max(0, Math.round(next.seconds_until / 60)) })})
        </p>
      )}
      {c.id === "storage" && (
        <p className="muted">{t("usedOfQuota", { used: c.details.used_gb, quota: c.details.quota_gb, free: c.details.free_gb })}</p>
      )}
    </div>
  );
}

export default function DashboardPage() {
  const { t } = useTranslation();
  const health = useAsync<Health>(() => api.health());
  const jobs = useAsync<Job[]>(() => api.jobs(6));
  const { state, tick } = useStream();
  // új esemény (SSE) esetén azonnal frissít, a lekérdezés csak tartalék
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { health.reload(); jobs.reload(); }, [tick]);
  useEffect(() => {
    const id = setInterval(() => { health.reload(); jobs.reload(); }, 15000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <>
      {health.error && <p className="err">{t("error")}: {health.error}</p>}
      <div className="cards">{health.data?.components.map((c) => <Card key={c.id} c={c} onRescan={() => api.sdr(true).then(health.reload)} />)}</div>
      {state?.live_waterfall ? <LiveWaterfall /> : <p className="muted">{t("noLiveRecording")}</p>}

      <ConsoleLog />

      <section>
        <h2>{t("recentJobs")}</h2>
        <table>
          <thead><tr><th>#</th><th>{t("kind")}</th><th>{t("satellite")}</th><th>{t("status")}</th><th>{t("started")}</th></tr></thead>
          <tbody>
            {jobs.data?.map((j) => (
              <tr key={j.id}>
                <td>{j.id}</td><td>{t(`kind_${j.kind}`)}</td><td>{j.satellite_name ?? "-"}</td>
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
