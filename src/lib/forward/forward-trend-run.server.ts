import type { FundingEvent } from "./forward-contract";
import { FORWARD_SYMBOLS } from "./forward-run.server";
import {
  DAY_MS,
  validateTrendResponse,
  type DailyBar,
  type ForwardTrendResponse,
  type StoredTrendDecision,
  type TrendState,
} from "./forward-trend-contract";

export const TREND_TRACK_START_MS = Date.UTC(2026, 9, 1);
// Must match benchmark.trend.WARMUP_FIRST_MONTH. A finite warm-up changes path-dependent weights.
export const TREND_HISTORY_START_MS = Date.UTC(2020, 0, 1);
const FUNDING_PAGE = 1000;
export const FUNDING_INTERVAL_MS = 8 * 3_600_000;

export type TrendSnapshot = {
  id: string;
  params_hash: string;
  symbols: string[];
  history_start_ms: number;
  track_start_ms: number;
  states: Record<string, TrendState>;
};

export interface TrendRepository {
  findOkRun(userId: string, runKey: string): Promise<boolean>;
  latestSnapshot(userId: string): Promise<TrendSnapshot | null>;
  readDecisions(userId: string, fromMs: number): Promise<StoredTrendDecision[]>;
  commitRun(
    userId: string,
    expectedStateId: string | null,
    response: ForwardTrendResponse,
    row: Record<string, unknown>,
  ): Promise<Record<string, number>>;
  recordDiagnostic(userId: string, row: Record<string, unknown>): Promise<void>;
}

export type TrendDeps = {
  now(): number;
  readDailyBars(symbol: string, sinceMs: number): Promise<DailyBar[]>;
  recordDailyBars(bars: DailyBar[], receivedAtMs: number): Promise<number>;
  fetchDailyKlines(symbol: string, startMs: number, nowMs: number): Promise<DailyBar[]>;
  fetchFunding(symbol: string, startMs: number): Promise<FundingEvent[]>;
  fetchFundingIntervals(): Promise<Record<string, number>>;
  callPython(body: unknown): Promise<unknown>;
  repository: TrendRepository;
};

export type TrendRunInput = {
  userId: string;
  trigger: "daily" | "on_demand";
  symbols?: readonly string[];
};
export type TrendRunSummary = {
  runKey: string;
  status: "ok" | "partial" | "already_done";
  reason?: string;
  counts?: Record<string, number>;
};

/** An initial run verifies the whole history from the canonical origin, even after a partial fetch.
 * Pre-listing days are missing, as in dk1. Only a successfully committed snapshot permits append.
 */
export async function updateDailyFeed(
  deps: Pick<TrendDeps, "readDailyBars" | "recordDailyBars" | "fetchDailyKlines">,
  symbol: string,
  lastCompleteMs: number,
  nowMs: number,
  initialize = true,
): Promise<{ bars: DailyBar[]; inserted: number }> {
  const stored = await deps.readDailyBars(symbol, TREND_HISTORY_START_MS);
  const bars = new Map(stored.map((bar) => [bar.day_ms, bar]));
  let cursor = initialize
    ? TREND_HISTORY_START_MS
    : (stored.at(-1)?.day_ms ?? TREND_HISTORY_START_MS - DAY_MS) + DAY_MS;
  let inserted = 0;
  while (cursor <= lastCompleteMs) {
    const fetched = await deps.fetchDailyKlines(symbol, cursor, nowMs);
    const fresh = fetched.filter((bar) => bar.day_ms >= cursor && bar.day_ms <= lastCompleteMs);
    if (!fresh.length) throw new Error("Daily history does not reach the required boundary");
    inserted += await deps.recordDailyBars(fresh, nowMs);
    for (const bar of fresh) bars.set(bar.day_ms, bar);
    cursor = fresh.at(-1)!.day_ms + DAY_MS;
  }
  return { bars: [...bars.values()].sort((a, b) => a.day_ms - b.day_ms), inserted };
}

/** Paginate until closing midnight is observed. A short page is never evidence of coverage.
 * The verified schedule comes from fundingInfo (standard 8h for unadjusted symbols).
 * Missing or off-grid settlements stay unavailable;
 * an interval change requires a separately verified schedule, rather than invented zero funding.
 */
export async function fetchTrendFunding(
  deps: Pick<TrendDeps, "fetchFunding">,
  symbol: string,
  fromMs: number,
  throughMs: number,
  intervalMs = FUNDING_INTERVAL_MS,
): Promise<{ events: FundingEvent[]; toMs: number | null }> {
  // A resumed checkpoint can already cover the boundary. No day is finalized from this empty span.
  if (throughMs === fromMs) return { events: [], toMs: fromMs };
  const events: FundingEvent[] = [];
  let cursor = fromMs + 1;
  while (cursor <= throughMs) {
    const page = await deps.fetchFunding(symbol, cursor);
    const fresh = page.filter(
      (event) => event.calc_time_ms >= cursor && event.calc_time_ms <= throughMs,
    );
    events.push(...fresh);
    if (!fresh.length || fresh.at(-1)!.calc_time_ms >= throughMs || page.length < FUNDING_PAGE)
      break;
    cursor = fresh.at(-1)!.calc_time_ms + 1;
  }
  if (
    !Number.isInteger(intervalMs) ||
    intervalMs <= 0 ||
    intervalMs % 3_600_000 !== 0 ||
    DAY_MS % intervalMs !== 0
  ) {
    throw new Error("Unverified funding interval");
  }
  let expected = fromMs + intervalMs;
  for (const event of events) {
    if (event.calc_time_ms !== expected) break;
    expected += intervalMs;
  }
  const lastVerified = expected - intervalMs;
  const toMs = lastVerified > fromMs ? Math.floor(lastVerified / DAY_MS) * DAY_MS : null;
  return { events, toMs: toMs !== null && toMs > fromMs ? toMs : null };
}

export async function runForwardTrend(
  deps: TrendDeps,
  input: TrendRunInput,
): Promise<TrendRunSummary> {
  const symbols = input.symbols ?? FORWARD_SYMBOLS;
  const now = deps.now();
  const lastCompleteMs = Math.floor(now / DAY_MS) * DAY_MS - DAY_MS;
  const runKey = `day:${lastCompleteMs}`;
  const { repository } = deps;
  const snapshot = await repository.latestSnapshot(input.userId);
  if (
    snapshot &&
    (JSON.stringify(snapshot.symbols) !== JSON.stringify(symbols) ||
      snapshot.history_start_ms !== TREND_HISTORY_START_MS ||
      snapshot.track_start_ms !== TREND_TRACK_START_MS)
  ) {
    throw new Error(
      "Saved trend configuration differs; reset the empty database before starting this track",
    );
  }
  const reasons: string[] = [];
  const bars = new Map<string, DailyBar[]>();
  const freshness: Record<string, unknown> = {};
  let inserted = 0;
  let startupIncomplete = lastCompleteMs < TREND_TRACK_START_MS - DAY_MS;
  for (const symbol of symbols) {
    try {
      const feed = await updateDailyFeed(deps, symbol, lastCompleteMs, now, !snapshot);
      bars.set(symbol, feed.bars);
      inserted += feed.inserted;
    } catch {
      bars.set(symbol, await deps.readDailyBars(symbol, TREND_HISTORY_START_MS));
      reasons.push(`${symbol}: daily kline fetch failed or history incomplete`);
      if (!snapshot) startupIncomplete = true;
    }
    const series = bars.get(symbol)!;
    freshness[symbol] = { rows: series.length, last_day_ms: series.at(-1)?.day_ms ?? null };
    if (!series.length || series.at(-1)!.day_ms < TREND_TRACK_START_MS - DAY_MS)
      startupIncomplete = true;
  }
  if (startupIncomplete) {
    const reason = [...reasons, "incomplete startup: complete frozen universe history required"]
      .join("; ")
      .slice(0, 2000);
    const key = `${runKey}:partial:${now}`;
    const counts = { bars_inserted: inserted, ledger: 0, weights: 0 };
    await repository.recordDiagnostic(input.userId, {
      run_key: key,
      trigger: input.trigger,
      status: "partial",
      reason,
      last_complete_day_ms: lastCompleteMs,
      through_day_ms: null,
      freshness,
      counts,
      states: {},
      symbols: [...symbols],
      history_start_ms: TREND_HISTORY_START_MS,
      track_start_ms: TREND_TRACK_START_MS,
    });
    return { runKey: key, status: "partial", reason, counts };
  }
  const throughMs = Math.min(
    lastCompleteMs,
    ...symbols.map((symbol) => bars.get(symbol)!.at(-1)!.day_ms),
  );
  if (throughMs < lastCompleteMs) reasons.push("daily bars not yet complete for every symbol");
  const states = snapshot?.states ?? null;
  const nextDay =
    states && Object.keys(states).length
      ? Math.min(
          ...Object.values(states).map(
            (state) => (state.last_day_ms ?? TREND_TRACK_START_MS - DAY_MS) + DAY_MS,
          ),
        )
      : TREND_TRACK_START_MS;
  const decisions = await repository.readDecisions(input.userId, nextDay);
  let intervals: Record<string, number> | null = null;
  try {
    intervals = await deps.fetchFundingIntervals();
  } catch {
    reasons.push("funding settlement schedule unavailable");
  }
  const funding = new Map<
    string,
    { events: FundingEvent[]; toMs: number | null; intervalMs: number }
  >();
  for (const symbol of symbols) {
    const intervalMs = intervals?.[symbol] ?? FUNDING_INTERVAL_MS;
    try {
      if (!intervals) throw new Error("Funding settlement schedule unavailable");
      funding.set(symbol, {
        ...(await fetchTrendFunding(deps, symbol, nextDay, throughMs + DAY_MS, intervalMs)),
        intervalMs,
      });
    } catch {
      funding.set(symbol, { events: [], toMs: null, intervalMs });
    }
  }
  const response = validateTrendResponse(
    await deps.callPython({
      schema_version: 1,
      symbols: symbols.map((symbol) => {
        const coverage = funding.get(symbol)!;
        return {
          symbol,
          bars: bars
            .get(symbol)!
            .map(({ day_ms, open, high, low, close, volume, quote_volume }) => ({
              day_ms,
              open,
              high,
              low,
              close,
              volume,
              quote_volume,
            })),
          funding: coverage.events,
          funding_available: coverage.toMs !== null,
          funding_from_ms: nextDay,
          funding_to_ms: coverage.toMs,
          funding_interval_ms: coverage.intervalMs,
        };
      }),
      through_day_ms: throughMs,
      states,
      saved_params_hash: snapshot?.params_hash ?? null,
      decisions,
      expected_symbols: [...symbols],
      history_start_ms: TREND_HISTORY_START_MS,
      track_start_ms: TREND_TRACK_START_MS,
    }),
  );
  if (snapshot && snapshot.params_hash !== response.params_hash)
    throw new Error("Saved trend parameter hash differs");
  if (
    JSON.stringify(response.symbols) !== JSON.stringify(symbols) ||
    response.history_start_ms !== TREND_HISTORY_START_MS ||
    response.track_start_ms !== TREND_TRACK_START_MS
  )
    throw new Error("Python returned another trend configuration");
  if (!Object.keys(response.states).length)
    throw new Error("Python refused complete-universe initialization");
  // Even a completed run verifies its saved identity with the current Python strategy before returning.
  if (await repository.findOkRun(input.userId, runKey)) return { runKey, status: "already_done" };
  if (response.funding_unavailable.length)
    reasons.push(`funding_unavailable: ${response.funding_unavailable.join(",")}`);
  for (const reason of Object.values(response.reasons))
    if (!reasons.includes(reason)) reasons.push(reason);
  const complete = !reasons.length && response.through_day_ms === lastCompleteMs;
  const key = complete ? runKey : `${runKey}:partial:${now}`;
  const reason =
    reasons.join("; ").slice(0, 2000) || (complete ? undefined : "not every track is finalised");
  const counts = await repository.commitRun(input.userId, snapshot?.id ?? null, response, {
    run_key: key,
    trigger: input.trigger,
    status: complete ? "ok" : "partial",
    reason: reason ?? null,
    last_complete_day_ms: lastCompleteMs,
    freshness,
    counts: { bars_inserted: inserted },
  });
  return {
    runKey: key,
    status: complete ? "ok" : "partial",
    counts,
    ...(reason ? { reason } : {}),
  };
}

export function msUntilNextDailyRun(now: number): number {
  const today = Math.floor(now / DAY_MS) * DAY_MS + 5 * 60_000;
  return (now < today ? today : today + DAY_MS) - now;
}
