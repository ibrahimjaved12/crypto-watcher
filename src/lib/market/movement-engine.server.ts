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
import type { MovementBucketSnapshot } from "./movement-contract";
import type { BinanceFuturesCollector } from "./collector";
import {
  buildMovementCurrentEvidence,
  MarketMovementEngine,
  snapshotEventTimes,
  transitionToPersisted,
} from "./market-movement-engine";
import type { MovementInstrumentCompatibility, MovementRawHistory } from "./movement-normalization-input";
import { buildMarketUniverse, type MarketUniverse } from "./market-universe";
import {
  DEFAULT_MARKET_EPISODE_LIFECYCLE_CONFIG,
  deserializeMarketEpisodeLifecycleState,
  restoreLifecycleStateOnRestart,
  serializeMarketEpisodeLifecycleState,
  type MarketEpisodeLifecycleConfig,
  type MarketMovementEvent,
} from "./market-episode-lifecycle";
import {
  DEFAULT_MOVEMENT_FINALIZATION_CONFIG,
  finalizableMovementBoundary,
  type MovementFinalizationConfig,
} from "./movement-finalization";
import { DEFAULT_MARKET_MOVEMENT_CONFIG, type MarketMovementConfig,
  type MarketMovementEvaluation } from "./movement-metrics-contract";
import type { ConfirmedMarketDirection, MarketClassification } from "./market-state-contract";
import {
  deriveMovementEngineStatus,
  MARKET_UNIVERSE_ID,
  type PersistedMovementTransition,
} from "./market-movement-state";

export const MOVEMENT_ENGINE_TICK_MS = 1_000;
const MINUTE_MS = 60_000;
const LONGEST_MOVEMENT_WINDOW_MS = 15 * MINUTE_MS;
const RAW_HISTORY_WARMUP_MS = LONGEST_MOVEMENT_WINDOW_MS + MINUTE_MS;

/**
 * Bounded exponential backoff for operational reads/writes that must not be
 * retried on every one-second engine tick (state restore, normalization history).
 */
export const MOVEMENT_RETRY_BASE_MS = 5_000;
export const MOVEMENT_RETRY_MAX_MS = 60_000;

function backoffDelay(failures: number): number {
  const exponent = Math.min(Math.max(failures - 1, 0), 20);
  return Math.min(MOVEMENT_RETRY_MAX_MS, MOVEMENT_RETRY_BASE_MS * 2 ** exponent);
}

/** Narrow view of the shared collector the engine depends on. */
export type MovementCollectorPort = Pick<
  BinanceFuturesCollector,
  | "subscribedSymbols"
  | "movementSnapshot"
  | "advanceMovementBuckets"
  | "movementLateRejections"
  | "currentMovementSessionId"
>;

export type MovementEngineRuntimeDependencies = {
  store: OperationalStore;
  collector: MovementCollectorPort;
  registerHistory: (sessionId: string, historyVersion: string, universe: MarketUniverse,
    config: MarketMovementConfig, historical: MovementRawHistory,
    compatibility: MovementInstrumentCompatibility,
    asOfBoundaryTime: number) => Promise<void>;
  instrumentCompatibility: (symbol: string) => Promise<boolean | null>;
  calculateAssessment: (sessionId: string, boundaryTime: number, historyVersion: string,
    universe: MarketUniverse, configVersion: string,
    previousConfirmedPrimaryDirection: ConfirmedMarketDirection | null) => Promise<{
      movement: MarketMovementEvaluation;
      classification: MarketClassification;
    }>;
  finalization?: MovementFinalizationConfig;
  movementConfig?: MarketMovementConfig;
  lifecycleConfig?: MarketEpisodeLifecycleConfig;
  now?: () => number;
};

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * A persistence-required batch (current snapshot plus zero or more transitions)
 * held until #73's atomic write succeeds. Event IDs are deterministic and the DB
 * append is idempotent, so replaying the exact batch can never duplicate an event.
 */
type PendingMovementPersistence = {
  current: PersistedMarketStateCurrent;
  events: MarketMovementEvent[];
  mostRecentTransition: PersistedMovementTransition | null;
};

export class MovementEngineRuntime {
  private engine = new MarketMovementEngine();
  private readonly now: () => number;
  private readonly movementConfig: MarketMovementConfig;
  private readonly lifecycleConfig: MarketEpisodeLifecycleConfig;
  private readonly finalization: MovementFinalizationConfig;
  private timer: ReturnType<typeof setInterval> | null = null;
  private active = false;
  private running = false;
  /**
   * Monotonic ownership generation. Incremented whenever this runtime loses (or
   * gives up) authoritative ownership, so in-flight work from a prior lease
   * tenure can never write over a newer owner's durable state.
   */
  private tenure = 0;
  private lifecycleRestored = false;
  private lifecycleRestoreRetryAt = 0;
  private lifecycleRestoreFailures = 0;
  private historical: MovementRawHistory = new Map();
  private historicalCompatibility: MovementInstrumentCompatibility = new Map();
  private historicalCutoff: number | null = null;
  private historicalRetryAt = 0;
  private historicalFailures = 0;
  private historicalUniverseVersion: string | null = null;
  private historicalVersion: string | null = null;
  private historicalAsOfBoundary: number | null = null;
  private registeredSessionId: string | null = null;
  private registeredUniverseVersion: string | null = null;
  private historicalLoading: Promise<void> | null = null;
  private historicalRefreshGeneration = 0;
  private appliedHistoricalRefreshGeneration = 0;
  private lastTransition: PersistedMovementTransition | null = null;
  private pendingPersistence: PendingMovementPersistence | null = null;

  constructor(private readonly deps: MovementEngineRuntimeDependencies) {
    this.now = deps.now ?? Date.now;
    this.movementConfig = deps.movementConfig ?? DEFAULT_MARKET_MOVEMENT_CONFIG;
    this.lifecycleConfig = deps.lifecycleConfig ?? DEFAULT_MARKET_EPISODE_LIFECYCLE_CONFIG;
    this.finalization = deps.finalization ?? DEFAULT_MOVEMENT_FINALIZATION_CONFIG;
  }

  /** Starts (or resumes) periodic evaluation. Safe to call after a lease reacquire. */
  async start(): Promise<void> {
    if (this.active) return;
    this.active = true;
    if (!this.timer) {
      this.timer = setInterval(() => void this.tick(), MOVEMENT_ENGINE_TICK_MS);
      this.timer.unref?.();
    }
  }

  async stop(): Promise<void> {
    this.active = false;
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    // Losing the lease means another instance may advance and persist the market
    // lifecycle while this one is inactive, so all in-memory ownership from this
    // tenure is discarded; the next `start()` must reload from the database.
    this.invalidateOwnership();
    await this.historicalLoading?.catch(() => undefined);
  }

  /** Forces the next evaluation cycle to reload normalization input from storage. */
  requestNormalizationHistoryRefresh(): void {
    this.historicalRefreshGeneration += 1;
    this.historicalRetryAt = 0;
  }

  /**
   * Drops every piece of in-memory lifecycle ownership so a reacquisition cannot
   * continue from stale counters or blindly write an old tenure's pending batch.
   */
  private invalidateOwnership(): void {
    this.tenure += 1;
    this.lifecycleRestored = false;
    this.lifecycleRestoreRetryAt = 0;
    this.lifecycleRestoreFailures = 0;
    this.historical = new Map();
    this.historicalCompatibility = new Map();
    this.historicalCutoff = null;
    this.historicalRetryAt = 0;
    this.historicalFailures = 0;
    this.historicalUniverseVersion = null;
    this.historicalVersion = null;
    this.historicalAsOfBoundary = null;
    this.registeredSessionId = null;
    this.registeredUniverseVersion = null;
    this.historicalRefreshGeneration = 0;
    this.appliedHistoricalRefreshGeneration = 0;
    this.lastTransition = null;
    this.pendingPersistence = null;
    this.engine = new MarketMovementEngine();
  }

  private async tick(): Promise<void> {
    if (!this.active || this.running) return;
    this.running = true;
    try {
      await this.runOnce();
    } catch (error) {
      console.error(`[movement-engine] evaluation failed: ${message(error)}`);
    } finally {
      this.running = false;
    }
  }

  /**
   * Runs exactly one evaluation cycle. Exposed so focused runtime tests can drive
   * the engine deterministically; the periodic timer calls it through `tick`.
   */
  async runOnce(): Promise<void> {
    const tenure = this.tenure;
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
    // Canonical #70 finalization is upstream of lifecycle storage. It must keep
    // consuming safe explicit boundaries even while #73 restore/write I/O fails.
    await this.deps.collector.advanceMovementBuckets(finalizable);
    if (tenure !== this.tenure) return;
    const movementSessionId = this.deps.collector.currentMovementSessionId();
    const stillCurrent = () =>
      tenure === this.tenure &&
      this.deps.collector.currentMovementSessionId() === movementSessionId &&
      buildMarketUniverse(this.deps.collector.subscribedSymbols()).version === universe.version;
    if (!stillCurrent()) return;
    // Fail closed downstream: never evaluate against lifecycle state that has
    // not been established by a successful operational read.
    if (!this.lifecycleRestored && !(await this.attemptLifecycleRestore(now))) return;
    if (!stillCurrent()) return;
    // An outstanding #73 batch must persist before any later lifecycle boundary
    // is evaluated, but it no longer blocks upstream #70 bucket finalization.
    if (this.pendingPersistence && !(await this.flushPendingPersistence())) return;
    if (!stillCurrent()) return;
    const firstBoundary = this.engine.firstPendingBoundary(finalizable, universe.version);
    if (firstBoundary === null) return;
    await this.refreshHistorical(universe, now, movementSessionId, firstBoundary, finalizable);
    if (!stillCurrent() || !this.historicalVersion ||
        this.registeredSessionId !== movementSessionId ||
        this.registeredUniverseVersion !== universe.version) return;
    const snapshots = new Map<string, MovementBucketSnapshot>();
    for (const symbol of universe.symbols) {
      const snapshot = this.deps.collector.movementSnapshot(symbol);
      if (snapshot) snapshots.set(symbol, snapshot);
    }
    let results: Awaited<ReturnType<MarketMovementEngine["advance"]>>;
    try {
      results = await this.engine.advance({
        finalizableBoundary: finalizable,
        universe,
        assessmentForBoundary: async (boundary, previousConfirmedPrimaryDirection) => {
          const assessment = await this.deps.calculateAssessment(
            movementSessionId, boundary, this.historicalVersion!, universe,
            this.movementConfig.version, previousConfirmedPrimaryDirection,
          );
          if (!stillCurrent()) throw new Error("stale movement session or universe response");
          return assessment;
        },
        lifecycleConfig: this.lifecycleConfig,
      });
    } catch (error) {
      console.error(`[movement-engine] canonical #71/#72 assessment unavailable: ${message(error)}`);
      return;
    }
    if (!stillCurrent()) return;
    if (results.length === 0) return;
    await this.persist(results, universe, snapshots, now);
  }

  /**
   * Establishes the #73 lifecycle state from the operational store exactly once.
   * A transient read/deserialize/restore failure is retried with bounded backoff;
   * evaluation does not start until this succeeds.
   */
  private async attemptLifecycleRestore(now: number): Promise<boolean> {
    if (this.lifecycleRestored) return true;
    if (now < this.lifecycleRestoreRetryAt) return false;
    const tenure = this.tenure;
    try {
      const current = await this.deps.store.getMarketStateCurrent(MARKET_UNIVERSE_ID);
      // Ownership may have been lost while the read was in flight.
      if (tenure !== this.tenure) return false;
      if (!current) {
        this.lifecycleRestored = true;
        return true;
      }
      const restored = deserializeMarketEpisodeLifecycleState(current.lifecycleState);
      this.engine.restoreLifecycleState(restoreLifecycleStateOnRestart(restored));
      this.lastTransition = current.currentEvidence?.mostRecentTransition ?? null;
      this.lifecycleRestored = true;
      return true;
    } catch (error) {
      if (tenure !== this.tenure) return false;
      this.lifecycleRestoreFailures += 1;
      const delay = backoffDelay(this.lifecycleRestoreFailures);
      this.lifecycleRestoreRetryAt = now + delay;
      console.error(
        `[movement-engine] persisted state restore failed; retrying in ${delay}ms: ${message(error)}`,
      );
      return false;
    }
  }

  private async refreshHistorical(universe: MarketUniverse, now: number,
    sessionId: string, firstBoundary: number, finalizable: number): Promise<void> {
    if (this.historicalLoading) return;
    if (now < this.historicalRetryAt) return;
    // Reuse only raw candles for the same strict completed-minute cutoff.
    const cutoff = Math.ceil(finalizable / MINUTE_MS) * MINUTE_MS;
    const universeChanged = this.historicalUniverseVersion !== universe.version;
    const refreshGeneration = this.historicalRefreshGeneration;
    const needsRefresh = universeChanged || this.historicalCutoff !== cutoff ||
      this.historicalAsOfBoundary === null || firstBoundary < this.historicalAsOfBoundary ||
      this.appliedHistoricalRefreshGeneration !== refreshGeneration ||
      this.historicalVersion === null;
    const needsRegistration = this.registeredSessionId !== sessionId ||
      this.registeredUniverseVersion !== universe.version;
    if (!needsRefresh && !needsRegistration) return;
    const tenure = this.tenure;
    this.historicalLoading = (async () => {
      try {
        if (needsRefresh) {
          const candles = await this.deps.store.readMovementCandleHistory(
            universe.symbols,
            Math.max(0, firstBoundary - this.movementConfig.historicalLookbackMs -
              RAW_HISTORY_WARMUP_MS),
            finalizable,
          );
          if (tenure !== this.tenure ||
              this.deps.collector.currentMovementSessionId() !== sessionId ||
              buildMarketUniverse(this.deps.collector.subscribedSymbols()).version !== universe.version) return;
          this.historical = candles;
          this.historicalCompatibility = new Map(await Promise.all(universe.symbols.map(
            async (symbol) => [symbol, await this.deps.instrumentCompatibility(symbol)] as const,
          )));
          if (tenure !== this.tenure ||
              this.deps.collector.currentMovementSessionId() !== sessionId ||
              buildMarketUniverse(this.deps.collector.subscribedSymbols()).version !== universe.version) return;
          this.historicalVersion = crypto.randomUUID();
          this.historicalAsOfBoundary = firstBoundary;
          this.historicalCutoff = cutoff;
          this.historicalUniverseVersion = universe.version;
          this.appliedHistoricalRefreshGeneration = refreshGeneration;
          this.registeredSessionId = null;
          this.registeredUniverseVersion = null;
        }
        await this.deps.registerHistory(sessionId, this.historicalVersion!, universe,
          this.movementConfig, this.historical, this.historicalCompatibility,
          this.historicalAsOfBoundary!);
        if (tenure !== this.tenure ||
            this.deps.collector.currentMovementSessionId() !== sessionId ||
            buildMarketUniverse(this.deps.collector.subscribedSymbols()).version !== universe.version) return;
        this.registeredSessionId = sessionId;
        this.registeredUniverseVersion = universe.version;
        this.historicalFailures = 0;
        this.historicalRetryAt = 0;
      } catch (error) {
        if (tenure !== this.tenure) return;
        this.historicalFailures += 1;
        const delay = backoffDelay(this.historicalFailures);
        this.historicalRetryAt = now + delay;
        console.error(
          `[movement-engine] normalization history load failed; retrying in ${delay}ms: ${message(error)}`,
        );
      }
    })().finally(() => {
      this.historicalLoading = null;
    });
    await this.historicalLoading;
  }

  private async persist(
    results: Awaited<ReturnType<MarketMovementEngine["advance"]>>,
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
    const { lastSourceEventTime, lastTradeTime, lastReceivedAt } = snapshotEventTimes(snapshots);
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
      lastReceivedAt,
      finalizationConfigVersion: this.finalization.version,
      finalizationGraceMs: this.finalization.graceMs,
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
    await this.flushPersistence({ current, events, mostRecentTransition });
  }

  private async flushPendingPersistence(): Promise<boolean> {
    const batch = this.pendingPersistence;
    if (!batch) return true;
    return this.flushPersistence(batch);
  }

  /**
   * Atomically persists one batch. On failure the exact same batch (deterministic
   * event IDs and current snapshot) is retained as pending and retried before any
   * later boundary is evaluated; lifecycle cadence is acknowledged only after the
   * database write succeeds.
   */
  private async flushPersistence(batch: PendingMovementPersistence): Promise<boolean> {
    const tenure = this.tenure;
    try {
      await this.deps.store.persistMarketEpisodeLifecycleStep(batch.current, batch.events);
    } catch (error) {
      // A newer owner may have taken over: never re-queue a stale tenure's batch.
      if (tenure !== this.tenure) return false;
      this.pendingPersistence = batch;
      console.error(
        `[movement-engine] lifecycle persistence failed; retrying the same batch: ${message(error)}`,
      );
      return false;
    }
    if (tenure !== this.tenure) return false;
    this.pendingPersistence = null;
    this.engine.acknowledgePersisted();
    this.lastTransition = batch.mostRecentTransition;
    return true;
  }
}
