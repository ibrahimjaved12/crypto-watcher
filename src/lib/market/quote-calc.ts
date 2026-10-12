// Pure quote maths shared by the machine-side publisher, the legacy exchange-backed quote builder
// and the tests, so the published numbers stay identical to the ones the dashboard always showed.
// No server-only imports: only type imports, which are erased.
import type { ProviderAttempt } from "./providers.server";

export type QuoteCandle = { time: number; close: number };

export type SymbolQuote = {
  symbol: string;
  ok: boolean;
  source: string | null;
  price: number | null;
  /** Minutes -> percent change. Missing key means the window could not be computed. */
  changes: Record<number, number | null>;
  /** 24h of 15m closes for the chart. */
  chart: Array<{ time: number; close: number }>;
  lastCandleAt: number | null;
  /** True when the newest candle is older than 10 minutes. */
  stale: boolean;
  error: string | null;
  /**
   * Failed provider attempts in fallback order. On a successful quote these are the earlier sources
   * that failed (empty when the first source answered); on a failed quote, every source. Published
   * quotes never involve a fallback, so they carry an empty list.
   */
  fallbackReasons: ProviderAttempt[];
  fetchedAt: string;
};

/** 1m candles needed for the 5/15/60 minute changes (60 bars back plus the newest). */
export const QUOTE_MINUTE_BARS = 61;
/** 15m candles needed for the 4h and 24h changes and the chart (96 bars back plus the newest). */
export const QUOTE_QUARTER_BARS = 97;
/** A quote whose newest candle or publication is older than this is stale. */
export const QUOTE_STALE_MS = 10 * 60 * 1000;

export function pctFrom(series: readonly QuoteCandle[], barsBack: number): number | null {
  if (series.length <= barsBack) return null;
  const last = series[series.length - 1];
  const prev = series[series.length - 1 - barsBack];
  if (!last || !prev || !prev.close) return null;
  return ((last.close - prev.close) / prev.close) * 100;
}

export type QuoteFigures = {
  price: number;
  changes: Record<number, number | null>;
  chart: QuoteCandle[];
  /** Open time (ms) of the newest 1m candle. */
  lastCandleAt: number;
};

/**
 * Price, changes and chart from a 1m series and a 15m series, oldest first. Returns null when
 * there is no usable newest 1m close (never a made-up price).
 */
export function quoteFigures(
  minuteSeries: readonly QuoteCandle[],
  quarterSeries: readonly QuoteCandle[],
): QuoteFigures | null {
  const minute = minuteSeries.slice(-QUOTE_MINUTE_BARS);
  const quarter = quarterSeries.slice(-QUOTE_QUARTER_BARS);
  const last = minute[minute.length - 1];
  if (!last || !Number.isFinite(last.close) || last.close <= 0) return null;
  return {
    price: last.close,
    changes: {
      5: pctFrom(minute, 5),
      15: pctFrom(minute, 15),
      60: pctFrom(minute, 60),
      240: pctFrom(quarter, 16),
      1440: pctFrom(quarter, 96),
    },
    chart: quarter.map(({ time, close }) => ({ time, close })),
    lastCandleAt: last.time,
  };
}

/** True when the newest candle is older than the stale threshold. */
export function quoteStale(lastCandleAt: number, now: number): boolean {
  return now - lastCandleAt > QUOTE_STALE_MS;
}
