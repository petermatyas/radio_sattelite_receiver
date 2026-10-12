import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, Pipeline, Profile, TimedEvent, TimedEventIn, TimedEventPreview } from "./api";
import { fmt, fmtBytes, isoToZoned, useAsync, zonedToIso } from "./hooks";

const browserZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

interface Form {
  name: string; profile: string; pipeline: string; start: string; minutes: string; recurrence: "none" | "daily" | "weekly";
  weekdays: number[]; until: string; timezone: string; priority: string; partial: boolean; enabled: boolean;
}

const blank = (): Form => {
  const tz = browserZone();
  const soon = new Date(Date.now() + 3600_000);
  return {
    name: "", profile: "", pipeline: "", start: isoToZoned(soon.toISOString(), tz).slice(0, 13) + ":00", minutes: "30",
    recurrence: "none", weekdays: [], until: "", timezone: tz, priority: "0", partial: false, enabled: true,
  };
};

/** Időalapú (NORAD ID nélküli) események: fix időablak egy rádióprofil frekvenciáján, egyszeri vagy ismétlődő. */
export default function TimedEventsSection() {
  const { t } = useTranslation();
  const events = useAsync<TimedEvent[]>(() => api.timedEvents());
  const profiles = useAsync<Profile[]>(() => api.profiles());
  const pipelines = useAsync<Pipeline[]>(() => api.pipelines());
  const [form, setForm] = useState<Form>(blank);
  const [editing, setEditing] = useState<number>();
  const [error, setError] = useState<string>();
  const [preview, setPreview] = useState<TimedEventPreview>();
  const [previewError, setPreviewError] = useState<string>();

  const body = useMemo((): TimedEventIn | undefined => {
    const profile = Number(form.profile || profiles.data?.[0]?.id);
    const pipeline = Number(form.pipeline || pipelines.data?.[0]?.id);
    const minutes = Number(form.minutes);
    if (!form.name.trim() || !form.start || !profile || !pipeline || !(minutes > 0)) return undefined;
    try {
      return {
        name: form.name.trim(), enabled: form.enabled, start_at: zonedToIso(form.start, form.timezone),
        duration_s: Math.round(minutes * 60), recurrence: form.recurrence, weekdays: form.recurrence === "weekly" ? form.weekdays : [],
        repeat_until: form.recurrence !== "none" && form.until ? zonedToIso(form.until, form.timezone) : null,
        timezone: form.timezone, profile_id: profile, pipeline_id: pipeline, priority: Number(form.priority) || 0,
        allow_partial: form.partial, notes: "",
      };
    } catch { return undefined; }     // ismeretlen időzóna a mezőben
  }, [form, profiles.data, pipelines.data]);

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
      name: ev.name, profile: String(ev.profile_id), pipeline: String(ev.pipeline_id), start: isoToZoned(ev.start_at, ev.timezone),
      minutes: String(ev.duration_s / 60), recurrence: ev.recurrence, weekdays: ev.weekdays,
      until: ev.repeat_until ? isoToZoned(ev.repeat_until, ev.timezone) : "", timezone: ev.timezone,
      priority: String(ev.priority), partial: ev.allow_partial, enabled: ev.enabled,
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const toggle = (ev: TimedEvent, enabled: boolean) => {
    const { id, next_occurrences, estimated_bytes, profile_name, pipeline_name, ...rest } = ev;
    void next_occurrences; void estimated_bytes; void profile_name; void pipeline_name;
    api.updateTimedEvent(id, { ...rest, enabled }).then(events.reload).catch((x) => setError(x.message));
  };

  const recurrenceText = (ev: TimedEvent) =>
    ev.recurrence === "none" ? t("recNone")
      : ev.recurrence === "daily" ? t("recDaily")
        : `${t("recWeekly")}: ${ev.weekdays.map((d) => t(`day_${DAYS[d]}`)).join(", ")}`;

  return (
    <section>
      <h2>{t("timedEvents")}</h2>
      <p className="muted">{t("timedEventsHint")}</p>
      <form className="row" onSubmit={submit}>
        <label>{t("name")}<input required value={form.name} onChange={(e) => set("name", e.target.value)} /></label>
        <label>{t("profile")}
          <select value={form.profile} onChange={(e) => set("profile", e.target.value)}>
            {profiles.data?.map((p) => <option key={p.id} value={p.id}>{p.name} ({(p.center_freq_hz / 1e6).toFixed(3)} MHz)</option>)}
          </select>
        </label>
        <label>{t("pipeline")}
          <select value={form.pipeline} onChange={(e) => set("pipeline", e.target.value)}>
            {pipelines.data?.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </label>
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
          <tr><th>{t("name")}</th><th>{t("profile")} / {t("pipeline")}</th><th>{t("recurrence")}</th><th>{t("durationMin")}</th><th>{t("nextOccurrences")}</th><th>{t("enabled")}</th><th /></tr>
        </thead>
        <tbody>
          {events.data?.map((ev) => (
            <tr key={ev.id}>
              <td>⏱ {ev.name}</td>
              <td>{ev.profile_name} / {ev.pipeline_name}</td>
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
