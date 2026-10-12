import { useTranslation } from "react-i18next";

/** A processzor paramétereinek űrlapja a Pydantic JSON-sémából. Az üres mező = alapértelmezett érték (nem kerül a paraméterek közé). */
export type Params = Record<string, unknown>;
type Prop = Record<string, any>;

interface Field {
  kind: "bool" | "number" | "integer" | "enum" | "string" | "strings" | "json";
  nullable: boolean;
  options?: string[];
  prop: Prop;
}

function describe(raw: Prop): Field {
  let prop = raw;
  let nullable = false;
  if (Array.isArray(raw.anyOf)) {
    const real = raw.anyOf.filter((x: Prop) => x.type !== "null");
    nullable = real.length !== raw.anyOf.length;
    prop = { ...real[0], description: raw.description, default: raw.default, title: raw.title };
  }
  if (prop.enum) return { kind: "enum", nullable, options: prop.enum.map(String), prop };
  if (prop.type === "boolean") return { kind: "bool", nullable, prop };
  if (prop.type === "integer") return { kind: "integer", nullable, prop };
  if (prop.type === "number") return { kind: "number", nullable, prop };
  if (prop.type === "string") return { kind: "string", nullable, prop };
  if (prop.type === "array" && prop.items?.type === "string") return { kind: "strings", nullable, prop };
  return { kind: "json", nullable, prop };
}

export default function ParamsForm({ schema, value, onChange }: { schema: Prop; value: Params; onChange: (v: Params) => void }) {
  const { t } = useTranslation();
  const props: Record<string, Prop> = schema?.properties ?? {};
  const names = Object.keys(props);
  if (names.length === 0) return <p className="muted">{t("noParams")}</p>;

  const set = (name: string, v: unknown) => {
    const next = { ...value };
    if (v === undefined || v === "") delete next[name]; else next[name] = v;
    onChange(next);
  };

  return (
    <div className="params">
      {names.map((name) => {
        const f = describe(props[name]);
        const current = value[name];
        const def = f.prop.default;
        const help = f.prop.description as string | undefined;
        let input: JSX.Element;
        if (f.kind === "bool") {
          input = <input type="checkbox" checked={Boolean(current ?? def ?? false)} onChange={(e) => set(name, e.target.checked)} />;
        } else if (f.kind === "enum") {
          input = (
            <select value={current === undefined ? "" : String(current)} onChange={(e) => set(name, e.target.value || undefined)}>
              <option value="">{t("defaultValue")}{def != null ? ` (${String(def)})` : ""}</option>
              {f.options!.map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
          );
        } else if (f.kind === "number" || f.kind === "integer") {
          input = (
            <input
              type="number" step={f.kind === "integer" ? 1 : "any"} min={f.prop.minimum ?? f.prop.exclusiveMinimum} max={f.prop.maximum}
              value={current === undefined ? "" : String(current)} placeholder={def != null ? String(def) : f.nullable ? t("optional") : ""}
              onChange={(e) => set(name, e.target.value === "" ? undefined : Number(e.target.value))}
            />
          );
        } else if (f.kind === "strings") {
          input = (
            <input
              value={Array.isArray(current) ? (current as string[]).join(", ") : ""} placeholder={t("commaSeparated")}
              onChange={(e) => set(name, e.target.value.trim() === "" ? undefined : e.target.value.split(",").map((s) => s.trim()).filter(Boolean))}
            />
          );
        } else if (f.kind === "string") {
          input = <input value={(current as string | undefined) ?? ""} placeholder={def != null ? String(def) : ""} onChange={(e) => set(name, e.target.value)} />;
        } else {
          input = (
            <textarea
              rows={2} value={current === undefined ? "" : JSON.stringify(current)} placeholder="JSON"
              onChange={(e) => { try { set(name, e.target.value === "" ? undefined : JSON.parse(e.target.value)); } catch { /* gépelés közben érvénytelen lehet */ } }}
            />
          );
        }
        return (
          <label key={name} title={help}>
            <span>{name}</span>
            {input}
            {help && <small className="muted">{help}</small>}
          </label>
        );
      })}
    </div>
  );
}
