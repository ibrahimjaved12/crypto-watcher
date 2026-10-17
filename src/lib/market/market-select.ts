/**
 * Crypto/Forex switch (#374): pure selection rules shared by the provider and tests. No React, no DOM.
 * The switch is UI-only (the backend computes both markets); `my_markets()` decides what may be opened.
 */
import { MARKETS, parseMarket, type Market } from "./instruments";

export const MARKET_STORAGE_KEY = "cw.market";

export type MarketAccess = { entitled: readonly Market[] };

/** "crypto" is always openable; "forex" only when the user's entitlements list it. */
export function canOpen(market: Market, entitled: readonly string[]): boolean {
  return market === "crypto" || entitled.includes(market);
}

/** The stored choice when it is a known market the user may open, otherwise crypto. */
export function resolveMarket(stored: unknown, entitled: readonly string[]): Market {
  const market = parseMarket(stored, "crypto");
  return canOpen(market, entitled) ? market : "crypto";
}

/** Known markets only, de-duplicated, in MARKETS order; an unknown shape means crypto only. */
export function normalizeEntitled(value: unknown): Market[] {
  if (!Array.isArray(value)) return ["crypto"];
  const listed = new Set(value.filter((item): item is string => typeof item === "string"));
  return MARKETS.filter((market) => listed.has(market));
}

export const MARKET_META: Record<Market, { label: string; tagline: string; icon: "bitcoin" | "globe" }> = {
  crypto: { label: "Crypto", tagline: "24/7 perpetual futures", icon: "bitcoin" },
  forex: { label: "Forex", tagline: "Weekday FX, metals and oil", icon: "globe" },
};
