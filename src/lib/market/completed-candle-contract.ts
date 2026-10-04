/** Native collector candle facts. Python completed-candle-v1 admits only 1m;
 * the operational reader also exposes native 15m/1h/4h to existing TA consumers. */
export const COMPLETED_CANDLE_CONTRACT_VERSION = "completed-candle-v1" as const;
export type CollectorCandleTimeframe = 1 | 15 | 60 | 240;
export type CompletedCandleSeriesIdentity<T extends CollectorCandleTimeframe = 1> = {
  provider: "binance-usdm";
  exchange: "binance";
  marketType: "futures";
  contractType: "perpetual";
  instrumentId: string;
  symbol: string;
  nativeSymbol: string;
  priceType: "trade";
  seriesBasis: "native-kline";
  timeframeMinutes: T;
};
export type CompletedCandleMarketData = {
  openTime: number;
  closeTime: number;
  open: number;
  high: number;
  low: number;
  close: number;
  baseVolume: number;
  quoteVolume: number;
};
export type WebSocketCandleProvenance = {
  sourceKind: "websocket";
  endpoint: string;
  sourceEventTime: number;
  receivedAt: number;
};
export type RestCandleProvenance = {
  sourceKind: "rest";
  endpoint: string;
  retrievedAt: number;
};
export type ArchiveCandleProvenance = {
  sourceKind: "archive";
  datasetId: string;
  datasetVersion: string;
  datasetContentSha256: string;
};
export type CompletedCandleObservation = {
  candle: CompletedCandleMarketData;
  provenance: WebSocketCandleProvenance | RestCandleProvenance | ArchiveCandleProvenance;
};
export type CompletedCandleSeries<T extends CollectorCandleTimeframe = 1> = {
  contractVersion: typeof COMPLETED_CANDLE_CONTRACT_VERSION;
  identity: CompletedCandleSeriesIdentity<T>;
  observations: CompletedCandleObservation[];
};
export function completedCandleIdentity<T extends CollectorCandleTimeframe>(
  symbol: string, timeframeMinutes: T,
): CompletedCandleSeriesIdentity<T> {
  return { provider: "binance-usdm", exchange: "binance", marketType: "futures",
    contractType: "perpetual", instrumentId: `binance-usdm:${symbol}`, symbol,
    nativeSymbol: symbol, priceType: "trade", seriesBasis: "native-kline", timeframeMinutes };
}
const timestamp = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const text = (value: unknown): value is string => typeof value === "string" && value.trim().length > 0;
function exactKeys(value: object, keys: string[]) {
  return Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));
}

/** Validate factual structure; completion has already been attested by ingestion. */
export function validateCompletedCandleSeries<T extends CollectorCandleTimeframe>(
  series: CompletedCandleSeries<T>, operational = false,
): CompletedCandleSeries<T> {
  const id = series.identity;
  if (series.contractVersion !== COMPLETED_CANDLE_CONTRACT_VERSION ||
      id.provider !== "binance-usdm" || id.exchange !== "binance" ||
      id.marketType !== "futures" || id.contractType !== "perpetual" ||
      id.priceType !== "trade" || id.seriesBasis !== "native-kline" ||
      !/^[A-Z0-9]+USDT$/.test(id.nativeSymbol) || id.symbol !== id.nativeSymbol ||
      id.instrumentId !== `binance-usdm:${id.nativeSymbol}` ||
      !(operational ? [1, 15, 60, 240] : [1]).includes(id.timeframeMinutes)) {
    throw new Error("Invalid completed candle identity");
  }
  for (const { candle: c, provenance: p } of series.observations) {
    const duration = id.timeframeMinutes * 60_000;
    if (!timestamp(c.openTime) || !timestamp(c.closeTime) || !timestamp(c.closeTime + 1) ||
        c.openTime % duration !== 0 || c.closeTime !== c.openTime + duration - 1 ||
        ![c.open, c.high, c.low, c.close].every((value) => Number.isFinite(value) && value > 0) ||
        ![c.baseVolume, c.quoteVolume].every((value) => Number.isFinite(value) && value >= 0) ||
        c.low > Math.min(c.open, c.close) || c.high < Math.max(c.open, c.close)) {
      throw new Error("Invalid completed candle market facts");
    }
    if (p.sourceKind === "websocket") {
      if (!exactKeys(p, ["sourceKind", "endpoint", "sourceEventTime", "receivedAt"]) ||
          !text(p.endpoint) || !timestamp(p.sourceEventTime) || !timestamp(p.receivedAt))
        throw new Error("Invalid WebSocket candle provenance");
    } else if (p.sourceKind === "rest") {
      if (!exactKeys(p, ["sourceKind", "endpoint", "retrievedAt"]) ||
          !text(p.endpoint) || !timestamp(p.retrievedAt)) throw new Error("Invalid REST candle provenance");
    } else if (p.sourceKind === "archive") {
      if (!exactKeys(p, ["sourceKind", "datasetId", "datasetVersion", "datasetContentSha256"]) ||
          !text(p.datasetId) || !text(p.datasetVersion) || !/^[0-9a-f]{64}$/.test(p.datasetContentSha256))
        throw new Error("Invalid archive candle provenance");
    } else throw new Error("Invalid candle provenance");
  }
  completedMarketCandles(series);
  return series;
}
export function completedMarketCandles(series: CompletedCandleSeries<CollectorCandleTimeframe>) {
  const candles = new Map<number, CompletedCandleMarketData>();
  const fields = ["openTime", "closeTime", "open", "high", "low", "close", "baseVolume", "quoteVolume"] as const;
  for (const { candle } of series.observations) {
    const prior = candles.get(candle.openTime);
    if (prior && fields.some((key) => prior[key] !== candle[key]))
      throw new Error("Conflicting completed candle market facts");
    candles.set(candle.openTime, candle);
  }
  return [...candles.values()].sort((a, b) => a.openTime - b.openTime);
}
export function missingCompletedCandleOpenTimes(series: CompletedCandleSeries<CollectorCandleTimeframe>) {
  const candles = completedMarketCandles(series);
  const missing: number[] = [];
  for (let index = 1; index < candles.length; index++) {
    for (let time = candles[index - 1]!.openTime + series.identity.timeframeMinutes * 60_000;
         time < candles[index]!.openTime; time += series.identity.timeframeMinutes * 60_000) missing.push(time);
  }
  return missing;
}
