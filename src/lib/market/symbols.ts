/** Supported Binance USDⓈ-M USDT perpetual contracts. Shared by client and server. */
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
  "POLUSDT",
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

export const MARKET_SOURCE = "binance-usdm" as const;
export const MARKET_SOURCES = [MARKET_SOURCE, "okx-usdt-swap", "kraken-futures"] as const;
export type MarketSource = (typeof MARKET_SOURCES)[number];
export const MARKET_PRICE_TYPE = "trade" as const;

export function isMarketSource(source: string): source is MarketSource {
  return (MARKET_SOURCES as readonly string[]).includes(source);
}

export type FuturesInstrument = {
  id: string;
  exchange: "binance";
  nativeSymbol: SupportedSymbol;
  marketType: "futures";
  contractType: "perpetual";
  baseAsset: string;
  quoteAsset: "USDT";
  marginAsset: "USDT";
  settlementAsset: "USDT";
  linear: true;
  contractMultiplier: 1;
};

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

export function instrumentId(symbol: string): string {
  if (!isSupportedSymbol(symbol)) throw new Error(`Unsupported futures contract: ${symbol}`);
  return `${MARKET_SOURCE}:${symbol.toUpperCase()}`;
}

export function futuresInstrument(symbol: string): FuturesInstrument {
  const nativeSymbol = symbol.toUpperCase() as SupportedSymbol;
  if (!isSupportedSymbol(nativeSymbol)) throw new Error(`Unsupported futures contract: ${symbol}`);
  return {
    id: instrumentId(nativeSymbol),
    exchange: "binance",
    nativeSymbol,
    marketType: "futures",
    contractType: "perpetual",
    baseAsset: baseAsset(nativeSymbol),
    quoteAsset: "USDT",
    marginAsset: "USDT",
    settlementAsset: "USDT",
    linear: true,
    contractMultiplier: 1,
  };
}
