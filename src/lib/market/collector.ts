import { isSupportedSymbol } from "./symbols";
import type { Candle } from "./providers.server";
import type {
  CollectorCandle,
  CollectorHealthStatus,
  OperationalStore,
} from "../operational/types";
import { FuturesMovementBuckets, type MovementBucketSnapshot } from "./movement-buckets";

export const BINANCE_USDM_WS_ENDPOINT = "wss://fstream.binance.com/market/stream";
export const BINANCE_USDM_REST_ENDPOINT = "/fapi/v1/klines";
export const COLLECTOR_INTERVALS = [1, 15, 60, 240] as const;
export type CollectorInterval = (typeof COLLECTOR_INTERVALS)[number];
/**
 * Completed-candle history the REST bootstrap rebuilds after a reset. The application TA path
 * needs the 200-candle minimum history plus a bounded catch-up batch, and the operational store
 * retains the newest 260 candles per series, so fetch one extra kline (the still-developing REST
 * candle is excluded) with margin.
 */
export const COLLECTOR_BOOTSTRAP_LIMIT = 300;
export type CompletedCandleOrigin = "bootstrap" | "recovery" | "live";

export type AggregateTrade = {
  symbol: string;
  aggregateId: number;
  price: number;
  quantity: number;
  eventTime: number;
  tradeTime: number;
  receivedAt: number;
};

export type CompletedCandleEvent = {
  candle: CollectorCandle;
  origin: CompletedCandleOrigin;
};

type RestRequest = {
  symbol: string;
  timeframeMinutes: CollectorInterval;
  limit: number;
  startTime?: number;
  endTime?: number;
};

type CollectorDependencies = {
  store: Pick<OperationalStore, "recordCollectorCandles" | "recordCollectorHealth">;
  loadRest(request: RestRequest): Promise<CollectorCandle[]>;
  onCompleted?(event: CompletedCandleEvent): Promise<void> | void;
  onOverload?(): void;
  now?: () => number;
  queueCapacity?: number;
  tradeWindowMs?: number;
  tradeMaxCount?: number;
};

type HealthState = {
  status: CollectorHealthStatus;
  lastEventAt: number | null;
  lastCompletedOpenTime: number | null;
  errorMessage: string | null;
};

function finitePositive(value: unknown, label: string): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) throw new Error(`invalid ${label}`);
  return parsed;
}

function finiteNonnegative(value: unknown, label: string): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0) throw new Error(`invalid ${label}`);
  return parsed;
}

function safeTimestamp(value: unknown, label: string): number {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 0) throw new Error(`invalid ${label}`);
  return parsed;
}

function collectorInterval(value: unknown): CollectorInterval {
  const mapping: Record<string, CollectorInterval> = { "1m": 1, "15m": 15, "1h": 60, "4h": 240 };
  const result = mapping[String(value)];
  if (!result) throw new Error("unsupported kline interval");
  return result;
}

function canonicalBase(
  symbol: string,
  timeframeMinutes: CollectorInterval,
): Pick<
  CollectorCandle,
  "instrumentId" | "symbol" | "nativeSymbol" | "provider" | "priceType" | "timeframeMinutes"
> {
  const normalized = symbol.toUpperCase();
  if (!isSupportedSymbol(normalized)) throw new Error("unsupported futures contract");
  return {
    instrumentId: `binance-usdm:${normalized}`,
    symbol: normalized,
    nativeSymbol: normalized,
    provider: "binance-usdm",
    priceType: "trade",
    timeframeMinutes,
  };
}

export function candleIdentity(candle: CollectorCandle): string {
  return [
    candle.provider,
    candle.instrumentId,
    candle.priceType,
    candle.timeframeMinutes,
    candle.openTime,
  ].join("|");
}

export function normalizeRestCandles(input: {
  symbol: string;
  timeframeMinutes: CollectorInterval;
  candles: Candle[];
  retrievedAt: number;
}): CollectorCandle[] {
  const duration = input.timeframeMinutes * 60_000;
  return input.candles
    .filter((candle) => candle.complete === true)
    .map((candle) => {
      if (!Number.isSafeInteger(candle.time) || candle.time % duration !== 0) {
        throw new Error("invalid REST candle boundary");
      }
      const closeTime = candle.time + duration - 1;
      return {
        ...canonicalBase(input.symbol, input.timeframeMinutes),
        endpoint: BINANCE_USDM_REST_ENDPOINT,
        openTime: candle.time,
        closeTime,
        open: finitePositive(candle.open, "open price"),
        high: finitePositive(candle.high, "high price"),
        low: finitePositive(candle.low, "low price"),
        close: finitePositive(candle.close, "close price"),
        volume: finiteNonnegative(candle.volume, "volume"),
        // REST bootstrap/recovery has no exchange event, so the source event time is
        // honestly absent. The deterministic completion boundary is open + timeframe.
        sourceEventTime: null,
        receivedAt: input.retrievedAt,
        transport: "rest" as const,
      };
    });
}

export function parseBinanceMarketMessage(
  raw: unknown,
  receivedAt: number,
):
  | { kind: "developing" | "completed"; candle: CollectorCandle }
  | { kind: "trade"; trade: AggregateTrade }
  | { kind: "control" } {
  const envelope = raw as Record<string, unknown>;
  const value =
    envelope && typeof envelope === "object" && envelope["data"]
      ? (envelope["data"] as Record<string, unknown>)
      : envelope;
  if (!value || typeof value !== "object") throw new Error("invalid websocket message");
  if (value["id"] !== undefined || value["result"] !== undefined) return { kind: "control" };
  if (value["st"] !== undefined && value["st"] !== 1) {
    throw new Error("non-USD-M market event rejected");
  }

  if (value["e"] === "aggTrade") {
    const symbol = String(value["s"] ?? "").toUpperCase();
    canonicalBase(symbol, 1);
    return {
      kind: "trade",
      trade: {
        symbol,
        aggregateId: safeTimestamp(value["a"], "aggregate trade id"),
        price: finitePositive(value["p"], "trade price"),
        quantity: finitePositive(value["q"], "trade quantity"),
        eventTime: safeTimestamp(value["E"], "event time"),
        tradeTime: safeTimestamp(value["T"], "trade time"),
        receivedAt,
      },
    };
  }

  if (value["e"] !== "kline") throw new Error("unsupported websocket event");
  const kline = value["k"] as Record<string, unknown> | undefined;
  if (!kline || typeof kline !== "object") throw new Error("invalid kline event");
  const symbol = String(value["s"] ?? kline["s"] ?? "").toUpperCase();
  if (kline["s"] !== undefined && String(kline["s"]).toUpperCase() !== symbol) {
    throw new Error("mismatched kline symbol");
  }
  const timeframeMinutes = collectorInterval(kline["i"]);
  const duration = timeframeMinutes * 60_000;
  const openTime = safeTimestamp(kline["t"], "kline open time");
  const closeTime = safeTimestamp(kline["T"], "kline close time");
  if (openTime % duration !== 0 || closeTime !== openTime + duration - 1) {
    throw new Error("invalid kline boundary");
  }
  const sourceEventTime = safeTimestamp(value["E"], "event time");
  const completed = kline["x"] === true;
  const candle: CollectorCandle = {
    ...canonicalBase(symbol, timeframeMinutes),
    endpoint: BINANCE_USDM_WS_ENDPOINT,
    openTime,
    closeTime,
    open: finitePositive(kline["o"], "open price"),
    high: finitePositive(kline["h"], "high price"),
    low: finitePositive(kline["l"], "low price"),
    close: finitePositive(kline["c"], "close price"),
    volume: finiteNonnegative(kline["v"], "volume"),
    // The exchange event time is preserved exactly as received; completion is
    // defined by openTime + timeframeMinutes, not by this timestamp.
    sourceEventTime,
    // The collector receive time is preserved exactly as passed in.
    receivedAt,
    transport: "websocket",
  };
  return { kind: completed ? "completed" : "developing", candle };
}

class TradeBuffer {
  private values: AggregateTrade[] = [];
  private head = 0;

  constructor(
    private readonly windowMs: number,
    private readonly maxCount: number,
  ) {}

  push(trade: AggregateTrade): void {
    this.values.push(trade);
    const cutoff = trade.tradeTime - this.windowMs;
    while (
      this.head < this.values.length &&
      (this.values[this.head]!.tradeTime < cutoff || this.values.length - this.head > this.maxCount)
    ) {
      this.head += 1;
    }
    if (this.head > 512 && this.head * 2 > this.values.length) {
      this.values = this.values.slice(this.head);
      this.head = 0;
    }
  }

  snapshot(): AggregateTrade[] {
    return this.values.slice(this.head);
  }
}

export class BinanceFuturesCollector {
  private readonly now: () => number;
  private readonly queueCapacity: number;
  private readonly tradeWindowMs: number;
  private readonly tradeMaxCount: number;
  private readonly symbols = new Set<string>();
  private readonly developing = new Map<string, CollectorCandle>();
  private readonly latestCompleted = new Map<string, CollectorCandle>();
  private readonly latestTrade = new Map<string, AggregateTrade>();
  private readonly trades = new Map<string, TradeBuffer>();
  private readonly movementBuckets = new FuturesMovementBuckets();
  private readonly health = new Map<string, HealthState>();
  private readonly queue: CollectorCandle[] = [];
  private processing: Promise<void> | null = null;
  private recovery: Promise<void> | null = null;
  private reconnectCount = 0;

  constructor(private readonly dependencies: CollectorDependencies) {
    this.now = dependencies.now ?? Date.now;
    this.queueCapacity = dependencies.queueCapacity ?? 256;
    this.tradeWindowMs = dependencies.tradeWindowMs ?? 5 * 60_000;
    this.tradeMaxCount = dependencies.tradeMaxCount ?? 2_000;
    if (this.queueCapacity < 1 || this.tradeWindowMs < 1 || this.tradeMaxCount < 1) {
      throw new Error("invalid collector bounds");
    }
  }

  subscribedSymbols(): string[] {
    return [...this.symbols].sort();
  }

  streamNames(): string[] {
    return this.subscribedSymbols().flatMap((symbol) => {
      const name = symbol.toLowerCase();
      return [
        `${name}@aggTrade`,
        ...["1m", "15m", "1h", "4h"].map((interval) => `${name}@kline_${interval}`),
      ];
    });
  }

  latestTrades(symbol: string): AggregateTrade[] {
    return this.trades.get(symbol.toUpperCase())?.snapshot() ?? [];
  }

  latestPrice(symbol: string): AggregateTrade | null {
    return this.latestTrade.get(symbol.toUpperCase()) ?? null;
  }

  movementSnapshot(symbol: string): MovementBucketSnapshot | null {
    return this.movementBuckets.snapshot(symbol);
  }

  movementSnapshots(): MovementBucketSnapshot[] {
    return this.movementBuckets.snapshots();
  }

  /** Trades rejected because their exchange-time bucket had already been finalized. */
  movementLateRejections(): number {
    return this.movementBuckets.lateAfterFinalizationCount;
  }

  /**
   * Coarse per-symbol source health for the movement engine's source gate.
   * Uses the best timeframe status so a healthy contract is not excluded by an
   * unrelated long-timeframe recovery.
   */
  symbolSourceStatus(symbol: string): CollectorHealthStatus {
    const normalized = symbol.toUpperCase();
    const rank: Record<CollectorHealthStatus, number> = {
      UNAVAILABLE: 0,
      STALE: 1,
      RECOVERING: 2,
      LIVE: 3,
    };
    let best: CollectorHealthStatus = "UNAVAILABLE";
    for (const timeframe of COLLECTOR_INTERVALS) {
      const status = this.health.get(this.key(normalized, timeframe))?.status;
      if (status && rank[status] > rank[best]) best = status;
    }
    return best;
  }

  advanceMovementBuckets(boundaryTime: number): void {
    this.movementBuckets.advanceTo(boundaryTime);
  }

  developingCandle(symbol: string, timeframeMinutes: CollectorInterval): CollectorCandle | null {
    return this.developing.get(this.key(symbol, timeframeMinutes)) ?? null;
  }

  noteReconnect(): void {
    this.reconnectCount += 1;
  }

  async markConnectionStatus(
    status: CollectorHealthStatus,
    errorMessage: string | null,
  ): Promise<void> {
    for (const symbol of this.subscribedSymbols()) {
      for (const timeframe of COLLECTOR_INTERVALS) {
        const candle =
          this.latestCompleted.get(this.key(symbol, timeframe)) ??
          this.placeholder(symbol, timeframe);
        await this.setHealth(candle, status, errorMessage);
      }
    }
  }

  async reconcile(symbols: Iterable<string>): Promise<void> {
    const next = new Set<string>();
    for (const value of symbols) {
      const symbol = value.toUpperCase();
      if (!isSupportedSymbol(symbol)) throw new Error(`unsupported futures contract: ${symbol}`);
      next.add(symbol);
    }
    this.movementBuckets.reconcile(next);
    for (const symbol of this.symbols) {
      if (next.has(symbol)) continue;
      this.symbols.delete(symbol);
      this.trades.delete(symbol);
      this.latestTrade.delete(symbol);
      for (const interval of COLLECTOR_INTERVALS)
        this.developing.delete(this.key(symbol, interval));
    }
    for (const symbol of next) {
      if (this.symbols.has(symbol)) continue;
      this.symbols.add(symbol);
      this.trades.set(symbol, new TradeBuffer(this.tradeWindowMs, this.tradeMaxCount));
      for (const interval of COLLECTOR_INTERVALS) await this.bootstrap(symbol, interval);
    }
  }

  accept(raw: unknown, receivedAt = this.now()): boolean {
    let event;
    try {
      event = parseBinanceMarketMessage(raw, receivedAt);
    } catch {
      return false;
    }
    if (event.kind === "control") return true;
    if (!this.symbols.has(event.kind === "trade" ? event.trade.symbol : event.candle.symbol)) {
      return false;
    }
    if (event.kind === "trade") {
      const previous = this.latestTrade.get(event.trade.symbol);
      if (
        previous &&
        (event.trade.aggregateId <= previous.aggregateId ||
          event.trade.tradeTime < previous.tradeTime)
      ) {
        return false;
      }
      try {
        if (!this.movementBuckets.accept(event.trade)) return false;
      } catch {
        return false;
      }
      this.latestTrade.set(event.trade.symbol, event.trade);
      this.trades.get(event.trade.symbol)!.push(event.trade);
      return true;
    }
    const key = this.key(event.candle.symbol, event.candle.timeframeMinutes);
    const state = this.state(key);
    state.lastEventAt = event.candle.sourceEventTime;
    if (event.kind === "developing") {
      const previous = this.developing.get(key);
      if (previous) {
        const previousEventTime = previous.sourceEventTime;
        const eventTime = event.candle.sourceEventTime;
        if (
          event.candle.openTime < previous.openTime ||
          (event.candle.openTime === previous.openTime &&
            eventTime !== null &&
            previousEventTime !== null &&
            eventTime < previousEventTime)
        ) {
          return false;
        }
      }
      this.developing.set(key, event.candle);
      return true;
    }
    this.developing.delete(key);
    if (this.queue.length >= this.queueCapacity) {
      void this.setHealth(
        event.candle,
        "STALE",
        "completed-candle processing capacity exceeded",
      ).catch(() => undefined);
      this.dependencies.onOverload?.();
      return false;
    }
    this.queue.push(event.candle);
    this.startDrain();
    return true;
  }

  async recoverAfterReconnect(): Promise<void> {
    if (this.recovery) return this.recovery;
    this.recovery = (async () => {
      for (const symbol of this.subscribedSymbols()) {
        for (const timeframe of COLLECTOR_INTERVALS) {
          const latest = this.latestCompleted.get(this.key(symbol, timeframe));
          if (!latest) {
            await this.bootstrap(symbol, timeframe);
            if (!this.latestCompleted.has(this.key(symbol, timeframe))) {
              throw new Error(`REST continuity unavailable for ${symbol} ${timeframe}m`);
            }
          } else await this.recoverThrough(symbol, timeframe, this.latestExpectedOpen(timeframe));
        }
      }
    })().finally(() => {
      this.recovery = null;
      this.startDrain();
    });
    return this.recovery;
  }

  async waitForIdle(): Promise<void> {
    await this.recovery;
    while (this.processing) await this.processing;
  }

  private async bootstrap(symbol: string, timeframeMinutes: CollectorInterval): Promise<void> {
    const placeholder = this.placeholder(symbol, timeframeMinutes);
    await this.setHealth(placeholder, "RECOVERING", null);
    const candles = (
      await this.dependencies.loadRest({
        symbol,
        timeframeMinutes,
        limit: COLLECTOR_BOOTSTRAP_LIMIT,
      })
    )
      .filter((candle) => candle.closeTime < this.now())
      .sort((left, right) => left.openTime - right.openTime);
    if (candles.length === 0 || !this.contiguous(candles, timeframeMinutes)) {
      await this.setHealth(placeholder, "UNAVAILABLE", "REST bootstrap continuity unavailable");
      return;
    }
    await this.persist(candles, "bootstrap");
    const latest = candles.at(-1)!;
    this.latestCompleted.set(this.key(symbol, timeframeMinutes), latest);
    await this.setHealth(latest, "RECOVERING", null);
  }

  private startDrain(): void {
    if (this.processing || this.recovery || this.queue.length === 0) return;
    this.processing = (async () => {
      while (this.queue.length > 0 && !this.recovery) {
        const candle = this.queue.shift()!;
        try {
          await this.processFinal(candle);
        } catch (error) {
          await this.setHealth(
            candle,
            "UNAVAILABLE",
            error instanceof Error ? error.message : String(error),
          );
        }
      }
    })()
      .catch(() => {
        this.dependencies.onOverload?.();
      })
      .finally(() => {
        this.processing = null;
        if (this.queue.length > 0) this.startDrain();
      });
  }

  private async processFinal(candle: CollectorCandle): Promise<void> {
    const key = this.key(candle.symbol, candle.timeframeMinutes);
    let latest = this.latestCompleted.get(key);
    if (!latest) {
      await this.bootstrap(candle.symbol, candle.timeframeMinutes);
      latest = this.latestCompleted.get(key);
    }
    if (latest && candle.openTime <= latest.openTime) return;
    const duration = candle.timeframeMinutes * 60_000;
    if (latest && candle.openTime !== latest.openTime + duration) {
      await this.setHealth(candle, "RECOVERING", "completed-candle gap detected");
      const recovered = await this.recoverRange(
        candle.symbol,
        candle.timeframeMinutes,
        latest.openTime + duration,
        candle.openTime - duration,
      );
      if (!recovered) {
        throw new Error("REST recovery could not prove candle continuity");
      }
    }
    await this.persist([candle], "live");
    this.latestCompleted.set(key, candle);
    await this.setHealth(candle, "LIVE", null);
  }

  private async recoverThrough(
    symbol: string,
    timeframeMinutes: CollectorInterval,
    targetOpen: number,
  ): Promise<void> {
    const latest = this.latestCompleted.get(this.key(symbol, timeframeMinutes));
    if (!latest || targetOpen <= latest.openTime) return;
    await this.setHealth(latest, "RECOVERING", "reconnect recovery");
    if (
      !(await this.recoverRange(
        symbol,
        timeframeMinutes,
        latest.openTime + timeframeMinutes * 60_000,
        targetOpen,
      ))
    ) {
      await this.setHealth(latest, "UNAVAILABLE", "reconnect continuity unavailable");
      throw new Error(`REST reconnect continuity unavailable for ${symbol} ${timeframeMinutes}m`);
    }
  }

  private async recoverRange(
    symbol: string,
    timeframeMinutes: CollectorInterval,
    firstOpen: number,
    lastOpen: number,
  ): Promise<boolean> {
    if (lastOpen < firstOpen) return true;
    const duration = timeframeMinutes * 60_000;
    const count = Math.floor((lastOpen - firstOpen) / duration) + 1;
    if (count > 1000) return false;
    const candles = (
      await this.dependencies.loadRest({
        symbol,
        timeframeMinutes,
        startTime: firstOpen,
        endTime: lastOpen + duration - 1,
        limit: count,
      })
    )
      .filter((candle) => candle.closeTime < this.now())
      .sort((left, right) => left.openTime - right.openTime);
    if (
      candles.length !== count ||
      candles[0]?.openTime !== firstOpen ||
      candles.at(-1)?.openTime !== lastOpen ||
      !this.contiguous(candles, timeframeMinutes)
    ) {
      return false;
    }
    await this.persist(candles, "recovery");
    const latest = candles.at(-1)!;
    this.latestCompleted.set(this.key(symbol, timeframeMinutes), latest);
    return true;
  }

  private async persist(candles: CollectorCandle[], origin: CompletedCandleOrigin): Promise<void> {
    const inserted = new Set(await this.dependencies.store.recordCollectorCandles(candles));
    for (const candle of candles) {
      this.latestCompleted.set(this.key(candle.symbol, candle.timeframeMinutes), candle);
      if (inserted.has(candleIdentity(candle))) {
        await this.dependencies.onCompleted?.({ candle, origin });
      }
    }
  }

  private contiguous(candles: CollectorCandle[], timeframeMinutes: CollectorInterval): boolean {
    const duration = timeframeMinutes * 60_000;
    return candles.every(
      (candle, index) =>
        candle.timeframeMinutes === timeframeMinutes &&
        (index === 0 || candle.openTime === candles[index - 1]!.openTime + duration),
    );
  }

  private latestExpectedOpen(timeframeMinutes: CollectorInterval): number {
    const duration = timeframeMinutes * 60_000;
    return Math.floor(this.now() / duration) * duration - duration;
  }

  private key(symbol: string, timeframeMinutes: CollectorInterval): string {
    return `${symbol.toUpperCase()}:${timeframeMinutes}`;
  }

  private state(key: string): HealthState {
    let state = this.health.get(key);
    if (!state) {
      state = {
        status: "RECOVERING",
        lastEventAt: null,
        lastCompletedOpenTime: null,
        errorMessage: null,
      };
      this.health.set(key, state);
    }
    return state;
  }

  private placeholder(symbol: string, timeframeMinutes: CollectorInterval): CollectorCandle {
    const now = this.now();
    return {
      ...canonicalBase(symbol, timeframeMinutes),
      endpoint: BINANCE_USDM_REST_ENDPOINT,
      openTime: 0,
      closeTime: 0,
      open: 1,
      high: 1,
      low: 1,
      close: 1,
      volume: 0,
      sourceEventTime: now,
      receivedAt: now,
      transport: "rest",
    };
  }

  private async setHealth(
    candle: CollectorCandle,
    status: CollectorHealthStatus,
    errorMessage: string | null,
  ): Promise<void> {
    const key = this.key(candle.symbol, candle.timeframeMinutes);
    const state = this.state(key);
    state.status = status;
    state.errorMessage = errorMessage;
    if (candle.openTime > 0) state.lastCompletedOpenTime = candle.openTime;
    await this.dependencies.store.recordCollectorHealth({
      instrumentId: candle.instrumentId,
      symbol: candle.symbol,
      timeframeMinutes: candle.timeframeMinutes,
      status,
      lastEventAt: state.lastEventAt,
      lastCompletedOpenTime: state.lastCompletedOpenTime,
      lagMs: state.lastEventAt === null ? null : Math.max(0, this.now() - state.lastEventAt),
      queueDepth: this.queue.length,
      reconnectCount: this.reconnectCount,
      errorMessage,
    });
  }
}
