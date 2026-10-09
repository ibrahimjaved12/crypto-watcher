import type { SupabaseClient } from "@supabase/supabase-js";

/** Forward dashboard data (#239 P11). Reads only the signed-in user's rows (RLS). */
export type OutcomeRow = {
  setup_id: string;
  strategy_id: string;
  version: string;
  rr: string;
  status: string;
  net_ur: number | null;
  exit_ms: number | null;
};

export type StrategySummary = {
  strategyId: string;
  version: string;
  rr: string;
  n: number;
  wins: number;
  ambiguous: number;
  meanNetR: number | null;
  placebo: { n: number; meanNetR: number | null } | null;
};

const mean = (values: number[]) => (values.length ? values.reduce((a, b) => a + b, 0) / values.length / 1e6 : null);

/**
 * Final outcomes grouped by strategy, version and rr, sample size first, with the matched
 * `placebo-v1:<strategy>` control next to each strategy. Ambiguous outcomes count with their
 * pessimistic net (never resolved optimistically).
 */
export function summarizeOutcomes(rows: OutcomeRow[], sinceMs = 0): StrategySummary[] {
  const groups = new Map<string, OutcomeRow[]>();
  for (const row of rows) {
    if (row.net_ur === null || (row.exit_ms ?? 0) < sinceMs) continue;
    const key = `${row.strategy_id}|${row.version}|${row.rr}`;
    groups.set(key, [...(groups.get(key) ?? []), row]);
  }
  const summaries: StrategySummary[] = [];
  for (const [key, items] of groups) {
    const [strategyId, version, rr] = key.split("|") as [string, string, string];
    if (strategyId.startsWith("placebo-v1:")) continue;
    const placebo = [...groups.entries()].find(([other]) => other.startsWith(`placebo-v1:${strategyId}|`)
      && other.endsWith(`|${rr}`))?.[1];
    summaries.push({
      strategyId, version, rr, n: items.length,
      wins: items.filter((item) => item.status === "T").length,
      ambiguous: items.filter((item) => item.status === "ambiguous").length,
      meanNetR: mean(items.map((item) => item.net_ur!)),
      placebo: placebo ? { n: placebo.length, meanNetR: mean(placebo.map((item) => item.net_ur!)) } : null,
    });
  }
  return summaries.sort((a, b) => b.n - a.n || a.strategyId.localeCompare(b.strategyId));
}

export async function loadForwardDashboard(client: SupabaseClient) {
  const [signals, setups, outcomes, ledger, runs] = await Promise.all([
    client.from("forward_signals").select("signal_id, strategy_id, version, symbol, signal_ms, side, horizon_min")
      .order("signal_ms", { ascending: false }).limit(50),
    client.from("forward_setups").select("setup_id, strategy_id, version, symbol, side, horizon_min, entry_ms, rr, status")
      .eq("status", "T").order("entry_ms", { ascending: false }).limit(2000),
    client.from("forward_outcomes").select("setup_id, status, final, net_ur, exit_ms").limit(10000),
    client.from("paper_ledger").select("seq, ms, type, symbol, amount_e8, balance_e8")
      .order("seq", { ascending: true }).limit(10000),
    client.from("paper_runs").select("run_key, status, reason, boundary_ms, freshness, wallet_state, assumptions, created_at")
      .order("boundary_ms", { ascending: false }).limit(1),
  ]);
  for (const result of [signals, setups, outcomes, ledger, runs]) {
    if (result.error) throw new Error(`Forward dashboard read failed: ${result.error.message}`);
  }
  type Setup = { setup_id: string; strategy_id: string; version: string; symbol: string; side: number;
    horizon_min: number; entry_ms: number; rr: string; status: string };
  type Outcome = { setup_id: string; status: string; final: boolean; net_ur: number | null; exit_ms: number | null };
  const setupRows = (setups.data ?? []) as Setup[];
  const finals = new Map(((outcomes.data ?? []) as Outcome[]).filter((o) => o.final).map((o) => [o.setup_id, o]));
  const byId = new Map(setupRows.map((setup) => [setup.setup_id, setup]));
  const outcomeRows: OutcomeRow[] = [...finals.values()].flatMap((outcome) => {
    const setup = byId.get(outcome.setup_id);
    return setup ? [{ setup_id: outcome.setup_id, strategy_id: setup.strategy_id, version: setup.version,
      rr: setup.rr, status: outcome.status, net_ur: outcome.net_ur, exit_ms: outcome.exit_ms }] : [];
  });
  return {
    signals: signals.data ?? [],
    openSetups: setupRows.filter((setup) => !finals.has(setup.setup_id)).slice(0, 100),
    outcomeRows,
    equity: ((ledger.data ?? []) as { ms: number; balance_e8: number }[]).map((line) => ({
      ms: line.ms, balance: line.balance_e8 / 1e8 })),
    latestRun: (runs.data ?? [])[0] ?? null,
  };
}
