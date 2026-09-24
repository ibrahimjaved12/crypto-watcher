import { createClient, type SupabaseClient } from "@supabase/supabase-js";
import { operationalDbConfig } from "./config.server";
import type {
  OperationalCandleBatch,
  CollectorCandle,
  CollectorHealth,
  CollectorStorageDiagnostics,
  OperationalCheckpoint,
  OperationalMonitorRun,
  OperationalStore,
  OutboxEvent,
  StorageDiagnostics,
} from "./types";

type RpcClient = Pick<SupabaseClient, "from" | "rpc">;

function createOperationalFetch(serviceRoleKey: string): typeof fetch {
  return (input, init) => {
    const headers = new Headers(
      typeof Request !== "undefined" && input instanceof Request ? input.headers : undefined,
    );
    if (init?.headers) new Headers(init.headers).forEach((value, key) => headers.set(key, value));
    if (
      serviceRoleKey.startsWith("sb_secret_") &&
      headers.get("Authorization") === `Bearer ${serviceRoleKey}`
    ) {
      headers.delete("Authorization");
    }
    headers.set("apikey", serviceRoleKey);
    return fetch(input, { ...init, headers });
  };
}

const disabledStore: OperationalStore = {
  enabled: false,
  async recordCandles() {},
  async recordCheckpoint() {
    throw new Error("Operational checkpoint ownership is disabled");
  },
  async recordMonitorRun() {
    throw new Error("Operational monitor-run ownership is disabled");
  },
  async listMonitorRuns() {
    return [];
  },
  async diagnostics() {
    return {
      recent_candle_rows: 0,
      checkpoint_rows: 0,
      monitor_run_rows: 0,
      pending_outbox_rows: 0,
      failed_outbox_rows: 0,
      dead_outbox_rows: 0,
      oldest_candle_at: null,
      oldest_undelivered_at: null,
    };
  },
  async recordCollectorCandles() {
    throw new Error("Operational collector ownership is disabled");
  },
  async recordCollectorHealth() {
    throw new Error("Operational collector ownership is disabled");
  },
  async listCollectorHealth() {
    return [];
  },
  async collectorDiagnostics() {
    return { candle_rows: 0, health_rows: 0, oldest_candle_at: null, newest_candle_at: null };
  },
  async claimCollectorLease() {
    return false;
  },
  async renewCollectorLease() {
    return false;
  },
  async releaseCollectorLease() {},
  async stageDurableResult() {
    throw new Error("Operational outbox is disabled");
  },
  async claimOutbox() {
    return [];
  },
  async markOutboxDelivered() {
    throw new Error("Operational outbox is disabled");
  },
  async markOutboxFailed() {
    throw new Error("Operational outbox is disabled");
  },
};

function nativeSymbol(source: string, symbol: string) {
  const base = symbol.replace(/USDT$/, "");
  if (source === "okx-usdt-swap") return `${base}-USDT-SWAP`;
  if (source === "kraken-futures") return `PF_${base === "BTC" ? "XBT" : base}USD`;
  return symbol;
}

function rpcError(error: { message: string } | null, operation: string): void {
  if (error) throw new Error(`Operational database ${operation} failed: ${error.message}`);
}

export function createOperationalStore(
  client: RpcClient,
  options: {
    candleRetentionDays: number;
    monitorRunRetentionDays: number;
    outboxMaxAttempts: number;
  },
): OperationalStore {
  return {
    enabled: true,
    async recordCandles(batch: OperationalCandleBatch) {
      const duration = batch.timeframeMinutes * 60_000;
      const rows = batch.candles
        .filter((candle) => candle.complete === true)
        .map((candle) => ({
          instrument_id: batch.instrumentId,
          symbol: batch.symbol,
          native_symbol: batch.nativeSymbol,
          source: batch.source,
          endpoint: batch.endpoint,
          price_type: batch.priceType,
          timeframe_minutes: batch.timeframeMinutes,
          open_time: new Date(candle.time).toISOString(),
          close_time: new Date(candle.time + duration).toISOString(),
          open: candle.open,
          high: candle.high,
          low: candle.low,
          close: candle.close,
          volume: candle.volume,
          source_event_at: new Date(candle.time + duration).toISOString(),
          collected_at: batch.retrievedAt,
        }));
      if (rows.length === 0) return;
      const { error } = await client.rpc("record_recent_candles", {
        p_user_id: batch.userId,
        p_rows: rows,
        p_retention_days: options.candleRetentionDays,
      });
      rpcError(error, "candle write");
    },
    async recordCheckpoint(checkpoint: OperationalCheckpoint) {
      const { data, error } = await client.rpc("record_market_data_checkpoint", {
        p_user_id: checkpoint.userId,
        p_instrument_id: checkpoint.instrumentId,
        p_symbol: checkpoint.symbol,
        p_native_symbol: checkpoint.nativeSymbol,
        p_source: checkpoint.source,
        p_endpoint: checkpoint.endpoint,
        p_price_type: checkpoint.priceType,
        p_timeframe_minutes: checkpoint.timeframeMinutes,
        p_observed_at: checkpoint.observedAt,
        p_price: checkpoint.price,
      });
      rpcError(error, "checkpoint write");
      const status = (data as { status?: "recorded" | "already_processed" } | null)?.status;
      if (!status) throw new Error("Operational database returned an invalid checkpoint status");
      return status;
    },
    async recordMonitorRun(userId, run) {
      const { error } = await client.rpc("record_monitor_run", {
        p_user_id: userId,
        p_status: run.status,
        p_symbols_checked: run.symbols_checked,
        p_alerts_created: run.alerts_created,
        p_data_source: run.data_source,
        p_error_message: run.error_message,
        p_duration_ms: run.duration_ms,
        p_metrics: run.metrics,
        p_retention_days: options.monitorRunRetentionDays,
      });
      rpcError(error, "monitor-run write");
    },
    async listMonitorRuns(userId, limit = 25) {
      const { data, error } = await client
        .from("monitor_runs")
        .select(
          "id, ran_at, status, symbols_checked, alerts_created, data_source, error_message, duration_ms, metrics",
        )
        .eq("user_id", userId)
        .order("ran_at", { ascending: false })
        .limit(Math.min(Math.max(limit, 1), 100));
      rpcError(error, "monitor-run read");
      return (data ?? []) as unknown as OperationalMonitorRun[];
    },
    async diagnostics(userId) {
      const { data, error } = await client.rpc("get_storage_diagnostics", {
        p_user_id: userId,
      });
      rpcError(error, "diagnostics read");
      const row = Array.isArray(data) ? data[0] : data;
      if (!row) throw new Error("Operational database returned no storage diagnostics");
      return row as StorageDiagnostics;
    },
    async recordCollectorCandles(candles: CollectorCandle[]) {
      if (candles.length === 0) return [];
      const { data, error } = await client.rpc("record_collector_candles", {
        p_rows: candles.map((candle) => ({
          instrument_id: candle.instrumentId,
          symbol: candle.symbol,
          native_symbol: candle.nativeSymbol,
          provider: candle.provider,
          endpoint: candle.endpoint,
          price_type: candle.priceType,
          timeframe_minutes: candle.timeframeMinutes,
          open_time: new Date(candle.openTime).toISOString(),
          close_time: new Date(candle.closeTime).toISOString(),
          open: candle.open,
          high: candle.high,
          low: candle.low,
          close: candle.close,
          volume: candle.volume,
          source_event_at: new Date(candle.sourceEventTime).toISOString(),
          received_at: new Date(candle.receivedAt).toISOString(),
          transport: candle.transport,
        })),
        p_retention_days: options.candleRetentionDays,
      });
      rpcError(error, "collector candle write");
      return ((data ?? []) as Array<{ candle_identity: string }>).map((row) => row.candle_identity);
    },
    async recordCollectorHealth(input) {
      const { error } = await client.rpc("record_collector_health", {
        p_instrument_id: input.instrumentId,
        p_symbol: input.symbol,
        p_timeframe_minutes: input.timeframeMinutes,
        p_status: input.status,
        p_last_event_at:
          input.lastEventAt === null ? null : new Date(input.lastEventAt).toISOString(),
        p_last_completed_open_time:
          input.lastCompletedOpenTime === null
            ? null
            : new Date(input.lastCompletedOpenTime).toISOString(),
        p_lag_ms: input.lagMs,
        p_queue_depth: input.queueDepth,
        p_reconnect_count: input.reconnectCount,
        p_error_message: input.errorMessage,
      });
      rpcError(error, "collector health write");
    },
    async listCollectorHealth(symbols) {
      if (symbols.length === 0) return [];
      const { data, error } = await client
        .from("collector_health")
        .select(
          "instrument_id, symbol, timeframe_minutes, status, last_event_at, last_completed_open_time, lag_ms, queue_depth, reconnect_count, error_message, updated_at",
        )
        .in("symbol", symbols)
        .order("symbol", { ascending: true })
        .order("timeframe_minutes", { ascending: true });
      rpcError(error, "collector health read");
      return (data ?? []) as unknown as CollectorHealth[];
    },
    async collectorDiagnostics() {
      const { data, error } = await client.rpc("get_collector_storage_diagnostics");
      rpcError(error, "collector diagnostics read");
      const row = Array.isArray(data) ? data[0] : data;
      if (!row) throw new Error("Operational database returned no collector diagnostics");
      return row as CollectorStorageDiagnostics;
    },
    async claimCollectorLease(instanceId, leaseSeconds = 60) {
      const { data, error } = await client.rpc("claim_collector_lease", {
        p_instance_id: instanceId,
        p_lease_seconds: leaseSeconds,
      });
      rpcError(error, "collector lease claim");
      return data === true;
    },
    async renewCollectorLease(instanceId, leaseSeconds = 60) {
      const { data, error } = await client.rpc("renew_collector_lease", {
        p_instance_id: instanceId,
        p_lease_seconds: leaseSeconds,
      });
      rpcError(error, "collector lease renewal");
      return data === true;
    },
    async releaseCollectorLease(instanceId) {
      const { error } = await client.rpc("release_collector_lease", {
        p_instance_id: instanceId,
      });
      rpcError(error, "collector lease release");
    },
    async stageDurableResult(input) {
      const { error } = await client.rpc("stage_durable_result", {
        p_result_id: input.resultId,
        p_event_id: input.eventId,
        p_user_id: input.userId,
        p_result_kind: input.resultKind,
        p_payload: input.payload,
      });
      rpcError(error, "outbox staging");
    },
    async claimOutbox(workerId, limit = 50) {
      const { data, error } = await client.rpc("claim_sync_outbox", {
        p_worker_id: workerId,
        p_limit: Math.min(Math.max(limit, 1), 100),
      });
      rpcError(error, "outbox claim");
      return ((data ?? []) as Array<Record<string, unknown>>).map((row) => ({
        eventId: String(row["event_id"]),
        resultId: String(row["result_id"]),
        userId: String(row["user_id"]),
        resultKind: String(row["result_kind"]),
        payload: row["payload"],
        attempts: Number(row["attempts"]),
      }));
    },
    async markOutboxDelivered(eventId, workerId) {
      const { error } = await client.rpc("mark_sync_outbox_delivered", {
        p_event_id: eventId,
        p_worker_id: workerId,
      });
      rpcError(error, "outbox delivery confirmation");
    },
    async markOutboxFailed(eventId, workerId, failure) {
      const { error } = await client.rpc("mark_sync_outbox_failed", {
        p_event_id: eventId,
        p_worker_id: workerId,
        p_error: failure.slice(0, 2_000),
        p_max_attempts: options.outboxMaxAttempts,
      });
      rpcError(error, "outbox failure update");
    },
  };
}

let singleton: OperationalStore | undefined;

export function getOperationalStore(): OperationalStore {
  if (singleton) return singleton;
  const config = operationalDbConfig();
  if (!config.enabled) return (singleton = disabledStore);
  const client = createClient(config.url, config.serviceRoleKey, {
    global: { fetch: createOperationalFetch(config.serviceRoleKey) },
    auth: { persistSession: false, autoRefreshToken: false, detectSessionInUrl: false },
  });
  singleton = createOperationalStore(client, config);
  return singleton;
}

export function operationalNativeSymbol(source: string, symbol: string): string {
  return nativeSymbol(source, symbol);
}

/** Test-only reset for environment/config isolation. */
export function resetOperationalStore(): void {
  singleton = undefined;
}
