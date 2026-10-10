import type { SupabaseClient } from "@supabase/supabase-js";

import { DAY_MS, rangeMs, reportRpcArgs, strategyIdsFor, type ForwardFilters } from "./forward-filters";
import { buildReportRows, toCsv, type NonTradeRow, type OutcomeReportRow } from "./forward-report";

/**
 * Forward dashboard data (#239 P11, P20). Reads only the signed-in user's rows (RLS). Aggregates
 * (results, non-trades, equity) are computed by SQL over every row; nothing here has a row cap that
 * could drop old trades. Logs are keyset-paginated.
 */
export const LOG_PAGE_SIZE = 50;
export const EXPORT_PAGE_SIZE = 1000;
export const EXPORT_MAX_ROWS = 50_000;
const DAILY_EQUITY_SPAN_MS = 60 * DAY_MS;
const HOUR_MS = 3_600_000;

export type LogKind = "signals" | "outcomes" | "ledger";
export type LogRow = Record<string, string | number | null>;
export type SignalRow = {
  signal_id: string; strategy_id: string; version: string; symbol: string; signal_ms: number; side: number;
  horizon_min: number;
};
export type OpenSetupRow = {
  setup_id: string; strategy_id: string; version: string; symbol: string; side: number; horizon_min: number;
  entry_ms: number; rr: string;
};
export type LogPage = { rows: LogRow[]; nextCursor: string | null };

type Result = { data: unknown; error: { message: string } | null };
type Query = PromiseLike<Result> & {
  select(columns: string): Query;
  eq(column: string, value: unknown): Query;
  in(column: string, values: readonly unknown[]): Query;
  gte(column: string, value: unknown): Query;
  lt(column: string, value: unknown): Query;
  or(filter: string): Query;
  order(column: string, options: { ascending: boolean }): Query;
  limit(count: number): Query;
};

async function rows<T>(query: PromiseLike<Result>, what: string): Promise<T[]> {
  const { data, error } = await query;
  if (error) throw new Error(`Forward ${what} read failed: ${error.message}`);
  return (data ?? []) as T[];
}

const COLUMNS: Record<LogKind, string[]> = {
  signals: ["signal_id", "strategy_id", "version", "symbol", "signal_ms", "side", "horizon_min"],
  outcomes: ["setup_id", "strategy_id", "version", "symbol", "side", "horizon_min", "rr", "entry_ms", "status",
    "net_ur", "cost_ur", "fund_ur", "exit_ms"],
  ledger: ["seq", "ms", "type", "symbol", "setup_id", "amount_e8", "balance_e8"],
};
const SOURCE: Record<LogKind, string> = { signals: "forward_signals", outcomes: "forward_outcome_log", ledger: "paper_ledger" };
const TIME_COLUMN: Record<LogKind, string> = { signals: "signal_ms", outcomes: "exit_ms", ledger: "ms" };
const CURSOR = { signals: /^\d+\|[0-9a-f]{64}$/, outcomes: /^\d+\|[0-9a-f]{64}$/, ledger: /^\d+$/ };

export function isValidCursor(kind: LogKind, cursor: string): boolean {
  return CURSOR[kind].test(cursor);
}

/** One page of a log, newest first, keyset-paginated; `nextCursor` is null on the last page. */
export async function loadForwardLogPage(
  client: SupabaseClient,
  kind: LogKind,
  filters: ForwardFilters,
  cursor: string | null,
  pageSize = LOG_PAGE_SIZE,
): Promise<LogPage> {
  if (cursor !== null && !isValidCursor(kind, cursor)) throw new Error("Invalid log cursor");
  let query = (client.from(SOURCE[kind]) as unknown as Query).select(COLUMNS[kind].join(","));
  const time = TIME_COLUMN[kind];
  const { fromMs, toMs } = rangeMs(filters);
  if (fromMs !== null) query = query.gte(time, fromMs);
  if (toMs !== null) query = query.lt(time, toMs);
  if (kind !== "ledger") {
    const ids = strategyIdsFor(filters);
    if (ids) query = query.in("strategy_id", ids);
    if (filters.symbols.length) query = query.in("symbol", filters.symbols);
    if (filters.horizons.length) query = query.in("horizon_min", filters.horizons);
    if (filters.sides.length) query = query.in("side", filters.sides);
  }
  if (kind === "outcomes" && filters.rr.length) query = query.in("rr", filters.rr);
  const idColumn = kind === "signals" ? "signal_id" : "setup_id";
  if (kind === "ledger") {
    if (cursor !== null) query = query.lt("seq", Number(cursor));
    query = query.order("seq", { ascending: false });
  } else {
    if (cursor !== null) {
      const [ms, id] = cursor.split("|") as [string, string];
      query = query.or(`${time}.lt.${ms},and(${time}.eq.${ms},${idColumn}.gt.${id})`);
    }
    query = query.order(time, { ascending: false }).order(idColumn, { ascending: true });
  }
  const page = await rows<LogRow>(query.limit(pageSize + 1), `${kind} log`);
  const more = page.length > pageSize;
  const out = more ? page.slice(0, pageSize) : page;
  const last = out.at(-1);
  const nextCursor =
    more && last ? (kind === "ledger" ? String(last["seq"]) : `${last[time]}|${last[idColumn]}`) : null;
  return { rows: out, nextCursor };
}

/** CSV of up to EXPORT_MAX_ROWS log rows honouring the filters; pages are read one after the other. */
export async function exportForwardLog(
  client: SupabaseClient,
  kind: LogKind,
  filters: ForwardFilters,
): Promise<{ csv: string; rows: number; truncated: boolean }> {
  const lines: unknown[][] = [];
  let cursor: string | null = null;
  let truncated = false;
  do {
    const page: LogPage = await loadForwardLogPage(client, kind, filters, cursor, EXPORT_PAGE_SIZE);
    for (const row of page.rows) lines.push(COLUMNS[kind].map((column) => row[column] ?? null));
    cursor = page.nextCursor;
    if (lines.length >= EXPORT_MAX_ROWS) {
      truncated = cursor !== null;
      break;
    }
  } while (cursor !== null);
  return { csv: toCsv(COLUMNS[kind], lines), rows: lines.length, truncated };
}

/** Strategy rows (with matched random-timing control) and non-trade counts for the filters. */
export async function loadForwardReport(client: SupabaseClient, filters: ForwardFilters) {
  const args = reportRpcArgs(filters);
  const rpc = (name: string) => client.rpc(name, args) as unknown as PromiseLike<Result>;
  const [outcomes, nonTrades] = await Promise.all([
    rows<OutcomeReportRow>(rpc("forward_outcome_report"), "outcome report"),
    rows<NonTradeRow>(rpc("forward_nontrade_report"), "non-trade report"),
  ]);
  return buildReportRows(outcomes, nonTrades);
}

export async function loadForwardDashboard(client: SupabaseClient) {
  const from = (table: string) => client.from(table) as unknown as Query;
  const [signals, openSetups, firstLine, lastLine, runs] = await Promise.all([
    rows<SignalRow>(from("forward_signals").select(COLUMNS.signals.join(","))
      .order("signal_ms", { ascending: false }).limit(50), "signals"),
    rows<OpenSetupRow>(from("forward_open_setups").select("setup_id, strategy_id, version, symbol, side, horizon_min, entry_ms, rr")
      .order("entry_ms", { ascending: false }).limit(100), "open setups"),
    rows<{ ms: number }>(from("paper_ledger").select("ms").order("seq", { ascending: true }).limit(1), "ledger"),
    rows<{ ms: number }>(from("paper_ledger").select("ms").order("seq", { ascending: false }).limit(1), "ledger"),
    rows<Record<string, unknown>>(from("paper_runs")
      .select("run_key, status, reason, boundary_ms, freshness, wallet_state, assumptions, created_at")
      .order("boundary_ms", { ascending: false }).limit(1), "runs"),
  ]);
  const span = firstLine[0] && lastLine[0] ? lastLine[0].ms - firstLine[0].ms : 0;
  const bucketMs = span > DAILY_EQUITY_SPAN_MS ? DAY_MS : HOUR_MS;
  const series = await rows<{ ms: number; balance_e8: number | string }>(
    client.rpc("paper_equity_series", { p_bucket_ms: bucketMs, p_from_ms: null }) as unknown as PromiseLike<Result>,
    "equity series",
  );
  return {
    signals,
    openSetups,
    equity: series.map((line) => ({ ms: Number(line.ms), balance: Number(line.balance_e8) / 1e8 })),
    equityBucketMs: bucketMs,
    latestRun: runs[0] ?? null,
  };
}
