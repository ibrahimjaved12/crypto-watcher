// TEMPORARY degraded-mode fallback (#323): remove after the machine has run 24/7 for 30 days
import type { PublishedSnapshot } from "./published-quotes";
import type { SymbolQuote } from "./quote-calc";
import type { ProviderObserver } from "./providers.server";
import type { MarketSource } from "./symbols";

export const DEGRADED_SOURCES = ["kraken-futures", "okx-usdt-swap"] as const;
export type DegradedMarket = {
  source: (typeof DEGRADED_SOURCES)[number];
  reason: "empty" | "stale";
  lastPublishedAt: string | null;
};

/** Keep fresh published pairs; fetch new candles for every old or missing pair. */
export async function withDegradedFallback<T extends PublishedSnapshot>(
  snapshot: T,
  symbols: string[],
  loadQuotes: (
    symbols: string[],
    options: { sources: readonly MarketSource[]; observer: ProviderObserver },
  ) => Promise<SymbolQuote[]>,
  enabled: string | undefined = undefined,
): Promise<T & { degraded?: DegradedMarket }> {
  if (enabled === "false" || symbols.length === 0) return snapshot;
  const requested = [...new Set(symbols)];
  const publishedBySymbol = new Map(snapshot.quotes.map((quote) => [quote.symbol, quote]));
  const fresh = (quote: SymbolQuote | undefined) =>
    !!quote?.ok && quote.price !== null && !quote.stale;
  let pending = requested.filter((symbol) => !fresh(publishedBySymbol.get(symbol)));
  if (pending.length === 0) return snapshot;
  const reason = pending.some((symbol) => publishedBySymbol.get(symbol)?.ok) ? "stale" : "empty";
  // Leave headroom under the Worker's subrequest limit, including the hosted read.
  let requests = 0;
  const observer: ProviderObserver = {
    request() {
      if (requests >= 40) throw new Error("Temporary fallback exchange request budget exhausted");
      requests += 1;
    },
    candleRows() {},
  };
  const attempts = new Map<string, SymbolQuote["fallbackReasons"]>();
  const refreshed = new Map<string, SymbolQuote>();
  const failed = new Map<string, SymbolQuote>();
  let firstSource: DegradedMarket["source"] | undefined;
  for (const source of DEGRADED_SOURCES) {
    const loaded = await loadQuotes(pending, { sources: [source], observer });
    for (const quote of loaded) {
      if (!pending.includes(quote.symbol)) continue;
      const fallbackReasons = [
        ...(attempts.get(quote.symbol) ?? []),
        ...quote.fallbackReasons,
        ...(quote.ok && quote.stale
          ? [{ source, status: null, message: "Latest candles are too old" }]
          : []),
      ];
      attempts.set(quote.symbol, fallbackReasons);
      const result = { ...quote, source: quote.source ?? source, fallbackReasons };
      if (fresh(quote)) {
        refreshed.set(quote.symbol, result);
        firstSource ??= source;
      } else {
        failed.set(quote.symbol, result);
      }
    }
    pending = pending.filter((symbol) => !refreshed.has(symbol));
    if (pending.length === 0) break;
  }
  // Keep the API's published snapshot on total failure; cards hide prices marked old.
  if (!firstSource) return snapshot;
  return {
    ...snapshot,
    quotes: requested.map((symbol) => {
      const replacement = refreshed.get(symbol);
      if (replacement) return replacement;
      const published = publishedBySymbol.get(symbol);
      if (published && fresh(published)) return published;
      const failure = failed.get(symbol);
      if (published)
        return {
          ...published,
          fallbackReasons: [...published.fallbackReasons, ...(attempts.get(symbol) ?? [])],
        };
      return (
        failure ?? {
          symbol,
          ok: false,
          source: null,
          price: null,
          changes: {},
          chart: [],
          lastCandleAt: null,
          stale: true,
          error: "Fresh prices could not be loaded.",
          fallbackReasons: attempts.get(symbol) ?? [],
          fetchedAt: new Date().toISOString(),
        }
      );
    }),
    degraded: { source: firstSource, reason, lastPublishedAt: snapshot.publishedAt },
  };
}
