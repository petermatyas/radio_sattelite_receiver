import type { TFunction } from "i18next";
import { useCallback, useEffect, useState } from "react";
import type { AppEvent } from "./api";

export function useAsync<T>(fn: () => Promise<T>) {
  const [data, setData] = useState<T | undefined>();
  const [error, setError] = useState<string | undefined>();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const reload = useCallback(() => {
    fn()
      .then((d) => { setData(d); setError(undefined); })
      .catch((e) => setError(String(e.message ?? e)));
  }, []);
  useEffect(reload, [reload]);
  return { data, error, reload };
}

export const fmt = (iso: string) => new Date(iso).toLocaleString();
export const mb = (bytes: number) => `${(bytes / 1e6).toFixed(1)} MB`;

/** Az esemény fordított szövege (a `code` alapján) a javasolt teendővel; fordítás hiányában az angol üzenet. */
export function eventText(t: TFunction, e: AppEvent): string {
  const main = t(`ev_${e.code}`, { defaultValue: e.message, ...e.details }) as string;
  const hint = t(`hint_${e.code}`, { defaultValue: "" }) as string;
  return hint ? `${main} – ${hint}` : main;
}

/** A `tz` időzóna falióra-eltolása (ms) az adott pillanatban. */
function zoneOffsetMs(utcMs: number, tz: string): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).formatToParts(new Date(utcMs));
  const v = (type: string) => Number(parts.find((p) => p.type === type)?.value);
  return Date.UTC(v("year"), v("month") - 1, v("day"), v("hour"), v("minute"), v("second")) - Math.floor(utcMs / 1000) * 1000;
}

/** "2026-10-12T18:00" (az adott időzóna falióráján) -> UTC ISO-idő. A nyári időszámításváltást is kezeli. */
export function zonedToIso(local: string, tz: string): string {
  const [d, tm] = local.split("T");
  const [y, mo, da] = d.split("-").map(Number);
  const [h, mi] = tm.split(":").map(Number);
  const wall = Date.UTC(y, mo - 1, da, h, mi);
  let guess = wall - zoneOffsetMs(wall, tz);
  guess = wall - zoneOffsetMs(guess, tz);
  return new Date(guess).toISOString();
}

/** UTC ISO-idő -> "2026-10-12T18:00" az adott időzóna falióráján (datetime-local mezőhöz). */
export function isoToZoned(iso: string, tz: string): string {
  const ms = new Date(iso).getTime();
  return new Date(ms + zoneOffsetMs(ms, tz)).toISOString().slice(0, 16);
}

export function fmtBytes(bytes: number): string {
  if (bytes < 1000) return `${bytes} B`;
  const units = ["kB", "MB", "GB", "TB"];
  let v = bytes / 1000, i = 0;
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i++; }
  return `${v.toFixed(v < 10 ? 2 : v < 100 ? 1 : 0)} ${units[i]}`;
}
