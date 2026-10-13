import { createServerFn } from "@tanstack/react-start";

import { MAX_WATCHLIST_SIZE, isSupportedSymbol } from "./market/symbols";
import type { SymbolQuote } from "./market/quote-calc";

// TEMPORARY degraded-mode fallback (#323): remove after the machine has run 24/7 for 30 days
import type { DegradedMarket } from "./market/degraded-fallback";

export type MarketSnapshot = {
  // TEMPORARY degraded-mode fallback (#323): remove after the machine has run 24/7 for 30 days
  degraded?: DegradedMarket;
  quotes: SymbolQuote[];
  fetchedAt: string;
  /** Newest `published_at` among the returned quotes; null when nothing is published yet. */
  publishedAt: string | null;
};

/**
 * Public read of the market quotes the owner's machine publishes to hosted Supabase (#323). No user
 * data is touched, so this needs no auth and can be called from public loaders. The Worker never
 * calls an exchange except the temporary degraded-mode fallback.
 */
export const getMarketSnapshot = createServerFn({ method: "POST" })
  .validator((input: { symbols: string[] }) => {
    const symbols = (input?.symbols ?? [])
      .map((s) => String(s).toUpperCase())
      .filter(isSupportedSymbol)
      .slice(0, MAX_WATCHLIST_SIZE);
    return { symbols };
  })
  .handler(async ({ data }): Promise<MarketSnapshot> => {
    const { readPublishedSnapshot } = await import("./market/published-quotes.server");
    const { quotes, publishedAt } = await readPublishedSnapshot(data.symbols);
    // TEMPORARY degraded-mode fallback (#323): remove after the machine has run 24/7 for 30 days
    const { withDegradedFallback } = await import("./market/degraded-fallback");
    return withDegradedFallback(
      { quotes, fetchedAt: new Date().toISOString(), publishedAt },
      data.symbols,
      async (symbols, options) => {
        const { getQuotes } = await import("./market/quotes.server");
        return getQuotes(symbols, options);
      },
      process.env["MARKET_DEGRADED_FALLBACK_ENABLED"],
    );
  });
