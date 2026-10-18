// Pure forex quote maths (#433, part of #374): from completed 1m BID bars (oldest first, newest last)
// and the broker's symbol spec (#432) to one row of the hosted `fx_market_quotes`. No I/O.
//
// - bid: the close of the newest completed bar; ask = bid + spread_points * point, computed on integer
//   points (round(bid * 10^digits) + spread_points, then / 10^digits) so there is no float drift.
// - changes[m]: percent change of the bid against the close of the latest bar at or before
//   (newest open - m minutes), only when that bar is at most 60 minutes older than the target time; a
//   market closure (e.g. the weekend) therefore gives null, never a stale comparison.
// - chart: 15-minute bid closes (the close of the last 1m bar in each bucket) of the 96 buckets ending
//   with the newest bar's bucket; buckets without a bar are omitted.
import type { FxGatewayRow, FxSymbolSpec } from "../operational/types";

const MINUTE_MS = 60_000;
export const FX_CHANGE_WINDOWS = [5, 15, 60, 240, 1440] as const;
export const FX_CHART_BUCKET_MS = 15 * MINUTE_MS;
export const FX_CHART_BUCKETS = 96;
/** The longest a reference bar may be older than its target time. */
export const FX_CHANGE_MAX_GAP_MS = 60 * MINUTE_MS;
/** History a quote needs: the 1440-minute window plus its gap tolerance. */
export const FX_QUOTE_HISTORY_MS = (1440 + 60) * MINUTE_MS;

export type FxChartPoint = { time: number; close: number };

/** What the publisher sends to publish_fx_market_quotes (the database stamps `published_at`). */
export type FxQuoteInput = {
  symbol: string;
  instrument_id: string;
  venue: string;
  bid: number;
  ask: number;
  spread_points: number;
  digits: number;
  changes: Record<string, number | null>;
  chart: FxChartPoint[];
  last_bar_at: string;
};

/** ask = bid + spreadPoints * 10^-digits on integer points. */
export function fxAsk(bid: number, spreadPoints: number, digits: number): number {
  const scale = 10 ** digits;
  return (Math.round(bid * scale) + spreadPoints) / scale;
}

/** The latest bar with open time <= target (bars oldest first), or null. */
function barAtOrBefore(bars: readonly FxGatewayRow[], target: number): FxGatewayRow | null {
  let lo = 0;
  let hi = bars.length - 1;
  let found: FxGatewayRow | null = null;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const bar = bars[mid]!;
    if (bar.open_time_ms <= target) {
      found = bar;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return found;
}

export function fxChanges(bars: readonly FxGatewayRow[]): Record<string, number | null> {
  const changes: Record<string, number | null> = {};
  const newest = bars.at(-1);
  for (const minutes of FX_CHANGE_WINDOWS) {
    let change: number | null = null;
    if (newest) {
      const target = newest.open_time_ms - minutes * MINUTE_MS;
      const reference = barAtOrBefore(bars, target);
      if (reference && target - reference.open_time_ms <= FX_CHANGE_MAX_GAP_MS && reference.close > 0) {
        change = ((newest.close - reference.close) / reference.close) * 100;
      }
    }
    changes[String(minutes)] = change;
  }
  return changes;
}

export function fxChart(bars: readonly FxGatewayRow[]): FxChartPoint[] {
  const newest = bars.at(-1);
  if (!newest) return [];
  const lastBucket = Math.floor(newest.open_time_ms / FX_CHART_BUCKET_MS) * FX_CHART_BUCKET_MS;
  const firstBucket = lastBucket - (FX_CHART_BUCKETS - 1) * FX_CHART_BUCKET_MS;
  const closes = new Map<number, number>();
  for (const bar of bars) {
    if (bar.open_time_ms < firstBucket || bar.open_time_ms > newest.open_time_ms) continue;
    // Bars are oldest first, so the last write per bucket is the bucket's last 1m bar.
    closes.set(Math.floor(bar.open_time_ms / FX_CHART_BUCKET_MS) * FX_CHART_BUCKET_MS, bar.close);
  }
  return [...closes].sort(([a], [b]) => a - b).map(([time, close]) => ({ time, close }));
}

/** One quote, or null when there is no bar or the bars belong to another instrument than the spec. */
export function fxQuoteFromBars(bars: readonly FxGatewayRow[], spec: FxSymbolSpec): FxQuoteInput | null {
  const newest = bars.at(-1);
  if (!newest || newest.instrument_id !== spec.instrumentId || newest.venue !== spec.venue) return null;
  const symbol = spec.instrumentId.split(":")[1] ?? "";
  return {
    symbol,
    instrument_id: spec.instrumentId,
    venue: spec.venue,
    bid: newest.close,
    ask: fxAsk(newest.close, newest.spread_points, spec.digits),
    spread_points: newest.spread_points,
    digits: spec.digits,
    changes: fxChanges(bars),
    chart: fxChart(bars),
    last_bar_at: new Date(newest.open_time_ms).toISOString(),
  };
}
