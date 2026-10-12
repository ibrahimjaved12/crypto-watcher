import { createServerFn } from "@tanstack/react-start";

import { MAX_WATCHLIST_SIZE, isSupportedSymbol } from "./market/symbols";
import type { SymbolQuote } from "./market/quote-calc";

export type MarketSnapshot = {
  quotes: SymbolQuote[];
  fetchedAt: string;
  /** Newest `published_at` among the returned quotes; null when nothing is published yet. */
  publishedAt: string | null;
};

/**
 * Public read of the market quotes the owner's machine publishes to hosted Supabase (#323). No user
 * data is touched, so this needs no auth and can be called from public loaders. The Worker never
 * calls an exchange: ingestion (the machine) and serving (this read) are separate.
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
    return { quotes, fetchedAt: new Date().toISOString(), publishedAt };
  });
