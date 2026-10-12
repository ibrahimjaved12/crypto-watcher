import { loadCandles } from "./providers.server";
import { quoteFigures, quoteStale, type SymbolQuote } from "./quote-calc";

export type { SymbolQuote } from "./quote-calc";

/**
 * Exchange-backed quote builder. The dashboard no longer uses it: `getMarketSnapshot` reads quotes
 * the machine published (#323), so the Worker makes no exchange request. Kept (unreferenced) until
 * the follow-up that removes the dead Worker-side fallback path; the maths lives in `quote-calc.ts`.
 */
export async function getQuote(symbol: string): Promise<SymbolQuote> {
  const fetchedAt = new Date().toISOString();
  const outcome = await loadCandles(symbol);

  const failed = (error: string, fallbackReasons: SymbolQuote["fallbackReasons"]): SymbolQuote => ({
    symbol,
    ok: false,
    source: null,
    price: null,
    changes: {},
    chart: [],
    lastCandleAt: null,
    stale: true,
    error,
    fallbackReasons,
    fetchedAt,
  });

  if (!outcome.ok) {
    return failed(`Futures data unavailable — ${outcome.errors.join(" | ")}`, outcome.attempts);
  }

  const { minute, quarter, source, attempts } = outcome.result;
  const figures = quoteFigures(minute, quarter);
  if (!figures) return failed("Futures data unavailable — no usable latest price", attempts);

  return {
    symbol,
    ok: true,
    source,
    price: figures.price,
    changes: figures.changes,
    chart: figures.chart,
    lastCandleAt: figures.lastCandleAt,
    stale: quoteStale(figures.lastCandleAt, Date.now()),
    error: null,
    fallbackReasons: attempts,
    fetchedAt,
  };
}

export async function getQuotes(symbols: string[]): Promise<SymbolQuote[]> {
  return Promise.all(symbols.map((s) => getQuote(s)));
}
