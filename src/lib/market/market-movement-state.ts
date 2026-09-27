/**
 * Client-safe market-movement current-state types and pure mappers (Issue #74).
 *
 * These types describe the bounded operational current state that later #30/#31
 * consumers read through authenticated server functions. No service-role
 * credentials or server-only modules are referenced here.
 */
import type {
  ConfirmedMarketDirection,
  MarketDirectionState,
  MarketHorizonRole,
  MarketClassificationWindow,
} from "./market-state-contract";
import type { MarketEpisodeTransitionType, MarketPace } from "./market-episode-contract";

/** Stable identity for the shared, market-wide Binance USDⓈ-M universe. */
export const MARKET_UNIVERSE_ID = "binance-usdm-public-market";

/** Market-wide state is unavailable below this many configured/eligible contracts. */
export const MINIMUM_MARKET_UNIVERSE_SIZE = 5;

/**
 * A movement engine whose persisted evaluation boundary is older than this is STALE.
 *
 * The bounded current state is written on transitions and on the default 30-second
 * cadence, so a healthy engine's persisted boundary legitimately lags wall-clock now
 * by the persistence cadence (30s) plus the live finalization grace/bucket lag
 * (~7s). This explicit, version-independent threshold leaves margin above that so
 * normal persistence/timer jitter never flickers a healthy engine to STALE, while
 * still surfacing a genuinely stalled engine. It does not require increasing the
 * write frequency.
 */
export const MOVEMENT_ENGINE_STALE_AFTER_MS = 60_000;

export type MovementEngineStatus = "LIVE" | "WARMING" | "STALE" | "UNAVAILABLE";

/** Structural mirror of the collector health status used as a movement source gate. */
export type MovementSourceStatus = "LIVE" | "RECOVERING" | "STALE" | "UNAVAILABLE";

/** Canonical completed one-minute candle transported as raw #71 history. */
export type MovementCandle = {
  openTime: number;
  close: number;
  volume: number;
  /** Exact Binance USD-M quote-asset volume. */
  quoteVolume: number;
};

export type PersistedMovementTransition = {
  transition: MarketEpisodeTransitionType;
  transitionReason: string;
  episodeId: string;
  direction: ConfirmedMarketDirection;
  fromDirection: ConfirmedMarketDirection | null;
  toDirection: ConfirmedMarketDirection | null;
  pace: MarketPace;
  episodeStartBoundaryTime: number;
  evaluationBoundaryTime: number;
};

export type PersistedMovementUniverse = {
  id: string;
  version: string;
  configuredSymbols: string[];
  includedSymbols: string[];
  excludedSymbols: Array<{ symbol: string; reasons: string[] }>;
};

export type PersistedMovementTimestamps = {
  evaluationBoundaryTime: number;
  lastSourceEventTime: number | null;
  lastTradeTime: number | null;
  lastReceivedAt: number | null;
  engineUpdatedAt: number;
};

/**
 * Enriched bounded current evidence persisted with #73's `market_state_current`.
 * It retains the canonical primary #72 window, 1m/15m context and the exact
 * universe/timestamps/engine status #74 requires consumers to inspect.
 */
export type PersistedMarketMovementCurrentEvidence = {
  primaryWindow: MarketClassificationWindow;
  windowsContext: MarketClassificationWindow[];
  universe: PersistedMovementUniverse;
  engine: {
    status: MovementEngineStatus;
    lateAfterFinalizationCount: number;
  };
  timestamps: PersistedMovementTimestamps;
  /**
   * Effective finalization config in force when this evidence was written. The
   * version deterministically identifies the grace so tuning it is auditable.
   */
  finalizationConfigVersion: string;
  finalizationGraceMs: number;
  mostRecentTransition: PersistedMovementTransition | null;
};

export type MarketMovementCurrentState = {
  available: boolean;
  status: MovementEngineStatus;
  universe: PersistedMovementUniverse | null;
  primary: {
    windowMinutes: 5;
    directionState: MarketDirectionState;
    pace: MarketPace;
    reversalCandidate: boolean;
    horizonRole: MarketHorizonRole;
    evidence: MarketClassificationWindow;
  } | null;
  /** 1m, 5m and 15m structured classifications, primary included. */
  context: MarketClassificationWindow[];
  timestamps: PersistedMovementTimestamps | null;
  /** Effective finalization grace/config the persisted evidence was produced under. */
  finalizationConfigVersion: string | null;
  finalizationGraceMs: number | null;
  versions: {
    movementAlgorithmVersion: string;
    movementConfigVersion: string;
    classifierAlgorithmVersion: string;
    classifierConfigVersion: string;
    episodeAlgorithmVersion: string;
    lifecycleConfigVersion: string;
    universeVersion: string;
  } | null;
  mostRecentTransition: PersistedMovementTransition | null;
  lateAfterFinalizationCount: number;
  activeEpisode: {
    episodeId: string;
    direction: ConfirmedMarketDirection;
    interrupted: boolean;
  } | null;
};

export type MovementEngineDiagnostics = {
  status: MovementEngineStatus;
  configuredSymbolCount: number;
  eligibleSymbolCount: number;
  primaryDirectionState: MarketDirectionState;
  primaryPace: MarketPace;
  lastEvaluationBoundaryTime: number;
  lastSourceEventTime: number | null;
  finalizationConfigVersion: string | null;
  finalizationGraceMs: number | null;
  movementAlgorithmVersion: string;
  movementConfigVersion: string;
  universeVersion: string;
  mostRecentTransition: PersistedMovementTransition | null;
  lateAfterFinalizationCount: number;
};

type MovementEngineStatusInput = {
  directionState: MarketDirectionState;
  configuredCount: number;
  evaluationBoundaryTime: number;
  now: number;
  staleAfterMs?: number;
};

/**
 * Keeps LIVE / WARMING / STALE / UNAVAILABLE distinct.
 *
 * - UNAVAILABLE: fewer than the V1 gate of configured contracts, or the
 *   classified market state itself is unavailable.
 * - STALE: no fresh evaluation within the staleness window (engine/process lag).
 * - WARMING: evaluating, but live 5-second history is not yet sufficient.
 * - LIVE: a fresh evaluation with a usable market-wide state.
 */
export function deriveMovementEngineStatus(input: MovementEngineStatusInput): MovementEngineStatus {
  if (input.configuredCount < MINIMUM_MARKET_UNIVERSE_SIZE) return "UNAVAILABLE";
  const staleAfterMs = input.staleAfterMs ?? MOVEMENT_ENGINE_STALE_AFTER_MS;
  if (input.now - input.evaluationBoundaryTime > staleAfterMs) return "STALE";
  if (input.directionState === "WARMING") return "WARMING";
  if (input.directionState === "UNAVAILABLE") return "UNAVAILABLE";
  return "LIVE";
}

type PersistedCurrentLike = {
  universeId: string;
  universeVersion: string;
  evaluationBoundaryTime: number;
  directionState: MarketDirectionState;
  pace: MarketPace;
  activeEpisodeId: string | null;
  activeDirection: ConfirmedMarketDirection | null;
  interrupted: boolean;
  episodeAlgorithmVersion: string;
  lifecycleConfigVersion: string;
  classifierAlgorithmVersion: string;
  classifierConfigVersion: string;
  movementAlgorithmVersion: string;
  movementConfigVersion: string;
  currentEvidence: PersistedMarketMovementCurrentEvidence;
};

function configuredCount(evidence: PersistedMarketMovementCurrentEvidence | null): number {
  return evidence?.universe?.configuredSymbols?.length ?? 0;
}

/** Compact diagnostics for the settings surface; never carries credentials. */
export function toMovementEngineDiagnostics(
  current: PersistedCurrentLike | null,
  options: { now: number; staleAfterMs?: number },
): MovementEngineDiagnostics | null {
  if (!current) return null;
  const evidence = current.currentEvidence;
  const primary = evidence.windowsContext?.find((window) => window.windowMinutes === 5);
  return {
    status: deriveMovementEngineStatus({
      directionState: current.directionState,
      configuredCount: configuredCount(evidence),
      evaluationBoundaryTime: current.evaluationBoundaryTime,
      now: options.now,
      ...(options.staleAfterMs === undefined ? {} : { staleAfterMs: options.staleAfterMs }),
    }),
    configuredSymbolCount: configuredCount(evidence),
    eligibleSymbolCount: primary?.eligibleCount ?? evidence.primaryWindow.eligibleCount,
    primaryDirectionState: current.directionState,
    primaryPace: current.pace,
    lastEvaluationBoundaryTime: current.evaluationBoundaryTime,
    lastSourceEventTime: evidence.timestamps?.lastSourceEventTime ?? null,
    finalizationConfigVersion: evidence.finalizationConfigVersion ?? null,
    finalizationGraceMs: evidence.finalizationGraceMs ?? null,
    movementAlgorithmVersion: current.movementAlgorithmVersion,
    movementConfigVersion: current.movementConfigVersion,
    universeVersion: current.universeVersion,
    mostRecentTransition: evidence.mostRecentTransition ?? null,
    lateAfterFinalizationCount: evidence.engine?.lateAfterFinalizationCount ?? 0,
  };
}

/** Maps the persisted bounded state into the structured consumer contract. */
export function toMarketMovementCurrentState(
  current: PersistedCurrentLike | null,
  options: { now: number; staleAfterMs?: number },
): MarketMovementCurrentState {
  if (!current) {
    return {
      available: false,
      status: "UNAVAILABLE",
      universe: null,
      primary: null,
      context: [],
      timestamps: null,
      finalizationConfigVersion: null,
      finalizationGraceMs: null,
      versions: null,
      mostRecentTransition: null,
      lateAfterFinalizationCount: 0,
      activeEpisode: null,
    };
  }
  const evidence = current.currentEvidence;
  const windowsContext = evidence.windowsContext ?? [];
  const primary = windowsContext.find((window) => window.windowMinutes === 5) ?? null;
  return {
    available: true,
    status: deriveMovementEngineStatus({
      directionState: current.directionState,
      configuredCount: configuredCount(evidence),
      evaluationBoundaryTime: current.evaluationBoundaryTime,
      now: options.now,
      ...(options.staleAfterMs === undefined ? {} : { staleAfterMs: options.staleAfterMs }),
    }),
    universe: evidence.universe ?? null,
    primary: primary
      ? {
          windowMinutes: 5,
          directionState: current.directionState,
          pace: current.pace,
          reversalCandidate: primary.reversalCandidate !== null,
          horizonRole: primary.horizonRole,
          evidence: primary,
        }
      : null,
    context: windowsContext,
    timestamps: evidence.timestamps ?? null,
    finalizationConfigVersion: evidence.finalizationConfigVersion ?? null,
    finalizationGraceMs: evidence.finalizationGraceMs ?? null,
    versions: {
      movementAlgorithmVersion: current.movementAlgorithmVersion,
      movementConfigVersion: current.movementConfigVersion,
      classifierAlgorithmVersion: current.classifierAlgorithmVersion,
      classifierConfigVersion: current.classifierConfigVersion,
      episodeAlgorithmVersion: current.episodeAlgorithmVersion,
      lifecycleConfigVersion: current.lifecycleConfigVersion,
      universeVersion: current.universeVersion,
    },
    mostRecentTransition: evidence.mostRecentTransition ?? null,
    lateAfterFinalizationCount: evidence.engine?.lateAfterFinalizationCount ?? 0,
    activeEpisode:
      current.activeEpisodeId && current.activeDirection
        ? {
            episodeId: current.activeEpisodeId,
            direction: current.activeDirection,
            interrupted: current.interrupted,
          }
        : null,
  };
}
