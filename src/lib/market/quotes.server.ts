import { loadCandles, type Candle } from "./providers.server";

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
  fetchedAt: string;
};

function pctFrom(series: Candle[], barsBack: number): number | null {
  if (series.length <= barsBack) return null;
  const last = series[series.length - 1];
  const prev = series[series.length - 1 - barsBack];
  if (!last || !prev || !prev.close) return null;
  return ((last.close - prev.close) / prev.close) * 100;
}

export async function getQuote(symbol: string): Promise<SymbolQuote> {
  const fetchedAt = new Date().toISOString();
  const outcome = await loadCandles(symbol);

  if (!outcome.ok) {
    return {
      symbol,
      ok: false,
      source: null,
      price: null,
      changes: {},
      chart: [],
      lastCandleAt: null,
      stale: true,
      error: `No exchange reachable — ${outcome.errors.join(" | ")}`,
      fetchedAt,
    };
  }

  const { minute, quarter, source } = outcome.result;
  const last = minute[minute.length - 1]!;
  const lastCandleAt = last.time;

  return {
    symbol,
    ok: true,
    source,
    price: last.close,
    changes: {
      5: pctFrom(minute, 5),
      15: pctFrom(minute, 15),
      60: pctFrom(minute, 60),
      240: pctFrom(quarter, 16),
      1440: pctFrom(quarter, 96),
    },
    chart: quarter,
    lastCandleAt,
    stale: Date.now() - lastCandleAt > 10 * 60 * 1000,
    error: null,
    fetchedAt,
  };
}

export async function getQuotes(symbols: string[]): Promise<SymbolQuote[]> {
  return Promise.all(symbols.map((s) => getQuote(s)));
}
