import { useTranslation } from "react-i18next";
import { Processor } from "./api";
import ParamsForm, { Params } from "./ParamsForm";

export interface Step { processor: string; params: Params }

/** A felvétel kiértékelése: a feldolgozó lépések sorrendje és beállításai (pl. FM-demodulálás, SSTV-dekódolás, waterfall).
 *  Az üres lista azt jelenti, hogy csak a nyers felvétel készül el. */
export default function ProcessingEditor({ steps, processors, onChange, presets, onPreset }: {
  steps: Step[]; processors: Processor[]; onChange: (s: Step[]) => void;
  presets?: { key: string; label: string }[]; onPreset?: (key: string) => void;
}) {
  const { t } = useTranslation();
  const patch = (i: number, p: Partial<Step>) => onChange(steps.map((s, j) => (j === i ? { ...s, ...p } : s)));
  const move = (i: number, dir: -1 | 1) => {
    if (i + dir < 0 || i + dir >= steps.length) return;
    const next = [...steps];
    [next[i], next[i + dir]] = [next[i + dir], next[i]];
    onChange(next);
  };
  return (
    <fieldset className="processing">
      <legend>{t("processing")}</legend>
      {presets && onPreset && (
        <div className="row">
          <span className="muted">{t("loadPreset")}:</span>
          {presets.map((p) => <button type="button" key={p.key} onClick={() => onPreset(p.key)}>{p.label}</button>)}
        </div>
      )}
      {steps.length === 0 && <p className="muted">{t("noProcessing")}</p>}
      <ol>
        {steps.map((s, i) => {
          const proc = processors.find((p) => p.name === s.processor);
          return (
            <li key={i} className="step-edit">
              <div className="row">
                <label>{t("processor")}
                  <select value={s.processor} onChange={(e) => patch(i, { processor: e.target.value, params: {} })}>
                    {!proc && <option value={s.processor}>{s.processor} ⚠</option>}
                    {processors.map((p) => <option key={p.name} value={p.name}>{p.title}</option>)}
                  </select>
                </label>
                <button type="button" onClick={() => move(i, -1)} disabled={i === 0} aria-label={t("moveUp")}>↑</button>
                <button type="button" onClick={() => move(i, 1)} disabled={i === steps.length - 1} aria-label={t("moveDown")}>↓</button>
                <button type="button" onClick={() => onChange(steps.filter((_, j) => j !== i))}>{t("remove")}</button>
              </div>
              {proc && <ParamsForm schema={proc.params_schema} value={s.params} onChange={(params) => patch(i, { params })} />}
            </li>
          );
        })}
      </ol>
      <button type="button" disabled={processors.length === 0} onClick={() => onChange([...steps, { processor: processors[0].name, params: {} }])}>
        + {t("addStep")}
      </button>
    </fieldset>
  );
}
