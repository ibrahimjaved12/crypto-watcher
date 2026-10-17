/**
 * Crypto/Forex switch (#374): pure selection rules shared by the provider and tests. No React, no DOM.
 * The switch is UI-only (the backend computes both markets); `my_markets()` decides what may be opened.
 */
import { MARKETS, parseMarket, type Market } from "./instruments";

export const MARKET_STORAGE_KEY = "cw.market";

export type MarketAccess = { entitled: readonly Market[] };

/** A market opens only when the user's access label lists it; nobody has access by default (#411). */
export function canOpen(market: Market, entitled: readonly string[]): boolean {
  return entitled.includes(market);
}

/** The stored choice when known and openable, else the first open market, else crypto (the gate screen shows). */
export function resolveMarket(stored: unknown, entitled: readonly string[]): Market {
  const market = parseMarket(stored, "crypto");
  if (canOpen(market, entitled)) return market;
  return MARKETS.find((m) => entitled.includes(m)) ?? "crypto";
}

/** Known markets only, de-duplicated, in MARKETS order; an unknown shape means no access. */
export function normalizeEntitled(value: unknown): Market[] {
  if (!Array.isArray(value)) return [];
  const listed = new Set(value.filter((item): item is string => typeof item === "string"));
  return MARKETS.filter((market) => listed.has(market));
}

export type AccessState = "loading" | "error" | "none" | "granted";

export function accessState(status: "loading" | "error" | "ready", entitled: readonly string[]): AccessState {
  if (status !== "ready") return status;
  return entitled.length ? "granted" : "none";
}

export const MARKET_META: Record<Market, { label: string }> = {
  crypto: { label: "Crypto" },
  forex: { label: "Forex" },
};
