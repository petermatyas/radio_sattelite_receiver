import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, Channel, ChannelIn, ChannelOptions } from "./api";

interface Form {
  name: string; freq: string; mode: string; bw: string; rate: string; gain: string; doppler: boolean; keepIq: boolean;
  sstvMode: string; minLines: string; lrptPipeline: string; squelch: string; waterfall: boolean;
}

const blank = (): Form => ({
  name: "", freq: "", mode: "fm", bw: "", rate: "", gain: "", doppler: true, keepIq: true,
  sstvMode: "auto", minLines: "", lrptPipeline: "auto", squelch: "", waterfall: false,
});

/** Egy műhold frekvenciái és módjai: a frekvencia, a mód és a módhoz tartozó beállítások (pl. SSTV-dekódolási mód) egy helyen. */
export default function SatelliteChannels({ noradId, onChanged }: { noradId: number; onChanged?: () => void }) {
  const { t } = useTranslation();
  const [channels, setChannels] = useState<Channel[]>([]);
  const [options, setOptions] = useState<ChannelOptions>();
  const [form, setForm] = useState<Form>(blank);
  const [editing, setEditing] = useState<number>();
  const [error, setError] = useState<string>();
  const load = () => api.channels(noradId).then(setChannels).catch((e) => setError(e.message));
  useEffect(() => { load(); api.channelOptions().then(setOptions).catch(() => {}); }, [noradId]);   // eslint-disable-line react-hooks/exhaustive-deps

  const set = <K extends keyof Form>(key: K, value: Form[K]) => setForm((f) => ({ ...f, [key]: value }));
  const defaults = options?.defaults[form.mode];
  const reset = () => { setForm(blank()); setEditing(undefined); };

  const body = (): ChannelIn => {
    const settings: Record<string, unknown> = {};
    if (form.mode === "fm") {
      if (form.squelch !== "") settings.squelch_db = Number(form.squelch);
      if (form.waterfall) settings.waterfall = true;
    }
    if (form.mode === "sstv") {
      settings.sstv_mode = form.sstvMode;
      if (form.minLines !== "") settings.min_lines = Number(form.minLines);
    }
    if (form.mode === "lrpt") settings.lrpt_pipeline = form.lrptPipeline;
    return {
      name: form.name.trim(), center_freq_hz: Math.round(Number(form.freq) * 1e6), mode: form.mode as ChannelIn["mode"],
      bandwidth_hz: form.bw === "" ? null : Math.round(Number(form.bw) * 1e3),
      sample_rate_hz: form.rate === "" ? null : Math.round(Number(form.rate) * 1e3),
      gain_db: form.gain === "" ? null : Number(form.gain), doppler: form.doppler, keep_iq: form.keepIq, settings,
    };
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      if (editing !== undefined) await api.updateChannel(editing, body()); else await api.createChannel(noradId, body());
      setError(undefined); reset(); await load(); onChanged?.();
    } catch (err) { setError((err as Error).message); }
  };

  const edit = (c: Channel) => {
    setEditing(c.id);
    const s = c.settings as Record<string, any>;
    setForm({
      name: c.name, freq: String(c.center_freq_hz / 1e6), mode: c.mode, bw: String(c.bandwidth_hz / 1e3), rate: String(c.sample_rate_hz / 1e3),
      gain: c.gain_db === null ? "" : String(c.gain_db), doppler: c.doppler, keepIq: c.keep_iq, sstvMode: s.sstv_mode ?? "auto",
      minLines: s.min_lines ? String(s.min_lines) : "", lrptPipeline: s.lrpt_pipeline ?? "auto",
      squelch: s.squelch_db !== undefined ? String(s.squelch_db) : "", waterfall: !!s.waterfall,
    });
  };

  const summary = (c: Channel) => {
    const s = c.settings as Record<string, any>;
    if (c.mode === "sstv") return `${t("sstvDecoder")}: ${s.sstv_mode ?? "auto"}`;
    if (c.mode === "lrpt") return `${t("lrptPipeline")}: ${s.lrpt_pipeline ?? "auto"}`;
    if (c.mode === "fm") return [s.squelch_db !== undefined ? `${t("squelch")}: ${s.squelch_db} dB` : "", s.waterfall ? "waterfall" : ""].filter(Boolean).join(", ");
    return "";
  };

  return (
    <div className="panel">
      <table>
        <thead><tr><th>{t("name")}</th><th>{t("freqMhz")}</th><th>{t("mode")}</th><th>{t("bandwidthKhz")}</th><th>{t("settings")}</th><th /></tr></thead>
        <tbody>
          {channels.map((c) => (
            <tr key={c.id}>
              <td>{c.name}</td><td>{(c.center_freq_hz / 1e6).toFixed(4)}</td><td>{t(`chMode_${c.mode}`)}</td>
              <td>{c.bandwidth_hz / 1e3}</td><td className="muted">{summary(c)}</td>
              <td>
                <button onClick={() => edit(c)}>{t("edit")}</button>{" "}
                <button onClick={() => window.confirm(t("confirmDeleteChannel")) && api.deleteChannel(c.id).then(() => { load(); onChanged?.(); }).catch((e) => setError(e.message))}>{t("remove")}</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {channels.length === 0 && <p className="muted">{t("noChannels")}</p>}
      <form className="row" onSubmit={submit}>
        <label>{t("freqMhz")}<input type="number" step="any" required value={form.freq} onChange={(e) => set("freq", e.target.value)} style={{ width: "9rem" }} /></label>
        <label>{t("mode")}
          <select value={form.mode} onChange={(e) => set("mode", e.target.value)}>
            {(options?.modes ?? ["fm", "sstv", "lrpt", "record"]).map((m) => <option key={m} value={m}>{t(`chMode_${m}`)}</option>)}
          </select>
        </label>
        {form.mode === "sstv" && (
          <>
            <label>{t("sstvDecoder")}
              <select value={form.sstvMode} onChange={(e) => set("sstvMode", e.target.value)}>
                <option value="auto">{t("sstvAuto")}</option>
                {options?.sstv_modes.map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
            </label>
            <label>{t("minLines")}<input type="number" min="2" value={form.minLines} placeholder="8" onChange={(e) => set("minLines", e.target.value)} style={{ width: "5rem" }} /></label>
          </>
        )}
        {form.mode === "lrpt" && (
          <label>{t("lrptPipeline")}
            <select value={form.lrptPipeline} onChange={(e) => set("lrptPipeline", e.target.value)}>
              {options?.lrpt_pipelines.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
          </label>
        )}
        {form.mode === "fm" && (
          <>
            <label>{t("squelch")}<input type="number" step="any" value={form.squelch} placeholder="dBFS" onChange={(e) => set("squelch", e.target.value)} style={{ width: "6rem" }} /></label>
            <label className="inline"><input type="checkbox" checked={form.waterfall} onChange={(e) => set("waterfall", e.target.checked)} />waterfall</label>
          </>
        )}
        <label>{t("bandwidthKhz")}<input type="number" step="any" value={form.bw} placeholder={defaults ? String(defaults.bandwidth_hz / 1e3) : ""} onChange={(e) => set("bw", e.target.value)} style={{ width: "6rem" }} /></label>
        <label>{t("sampleRate")}<input type="number" step="any" value={form.rate} placeholder={defaults ? String(defaults.sample_rate_hz / 1e3) : ""} onChange={(e) => set("rate", e.target.value)} style={{ width: "6rem" }} /></label>
        <label>{t("gain")}<input type="number" step="any" value={form.gain} onChange={(e) => set("gain", e.target.value)} style={{ width: "5rem" }} /></label>
        <label className="inline"><input type="checkbox" checked={form.doppler} onChange={(e) => set("doppler", e.target.checked)} />{t("doppler")}</label>
        {form.mode !== "lrpt" && <label className="inline"><input type="checkbox" checked={form.keepIq} onChange={(e) => set("keepIq", e.target.checked)} />{t("keepIq")}</label>}
        <label>{t("name")}<input value={form.name} placeholder={t("autoName")} onChange={(e) => set("name", e.target.value)} /></label>
        <button type="submit">{editing !== undefined ? t("save") : t("add")}</button>
        {editing !== undefined && <button type="button" onClick={reset}>{t("cancel")}</button>}
      </form>
      {error && <p className="err">{t("error")}: {error}</p>}
    </div>
  );
}
