/**
 * Forward-test report maths (#239 P20, pure). The database aggregates every final outcome in SQL
 * (`forward_outcome_report`, `forward_nontrade_report`); this module only turns those sums into
 * display rows: mean and standard error in R, the matched random-timing control, a Welch z for the
 * difference, a Bonferroni note for the number of strategy rows shown, and a fixed-rule verdict.
 * A verdict is a statement about this sample, not an edge: a positive row is a hypothesis for the
 * next sample.
 */
export const MIN_TRADES = 30;
export const Z_NOTABLE = 2;
const UR = 1e6; // 1 R = 1e6 micro-R in the stored `*_ur` columns

export type OutcomeReportRow = {
  strategy_id: string;
  version: string;
  horizon_min: number;
  rr: string;
  n: number | string;
  n_t: number | string;
  n_s: number | string;
  n_e: number | string;
  n_l: number | string;
  n_x: number | string;
  n_ambiguous: number | string;
  sum_net_ur: number | string;
  sum_sq_net_ur: number | string;
  sum_cost_ur: number | string;
  sum_fund_ur: number | string;
  first_exit_ms: number | null;
  last_exit_ms: number | null;
  placebo_n: number | string;
  placebo_sum_net_ur: number | string;
  placebo_sum_sq_net_ur: number | string;
};

export type NonTradeRow = {
  strategy_id: string;
  horizon_min: number;
  rr: string;
  n_v: number | string;
  n_c: number | string;
  n_p: number | string;
  n_g: number | string;
  n_n: number | string;
  pending_or_open: number | string;
};

export type NonTrades = { vetoed: number; cost: number; position: number; gap: number; notEntered: number; pending: number };
export type Moments = { n: number; mean: number | null; se: number | null };
export type VerdictKind = "too_few" | "no_baseline" | "no_spread" | "no_difference" | "better" | "worse";
export type ReportRow = {
  key: string;
  strategyId: string;
  version: string;
  horizonMin: number;
  rr: string;
  n: number;
  wins: number;
  stops: number;
  expired: number;
  liquidated: number;
  unresolved: number;
  ambiguous: number;
  winRate: number | null;
  mean: Moments;
  meanCostR: number | null;
  meanFundR: number | null;
  placebo: Moments | null;
  diffR: number | null;
  diffSe: number | null;
  z: number | null;
  verdict: { kind: VerdictKind; text: string };
  clearsBonferroni: boolean;
  nonTrades: NonTrades;
  firstExitMs: number | null;
  lastExitMs: number | null;
};

const num = (value: number | string | null | undefined) => (value === null || value === undefined ? 0 : Number(value));

/** Mean and standard error of the mean, in R, from n, the sum and the sum of squares in micro-R. */
export function moments(n: number, sum: number, sumSq: number): Moments {
  if (n <= 0) return { n: 0, mean: null, se: null };
  const meanUr = sum / n;
  if (n < 2) return { n, mean: meanUr / UR, se: null };
  const variance = Math.max(0, ((sumSq / n - meanUr * meanUr) * n) / (n - 1)); // rounding can dip below 0
  return { n, mean: meanUr / UR, se: Math.sqrt(variance / n) / UR };
}

/** Standard normal quantile (Acklam's rational approximation, relative error about 1e-9). */
export function normalQuantile(p: number): number {
  if (!(p > 0 && p < 1)) throw new RangeError("p must be in (0, 1)");
  const a = [-3.969683028665376e1, 2.209460984245205e2, -2.759285104469687e2, 1.38357751867269e2, -3.066479806614716e1, 2.506628277459239];
  const b = [-5.447609879822406e1, 1.615858368580409e2, -1.556989798598866e2, 6.680131188771972e1, -1.328068155288572e1];
  const c = [-7.784894002430293e-3, -3.223964580411365e-1, -2.400758277161838, -2.549732539343734, 4.374664141464968, 2.938163982698783];
  const d = [7.784695709041462e-3, 3.224671290700398e-1, 2.445134137142996, 3.754408661907416];
  const tail = (q: number) =>
    (((((c[0]! * q + c[1]!) * q + c[2]!) * q + c[3]!) * q + c[4]!) * q + c[5]!) /
    ((((d[0]! * q + d[1]!) * q + d[2]!) * q + d[3]!) * q + 1);
  const low = 0.02425;
  if (p < low) return tail(Math.sqrt(-2 * Math.log(p)));
  if (p > 1 - low) return -tail(Math.sqrt(-2 * Math.log(1 - p)));
  const q = p - 0.5;
  const r = q * q;
  return (
    ((((((a[0]! * r + a[1]!) * r + a[2]!) * r + a[3]!) * r + a[4]!) * r + a[5]!) * q) /
    (((((b[0]! * r + b[1]!) * r + b[2]!) * r + b[3]!) * r + b[4]!) * r + 1)
  );
}

/** Two-sided Bonferroni critical |z| at alpha = 0.05 for `k` strategy rows. */
export function bonferroniCritical(k: number, alpha = 0.05): number {
  return normalQuantile(1 - alpha / (2 * Math.max(1, Math.floor(k))));
}

export function verdictFor(
  n: number,
  z: number | null,
  hasBaseline: boolean,
  k: number,
): { kind: VerdictKind; text: string } {
  if (n < MIN_TRADES) return { kind: "too_few", text: "too few trades" };
  if (!hasBaseline) return { kind: "no_baseline", text: "no random-timing baseline yet" };
  if (z === null) return { kind: "no_spread", text: "no spread in the results; cannot compare" };
  if (Math.abs(z) < Z_NOTABLE) return { kind: "no_difference", text: "no difference from random timing" };
  if (z <= -Z_NOTABLE) return { kind: "worse", text: "worse than random timing" };
  return {
    kind: "better",
    text: `better than random timing, unadjusted for K (threshold for ${k} rows: z > ${bonferroniCritical(k).toFixed(2)})`,
  };
}

const keyOf = (strategyId: string, horizon: number, rr: string) => `${strategyId}|${horizon}|${rr}`;
const emptyNonTrades = (): NonTrades => ({ vetoed: 0, cost: 0, position: 0, gap: 0, notEntered: 0, pending: 0 });

export function buildReportRows(outcomes: OutcomeReportRow[], nonTrades: NonTradeRow[] = []): ReportRow[] {
  const nonTradeByKey = new Map<string, NonTrades>();
  for (const row of nonTrades) {
    nonTradeByKey.set(keyOf(row.strategy_id, row.horizon_min, row.rr), {
      vetoed: num(row.n_v), cost: num(row.n_c), position: num(row.n_p), gap: num(row.n_g),
      notEntered: num(row.n_n), pending: num(row.pending_or_open),
    });
  }
  const seen = new Set<string>();
  const rows: ReportRow[] = outcomes.map((row) => {
    const key = keyOf(row.strategy_id, row.horizon_min, row.rr);
    seen.add(key);
    return partialRow(row, nonTradeByKey.get(key) ?? emptyNonTrades());
  });
  for (const row of nonTrades) {
    const key = keyOf(row.strategy_id, row.horizon_min, row.rr);
    if (seen.has(key)) continue;
    seen.add(key);
    rows.push(partialRow(null, nonTradeByKey.get(key)!, row));
  }
  const k = rows.length;
  return rows.map((row) => {
    const verdict = verdictFor(row.n, row.z, row.placebo !== null && row.placebo.n >= 2, k);
    return {
      ...row,
      verdict,
      clearsBonferroni: verdict.kind === "better" && (row.z ?? 0) > bonferroniCritical(k),
    };
  });
}

function partialRow(row: OutcomeReportRow | null, nonTrades: NonTrades, fallback?: NonTradeRow): ReportRow {
  if (!row) {
    const id = fallback!;
    return {
      key: keyOf(id.strategy_id, id.horizon_min, id.rr), strategyId: id.strategy_id, version: "", horizonMin: id.horizon_min,
      rr: id.rr, n: 0, wins: 0, stops: 0, expired: 0, liquidated: 0, unresolved: 0, ambiguous: 0, winRate: null,
      mean: moments(0, 0, 0), meanCostR: null, meanFundR: null, placebo: null, diffR: null, diffSe: null, z: null,
      verdict: { kind: "too_few", text: "too few trades" }, clearsBonferroni: false, nonTrades,
      firstExitMs: null, lastExitMs: null,
    };
  }
  const n = num(row.n);
  const mean = moments(n, num(row.sum_net_ur), num(row.sum_sq_net_ur));
  const placeboN = num(row.placebo_n);
  const placebo = placeboN > 0 ? moments(placeboN, num(row.placebo_sum_net_ur), num(row.placebo_sum_sq_net_ur)) : null;
  let diffR: number | null = null;
  let diffSe: number | null = null;
  let z: number | null = null;
  if (placebo && mean.mean !== null && placebo.mean !== null) {
    diffR = mean.mean - placebo.mean;
    if (mean.se !== null && placebo.se !== null) {
      diffSe = Math.sqrt(mean.se ** 2 + placebo.se ** 2); // Welch: unequal variances, no pooling
      z = diffSe > 0 ? diffR / diffSe : null;
    }
  }
  return {
    key: keyOf(row.strategy_id, row.horizon_min, row.rr),
    strategyId: row.strategy_id, version: row.version, horizonMin: row.horizon_min, rr: row.rr, n,
    wins: num(row.n_t), stops: num(row.n_s), expired: num(row.n_e), liquidated: num(row.n_l),
    unresolved: num(row.n_x), ambiguous: num(row.n_ambiguous),
    winRate: n ? num(row.n_t) / n : null,
    mean,
    meanCostR: n ? num(row.sum_cost_ur) / n / UR : null,
    meanFundR: n ? num(row.sum_fund_ur) / n / UR : null,
    placebo, diffR, diffSe, z,
    verdict: { kind: "too_few", text: "too few trades" }, clearsBonferroni: false, nonTrades,
    firstExitMs: row.first_exit_ms, lastExitMs: row.last_exit_ms,
  };
}

export type EquityPoint = { ms: number; balance: number };

/** Peak-to-trough drawdown of an equity series (bucketed, so intra-bucket dips are not seen). */
export function maxDrawdown(points: EquityPoint[]): { usdt: number; fraction: number } | null {
  if (points.length < 2) return null;
  let peak = points[0]!.balance;
  let worst = { usdt: 0, fraction: 0 };
  for (const point of points) {
    if (point.balance > peak) peak = point.balance;
    const usdt = peak - point.balance;
    if (usdt > worst.usdt) worst = { usdt, fraction: peak > 0 ? usdt / peak : 0 };
  }
  return worst;
}

/** CSV cell: quotes escaped, and a leading `= + - @` (or tab/CR) neutralised against spreadsheet formulas. */
export function csvCell(value: unknown): string {
  let text = value === null || value === undefined ? "" : String(value);
  if (typeof value !== "number" && /^[=+\-@\t\r]/.test(text)) text = `'${text}`; // numbers keep their sign
  return /[",\n\r]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

export function toCsv(header: string[], rows: unknown[][]): string {
  return [header, ...rows].map((row) => row.map(csvCell).join(",")).join("\n") + "\n";
}
