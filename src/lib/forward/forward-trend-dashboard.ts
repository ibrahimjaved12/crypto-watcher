import type { SupabaseClient } from "@supabase/supabase-js";

/**
 * Forward trend track dashboard (#239 P14). Reads only the signed-in user's rows (RLS). Statistics
 * come from the recorded daily ppm values; mu_min = z * sd / sqrt(n) with the backtest's z
 * (`power.min_detectable_edge_per_day`, alpha 0.05 two-sided, power 0.8: z = 2.8016).
 */
export const MU_MIN_Z = 2.801585218;

export type TrendLedgerPoint = {
  track: string;
  day_ms: number;
  daily_ppm: number;
  equity_ppm: number;
  turnover_ppm: number;
  gross_ppm: number;
};

export type TrackInfo = { name: string; kind: string; control: string | null };

export type TrackSummary = {
  track: string;
  kind: string;
  control: string | null;
  days: number;
  meanDaily: number | null;
  sdDaily: number | null;
  muMinDaily: number | null;
  /** Days needed for the current mean to reach mu_min at the current sigma (null when mean <= 0). */
  daysNeeded: number | null;
  significanceLine: string;
  equity: { day_ms: number; equity: number }[];
  meanTurnover: number | null;
  meanGross: number | null;
};

export function summarizeTrack(info: TrackInfo, rows: TrendLedgerPoint[]): TrackSummary {
  const own = rows.filter((row) => row.track === info.name).sort((a, b) => a.day_ms - b.day_ms);
  const n = own.length;
  const values = own.map((row) => row.daily_ppm / 1e6);
  const mean = n ? values.reduce((a, b) => a + b, 0) / n : null;
  const sd = n > 1 && mean !== null
    ? Math.sqrt(values.reduce((a, b) => a + (b - mean) ** 2, 0) / (n - 1)) : null;
  const muMin = sd !== null ? (MU_MIN_Z * sd) / Math.sqrt(n) : null;
  const daysNeeded = sd !== null && mean !== null && mean > 0 ? Math.ceil(((MU_MIN_Z * sd) / mean) ** 2) : null;
  const pct = (value: number) => `${(value * 100).toFixed(3)}%`;
  const significanceLine = n < 2 || muMin === null || mean === null
    ? `${n} day${n === 1 ? "" : "s"}: not enough days for a test`
    : mean >= muMin
      ? `${n} days: mean ${pct(mean)}/day is at or above mu_min ${pct(muMin)}/day (one look, not a verdict)`
      : `${n} days, not significant: mean ${pct(mean)}/day < mu_min ${pct(muMin)}/day` +
        (daysNeeded ? `; at this mean and sigma about ${daysNeeded} days are needed` : "; mean is not positive");
  const avg = (key: "turnover_ppm" | "gross_ppm") => (n ? own.reduce((a, row) => a + row[key], 0) / n / 1e6 : null);
  return {
    track: info.name, kind: info.kind, control: info.control, days: n, meanDaily: mean, sdDaily: sd,
    muMinDaily: muMin, daysNeeded, significanceLine,
    equity: own.map((row) => ({ day_ms: row.day_ms, equity: row.equity_ppm / 1e6 })),
    meanTurnover: avg("turnover_ppm"), meanGross: avg("gross_ppm"),
  };
}

export async function loadTrendDashboard(client: SupabaseClient) {
  const [ledger, weights, state] = await Promise.all([
    client.from("forward_trend_ledger").select("track, day_ms, daily_ppm, equity_ppm, turnover_ppm, gross_ppm")
      .order("day_ms", { ascending: true }).limit(20000),
    client.from("forward_trend_weights").select("track, day_ms, weights")
      .order("day_ms", { ascending: false }).limit(200),
    client.from("forward_trend_state").select("run_key, status, reason, through_day_ms, tracks, created_at")
      .order("created_at", { ascending: false }).limit(1),
  ]);
  for (const result of [ledger, weights, state]) {
    if (result.error) throw new Error(`Forward trend dashboard read failed: ${result.error.message}`);
  }
  const latest = ((state.data ?? [])[0] ?? null) as { run_key: string; status: string; reason: string | null;
    through_day_ms: number | null; tracks: TrackInfo[]; created_at: string } | null;
  const current = new Map<string, { day_ms: number; weights: Record<string, number> }>();
  for (const row of (weights.data ?? []) as { track: string; day_ms: number; weights: Record<string, number> }[]) {
    if (!current.has(row.track)) current.set(row.track, row);  // newest day first: the weights held next
  }
  const rows = (ledger.data ?? []) as TrendLedgerPoint[];
  return {
    latestRun: latest,
    tracks: (latest?.tracks ?? []).map((info) => ({ ...summarizeTrack(info, rows),
      currentWeights: current.get(info.name) ?? null })),
  };
}
