import type { FundingEvent } from "./forward-contract";
import { fetchRunFunding, FORWARD_SYMBOLS } from "./forward-run.server";
import {
  DAY_MS,
  validateTrendResponse,
  type DailyBar,
  type ForwardTrendResponse,
  type TrendState,
} from "./forward-trend-contract";

/**
 * Forward trend track orchestration (#239 P14, #222). Once per day shortly after 00:05 UTC (and on
 * demand) for the six frozen symbols:
 *
 * 1. Daily feed: append the completed UTC days since the last stored one to the operational
 *    `forward_daily_bars` (the first run backfills from TREND_HISTORY_START_MS, >= 400 days before the
 *    track starts, for the 360-day lookback). The running day is never stored; gaps stay missing.
 * 2. Funding history from the first day not yet finalised (public `/fapi/v1/fundingRate`, once per
 *    symbol per run). A failed fetch finalises nothing (`funding_unavailable`), never zeros.
 * 3. The stateless Python `/v1/forward/trend` with every stored bar since the fixed history start and
 *    the latest states; the response is validated, then ledger and weight rows are inserted
 *    (idempotent per (track, day)) BEFORE the run's state row, so a state never claims a missing day.
 *
 * Status `ok` (run key `day:<last completed day>`) means every track is finalised through the last
 * completed day; otherwise the run is `partial` with its reasons and a later run continues.
 */
export const TREND_TRACK_START_MS = Date.UTC(2026, 9, 1);  // forward live from 2026-10 (owner decision)
export const TREND_BACKFILL_DAYS = 420;  // >= 400 completed days before the first tracked day
// Fixed history start: every run sends the same first day, so the band-dependent weight paths are
// identical from run to run (a moving window would change them).
export const TREND_HISTORY_START_MS = TREND_TRACK_START_MS - TREND_BACKFILL_DAYS * DAY_MS;
const KLINE_PAGE = 1500;
const FUNDING_PAGE = 1000;

export interface TrendRepository {
  findOkRun(userId: string, runKey: string): Promise<boolean>;
  latestStates(userId: string): Promise<Record<string, TrendState> | null>;
  persistRows(userId: string, runKey: string, response: ForwardTrendResponse): Promise<Record<string, number>>;
  insertState(userId: string, row: Record<string, unknown>): Promise<void>;
}

export type TrendDeps = {
  now(): number;
  readDailyBars(symbol: string, sinceMs: number): Promise<DailyBar[]>;
  recordDailyBars(bars: DailyBar[], receivedAtMs: number): Promise<number>;
  /** Completed daily klines from `startMs` (at most 1500; the running day already dropped). */
  fetchDailyKlines(symbol: string, startMs: number, nowMs: number): Promise<DailyBar[]>;
  fetchFunding(symbol: string, startMs: number): Promise<FundingEvent[]>;
  callPython(body: unknown): Promise<unknown>;
  repository: TrendRepository;
};

export type TrendRunInput = { userId: string; trigger: "daily" | "on_demand"; symbols?: readonly string[] };

export type TrendRunSummary = {
  runKey: string;
  status: "ok" | "partial" | "already_done";
  reason?: string;
  counts?: Record<string, number>;
};

/** Brings one symbol's stored feed up to the last completed day; returns the stored bars and new rows. */
export async function updateDailyFeed(
  deps: Pick<TrendDeps, "readDailyBars" | "recordDailyBars" | "fetchDailyKlines">,
  symbol: string,
  lastCompleteMs: number,
  nowMs: number,
): Promise<{ bars: DailyBar[]; inserted: number }> {
  let bars = await deps.readDailyBars(symbol, TREND_HISTORY_START_MS);
  let inserted = 0;
  for (let page = 0; page < 10; page++) {
    const last = bars.length ? bars[bars.length - 1]!.day_ms : TREND_HISTORY_START_MS - DAY_MS;
    if (last >= lastCompleteMs) break;
    const fetched = await deps.fetchDailyKlines(symbol, last + DAY_MS, nowMs);
    const fresh = fetched.filter((bar) => bar.day_ms > last && bar.day_ms + DAY_MS <= nowMs);
    if (fresh.length) {
      inserted += await deps.recordDailyBars(fresh, nowMs);
      bars = [...bars, ...fresh];
    }
    if (fetched.length < KLINE_PAGE - 1) break;  // the last page (the running day was dropped)
  }
  return { bars, inserted };
}

export async function runForwardTrend(deps: TrendDeps, input: TrendRunInput): Promise<TrendRunSummary> {
  const symbols = input.symbols ?? FORWARD_SYMBOLS;
  const now = deps.now();
  const lastCompleteMs = Math.floor(now / DAY_MS) * DAY_MS - DAY_MS;
  const runKey = `day:${lastCompleteMs}`;
  const { repository } = deps;
  if (await repository.findOkRun(input.userId, runKey)) return { runKey, status: "already_done" };

  const reasons: string[] = [];
  const bars = new Map<string, DailyBar[]>();
  const freshness: Record<string, unknown> = {};
  let inserted = 0;
  for (const symbol of symbols) {
    try {
      const feed = await updateDailyFeed(deps, symbol, lastCompleteMs, now);
      bars.set(symbol, feed.bars);
      inserted += feed.inserted;
    } catch {
      bars.set(symbol, await deps.readDailyBars(symbol, TREND_HISTORY_START_MS));
      reasons.push(`${symbol}: daily kline fetch failed`);
    }
    const series = bars.get(symbol)!;
    freshness[symbol] = { rows: series.length, last_day_ms: series[series.length - 1]?.day_ms ?? null };
  }
  const missing = symbols.filter((symbol) => !bars.get(symbol)!.length);
  if (missing.length) reasons.push(`no daily bars: ${missing.join(",")}`);
  // Finalise only through the last day every symbol has stored: a symbol whose fetch lagged must not
  // turn into a permanently flat (MISSING) day.
  const throughMs = missing.length ? TREND_TRACK_START_MS - DAY_MS : Math.min(lastCompleteMs,
    ...symbols.map((symbol) => bars.get(symbol)![bars.get(symbol)!.length - 1]!.day_ms));
  if (throughMs < lastCompleteMs && !missing.length) reasons.push("daily bars not yet complete for every symbol");

  const states = await repository.latestStates(input.userId);
  const nextDay = states && Object.keys(states).length
    ? Math.min(...Object.values(states).map((state) => (state.last_day_ms ?? TREND_TRACK_START_MS - DAY_MS) + DAY_MS))
    : TREND_TRACK_START_MS;
  const funding = await fetchRunFunding(deps, symbols, nextDay);
  const response = validateTrendResponse(await deps.callPython({
    schema_version: 1,
    symbols: symbols.filter((symbol) => bars.get(symbol)!.length).map((symbol) => {
      const events = funding.get(symbol);
      return {
        symbol,
        bars: bars.get(symbol)!.map(({ day_ms, open, high, low, close, volume, quote_volume }) =>
          ({ day_ms, open, high, low, close, volume, quote_volume })),
        funding: events ?? [],
        funding_available: events !== null,
        funding_from_ms: nextDay,
        // A full page may stop short of now: only the span it covers counts as known.
        funding_to_ms: events && events.length >= FUNDING_PAGE ? events[events.length - 1]!.calc_time_ms : now,
      };
    }),
    through_day_ms: Math.max(0, throughMs),
    states,
    track_start_ms: TREND_TRACK_START_MS,
  }));
  if (response.funding_unavailable.length) reasons.push(`funding_unavailable: ${response.funding_unavailable.join(",")}`);
  const complete = !reasons.length && response.through_day_ms === lastCompleteMs;
  const key = complete ? runKey : `${runKey}:partial:${now}`;
  const counts = { bars_inserted: inserted, ...(await repository.persistRows(input.userId, key, response)) };
  const reason = reasons.join("; ").slice(0, 2000) || (complete ? undefined : "not every track is finalised");
  await repository.insertState(input.userId, {
    run_key: key, trigger: input.trigger, status: complete ? "ok" : "partial", reason: reason ?? null,
    last_complete_day_ms: lastCompleteMs, through_day_ms: response.through_day_ms, states: response.states,
    versions: response.versions, params_hash: response.params_hash, tracks: response.tracks,
    funding_unavailable: response.funding_unavailable, assumptions: response.assumptions, freshness, counts,
  });
  return { runKey: key, status: complete ? "ok" : "partial", counts, ...(reason ? { reason } : {}) };
}

/** Ms until the next daily run time (00:05 UTC). */
export function msUntilNextDailyRun(now: number): number {
  const today = Math.floor(now / DAY_MS) * DAY_MS + 5 * 60_000;
  return (now < today ? today : today + DAY_MS) - now;
}
