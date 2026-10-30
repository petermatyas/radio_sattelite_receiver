import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, Channel, Pipeline, Profile, Rule, Satellite } from "./api";
import { fmt, useAsync } from "./hooks";
import PipelinesPage from "./PipelinesPage";
import TimedEventsSection from "./TimedEventsSection";

function ProfilesSection() {
  const { t } = useTranslation();
  const profiles = useAsync<Profile[]>(() => api.profiles());
  const [form, setForm] = useState({ name: "", freq: "145.800", bw: "25", rate: "250", gain: "", doppler: true });
  const [error, setError] = useState<string>();

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await api.createProfile({
        name: form.name,
        center_freq_hz: Math.round(Number(form.freq) * 1e6),
        bandwidth_hz: Math.round(Number(form.bw) * 1e3),
        sample_rate_hz: Math.round(Number(form.rate) * 1e3),
        gain_db: form.gain === "" ? null : Number(form.gain),
        modulation_hint: "",
        doppler_correction: form.doppler,
        antenna_id: null,
      });
      setError(undefined);
      setForm({ ...form, name: "" });
    } catch (err) { setError((err as Error).message); }
    profiles.reload();
  };

  const num = (key: "freq" | "bw" | "rate" | "gain", label: string, required = true) => (
    <label>
      {label}
      <input type="number" step="any" required={required} value={form[key]} onChange={(e) => setForm({ ...form, [key]: e.target.value })} />
    </label>
  );

  return (
    <section>
      <h2>{t("profiles")}</h2>
      <form className="row" onSubmit={add}>
        <label>
          {t("name")}
          <input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </label>
        {num("freq", t("freqMhz"))}{num("bw", t("bandwidthKhz"))}{num("rate", t("sampleRate"))}{num("gain", t("gain"), false)}
        <label>
          {t("doppler")}
          <input type="checkbox" checked={form.doppler} onChange={(e) => setForm({ ...form, doppler: e.target.checked })} />
        </label>
        <button type="submit">{t("add")}</button>
      </form>
      {(error || profiles.error) && <p className="err">{t("error")}: {error ?? profiles.error}</p>}
      <table>
        <thead>
          <tr><th>{t("name")}</th><th>{t("freqMhz")}</th><th>{t("bandwidthKhz")}</th><th>{t("sampleRate")}</th><th>{t("doppler")}</th><th /></tr>
        </thead>
        <tbody>
          {profiles.data?.map((p) => (
            <tr key={p.id}>
              <td>{p.name}</td>
              <td>{(p.center_freq_hz / 1e6).toFixed(3)}</td>
              <td>{p.bandwidth_hz / 1e3}</td>
              <td>{p.sample_rate_hz / 1e3}</td>
              <td>{p.doppler_correction ? "✓" : ""}</td>
              <td>
                <button onClick={() => api.deleteProfile(p.id).then(profiles.reload).catch((e) => setError(e.message))}>
                  {t("remove")}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

const toLocalInput = (d: Date) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);

function RulesSection() {
  const { t } = useTranslation();
  const rules = useAsync<Rule[]>(() => api.rules());
  const sats = useAsync<Satellite[]>(() => api.satellites());
  const profiles = useAsync<Profile[]>(() => api.profiles());
  const pipelines = useAsync<Pipeline[]>(() => api.pipelines());
  const channels = useAsync<Channel[]>(() => api.allChannels());
  const now = new Date();
  const [form, setForm] = useState({
    norad: "", channel: "", profile: "", pipeline: "", from: toLocalInput(now),
    to: toLocalInput(new Date(now.getTime() + 7 * 86400000)), priority: "0", minEl: "", partial: true,
  });
  const [error, setError] = useState<string>();

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await api.createRule({
        norad_id: Number(form.norad || sats.data?.[0]?.norad_id),
        channel_id: form.channel && form.channel !== "custom" ? Number(form.channel) : null,   // üres: a műhold első frekvenciája
        profile_id: form.channel === "custom" && form.profile ? Number(form.profile) : null,
        pipeline_id: form.channel === "custom" && form.pipeline ? Number(form.pipeline) : null,
        valid_from: new Date(form.from).toISOString(),
        valid_to: new Date(form.to).toISOString(),
        priority: Number(form.priority),
        min_elevation_deg: form.minEl === "" ? null : Number(form.minEl),
        allow_partial: form.partial,
        enabled: true,
      });
      setError(undefined);
    } catch (err) { setError((err as Error).message); }
    rules.reload();
  };

  const select = <T extends { id?: number; norad_id?: number; name: string }>(
    key: "norad" | "profile" | "pipeline", label: string, items: T[] | undefined,
  ) => (
    <label>
      {label}
      <select value={form[key]} onChange={(e) => setForm({ ...form, [key]: e.target.value })}>
        {items?.map((i) => {
          const id = i.id ?? i.norad_id;
          return <option key={id} value={id}>{i.name}</option>;
        })}
      </select>
    </label>
  );

  return (
    <section>
      <h2>{t("rules")}</h2>
      <form className="row" onSubmit={add}>
        {select("norad", t("satellite"), sats.data)}
        {(() => {
          const norad = Number(form.norad || sats.data?.[0]?.norad_id);
          const own = channels.data?.filter((c) => c.norad_id === norad) ?? [];
          return (
            <label>{t("satFrequency")}
              <select value={form.channel} onChange={(e) => setForm({ ...form, channel: e.target.value })}>
                {own.length === 0 && <option value="">{t("noChannelsShort")}</option>}
                {own.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                <option value="custom">{t("customChannel")}</option>
              </select>
            </label>
          );
        })()}
        {form.channel === "custom" && select("profile", t("profile"), profiles.data)}
        {form.channel === "custom" && select("pipeline", t("pipeline"), pipelines.data)}
        <label>{t("validFrom")}<input type="datetime-local" required value={form.from} onChange={(e) => setForm({ ...form, from: e.target.value })} /></label>
        <label>{t("validTo")}<input type="datetime-local" required value={form.to} onChange={(e) => setForm({ ...form, to: e.target.value })} /></label>
        <label>{t("priority")}<input type="number" value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })} style={{ width: "5rem" }} /></label>
        <label>{t("minElevation")}<input type="number" min="0" max="90" step="any" value={form.minEl} onChange={(e) => setForm({ ...form, minEl: e.target.value })} style={{ width: "5rem" }} /></label>
        <label>{t("allowPartial")}<input type="checkbox" checked={form.partial} onChange={(e) => setForm({ ...form, partial: e.target.checked })} /></label>
        <button type="submit">{t("add")}</button>
      </form>
      {(error || rules.error) && <p className="err">{t("error")}: {error ?? rules.error}</p>}
      <table>
        <thead>
          <tr>
            <th>{t("satellite")}</th><th>{t("validFrom")}</th><th>{t("validTo")}</th><th>{t("priority")}</th>
            <th>{t("minElevation")}</th><th>{t("enabled")}</th><th />
          </tr>
        </thead>
        <tbody>
          {rules.data?.map((r) => (
            <tr key={r.id}>
              <td>{r.satellite_name} ({r.norad_id})</td>
              <td>{fmt(r.valid_from)}</td><td>{fmt(r.valid_to)}</td>
              <td>{r.priority}</td><td>{r.min_elevation_deg ?? "-"}</td>
              <td>
                <input
                  type="checkbox" checked={r.enabled}
                  onChange={(e) => api.updateRule(r.id, { ...r, enabled: e.target.checked }).then(rules.reload).catch((x) => setError(x.message))}
                />
              </td>
              <td><button onClick={() => api.deleteRule(r.id).then(rules.reload)}>{t("remove")}</button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

type Part = "rules" | "events" | "profiles" | "pipelines";
const PARTS: Part[] = ["rules", "events", "profiles", "pipelines"];

/** Ütemezés: mit és mikor vegyen fel a rendszer. Négy külön, átlátható rész; egyszerre mindig csak egy látszik. */
export default function SchedulePage() {
  const { t } = useTranslation();
  const [part, setPart] = useState<Part>("rules");
  return (
    <>
      <div className="row subtabs">
        {PARTS.map((p) => (
          <button key={p} className={`tab ${part === p ? "active" : ""}`} onClick={() => setPart(p)}>{t(`part_${p}`)}</button>
        ))}
      </div>
      <p className="muted">{t(`partHint_${part}`)}</p>
      {part === "rules" && <RulesSection />}
      {part === "events" && <TimedEventsSection />}
      {part === "profiles" && <ProfilesSection />}
      {part === "pipelines" && <PipelinesPage />}
    </>
  );
}
