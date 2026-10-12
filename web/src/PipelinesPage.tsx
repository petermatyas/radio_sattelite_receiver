import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, Pipeline, Processor } from "./api";
import ParamsForm, { Params } from "./ParamsForm";
import { useAsync } from "./hooks";

interface Draft {
  id?: number;
  name: string;
  mode: string;
  keep_iq: boolean;
  steps: { processor: string; params: Params }[];
}

const MODES = ["fm", "sstv", "lrpt", "record", "custom"];
const blank = (): Draft => ({ name: "", mode: "custom", keep_iq: true, steps: [] });

export default function PipelinesPage() {
  const { t } = useTranslation();
  const pipelines = useAsync<Pipeline[]>(() => api.pipelines());
  const processors = useAsync<Processor[]>(() => api.processors());
  const [draft, setDraft] = useState<Draft>();
  const [error, setError] = useState<string>();
  const [saved, setSaved] = useState(false);

  const procs = processors.data ?? [];
  const edit = (p: Pipeline) => {
    setDraft({ id: p.id, name: p.name, mode: p.mode, keep_iq: p.keep_iq,
      steps: p.steps.map((s) => ({ processor: s.processor, params: (s.params ?? {}) as Params })) });
    setError(undefined); setSaved(false);
  };
  const copy = (p: Pipeline) => { edit(p); setDraft((d) => d && { ...d, id: undefined, name: `${p.name} (${t("copy")})` }); };

  const patchStep = (i: number, patch: Partial<Draft["steps"][number]>) =>
    setDraft((d) => d && { ...d, steps: d.steps.map((s, j) => (j === i ? { ...s, ...patch } : s)) });
  const move = (i: number, dir: -1 | 1) => setDraft((d) => {
    if (!d || i + dir < 0 || i + dir >= d.steps.length) return d;
    const steps = [...d.steps];
    [steps[i], steps[i + dir]] = [steps[i + dir], steps[i]];
    return { ...d, steps };
  });

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!draft) return;
    const body = { name: draft.name, mode: draft.mode, keep_iq: draft.keep_iq, steps: draft.steps };
    try {
      const out = draft.id ? await api.updatePipeline(draft.id, body) : await api.createPipeline(body);
      setDraft({ ...draft, id: out.id }); setError(undefined); setSaved(true); pipelines.reload();
    } catch (err) { setError((err as Error).message); setSaved(false); }
  };
  const remove = async (p: Pipeline) => {
    if (!window.confirm(t("confirmDeletePipeline", { name: p.name }))) return;
    try { await api.deletePipeline(p.id); if (draft?.id === p.id) setDraft(undefined); setError(undefined); } catch (err) { setError((err as Error).message); }
    pipelines.reload();
  };

  return (
    <>
      <section>
        <div className="row">
          <h2 style={{ margin: 0 }}>{t("pipelines")}</h2>
          <button onClick={() => { setDraft(blank()); setError(undefined); setSaved(false); }}>{t("newPipeline")}</button>
        </div>
        {pipelines.error && <p className="err">{pipelines.error}</p>}
        <table>
          <thead><tr><th>{t("name")}</th><th>{t("mode")}</th><th>{t("steps")}</th><th>{t("keepIq")}</th><th /></tr></thead>
          <tbody>
            {pipelines.data?.map((p) => (
              <tr key={p.id}>
                <td>{p.name}</td><td>{p.mode}</td>
                <td className="muted">{p.steps.length ? p.steps.map((s) => s.processor).join(" → ") : t("noSteps")}</td>
                <td>{p.keep_iq ? "✓" : ""}</td>
                <td>
                  <button onClick={() => edit(p)}>{t("edit")}</button>{" "}
                  <button onClick={() => copy(p)}>{t("copy")}</button>{" "}
                  <button onClick={() => remove(p)}>{t("remove")}</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {draft && (
        <form className="panel" onSubmit={save}>
          <h3>{draft.id ? t("editPipeline") : t("newPipeline")}</h3>
          <div className="row">
            <label>{t("name")}<input required value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} /></label>
            <label>{t("mode")}
              <input list="modes" required value={draft.mode} onChange={(e) => setDraft({ ...draft, mode: e.target.value })} />
              <datalist id="modes">{MODES.map((m) => <option key={m} value={m} />)}</datalist>
            </label>
            <label>{t("keepIq")}<input type="checkbox" checked={draft.keep_iq} onChange={(e) => setDraft({ ...draft, keep_iq: e.target.checked })} /></label>
          </div>
          <p className="muted">{draft.keep_iq ? t("keepIqHelpOn") : t("keepIqHelpOff")}</p>

          <ol className="steps editor">
            {draft.steps.map((s, i) => {
              const proc = procs.find((p) => p.name === s.processor);
              return (
                <li key={i} className="step-edit">
                  <div className="row">
                    <label>{t("processor")}
                      <select value={s.processor} onChange={(e) => patchStep(i, { processor: e.target.value, params: {} })}>
                        {!proc && <option value={s.processor}>{s.processor} ⚠</option>}
                        {procs.map((p) => <option key={p.name} value={p.name}>{p.name} – {p.title}</option>)}
                      </select>
                    </label>
                    <button type="button" onClick={() => move(i, -1)} disabled={i === 0} aria-label={t("moveUp")}>↑</button>
                    <button type="button" onClick={() => move(i, 1)} disabled={i === draft.steps.length - 1} aria-label={t("moveDown")}>↓</button>
                    <button type="button" onClick={() => setDraft({ ...draft, steps: draft.steps.filter((_, j) => j !== i) })}>{t("remove")}</button>
                  </div>
                  {proc && <ParamsForm schema={proc.params_schema} value={s.params} onChange={(params) => patchStep(i, { params })} />}
                </li>
              );
            })}
          </ol>
          <div className="row">
            <button type="button" disabled={procs.length === 0}
              onClick={() => setDraft({ ...draft, steps: [...draft.steps, { processor: procs[0].name, params: {} }] })}>
              + {t("addStep")}
            </button>
            <button type="submit">{t("save")}</button>
            <button type="button" onClick={() => setDraft(undefined)}>{t("close")}</button>
            {saved && <span className="st-done">{t("saved")}</span>}
          </div>
          {error && <p className="err">{t("error")}: {error}</p>}
        </form>
      )}
    </>
  );
}
