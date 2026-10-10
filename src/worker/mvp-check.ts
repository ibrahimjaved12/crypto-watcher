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
  type RunRow,
} from "../lib/forward/mvp-check";
import { getOperationalStore } from "../lib/operational/repository.server";

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

type CountRow = { user_id: string; kind: string; status: string; n: number | string };

async function accountChecks(userId: string, label: string, all: { counts: CountRow[]; ledgers: (LedgerSummary & { user_id: string; paper_account: string })[]; strategies: { user_id: string; strategy_id: string }[] }): Promise<Check[]> {
  const p = label ? `${label} ` : "";
  const checks: Check[] = [];
  const { data: runs } = await db.from("paper_runs")
    .select("status, reason, boundary_ms, reasons, freshness, wallet_config").eq("user_id", userId)
    .order("boundary_ms", { ascending: false }).limit(1);
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

  const mine = all.counts.filter((row) => row.user_id === userId);
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

  const recent = all.strategies.filter((row) => row.user_id === userId).map((row) => row.strategy_id);
  const noPlacebo = strategiesWithoutPlacebo(recent);
  checks.push({
    name: `${p}placebo controls`,
    level: noPlacebo.length ? "WARN" : "PASS",
    detail: noPlacebo.length ? `no matched placebo-v1 signal in the last 7 days for: ${noPlacebo.join(", ")}` : `${new Set(recent.filter((id) => !id.startsWith("placebo-v1:"))).size} strategies with matched controls (of ${FORWARD_STRATEGY_IDS.length / 2} configured)`,
  });

  const initial = Number(run?.wallet_config?.initial_balance_e8 ?? 100e8);
  const ledger = all.ledgers.find((row) => row.user_id === userId && row.paper_account === "default") ?? null;
  const result = ledgerInvariant(initial, ledger && {
    n: Number(ledger.n), max_seq: Number(ledger.max_seq), sum_amount_e8: Number(ledger.sum_amount_e8),
    last_balance_e8: Number(ledger.last_balance_e8),
  });
  checks.push({ name: `${p}paper ledger`, level: result.ok ? "PASS" : "FAIL", detail: result.detail });
  return checks;
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

async function main() {
  const checks: Check[] = [];
  checks.push(await pythonCheck());
  checks.push(...(await collectorChecks()));
  const users = await listForwardUsers(db, process.env["FORWARD_USER_ID"]);
  const [counts, ledgers, strategies, missingApp] = await Promise.all([
    rpc<CountRow>("forward_state_counts"),
    rpc<LedgerSummary & { user_id: string; paper_account: string }>("paper_ledger_summary"),
    rpc<{ user_id: string; strategy_id: string }>("forward_recent_strategies", {
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
    detail: missing.length ? `no trigger on: ${missing.join(", ")}` : "the five forward tables are protected",
  });
  if (process.argv.includes("--json")) console.log(JSON.stringify({ ok: exitCodeFor(checks) === 0, checks }, null, 2));
  else console.log(formatTable(checks));
  process.exit(exitCodeFor(checks));
}

await main().catch((error) => {
  console.error(`[mvp-check] ${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
});
