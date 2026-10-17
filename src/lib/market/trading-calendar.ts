/**
 * FX trading week for the UI (#374). Mirrors the default FxWeekCalendar in
 * python/market_analysis/trading_calendar.py: Sunday 17:00 to Friday 17:00 America/New_York.
 * Breaks and closures are Python-only for now. Shared by client and server; no node: imports.
 */
import type { Market } from "./instruments";

const FX_TZ = "America/New_York";
const OPEN_WEEKDAY = 6; // Sunday, Monday = 0 (Python weekday numbering)
const OPEN_LOCAL: readonly [number, number] = [17, 0];
const CLOSE_LOCAL: readonly [number, number] = [17, 0];
const SPAN_DAYS = 5; // Sunday to Friday
const DAY_MS = 86_400_000;

/** Conventional session hours in each centre's local time (not exchange-official). */
export const FX_SESSIONS: readonly (readonly [string, string, number, number])[] = [
  ["sydney", "Australia/Sydney", 7, 16],
  ["tokyo", "Asia/Tokyo", 9, 18],
  ["london", "Europe/London", 8, 17],
  ["new_york", "America/New_York", 8, 17],
];

const WEEKDAYS: Record<string, number> = { Mon: 0, Tue: 1, Wed: 2, Thu: 3, Fri: 4, Sat: 5, Sun: 6 };
const formatters = new Map<string, Intl.DateTimeFormat>();

type LocalParts = { year: number; month: number; day: number; hour: number; minute: number; second: number; weekday: number };

function checkMs(ms: number): number {
  if (!Number.isSafeInteger(ms)) throw new Error("ms must be an integer of UTC epoch milliseconds");
  return ms;
}

function formatter(timeZone: string): Intl.DateTimeFormat {
  let found = formatters.get(timeZone);
  if (!found) {
    found = new Intl.DateTimeFormat("en-US", {
      timeZone,
      hourCycle: "h23",
      year: "numeric",
      month: "numeric",
      day: "numeric",
      hour: "numeric",
      minute: "numeric",
      second: "numeric",
      weekday: "short",
    });
    formatters.set(timeZone, found);
  }
  return found;
}

function localParts(ms: number, timeZone: string): LocalParts {
  const parts: Record<string, string> = {};
  for (const part of formatter(timeZone).formatToParts(new Date(ms))) parts[part.type] = part.value;
  const weekday = WEEKDAYS[parts["weekday"] ?? ""];
  if (weekday === undefined) throw new Error(`unreadable weekday for ${timeZone}`);
  return {
    year: Number(parts["year"]),
    month: Number(parts["month"]),
    day: Number(parts["day"]),
    hour: Number(parts["hour"]) % 24,
    minute: Number(parts["minute"]),
    second: Number(parts["second"]),
    weekday,
  };
}

/** Local wall time minus UTC at the instant, in ms (whole seconds). */
function zoneOffsetMs(ms: number, timeZone: string): number {
  const p = localParts(ms, timeZone);
  const wall = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second);
  return wall - (Math.floor(ms / 1000) * 1000);
}

/** UTC ms of a local wall time; `dayUtc` is any instant on the local calendar date (as Date.UTC midnight). */
function localToUtc(dayUtc: number, [hour, minute]: readonly [number, number], timeZone: string): number {
  const d = new Date(dayUtc);
  const guess = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), hour, minute);
  const first = guess - zoneOffsetMs(guess, timeZone);
  return guess - zoneOffsetMs(first, timeZone);
}

function windowFrom(startDayUtc: number): [number, number] {
  return [
    localToUtc(startDayUtc, OPEN_LOCAL, FX_TZ),
    localToUtc(startDayUtc + SPAN_DAYS * DAY_MS, CLOSE_LOCAL, FX_TZ),
  ];
}

/** The [open, close) FX week containing `ms`, or the next one if `ms` is in the weekend gap. */
export function fxWeekWindow(ms: number): [number, number] {
  checkMs(ms);
  const p = localParts(ms, FX_TZ);
  const back = (((p.weekday - OPEN_WEEKDAY) % 7) + 7) % 7;
  let start = Date.UTC(p.year, p.month - 1, p.day) - back * DAY_MS;
  let [open, close] = windowFrom(start);
  if (open > ms) {
    start -= 7 * DAY_MS;
    [open, close] = windowFrom(start);
  }
  if (ms >= close) [open, close] = windowFrom(start + 7 * DAY_MS);
  return [open, close];
}

export function isFxOpen(ms: number): boolean {
  const [open, close] = fxWeekWindow(ms);
  return open <= ms && ms < close;
}

export function fxNextOpenMs(ms: number): number {
  return isFxOpen(ms) ? ms : fxWeekWindow(ms)[0];
}

/** Conventional sessions active at `ms`; empty while the FX market is closed. */
export function fxSessionTags(ms: number): string[] {
  if (!isFxOpen(ms)) return [];
  return FX_SESSIONS.filter(([, timeZone, start, end]) => {
    const hour = localParts(ms, timeZone).hour;
    return start <= hour && hour < end;
  }).map(([name]) => name);
}

export function isMarketOpen(market: Market, ms: number): boolean {
  checkMs(ms);
  return market === "crypto" ? true : isFxOpen(ms);
}
