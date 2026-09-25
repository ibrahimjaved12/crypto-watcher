/**
 * Pure market-movement evaluation engine (Issue #74).
 *
 * Orchestrates the completed pure layers once per shared, finalized five-second
 * exchange-time boundary:
 *
 *   #70 buckets -> #71 metrics -> #72 classification -> #73 lifecycle
 *
 * It performs no I/O and owns no WebSocket. The server runtime feeds it the shared
 * collector snapshots, per-symbol source status, and caller-supplied normalization
 * history, then persists whatever #73 decides is due.
 */
import {
  MOVEMENT_BUCKET_MS,
  MOVEMENT_WINDOWS_MINUTES,
  type MovementBucketSnapshot,
  type MovementWindowMinutes,
} from "./movement-buckets";
import {
  calculateMarketMovement,
  DEFAULT_MARKET_MOVEMENT_CONFIG,
  type HistoricalMovementWindowInput,
  type MarketMovementConfig,
  type MarketMovementEvaluation,
  type MarketMovementSymbolInput,
} from "./movement-metrics";
import {
  classifyMarketState,
  DEFAULT_MARKET_STATE_CLASSIFIER_CONFIG,
  type MarketStateClassification,
  type MarketStateClassifierConfig,
  type MarketStateEvidence,
} from "./market-state-classifier";
import {
  markMarketEpisodeStatePersisted,
  processMarketEpisodeLifecycle,
  type MarketEpisodeLifecycleConfig,
  type MarketEpisodeLifecycleState,
  type MarketMovementEvent,
  type ProcessMarketEpisodeLifecycleResult,
} from "./market-episode-lifecycle";
import type { MarketUniverse } from "./market-universe";
import type {
  MovementCandle,
  MovementSourceStatus,
  PersistedMarketMovementCurrentEvidence,
  PersistedMovementTransition,
} from "./market-movement-state";

const MINUTE_MS = 60_000;

/** Bound on boundaries caught up in a single advance before jumping to the latest safe one. */
export const MOVEMENT_ENGINE_MAX_CATCHUP_BOUNDARIES = 60;

export type MovementNormalizationHistory = Map<
  string,
  Partial<Record<MovementWindowMinutes, HistoricalMovementWindowInput>>
>;

export type MovementBoundaryEvaluation = {
  boundaryTime: number;
  movement: MarketMovementEvaluation;
  classification: MarketStateClassification;
  lifecycle: ProcessMarketEpisodeLifecycleResult;
};

export type MovementEngineAdvanceInput = {
  finalizableBoundary: number;
  snapshots: ReadonlyMap<string, MovementBucketSnapshot>;
  sourceStatus: ReadonlyMap<string, MovementSourceStatus>;
  historical: MovementNormalizationHistory;
  universe: MarketUniverse;
  movementConfig?: MarketMovementConfig;
  classifierConfig?: MarketStateClassifierConfig;
  lifecycleConfig?: MarketEpisodeLifecycleConfig;
};

export type MovementCurrentEvidenceInput = {
  evidence: MarketStateEvidence;
  classification: MarketStateClassification;
  universe: MarketUniverse;
  status: PersistedMarketMovementCurrentEvidence["engine"]["status"];
  lateAfterFinalizationCount: number;
  engineUpdatedAt: number;
  lastSourceEventTime: number | null;
  lastTradeTime: number | null;
  mostRecentTransition: PersistedMovementTransition | null;
};

function finiteNonnegative(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

/**
 * Keeps only the trailing contiguous run of one-minute candles, oldest to newest.
 * A gap means the earlier candles cannot be trusted as comparable history.
 */
function trailingContiguousRun(candles: readonly MovementCandle[]): MovementCandle[] {
  const usable = candles
    .filter(
      (candle) =>
        Number.isSafeInteger(candle.openTime) &&
        candle.openTime >= 0 &&
        Number.isFinite(candle.close) &&
        candle.close > 0 &&
        Number.isFinite(candle.volume) &&
        candle.volume >= 0,
    )
    .sort((left, right) => left.openTime - right.openTime);
  const deduped: MovementCandle[] = [];
  for (const candle of usable) {
    const previous = deduped.at(-1);
    if (previous && previous.openTime === candle.openTime) deduped[deduped.length - 1] = candle;
    else deduped.push(candle);
  }
  if (deduped.length === 0) return [];
  let start = deduped.length - 1;
  while (start > 0 && deduped[start]!.openTime - deduped[start - 1]!.openTime === MINUTE_MS) {
    start -= 1;
  }
  return deduped.slice(start);
}

/**
 * Derives #71's caller-supplied normalization inputs from canonical completed
 * one-minute candles. Window returns use the same horizon as the live window
 * (`log(close[t]/close[t-window])`); five-second history is never fabricated.
 */
export function buildMovementNormalizationHistory(
  candlesBySymbol: ReadonlyMap<string, readonly MovementCandle[]>,
  config: MarketMovementConfig = DEFAULT_MARKET_MOVEMENT_CONFIG,
): MovementNormalizationHistory {
  const result: MovementNormalizationHistory = new Map();
  for (const [symbol, candles] of candlesBySymbol) {
    result.set(symbol.toUpperCase(), buildSymbolNormalization(candles, config));
  }
  return result;
}

function buildSymbolNormalization(
  candles: readonly MovementCandle[],
  config: MarketMovementConfig,
): Partial<Record<MovementWindowMinutes, HistoricalMovementWindowInput>> {
  const windows: Partial<Record<MovementWindowMinutes, HistoricalMovementWindowInput>> = {};
  const run = trailingContiguousRun(candles);
  if (run.length < 2) return windows;
  const usableCoverageMs = run.at(-1)!.openTime + MINUTE_MS - run[0]!.openTime;

  for (const windowMinutes of MOVEMENT_WINDOWS_MINUTES) {
    const offset = windowMinutes;
    if (run.length <= offset) continue;
    const returns: number[] = [];
    const previousNotionalVolumes: number[] = [];
    for (let index = offset; index < run.length; index += windowMinutes) {
      const current = run[index]!;
      const previous = run[index - offset]!;
      returns.push(Math.log(current.close / previous.close));
      let notional = 0;
      for (let step = index - offset + 1; step <= index; step += 1) {
        const candle = run[step]!;
        notional += candle.close * candle.volume;
      }
      previousNotionalVolumes.push(notional);
    }
    if (returns.length === 0) continue;
    windows[windowMinutes] = {
      returns,
      usableCoverageMs,
      previousNotionalVolumes: previousNotionalVolumes.slice(-config.rvolComparisonWindows),
    };
  }
  return windows;
}

/** Converts a #73 movement event into the compact transition consumers inspect. */
export function transitionToPersisted(event: MarketMovementEvent): PersistedMovementTransition {
  return {
    transition: event.transition,
    transitionReason: event.transitionReason,
    episodeId: event.episodeId,
    direction: event.direction,
    fromDirection: event.fromDirection,
    toDirection: event.toDirection,
    pace: event.pace,
    episodeStartBoundaryTime: event.episodeStartBoundaryTime,
    evaluationBoundaryTime: event.evaluationBoundaryTime,
  };
}

/**
 * Builds the enriched bounded current evidence persisted alongside #73's
 * `market_state_current`: primary 5m evidence plus 1m/15m context, the exact
 * universe, engine status/timestamps and the most recent transition.
 */
export function buildMovementCurrentEvidence(
  input: MovementCurrentEvidenceInput,
): PersistedMarketMovementCurrentEvidence {
  const primary = input.classification.windows.find((window) => window.windowMinutes === 5);
  if (!primary) throw new Error("movement classification is missing the 5m primary window");
  return {
    ...input.evidence,
    windowsContext: input.classification.windows,
    universe: {
      id: input.universe.id,
      version: input.universe.version,
      configuredSymbols: [...input.universe.symbols],
      includedSymbols: [...primary.evidence.includedSymbols],
      excludedSymbols: primary.evidence.excludedSymbols.map(({ symbol, reasons }) => ({
        symbol,
        reasons: [...reasons],
      })),
    },
    engine: {
      status: input.status,
      lateAfterFinalizationCount: input.lateAfterFinalizationCount,
    },
    timestamps: {
      evaluationBoundaryTime: input.classification.evaluationBoundaryTime,
      lastSourceEventTime: input.lastSourceEventTime,
      lastTradeTime: input.lastTradeTime,
      engineUpdatedAt: input.engineUpdatedAt,
    },
    mostRecentTransition: input.mostRecentTransition,
  };
}

export class MarketMovementEngine {
  private lastEvaluated: number | null = null;
  private lifecycleState: MarketEpisodeLifecycleState | null = null;

  constructor(private readonly options: { maxCatchUpBoundaries?: number } = {}) {}

  get lastEvaluatedBoundary(): number | null {
    return this.lastEvaluated;
  }

  /** Seeds #73 state loaded from the bounded operational current state after restart. */
  restoreLifecycleState(state: MarketEpisodeLifecycleState | null): void {
    this.lifecycleState = state;
  }

  /** Marks the last evaluation as durably persisted so cadence accounting advances. */
  acknowledgePersisted(): void {
    if (this.lifecycleState) {
      this.lifecycleState = markMarketEpisodeStatePersisted(this.lifecycleState);
    }
  }

  /**
   * Evaluates every finalized boundary up to `finalizableBoundary`. Idempotent:
   * re-advancing to an already-evaluated boundary performs no new evaluation.
   */
  advance(input: MovementEngineAdvanceInput): MovementBoundaryEvaluation[] {
    if (
      !Number.isSafeInteger(input.finalizableBoundary) ||
      input.finalizableBoundary < 0 ||
      input.finalizableBoundary % MOVEMENT_BUCKET_MS !== 0
    ) {
      throw new Error("movement finalizable boundary must be an aligned exchange boundary");
    }
    // A restored episode can only advance; never evaluate behind its persisted boundary.
    if (
      this.lastEvaluated === null &&
      this.lifecycleState !== null &&
      input.finalizableBoundary < this.lifecycleState.evaluationBoundaryTime
    ) {
      return [];
    }
    if (this.lastEvaluated !== null && input.finalizableBoundary <= this.lastEvaluated) return [];
    let next =
      this.lastEvaluated === null
        ? input.finalizableBoundary
        : this.lastEvaluated + MOVEMENT_BUCKET_MS;
    if (next > input.finalizableBoundary) return [];
    const maxCatchUp = this.options.maxCatchUpBoundaries ?? MOVEMENT_ENGINE_MAX_CATCHUP_BOUNDARIES;
    if (input.finalizableBoundary - next > maxCatchUp * MOVEMENT_BUCKET_MS) {
      next = input.finalizableBoundary;
    }
    const results: MovementBoundaryEvaluation[] = [];
    for (
      let boundary = next;
      boundary <= input.finalizableBoundary;
      boundary += MOVEMENT_BUCKET_MS
    ) {
      results.push(this.evaluateBoundary(boundary, input));
    }
    return results;
  }

  private evaluateBoundary(
    boundaryTime: number,
    input: MovementEngineAdvanceInput,
  ): MovementBoundaryEvaluation {
    const symbols: MarketMovementSymbolInput[] = input.universe.symbols.map((symbol) => ({
      symbol,
      sourceStatus: input.sourceStatus.get(symbol) ?? "UNAVAILABLE",
      instrumentCompatible: true,
      snapshot: input.snapshots.get(symbol) ?? null,
      historical: input.historical.get(symbol) ?? {},
    }));
    const movement = calculateMarketMovement({
      evaluationBoundaryTime: boundaryTime,
      universe: {
        id: input.universe.id,
        version: input.universe.version,
        symbols: input.universe.symbols,
      },
      symbols,
      ...(input.movementConfig ? { config: input.movementConfig } : {}),
    });
    const classification = classifyMarketState({
      movement,
      config: input.classifierConfig ?? DEFAULT_MARKET_STATE_CLASSIFIER_CONFIG,
    });
    const lifecycle = processMarketEpisodeLifecycle({
      classification,
      movement,
      previousState: this.lifecycleState,
      ...(input.lifecycleConfig ? { config: input.lifecycleConfig } : {}),
    });
    this.lifecycleState = lifecycle.nextState;
    this.lastEvaluated = boundaryTime;
    return { boundaryTime, movement, classification, lifecycle };
  }
}

/** Latest exchange event time / trade time observed across the shared snapshots. */
export function snapshotEventTimes(snapshots: ReadonlyMap<string, MovementBucketSnapshot>): {
  lastSourceEventTime: number | null;
  lastTradeTime: number | null;
} {
  let lastSourceEventTime: number | null = null;
  let lastTradeTime: number | null = null;
  for (const snapshot of snapshots.values()) {
    const latest = snapshot.buckets.at(-1);
    const eventTime = finiteNonnegative(latest?.lastRealEventTime);
    if (eventTime !== null && (lastSourceEventTime === null || eventTime > lastSourceEventTime)) {
      lastSourceEventTime = eventTime;
    }
    const tradeTime = finiteNonnegative(snapshot.latestRealTradeTime);
    if (tradeTime !== null && (lastTradeTime === null || tradeTime > lastTradeTime)) {
      lastTradeTime = tradeTime;
    }
  }
  return { lastSourceEventTime, lastTradeTime };
}
