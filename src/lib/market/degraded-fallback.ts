// TEMPORARY degraded-mode fallback (#323): remove after the machine has run 24/7 for 30 days
import { publishedFreshness, type PublishedSnapshot } from "./published-quotes";
import type { SymbolQuote } from "./quote-calc";
import type { ProviderObserver } from "./providers.server";
import type { MarketSource } from "./symbols";

export const DEGRADED_SOURCES = ["kraken-futures", "okx-usdt-swap"] as const;
export type DegradedMarket = {
  source: (typeof DEGRADED_SOURCES)[number];
  reason: "empty" | "stale";
  lastPublishedAt: string | null;
};

export function degradedSourceLabel(source: string): string {
  return source === "kraken-futures" ? "Kraken futures" : "OKX USDT perpetual swaps";
}

export function degradedContractLabel(source: string | null, symbol: string): string | null {
  const base = symbol.replace(/USDT$/, "");
  if (source === "kraken-futures")
    return `Kraken ${base === "BTC" ? "PF_XBTUSD" : `PF_${base}USD`} · USD perpetual · trade price (not Binance USDⓈ-M)`;
  if (source === "okx-usdt-swap")
    return `OKX ${base}-USDT-SWAP · USDT perpetual · trade price (not Binance USDⓈ-M)`;
  return null;
}

/** One decision per snapshot. Never fill gaps in a fresh Binance snapshot with another venue. */
export async function withDegradedFallback<T extends PublishedSnapshot>(
  snapshot: T,
  symbols: string[],
  loadQuotes: (symbols: string[], options: { sources: readonly MarketSource[]; observer: ProviderObserver }) => Promise<SymbolQuote[]>,
  enabled: string | undefined = undefined,
): Promise<T & { degraded?: DegradedMarket }> {
  const reason = publishedFreshness(snapshot);
  if (enabled === "false" || reason === "fresh" || symbols.length === 0) return snapshot;
  // Leave headroom under the Worker's subrequest limit, including the hosted read.
  let requests = 0;
  const observer: ProviderObserver = {
    request() {
      if (requests >= 40) throw new Error("Temporary fallback exchange request budget exhausted");
      requests += 1;
    },
    candleRows() {},
  };
  const publishedBySymbol = new Map(snapshot.quotes.map((quote) => [quote.symbol, quote]));
  const attempts = new Map<string, SymbolQuote["fallbackReasons"]>();
  for (const source of DEGRADED_SOURCES) {
    const loaded = await loadQuotes([...new Set(symbols)], { sources: [source], observer });
    const quotes = loaded.map((quote) => {
      const fallbackReasons = [...(attempts.get(quote.symbol) ?? []), ...quote.fallbackReasons];
      attempts.set(quote.symbol, fallbackReasons);
      return { ...quote, source: quote.source ?? source, fallbackReasons };
    });
    if (quotes.some((quote) => quote.ok && quote.price !== null)) {
      return {
        ...snapshot,
        quotes: quotes.map((quote) => {
          if (quote.ok && quote.price !== null) return quote;
          const published = publishedBySymbol.get(quote.symbol);
          if (!published?.ok || published.price === null) return quote;
          // Preserve the published venue, timestamps and stale flag for failed pairs.
          return {
            ...published,
            fallbackReasons: [...published.fallbackReasons, ...quote.fallbackReasons],
          };
        }),
        degraded: { source, reason, lastPublishedAt: snapshot.publishedAt },
      };
    }
  }
  // Both venues failed: retain the exact published snapshot and its stale/empty presentation.
  return snapshot;
}
