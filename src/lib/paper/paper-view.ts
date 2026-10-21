/**
 * Paper account page helpers shared by both markets (#454, pure): the react-query keys and the equity-curve range
 * selection. The market is the first part of every key, so the crypto and forex accounts never share a cache
 * entry, and a market switch never shows the other account's numbers while the new ones load.
 */
import type { EquityPoint } from "./paper-account";

export type PaperMarket = "crypto" | "forex";
export type PaperListKind = "trades" | "rejected" | "ledger";

export const paperKeys = {
  /** The account read (wallet run, totals, balance history, prices). */
  account: (market: PaperMarket) => ["paper", market, "account"] as const,
  /** One paginated list; the wallet's last boundary is in the key, so a new hour refetches the first page. */
  list: (market: PaperMarket, kind: PaperListKind, accountId: string | null, boundary: number | null) =>
    ["paper", market, kind, accountId, boundary] as const,
};

/** A forex paper account id is `<user uuid>:forex` (forward_wallet_accounts); a crypto one never has the suffix. */
export const FOREX_ACCOUNT_SUFFIX = ":forex";
export const accountMarketOf = (accountId: string): PaperMarket =>
  accountId.endsWith(FOREX_ACCOUNT_SUFFIX) ? "forex" : "crypto";

// ---------------------------------------------------------------- equity range

export type EquityRange = "1d" | "1w" | "1m" | "all";
export const EQUITY_RANGES: readonly { key: EquityRange; label: string; long: string; ms: number | null }[] = [
  { key: "1d", label: "1D", long: "Last 24 hours", ms: 86_400_000 },
  { key: "1w", label: "1W", long: "Last 7 days", ms: 7 * 86_400_000 },
  { key: "1m", label: "1M", long: "Last 30 days", ms: 30 * 86_400_000 },
  { key: "all", label: "All", long: "Since the start", ms: null },
];

/**
 * The points of one range. The balance is a step series (it only changes on a ledger line), so the last point
 * before the window is carried to the window's start: a quiet day still shows a flat line at the right level
 * instead of nothing.
 */
export function rangePoints(points: readonly EquityPoint[], range: EquityRange, nowMs: number): EquityPoint[] {
  const span = EQUITY_RANGES.find((item) => item.key === range)?.ms ?? null;
  if (span === null) return [...points];
  const from = nowMs - span;
  const inside = points.filter((point) => point.ms >= from);
  const before = points.filter((point) => point.ms < from).at(-1);
  if (before && (inside.length === 0 || inside[0]!.ms > from)) inside.unshift({ ms: from, value: before.value });
  return inside;
}

/** Change over the shown points: absolute and as a fraction of the first one (null without two points). */
export function rangeChange(points: readonly EquityPoint[]): { value: number; fraction: number | null } | null {
  if (points.length < 2) return null;
  const first = points[0]!.value;
  const last = points[points.length - 1]!.value;
  return { value: last - first, fraction: first > 0 ? (last - first) / first : null };
}

/**
 * The realized balance as a step series for the hero curve: the `paper_equity_series` points (last balance of each
 * bucket), extended flat to now so the line reaches the present. Unrealized P&L is not drawn (it is shown beside).
 */
export function balanceSeries(
  series: readonly { ms: number; balance_e8: number | string }[],
  nowMs: number,
): EquityPoint[] {
  const points = series
    .map((point) => ({ ms: Number(point.ms), value: Number(point.balance_e8) / 1e8 }))
    .filter((point) => Number.isFinite(point.ms) && Number.isFinite(point.value))
    .sort((a, b) => a.ms - b.ms);
  const last = points[points.length - 1];
  if (last && nowMs > last.ms) points.push({ ms: nowMs, value: last.value });
  return points;
}
