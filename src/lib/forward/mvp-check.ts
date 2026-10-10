/**
 * MVP self-check rules (#239 P21, pure). `npm run mvp:check` gathers read-only facts from the two
 * databases and the Python service; this module turns them into PASS / WARN / FAIL lines. Every
 * threshold is a constant here so the rules are visible and testable.
 */
export type Level = "PASS" | "WARN" | "FAIL";
export type Check = { name: string; level: Level; detail: string };

export const HOUR_MS = 3_600_000;
export const DAY_MS = 86_400_000;
export const THRESHOLDS = {
  historyFailBelowDays: 60,
  historyWarnBelowDays: 120,
  /** Missing minutes inside the last 120 days: PASS 0, WARN up to this, FAIL above. */
  missingWarnMax: 120,
  /** The forward stale rule looks at the last 48 hours. */
  recentGapWindowMs: 48 * HOUR_MS,
  historyWindowMs: 120 * DAY_MS,
  runPassWithinMs: 2 * HOUR_MS,
  runWarnWithinMs: 6 * HOUR_MS,
  placeboWindowMs: 7 * DAY_MS,
  conflictAgeHours: 24,
} as const;
export const BENCHMARK_SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT"] as const;
export const COLLECTOR_TIMEFRAMES = [1, 15, 60, 240] as const;
export const FORWARD_TABLES = ["paper_runs", "forward_signals", "forward_setups", "forward_outcomes", "paper_ledger"] as const;
export const CANDLE_EVIDENCE_TABLES = ["collector_candle_conflicts", "collector_candle_revisions"] as const;

export const classifyHistoryDays = (days: number): Level =>
  days < THRESHOLDS.historyFailBelowDays ? "FAIL" : days < THRESHOLDS.historyWarnBelowDays ? "WARN" : "PASS";

export const classifyMissing120 = (missing: number): Level =>
  missing === 0 ? "PASS" : missing <= THRESHOLDS.missingWarnMax ? "WARN" : "FAIL";

export const classifyMissingRecent = (missing: number): Level => (missing === 0 ? "PASS" : "FAIL");

export const classifyRunAge = (ageMs: number): Level =>
  ageMs < THRESHOLDS.runPassWithinMs ? "PASS" : ageMs < THRESHOLDS.runWarnWithinMs ? "WARN" : "FAIL";

/** Days of stored history, saturating at the 120-day window the job reads. */
export function historyDays(firstMs: number | null, boundaryMs: number): number {
  if (firstMs === null) return 0;
  return Math.min(THRESHOLDS.historyWindowMs / DAY_MS, Math.max(0, (boundaryMs - firstMs) / DAY_MS));
}

/** Minutes missing from [fromMs, boundaryMs) given the stored candle open times (any order, may start earlier). */
export function missingMinutesIn(openTimes: readonly number[], fromMs: number, boundaryMs: number): number {
  const start = Math.ceil(fromMs / 60_000) * 60_000;
  const expected = Math.max(0, Math.floor((boundaryMs - start) / 60_000));
  const stored = new Set<number>();
  for (const open of openTimes) if (open >= start && open < boundaryMs) stored.add(open);
  return Math.max(0, expected - stored.size);
}

export type LedgerSummary = { n: number; max_seq: number; sum_amount_e8: number; last_balance_e8: number };

/** `initial + sum(amount) == last balance` and `seq` contiguous from 1 (count equals the maximum). */
export function ledgerInvariant(initialE8: number, ledger: LedgerSummary | null): { ok: boolean; detail: string } {
  if (!ledger || ledger.n === 0) return { ok: true, detail: "no ledger lines yet" };
  if (ledger.n !== ledger.max_seq) {
    return { ok: false, detail: `seq is not contiguous from 1 (${ledger.n} lines, highest seq ${ledger.max_seq})` };
  }
  const expected = initialE8 + ledger.sum_amount_e8;
  if (expected !== ledger.last_balance_e8) {
    return { ok: false, detail: `initial + sum(amount) = ${expected} but the last balance is ${ledger.last_balance_e8} (e8)` };
  }
  return { ok: true, detail: `${ledger.n} lines, balance reconciles` };
}

/** Sum of the signals reported as `no_sigma: N signal(s) ...` in a run's per-symbol `reasons`. */
export function noSigmaSignals(reasons: unknown): number {
  if (!reasons || typeof reasons !== "object") return 0;
  let total = 0;
  for (const value of Object.values(reasons as Record<string, unknown>)) {
    const sigma = (value as { sigma?: unknown } | null)?.sigma;
    const match = typeof sigma === "string" ? /^no_sigma: (\d+)/.exec(sigma) : null;
    if (match) total += Number(match[1]);
  }
  return total;
}

/** Non-placebo strategies that fired recently without a matching `placebo-v1:` strategy that also fired. */
export function strategiesWithoutPlacebo(recentStrategyIds: readonly string[]): string[] {
  const seen = new Set(recentStrategyIds);
  return [...seen].filter((id) => !id.startsWith("placebo-v1:") && !seen.has(`placebo-v1:${id}`)).sort();
}

export type RunRow = {
  status: string;
  reason: string | null;
  boundary_ms: number;
  reasons?: unknown;
  freshness?: { request?: { rows?: number; python_ms?: number } } | null;
};

export function checkLatestRun(run: RunRow | null, nowMs: number): Check[] {
  if (!run) {
    return [{ name: "forward run", level: "FAIL", detail: "no forward run recorded yet (start the hourly job or run it once)" }];
  }
  const checks: Check[] = [];
  const hours = (Math.max(0, nowMs - run.boundary_ms) / HOUR_MS).toFixed(1);
  if (run.status === "ok") {
    checks.push({ name: "forward run status", level: "PASS", detail: "latest run ok" });
  } else {
    checks.push({ name: "forward run status", level: "FAIL", detail: `${run.status}${run.reason ? `: ${run.reason}` : ""}` });
  }
  const age = classifyRunAge(Math.max(0, nowMs - run.boundary_ms));
  checks.push({ name: "forward run age", level: age, detail: `latest run boundary was ${hours} h ago` });
  const request = run.freshness?.request;
  if (request?.rows !== undefined) {
    checks.push({ name: "forward request", level: "PASS", detail: `${request.rows} rows sent to Python, ${request.python_ms ?? "?"} ms` });
  }
  if (run.reason && /backfill_failed/.test(run.reason)) {
    checks.push({ name: "backfill", level: "WARN", detail: run.reason.slice(0, 300) });
  }
  return checks;
}

export function worst(checks: readonly Check[]): Level {
  return checks.some((c) => c.level === "FAIL") ? "FAIL" : checks.some((c) => c.level === "WARN") ? "WARN" : "PASS";
}

export function exitCodeFor(checks: readonly Check[]): number {
  return worst(checks) === "FAIL" ? 1 : 0;
}

export function formatTable(checks: readonly Check[]): string {
  const width = Math.max(...checks.map((c) => c.name.length), 4);
  return checks.map((c) => `${c.level.padEnd(4)}  ${c.name.padEnd(width)}  ${c.detail}`).join("\n");
}
