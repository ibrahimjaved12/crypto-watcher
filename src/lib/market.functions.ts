import { createServerFn } from "@tanstack/react-start";

import { MAX_WATCHLIST_SIZE, isSupportedSymbol } from "./market/symbols";
import type { SymbolQuote } from "./market/quotes.server";

export type MarketSnapshot = {
  quotes: SymbolQuote[];
  fetchedAt: string;
};

/**
 * Public read of exchange market data. No user data is touched, so this needs
 * no auth and can be called from public loaders.
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
    const { getQuotes } = await import("./market/quotes.server");
    const quotes = data.symbols.length ? await getQuotes(data.symbols) : [];
    return { quotes, fetchedAt: new Date().toISOString() };
  });
