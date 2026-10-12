import { createPublicSupabaseClient } from "../../integrations/supabase/public-client.server";
import { snapshotFromRows, type MarketQuoteRow, type PublishedSnapshot } from "./published-quotes";

/**
 * The Worker's whole market read path (#323): one query for the requested symbols against the
 * machine-published `market_quotes` table. It makes no exchange request and has no fallback; a read
 * failure throws so the page shows a load error instead of another venue's prices.
 */
export async function readPublishedSnapshot(
  symbols: readonly string[],
  now = Date.now(),
): Promise<PublishedSnapshot> {
  if (symbols.length === 0) return { quotes: [], publishedAt: null };
  const { data, error } = await createPublicSupabaseClient()
    .from("market_quotes")
    .select("symbol, source, price_type, price, changes, chart, last_candle_at, published_at")
    .in("symbol", [...symbols]);
  if (error) throw new Error(`Published market quotes unavailable: ${error.message}`);
  return snapshotFromRows(symbols, (data ?? []) as MarketQuoteRow[], now);
}
