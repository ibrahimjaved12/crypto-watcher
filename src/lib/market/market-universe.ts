/**
 * Versioned market-wide universe for the movement engine (Issue #74).
 *
 * The universe is the union of the supported, watched Binance USDⓈ-M perpetual
 * contracts — one shared denominator, not one per browser/account. Its version is
 * a deterministic hash of the sorted membership so any membership change yields a
 * new version; #73 then terminates the prior episode with `universe_changed`
 * rather than silently redefining an in-flight episode's denominator.
 */
import { createHash } from "node:crypto";
import { MARKET_UNIVERSE_ID } from "./market-movement-state";

export const MARKET_UNIVERSE_VERSION_NAMESPACE = "market-universe-v1";

export type MarketUniverse = {
  id: string;
  version: string;
  symbols: string[];
};

/** Deterministic version derived only from the normalized symbol membership. */
export function computeMarketUniverseVersion(symbols: readonly string[]): string {
  const normalized = [...new Set(symbols.map((symbol) => symbol.toUpperCase()))].sort();
  const payload = [MARKET_UNIVERSE_VERSION_NAMESPACE, ...normalized].join("|");
  const hash = createHash("sha256").update(payload, "utf8").digest("hex").slice(0, 16);
  return `${MARKET_UNIVERSE_VERSION_NAMESPACE}:${hash}`;
}

export function buildMarketUniverse(symbols: Iterable<string>): MarketUniverse {
  const normalized = [...new Set([...symbols].map((symbol) => symbol.toUpperCase()))].sort();
  return {
    id: MARKET_UNIVERSE_ID,
    version: computeMarketUniverseVersion(normalized),
    symbols: normalized,
  };
}
