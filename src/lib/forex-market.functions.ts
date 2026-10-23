import { createServerFn } from "@tanstack/react-start";
import { requireMarket } from "@/integrations/supabase/auth-middleware";
import { toFxQuote, type FxQuote, type FxQuoteRow } from "./market/fx-quotes";

/**
 * Forex quotes for the Market page (#398). Read with the signed-in user's own client: the table's RLS
 * (`has_market('forex')`) already limits it to forex-entitled users, so the service role is never used
 * here. A read error throws, so the page shows an error state instead of an empty market.
 */
export const getForexQuotes = createServerFn({ method: "GET" })
  .middleware([requireMarket("forex")])
  .handler(async ({ context }): Promise<{ quotes: FxQuote[]; readAt: number }> => {
    const { data, error } = await context.supabase
      .from("fx_market_quotes")
      .select("symbol, instrument_id, venue, bid, ask, spread_points, digits, changes, chart, last_bar_at, published_at");
    if (error) throw new Error("Forex quotes are unavailable");
    const readAt = Date.now();
    const quotes = ((data ?? []) as FxQuoteRow[]).flatMap((row) => {
      const quote = toFxQuote(row, readAt);
      return quote ? [quote] : [];
    });
    return { quotes, readAt };
  });
