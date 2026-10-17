import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

import type { SupabaseClient } from "@supabase/supabase-js";

import { supabaseAdmin } from "../integrations/supabase/client.server";
import { FORWARD_STRATEGY_IDS } from "../lib/forward/forward-deps.server";
import { listForwardUsers } from "../lib/forward/forward-users.server";
import {
  BENCHMARK_SYMBOLS,
  CANDLE_EVIDENCE_TABLES,
  COLLECTOR_TIMEFRAMES,
  FORWARD_TABLES,
  HOUR_MS,
  THRESHOLDS,
  checkLatestRun,
  classifyReconciliation,
  classifyHistoryDays,
  classifyMissing120,
  classifyMissingRecent,
  exitCodeFor,
  formatTable,
  historyDays,
  ledgerInvariant,
  missingMinutesIn,
  noSigmaSignals,
  strategiesWithoutPlacebo,
  type Check,
  type LedgerSummary,
  type ReconcileReport,
  type RunRow,
} from "../lib/forward/mvp-check";
import { pythonServiceConfig } from "../lib/python-service.server";
import { publishFreshnessChecks } from "../lib/market/published-quotes";
import { readCollectorUniverse } from "../lib/market/collector-subscriptions.server";
import { collectorLogMessage } from "../lib/market/collector-logging";
import { getOperationalStore } from "../lib/operational/repository.server";
import { STATUS_FILE_NAME, classifyWorkerHost } from "../lib/worker-host";
import { hostedDatabaseSizeCheck } from "./hosted-db-size";
import { classifyAlertCharts } from "../lib/alert-charts/check";
import { alertChartsConfig } from "../lib/alert-charts/config";
import { getAlertChartStore } from "../lib/alert-charts/store.server";

// `npm run mvp:check` (#239 P21): READ-ONLY health table for the local MVP. It reads the operational
// and application databases and GET <python>/health; it writes nothing. Exit code 1 on any FAIL;
// --json prints the checks as JSON instead of a table.
const db = supabaseAdmin as unknown as SupabaseClient;
const rpc = async <T>(name: string, args?: Record<string, unknown>): Promise<T[]> => {
  const { data, error } = await db.rpc(name, args);
  if (error) throw new Error(`${name}: ${error.message}`);
  return (data ?? []) as T[];
};

async function collectorChecks(): Promise<Check[]> {
  const store = getOperationalStore();
  if (!store.enabled) {
    return [{ name: "operational database", level: "FAIL", detail: "OPERATIONAL_DB_ENABLED is not true in .env.local (npm run env:local:operational, or run npm run dev:local:all once)" }];
  }
  const checks: Check[] = [];
  const health = await store.listCollectorHealth([...BENCHMARK_SYMBOLS]);
  const bad: string[] = [];
  for (const symbol of BENCHMARK_SYMBOLS) {
    for (const timeframe of COLLECTOR_TIMEFRAMES) {
      const row = health.find((item) => item.symbol === symbol && item.timeframe_minutes === timeframe);
      if (!row || row.status !== "LIVE") bad.push(`${symbol} ${timeframe}m ${row?.status ?? "missing"}`);
    }
  }
  checks.push({
    name: "collector health",
    level: bad.length ? "FAIL" : "PASS",
    detail: bad.length ? `not LIVE: ${bad.slice(0, 6).join(", ")}${bad.length > 6 ? ` (+${bad.length - 6} more)` : ""}` : "6 symbols x 1/15/60/240m LIVE",
  });
  const conflicts24h = health.reduce((sum, row) => sum + (row.conflict_count_24h ?? 0), 0);
  const conflicts = await store.listCandleConflicts(168, 1000);
  const cutoff = Date.now() - THRESHOLDS.conflictAgeHours * HOUR_MS;
  const unreconciled = conflicts.filter((c) => c.revision_id === null && Date.parse(c.detected_at) < cutoff).length;
  checks.push({
    name: "candle conflicts",
    level: unreconciled > 0 ? "WARN" : "PASS",
    detail: `${conflicts24h} in the last 24 h; ${unreconciled} older than ${THRESHOLDS.conflictAgeHours} h without a revision` +
      (unreconciled > 0 ? " (reconcile runs hourly; those outside tolerance need a look)" : ""),
  });
  const boundary = Math.floor(Date.now() / HOUR_MS) * HOUR_MS;
  for (const symbol of BENCHMARK_SYMBOLS) {
    const rows = await store.readForwardMinuteCandles(symbol, boundary - THRESHOLDS.historyWindowMs, boundary);
    const opens = rows.map((row) => row.open_time_ms);
    const days = historyDays(opens[0] ?? null, boundary);
    const missing120 = missingMinutesIn(opens, boundary - THRESHOLDS.historyWindowMs, boundary);
    const missing48 = missingMinutesIn(opens, boundary - THRESHOLDS.recentGapWindowMs, boundary);
    const first = opens[0] === undefined ? "none" : new Date(opens[0]).toISOString().slice(0, 16) + "Z";
    checks.push({ name: `${symbol} history`, level: classifyHistoryDays(days), detail: `${days.toFixed(0)} days (first minute ${first})` });
    checks.push({ name: `${symbol} gaps 120d`, level: classifyMissing120(missing120), detail: `${missing120} missing minutes` });
    checks.push({ name: `${symbol} gaps 48h`, level: classifyMissingRecent(missing48), detail: `${missing48} missing minutes (the stale rule blocks the engine on any)` });
  }
  const missingOp = await store.missingAppendOnlyTriggers([...CANDLE_EVIDENCE_TABLES]);
  checks.push({
    name: "append-only (candle evidence)",
    level: missingOp.length ? "FAIL" : "PASS",
    detail: missingOp.length ? `no trigger on: ${missingOp.join(", ")}` : "conflicts and revisions are protected",
  });
  return checks;
}

type CountRow = { user_id: string | null; kind: string; status: string; n: number | string };

async function accountChecks(userId: string, label: string, all: { counts: CountRow[]; ledgers: (LedgerSummary & { user_id: string; paper_account: string })[]; strategies: { user_id: string | null; strategy_id: string }[] }): Promise<Check[]> {
  const p = label ? `${label} ` : "";
  const checks: Check[] = [];
  const { data: runs } = await db.from("paper_runs")
    .select("status, reason, boundary_ms, reasons, freshness, wallet_config").eq("user_id", userId)
    .eq("market", "crypto").order("boundary_ms", { ascending: false }).limit(1);
  const run = (runs?.[0] ?? null) as (RunRow & { wallet_config?: { initial_balance_e8?: number } | null }) | null;
  for (const check of checkLatestRun(run, Date.now())) checks.push({ ...check, name: p + check.name });

  const { data: trend } = await db.from("forward_trend_state")
    .select("status, reason, through_day_ms, committed").eq("user_id", userId)
    .order("revision", { ascending: false }).limit(1);
  const t = trend?.[0] as { status: string; reason: string | null; through_day_ms: number | null; committed: boolean } | undefined;
  checks.push({
    name: `${p}trend run`,
    level: !t ? "WARN" : t.status === "ok" && t.committed ? "PASS" : "WARN",
    detail: t ? `${t.status}${t.committed ? "" : " (not committed)"}, scored through ${t.through_day_ms === null ? "—" : new Date(t.through_day_ms).toISOString().slice(0, 10)}${t.reason ? `: ${t.reason}` : ""}` : "no daily trend run recorded yet",
  });

  const mine = all.counts.filter((row) => row.user_id === null || row.user_id === userId);
  const by = (kind: string) => mine.filter((row) => row.kind === kind).map((row) => `${row.status || "all"} ${row.n}`).join(", ") || "none";
  const signals = Number(mine.find((row) => row.kind === "signals")?.n ?? 0);
  const setups = mine.filter((row) => row.kind === "setups").reduce((sum, row) => sum + Number(row.n), 0);
  const noSigma = noSigmaSignals(run?.reasons);
  checks.push({
    name: `${p}records`,
    level: signals > 0 && setups === 0 ? "WARN" : "PASS",
    detail: `signals ${signals}; setups ${by("setups")}; final outcomes ${by("outcomes")}` +
      (signals > 0 && setups === 0 ? `; sigma not ready (${noSigma} signal(s) reported no_sigma)` : ""),
  });

  const recent = all.strategies.filter((row) => row.user_id === null || row.user_id === userId).map((row) => row.strategy_id);
  const noPlacebo = strategiesWithoutPlacebo(recent);
  checks.push({
    name: `${p}placebo controls`,
    level: noPlacebo.length ? "WARN" : "PASS",
    detail: noPlacebo.length ? `no matched placebo-v1 signal in the last 7 days for: ${noPlacebo.join(", ")}` : `${new Set(recent.filter((id) => !id.startsWith("placebo-v1:"))).size} strategies with matched controls (of ${FORWARD_STRATEGY_IDS.length / 2} configured)`,
  });

  const { data: account, error: accountError } = await db.from("forward_wallet_accounts")
    .select("account_id,config").eq("user_id", userId).eq("market", "crypto").maybeSingle();
  if (accountError) throw new Error(`Forward wallet account read failed: ${accountError.message}`);
  const initial = Number(account?.config?.initial_balance_e8 ?? run?.wallet_config?.initial_balance_e8 ?? 100e8);
  const ledger = all.ledgers.find((row) => row.user_id === userId && row.paper_account === account?.account_id) ?? null;
  const result = ledgerInvariant(initial, ledger && {
    n: Number(ledger.n), max_seq: Number(ledger.max_seq), sum_amount_e8: Number(ledger.sum_amount_e8),
    last_balance_e8: Number(ledger.last_balance_e8),
  });
  checks.push({ name: `${p}paper ledger`, level: result.ok ? "PASS" : "FAIL", detail: result.detail });
  if (account?.account_id) {
    const reconciliation = await walletReconciliation(userId, String(account.account_id), initial);
    checks.push({ ...reconciliation, name: `${p}${reconciliation.name}` });
  }
  return checks;
}

// #36: the whole ledger of an account against its stored wallet state, by the wallet service's pure `reconcile`
// (events: [] changes nothing). Read-only; the result line carries counts only.
const RECONCILE_MAX_LINES = 200_000;
async function walletReconciliation(userId: string, accountId: string, initialE8: number): Promise<Check> {
  const { data: runs, error } = await db.from("paper_runs").select("wallet_state")
    .eq("user_id", userId).eq("paper_account", accountId).eq("status", "ok")
    .order("boundary_ms", { ascending: false }).order("created_at", { ascending: false }).limit(1);
  if (error) return classifyReconciliation(null, `wallet state read failed: ${error.message}`);
  const state = (runs?.[0] as { wallet_state?: { version?: string } | null } | undefined)?.wallet_state;
  if (!state) return classifyReconciliation(null);
  const ledger: Record<string, unknown>[] = [];
  for (let from = 0; from < RECONCILE_MAX_LINES; from += 1000) {
    const { data, error: pageError } = await db.from("paper_ledger")
      .select("seq,type,amount_e8,balance_e8,fill_id:payload->>fill_id,order_id:payload->>order_id")
      .eq("user_id", userId).eq("paper_account", accountId).order("seq", { ascending: true }).range(from, from + 999);
    if (pageError) return classifyReconciliation(null, `ledger read failed: ${pageError.message}`);
    ledger.push(...((data ?? []) as Record<string, unknown>[]));
    if ((data ?? []).length < 1000) break;
    if (from + 1000 >= RECONCILE_MAX_LINES) return classifyReconciliation(null, `ledger longer than ${RECONCILE_MAX_LINES} lines`);
  }
  let service;
  try {
    service = pythonServiceConfig("/v1/forward/wallet_step");
  } catch {
    return classifyReconciliation(null, "python wallet service is not configured");
  }
  try {
    const response = await fetch(service.url, { method: "POST", signal: AbortSignal.timeout(60_000),
      headers: { Authorization: `Bearer ${service.token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ schema_version: 1, state, events: [],
        ...(state.version === "wallet-v3" ? { config: { wallet_version: "wallet-v3" } } : {}),
        reconcile: { initial_balance_e8: initialE8, ledger } }) });
    if (!response.ok) return classifyReconciliation(null, `wallet service answered HTTP ${response.status}`);
    const body = (await response.json()) as { full_reconciliation?: ReconcileReport };
    return classifyReconciliation(body.full_reconciliation ?? null, "no reconciliation in the response");
  } catch (reason) {
    return classifyReconciliation(null, `wallet service unreachable: ${collectorLogMessage(reason).slice(0, 120)}`);
  }
}

async function pythonCheck(): Promise<Check> {
  const base = process.env["PYTHON_ANALYSIS_URL"];
  if (!base) return { name: "python service", level: "WARN", detail: "PYTHON_ANALYSIS_URL is not set; skipped" };
  try {
    const response = await fetch(new URL("/health", base), { signal: AbortSignal.timeout(5_000) });
    return { name: "python service", level: response.ok ? "PASS" : "FAIL", detail: `GET /health -> ${response.status}` };
  } catch (error) {
    return { name: "python service", level: "FAIL", detail: `unreachable: ${error instanceof Error ? error.message : String(error)}` };
  }
}

// `npm run worker:host` (#270) writes .worker-host.status.json in the repository root; npm runs
// this script from there. Missing = the host is not in use (the local dev:local:all flow).
function workerHostCheck(): Check {
  const path = resolve(process.cwd(), STATUS_FILE_NAME);
  let raw: string | null = null;
  try {
    if (existsSync(path)) raw = readFileSync(path, "utf8");
  } catch (error) {
    return { name: "worker host", level: "FAIL", detail: `cannot read ${STATUS_FILE_NAME}: ${error instanceof Error ? error.message : String(error)}` };
  }
  return { name: "worker host", ...classifyWorkerHost(raw, Date.now()) };
}

// #323: the Worker's price cards read the quotes the machine publishes. Production mode only, where the
// hosted project is the one the deployed Worker reads; WARN when a symbol's newest published_at is older
// than 10 minutes (the publisher is down, or the symbol is not collected).
const productionMode = process.argv.includes("production") || process.env["NODE_ENV"] === "production";

// Read-only: use the reconciliation's derivation, never assign from a health check.
async function collectorUniverseCheck(): Promise<Check> {
  const name = "collector universe";
  try {
    const store = getOperationalStore();
    if (!store.enabled) return { name, level: "WARN", detail: "operational store disabled; cannot compare subscriptions" };
    const [expected, current] = await Promise.all([
      readCollectorUniverse(supabaseAdmin),
      store.readCollectorSubscriptions(),
    ]);
    const missing = expected.filter((symbol) => !current.includes(symbol));
    const extra = current.filter((symbol) => !expected.includes(symbol));
    return {
      name,
      level: missing.length || extra.length ? "WARN" : "PASS",
      detail: missing.length || extra.length
        ? `stale universe: missing=${missing.join(",") || "none"}; extra=${extra.join(",") || "none"}; check collector-universe-sync child`
        : `${current.length} subscriptions match hosted watchlists/settings plus benchmarks`,
    };
  } catch (error) {
    return { name, level: "WARN", detail: `cannot compare universe: ${collectorLogMessage(error).slice(0, 200)}` };
  }
}

// #299: stored alert chart windows and PNG sizes against the configured caps (read-only).
async function alertChartsCheck(): Promise<Check> {
  const name = "alert charts";
  try {
    const config = alertChartsConfig();
    const store = getAlertChartStore(config);
    if (!store) return { name, level: "WARN", detail: "operational store disabled; no alert charts are captured" };
    return { name, ...classifyAlertCharts(await store.diagnostics(), config) };
  } catch (error) {
    return { name, level: "WARN", detail: `cannot read alert charts: ${collectorLogMessage(error).slice(0, 200)}` };
  }
}

async function publishedQuoteChecks(): Promise<Check[]> {
  const { data, error } = await db.from("market_quotes").select("symbol, published_at");
  if (error) return [{ name: "published quotes", level: "FAIL", detail: `market_quotes read failed: ${error.message}` }];
  return publishFreshnessChecks([...BENCHMARK_SYMBOLS], (data ?? []) as { symbol: string; published_at: string }[], Date.now());
}

async function main() {
  const checks: Check[] = [];
  checks.push(await hostedDatabaseSizeCheck());
  checks.push(await pythonCheck());
  checks.push(workerHostCheck());
  if (productionMode) {
    checks.push(...(await publishedQuoteChecks()));
    checks.push(await collectorUniverseCheck());
  }
  checks.push(...(await collectorChecks()));
  checks.push(await alertChartsCheck());
  const users = await listForwardUsers(db, process.env["FORWARD_USER_ID"]);
  const [counts, ledgers, strategies, missingApp] = await Promise.all([
    rpc<CountRow>("forward_state_counts"),
    rpc<LedgerSummary & { user_id: string; paper_account: string }>("paper_ledger_summary"),
    rpc<{ user_id: string | null; strategy_id: string }>("forward_recent_strategies", {
      p_since_ms: Date.now() - THRESHOLDS.placeboWindowMs }),
    rpc<string>("missing_append_only_triggers", { p_tables: [...FORWARD_TABLES] }),
  ]);
  if (!users.length) checks.push({ name: "accounts", level: "FAIL", detail: "no registered account; sign up in the local app first" });
  for (const userId of users) {
    checks.push(...(await accountChecks(userId, users.length > 1 ? `[${userId.slice(0, 8)}]` : "", { counts, ledgers, strategies })));
  }
  // The RPC returns text[] as one array value; normalise either shape.
  const missing = (Array.isArray(missingApp[0]) ? (missingApp[0] as unknown as string[]) : missingApp) as string[];
  checks.push({
    name: "append-only (forward tables)",
    level: missing.length ? "FAIL" : "PASS",
    detail: missing.length ? `no trigger on: ${missing.join(", ")}` : "shared market and per-user ledger/run tables are protected",
  });
  if (process.argv.includes("--json")) console.log(JSON.stringify({ ok: exitCodeFor(checks) === 0, checks }, null, 2));
  else console.log(formatTable(checks));
  process.exit(exitCodeFor(checks));
}

await main().catch((error) => {
  console.error(`[mvp-check] ${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
});
