import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "./api";
import { eventText } from "./hooks";
import { useStream } from "./stream";

const LEVEL_TAG: Record<string, string> = { info: "INFO", warning: "WARN", error: "ERR " };

/** Konzolszerű élő napló: mi történik (felvétel indul/kész, feldolgozás), és a hibák. A legújabb sor alul van. */
export default function ConsoleLog() {
  const { t } = useTranslation();
  const { events, state } = useStream();
  const [onlyProblems, setOnlyProblems] = useState(false);
  const [paused, setPaused] = useState(false);
  const [clearedBefore, setClearedBefore] = useState(0);
  const [frozen, setFrozen] = useState(events);
  const box = useRef<HTMLDivElement>(null);
  const stick = useRef(true);                       // csak akkor görget, ha a felhasználó az aljára nézett

  useEffect(() => { if (!paused) setFrozen(events); }, [events, paused]);
  const lines = useMemo(
    () => frozen.filter((e) => e.id > clearedBefore && (!onlyProblems || e.level !== "info")),
    [frozen, clearedBefore, onlyProblems],
  );
  useEffect(() => {
    const el = box.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [lines]);

  const problems = (state?.unacknowledged.error ?? 0) + (state?.unacknowledged.warning ?? 0);
  return (
    <section>
      <div className="row console-bar">
        <h2 style={{ margin: 0 }}>{t("console")}</h2>
        <span className="spacer" />
        <button className={`tab ${!onlyProblems ? "active" : ""}`} onClick={() => setOnlyProblems(false)}>{t("consoleAll")}</button>
        <button className={`tab ${onlyProblems ? "active" : ""}`} onClick={() => setOnlyProblems(true)}>{t("consoleProblems")}</button>
        <button onClick={() => setPaused(!paused)}>{paused ? t("consoleResume") : t("consolePause")}</button>
        <button onClick={() => setClearedBefore(events.length ? events[events.length - 1].id : 0)}>{t("consoleClear")}</button>
        {problems > 0 && <button onClick={() => api.ackAll()}>{t("ackAll")} ({problems})</button>}
      </div>
      <div
        className="console" ref={box} role="log" aria-live="polite"
        onScroll={(e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24; }}
      >
        {lines.length === 0 && <div className="console-empty">{t("consoleEmpty")}</div>}
        {lines.map((e) => (
          <div key={e.id} className={`cl cl-${e.level}`}>
            <span className="cl-time">{new Date(e.ts).toLocaleTimeString()}</span>
            <span className="cl-lvl">{LEVEL_TAG[e.level]}</span>
            <span className="cl-src">{e.source}</span>
            <span className="cl-msg">{eventText(t, e)}</span>
          </div>
        ))}
      </div>
    </section>
  );
}
