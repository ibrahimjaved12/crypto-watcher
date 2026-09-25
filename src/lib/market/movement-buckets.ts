export const MOVEMENT_BUCKET_MS = 5_000;
export const MOVEMENT_HISTORY_MINUTES = 35;
export const MOVEMENT_BUCKET_CAPACITY = (MOVEMENT_HISTORY_MINUTES * 60_000) / MOVEMENT_BUCKET_MS;
export const MAX_LAST_TRADE_AGE_SECONDS = 15;
export const MAX_LAST_TRADE_AGE_MS = MAX_LAST_TRADE_AGE_SECONDS * 1_000;
export const MOVEMENT_WINDOWS_MINUTES = [1, 5, 15] as const;

/** A trade whose exchange time belongs to an already-finalized bucket is rejected, never rewritten. */
export const MOVEMENT_LATE_FINALIZATION_MESSAGE =
  "movement trade arrived after its bucket was finalized";

export type MovementWindowMinutes = (typeof MOVEMENT_WINDOWS_MINUTES)[number];
export type MovementReadinessStatus = "READY" | "WARMING" | "STALE";

export type MovementTradeInput = {
  symbol: string;
  price: number;
  quantity: number;
  eventTime: number;
  tradeTime: number;
  /** Local receive time of the observation; carried for provenance, never used in math. */
  receivedAt: number;
};

export type MovementBucket = {
  boundaryTime: number;
  endpointPrice: number | null;
  baseQuantity: number;
  quoteVolume: number;
  tradeCount: number;
  lastRealTradeTime: number | null;
  lastRealEventTime: number | null;
  lastRealReceivedAt: number | null;
  carriedForward: boolean;
  provider: "binance-usdm";
  instrumentId: string;
  nativeSymbol: string;
  symbol: string;
  marketType: "futures";
  contractType: "perpetual";
  priceType: "trade";
};

export type MovementWindowReadiness = {
  windowMinutes: MovementWindowMinutes;
  status: MovementReadinessStatus;
  requiredHistoryMs: number;
  availableHistoryMs: number;
};

export type MovementBucketSnapshot = {
  symbol: string;
  provider: "binance-usdm";
  instrumentId: string;
  priceType: "trade";
  bucketMs: typeof MOVEMENT_BUCKET_MS;
  maxLastTradeAgeMs: typeof MAX_LAST_TRADE_AGE_MS;
  buckets: MovementBucket[];
  latestRealTradeTime: number | null;
  latestRealReceivedAt: number | null;
  readiness: Record<MovementWindowMinutes, MovementWindowReadiness>;
};

type PendingBucket = {
  boundaryTime: number;
  endpointPrice: number;
  baseQuantity: number;
  quoteVolume: number;
  tradeCount: number;
  lastRealTradeTime: number;
  lastRealEventTime: number;
  lastRealReceivedAt: number;
};

class BoundedBucketRing {
  private readonly values: Array<MovementBucket | undefined>;
  private next = 0;
  private size = 0;

  constructor(readonly capacity: number) {
    if (!Number.isSafeInteger(capacity) || capacity < 1) {
      throw new Error("invalid movement bucket capacity");
    }
    this.values = new Array(capacity);
  }

  push(bucket: MovementBucket): void {
    this.values[this.next] = bucket;
    this.next = (this.next + 1) % this.capacity;
    this.size = Math.min(this.size + 1, this.capacity);
  }

  snapshot(): MovementBucket[] {
    const start = (this.next - this.size + this.capacity) % this.capacity;
    return Array.from({ length: this.size }, (_, index) => {
      return { ...this.values[(start + index) % this.capacity]! };
    });
  }
}

function safeNonnegativeInteger(value: number, label: string): void {
  if (!Number.isSafeInteger(value) || value < 0) throw new Error(`invalid ${label}`);
}

function finitePositive(value: number, label: string): void {
  if (!Number.isFinite(value) || value <= 0) throw new Error(`invalid ${label}`);
}

function boundaryAtOrAfter(exchangeTime: number): number {
  return Math.ceil(exchangeTime / MOVEMENT_BUCKET_MS) * MOVEMENT_BUCKET_MS;
}

class SymbolMovementBuckets {
  private readonly ring: BoundedBucketRing;
  private pending: PendingBucket | null = null;
  private lastFinalizedBoundary: number | null = null;
  private lastAcceptedTradeTime: number | null = null;
  private lastRealTrade: MovementTradeInput | null = null;

  constructor(
    readonly symbol: string,
    capacity: number,
  ) {
    this.ring = new BoundedBucketRing(capacity);
  }

  accept(trade: MovementTradeInput): void {
    if (trade.symbol.toUpperCase() !== this.symbol) throw new Error("movement symbol mismatch");
    finitePositive(trade.price, "movement trade price");
    finitePositive(trade.quantity, "movement trade quantity");
    safeNonnegativeInteger(trade.tradeTime, "movement trade time");
    safeNonnegativeInteger(trade.eventTime, "movement event time");
    safeNonnegativeInteger(trade.receivedAt, "movement receive time");
    if (this.lastAcceptedTradeTime !== null && trade.tradeTime < this.lastAcceptedTradeTime) {
      throw new Error("out-of-order movement trade");
    }
    if (this.lastFinalizedBoundary !== null && trade.tradeTime <= this.lastFinalizedBoundary) {
      throw new Error(MOVEMENT_LATE_FINALIZATION_MESSAGE);
    }

    const safeBoundary =
      Math.floor((trade.tradeTime - 1) / MOVEMENT_BUCKET_MS) * MOVEMENT_BUCKET_MS;
    if (safeBoundary >= 0) this.advanceTo(safeBoundary);

    const boundaryTime = boundaryAtOrAfter(trade.tradeTime);
    if (!this.pending) {
      this.pending = {
        boundaryTime,
        endpointPrice: trade.price,
        baseQuantity: trade.quantity,
        quoteVolume: trade.price * trade.quantity,
        tradeCount: 1,
        lastRealTradeTime: trade.tradeTime,
        lastRealEventTime: trade.eventTime,
        lastRealReceivedAt: trade.receivedAt,
      };
    } else {
      if (this.pending.boundaryTime !== boundaryTime) {
        throw new Error("movement bucket handoff failed");
      }
      this.pending.endpointPrice = trade.price;
      this.pending.baseQuantity += trade.quantity;
      this.pending.quoteVolume += trade.price * trade.quantity;
      this.pending.tradeCount += 1;
      this.pending.lastRealTradeTime = trade.tradeTime;
      this.pending.lastRealEventTime = trade.eventTime;
      this.pending.lastRealReceivedAt = trade.receivedAt;
    }
    this.lastAcceptedTradeTime = trade.tradeTime;
    this.lastRealTrade = { ...trade, symbol: this.symbol };
  }

  advanceTo(boundaryTime: number): void {
    safeNonnegativeInteger(boundaryTime, "movement boundary");
    if (boundaryTime % MOVEMENT_BUCKET_MS !== 0) {
      throw new Error("movement boundary is not aligned to five seconds");
    }
    if (this.lastFinalizedBoundary !== null && boundaryTime <= this.lastFinalizedBoundary) {
      return;
    }
    if (!this.pending && !this.lastRealTrade) return;

    let nextBoundary =
      this.lastFinalizedBoundary === null
        ? this.pending!.boundaryTime
        : this.lastFinalizedBoundary + MOVEMENT_BUCKET_MS;
    const oldestRetainedBoundary = boundaryTime - (this.ring.capacity - 1) * MOVEMENT_BUCKET_MS;
    if (nextBoundary < oldestRetainedBoundary) {
      if (this.pending && this.pending.boundaryTime < oldestRetainedBoundary) this.pending = null;
      this.lastFinalizedBoundary = oldestRetainedBoundary - MOVEMENT_BUCKET_MS;
      nextBoundary = oldestRetainedBoundary;
    }

    while (nextBoundary <= boundaryTime) {
      this.ring.push(this.finalize(nextBoundary));
      this.lastFinalizedBoundary = nextBoundary;
      nextBoundary += MOVEMENT_BUCKET_MS;
    }
  }

  snapshot(): MovementBucketSnapshot {
    const buckets = this.ring.snapshot();
    const readiness = Object.fromEntries(
      MOVEMENT_WINDOWS_MINUTES.map((windowMinutes) => [
        windowMinutes,
        this.readiness(windowMinutes, buckets),
      ]),
    ) as Record<MovementWindowMinutes, MovementWindowReadiness>;
    return {
      symbol: this.symbol,
      provider: "binance-usdm",
      instrumentId: `binance-usdm:${this.symbol}`,
      priceType: "trade",
      bucketMs: MOVEMENT_BUCKET_MS,
      maxLastTradeAgeMs: MAX_LAST_TRADE_AGE_MS,
      buckets,
      latestRealTradeTime: this.lastRealTrade?.tradeTime ?? null,
      latestRealReceivedAt: this.lastRealTrade?.receivedAt ?? null,
      readiness,
    };
  }

  private finalize(boundaryTime: number): MovementBucket {
    const pending = this.pending?.boundaryTime === boundaryTime ? this.pending : null;
    if (pending) this.pending = null;
    const age = this.lastRealTrade ? boundaryTime - this.lastRealTrade.tradeTime : Infinity;
    const carry = !pending && age >= 0 && age <= MAX_LAST_TRADE_AGE_MS;
    return {
      boundaryTime,
      endpointPrice: pending?.endpointPrice ?? (carry ? this.lastRealTrade!.price : null),
      baseQuantity: pending?.baseQuantity ?? 0,
      quoteVolume: pending?.quoteVolume ?? 0,
      tradeCount: pending?.tradeCount ?? 0,
      lastRealTradeTime: pending?.lastRealTradeTime ?? this.lastRealTrade?.tradeTime ?? null,
      lastRealEventTime: pending?.lastRealEventTime ?? this.lastRealTrade?.eventTime ?? null,
      lastRealReceivedAt: pending?.lastRealReceivedAt ?? this.lastRealTrade?.receivedAt ?? null,
      carriedForward: carry,
      provider: "binance-usdm",
      instrumentId: `binance-usdm:${this.symbol}`,
      nativeSymbol: this.symbol,
      symbol: this.symbol,
      marketType: "futures",
      contractType: "perpetual",
      priceType: "trade",
    };
  }

  private readiness(
    windowMinutes: MovementWindowMinutes,
    buckets: MovementBucket[],
  ): MovementWindowReadiness {
    const requiredHistoryMs = windowMinutes * 2 * 60_000;
    const latest = buckets.at(-1);
    if (!latest) {
      return { windowMinutes, status: "WARMING", requiredHistoryMs, availableHistoryMs: 0 };
    }
    if (latest.endpointPrice === null) {
      return { windowMinutes, status: "STALE", requiredHistoryMs, availableHistoryMs: 0 };
    }
    let first = latest;
    for (let index = buckets.length - 2; index >= 0; index -= 1) {
      const candidate = buckets[index]!;
      if (
        candidate.endpointPrice === null ||
        candidate.boundaryTime + MOVEMENT_BUCKET_MS !== first.boundaryTime
      ) {
        break;
      }
      first = candidate;
    }
    const availableHistoryMs = latest.boundaryTime - first.boundaryTime;
    return {
      windowMinutes,
      status: availableHistoryMs >= requiredHistoryMs ? "READY" : "WARMING",
      requiredHistoryMs,
      availableHistoryMs,
    };
  }
}

/** Bounded, persistence-free input shared by live processing and deterministic replay. */
export class FuturesMovementBuckets {
  private readonly symbols = new Map<string, SymbolMovementBuckets>();
  private lateAfterFinalization = 0;

  constructor(private readonly capacity = MOVEMENT_BUCKET_CAPACITY) {
    if (!Number.isSafeInteger(capacity) || capacity < 1) {
      throw new Error("invalid movement history capacity");
    }
  }

  /** Count of trades rejected because their bucket had already been finalized. */
  get lateAfterFinalizationCount(): number {
    return this.lateAfterFinalization;
  }

  reconcile(symbols: Iterable<string>): void {
    const next = new Set([...symbols].map((symbol) => symbol.toUpperCase()));
    for (const symbol of this.symbols.keys()) {
      if (!next.has(symbol)) this.symbols.delete(symbol);
    }
    for (const symbol of next) {
      if (!this.symbols.has(symbol)) {
        this.symbols.set(symbol, new SymbolMovementBuckets(symbol, this.capacity));
      }
    }
  }

  accept(trade: MovementTradeInput): boolean {
    const state = this.symbols.get(trade.symbol.toUpperCase());
    if (!state) return false;
    try {
      state.accept(trade);
      return true;
    } catch (error) {
      if (error instanceof Error && error.message === MOVEMENT_LATE_FINALIZATION_MESSAGE) {
        this.lateAfterFinalization += 1;
      }
      throw error;
    }
  }

  advanceTo(boundaryTime: number): void {
    for (const state of this.symbols.values()) state.advanceTo(boundaryTime);
  }

  snapshot(symbol: string): MovementBucketSnapshot | null {
    return this.symbols.get(symbol.toUpperCase())?.snapshot() ?? null;
  }

  snapshots(): MovementBucketSnapshot[] {
    return [...this.symbols.values()]
      .map((state) => state.snapshot())
      .sort((left, right) => left.symbol.localeCompare(right.symbol));
  }
}
