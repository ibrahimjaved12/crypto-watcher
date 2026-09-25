/**
 * Live market-movement engine runtime (Issue #74).
 *
 * Runs only inside the authoritative collector process (the operational lease
 * owner). Every tick it finalizes quiet symbols up to the grace-based exchange
 * boundary, then evaluates each newly finalized five-second boundary once through
 * the shared pure pipeline and persists bounded current state via #73.
 *
 * There is no browser timer and no per-user engine: the shared universe is
 * evaluated once for identical market data.
 */
import type { OperationalStore, PersistedMarketStateCurrent } from "../operational/types";
import type { MovementBucketSnapshot } from "./movement-buckets";
import type { BinanceFuturesCollector } from "./collector";
import {
  buildMovementCurrentEvidence,
  buildMovementNormalizationHistory,
  MarketMovementEngine,
  snapshotEventTimes,
  transitionToPersisted,
  type MovementNormalizationHistory,
} from "./market-movement-engine";
import { buildMarketUniverse, type MarketUniverse } from "./market-universe";
import {
  DEFAULT_MARKET_EPISODE_LIFECYCLE_CONFIG,
  deserializeMarketEpisodeLifecycleState,
  restoreLifecycleStateOnRestart,
  serializeMarketEpisodeLifecycleState,
  type MarketEpisodeLifecycleConfig,
} from "./market-episode-lifecycle";
import {
  DEFAULT_MOVEMENT_FINALIZATION_CONFIG,
  finalizableMovementBoundary,
  type MovementFinalizationConfig,
} from "./movement-finalization";
import { DEFAULT_MARKET_MOVEMENT_CONFIG, type MarketMovementConfig } from "./movement-metrics";
import {
  DEFAULT_MARKET_STATE_CLASSIFIER_CONFIG,
  type MarketStateClassifierConfig,
} from "./market-state-classifier";
import {
  deriveMovementEngineStatus,
  MARKET_UNIVERSE_ID,
  type MovementSourceStatus,
  type PersistedMovementTransition,
} from "./market-movement-state";

export const MOVEMENT_ENGINE_TICK_MS = 1_000;
export const MOVEMENT_HISTORICAL_REFRESH_MS = 15 * 60_000;

/** Narrow view of the shared collector the engine depends on. */
export type MovementCollectorPort = Pick<
  BinanceFuturesCollector,
  | "subscribedSymbols"
  | "movementSnapshot"
  | "advanceMovementBuckets"
  | "symbolSourceStatus"
  | "movementLateRejections"
>;

export type MovementEngineRuntimeDependencies = {
  store: OperationalStore;
  collector: MovementCollectorPort;
  finalization?: MovementFinalizationConfig;
  movementConfig?: MarketMovementConfig;
  classifierConfig?: MarketStateClassifierConfig;
  lifecycleConfig?: MarketEpisodeLifecycleConfig;
  now?: () => number;
};

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export class MovementEngineRuntime {
  private readonly engine = new MarketMovementEngine();
  private readonly now: () => number;
  private readonly movementConfig: MarketMovementConfig;
  private readonly classifierConfig: MarketStateClassifierConfig;
  private readonly lifecycleConfig: MarketEpisodeLifecycleConfig;
  private readonly finalization: MovementFinalizationConfig;
  private timer: ReturnType<typeof setInterval> | null = null;
  private active = false;
  private running = false;
  private lifecycleRestored = false;
  private historical: MovementNormalizationHistory = new Map();
  private historicalLoadedAt = 0;
  private historicalLoading: Promise<void> | null = null;
  private lastTransition: PersistedMovementTransition | null = null;

  constructor(private readonly deps: MovementEngineRuntimeDependencies) {
    this.now = deps.now ?? Date.now;
    this.movementConfig = deps.movementConfig ?? DEFAULT_MARKET_MOVEMENT_CONFIG;
    this.classifierConfig = deps.classifierConfig ?? DEFAULT_MARKET_STATE_CLASSIFIER_CONFIG;
    this.lifecycleConfig = deps.lifecycleConfig ?? DEFAULT_MARKET_EPISODE_LIFECYCLE_CONFIG;
    this.finalization = deps.finalization ?? DEFAULT_MOVEMENT_FINALIZATION_CONFIG;
  }

  /** Starts (or resumes) periodic evaluation. Safe to call after a lease reacquire. */
  async start(): Promise<void> {
    if (this.active) return;
    this.active = true;
    if (!this.lifecycleRestored) await this.loadPersistedState();
    if (!this.timer) {
      this.timer = setInterval(() => void this.tick(), MOVEMENT_ENGINE_TICK_MS);
      this.timer.unref?.();
    }
  }

  async stop(): Promise<void> {
    this.active = false;
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    await this.historicalLoading?.catch(() => undefined);
  }

  private async loadPersistedState(): Promise<void> {
    try {
      const current = await this.deps.store.getMarketStateCurrent(MARKET_UNIVERSE_ID);
      if (!current) {
        this.lifecycleRestored = true;
        return;
      }
      const restored = deserializeMarketEpisodeLifecycleState(current.lifecycleState);
      this.engine.restoreLifecycleState(restoreLifecycleStateOnRestart(restored));
      this.lastTransition = current.currentEvidence?.mostRecentTransition ?? null;
      this.lifecycleRestored = true;
    } catch (error) {
      console.error(`[movement-engine] persisted state load failed: ${message(error)}`);
    }
  }

  private async tick(): Promise<void> {
    if (!this.active || this.running) return;
    this.running = true;
    try {
      const symbols = this.deps.collector.subscribedSymbols();
      if (symbols.length === 0) return;
      const now = this.now();
      let finalizable: number;
      try {
        finalizable = finalizableMovementBoundary(now, this.finalization);
      } catch {
        // Wall clock is still inside the grace of the epoch; nothing is safe yet.
        return;
      }
      const universe = buildMarketUniverse(symbols);
      // Finalize quiet/no-trade symbols only through the safe boundary.
      this.deps.collector.advanceMovementBuckets(finalizable);
      await this.refreshHistorical(universe, now);
      const snapshots = new Map<string, MovementBucketSnapshot>();
      const sourceStatus = new Map<string, MovementSourceStatus>();
      for (const symbol of universe.symbols) {
        const snapshot = this.deps.collector.movementSnapshot(symbol);
        if (snapshot) snapshots.set(symbol, snapshot);
        sourceStatus.set(symbol, this.deps.collector.symbolSourceStatus(symbol));
      }
      const results = this.engine.advance({
        finalizableBoundary: finalizable,
        snapshots,
        sourceStatus,
        historical: this.historical,
        universe,
        movementConfig: this.movementConfig,
        classifierConfig: this.classifierConfig,
        lifecycleConfig: this.lifecycleConfig,
      });
      if (results.length === 0) return;
      await this.persist(results, universe, snapshots, now);
    } catch (error) {
      console.error(`[movement-engine] evaluation failed: ${message(error)}`);
    } finally {
      this.running = false;
    }
  }

  private async refreshHistorical(universe: MarketUniverse, now: number): Promise<void> {
    if (this.historicalLoading) return;
    if (
      this.historicalLoadedAt !== 0 &&
      now - this.historicalLoadedAt < MOVEMENT_HISTORICAL_REFRESH_MS
    ) {
      return;
    }
    this.historicalLoading = (async () => {
      const candles = await this.deps.store.readMovementCandleHistory(
        universe.symbols,
        now - this.movementConfig.historicalLookbackMs,
      );
      this.historical = buildMovementNormalizationHistory(candles, this.movementConfig);
      this.historicalLoadedAt = now;
    })().finally(() => {
      this.historicalLoading = null;
    });
    await this.historicalLoading.catch((error) => {
      console.error(`[movement-engine] normalization history load failed: ${message(error)}`);
    });
  }

  private async persist(
    results: ReturnType<MarketMovementEngine["advance"]>,
    universe: MarketUniverse,
    snapshots: ReadonlyMap<string, MovementBucketSnapshot>,
    now: number,
  ): Promise<void> {
    const shouldPersist = results.some(
      (result) => result.lifecycle.shouldPersistCurrentImmediately,
    );
    if (!shouldPersist) return;

    const final = results.at(-1)!;
    const events = results.flatMap((result) => result.lifecycle.transitions);
    const state = final.lifecycle.nextState;
    const { lastSourceEventTime, lastTradeTime } = snapshotEventTimes(snapshots);
    const status = deriveMovementEngineStatus({
      directionState: state.currentDirectionState,
      configuredCount: universe.symbols.length,
      evaluationBoundaryTime: final.boundaryTime,
      now,
    });
    const mostRecentTransition =
      events.length > 0 ? transitionToPersisted(events.at(-1)!) : this.lastTransition;
    const currentEvidence = buildMovementCurrentEvidence({
      evidence: final.lifecycle.currentEvidence,
      classification: final.classification,
      universe,
      status,
      lateAfterFinalizationCount: this.deps.collector.movementLateRejections(),
      engineUpdatedAt: now,
      lastSourceEventTime,
      lastTradeTime,
      mostRecentTransition,
    });
    const current: PersistedMarketStateCurrent = {
      universeId: universe.id,
      primaryWindowMinutes: 5,
      universeVersion: universe.version,
      provider: "binance-usdm",
      exchange: "binance",
      priceType: "trade",
      evaluationBoundaryTime: final.boundaryTime,
      directionState: state.currentDirectionState,
      pace: state.currentPace,
      activeEpisodeId: state.activeEpisode?.episodeId ?? null,
      activeDirection: state.activeEpisode?.direction ?? null,
      interrupted: state.interrupted,
      episodeAlgorithmVersion: state.episodeAlgorithmVersion,
      lifecycleConfigVersion: state.lifecycleConfigVersion,
      classifierAlgorithmVersion: state.classifierAlgorithmVersion,
      classifierConfigVersion: state.classifierConfigVersion,
      movementAlgorithmVersion: state.movementAlgorithmVersion,
      movementConfigVersion: state.movementConfigVersion,
      lifecycleState: serializeMarketEpisodeLifecycleState(state),
      currentEvidence,
    };
    await this.deps.store.persistMarketEpisodeLifecycleStep(current, events);
    this.engine.acknowledgePersisted();
    this.lastTransition = mostRecentTransition;
  }
}
