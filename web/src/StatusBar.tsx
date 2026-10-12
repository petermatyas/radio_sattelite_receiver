import { useTranslation } from "react-i18next";
import { eventText } from "./hooks";
import { formatCountdown, useNow, useStream } from "./stream";

/** A fejléc alatti állapotsor: SDR, folyamatban lévő feldolgozás, következő felvétel, kapcsolat. */
export function StatusBar({ onOpenEvents }: { onOpenEvents: () => void }) {
  const { t } = useTranslation();
  const { state, connected } = useStream();
  const now = useNow();
  if (!state) return <div className="statusbar muted">{connected ? "…" : t("connecting")}</div>;

  const rec = state.recorder;
  const next = state.next_pass;
  const until = next ? (new Date(next.rec_start).getTime() - now) / 1000 : null;
  const problems = state.unacknowledged.error + state.unacknowledged.warning;
  // az eltelt időt a szerver másodpercenkénti pillanatképe adja, a köztes időt a böngésző interpolálja
  const sinceSnapshot = rec.state === "idle" ? 0 : Math.max(0, (now - new Date(state.time).getTime()) / 1000);
  const elapsed = Math.min(rec.duration_s, rec.elapsed_s + sinceSnapshot);
  const pct = rec.duration_s > 0 ? (elapsed / rec.duration_s) * 100 : 0;

  return (
    <div className="statusbar">
      <span className="sb-item">
        <span className={`dot ${rec.state === "idle" ? "dot-ok" : "dot-rec"}`} />
        {rec.state === "idle" ? t("sdrIdle") : (
          <>
            {t(rec.state === "stopping" ? "sdrStopping" : "sdrRecording")}
            {rec.recording?.satellite ? ` – ${rec.recording.satellite}` : ""}
            {` ${formatCountdown(elapsed)} / ${formatCountdown(rec.duration_s)}`}
            <span className="progress"><span style={{ width: `${pct}%` }} /></span>
          </>
        )}
      </span>
      {state.processing_jobs > 0 && <span className="sb-item">⚙ {t("processingJobs", { n: state.processing_jobs })}</span>}
      {next && until !== null && (
        <span className="sb-item">
          ⏱ {t("nextPass")}: {next.satellite} – {t("inTime", { t: formatCountdown(until) })}
        </span>
      )}
      {rec.last_error && <span className="sb-item sb-err">⚠ {rec.last_error}</span>}
      {state.sdr?.simulated && (
        <span className="sb-sim" title={t("simulatedHint")}>⚠ {t("simulatedSdr", { backend: state.sdr.backend })}</span>
      )}
      <span className="spacer" />
      {problems > 0 && (
        <button className={`sb-problems ${state.unacknowledged.error > 0 ? "badge-error" : "badge-warning"}`} onClick={onOpenEvents}>
          {state.unacknowledged.error > 0 ? "✖" : "⚠"} {t("problemsCount", { n: problems })}
        </button>
      )}
      <span className={`sb-item ${connected ? "muted" : "sb-err"}`} title={connected ? t("live") : t("disconnected")}>
        {connected ? "●" : "○"}
      </span>
    </div>
  );
}

/** Új hibák/figyelmeztetések felugró értesítései (a jobb alsó sarokban). */
export function Toasts({ onOpenEvents }: { onOpenEvents: () => void }) {
  const { t } = useTranslation();
  const { toasts, dismiss } = useStream();
  if (toasts.length === 0) return null;
  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map(({ id, event }) => (
        <div key={id} className={`toast toast-${event.level}`}>
          <div className="toast-body" onClick={() => { onOpenEvents(); dismiss(id); }}>
            <strong>{t(`lvl_${event.level}`)}</strong> {eventText(t, event)}
          </div>
          <button aria-label={t("close")} onClick={() => dismiss(id)}>×</button>
        </div>
      ))}
    </div>
  );
}
