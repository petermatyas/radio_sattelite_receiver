import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, Processor, TimedEvent, TimedEventIn, TimedEventPreview } from "./api";
import ProcessingEditor, { Step } from "./ProcessingEditor";
import { fmt, fmtBytes, isoToZoned, useAsync, zonedToIso } from "./hooks";

const browserZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

interface Form {
  name: string; freq: string; bw: string; rate: string; gain: string; steps: Step[]; start: string; minutes: string; recurrence: "none" | "daily" | "weekly";
  weekdays: number[]; until: string; timezone: string; priority: string; partial: boolean; enabled: boolean;
}

const blank = (): Form => {
  const tz = browserZone();
  const soon = new Date(Date.now() + 3600_000);
  return {
    name: "", freq: "", bw: "", rate: "", gain: "", steps: [{ processor: "fm_demod", params: {} }], start: isoToZoned(soon.toISOString(), tz).slice(0, 13) + ":00", minutes: "30",
    recurrence: "none", weekdays: [], until: "", timezone: tz, priority: "0", partial: false, enabled: true,
  };
};

/** Időalapú (NORAD ID nélküli) események: fix időablak a megadott frekvencián, egyszeri vagy ismétlődő. */
export default function TimedEventsSection() {
  const { t } = useTranslation();
  const events = useAsync<TimedEvent[]>(() => api.timedEvents());
  const processors = useAsync<Processor[]>(() => api.processors());
  const [form, setForm] = useState<Form>(blank);
  const [editing, setEditing] = useState<number>();
  const [error, setError] = useState<string>();
  const [preview, setPreview] = useState<TimedEventPreview>();
  const [previewError, setPreviewError] = useState<string>();

  const body = useMemo((): TimedEventIn | undefined => {
    const minutes = Number(form.minutes);
    const freq = Math.round(Number(form.freq) * 1e6);
    if (!form.name.trim() || !form.start || !(freq > 0) || !(minutes > 0)) return undefined;
    try {
      return {
        name: form.name.trim(), enabled: form.enabled, start_at: zonedToIso(form.start, form.timezone),
        duration_s: Math.round(minutes * 60), recurrence: form.recurrence, weekdays: form.recurrence === "weekly" ? form.weekdays : [],
        repeat_until: form.recurrence !== "none" && form.until ? zonedToIso(form.until, form.timezone) : null,
        timezone: form.timezone, center_freq_hz: freq,
        bandwidth_hz: form.bw === "" ? null : Math.round(Number(form.bw) * 1e3), sample_rate_hz: form.rate === "" ? null : Math.round(Number(form.rate) * 1e3),
        gain_db: form.gain === "" ? null : Number(form.gain), steps: form.steps, priority: Number(form.priority) || 0,
        allow_partial: form.partial, notes: "",
      };
    } catch { return undefined; }     // ismeretlen időzóna a mezőben
  }, [form]);

  // élő előnézet: a következő előfordulások mentés nélkül
  useEffect(() => {
    if (!body) { setPreview(undefined); setPreviewError(undefined); return; }
    const id = setTimeout(() => {
      api.previewTimedEvent(body).then((p) => { setPreview(p); setPreviewError(undefined); })
        .catch((e) => { setPreview(undefined); setPreviewError(e.message); });
    }, 300);
    return () => clearTimeout(id);
  }, [body]);

  const set = <K extends keyof Form>(key: K, value: Form[K]) => setForm((f) => ({ ...f, [key]: value }));
  const reset = () => { setForm(blank()); setEditing(undefined); setError(undefined); };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!body) return;
    try {
      if (editing !== undefined) await api.updateTimedEvent(editing, body); else await api.createTimedEvent(body);
      reset();
    } catch (err) { setError((err as Error).message); }
    events.reload();
  };

  const edit = (ev: TimedEvent) => {
    setEditing(ev.id);
    setForm({
      name: ev.name, freq: String(ev.center_freq_hz / 1e6), bw: String((ev.bandwidth_hz ?? 25000) / 1e3), rate: String((ev.sample_rate_hz ?? 250000) / 1e3),
      gain: ev.gain_db == null ? "" : String(ev.gain_db),
      steps: ev.steps.map((x) => ({ processor: x.processor, params: (x.params ?? {}) as Step["params"] })), start: isoToZoned(ev.start_at, ev.timezone),
      minutes: String(ev.duration_s / 60), recurrence: ev.recurrence, weekdays: ev.weekdays,
      until: ev.repeat_until ? isoToZoned(ev.repeat_until, ev.timezone) : "", timezone: ev.timezone,
      priority: String(ev.priority), partial: ev.allow_partial, enabled: ev.enabled,
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const toggle = (ev: TimedEvent, enabled: boolean) => {
    const { id, next_occurrences, estimated_bytes, profile_name, pipeline_name, profile_id, pipeline_id, ...rest } = ev;
    void next_occurrences; void estimated_bytes; void profile_name; void pipeline_name; void profile_id; void pipeline_id;
    api.updateTimedEvent(id, { ...rest, enabled }).then(events.reload).catch((x) => setError(x.message));
  };

  const recurrenceText = (ev: TimedEvent) =>
    ev.recurrence === "none" ? t("recNone")
      : ev.recurrence === "daily" ? t("recDaily")
        : `${t("recWeekly")}: ${ev.weekdays.map((d) => t(`day_${DAYS[d]}`)).join(", ")}`;

  return (
    <section>
      <h2>{t("timedEvents")}</h2>
      <form className="row" onSubmit={submit}>
        <label>{t("name")}<input required value={form.name} onChange={(e) => set("name", e.target.value)} /></label>
        <label>{t("freqMhz")}<input type="number" step="any" required value={form.freq} onChange={(e) => set("freq", e.target.value)} style={{ width: "9rem" }} /></label>
        <label>{t("bandwidthKhz")}<input type="number" step="any" value={form.bw} placeholder="25" onChange={(e) => set("bw", e.target.value)} style={{ width: "6rem" }} /></label>
        <label>{t("sampleRate")}<input type="number" step="any" value={form.rate} placeholder="250" onChange={(e) => set("rate", e.target.value)} style={{ width: "6rem" }} /></label>
        <label>{t("gain")}<input type="number" step="any" value={form.gain} onChange={(e) => set("gain", e.target.value)} style={{ width: "5rem" }} /></label>
        <label>{t("eventStart")}<input type="datetime-local" required value={form.start} onChange={(e) => set("start", e.target.value)} /></label>
        <label>{t("durationMin")}<input type="number" min="0.1" max="360" step="any" required value={form.minutes} onChange={(e) => set("minutes", e.target.value)} style={{ width: "6rem" }} /></label>
        <label>{t("recurrence")}
          <select value={form.recurrence} onChange={(e) => set("recurrence", e.target.value as Form["recurrence"])}>
            <option value="none">{t("recNone")}</option><option value="daily">{t("recDaily")}</option><option value="weekly">{t("recWeekly")}</option>
          </select>
        </label>
        {form.recurrence === "weekly" && (
          <span className="weekdays">
            {DAYS.map((d, i) => (
              <label key={d} className="inline">
                <input type="checkbox" checked={form.weekdays.includes(i)}
                  onChange={(e) => set("weekdays", e.target.checked ? [...form.weekdays, i].sort() : form.weekdays.filter((x) => x !== i))} />
                {t(`day_${d}`)}
              </label>
            ))}
          </span>
        )}
        {form.recurrence !== "none" && (
          <label>{t("repeatUntil")}<input type="datetime-local" value={form.until} onChange={(e) => set("until", e.target.value)} /></label>
        )}
        <label>{t("timezone")}<input value={form.timezone} onChange={(e) => set("timezone", e.target.value)} list="tz-list" style={{ width: "11rem" }} /></label>
        <datalist id="tz-list">{["UTC", browserZone(), "Europe/Budapest"].map((z) => <option key={z} value={z} />)}</datalist>
        <label>{t("priority")}<input type="number" value={form.priority} onChange={(e) => set("priority", e.target.value)} style={{ width: "5rem" }} /></label>
        <label>{t("allowPartial")}<input type="checkbox" checked={form.partial} onChange={(e) => set("partial", e.target.checked)} /></label>
        <div style={{ width: "100%" }}>
          <ProcessingEditor
            steps={form.steps} processors={processors.data ?? []} onChange={(steps) => set("steps", steps)}
            presets={["fm", "sstv", "record"].map((m) => ({ key: m, label: t(`chMode_${m}`) }))}
            onPreset={(m) => api.previewSteps({ name: "", center_freq_hz: 1, mode: m as "fm", settings: {} })
              .then((r) => set("steps", r.steps.map((x) => ({ processor: x.processor, params: (x.params ?? {}) as Step["params"] }))))
              .catch((e) => setError(e.message))}
          />
        </div>
        <button type="submit" disabled={!body || !!previewError}>{editing !== undefined ? t("save") : t("add")}</button>
        {editing !== undefined && <button type="button" onClick={reset}>{t("cancel")}</button>}
      </form>
      {previewError && <p className="err">{t("error")}: {previewError}</p>}
      {preview && (
        <p className="muted">
          {t("nextOccurrences")}: {preview.next_occurrences.length ? preview.next_occurrences.slice(0, 4).map(fmt).join(" · ") : t("noOccurrences")}
          {" – "}{t("estimatedSize")}: {fmtBytes(preview.estimated_bytes)}
        </p>
      )}
      {(error || events.error) && <p className="err">{t("error")}: {error ?? events.error}</p>}
      <table>
        <thead>
          <tr><th>{t("name")}</th><th>{t("freqMhz")} / {t("processing")}</th><th>{t("recurrence")}</th><th>{t("durationMin")}</th><th>{t("nextOccurrences")}</th><th>{t("enabled")}</th><th /></tr>
        </thead>
        <tbody>
          {events.data?.map((ev) => (
            <tr key={ev.id}>
              <td>⏱ {ev.name}</td>
              <td>{(ev.center_freq_hz / 1e6).toFixed(4)} / {ev.steps.length ? ev.steps.map((x) => x.processor).join(" → ") : t("noSteps")}</td>
              <td>{recurrenceText(ev)}</td>
              <td>{+(ev.duration_s / 60).toFixed(1)}</td>
              <td>{ev.next_occurrences.length ? ev.next_occurrences.slice(0, 2).map(fmt).join(", ") : "-"}</td>
              <td><input type="checkbox" checked={ev.enabled} onChange={(e) => toggle(ev, e.target.checked)} /></td>
              <td>
                <button onClick={() => edit(ev)}>{t("edit")}</button>{" "}
                <button onClick={() => window.confirm(t("confirmDeleteEvent")) && api.deleteTimedEvent(ev.id).then(events.reload).catch((x) => setError(x.message))}>{t("remove")}</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {events.data?.length === 0 && <p className="muted">{t("noTimedEvents")}</p>}
    </section>
  );
}
