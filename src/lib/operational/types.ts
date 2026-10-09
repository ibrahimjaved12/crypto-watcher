import type { CompletedCandleSeries, CollectorCandleTimeframe } from "../market/completed-candle-contract";
import type { Candle } from "../market/providers.server";
import type {
  ConfirmedMarketDirection,
  MarketDirectionState,
} from "../market/market-state-contract";
import type {
  CanonicalMarketEpisodeTransition, MarketPace,
  SerializedMarketEpisodeLifecycleState,
} from "../market/market-episode-contract";
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
  quoteVolume: number;
  /**
   * Actual exchange event time for this candle. WebSocket klines carry the
   * exchange event timestamp (`E`); REST bootstrap/recovery has no exchange event,
   * so the absence is recorded as null rather than an invented timestamp. Candle
   * completion is defined by `openTime + timeframeMinutes`, never by this value.
   */
  sourceEventTime: number | null;
  receivedAt: number;
  transport: "rest" | "websocket";
};

/** Completed 1m collector candle as sent to the forward engine (#239); doubles, provenance kept. */
export type ForwardMinuteRow = {
  open_time_ms: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  transport: "rest" | "websocket";
  source_event_at_ms: number | null;
};

/** One completed UTC-day kline of the forward trend feed (#239 P14); prices are exact decimal text. */
export type ForwardDailyBar = {
  symbol: string;
  day_ms: number;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
  quote_volume: string;
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

export type PersistedMarketMovementEvent = CanonicalMarketEpisodeTransition;

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
  /**
   * Canonical completed candles the leased collector already persisted, read back
   * for application consumers (#91), including the unchanged TA path (#20).
   * Each observation keeps the
   * provenance the collector recorded (#24/#26) — endpoint, transport, candle close
   * time, source event time and receive time — so the application never fabricates
   * it. While collector mode is active this replaces a second live exchange candle
   * fetch; missing or stale history must surface as a visible TA failure, never a
   * silent fallback.
   */
  readCollectorCompletedCandles<T extends CollectorCandleTimeframe>(
    symbol: string,
    timeframeMinutes: T,
    limit?: number,
  ): Promise<CompletedCandleSeries<T>>;
  /** Completed one-minute candles of one symbol in [sinceMs, beforeMs) for the forward engine (#239). */
  readForwardMinuteCandles(symbol: string, sinceMs: number, beforeMs: number): Promise<ForwardMinuteRow[]>;
  /** Stored completed daily bars of one symbol from `sinceMs` (a UTC day start), oldest first (#239 P14). */
  readForwardDailyBars(symbol: string, sinceMs: number): Promise<ForwardDailyBar[]>;
  /** Append completed daily bars; an existing (symbol, day) is never changed. Returns rows inserted. */
  recordForwardDailyBars(bars: ForwardDailyBar[], receivedAtMs: number): Promise<number>;
  /** Canonical completed one-minute candles used to derive #71 normalization history. */
  readMovementCandleHistory(
    symbols: string[],
    sinceMs: number,
    beforeBoundaryMs: number,
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
