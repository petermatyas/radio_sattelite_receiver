import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, Channel, ChannelIn, ChannelOptions, Processor } from "./api";
import ProcessingEditor, { Step } from "./ProcessingEditor";

interface Form {
  name: string; freq: string; mode: string; bw: string; rate: string; gain: string; doppler: boolean; keepIq: boolean; steps: Step[];
}

const blank = (): Form => ({ name: "", freq: "", mode: "fm", bw: "", rate: "", gain: "", doppler: true, keepIq: true, steps: [{ processor: "fm_demod", params: {} }] });

/** Egy műhold frekvenciái és módjai: a frekvencia, a vétel beállításai és a felvétel kiértékelése (feldolgozó lépések) egy helyen. */
export default function SatelliteChannels({ noradId, onChanged }: { noradId: number; onChanged?: () => void }) {
  const { t } = useTranslation();
  const [channels, setChannels] = useState<Channel[]>([]);
  const [options, setOptions] = useState<ChannelOptions>();
  const [processors, setProcessors] = useState<Processor[]>([]);
  const [form, setForm] = useState<Form>(blank);
  const [editing, setEditing] = useState<number>();
  const [error, setError] = useState<string>();
  const load = () => api.channels(noradId).then(setChannels).catch((e) => setError(e.message));
  useEffect(() => {
    load();
    api.channelOptions().then(setOptions).catch(() => {});
    api.processors().then(setProcessors).catch(() => {});
  }, [noradId]);   // eslint-disable-line react-hooks/exhaustive-deps

  const set = <K extends keyof Form>(key: K, value: Form[K]) => setForm((f) => ({ ...f, [key]: value }));
  const defaults = options?.defaults[form.mode];
  const reset = () => { setForm(blank()); setEditing(undefined); };

  const loadPreset = async (mode: string) => {
    try {
      const bw = form.bw === "" ? null : Math.round(Number(form.bw) * 1e3);
      const r = await api.previewSteps({ name: "", center_freq_hz: Math.round(Number(form.freq) * 1e6) || 1, mode: mode as ChannelIn["mode"], bandwidth_hz: bw, settings: {} });
      setForm((f) => ({ ...f, steps: r.steps.map((s) => ({ processor: s.processor, params: (s.params ?? {}) as Step["params"] })) }));
    } catch (err) { setError((err as Error).message); }
  };
  const changeMode = (mode: string) => {
    const d = options?.defaults[mode];
    setForm((f) => ({ ...f, mode, keepIq: d?.keep_iq ?? f.keepIq, doppler: d?.doppler ?? f.doppler }));
    loadPreset(mode);                                   // a mód előbeállítása adja a kiindulási lépéseket; utána szabadon szerkeszthetők
  };

  const body = (): ChannelIn => ({
    name: form.name.trim(), center_freq_hz: Math.round(Number(form.freq) * 1e6), mode: form.mode as ChannelIn["mode"],
    bandwidth_hz: form.bw === "" ? null : Math.round(Number(form.bw) * 1e3),
    sample_rate_hz: form.rate === "" ? null : Math.round(Number(form.rate) * 1e3),
    gain_db: form.gain === "" ? null : Number(form.gain), doppler: form.doppler, keep_iq: form.keepIq, settings: {}, steps: form.steps,
  });

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      if (editing !== undefined) await api.updateChannel(editing, body()); else await api.createChannel(noradId, body());
      setError(undefined); reset(); await load(); onChanged?.();
    } catch (err) { setError((err as Error).message); }
  };

  const edit = (c: Channel) => {
    setEditing(c.id);
    setForm({
      name: c.name, freq: String(c.center_freq_hz / 1e6), mode: c.mode, bw: String(c.bandwidth_hz / 1e3), rate: String(c.sample_rate_hz / 1e3),
      gain: c.gain_db === null ? "" : String(c.gain_db), doppler: c.doppler, keepIq: c.keep_iq,
      steps: (c.steps ?? []).map((s) => ({ processor: s.processor, params: (s.params ?? {}) as Step["params"] })),
    });
  };

  const summary = (c: Channel) => (c.steps && c.steps.length ? c.steps.map((s) => s.processor).join(" → ") : t("noSteps"));

  return (
    <div className="panel">
      <table>
        <thead><tr><th>{t("name")}</th><th>{t("freqMhz")}</th><th>{t("mode")}</th><th>{t("bandwidthKhz")}</th><th>{t("processing")}</th><th /></tr></thead>
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
          <select value={form.mode} onChange={(e) => changeMode(e.target.value)}>
            {(options?.modes ?? ["fm", "sstv", "lrpt", "record"]).map((m) => <option key={m} value={m}>{t(`chMode_${m}`)}</option>)}
          </select>
        </label>
        <label>{t("bandwidthKhz")}<input type="number" step="any" value={form.bw} placeholder={defaults ? String(defaults.bandwidth_hz / 1e3) : ""} onChange={(e) => set("bw", e.target.value)} style={{ width: "6rem" }} /></label>
        <label>{t("sampleRate")}<input type="number" step="any" value={form.rate} placeholder={defaults ? String(defaults.sample_rate_hz / 1e3) : ""} onChange={(e) => set("rate", e.target.value)} style={{ width: "6rem" }} /></label>
        <label>{t("gain")}<input type="number" step="any" value={form.gain} onChange={(e) => set("gain", e.target.value)} style={{ width: "5rem" }} /></label>
        <label className="inline"><input type="checkbox" checked={form.doppler} onChange={(e) => set("doppler", e.target.checked)} />{t("doppler")}</label>
        <label className="inline"><input type="checkbox" checked={form.keepIq} onChange={(e) => set("keepIq", e.target.checked)} />{t("keepIq")}</label>
        <label>{t("name")}<input value={form.name} placeholder={t("autoName")} onChange={(e) => set("name", e.target.value)} /></label>
        <div style={{ width: "100%" }}>
          <ProcessingEditor
            steps={form.steps} processors={processors} onChange={(steps) => set("steps", steps)}
            presets={(options?.modes ?? []).map((m) => ({ key: m, label: t(`chMode_${m}`) }))} onPreset={loadPreset}
          />
        </div>
        <button type="submit">{editing !== undefined ? t("save") : t("add")}</button>
        {editing !== undefined && <button type="button" onClick={reset}>{t("cancel")}</button>}
      </form>
      {error && <p className="err">{t("error")}: {error}</p>}
    </div>
  );
}
