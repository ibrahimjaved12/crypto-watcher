import { COMPLETED_CANDLE_CONTRACT_VERSION, completedCandleIdentity, validateCompletedCandleSeries,
  type CompletedCandleObservation } from "../market/completed-candle-contract";
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
  PersistedMarketMovementEvent,
  PersistedMarketStateCurrent,
  StorageDiagnostics,
} from "./types";
import type { MovementCandle } from "../market/market-movement-state";
import { MARKET_PRICE_TYPE, MARKET_SOURCE } from "../market/symbols";
import type {
  ConfirmedMarketDirection,
  MarketDirectionState,
} from "../market/market-state-contract";
import type { MarketPace } from "../market/market-episode-contract";

type RpcClient = Pick<SupabaseClient, "from" | "rpc">;

/** Existing newest-N read default; callers may explicitly request up to 1000. */
const COLLECTOR_COMPLETED_LIMIT = 260;

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
  async latestCheckpoint() {
    return null;
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
  async assignCollectorSubscriptions() {
    throw new Error("Operational collector subscriptions are disabled");
  },
  async readCollectorSubscriptions() {
    return [];
  },
  async readCollectorCompletedCandles() {
    throw new Error("Operational collector ownership is disabled");
  },
  async readMovementCandleHistory() {
    return new Map<string, MovementCandle[]>();
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
  async getMarketStateCurrent() {
    return null;
  },
  async upsertMarketStateCurrent() {
    throw new Error("Operational market-state storage is disabled");
  },
  async appendMarketMovementEvent() {
    throw new Error("Operational market-movement storage is disabled");
  },
  async persistMarketEpisodeLifecycleStep() {
    throw new Error("Operational market-episode storage is disabled");
  },
  async listMarketMovementEvents() {
    return [];
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
    async latestCheckpoint(userId) {
      const { data, error } = await client
        .from("market_data_checkpoints")
        .select("observed_at")
        .eq("user_id", userId)
        .order("observed_at", { ascending: false })
        .limit(1)
        .maybeSingle();
      rpcError(error, "latest checkpoint read");
      return (data as { observed_at?: string } | null)?.observed_at ?? null;
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
          quote_volume: candle.quoteVolume,
          source_event_at:
            candle.sourceEventTime === null ? null : new Date(candle.sourceEventTime).toISOString(),
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
    async assignCollectorSubscriptions(symbols) {
      const { error } = await client.rpc("assign_collector_subscriptions", {
        p_symbols: symbols.map((symbol) => symbol.toUpperCase()),
      });
      rpcError(error, "collector subscription assign");
    },
    async readCollectorSubscriptions() {
      const { data, error } = await client.rpc("get_collector_subscriptions");
      rpcError(error, "collector subscription read");
      return Array.isArray(data)
        ? (data as string[]).map((symbol) => String(symbol).toUpperCase())
        : [];
    },
    async readCollectorCompletedCandles(symbol, timeframeMinutes, limit = COLLECTOR_COMPLETED_LIMIT) {
      const { data, error } = await client.rpc("get_collector_completed_candles", {
        p_symbol: symbol.toUpperCase(),
        p_timeframe_minutes: timeframeMinutes,
        p_limit: limit,
      });
      rpcError(error, "collector completed candle read");
      if (!Array.isArray(data)) throw new Error("Invalid collector completed candle response");
      const requestedSymbol = symbol.toUpperCase();
      const identity = completedCandleIdentity(requestedSymbol, timeframeMinutes);
      const numeric = (value: unknown): number => {
        if ((typeof value !== "number" && typeof value !== "string") ||
            (typeof value === "string" && !value.trim())) return NaN;
        return Number(value);
      };
      const observations: CompletedCandleObservation[] = data.map((row: Record<string, unknown>) => {
        if (!row || row["provider"] !== identity.provider || row["symbol"] !== requestedSymbol ||
            row["native_symbol"] !== requestedSymbol || row["instrument_id"] !== identity.instrumentId ||
            row["market_type"] !== identity.marketType || row["contract_type"] !== identity.contractType ||
            row["price_type"] !== identity.priceType || row["timeframe_minutes"] !== timeframeMinutes ||
            typeof row["endpoint"] !== "string" || !row["endpoint"].trim() ||
            (row["transport"] !== "rest" && row["transport"] !== "websocket") ||
            (row["transport"] === "rest" && row["source_event_at_ms"] !== null) ||
            (row["transport"] === "websocket" && row["source_event_at_ms"] == null)) {
          throw new Error("Operational database returned an invalid collector completed candle row");
        }
        const receivedAt = numeric(row["received_at_ms"]);
        return {
          candle: { openTime: numeric(row["open_time_ms"]), closeTime: numeric(row["close_time_ms"]),
            open: numeric(row["open"]), high: numeric(row["high"]), low: numeric(row["low"]),
            close: numeric(row["close"]), baseVolume: numeric(row["volume"]),
            quoteVolume: numeric(row["quote_volume"]) },
          provenance: row["transport"] === "rest"
            ? { sourceKind: "rest", endpoint: row["endpoint"], retrievedAt: receivedAt }
            : { sourceKind: "websocket", endpoint: row["endpoint"],
                sourceEventTime: numeric(row["source_event_at_ms"]), receivedAt },
        };
      });
      try {
        return validateCompletedCandleSeries({
          contractVersion: COMPLETED_CANDLE_CONTRACT_VERSION, identity, observations,
        }, true);
      } catch {
        throw new Error("Operational database returned an invalid collector completed candle row");
      }
    },
    async readMovementCandleHistory(symbols, sinceMs, beforeBoundaryMs) {
      if (symbols.length === 0) return new Map<string, MovementCandle[]>();
      const { data, error } = await client.rpc("get_collector_movement_candles", {
        p_symbols: symbols.map((symbol) => symbol.toUpperCase()),
        p_since: new Date(sinceMs).toISOString(),
        p_before_boundary: new Date(beforeBoundaryMs).toISOString(),
      });
      rpcError(error, "movement candle history read");
      const result = new Map<string, MovementCandle[]>();
      if (!data || typeof data !== "object") return result;
      for (const [symbol, rows] of Object.entries(data as Record<string, unknown>)) {
        if (!Array.isArray(rows)) continue;
        const candles: MovementCandle[] = [];
        for (const row of rows) {
          if (!Array.isArray(row) || row.length < 4) continue;
          if (row[3] === null || row[3] === undefined) continue;
          const openTime = Number(row[0]);
          const close = Number(row[1]);
          const volume = Number(row[2]);
          const quoteVolume = Number(row[3]);
          if (
            !Number.isSafeInteger(openTime) ||
            !Number.isFinite(close) ||
            !Number.isFinite(volume) || volume < 0 ||
            !Number.isFinite(quoteVolume) || quoteVolume < 0
          ) {
            continue;
          }
          candles.push({ openTime, close, volume, quoteVolume });
        }
        result.set(symbol.toUpperCase(), candles);
      }
      return result;
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
    async getMarketStateCurrent(universeId, primaryWindowMinutes = 5) {
      const { data, error } = await client
        .from("market_state_current")
        .select("*")
        .eq("universe_id", universeId)
        .eq("primary_window_minutes", primaryWindowMinutes)
        .maybeSingle();
      rpcError(error, "market-state-current read");
      if (!data) return null;
      const row = data as Record<string, unknown>;
      return {
        universeId: String(row["universe_id"]),
        primaryWindowMinutes: 5,
        universeVersion: String(row["universe_version"]),
        provider: row["provider"] as "binance-usdm",
        exchange: row["exchange"] as "binance",
        priceType: row["price_type"] as "trade",
        evaluationBoundaryTime: new Date(String(row["evaluation_boundary_time"])).getTime(),
        directionState: row["direction_state"] as MarketDirectionState,
        pace: row["pace"] as MarketPace,
        activeEpisodeId: (row["active_episode_id"] as string | null) ?? null,
        activeDirection: (row["active_direction"] as ConfirmedMarketDirection | null) ?? null,
        interrupted: Boolean(row["interrupted"]),
        episodeAlgorithmVersion: String(row["episode_algorithm_version"]),
        lifecycleConfigVersion: String(row["lifecycle_config_version"]),
        classifierAlgorithmVersion: String(row["classifier_algorithm_version"]),
        classifierConfigVersion: String(row["classifier_config_version"]),
        movementAlgorithmVersion: String(row["movement_algorithm_version"]),
        movementConfigVersion: String(row["movement_config_version"]),
        lifecycleState: row["lifecycle_state"] as PersistedMarketStateCurrent["lifecycleState"],
        currentEvidence: row["current_evidence"] as PersistedMarketStateCurrent["currentEvidence"],
        updatedAt: String(row["updated_at"]),
      };
    },
    async upsertMarketStateCurrent(state) {
      const { error } = await client.rpc("upsert_market_state_current", {
        p_universe_id: state.universeId,
        p_primary_window_minutes: state.primaryWindowMinutes,
        p_universe_version: state.universeVersion,
        p_provider: state.provider,
        p_exchange: state.exchange,
        p_price_type: state.priceType,
        p_evaluation_boundary_time: new Date(state.evaluationBoundaryTime).toISOString(),
        p_direction_state: state.directionState,
        p_pace: state.pace,
        p_active_episode_id: state.activeEpisodeId,
        p_active_direction: state.activeDirection,
        p_interrupted: state.interrupted,
        p_episode_algorithm_version: state.episodeAlgorithmVersion,
        p_lifecycle_config_version: state.lifecycleConfigVersion,
        p_classifier_algorithm_version: state.classifierAlgorithmVersion,
        p_classifier_config_version: state.classifierConfigVersion,
        p_movement_algorithm_version: state.movementAlgorithmVersion,
        p_movement_config_version: state.movementConfigVersion,
        p_lifecycle_state: state.lifecycleState,
        p_current_evidence: state.currentEvidence,
      });
      rpcError(error, "market-state-current upsert");
    },
    async appendMarketMovementEvent(event) {
      const { data, error } = await client.rpc("append_market_movement_event", {
        p_canonical_event: event,
      });
      rpcError(error, "market-movement-event append");
      if (data !== "appended" && data !== "already_exists") {
        throw new Error(`Operational database market-movement-event append returned ${data}`);
      }
      return data;
    },
    async persistMarketEpisodeLifecycleStep(state, events) {
      const { data, error } = await client.rpc("persist_market_episode_lifecycle_step", {
        p_current_state: state,
        p_events: events,
      });
      rpcError(error, "market-episode lifecycle persistence");
      if (!Array.isArray(data)) {
        throw new Error(
          "Operational database market-episode lifecycle persistence returned invalid status",
        );
      }
      return data.map((entry) => {
        if (
          typeof entry !== "object" ||
          entry === null ||
          typeof (entry as Record<string, unknown>)["eventId"] !== "string" ||
          !["appended", "already_exists"].includes(
            String((entry as Record<string, unknown>)["status"]),
          )
        ) {
          throw new Error(
            "Operational database market-episode lifecycle persistence returned invalid status",
          );
        }
        return {
          eventId: String((entry as Record<string, unknown>)["eventId"]),
          status: (entry as Record<string, unknown>)["status"] as "appended" | "already_exists",
        };
      });
    },
    async listMarketMovementEvents(episodeId) {
      const { data, error } = await client
        .from("market_movement_events")
        .select("*")
        .eq("episode_id", episodeId)
        .order("evaluation_boundary_time", { ascending: true });
      rpcError(error, "market-movement-events list");
      return ((data ?? []) as Array<Record<string, unknown>>).map((row) =>
        row["canonical_event"] as PersistedMarketMovementEvent);
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
