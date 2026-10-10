import {
  FORWARD_HISTORY_DAYS,
  FORWARD_SYMBOLS,
  HOUR_MS,
  missingMinutes,
  repairSymbolHistory,
  runForward,
  type ForwardDeps,
  type ForwardRunSummary,
} from "./forward-run.server";

/**
 * The local hourly job (#239 P21), separated from the process entry so it can be tested with injected
 * dependencies. Every registered account gets its own run (`FORWARD_USER_ID` may narrow it); one
 * failing account never stops the others, and the failure count is returned so `--once` can exit 1.
 */
export type ForwardJobIo = {
  listUsers(): Promise<string[]>;
  deps(): Promise<ForwardDeps>;
  strategyIds: string[];
  log(line: string): void;
  error(line: string): void;
  run?: (deps: ForwardDeps, input: Parameters<typeof runForward>[1]) => Promise<ForwardRunSummary>;
};

export async function runHourlyForAccounts(io: ForwardJobIo): Promise<{ users: number; failed: number }> {
  const users = await io.listUsers();
  if (!users.length) {
    io.log("[forward-run] no registered accounts yet");
    return { users: 0, failed: 0 };
  }
  const deps = await io.deps();
  const run = io.run ?? runForward;
  let failed = 0;
  for (const userId of users) {
    try {
      const summary = await run(deps, { userId, trigger: "hourly", strategyIds: io.strategyIds });
      io.log(
        `[forward-run] ${userId} ${summary.runKey} ${summary.status}${summary.reason ? `: ${summary.reason}` : ""}` +
          (summary.counts ? ` ${JSON.stringify(summary.counts)}` : ""),
      );
    } catch (error) {
      failed += 1;
      io.error(`[forward-run] ${userId}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }
  return { users: users.length, failed };
}

export type BackfillReport = { symbol: string; days: number; missing: number; error: string | null };

/**
 * `forward-run --backfill-only`: seed the 1m history for the six symbols once, before the loops start,
 * and report the minutes still missing per symbol (those reach Python as missing bars).
 */
export async function backfillOnly(
  deps: Pick<ForwardDeps, "now" | "readMinutes" | "backfillMinutes">,
  log: (line: string) => void,
  symbols: readonly string[] = FORWARD_SYMBOLS,
): Promise<BackfillReport[]> {
  const boundaryMs = Math.floor(deps.now() / HOUR_MS) * HOUR_MS;
  const sinceMs = boundaryMs - FORWARD_HISTORY_DAYS * 86_400_000;
  const reports: BackfillReport[] = [];
  for (const symbol of symbols) {
    log(`[forward-backfill] ${symbol}: reading stored history`);
    const { series, error } = await repairSymbolHistory(deps, symbol, sinceMs, boundaryMs, true);
    const first = series[0]?.open_time_ms ?? null;
    const report = {
      symbol,
      days: first === null ? 0 : Math.max(0, Math.floor((boundaryMs - first) / 86_400_000)),
      missing: missingMinutes(series, sinceMs, boundaryMs),
      error,
    };
    log(`[forward-backfill] ${symbol}: ${report.days} days stored, ${report.missing} minutes missing in the last ` +
      `${FORWARD_HISTORY_DAYS} days${error ? `, FAILED: ${error}` : ""}`);
    reports.push(report);
  }
  return reports;
}
