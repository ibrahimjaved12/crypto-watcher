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
