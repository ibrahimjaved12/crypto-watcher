import { supabaseAdmin } from "../../integrations/supabase/client.server";
import { getOperationalStore } from "../operational/repository.server";
import type { OperationalStore } from "../operational/types";
import { createMonitorRunContext } from "../monitor/run-context";
import { DEFAULT_SETTINGS, type MonitorSettings } from "../monitor/engine.server";
import { runTA } from "../ta/engine.server";
import { loadBinanceFuturesKlines } from "./providers.server";
import {
  BINANCE_USDM_WS_ENDPOINT,
  BinanceFuturesCollector,
  normalizeRestCandles,
  type CompletedCandleEvent,
} from "./collector";
import { MovementEngineRuntime } from "./movement-engine.server";
import { movementFinalizationConfig } from "./movement-finalization";

const WATCHLIST_REFRESH_MS = 30_000;
const LEASE_SECONDS = 60;
const LEASE_REFRESH_MS = 20_000;
const STALE_AFTER_MS = 30_000;
const CONNECTION_MAX_AGE_MS = 23 * 60 * 60_000 + 50 * 60_000;
const MAX_BACKOFF_MS = 30_000;

function enabled(env: Record<string, string | undefined> = process.env): boolean {
  const value = env["BINANCE_COLLECTOR_ENABLED"];
  if (value !== undefined && value !== "true" && value !== "false") {
    throw new Error("BINANCE_COLLECTOR_ENABLED must be true or false");
  }
  return value === "true";
}

async function watchedSymbols(): Promise<string[]> {
  const { data, error } = await supabaseAdmin.from("watchlist_items").select("symbol");
  if (error) throw new Error(`Collector watchlist read failed: ${error.message}`);
  return [...new Set((data ?? []).map((row) => row.symbol.toUpperCase()))].sort();
}

async function analyzeCompletedCandle(event: CompletedCandleEvent): Promise<void> {
  if (event.origin !== "live" || event.candle.timeframeMinutes === 1) return;
  const { data: watchers, error } = await supabaseAdmin
    .from("watchlist_items")
    .select("user_id")
    .eq("symbol", event.candle.symbol);
  if (error) throw new Error(`Collector TA audience read failed: ${error.message}`);
  const userIds = [...new Set((watchers ?? []).map((row) => row.user_id))];
  if (userIds.length === 0) return;
  const { data: rows, error: settingsError } = await supabaseAdmin
    .from("monitor_settings")
    .select(
      "user_id, threshold_pct, window_minutes, cooldown_minutes, monitoring_enabled, market_data_collection_enabled, completed_candle_ta_enabled, movement_alerts_enabled, developing_setup_evaluation_enabled, paper_trading_enabled",
    )
    .in("user_id", userIds);
  if (settingsError) throw new Error(`Collector TA settings read failed: ${settingsError.message}`);
  const byUser = new Map(
    (rows ?? []).map((row) => [row.user_id, row as unknown as MonitorSettings]),
  );
  for (const userId of userIds) {
    const settings = { ...DEFAULT_SETTINGS, ...byUser.get(userId) };
    if (
      !settings.monitoring_enabled ||
      !settings.market_data_collection_enabled ||
      !settings.completed_candle_ta_enabled
    ) {
      continue;
    }
    const errors = await runTA(
      supabaseAdmin,
      userId,
      event.candle.symbol,
      createMonitorRunContext(),
      undefined,
      null,
    );
    if (errors.length > 0) console.error(`[binance-collector] ${errors.join(" | ")}`);
  }
}

export class CollectorRuntime {
  private readonly instanceId = crypto.randomUUID();
  private readonly collector: BinanceFuturesCollector;
  private readonly movement: MovementEngineRuntime;
  private socket: WebSocket | null = null;
  private stopped = false;
  private active = false;
  private reconnectAttempts = 0;
  private requestId = 1;
  private lastMessageAt = 0;
  private readonly pendingRequests = new Map<
    number,
    { resolve: () => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> }
  >();
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private watchlistTimer: ReturnType<typeof setInterval> | null = null;
  private leaseTimer: ReturnType<typeof setInterval> | null = null;
  private staleTimer: ReturnType<typeof setInterval> | null = null;
  private lifetimeTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly store: OperationalStore) {
    this.collector = new BinanceFuturesCollector({
      store,
      loadRest: async ({ symbol, timeframeMinutes, limit, startTime, endTime }) => {
        const result = await loadBinanceFuturesKlines(symbol, timeframeMinutes, {
          limit,
          ...(startTime === undefined ? {} : { startTime }),
          ...(endTime === undefined ? {} : { endTime }),
        });
        return normalizeRestCandles({
          symbol,
          timeframeMinutes,
          candles: result.candles,
          retrievedAt: Date.parse(result.retrievedAt),
        });
      },
      onCompleted: analyzeCompletedCandle,
      onOverload: () => this.socket?.close(1013, "bounded processing capacity exceeded"),
    });
    this.movement = new MovementEngineRuntime({
      store,
      collector: this.collector,
      finalization: movementFinalizationConfig(process.env),
    });
  }

  start(): void {
    void this.tryBecomeActive();
  }

  async stop(): Promise<void> {
    this.stopped = true;
    this.clearTimers();
    this.rejectPendingRequests("collector stopping");
    this.socket?.close(1000, "collector stopping");
    this.socket = null;
    if (this.active) await this.store.releaseCollectorLease(this.instanceId);
    this.active = false;
  }

  private async tryBecomeActive(): Promise<void> {
    if (this.stopped || this.active) return;
    try {
      this.active = await this.store.claimCollectorLease(this.instanceId, LEASE_SECONDS);
      if (!this.active) return this.retryStandby();
      await this.collector.reconcile(await watchedSymbols());
      this.startTimers();
      this.connect();
      console.info("[binance-collector] active lease acquired");
    } catch (error) {
      console.error(
        `[binance-collector] startup unavailable: ${error instanceof Error ? error.message : String(error)}`,
      );
      const heldLease = this.active;
      this.active = false;
      this.clearTimers();
      this.rejectPendingRequests("collector startup failed");
      this.socket?.close();
      this.socket = null;
      if (heldLease) {
        try {
          await this.store.releaseCollectorLease(this.instanceId);
        } catch {
          // The short lease expires safely if startup cleanup cannot reach the store.
        }
      }
      this.retryStandby();
    }
  }

  private retryStandby(): void {
    if (this.stopped || this.retryTimer) return;
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      void this.tryBecomeActive();
    }, LEASE_REFRESH_MS);
  }

  private startTimers(): void {
    this.leaseTimer = setInterval(() => void this.renewLease(), LEASE_REFRESH_MS);
    this.watchlistTimer = setInterval(() => void this.reconcileWatchlist(), WATCHLIST_REFRESH_MS);
    this.staleTimer = setInterval(() => {
      if (
        this.socket?.readyState === WebSocket.OPEN &&
        this.collector.subscribedSymbols().length > 0 &&
        Date.now() - this.lastMessageAt > STALE_AFTER_MS
      ) {
        this.backgroundHealth("STALE", "Binance stream is stale");
        this.socket.close(1013, "stale market stream");
      }
    }, 10_000);
    void this.movement.start();
  }

  private async renewLease(): Promise<void> {
    try {
      if (await this.store.renewCollectorLease(this.instanceId, LEASE_SECONDS)) return;
    } catch (error) {
      console.error(
        `[binance-collector] lease renewal failed: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
    this.active = false;
    this.clearTimers();
    this.rejectPendingRequests("authoritative collector lease lost");
    this.backgroundHealth("UNAVAILABLE", "authoritative collector lease lost");
    this.socket?.close(1012, "collector lease lost");
    this.socket = null;
    this.retryStandby();
  }

  private async reconcileWatchlist(): Promise<void> {
    if (!this.active) return;
    try {
      const before = new Set(this.collector.streamNames());
      await this.collector.reconcile(await watchedSymbols());
      const after = new Set(this.collector.streamNames());
      await Promise.all([
        this.sendSubscription(
          "UNSUBSCRIBE",
          [...before].filter((stream) => !after.has(stream)),
        ),
        this.sendSubscription(
          "SUBSCRIBE",
          [...after].filter((stream) => !before.has(stream)),
        ),
      ]);
      if (this.socket?.readyState === WebSocket.OPEN) {
        await this.collector.markConnectionStatus("LIVE", null);
      }
    } catch (error) {
      console.error(
        `[binance-collector] subscription reconciliation failed: ${error instanceof Error ? error.message : String(error)}`,
      );
      this.socket?.close(1013, "subscription reconciliation failed");
    }
  }

  private connect(): void {
    if (this.stopped || !this.active || this.socket) return;
    const socket = new WebSocket(BINANCE_USDM_WS_ENDPOINT);
    this.socket = socket;
    socket.addEventListener("open", () => {
      this.lastMessageAt = Date.now();
      void Promise.all([
        this.sendSubscription("SUBSCRIBE", this.collector.streamNames()),
        this.collector.recoverAfterReconnect(),
      ])
        .then(() => {
          this.reconnectAttempts = 0;
          return this.collector.markConnectionStatus("LIVE", null);
        })
        .catch((error) => {
          this.backgroundHealth(
            "UNAVAILABLE",
            error instanceof Error ? error.message : String(error),
          );
          socket.close(1013, "subscription or recovery failed");
        });
      this.lifetimeTimer = setTimeout(
        () => socket.close(1000, "scheduled Binance connection rotation"),
        CONNECTION_MAX_AGE_MS,
      );
    });
    socket.addEventListener("message", (event) => {
      this.lastMessageAt = Date.now();
      if (typeof event.data !== "string") return;
      try {
        const payload = JSON.parse(event.data) as Record<string, unknown>;
        const requestId = typeof payload["id"] === "number" ? payload["id"] : null;
        if (requestId !== null) {
          const pending = this.pendingRequests.get(requestId);
          if (pending) {
            clearTimeout(pending.timer);
            this.pendingRequests.delete(requestId);
            if (typeof payload["code"] === "number") {
              pending.reject(new Error(`Binance subscription rejected: ${String(payload["msg"])}`));
            } else pending.resolve();
          }
        }
        this.collector.accept(payload, this.lastMessageAt);
      } catch {
        // Malformed messages are rejected; the stream remains available for recovery.
      }
    });
    socket.addEventListener("close", () => {
      if (this.socket !== socket) return;
      this.socket = null;
      this.rejectPendingRequests("Binance connection closed before subscription acknowledgement");
      if (this.lifetimeTimer) clearTimeout(this.lifetimeTimer);
      this.lifetimeTimer = null;
      if (this.stopped || !this.active) return;
      this.collector.noteReconnect();
      this.backgroundHealth("RECOVERING", "Binance stream reconnecting");
      this.scheduleReconnect();
    });
    socket.addEventListener("error", () => socket.close());
  }

  private scheduleReconnect(): void {
    if (this.retryTimer || this.stopped || !this.active) return;
    const base = Math.min(MAX_BACKOFF_MS, 1_000 * 2 ** Math.min(this.reconnectAttempts, 5));
    const delay = Math.round(base + Math.random() * base * 0.25);
    this.reconnectAttempts += 1;
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this.connect();
    }, delay);
  }

  private sendSubscription(method: "SUBSCRIBE" | "UNSUBSCRIBE", streams: string[]): Promise<void> {
    if (streams.length === 0) return Promise.resolve();
    if (this.socket?.readyState !== WebSocket.OPEN) {
      return Promise.reject(new Error("Binance connection is not open"));
    }
    if (streams.length > 1024) throw new Error("Binance stream capacity exceeded");
    const id = this.requestId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pendingRequests.delete(id);
        reject(new Error("Binance subscription acknowledgement timed out"));
      }, 10_000);
      this.pendingRequests.set(id, { resolve, reject, timer });
      this.socket!.send(JSON.stringify({ method, params: streams, id }));
    });
  }

  private rejectPendingRequests(message: string): void {
    for (const pending of this.pendingRequests.values()) {
      clearTimeout(pending.timer);
      pending.reject(new Error(message));
    }
    this.pendingRequests.clear();
  }

  private backgroundHealth(
    status: "LIVE" | "RECOVERING" | "STALE" | "UNAVAILABLE",
    message: string | null,
  ): void {
    void this.collector.markConnectionStatus(status, message).catch((error) => {
      console.error(
        `[binance-collector] health write failed: ${error instanceof Error ? error.message : String(error)}`,
      );
    });
  }

  private clearTimers(): void {
    for (const timer of [this.watchlistTimer, this.leaseTimer, this.staleTimer]) {
      if (timer) clearInterval(timer);
    }
    if (this.retryTimer) clearTimeout(this.retryTimer);
    if (this.lifetimeTimer) clearTimeout(this.lifetimeTimer);
    this.watchlistTimer = null;
    this.leaseTimer = null;
    this.staleTimer = null;
    this.retryTimer = null;
    this.lifetimeTimer = null;
    void this.movement.stop();
  }
}

type CollectorGlobal = typeof globalThis & {
  __cryptoWatcherBinanceCollector?: CollectorRuntime;
};

/**
 * Shared collector startup path. Starts the one authoritative collector and returns
 * its runtime handle, or null when `BINANCE_COLLECTOR_ENABLED` is not true. It does
 * not wire process signal handling: an independent worker entrypoint owns graceful
 * shutdown and lease release.
 */
export function startBinanceCollector(): CollectorRuntime | null {
  if (!enabled()) return null;
  const global = globalThis as CollectorGlobal;
  if (global.__cryptoWatcherBinanceCollector) return global.__cryptoWatcherBinanceCollector;
  const store = getOperationalStore();
  if (!store.enabled) throw new Error("Binance collector requires OPERATIONAL_DB_ENABLED=true");
  const runtime = new CollectorRuntime(store);
  global.__cryptoWatcherBinanceCollector = runtime;
  runtime.start();
  return runtime;
}
