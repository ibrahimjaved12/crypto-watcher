/** Last used market (#467): only a default for entry redirects; the URL's first segment is the market. Client only. */
import { parseMarket, type Market } from "./instruments";
import { MARKET_STORAGE_KEY } from "./market-select";

export function readStoredMarket(): Market {
  try {
    return parseMarket(window.localStorage.getItem(MARKET_STORAGE_KEY), "crypto");
  } catch {
    return "crypto"; // private windows and blocked storage
  }
}

export function writeStoredMarket(market: Market) {
  try {
    window.localStorage.setItem(MARKET_STORAGE_KEY, market);
  } catch {
    // the URL still carries the market
  }
}
