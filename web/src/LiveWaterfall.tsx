import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { COLORMAP_NAMES, colormapLut } from "./colormaps";

interface Header { bins: number; sample_rate_hz: number; center_freq_hz: number; db_lo: number; db_hi: number; label: string }

const HEIGHT = 260;

/** Élő waterfall a felvétel közben: a szerver bájtos spektrumsorokat küld WebSocketen, a vászon lefelé görgetve rajzol. */
export default function LiveWaterfall() {
  const { t } = useTranslation();
  const canvas = useRef<HTMLCanvasElement>(null);
  const header = useRef<Header | null>(null);
  const [info, setInfo] = useState<Header | null>(null);
  const [center, setCenter] = useState<number>();
  const [status, setStatus] = useState<"connecting" | "waiting" | "live" | "ended">("connecting");
  const [range, setRange] = useState({ lo: -95, hi: -35 });
  const [cmap, setCmap] = useState("viridis");

  // a kezelők a legfrissebb értékeket látják, anélkül hogy újra kellene nyitni a kapcsolatot
  const view = useRef({ lo: -95, hi: -35, lut: colormapLut("viridis") });
  useEffect(() => { view.current.lo = range.lo; view.current.hi = range.hi; }, [range]);
  useEffect(() => { view.current.lut = colormapLut(cmap); }, [cmap]);

  useEffect(() => {
    let ws: WebSocket | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let closed = false;

    const drawRows = (data: Uint8Array) => {
      const h = header.current, cv = canvas.current;
      if (!h || !cv) return;
      const ctx = cv.getContext("2d")!;
      const rows = Math.floor(data.length / h.bins);
      for (let r = 0; r < rows; r++) {
        const { lo, hi, lut } = view.current;
        const row = ctx.createImageData(h.bins, 1);
        for (let x = 0; x < h.bins; x++) {
          const db = h.db_lo + (data[r * h.bins + x] / 255) * (h.db_hi - h.db_lo);
          const idx = Math.max(0, Math.min(255, Math.round(((db - lo) / (hi - lo)) * 255))) * 3;
          row.data[x * 4] = lut[idx]; row.data[x * 4 + 1] = lut[idx + 1]; row.data[x * 4 + 2] = lut[idx + 2]; row.data[x * 4 + 3] = 255;
        }
        ctx.drawImage(cv, 0, 0, h.bins, HEIGHT - 1, 0, 1, h.bins, HEIGHT - 1);   // az eddigi kép egy sorral lejjebb
        ctx.putImageData(row, 0, 0);
      }
    };

    const connect = () => {
      const token = localStorage.getItem("token");
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      ws = new WebSocket(`${proto}//${location.host}/api/live/waterfall${token ? `?token=${encodeURIComponent(token)}` : ""}`);
      ws.binaryType = "arraybuffer";
      setStatus("waiting");
      ws.onmessage = (m) => {
        if (typeof m.data === "string") {
          const msg = JSON.parse(m.data);
          if (msg.type === "header") {
            header.current = msg;
            setInfo(msg); setCenter(msg.center_freq_hz); setStatus("live");
            const cv = canvas.current;
            if (cv) { cv.width = msg.bins; cv.height = HEIGHT; cv.getContext("2d")!.clearRect(0, 0, cv.width, cv.height); }
          } else if (msg.type === "center") setCenter(msg.center_freq_hz);
          else if (msg.type === "end") { header.current = null; setStatus("ended"); }
        } else drawRows(new Uint8Array(m.data));
      };
      ws.onclose = () => { if (!closed) timer = setTimeout(connect, 3000); };   // szerver-újraindítás után újracsatlakozik
    };
    connect();
    return () => { closed = true; clearTimeout(timer); ws?.close(); };
  }, []);

  const half = info ? info.sample_rate_hz / 2 : 0;
  const mhz = (offset: number) => center === undefined ? "" : ((center + offset) / 1e6).toFixed(3);

  return (
    <div className="live">
      <div className="row">
        <strong>{t("liveWaterfall")}</strong>
        <span className={`muted ${status === "live" ? "live-on" : ""}`}>
          {status === "live" ? `● ${info?.label || t("live")}` : status === "ended" ? t("liveEnded") : t("liveWaiting")}
        </span>
        <span className="spacer" />
        <label>{t("minDb")}<input type="range" min={-120} max={-10} value={range.lo} onChange={(e) => setRange({ ...range, lo: Number(e.target.value) })} /></label>
        <label>{t("maxDb")}<input type="range" min={-110} max={0} value={range.hi} onChange={(e) => setRange({ ...range, hi: Number(e.target.value) })} /></label>
        <label>{t("colormap")}<select value={cmap} onChange={(e) => setCmap(e.target.value)}>{COLORMAP_NAMES.map((c) => <option key={c}>{c}</option>)}</select></label>
      </div>
      <canvas ref={canvas} width={1024} height={HEIGHT} className="live-canvas" />
      {info && (
        <div className="live-axis muted">
          <span>{mhz(-half)}</span><span>{mhz(0)} MHz</span><span>{mhz(half)}</span>
        </div>
      )}
    </div>
  );
}
