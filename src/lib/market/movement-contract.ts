export const MOVEMENT_BUCKET_MS = 5_000;
export const MOVEMENT_OBSERVATION_BATCH_MAX = 20_000;
export const MAX_LAST_TRADE_AGE_SECONDS = 15;
export const MAX_LAST_TRADE_AGE_MS = MAX_LAST_TRADE_AGE_SECONDS * 1_000;
export const MOVEMENT_WINDOWS_MINUTES = [1, 5, 15] as const;

export type MovementWindowMinutes = (typeof MOVEMENT_WINDOWS_MINUTES)[number];
export type MovementReadinessStatus = "READY" | "WARMING" | "STALE";
export type MovementSourceState = "LIVE" | "RECOVERING" | "STALE" | "UNAVAILABLE";

export type MovementTradeInput = {
  symbol: string;
  aggregateId: number;
  price: number;
  quantity: number;
  eventTime: number;
  tradeTime: number;
  receivedAt: number;
};

export type MovementBucket = {
  boundaryTime: number;
  sourceState: MovementSourceState;
  endpointPrice: number | null;
  baseQuantity: number;
  quoteVolume: number;
  tradeCount: number;
  lastRealTradeTime: number | null;
  lastRealEventTime: number | null;
  lastRealReceivedAt: number | null;
  carriedForward: boolean;
  provider: "binance-usdm";
  instrumentId: string;
  nativeSymbol: string;
  symbol: string;
  marketType: "futures";
  contractType: "perpetual";
  priceType: "trade";
};

export type MovementWindowReadiness = {
  windowMinutes: MovementWindowMinutes;
  status: MovementReadinessStatus;
};

export type MovementBucketSnapshot = {
  symbol: string;
  provider: "binance-usdm";
  instrumentId: string;
  priceType: "trade";
  bucketMs: typeof MOVEMENT_BUCKET_MS;
  maxLastTradeAgeMs: typeof MAX_LAST_TRADE_AGE_MS;
  buckets: MovementBucket[];
  latestRealTradeTime: number | null;
  latestRealReceivedAt: number | null;
  readiness: Record<MovementWindowMinutes, MovementWindowReadiness>;
};

export type MovementBoundarySymbolInput = {
  symbol: string;
  sourceState: MovementSourceState;
  observations: MovementTradeInput[];
};

export type MovementBoundaryResult = {
  snapshots: MovementBucketSnapshot[];
  lateAfterFinalizationCount: number;
};