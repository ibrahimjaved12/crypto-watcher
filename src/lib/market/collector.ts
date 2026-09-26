import { isSupportedSymbol } from "./symbols";
import type { Candle } from "./providers.server";
import type {
  CollectorCandle,
  CollectorHealthStatus,
  OperationalStore,
} from "../operational/types";
import {
  MOVEMENT_OBSERVATION_BATCH_MAX,
  type MovementBoundaryResult,
  type MovementBoundarySymbolInput,
  type MovementBucketSnapshot,
  type MovementSourceState,
} from "./movement-contract";

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
  priceText: string;
  quantityText: string;
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
  advanceMovementBoundary(
    sessionId: string,
    boundaryTime: number,
    symbols: MovementBoundarySymbolInput[],
  ): Promise<MovementBoundaryResult>;
};

type HealthState = {
  status: CollectorHealthStatus;
  lastEventAt: number | null;
  lastCompletedOpenTime: number | null;
  errorMessage: string | null;
};

type MovementSourceTransition = {
  at: number;
  state: MovementSourceState;
};

function finitePositive(value: unknown, label: string): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) throw new Error(`invalid ${label}`);
  return parsed;
}

function positiveDecimalText(value: unknown, label: string): string {
  if (typeof value !== "string" || !/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(value)) {
    throw new Error(`invalid ${label}`);
  }
  if (!Number.isFinite(Number(value)) || Number(value) <= 0) throw new Error(`invalid ${label}`);
  return value;
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

function aggregateTradeStreamSymbol(stream: unknown): string | null {
  if (typeof stream !== "string") return null;
  const match = /^([a-z0-9]+)@aggTrade$/.exec(stream);
  return match?.[1]?.toUpperCase() ?? null;
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
  const streamSymbol = aggregateTradeStreamSymbol(envelope?.["stream"]);
  if (
    streamSymbol !== null &&
    (value["e"] !== "aggTrade" ||
      typeof value["s"] !== "string" ||
      value["s"].toUpperCase() !== streamSymbol)
  ) {
    throw new Error("mismatched aggregate-trade stream identity");
  }
  if (value["id"] !== undefined || value["result"] !== undefined) return { kind: "control" };
  if (value["st"] !== undefined && value["st"] !== 1) {
    throw new Error("non-USD-M market event rejected");
  }

  if (value["e"] === "aggTrade") {
    const symbol = String(value["s"] ?? "").toUpperCase();
    canonicalBase(symbol, 1);
    const priceText = positiveDecimalText(value["p"], "movement trade price");
    const quantityText = positiveDecimalText(value["q"], "movement trade quantity");
    return {
      kind: "trade",
      trade: {
        symbol,
        aggregateId: safeTimestamp(value["a"], "aggregate trade id"),
        price: finitePositive(priceText, "trade price"),
        quantity: finitePositive(quantityText, "trade quantity"),
        priceText,
        quantityText,
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
  private readonly movementObservationMaxCount: number;
  private readonly symbols = new Set<string>();
  private readonly developing = new Map<string, CollectorCandle>();
  private readonly latestCompleted = new Map<string, CollectorCandle>();
  private readonly latestTrade = new Map<string, AggregateTrade>();
  private readonly movementLastAcceptedTrade = new Map<string, AggregateTrade>();
  private readonly movementSeenTrades = new Map<string, Map<number, string>>();
  private readonly trades = new Map<string, TradeBuffer>();
  private readonly movementObservations = new Map<string, AggregateTrade[]>();
  private readonly movementResults = new Map<string, MovementBucketSnapshot>();
  private readonly movementSourceTransitions = new Map<string, MovementSourceTransition[]>();
  private readonly health = new Map<string, HealthState>();
  private readonly queue: CollectorCandle[] = [];
  private processing: Promise<void> | null = null;
  private recovery: Promise<void> | null = null;
  private reconnectCount = 0;
  private movementBoundary: number | null = null;
  private movementRetry: {
    sessionId: string;
    boundaryTime: number;
    symbols: MovementBoundarySymbolInput[];
  } | null = null;
  private movementGeneration = 0;
  private movementMembershipEpoch = 0;
  private readonly movementMembershipEpochs = new Map<string, number>();
  private movementSessionId = crypto.randomUUID();
  private movementLateRejections = 0;
  private movementPythonLateRejections = 0;

  constructor(private readonly dependencies: CollectorDependencies) {
    this.now = dependencies.now ?? Date.now;
    this.queueCapacity = dependencies.queueCapacity ?? 256;
    this.tradeWindowMs = dependencies.tradeWindowMs ?? 5 * 60_000;
    this.tradeMaxCount = dependencies.tradeMaxCount ?? 2_000;
    this.movementObservationMaxCount = MOVEMENT_OBSERVATION_BATCH_MAX;
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
    return this.movementResults.get(symbol.toUpperCase()) ?? null;
  }

  movementSnapshots(): MovementBucketSnapshot[] {
    return [...this.movementResults.values()].sort((left, right) =>
      left.symbol.localeCompare(right.symbol),
    );
  }

  /** Trades rejected because their exchange-time bucket had already been finalized. */
  movementLateRejections(): number {
    return this.movementLateRejections;
  }

  movementSourceStatus(symbol: string): MovementSourceState {
    return this.movementSourceTransitions.get(symbol.toUpperCase())?.at(-1)?.state ?? "UNAVAILABLE";
  }

  markMovementUnavailable(symbol: string): void {
    this.recordMovementSourceTransition(symbol, "UNAVAILABLE");
  }

  markAllMovementUnavailable(): void {
    for (const symbol of this.subscribedSymbols()) {
      this.markMovementUnavailable(symbol);
    }
  }

  private rememberMovementTrade(trade: AggregateTrade): void {
    const seen = this.movementSeenTrades.get(trade.symbol) ?? new Map<number, string>();
    const fingerprint = [
      trade.tradeTime,
      trade.eventTime,
      trade.priceText,
      trade.quantityText,
    ].join("|");
    seen.set(trade.aggregateId, fingerprint);
    while (seen.size > 4_096) {
      const oldest = seen.keys().next().value;
      if (oldest === undefined) break;
      seen.delete(oldest);
    }
    this.movementSeenTrades.set(trade.symbol, seen);
  }

  resetMovementTransportState(): void {
    this.movementGeneration += 1;
    this.movementSessionId = crypto.randomUUID();
    this.movementBoundary = null;
    this.movementRetry = null;
    this.movementLateRejections = 0;
    this.movementPythonLateRejections = 0;
    this.movementResults.clear();
    this.movementLastAcceptedTrade.clear();
    this.movementSeenTrades.clear();
    for (const [symbol, observations] of this.movementObservations) {
      observations.length = 0;
      this.movementSourceTransitions.set(symbol, [
        { at: this.now(), state: "UNAVAILABLE" },
      ]);
    }
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

  async advanceMovementBuckets(boundaryTime: number): Promise<void> {
    const generation = this.movementGeneration;
    safeTimestamp(boundaryTime, "movement boundary");
    if (boundaryTime % 5_000 !== 0) throw new Error("movement boundary is not aligned to five seconds");
    if (this.movementBoundary !== null && boundaryTime <= this.movementBoundary) return;

    const earliestObservedBoundary = [...this.movementObservations.values()]
      .flatMap((trades) => trades.map((trade) => Math.ceil(trade.tradeTime / 5_000) * 5_000))
      .filter((value) => value <= boundaryTime)
      .sort((left, right) => left - right)[0];
    let nextBoundary = this.movementRetry?.boundaryTime ?? (
      this.movementBoundary === null
        ? earliestObservedBoundary ?? boundaryTime
        : this.movementBoundary + 5_000
    );

    while (nextBoundary <= boundaryTime) {
      const isExactRetry = this.movementRetry?.boundaryTime === nextBoundary;
      if (!isExactRetry && this.movementBoundary !== null) {
        for (const [symbol, pending] of this.movementObservations) {
          const retained: AggregateTrade[] = [];
          for (const trade of pending) {
            const assignedBoundary = Math.ceil(trade.tradeTime / 5_000) * 5_000;
            if (assignedBoundary <= this.movementBoundary) {
              this.movementLateRejections += 1;
            } else {
              retained.push(trade);
            }
          }
          this.movementObservations.set(symbol, retained);
        }
      }
      const retry = this.movementRetry?.boundaryTime === nextBoundary
        ? this.movementRetry.symbols
        : this.subscribedSymbols().map((symbol) => {
            const observations = (this.movementObservations.get(symbol) ?? []).filter(
              (trade) => Math.ceil(trade.tradeTime / 5_000) * 5_000 <= nextBoundary,
            );
            return {
              symbol,
              sourceState: this.movementSourceStateForBoundary(symbol, nextBoundary),
              observations: observations.map((trade) => ({
                symbol,
                aggregateId: trade.aggregateId,
                price: trade.priceText,
                quantity: trade.quantityText,
                eventTime: trade.eventTime,
                tradeTime: trade.tradeTime,
                receivedAt: trade.receivedAt,
              })),
            };
          });
      const sessionId = this.movementRetry?.boundaryTime === nextBoundary
        ? this.movementRetry.sessionId
        : this.movementSessionId;
      const input = retry;
      const inputMembershipEpochs = new Map(
        input.map((item) => [item.symbol, this.movementMembershipEpochs.get(item.symbol)]),
      );
      const isCurrentMembership = (symbol: string): boolean => {
        const inputEpoch = inputMembershipEpochs.get(symbol);
        return inputEpoch !== undefined && inputEpoch === this.movementMembershipEpochs.get(symbol);
      };
      this.movementRetry = { sessionId, boundaryTime: nextBoundary, symbols: input };
      const result = await this.dependencies.advanceMovementBoundary(sessionId, nextBoundary, input);
      if (generation !== this.movementGeneration) return;
      for (const snapshot of result.snapshots) {
        if (isCurrentMembership(snapshot.symbol)) {
          this.movementResults.set(snapshot.symbol, snapshot);
        }
      }
      if (result.lateAfterFinalizationCount < this.movementPythonLateRejections) {
        this.movementPythonLateRejections = result.lateAfterFinalizationCount;
      } else {
        this.movementLateRejections +=
          result.lateAfterFinalizationCount - this.movementPythonLateRejections;
        this.movementPythonLateRejections = result.lateAfterFinalizationCount;
      }
      for (const item of input) {
        if (!isCurrentMembership(item.symbol)) continue;
        const consumedIds = new Set(item.observations.map((trade) => trade.aggregateId));
        const pending = this.movementObservations.get(item.symbol) ?? [];
        this.movementObservations.set(
          item.symbol,
          pending.filter((trade) => !consumedIds.has(trade.aggregateId)),
        );
      }
      this.movementBoundary = nextBoundary;
      this.movementRetry = null;
      nextBoundary += 5_000;
    }
  }

  developingCandle(symbol: string, timeframeMinutes: CollectorInterval): CollectorCandle | null {
    return this.developing.get(this.key(symbol, timeframeMinutes)) ?? null;
  }

  noteReconnect(): void {
    this.reconnectCount += 1;
  }

  markMovementConnectionStatus(status: MovementSourceState): void {
    for (const symbol of this.subscribedSymbols()) {
      this.recordMovementSourceTransition(symbol, status);
    }
  }

  async markConnectionStatus(
    status: CollectorHealthStatus,
    errorMessage: string | null,
  ): Promise<void> {
    const symbols = this.subscribedSymbols();
    for (const symbol of symbols) {
      this.recordMovementSourceTransition(symbol, status);
    }
    await this.persistConnectionHealth(symbols, status, errorMessage);
  }

  async markCandleConnectionStatus(
    status: CollectorHealthStatus,
    errorMessage: string | null,
  ): Promise<void> {
    await this.persistConnectionHealth(this.subscribedSymbols(), status, errorMessage);
  }

  private async persistConnectionHealth(
    symbols: string[],
    status: CollectorHealthStatus,
    errorMessage: string | null,
  ): Promise<void> {
    for (const symbol of symbols) {
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
    for (const symbol of this.symbols) {
      if (next.has(symbol)) continue;
      this.symbols.delete(symbol);
      this.trades.delete(symbol);
      this.latestTrade.delete(symbol);
      this.movementLastAcceptedTrade.delete(symbol);
      this.movementSeenTrades.delete(symbol);
      this.movementObservations.delete(symbol);
      this.movementResults.delete(symbol);
      this.movementSourceTransitions.delete(symbol);
      this.movementMembershipEpochs.delete(symbol);
      for (const interval of COLLECTOR_INTERVALS)
        this.developing.delete(this.key(symbol, interval));
    }
    for (const symbol of next) {
      if (this.symbols.has(symbol)) continue;
      this.symbols.add(symbol);
      this.trades.set(symbol, new TradeBuffer(this.tradeWindowMs, this.tradeMaxCount));
      this.movementSeenTrades.set(symbol, new Map());
      this.movementObservations.set(symbol, []);
      this.movementMembershipEpoch += 1;
      this.movementMembershipEpochs.set(symbol, this.movementMembershipEpoch);
      this.movementSourceTransitions.set(symbol, [
        { at: this.now(), state: "RECOVERING" },
      ]);
      for (const interval of COLLECTOR_INTERVALS) await this.bootstrap(symbol, interval);
    }
  }

  accept(raw: unknown, receivedAt = this.now()): boolean {
    let event;
    try {
      event = parseBinanceMarketMessage(raw, receivedAt);
    } catch {
      const envelope = raw as Record<string, unknown> | null;
      const candidate = envelope && typeof envelope === "object" && envelope["data"]
        ? envelope["data"] as Record<string, unknown>
        : envelope;
      const stream = envelope && typeof envelope === "object" ? envelope["stream"] : null;
      const streamSymbol = aggregateTradeStreamSymbol(stream);
      const dataSymbol = candidate && typeof candidate === "object" && candidate["e"] === "aggTrade"
        ? String(candidate["s"] ?? "").toUpperCase()
        : "";
      let attributable: string | null = null;
      if (streamSymbol !== null) {
        if (this.symbols.has(streamSymbol)) attributable = streamSymbol;
      } else if (dataSymbol && this.symbols.has(dataSymbol)) {
        attributable = dataSymbol;
      }
      if (attributable) {
        this.markMovementUnavailable(attributable);
        this.dependencies.onOverload?.();
      }
      return false;
    }
    if (event.kind === "control") return true;
    if (!this.symbols.has(event.kind === "trade" ? event.trade.symbol : event.candle.symbol)) {
      return false;
    }
    if (event.kind === "trade") {
      const seen = this.movementSeenTrades.get(event.trade.symbol);
      const seenFingerprint = seen?.get(event.trade.aggregateId);
      if (seenFingerprint !== undefined) {
        const incomingFingerprint = [
          event.trade.tradeTime,
          event.trade.eventTime,
          event.trade.priceText,
          event.trade.quantityText,
        ].join("|");
        if (incomingFingerprint === seenFingerprint) return false;
        this.markMovementUnavailable(event.trade.symbol);
        this.dependencies.onOverload?.();
        return false;
      }
      const tradeBoundary = Math.ceil(event.trade.tradeTime / 5_000) * 5_000;
      if (this.movementBoundary !== null && tradeBoundary <= this.movementBoundary) {
        this.movementLateRejections += 1;
        this.rememberMovementTrade(event.trade);
        return false;
      }
      const previous = this.movementLastAcceptedTrade.get(event.trade.symbol);
      if (
        previous &&
        (event.trade.tradeTime < previous.tradeTime ||
          (event.trade.tradeTime === previous.tradeTime &&
            event.trade.aggregateId <= previous.aggregateId))
      ) {
        this.markMovementUnavailable(event.trade.symbol);
        this.dependencies.onOverload?.();
        return false;
      }
      const movementQueue = this.movementObservations.get(event.trade.symbol)!;
      if (movementQueue.length >= this.movementObservationMaxCount) {
        this.markMovementUnavailable(event.trade.symbol);
        this.dependencies.onOverload?.();
        return false;
      }
      movementQueue.push(event.trade);
      this.rememberMovementTrade(event.trade);
      this.movementLastAcceptedTrade.set(event.trade.symbol, event.trade);
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

  private recordMovementSourceTransition(
    symbol: string,
    state: MovementSourceState,
  ): void {
    const normalized = symbol.toUpperCase();
    const transitions = this.movementSourceTransitions.get(normalized) ?? [];
    if (transitions.at(-1)?.state === state) return;
    transitions.push({ at: this.now(), state });
    if (transitions.length > 1_024) {
      this.movementSourceTransitions.set(normalized, transitions.slice(-1_023));
    } else {
      this.movementSourceTransitions.set(normalized, transitions);
    }
  }

  private movementSourceStateForBoundary(
    symbol: string,
    boundaryTime: number,
  ): MovementSourceState {
    const transitions = this.movementSourceTransitions.get(symbol) ?? [];
    const intervalStart = boundaryTime - 5_000;
    let stateAtStart: MovementSourceState = "UNAVAILABLE";
    let stateAtEnd: MovementSourceState = "UNAVAILABLE";
    const intervalStates: MovementSourceState[] = [];
    for (const transition of transitions) {
      if (transition.at <= intervalStart) stateAtStart = transition.state;
      if (transition.at > intervalStart && transition.at <= boundaryTime) {
        intervalStates.push(transition.state);
      }
      if (transition.at <= boundaryTime) stateAtEnd = transition.state;
    }
    const nonLive = [stateAtStart, ...intervalStates].filter((state) => state !== "LIVE");
    let intervalState: MovementSourceState;
    if (nonLive.includes("UNAVAILABLE")) intervalState = "UNAVAILABLE";
    else if (nonLive.includes("STALE")) intervalState = "STALE";
    else if (nonLive.includes("RECOVERING")) intervalState = "RECOVERING";
    else intervalState = stateAtEnd;

    const retainFrom = boundaryTime - 5_000;
    let anchorIndex = -1;
    for (let index = 0; index < transitions.length; index += 1) {
      if (transitions[index]!.at <= retainFrom) anchorIndex = index;
    }
    if (anchorIndex > 0) {
      this.movementSourceTransitions.set(symbol, transitions.slice(anchorIndex));
    }
    return intervalState;
  }
}
