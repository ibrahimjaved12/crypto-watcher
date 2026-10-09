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
 * append-only outcomes and ledger), and record one `paper_runs` row. Stale data (a gap in the
 * candle history, a missing last minute, or collector health not LIVE) records `skipped_stale`
 * and generates no signals. Dependencies are injected so the logic is testable without services.
 */
export const FORWARD_SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT"] as const;
export const FORWARD_HISTORY_DAYS = 60;
export const HOUR_MS = 3_600_000;
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
                    previous: Map<string, string | null>): Promise<Record<string, number>>;
}

export type ForwardDeps = {
  now(): number;
  readMinutes(symbol: string, sinceMs: number, beforeMs: number): Promise<ForwardMinuteRow[]>;
  listHealth(symbols: string[]): Promise<CollectorHealth[]>;
  callPython(body: unknown): Promise<unknown>;
  /** Public funding history from `startMs` (Binance `/fapi/v1/fundingRate`, limit 1000). */
  fetchFunding(symbol: string, startMs: number): Promise<FundingEvent[]>;
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
  status: "ok" | "skipped_stale" | "already_done";
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
    let gaps = 0;
    for (let i = 1; i < series.length; i++) {
      if (series[i]!.open_time_ms - series[i - 1]!.open_time_ms !== MINUTE_MS) gaps++;
    }
    if (gaps) reasons.push(`${symbol}: ${gaps} gap(s) in candle history`);
  }
  return reasons;
}

export async function runForward(deps: ForwardDeps, input: ForwardRunInput): Promise<ForwardRunSummary> {
  const symbols = input.symbols ?? FORWARD_SYMBOLS;
  const boundaryMs = Math.floor(deps.now() / HOUR_MS) * HOUR_MS;
  const runKey = `hour:${boundaryMs}`;
  const { repository } = deps;
  if (await repository.findRun(input.userId, runKey)) return { runKey, status: "already_done" };

  const sinceMs = boundaryMs - FORWARD_HISTORY_DAYS * DAY_MS;
  const rows = new Map<string, ForwardMinuteRow[]>();
  for (const symbol of symbols) rows.set(symbol, await deps.readMinutes(symbol, sinceMs, boundaryMs));
  const health = await deps.listHealth([...symbols]);
  const freshness = Object.fromEntries(
    symbols.map((symbol) => {
      const series = rows.get(symbol) ?? [];
      return [symbol, { rows: series.length, last_open_ms: series[series.length - 1]?.open_time_ms ?? null }];
    }),
  );
  const stale = staleness(symbols, rows, health, boundaryMs);
  if (stale.length) {
    await repository.insertRun(input.userId, { run_key: runKey, trigger: input.trigger, status: "skipped_stale",
      reason: stale.join("; ").slice(0, 2000), boundary_ms: boundaryMs, freshness });
    return { runKey, status: "skipped_stale", reason: stale.join("; ") };
  }

  const latest = await repository.latestOkRun(input.userId);
  const previousTo = latest ? Math.min(...symbols.map((s) => latest.processedToMs[s] ?? boundaryMs - HOUR_MS))
    : boundaryMs - HOUR_MS;
  const fromMs = Math.min(previousTo, boundaryMs - MINUTE_MS);
  const open = await repository.openSetups(input.userId);
  const oldestEntry = Math.min(fromMs, ...open.map((item) => Number(item.setup["entry_ms"])).filter(Number.isFinite));
  const funding = await fetchRunFunding(deps, symbols, oldestEntry - HOUR_MS);
  const fundingUnavailable = symbols.filter((symbol) => funding.get(symbol) === null);
  const response = validateForwardResponse(await deps.callPython({
    schema_version: 1,
    symbols: symbols.map((symbol) => ({
      symbol,
      rows: rows.get(symbol),
      funding: funding.get(symbol) ?? [],
      funding_available: funding.get(symbol) !== null,
    })),
    strategy_ids: input.strategyIds,
    from_ms: fromMs,
    to_ms: boundaryMs - MINUTE_MS,
    open_setups: open,
    wallet_state: latest?.walletState ?? null,
  }));
  const previous = new Map<string, string | null>(
    open.map((item): [string, string | null] => [String(item.setup["setup_id"]), item.resolution?.status ?? null]),
  );
  const runId = await repository.insertRun(input.userId, {
    run_key: runKey, trigger: input.trigger, status: "ok", boundary_ms: boundaryMs, from_ms: fromMs,
    reason: fundingUnavailable.length ? `funding_unavailable: ${fundingUnavailable.join(",")}` : null,
    processed_to_ms: response.processed_to_ms, versions: response.versions, params_hash: response.params_hash,
    wallet_config: response.wallet_config, wallet_state: response.wallet_state,
    assumptions: response.assumptions, reasons: response.reasons, freshness,
  });
  const counts = await repository.persistEvaluation(input.userId, runId, response, previous);
  return fundingUnavailable.length
    ? { runKey, status: "ok", counts, reason: `funding_unavailable: ${fundingUnavailable.join(",")}` }
    : { runKey, status: "ok", counts };
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
