import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, Channel, Pass } from "./api";

const MODE_KEY: Record<string, string> = { fm: "chMode_fm", sstv: "chMode_sstv", lrpt: "chMode_lrpt", record: "chMode_record" };

/** Az idővonalról beütemezett egyedi átvonulás visszavonása. */
export function UnschedulePanel({ pass, onDone }: { pass: Pass; onDone: () => void }) {
  const { t } = useTranslation();
  const [error, setError] = useState<string>();
  return (
    <div className="row" style={{ marginTop: ".6rem" }}>
      <button onClick={() => api.unschedulePass(pass.id).then(onDone).catch((e) => setError(e.message))}>{t("unschedulePass")}</button>
      {error && <span className="err">{error}</span>}
    </div>
  );
}

/** Egy előrejelzett (még nem ütemezett) átvonulás beütemezése: a műholdhoz rendelt frekvenciák és módok közül választva. */
export default function SchedulePassPanel({ pass, onDone }: { pass: Pass; onDone: () => void }) {
  const { t } = useTranslation();
  const [channels, setChannels] = useState<Channel[]>();
  const [channel, setChannel] = useState("");
  const [priority, setPriority] = useState("0");
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setChannels(undefined);
    api.channels(pass.norad_id as number).then((c) => { setChannels(c); setChannel(c[0] ? String(c[0].id) : ""); }).catch((e) => setError(e.message));
  }, [pass.norad_id]);

  const submit = async () => {
    setBusy(true);
    try {
      const r = await api.schedulePass({ norad_id: pass.norad_id as number, aos: pass.aos, los: pass.los, channel_id: Number(channel), priority: Number(priority) || 0 });
      setError(undefined);
      if (r.status === "skipped_conflict" || r.status === "unsupported") setError(t("scheduledButSkipped", { status: t(`st_${r.status}`) }));
      else onDone();
    } catch (e) { setError((e as Error).message); }
    setBusy(false);
  };

  return (
    <div className="row" style={{ marginTop: ".6rem" }}>
      {channels && channels.length === 0 && <span className="hint">{t("noChannelsForPass")}</span>}
      {channels && channels.length > 0 && (
        <>
          <label>{t("satFrequency")}
            <select value={channel} onChange={(e) => setChannel(e.target.value)}>
              {channels.map((c) => <option key={c.id} value={c.id}>{c.name} – {t(MODE_KEY[c.mode])}</option>)}
            </select>
          </label>
          <label>{t("priority")}<input type="number" value={priority} onChange={(e) => setPriority(e.target.value)} style={{ width: "5rem" }} /></label>
          <button onClick={submit} disabled={busy || !channel}>{t("schedulePass")}</button>
        </>
      )}
      {error && <span className="err">{error}</span>}
    </div>
  );
}
