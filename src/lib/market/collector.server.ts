import { getOperationalStore } from "../operational/repository.server";
import type { OperationalStore } from "../operational/types";
import { loadBinanceFuturesKlines } from "./providers.server";
import {
  BINANCE_USDM_WS_ENDPOINT,
  BinanceFuturesCollector,
  normalizeRestCandles,
} from "./collector";
import { MovementEngineRuntime } from "./movement-engine.server";
import { advancePythonMovementBoundary } from "./movement-python-client.server";
import { movementFinalizationConfig } from "./movement-finalization";
import { validateCollectorWorkerEnvironment } from "./collector-worker-env.server";

const SUBSCRIPTION_REFRESH_MS = 30_000;
const LEASE_SECONDS = 60;
const LEASE_REFRESH_MS = 20_000;
const STALE_AFTER_MS = 30_000;
const CONNECTION_MAX_AGE_MS = 23 * 60 * 60_000 + 50 * 60_000;
const MAX_BACKOFF_MS = 30_000;
const RECOVERY_CLOSE_CODE = 4000;

function enabled(env: Record<string, string | undefined> = process.env): boolean {
  const value = env["BINANCE_COLLECTOR_ENABLED"];
  if (value !== undefined && value !== "true" && value !== "false") {
    throw new Error("BINANCE_COLLECTOR_ENABLED must be true or false");
  }
  return value === "true";
}

export class CollectorRuntime {
  private readonly instanceId = crypto.randomUUID();
  private readonly collector: BinanceFuturesCollector;
  private readonly movement: MovementEngineRuntime;
  private socket: WebSocket | null = null;
  private stopped = false;
  private active = false;
  private streamReady = false;
  private reconnectAttempts = 0;
  private requestId = 1;
  private lastMessageAt = 0;
  private readonly pendingRequests = new Map<
    number,
    { resolve: () => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> }
  >();
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private subscriptionTimer: ReturnType<typeof setInterval> | null = null;
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
      onOverload: () =>
        this.socket?.close(RECOVERY_CLOSE_CODE, "bounded processing capacity exceeded"),
      advanceMovementBoundary: (sessionId, boundaryTime, symbols) =>
        advancePythonMovementBoundary(sessionId, boundaryTime, symbols),
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
    this.streamReady = false;
    this.clearTimers();
    this.collector.resetMovementTransportState();
    this.rejectPendingRequests("collector stopping");
    this.socket?.close(1000, "collector stopping");
    this.socket = null;
    // An explicit shutdown must be a complete barrier for collector-owned runtime
    // work: await the movement engine's teardown before releasing ownership.
    await this.movement.stop();
    if (this.active) await this.store.releaseCollectorLease(this.instanceId);
    this.active = false;
    this.streamReady = false;
  }

  private async tryBecomeActive(): Promise<void> {
    if (this.stopped || this.active) return;
    try {
      this.active = await this.store.claimCollectorLease(this.instanceId, LEASE_SECONDS);
      if (!this.active) return this.retryStandby();
      this.collector.resetMovementTransportState();
      await this.collector.reconcile(await this.store.readCollectorSubscriptions());
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
      this.collector.resetMovementTransportState();
      void this.movement.stop();
      this.rejectPendingRequests("collector startup failed");
      this.socket?.close(RECOVERY_CLOSE_CODE, "collector startup failed");
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
    this.subscriptionTimer = setInterval(
      () => void this.reconcileSubscriptions(),
      SUBSCRIPTION_REFRESH_MS,
    );
    this.staleTimer = setInterval(() => {
      if (
        this.socket?.readyState === WebSocket.OPEN &&
        this.collector.subscribedSymbols().length > 0 &&
        Date.now() - this.lastMessageAt > STALE_AFTER_MS
      ) {
        this.backgroundHealth("STALE", "Binance stream is stale");
        this.socket.close(RECOVERY_CLOSE_CODE, "stale market stream");
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
    this.streamReady = false;
    this.clearTimers();
    this.collector.resetMovementTransportState();
    void this.movement.stop();
    this.rejectPendingRequests("authoritative collector lease lost");
    this.backgroundHealth("UNAVAILABLE", "authoritative collector lease lost");
    this.socket?.close(RECOVERY_CLOSE_CODE, "collector lease lost");
    this.socket = null;
    this.retryStandby();
  }

  /**
   * Re-reads the application-assigned subscription universe from the operational
   * store. The worker never reads Lovable user watchlists; the application owns
   * them and assigns the shared set as derived collector input.
   */
  private async reconcileSubscriptions(): Promise<void> {
    if (!this.active) return;
    try {
      const before = new Set(this.collector.streamNames());
      await this.collector.reconcile(await this.store.readCollectorSubscriptions());
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
      if (this.streamReady && this.socket?.readyState === WebSocket.OPEN) {
        await this.collector.markConnectionStatus("LIVE", null);
      }
    } catch (error) {
      console.error(
        `[binance-collector] subscription reconciliation failed: ${error instanceof Error ? error.message : String(error)}`,
      );
      this.socket?.close(RECOVERY_CLOSE_CODE, "subscription reconciliation failed");
    }
  }

  private connect(): void {
    if (this.stopped || !this.active || this.socket) return;
    this.streamReady = false;
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
          this.streamReady = true;
          return this.collector.markConnectionStatus("LIVE", null);
        })
        .catch((error) => {
          this.streamReady = false;
          this.backgroundHealth(
            "UNAVAILABLE",
            error instanceof Error ? error.message : String(error),
          );
          socket.close(RECOVERY_CLOSE_CODE, "subscription or recovery failed");
        });
      this.lifetimeTimer = setTimeout(
        () => socket.close(1000, "scheduled Binance connection rotation"),
        CONNECTION_MAX_AGE_MS,
      );
    });
    socket.addEventListener("message", (event) => {
      this.lastMessageAt = Date.now();
      if (typeof event.data !== "string") {
        this.collector.markAllMovementUnavailable();
        socket.close(RECOVERY_CLOSE_CODE, "non-text Binance movement frame");
        return;
      }
      let payload: Record<string, unknown>;
      try {
        payload = JSON.parse(event.data) as Record<string, unknown>;
      } catch {
        this.collector.markAllMovementUnavailable();
        socket.close(RECOVERY_CLOSE_CODE, "malformed Binance movement frame");
        return;
      }
      try {
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
        this.collector.markAllMovementUnavailable();
        socket.close(RECOVERY_CLOSE_CODE, "invalid Binance movement frame");
      }
    });
    socket.addEventListener("close", () => {
      if (this.socket !== socket) return;
      this.socket = null;
      this.streamReady = false;
      this.rejectPendingRequests("Binance connection closed before subscription acknowledgement");
      if (this.lifetimeTimer) clearTimeout(this.lifetimeTimer);
      this.lifetimeTimer = null;
      if (this.stopped || !this.active) return;
      this.collector.noteReconnect();
      this.backgroundHealth("RECOVERING", "Binance stream reconnecting");
      this.scheduleReconnect();
    });
    socket.addEventListener("error", () =>
      socket.close(RECOVERY_CLOSE_CODE, "Binance transport error"),
    );
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
    for (const timer of [this.subscriptionTimer, this.leaseTimer, this.staleTimer]) {
      if (timer) clearInterval(timer);
    }
    if (this.retryTimer) clearTimeout(this.retryTimer);
    if (this.lifetimeTimer) clearTimeout(this.lifetimeTimer);
    this.subscriptionTimer = null;
    this.leaseTimer = null;
    this.staleTimer = null;
    this.retryTimer = null;
    this.lifetimeTimer = null;
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
 *
 * When enabled, it first validates the collector worker's server-only runtime
 * configuration, so a misconfigured host fails visibly instead of half-starting.
 */
export function startBinanceCollector(): CollectorRuntime | null {
  if (!enabled()) return null;
  validateCollectorWorkerEnvironment();
  const global = globalThis as CollectorGlobal;
  if (global.__cryptoWatcherBinanceCollector) return global.__cryptoWatcherBinanceCollector;
  const store = getOperationalStore();
  if (!store.enabled) throw new Error("Binance collector requires OPERATIONAL_DB_ENABLED=true");
  const runtime = new CollectorRuntime(store);
  global.__cryptoWatcherBinanceCollector = runtime;
  runtime.start();
  return runtime;
}
