import type { Candle } from "../market/providers.server";
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

export interface OperationalStore {
  readonly enabled: boolean;
  recordCandles(batch: OperationalCandleBatch): Promise<void>;
  recordCheckpoint(checkpoint: OperationalCheckpoint): Promise<"recorded" | "already_processed">;
  recordMonitorRun(
    userId: string,
    run: Omit<OperationalMonitorRun, "id" | "ran_at">,
  ): Promise<void>;
  listMonitorRuns(userId: string, limit?: number): Promise<OperationalMonitorRun[]>;
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
}
