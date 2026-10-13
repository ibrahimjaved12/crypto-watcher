import { loadCandles, type ProviderObserver } from "./providers.server";
import { quoteFigures, quoteStale, type SymbolQuote } from "./quote-calc";

export type { SymbolQuote } from "./quote-calc";

// TEMPORARY degraded-mode fallback (#323): remove after the machine has run 24/7 for 30 days
import type { MarketSource } from "./symbols";
export type QuoteLoadOptions = {
  sources?: readonly MarketSource[];
  observer?: ProviderObserver;
  load?: typeof loadCandles;
};

/** Exchange quote builder used only when the published snapshot needs temporary fallback. */
export async function getQuote(symbol: string, options: QuoteLoadOptions = {}): Promise<SymbolQuote> {
  const fetchedAt = new Date().toISOString();
  const outcome = await (options.load ?? loadCandles)(symbol, undefined, options.observer, options.sources);

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

// TEMPORARY degraded-mode fallback (#323): remove after the machine has run 24/7 for 30 days
export async function getQuotes(symbols: string[], options: QuoteLoadOptions = {}): Promise<SymbolQuote[]> {
  return Promise.all([...new Set(symbols)].map((s) => getQuote(s, options)));
}
