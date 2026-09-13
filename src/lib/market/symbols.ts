/** Supported trading pairs (USDT quoted). Shared by client and server. */
export const SUPPORTED_SYMBOLS = [
  "BTCUSDT",
  "ETHUSDT",
  "DOGEUSDT",
  "SOLUSDT",
  "XRPUSDT",
  "ADAUSDT",
  "BNBUSDT",
  "AVAXUSDT",
  "LINKUSDT",
  "MATICUSDT",
  "DOTUSDT",
  "LTCUSDT",
  "TRXUSDT",
  "ATOMUSDT",
  "NEARUSDT",
  "APTUSDT",
  "ARBUSDT",
  "OPUSDT",
  "SUIUSDT",
  "TONUSDT",
] as const;

export type SupportedSymbol = (typeof SUPPORTED_SYMBOLS)[number];

export const DEFAULT_SYMBOLS: string[] = ["BTCUSDT", "ETHUSDT", "DOGEUSDT"];

export const MAX_WATCHLIST_SIZE = 10;

export const CHANGE_WINDOWS = [5, 15, 60, 240, 1440] as const;
export type ChangeWindow = (typeof CHANGE_WINDOWS)[number];

export const WINDOW_LABELS: Record<number, string> = {
  5: "5m",
  15: "15m",
  60: "1h",
  240: "4h",
  1440: "24h",
};

export function isSupportedSymbol(symbol: string): boolean {
  return (SUPPORTED_SYMBOLS as readonly string[]).includes(symbol.toUpperCase());
}

export function baseAsset(symbol: string): string {
  return symbol.replace(/USDT$/, "");
}
