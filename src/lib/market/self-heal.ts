import type { MarketSnapshot } from "../market.functions";
import type { SymbolQuote } from "./quote-calc";

export function isUnavailable(quote: SymbolQuote): boolean {
  return !quote.ok || quote.price == null || quote.stale;
}

const HEAL_DELAYS_MS = [2_000, 4_000, 8_000, 15_000, 30_000, 60_000];

/** One jittered batch, backing off to a minute instead of one timer per failed card. */
export function healDelayMs(attempt: number, rand = Math.random): number {
  const base = HEAL_DELAYS_MS[Math.max(0, Math.floor(attempt))] ?? 60_000;
  return Math.round(base * (0.8 + rand() * 0.4));
}

/** A partial retry can repair existing pairs, but cannot erase other quotes or advance refresh time. */
export function mergeHealed(
  current: MarketSnapshot | undefined,
  healed: MarketSnapshot,
): MarketSnapshot | undefined {
  if (!current) return current;
  const fresh = new Map(
    healed.quotes.filter((quote) => !isUnavailable(quote)).map((quote) => [quote.symbol, quote]),
  );
  let changed = false;
  const quotes = current.quotes.map((quote) => {
    const replacement = fresh.get(quote.symbol);
    if (!replacement || replacement === quote) return quote;
    changed = true;
    return replacement;
  });
  if (!changed) return current;
  const previousAt = Date.parse(current.publishedAt ?? "");
  const healedAt = Date.parse(healed.publishedAt ?? "");
  const publishedAt =
    Number.isFinite(healedAt) && (!Number.isFinite(previousAt) || healedAt > previousAt)
      ? healed.publishedAt
      : current.publishedAt;
  return { ...current, quotes, publishedAt };
}
