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
  /** candle-v1 version hash (collector_candle_hash); never sent to Python (P15). */
  candle_hash?: string | undefined;
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
  /** Candle conflicts in the last 24 h (P15). Reported only; never degrades `status`. */
  conflict_count_24h?: number | undefined;
  last_conflict_at?: string | null | undefined;
  updated_at: string;
};

export type CollectorStorageDiagnostics = {
  candle_rows: number;
  health_rows: number;
  oldest_candle_at: string | null;
  newest_candle_at: string | null;
};

/** Completed 1-minute forex BID bar from the MT5 gateway (#395); the row contract is fx/bars.py. */
export type FxBar = {
  venue: string;
  instrumentId: string;
  symbol: string;
  nativeSymbol: string;
  /** UTC minute boundary (ms); the bar covers [openTime, openTime + 1 minute). */
  openTime: number;
  open: number;
  high: number;
  low: number;
  close: number;
  /** ask - bid in MT5 points (SYMBOL_POINT = 10**-digits). */
  spreadPoints: number;
  tickVolume: number;
  transport: "poll" | "history" | "stream";
  receivedAt: number;
};

/** One stored bar exactly as get_fx_bars returns it: the gateway row shape fx_bars_from_rows accepts. */
export type FxGatewayRow = {
  time_basis: "utc";
  venue: string;
  instrument_id: string;
  symbol: string;
  native_symbol: string;
  transport: "poll" | "history" | "stream";
  open_time_ms: number;
  open: number;
  high: number;
  low: number;
  close: number;
  spread_points: number;
  tick_volume: number;
  received_at_ms: number;
};

/** A closed market is MARKET_CLOSED, never STALE; LOGIN_FAILED covers an expired demo account or a bad password. */
export type FxCollectorHealthStatus =
  | "LIVE" | "RECOVERING" | "STALE" | "UNAVAILABLE" | "MARKET_CLOSED" | "LOGIN_FAILED";

export type FxCollectorHealthRow = {
  venue: string;
  instrument_id: string;
  status: FxCollectorHealthStatus;
  last_bar_open_time_ms: number | null;
  lag_ms: number | null;
  error_message: string | null;
  /** Bar conflicts in the last 24 h. Reported only; never degrades `status`. */
  conflict_count_24h: number;
  last_conflict_at_ms: number | null;
  updated_at_ms: number;
};

export type FxCollectorHealthInput = {
  venue: string;
  instrumentId: string;
  status: FxCollectorHealthStatus;
  lastBarOpenTime: number | null;
  lagMs: number | null;
  errorMessage: string | null;
};

export type FxSubscription = {
  venue: string;
  instrumentId: string;
  nativeSymbol: string;
};

export type FxBarCoverage = {
  venue: string;
  instrument_id: string;
  bar_count: number;
  oldest_open_time_ms: number;
  newest_open_time_ms: number;
};

/** One gap-scan request the gateway answered 200 for (#457): [from_ms, to_ms), as get_fx_gap_checks returns it. */
export type FxGapCheck = {
  from_ms: number;
  to_ms: number;
  checked_at_ms: number;
  /** Missing open minutes inside the range that were asked for. */
  requested_minutes: number;
  /** Valid bars the gateway returned for those minutes; fewer than requested = minutes MT5 has no bar for. */
  returned_minutes: number;
};

export type FxGapCheckInput = {
  venue: string;
  instrumentId: string;
  fromMs: number;
  toMs: number;
  requestedMinutes: number;
  returnedMinutes: number;
};

/** One economic calendar observation as the fetcher records it (#401). A reschedule or re-rating is a new observation. */
export type FxCalendarObservation = {
  source: string;
  sourceEventKey: string;
  /** Scheduled event time, UTC epoch ms. */
  eventTs: number;
  currency: string;
  impact: "low" | "medium" | "high";
  title: string;
  /** forecast/previous/actual as the source gives them; not used by the blackout filter. */
  detail?: Record<string, unknown>;
};

/** One fetch attempt for the fail-closed coverage check; an ok fetch says how far its response covers. */
export type FxCalendarFetchResult =
  | { source: string; status: "ok"; eventCount: number; coveredTo: number; error?: string | null }
  | { source: string; status: "error"; eventCount: number; coveredTo?: null; error: string | null };

/** Exactly a news_blackout.CalendarEvent, as get_fx_calendar_events returns it (point in time by first_seen_ms). */
export type FxCalendarEventRow = {
  event_id: string;
  ts_ms: number;
  currency: string;
  impact: "low" | "medium" | "high";
  title: string;
  first_seen_ms: number;
  source: string;
  tags: string[];
};

/** One source's fetch history from get_fx_calendar_status (null when there is none). */
export type FxCalendarSourceStatus = {
  source: string;
  last_ok_ms: number | null;
  last_ok_covered_to_ms: number | null;
  last_error_ms: number | null;
  last_error: string | null;
};

/**
 * One forex symbol contract spec from the broker (#432); never guessed. `tickValue` is in
 * `accountCurrency` (MT5 reports it in the account currency). On reads, `specHash` is the stored
 * version identity and `observedAt` is epoch ms; on writes the database computes the hash.
 */
export type FxSymbolSpec = {
  venue: string;
  instrumentId: string;
  nativeSymbol: string;
  digits: number;
  point: number;
  tickSize: number;
  tickValue: number;
  contractSize: number;
  volumeMin: number;
  volumeMax: number;
  volumeStep: number;
  /** Not always an ISO currency (XAU, USOIL). */
  baseCurrency: string;
  profitCurrency: string;
  marginCurrency: string;
  accountCurrency: string;
  tradeMode: number;
  swapMode: number;
  swapLong: number;
  swapShort: number;
  swapRollover3days: number;
  specHash: string;
  observedAt: number;
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

export type CandleConflict = {
  conflict_id: string;
  symbol: string;
  timeframe_minutes: number;
  open_time: string;
  rest_side: string | null;
  differing_fields: string[];
  detected_at: string;
  revision_id: string | null;
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
  /** P15 policy: append REST revisions for conflicts older than `olderThanHours` that agree within the stated tolerance. Returns revisions written. */
  reconcileConflicts(olderThanHours?: number): Promise<number>;
  /** Self-check: which of these tables have no trigger rejecting UPDATE/DELETE (empty = all protected). */
  missingAppendOnlyTriggers(tables: string[]): Promise<string[]>;
  /** Recent candle conflicts, newest first, with the revision id when one exists (diagnostics). */
  listCandleConflicts(hours?: number, limit?: number): Promise<CandleConflict[]>;
  collectorDiagnostics(): Promise<CollectorStorageDiagnostics>;
  /**
   * Derived collector input: the shared symbol set the application assigns to the
   * collector worker. The application owns user watchlists; the collector reads
   * this operational representation instead of querying main-database user tables.
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
  firstForwardMinute?(symbol: string): Promise<number | null>;
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
  /** Completed forex minutes (#395). Stored bars are immutable; returns `venue|instrument_id|open_ms` of new bars. */
  recordFxBars(bars: FxBar[]): Promise<string[]>;
  /**
   * Newest `limit` bars (default 1000, max 20000) with open time >= `fromMs` and < `toMs` (EXCLUSIVE; page
   * backwards by passing the oldest open time of the previous page), oldest first, as stored.
   */
  getFxBars(
    venue: string,
    instrumentId: string,
    options?: { fromMs?: number; toMs?: number; limit?: number },
  ): Promise<FxGatewayRow[]>;
  recordFxCollectorHealth(rows: FxCollectorHealthInput[]): Promise<void>;
  getFxCollectorHealth(): Promise<FxCollectorHealthRow[]>;
  assignFxSubscriptions(instruments: FxSubscription[]): Promise<void>;
  getFxSubscriptions(): Promise<FxSubscription[]>;
  claimFxCollectorLease(instanceId: string, leaseSeconds?: number): Promise<boolean>;
  renewFxCollectorLease(instanceId: string, leaseSeconds?: number): Promise<boolean>;
  releaseFxCollectorLease(instanceId: string): Promise<void>;
  /** Stored range per series; what is missing needs the trading calendar and is not decided here. */
  getFxBarCoverage(): Promise<FxBarCoverage[]>;
  /** Append gap-scan checks (#457); 1..100 rows per call. */
  recordFxGapChecks(rows: FxGapCheckInput[]): Promise<void>;
  /** Checks of one series made at or after `sinceMs`, newest first, at most 500. */
  getFxGapChecks(venue: string, instrumentId: string, sinceMs: number): Promise<FxGapCheck[]>;
  /** Append calendar observations (#401); known ones are skipped. Returns the number of new observations. */
  recordFxCalendar(rows: FxCalendarObservation[]): Promise<number>;
  recordFxCalendarFetch(result: FxCalendarFetchResult): Promise<void>;
  /** Events with time in [fromMs, toMs] (at most 120 days); with `asOfMs` only those first seen by then. */
  getFxCalendarEvents(fromMs: number, toMs: number, asOfMs?: number): Promise<FxCalendarEventRow[]>;
  getFxCalendarStatus(): Promise<FxCalendarSourceStatus[]>;
  /** Append broker contract specs (#432); an unchanged spec is skipped. Returns the number of new observations. */
  recordFxSymbolSpecs(specs: Omit<FxSymbolSpec, "observedAt" | "specHash">[]): Promise<number>;
  /** The latest spec per (venue, instrument) observed at or before `asOfMs` (default now). */
  getFxSymbolSpecs(asOfMs?: number): Promise<FxSymbolSpec[]>;
}
