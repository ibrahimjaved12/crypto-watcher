import type { CollectorHealth, ForwardMinuteRow } from "../operational/types";
import {
  FINAL_STATUSES,
  type FundingEvent,
  validateForwardResponse,
  type ForwardEvaluateResponse,
  type ForwardResolution,
  type WalletState,
} from "./forward-contract";

/**
 * Forward-test orchestration (#239 P11). Every completed hour (and on demand) for the six frozen
 * symbols: read 1m collector candles from the operational store, call the stateless Python
 * `/v1/forward/evaluate`, validate the response, persist it idempotently (unique signal/setup ids,
 * append-only outcomes and ledger), and record one `paper_runs` row. Stale data (a gap within the
 * last STALE_GAP_WINDOW_MS, a missing last minute, or collector health not LIVE) records
 * `skipped_stale` and generates no signals. Older gaps are not stale: those minutes reach Python
 * as missing bars (MISSING, compromised exactly as in the benchmark). Dependencies are injected so
 * the logic is testable without services.
 *
 * History (P16): the ewma-robust-hcal sigma needs about 120 days of 1m candles (60 days of
 * completed horizon windows after the 28-day slot-factor and level warm-ups). Hourly (job) runs
 * repair the stored history from public REST klines before reading again (`backfillMinutes`): the
 * range before the first stored minute (from BACKFILL_DAYS) and every interior gap. Because the
 * ranges are derived from what is stored, an interrupted backfill (418/429, timeout) is resumed,
 * hole by hole, by the next hourly run. On-demand runs never backfill (it can take minutes).
 * Backfill failures are recorded in the run's reason. Until enough history exists Python emits no
 * setups and reports `no_sigma`.
 *
 * Candle versions (P15): each signal and setup is stored with the candle_version of its decision
 * candle (SHA-256 over the candle-v1 hashes of its 1m candles). Hashes are never sent to Python.
 */
export const FORWARD_SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT"] as const;
export const FORWARD_HISTORY_DAYS = 120;
/** P16: days of 1m history fetched from REST when the stored history starts too late. */
export const BACKFILL_DAYS = 130;
export const HOUR_MS = 3_600_000;
/**
 * Only gaps this recent make a run stale: the signals' decision candles (at most 4h) and the
 * recent sigma updates need them. Older gaps reach Python as missing bars.
 */
export const STALE_GAP_WINDOW_MS = 48 * 3_600_000;
/** Most backfill ranges attempted per symbol per run (each costs at least one REST page). */
export const MAX_BACKFILL_RANGES = 50;
const MINUTE_MS = 60_000;
const DAY_MS = 86_400_000;

export type StoredOpenSetup = { setup: Record<string, unknown>; resolution: ForwardResolution | null };
export type LatestRun = { processedToMs: Record<string, number>; walletState: WalletState | null };

export interface ForwardRepository {
  findRun(userId: string, runKey: string): Promise<{ id: string; status: string } | null>;
  latestOkRun(userId: string): Promise<LatestRun | null>;
  openSetups(userId: string): Promise<StoredOpenSetup[]>;
  insertRun(userId: string, row: Record<string, unknown>): Promise<string>;
  persistEvaluation(userId: string, runId: string, response: ForwardEvaluateResponse,
                    previous: Map<string, string | null>,
                    candleVersions?: Map<string, string | null>): Promise<Record<string, number>>;
}

export type ForwardDeps = {
  now(): number;
  readMinutes(symbol: string, sinceMs: number, beforeMs: number): Promise<ForwardMinuteRow[]>;
  listHealth(symbols: string[]): Promise<CollectorHealth[]>;
  callPython(body: unknown): Promise<unknown>;
  /** Public funding history from `startMs` (Binance `/fapi/v1/fundingRate`, limit 1000). */
  fetchFunding(symbol: string, startMs: number): Promise<FundingEvent[]>;
  /** P16: fetch and record 1m REST candles for [startMs, endMs); returns candles offered. */
  backfillMinutes?(symbol: string, startMs: number, endMs: number): Promise<number>;
  repository: ForwardRepository;
};

export type ForwardRunInput = {
  userId: string;
  trigger: "hourly" | "on_demand";
  strategyIds: string[];
  symbols?: readonly string[];
};

export type ForwardRunSummary = {
  runKey: string;
  status: "ok" | "skipped_stale" | "no_sigma" | "already_done";
  reason?: string;
  counts?: Record<string, number>;
};

/**
 * Funding events per symbol, fetched once per run (the cache is per run, never across runs). A
 * failed fetch yields `null`: Python then leaves every window containing a possible funding time
 * open instead of finalising it with zero funding.
 */
export async function fetchRunFunding(
  deps: Pick<ForwardDeps, "fetchFunding">,
  symbols: readonly string[],
  startMs: number,
  cache: Map<string, Promise<FundingEvent[] | null>> = new Map(),
): Promise<Map<string, FundingEvent[] | null>> {
  const out = new Map<string, FundingEvent[] | null>();
  for (const symbol of symbols) {
    if (!cache.has(symbol)) cache.set(symbol, deps.fetchFunding(symbol, startMs).catch(() => null));
    out.set(symbol, await cache.get(symbol)!);
  }
  return out;
}

/** Stale reasons per symbol; empty when every symbol is fresh. */
export function staleness(
  symbols: readonly string[],
  rows: Map<string, ForwardMinuteRow[]>,
  health: CollectorHealth[],
  boundaryMs: number,
): string[] {
  const reasons: string[] = [];
  for (const symbol of symbols) {
    const minute = health.find((item) => item.symbol === symbol && item.timeframe_minutes === 1);
    if (!minute || minute.status !== "LIVE") reasons.push(`${symbol}: collector ${minute?.status ?? "missing"}`);
    const series = rows.get(symbol) ?? [];
    if (series.length === 0) {
      reasons.push(`${symbol}: no candles`);
      continue;
    }
    if (series[series.length - 1]!.open_time_ms !== boundaryMs - MINUTE_MS) {
      reasons.push(`${symbol}: last completed minute missing`);
    }
    const recentFrom = boundaryMs - STALE_GAP_WINDOW_MS;
    let gaps = 0;
    for (let i = 1; i < series.length; i++) {
      // A gap counts when any of its missing minutes is recent (the later candle is past recentFrom).
      if (series[i]!.open_time_ms > recentFrom && series[i]!.open_time_ms - series[i - 1]!.open_time_ms !== MINUTE_MS) gaps++;
    }
    if (gaps) reasons.push(`${symbol}: ${gaps} gap(s) in the last ${STALE_GAP_WINDOW_MS / 3_600_000}h of candles`);
  }
  return reasons;
}

export async function runForward(deps: ForwardDeps, input: ForwardRunInput): Promise<ForwardRunSummary> {
  const symbols = input.symbols ?? FORWARD_SYMBOLS;
  const boundaryMs = Math.floor(deps.now() / HOUR_MS) * HOUR_MS;
  const runKey = `hour:${boundaryMs}`;
  const { repository } = deps;
  const claimed = await repository.findRun(input.userId, runKey);
  if (claimed && !["skipped_stale", "no_sigma"].includes(claimed.status)) return { runKey, status: "already_done" };

  const sinceMs = boundaryMs - FORWARD_HISTORY_DAYS * DAY_MS;
  const rows = new Map<string, ForwardMinuteRow[]>();
  const backfillErrors: string[] = [];
  for (const symbol of symbols) {
    const repaired = await repairSymbolHistory(deps, symbol, sinceMs, boundaryMs, input.trigger === "hourly");
    if (repaired.error) backfillErrors.push(repaired.error);
    const series = repaired.series;
    rows.set(symbol, series);
  }
  const backfillReason = backfillErrors.length ? `backfill_failed: ${backfillErrors.join(", ")}` : null;
  const health = await deps.listHealth([...symbols]);
  const freshness: Record<string, unknown> = Object.fromEntries(
    symbols.map((symbol) => {
      const series = rows.get(symbol) ?? [];
      return [symbol, { rows: series.length, last_open_ms: series[series.length - 1]?.open_time_ms ?? null,
        missing_minutes: missingMinutes(series, sinceMs, boundaryMs) }];
    }),
  );
  const stale = staleness(symbols, rows, health, boundaryMs);
  if (stale.length) {
    const reason = [...stale, ...(backfillReason ? [backfillReason] : [])].join("; ");
    await repository.insertRun(input.userId, { run_key: runKey, trigger: input.trigger, status: "skipped_stale",
      reason: reason.slice(0, 2000), boundary_ms: boundaryMs, freshness });
    return { runKey, status: "skipped_stale", reason };
  }

  const latest = await repository.latestOkRun(input.userId);
  const previousTo = latest ? Math.min(...symbols.map((s) => latest.processedToMs[s] ?? boundaryMs - HOUR_MS))
    : boundaryMs - HOUR_MS;
  const fromMs = Math.min(previousTo, boundaryMs - MINUTE_MS);
  const open = await repository.openSetups(input.userId);
  const oldestEntry = Math.min(fromMs, ...open.map((item) => Number(item.setup["entry_ms"])).filter(Number.isFinite));
  const funding = await fetchRunFunding(deps, symbols, oldestEntry - HOUR_MS);
  const fundingUnavailable = symbols.filter((symbol) => funding.get(symbol) === null);
  // Request size/latency of the full-history request (P16), kept so it can be judged before hosting.
  const requestRows = symbols.reduce((sum, symbol) => sum + (rows.get(symbol)?.length ?? 0), 0);
  const pythonStarted = Date.now();
  const response = validateForwardResponse(await deps.callPython({
    schema_version: 1,
    symbols: symbols.map((symbol) => ({
      symbol,
      rows: (rows.get(symbol) ?? []).map(({ candle_hash: _hash, ...row }) => row),
      funding: funding.get(symbol) ?? [],
      funding_available: funding.get(symbol) !== null,
    })),
    strategy_ids: input.strategyIds,
    from_ms: fromMs,
    to_ms: boundaryMs - MINUTE_MS,
    open_setups: open,
    wallet_state: latest?.walletState ?? null,
  }));
  freshness["request"] = { rows: requestRows, python_ms: Date.now() - pythonStarted };
  const reason = [
    ...(fundingUnavailable.length ? [`funding_unavailable: ${fundingUnavailable.join(",")}`] : []),
    ...(backfillReason ? [backfillReason] : []),
  ].join("; ") || null;
  const previous = new Map<string, string | null>(
    open.map((item): [string, string | null] => [String(item.setup["setup_id"]), item.resolution?.status ?? null]),
  );
  // Only wholly unevaluated runs are retryable. Healthy zero-signal runs and any
  // setup, resolution or wallet event still consume the hour key.
  const noSigma = symbols.length > 0 && symbols.every((symbol) =>
    String(response.reasons[symbol]?.["sigma"] ?? "").startsWith("no_sigma")) &&
    response.setups.length === 0 && response.resolutions.length === 0 && response.ledger.length === 0;
  const status = noSigma ? "no_sigma" : "ok";
  const runId = await repository.insertRun(input.userId, {
    run_key: runKey, trigger: input.trigger, status, boundary_ms: boundaryMs, from_ms: fromMs,
    reason: reason?.slice(0, 2000) ?? null,
    processed_to_ms: noSigma ? {} : response.processed_to_ms, versions: response.versions, params_hash: response.params_hash,
    wallet_config: response.wallet_config, wallet_state: response.wallet_state,
    assumptions: response.assumptions, reasons: response.reasons, freshness,
  });
  const candleVersions = await decisionCandleVersions(response, rows);
  const counts = await repository.persistEvaluation(input.userId, runId, response, previous, candleVersions);
  return reason ? { runKey, status, counts, reason } : { runKey, status, counts };
}

/**
 * Reads one symbol's stored 1m history and, when `backfill` is true, repairs it first from the
 * ranges `backfillRanges` derives (leading range and interior gaps). A failure stops this symbol's
 * repair and is returned (the next run resumes from what is stored); pages recorded before it are
 * kept and re-read. Used by the hourly run and by `forward-run --backfill-only`.
 */
export async function repairSymbolHistory(
  deps: Pick<ForwardDeps, "readMinutes" | "backfillMinutes">,
  symbol: string,
  sinceMs: number,
  boundaryMs: number,
  backfill: boolean,
): Promise<{ series: ForwardMinuteRow[]; error: string | null }> {
  let series = await deps.readMinutes(symbol, sinceMs, boundaryMs);
  const ranges = deps.backfillMinutes && backfill
    ? backfillRanges(series, sinceMs, boundaryMs - BACKFILL_DAYS * DAY_MS, boundaryMs)
    : [];
  let error: string | null = null;
  if (ranges.length && deps.backfillMinutes) {
    try {
      for (const range of ranges.slice(0, MAX_BACKFILL_RANGES)) {
        await deps.backfillMinutes(symbol, range.startMs, range.endMs);
      }
    } catch (failure) {
      error = `${symbol}: ${failure instanceof Error ? failure.message : String(failure)}`.slice(0, 200);
    }
    series = await deps.readMinutes(symbol, sinceMs, boundaryMs);
  }
  return { series, error };
}

/**
 * Ranges [startMs, endMs) to backfill, oldest first: from `leadingFromMs` to the first stored minute
 * when the history starts after `requiredFromMs` (or is empty), then every interior gap. Minutes
 * after the last stored one are left to the live collector (staleness reports them). Deriving the
 * ranges from what is stored makes an interrupted backfill resumable: pages recorded before a
 * failure leave a hole between them and the newer history, which the next run sees as a gap.
 * A minute Binance itself never published stays a gap and costs one page per hourly run.
 */
export function backfillRanges(
  rows: { open_time_ms: number }[],
  requiredFromMs: number,
  leadingFromMs: number,
  boundaryMs: number,
): { startMs: number; endMs: number }[] {
  const out: { startMs: number; endMs: number }[] = [];
  const first = rows[0]?.open_time_ms ?? boundaryMs;
  if (first > requiredFromMs) out.push({ startMs: Math.min(leadingFromMs, requiredFromMs), endMs: first });
  for (let i = 1; i < rows.length; i++) {
    const previous = rows[i - 1]!.open_time_ms;
    if (rows[i]!.open_time_ms - previous > MINUTE_MS) {
      out.push({ startMs: previous + MINUTE_MS, endMs: rows[i]!.open_time_ms });
    }
  }
  return out;
}

/** Minutes in [sinceMs, boundaryMs) without a stored candle (they reach Python as MISSING). */
export function missingMinutes(rows: { open_time_ms: number }[], sinceMs: number, boundaryMs: number): number {
  const expected = Math.max(0, Math.floor((boundaryMs - Math.ceil(sinceMs / MINUTE_MS) * MINUTE_MS) / MINUTE_MS));
  return Math.max(0, expected - rows.length);
}

/** Strategy timeframe of a forward strategy id (`name:minutes`, `placebo-v1:name:minutes`). */
export function decisionMinutes(strategyId: string, horizonMin: number): number {
  const match = /:(\d+)$/.exec(strategyId);
  return match ? Number(match[1]) : horizonMin;
}

async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

/**
 * candle_version per signal id (P15): SHA-256 of `candle-version-v1|<symbol>|<minutes>|` plus the
 * comma-joined candle-v1 hashes of the 1m candles in [signal_ms - minutes, signal_ms), oldest
 * first; null when any of those candles (or its hash) is missing. Setups inherit their signal's.
 */
export async function decisionCandleVersions(
  response: Pick<ForwardEvaluateResponse, "signals" | "setups">,
  rows: Map<string, ForwardMinuteRow[]>,
): Promise<Map<string, string | null>> {
  const index = new Map<string, Map<number, string | undefined>>();
  for (const [symbol, series] of rows)
    index.set(symbol, new Map(series.map((row) => [row.open_time_ms, row.candle_hash])));
  const out = new Map<string, string | null>();
  for (const signal of response.signals) {
    const minutes = decisionMinutes(signal.strategy_id, signal.horizon_min);
    const byOpen = index.get(signal.symbol);
    const hashes: string[] = [];
    for (let open = signal.signal_ms - minutes * MINUTE_MS; open < signal.signal_ms; open += MINUTE_MS) {
      const hash = byOpen?.get(open);
      if (!hash) break;
      hashes.push(hash);
    }
    out.set(signal.signal_id, hashes.length === minutes
      ? await sha256Hex(`candle-version-v1|${signal.symbol}|${minutes}|${hashes.join(",")}`)
      : null);
  }
  return out;
}

/** Outcome rows worth appending: a status not recorded before for that setup. */
export function newOutcomeRows(response: ForwardEvaluateResponse, previous: Map<string, string | null>) {
  return response.resolutions
    .filter((resolution) => previous.get(resolution.setup_id) !== resolution.status)
    .map((resolution) => ({
      setup_id: resolution.setup_id,
      status: resolution.status,
      final: FINAL_STATUSES.has(resolution.status),
      resolved_through_ms: resolution.resolved_through_ms,
      exit_ms: resolution.exit_ms,
      net_ur: resolution.net_ur,
      cost_ur: resolution.cost_ur,
      fund_ur: resolution.fund_ur,
      payload: resolution,
    }));
}
