import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { Pass } from "./api";
import { fmt } from "./hooks";
import { useNow } from "./stream";

const COLORS: Record<string, string> = {
  planned: "#3a7bd5", partial: "#e0a030", skipped_conflict: "#8a8f98", unsupported: "#8a8f98", recording: "#d33",
  processing: "#8e5bd0", done: "#2a9d3a", failed: "#8b1a1a", missed: "#b0762c",
};
const HATCHED = new Set(["skipped_conflict", "unsupported"]);   // nincs felvételi ablak: csak az átvonulás látszik
const laneName = (p: Pass) => (p.kind === "event" ? `⏱ ${p.satellite_name}` : p.satellite_name);
const LABEL_W = 150, ROW_H = 30, AXIS_H = 34, W = 1000;

/** Naptár-/idővonal nézet: műholdanként egy sáv, az átvonulások a felvételi ablakukkal, színezve az állapotuk szerint. */
export default function PassTimeline({ passes, hours }: { passes: Pass[]; hours: number }) {
  const { t } = useTranslation();
  const now = useNow(30_000);
  const [selected, setSelected] = useState<number>();

  const { lanes, start, end } = useMemo(() => {
    const start = now - 30 * 60_000;
    const end = now + hours * 3600_000;
    const names: string[] = [];
    for (const p of passes) if (!names.includes(laneName(p))) names.push(laneName(p));
    return { lanes: names, start, end };
  }, [passes, hours, now]);

  const plotW = W - LABEL_W - 10;
  const x = (ms: number) => LABEL_W + ((ms - start) / (end - start)) * plotW;
  const height = AXIS_H + Math.max(1, lanes.length) * ROW_H + 6;

  // idő-osztások: 1 / 3 / 6 / 12 órás lépés a tartománytól függően; a napváltások vastagabbak
  const step = hours <= 12 ? 1 : hours <= 48 ? 3 : hours <= 96 ? 6 : 12;
  const ticks: number[] = [];
  const first = new Date(start); first.setMinutes(0, 0, 0);
  for (let t0 = first.getTime(); t0 <= end; t0 += 3600_000) {
    if (t0 >= start && new Date(t0).getHours() % step === 0) ticks.push(t0);
  }
  const sel = passes.find((p) => p.id === selected);

  return (
    <div className="timeline">
      <svg viewBox={`0 0 ${W} ${height}`} width="100%" role="img" aria-label={t("timeline")}>
        <defs>
          <pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <rect width="6" height="6" fill="#8a8f9833" />
            <line x1="0" y1="0" x2="0" y2="6" stroke="#8a8f98" strokeWidth="2" />
          </pattern>
        </defs>
        {ticks.map((tk) => {
          const d = new Date(tk);
          const midnight = d.getHours() === 0;
          return (
            <g key={tk}>
              <line x1={x(tk)} x2={x(tk)} y1={AXIS_H - 6} y2={height} stroke={midnight ? "#8888" : "#8883"} strokeWidth={midnight ? 1.5 : 1} />
              <text x={x(tk)} y={AXIS_H - 20} fontSize="11" textAnchor="middle" fill="currentColor">
                {midnight ? d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) : ""}
              </text>
              <text x={x(tk)} y={AXIS_H - 8} fontSize="11" textAnchor="middle" fill="currentColor" opacity="0.7">
                {String(d.getHours()).padStart(2, "0")}:00
              </text>
            </g>
          );
        })}
        {lanes.map((name, i) => (
          <g key={name}>
            <rect x={0} y={AXIS_H + i * ROW_H} width={W} height={ROW_H} fill={i % 2 ? "#8881" : "none"} />
            <text x={6} y={AXIS_H + i * ROW_H + ROW_H / 2 + 4} fontSize="12" fill="currentColor">{name.length > 22 ? `${name.slice(0, 21)}…` : name}</text>
          </g>
        ))}
        {passes.map((p) => {
          const lane = lanes.indexOf(laneName(p));
          const a = new Date(p.rec_start ?? p.aos).getTime(), b = new Date(p.rec_end ?? p.los).getTime();
          const x0 = Math.max(LABEL_W, x(a)), x1 = Math.min(LABEL_W + plotW, x(b));
          if (x1 <= LABEL_W || x0 >= LABEL_W + plotW) return null;
          return (
            <rect
              key={p.id} x={x0} y={AXIS_H + lane * ROW_H + 5} width={Math.max(5, x1 - x0)} height={ROW_H - 10} rx={3}
              fill={HATCHED.has(p.status) ? "url(#hatch)" : COLORS[p.status] ?? "#3a7bd5"}
              stroke={p.id === selected ? "#fff" : "none"} strokeWidth={2} style={{ cursor: "pointer" }}
              onClick={() => setSelected(p.id === selected ? undefined : p.id)}
            >
              <title>{`${laneName(p)}\n${fmt(p.aos)} – ${new Date(p.los).toLocaleTimeString()}\n${p.max_elevation_deg.toFixed(0)}° · ${t(`st_${p.status}`)}`}</title>
            </rect>
          );
        })}
        <line x1={x(now)} x2={x(now)} y1={AXIS_H - 6} y2={height} stroke="#ff3cc8" strokeWidth={2} />
        <text x={x(now) + 4} y={AXIS_H + 10} fontSize="10" fill="#ff3cc8">{t("now")}</text>
      </svg>
      <div className="legend">
        {["planned", "partial", "skipped_conflict", "recording", "processing", "done", "failed", "missed"].map((s) => (
          <span key={s}><i style={{ background: HATCHED.has(s) ? "repeating-linear-gradient(45deg,#8a8f98,#8a8f98 2px,#8a8f9833 2px,#8a8f9833 5px)" : COLORS[s] }} />{t(`st_${s}`)}</span>
        ))}
      </div>
      {sel && (
        <div className="panel">
          <strong>{laneName(sel)}</strong>{sel.norad_id !== null && ` (${sel.norad_id})`} – <span className={`st-${sel.status}`}>{t(`st_${sel.status}`)}</span>
          <div className="muted">
            {t("aos")}: {fmt(sel.aos)} · {t("tca")}: {new Date(sel.tca).toLocaleTimeString()} · {t("los")}: {new Date(sel.los).toLocaleTimeString()}
            {" · "}{t("maxEl")}: {sel.max_elevation_deg.toFixed(1)}°
            {sel.rec_start && sel.rec_end && <> · {t("recWindow")}: {new Date(sel.rec_start).toLocaleTimeString()} – {new Date(sel.rec_end).toLocaleTimeString()}</>}
          </div>
        </div>
      )}
      {passes.length === 0 && <p className="muted">{t("noPasses")}</p>}
    </div>
  );
}
