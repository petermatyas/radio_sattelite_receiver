import { createContext, ReactNode, useCallback, useContext, useEffect, useRef, useState } from "react";
import { api, AppEvent } from "./api";

export interface StreamState {
  time: string;
  recorder: {
    state: "idle" | "recording" | "stopping"; elapsed_s: number; duration_s: number; last_error: string | null;
    recording: { id: number; pass_id: number | null; satellite: string | null; center_freq_hz: number | null } | null;
  };
  next_pass: { pass_id: number; satellite: string; kind?: "pass" | "event"; rec_start: string; max_elevation_deg: number } | null;
  processing_jobs: number;
  unacknowledged: { error: number; warning: number; info: number };
  live_waterfall: boolean;
  sdr?: { backend: string; simulated: boolean };
}

export interface Toast { id: number; event: AppEvent }

interface StreamContextValue {
  state?: StreamState;
  connected: boolean;
  /** Minden beérkező eseménynél nő: az oldalak ebből tudják, mikor érdemes újratölteni az adataikat. */
  tick: number;
  /** A legutóbbi események időrendben (a főoldali konzolhoz): induláskor betöltve, utána az SSE-ből bővül. */
  events: AppEvent[];
  toasts: Toast[];
  dismiss: (id: number) => void;
}

const Ctx = createContext<StreamContextValue>({ connected: false, tick: 0, events: [], toasts: [], dismiss: () => {} });
export const useStream = () => useContext(Ctx);

/** Ezekről az info eseményekről is értesítünk (a hibák és figyelmeztetések mindig kapnak toastot). */
const MAX_EVENTS = 300;
const NOTIFY_INFO = new Set(["pipeline_done", "recording_finished"]);

export function StreamProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<StreamState>();
  const [connected, setConnected] = useState(false);
  const [tick, setTick] = useState(0);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [events, setEvents] = useState<AppEvent[]>([]);
  const nextId = useRef(1);

  const addEvents = useCallback((incoming: AppEvent[]) => setEvents((cur) => {
    const byId = new Map(cur.map((e) => [e.id, e]));
    for (const e of incoming) byId.set(e.id, e);
    return [...byId.values()].sort((a, b) => a.id - b.id).slice(-MAX_EVENTS);
  }), []);

  const dismiss = useCallback((id: number) => setToasts((t) => t.filter((x) => x.id !== id)), []);

  useEffect(() => {
    const token = localStorage.getItem("token");
    const es = new EventSource(`/api/stream${token ? `?token=${encodeURIComponent(token)}` : ""}`);
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);   // az EventSource magától újracsatlakozik
    es.addEventListener("state", (m) => { setConnected(true); setState(JSON.parse((m as MessageEvent).data)); });
    es.addEventListener("event", (m) => {
      const event: AppEvent = JSON.parse((m as MessageEvent).data);
      setTick((n) => n + 1);
      addEvents([event]);
      if (event.level === "info" && !NOTIFY_INFO.has(event.code)) return;
      const id = nextId.current++;
      setToasts((t) => [...t.slice(-4), { id, event }]);
      // az információk eltűnnek; a hibák és figyelmeztetések maradnak, amíg el nem tüntetik
      if (event.level === "info") setTimeout(() => dismiss(id), 6000);
      else if (event.level === "warning") setTimeout(() => dismiss(id), 20000);
    });
    api.events({ limit: 200 }).then(addEvents).catch(() => {});
    return () => es.close();
  }, [dismiss, addEvents]);

  return <Ctx.Provider value={{ state, connected, tick, events, toasts, dismiss }}>{children}</Ctx.Provider>;
}

/** Másodpercenként frissülő idő (visszaszámlálókhoz). */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

export function formatCountdown(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const mm = String(m).padStart(2, "0"), ss = String(sec).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
}
