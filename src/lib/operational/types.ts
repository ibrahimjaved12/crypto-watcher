import type { Candle } from "../market/providers.server";
import type {
  ConfirmedMarketDirection,
  MarketDirectionState,
  MarketPace,
} from "../market/market-state-classifier";
import type {
  MarketEpisodeTransitionType,
  SerializedMarketEpisodeLifecycleState,
} from "../market/market-episode-lifecycle";
import type {
  MovementCandle,
  PersistedMarketMovementCurrentEvidence,
} from "../market/market-movement-state";
import type { MonitorMetrics } from "../monitor/run-context";

export type OperationalCandleBatch = {
  userId: string;
  instrumentId: string;
  symbol: string;
  nativeSymbol: string;
  source: string;
  endpoint: string;
  priceType: string;
  timeframeMinutes: number;
  retrievedAt: string;
  candles: Candle[];
};

export type CollectorCandle = {
  instrumentId: string;
  symbol: string;
  nativeSymbol: string;
  provider: "binance-usdm";
  endpoint: string;
  priceType: "trade";
  timeframeMinutes: 1 | 15 | 60 | 240;
  openTime: number;
  closeTime: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  sourceEventTime: number;
  receivedAt: number;
  transport: "rest" | "websocket";
};

export type CollectorHealthStatus = "LIVE" | "RECOVERING" | "STALE" | "UNAVAILABLE";

export type CollectorHealth = {
  instrument_id: string;
  symbol: string;
  timeframe_minutes: number;
  status: CollectorHealthStatus;
  last_event_at: string | null;
  last_completed_open_time: string | null;
  lag_ms: number | null;
  queue_depth: number;
  reconnect_count: number;
  error_message: string | null;
  updated_at: string;
};

export type CollectorStorageDiagnostics = {
  candle_rows: number;
  health_rows: number;
  oldest_candle_at: string | null;
  newest_candle_at: string | null;
};

export type OperationalCheckpoint = {
  userId: string;
  instrumentId: string;
  symbol: string;
  nativeSymbol: string;
  source: string;
  endpoint: string;
  priceType: string;
  timeframeMinutes: number;
  observedAt: string;
  price: number;
};

export type OperationalMonitorRun = {
  id: string;
  ran_at: string;
  status: string;
  symbols_checked: number;
  alerts_created: number;
  data_source: string | null;
  error_message: string | null;
  duration_ms: number | null;
  metrics: MonitorMetrics;
};

export type StorageDiagnostics = {
  recent_candle_rows: number;
  checkpoint_rows: number;
  monitor_run_rows: number;
  pending_outbox_rows: number;
  failed_outbox_rows: number;
  dead_outbox_rows: number;
  oldest_candle_at: string | null;
  oldest_undelivered_at: string | null;
};

export type OutboxEvent = {
  eventId: string;
  resultId: string;
  userId: string;
  resultKind: string;
  payload: unknown;
  attempts: number;
};

export type PersistedMarketStateCurrent = {
  universeId: string;
  primaryWindowMinutes: 5;
  universeVersion: string;
  provider: "binance-usdm";
  exchange: "binance";
  priceType: "trade";
  evaluationBoundaryTime: number;
  directionState: MarketDirectionState;
  pace: MarketPace;
  activeEpisodeId: string | null;
  activeDirection: ConfirmedMarketDirection | null;
  interrupted: boolean;
  episodeAlgorithmVersion: string;
  lifecycleConfigVersion: string;
  classifierAlgorithmVersion: string;
  classifierConfigVersion: string;
  movementAlgorithmVersion: string;
  movementConfigVersion: string;
  lifecycleState: SerializedMarketEpisodeLifecycleState;
  currentEvidence: PersistedMarketMovementCurrentEvidence;
  updatedAt?: string;
};

export type PersistedMarketMovementEvent = {
  eventId: string;
  episodeId: string;
  episodeAlgorithmVersion: string;
  lifecycleConfigVersion: string;
  transition: MarketEpisodeTransitionType;
  transitionReason: string;
  fromDirection: ConfirmedMarketDirection | null;
  toDirection: ConfirmedMarketDirection | null;
  episodeStartBoundaryTime: number;
  evaluationBoundaryTime: number;
  universeId: string;
  universeVersion: string;
  primaryWindowMinutes: 5;
  provider: "binance-usdm";
  exchange: "binance";
  priceType: "trade";
  direction: ConfirmedMarketDirection;
  pace: MarketPace;
  directionalBreadth: number;
  materialBreadth: number;
  medianRawReturn: number | null;
  medianNormalizedMovement: number | null;
  medianAcceleration: number | null;
  accelerationBreadth: number;
  dispersion: number | null;
  rvolSummary: unknown;
  outliers: unknown;
  supportingContracts: string[];
  conflictingContracts: string[];
  configuredUniverse: string[];
  includedSymbols: string[];
  excludedSymbols: unknown;
  windowsContext: unknown;
  classifierAlgorithmVersion: string;
  classifierConfigVersion: string;
  movementAlgorithmVersion: string;
  movementConfigVersion: string;
  createdAt?: string;
};

export type MarketEpisodePersistenceStatus = {
  eventId: string;
  status: "appended" | "already_exists";
};

export interface OperationalStore {
  readonly enabled: boolean;
  recordCandles(batch: OperationalCandleBatch): Promise<void>;
  recordCheckpoint(checkpoint: OperationalCheckpoint): Promise<"recorded" | "already_processed">;
  recordMonitorRun(
    userId: string,
    run: Omit<OperationalMonitorRun, "id" | "ran_at">,
  ): Promise<void>;
  listMonitorRuns(userId: string, limit?: number): Promise<OperationalMonitorRun[]>;
  latestCheckpoint(userId: string): Promise<string | null>;
  diagnostics(userId: string): Promise<StorageDiagnostics>;
  recordCollectorCandles(candles: CollectorCandle[]): Promise<string[]>;
  recordCollectorHealth(input: {
    instrumentId: string;
    symbol: string;
    timeframeMinutes: 1 | 15 | 60 | 240;
    status: CollectorHealthStatus;
    lastEventAt: number | null;
    lastCompletedOpenTime: number | null;
    lagMs: number | null;
    queueDepth: number;
    reconnectCount: number;
    errorMessage: string | null;
  }): Promise<void>;
  listCollectorHealth(symbols: string[]): Promise<CollectorHealth[]>;
  collectorDiagnostics(): Promise<CollectorStorageDiagnostics>;
  /**
   * Derived collector input: the shared symbol set the application assigns to the
   * collector worker. The application owns user watchlists; the collector reads
   * this operational representation instead of querying Lovable user tables.
   */
  assignCollectorSubscriptions(symbols: string[]): Promise<void>;
  readCollectorSubscriptions(): Promise<string[]>;
  /** Canonical completed one-minute candles used to derive #71 normalization history. */
  readMovementCandleHistory(
    symbols: string[],
    sinceMs: number,
  ): Promise<Map<string, MovementCandle[]>>;
  claimCollectorLease(instanceId: string, leaseSeconds?: number): Promise<boolean>;
  renewCollectorLease(instanceId: string, leaseSeconds?: number): Promise<boolean>;
  releaseCollectorLease(instanceId: string): Promise<void>;
  stageDurableResult(input: {
    resultId: string;
    eventId: string;
    userId: string;
    resultKind: string;
    payload: unknown;
  }): Promise<void>;
  claimOutbox(workerId: string, limit?: number): Promise<OutboxEvent[]>;
  markOutboxDelivered(eventId: string, workerId: string): Promise<void>;
  markOutboxFailed(eventId: string, workerId: string, error: string): Promise<void>;
  getMarketStateCurrent(
    universeId: string,
    primaryWindowMinutes?: number,
  ): Promise<PersistedMarketStateCurrent | null>;
  upsertMarketStateCurrent(state: PersistedMarketStateCurrent): Promise<void>;
  appendMarketMovementEvent(
    event: PersistedMarketMovementEvent,
  ): Promise<"appended" | "already_exists">;
  persistMarketEpisodeLifecycleStep(
    state: PersistedMarketStateCurrent,
    events: readonly PersistedMarketMovementEvent[],
  ): Promise<MarketEpisodePersistenceStatus[]>;
  listMarketMovementEvents(episodeId: string): Promise<PersistedMarketMovementEvent[]>;
}
