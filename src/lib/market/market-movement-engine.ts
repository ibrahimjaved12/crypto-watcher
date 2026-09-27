/**
 * Pure market-movement evaluation engine (Issue #74).
 *
 * Orchestrates the completed pure layers once per shared, finalized five-second
 * exchange-time boundary:
 *
 *   #70 buckets -> #71 metrics -> #72 classification -> #73 lifecycle
 *
 * The runtime supplies canonical Python #71/#72/#73 for each boundary.
 * This engine owns only ordered boundary sequencing and the opaque state handoff.
 */
import {
  MOVEMENT_BUCKET_MS,
  MOVEMENT_WINDOWS_MINUTES,
  type MovementBucketSnapshot,
} from "./movement-contract";
import {
  MARKET_MOVEMENT_ALGORITHM_VERSION,
  type MarketMovementEvaluation,
} from "./movement-metrics-contract";
import {
  type CanonicalMarketEpisodeTransition, type MarketEpisodeLifecycleTransport,
  type SerializedMarketEpisodeLifecycleState,
} from "./market-episode-contract";
import type { MarketClassification } from "./market-state-contract";
import type { MarketUniverse } from "./market-universe";
import type {
  PersistedMarketMovementCurrentEvidence,
  PersistedMovementTransition,
} from "./market-movement-state";

/** Bound on boundaries caught up in a single advance before jumping to the latest safe one. */
export const MOVEMENT_ENGINE_MAX_CATCHUP_BOUNDARIES = 60;

export type MovementBoundaryEvaluation = {
  boundaryTime: number;
  movement: MarketMovementEvaluation;
  classification: MarketClassification;
  lifecycle: MarketEpisodeLifecycleTransport;
};

export type MovementEngineAdvanceInput = {
  finalizableBoundary: number;
  universe: MarketUniverse;
  lifecycleForBoundary: (boundaryTime: number,
    previousLifecycleState: SerializedMarketEpisodeLifecycleState | null,
    interruptPreviousState: boolean) => Promise<{
      movement: MarketMovementEvaluation;
      classification: MarketClassification;
      lifecycle: MarketEpisodeLifecycleTransport;
    }>;
};

export type MovementCurrentEvidenceInput = {
  classification: MarketClassification;
  universe: MarketUniverse;
  status: PersistedMarketMovementCurrentEvidence["engine"]["status"];
  lateAfterFinalizationCount: number;
  engineUpdatedAt: number;
  lastSourceEventTime: number | null;
  lastTradeTime: number | null;
  lastReceivedAt: number | null;
  finalizationConfigVersion: string;
  finalizationGraceMs: number;
  mostRecentTransition: PersistedMovementTransition | null;
};

function finiteNonnegative(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

/** Converts a #73 movement event into the compact transition consumers inspect. */
export function transitionToPersisted(event: CanonicalMarketEpisodeTransition): PersistedMovementTransition {
  return {
    transition: event.transition,
    transitionReason: event.transition_reason,
    episodeId: event.episode_id,
    direction: event.episode_direction,
    fromDirection: event.from_direction,
    toDirection: event.to_direction,
    pace: event.pace.available ? event.pace.value : "NOT_APPLICABLE",
    episodeStartBoundaryTime: event.episode_start_boundary_time_ms,
    evaluationBoundaryTime: event.evaluation_boundary_time_ms,
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
    primaryWindow: primary,
    windowsContext: input.classification.windows,
    universe: {
      id: input.universe.id,
      version: input.universe.version,
      configuredSymbols: [...input.universe.symbols],
      includedSymbols: [...primary.includedSymbols],
      excludedSymbols: primary.excludedSymbols.map(({ symbol, reasons }) => ({
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
      lastReceivedAt: input.lastReceivedAt,
      engineUpdatedAt: input.engineUpdatedAt,
    },
    finalizationConfigVersion: input.finalizationConfigVersion,
    finalizationGraceMs: input.finalizationGraceMs,
    mostRecentTransition: input.mostRecentTransition,
  };
}

export class MarketMovementEngine {
  private lastEvaluated: number | null = null;
  private lastUniverseVersion: string | null = null;
  private lifecycleState: SerializedMarketEpisodeLifecycleState | null = null;
  private interruptPreviousState = false;
  private restoredBoundaryTime: number | null = null;

  constructor(private readonly options: { maxCatchUpBoundaries?: number } = {}) {}

  get lastEvaluatedBoundary(): number | null {
    return this.lastEvaluated;
  }

  /** First boundary this engine will evaluate using the same catch-up policy as advance(). */
  firstPendingBoundary(finalizableBoundary: number, universeVersion: string): number | null {
    if (
      !Number.isSafeInteger(finalizableBoundary) ||
      finalizableBoundary < 0 ||
      finalizableBoundary % MOVEMENT_BUCKET_MS !== 0
    ) {
      throw new Error("movement finalizable boundary must be an aligned exchange boundary");
    }
    if (this.lastEvaluated === null && this.restoredBoundaryTime !== null &&
        finalizableBoundary <= this.restoredBoundaryTime) return null;
    if (this.lastEvaluated !== null && finalizableBoundary <= this.lastEvaluated) return null;
    let next = this.lastEvaluated === null
      ? finalizableBoundary
      : this.lastEvaluated + MOVEMENT_BUCKET_MS;
    if (this.lastUniverseVersion !== null && this.lastUniverseVersion !== universeVersion) {
      next = finalizableBoundary;
    }
    if (next > finalizableBoundary) return null;
    const maxCatchUp = this.options.maxCatchUpBoundaries ?? MOVEMENT_ENGINE_MAX_CATCHUP_BOUNDARIES;
    if (finalizableBoundary - next > maxCatchUp * MOVEMENT_BUCKET_MS) {
      next = finalizableBoundary;
    }
    return next;
  }

  /** Seeds opaque #73 state; Python applies the one-time restart interruption. */
  restoreLifecycleState(state: SerializedMarketEpisodeLifecycleState | null,
    persistedBoundaryTime: number | null = null): void {
    this.lifecycleState = state;
    this.restoredBoundaryTime = persistedBoundaryTime;
    this.interruptPreviousState = state !== null;
  }

  /**
   * Evaluates every finalized boundary up to `finalizableBoundary`. Idempotent:
   * re-advancing to an already-evaluated boundary performs no new evaluation.
   */
  async advance(input: MovementEngineAdvanceInput): Promise<MovementBoundaryEvaluation[]> {
    if (
      !Number.isSafeInteger(input.finalizableBoundary) ||
      input.finalizableBoundary < 0 ||
      input.finalizableBoundary % MOVEMENT_BUCKET_MS !== 0
    ) {
      throw new Error("movement finalizable boundary must be an aligned exchange boundary");
    }
    const next = this.firstPendingBoundary(input.finalizableBoundary, input.universe.version);
    if (next === null) return [];
    const results: MovementBoundaryEvaluation[] = [];
    let stagedLifecycleState = this.lifecycleState;
    let stagedInterrupt = this.interruptPreviousState;
    let stagedLastEvaluated = this.lastEvaluated;
    let stagedUniverseVersion = this.lastUniverseVersion;
    for (
      let boundary = next;
      boundary <= input.finalizableBoundary;
      boundary += MOVEMENT_BUCKET_MS
    ) {
      const result = await this.evaluateBoundary(boundary, input, stagedLifecycleState,
        stagedInterrupt);
      results.push(result);
      stagedLifecycleState = result.lifecycle.serializedState;
      stagedInterrupt = false;
      stagedLastEvaluated = boundary;
      stagedUniverseVersion = input.universe.version;
    }
    this.lifecycleState = stagedLifecycleState;
    this.interruptPreviousState = stagedInterrupt;
    this.lastEvaluated = stagedLastEvaluated;
    this.lastUniverseVersion = stagedUniverseVersion;
    return results;
  }

  private async evaluateBoundary(
    boundaryTime: number,
    input: MovementEngineAdvanceInput,
    previousState: SerializedMarketEpisodeLifecycleState | null,
    interruptPreviousState: boolean,
  ): Promise<MovementBoundaryEvaluation> {
    const { movement, classification, lifecycle } = await input.lifecycleForBoundary(
      boundaryTime, previousState, interruptPreviousState,
    );
    if (
      movement.evaluationBoundaryTime !== boundaryTime ||
      movement.algorithmVersion !== MARKET_MOVEMENT_ALGORITHM_VERSION ||
      movement.universeId !== input.universe.id ||
      movement.universeVersion !== input.universe.version ||
      movement.configuredUniverse.length !== input.universe.symbols.length ||
      movement.configuredUniverse.some((symbol, index) => symbol !== input.universe.symbols[index]) ||
      movement.provider !== "binance-usdm" ||
      movement.exchange !== "binance" || movement.priceType !== "trade" ||
      movement.windows.length !== MOVEMENT_WINDOWS_MINUTES.length ||
      MOVEMENT_WINDOWS_MINUTES.some((window) =>
        movement.windows.filter((item) =>
          item.windowMinutes === window &&
          item.algorithmVersion === movement.algorithmVersion &&
          item.configVersion === movement.configVersion &&
          item.evaluationBoundaryTime === boundaryTime &&
          item.universeId === input.universe.id &&
          item.universeVersion === input.universe.version &&
          item.provider === "binance-usdm" && item.exchange === "binance" &&
          item.priceType === "trade" &&
          item.configuredUniverse.length === input.universe.symbols.length &&
          item.configuredUniverse.every((symbol, index) => symbol === input.universe.symbols[index]),
        ).length !== 1)
    ) {
      throw new Error("canonical movement response has mismatched boundary or provenance");
    }
    if (classification.evaluationBoundaryTime !== boundaryTime ||
        classification.universeId !== movement.universeId ||
        classification.universeVersion !== movement.universeVersion ||
        classification.movementAlgorithmVersion !== movement.algorithmVersion ||
        classification.movementConfigVersion !== movement.configVersion ||
        classification.provider !== movement.provider ||
        classification.exchange !== movement.exchange ||
        classification.priceType !== movement.priceType ||
        classification.primaryWindowMinutes !== 5 ||
        classification.windows.length !== MOVEMENT_WINDOWS_MINUTES.length ||
        MOVEMENT_WINDOWS_MINUTES.some((minute) =>
          classification.windows.filter((window) =>
            window.windowMinutes === minute &&
            window.evaluationBoundaryTime === boundaryTime &&
            window.universeId === movement.universeId &&
            window.universeVersion === movement.universeVersion &&
            window.configuredUniverse.length === input.universe.symbols.length &&
            window.configuredUniverse.every((symbol, index) => symbol === input.universe.symbols[index]),
          ).length !== 1)) {
      throw new Error("canonical classification response has mismatched boundary or provenance");
    }
    if (lifecycle.stateSummary.evaluationBoundaryTime !== boundaryTime ||
        lifecycle.stateSummary.universeId !== input.universe.id ||
        lifecycle.stateSummary.universeVersion !== input.universe.version ||
        lifecycle.stateSummary.classifierAlgorithmVersion !== classification.classifierAlgorithmVersion ||
        lifecycle.stateSummary.classifierConfigVersion !== classification.classifierConfigVersion ||
        lifecycle.stateSummary.movementAlgorithmVersion !== movement.algorithmVersion ||
        lifecycle.stateSummary.movementConfigVersion !== movement.configVersion) {
      throw new Error("canonical lifecycle response has mismatched boundary or provenance");
    }
    return { boundaryTime, movement, classification, lifecycle };
  }
}

/**
 * Latest exchange event time / trade time / receive time observed across the
 * shared snapshots.
 *
 * All three come from each symbol's latest *finalized* bucket (the ring holds
 * only finalized buckets), then the maximum across symbols is taken. A newer
 * trade still sitting in an unfinalized bucket is deliberately excluded so the
 * persisted evaluation provenance stays internally consistent; the snapshot-level
 * `latestReal*` fields may legitimately refer to that pending trade and are not
 * used here.
 */
export function snapshotEventTimes(snapshots: ReadonlyMap<string, MovementBucketSnapshot>): {
  lastSourceEventTime: number | null;
  lastTradeTime: number | null;
  lastReceivedAt: number | null;
} {
  let lastSourceEventTime: number | null = null;
  let lastTradeTime: number | null = null;
  let lastReceivedAt: number | null = null;
  for (const snapshot of snapshots.values()) {
    const latest = snapshot.buckets.at(-1);
    const eventTime = finiteNonnegative(latest?.lastRealEventTime);
    if (eventTime !== null && (lastSourceEventTime === null || eventTime > lastSourceEventTime)) {
      lastSourceEventTime = eventTime;
    }
    const tradeTime = finiteNonnegative(latest?.lastRealTradeTime);
    if (tradeTime !== null && (lastTradeTime === null || tradeTime > lastTradeTime)) {
      lastTradeTime = tradeTime;
    }
    const receivedAt = finiteNonnegative(latest?.lastRealReceivedAt);
    if (receivedAt !== null && (lastReceivedAt === null || receivedAt > lastReceivedAt)) {
      lastReceivedAt = receivedAt;
    }
  }
  return { lastSourceEventTime, lastTradeTime, lastReceivedAt };
}
