import { createHash } from "node:crypto";
import type {
  ConfirmedMarketDirection,
  IsolatedMarketOutlier,
  MarketDirectionState,
  MarketPace,
  MarketStateClassification,
  MarketStateEvidence,
  MarketStateWindowClassification,
} from "./market-state-classifier";
import type {
  MarketMovementEvaluation,
  MarketMovementWindowResult,
  SymbolExclusionReason,
} from "./movement-metrics";

export const MARKET_EPISODE_ALGORITHM_VERSION = "market-episode-v1";
export const DEFAULT_MARKET_EPISODE_CONFIG_VERSION = "market-episode-config-v1";
export const MARKET_EPISODE_STATE_SERIALIZATION_VERSION = "market-episode-state-v1";
export const MARKET_EPISODE_EVALUATION_CADENCE_MS = 5_000;

export type MarketEpisodeTransitionType =
  "STARTED" | "STRENGTHENED" | "WEAKENED" | "REVERSED" | "ENDED";

export type MarketEpisodeDirection = ConfirmedMarketDirection;

export type MarketEpisodeLifecycleConfig = {
  version: string;
  currentSnapshotCadenceMs: number;
  startConfirmationCount: number;
  exitConfirmationCount: number;
  reversalConfirmationCount: number;
  strengthConfirmationCount: number;
  continuationRisingBreadth: number;
  continuationFallingBreadth: number;
  materialStrengthenBreadth: number;
  materialWeakenBreadth: number;
};

export const DEFAULT_MARKET_EPISODE_LIFECYCLE_CONFIG: Readonly<MarketEpisodeLifecycleConfig> = {
  version: DEFAULT_MARKET_EPISODE_CONFIG_VERSION,
  currentSnapshotCadenceMs: 30_000,
  startConfirmationCount: 2,
  exitConfirmationCount: 3,
  reversalConfirmationCount: 2,
  strengthConfirmationCount: 2,
  continuationRisingBreadth: 0.55,
  continuationFallingBreadth: 0.55,
  materialStrengthenBreadth: 0.7,
  materialWeakenBreadth: 0.5,
};

export type ActiveMarketEpisode = {
  episodeId: string;
  episodeAlgorithmVersion: string;
  lifecycleConfigVersion: string;
  universeId: string;
  universeVersion: string;
  primaryWindowMinutes: 5;
  direction: MarketEpisodeDirection;
  classifierAlgorithmVersion: string;
  classifierConfigVersion: string;
  movementAlgorithmVersion: string;
  movementConfigVersion: string;
  startBoundaryTime: number;
  lastEvaluationBoundaryTime: number;
  confirmedPace: MarketPace;
  highMaterialBreadth: boolean;
  lowMaterialBreadth: boolean;
};

/**
 * Episode-scoping versions that identify which episode/config/universe an event
 * belongs to. Context-change ENDED events must describe the episode being ended,
 * not the evaluation that triggered the termination.
 */
type MarketEpisodeScope = Pick<
  ActiveMarketEpisode,
  | "episodeAlgorithmVersion"
  | "lifecycleConfigVersion"
  | "universeId"
  | "universeVersion"
  | "primaryWindowMinutes"
  | "classifierAlgorithmVersion"
  | "classifierConfigVersion"
  | "movementAlgorithmVersion"
  | "movementConfigVersion"
>;

export type MarketEpisodeLifecycleState = {
  episodeAlgorithmVersion: string;
  lifecycleConfigVersion: string;
  universeId: string;
  universeVersion: string;
  primaryWindowMinutes: 5;
  provider: "binance-usdm";
  exchange: "binance";
  priceType: "trade";
  evaluationBoundaryTime: number;
  currentDirectionState: MarketDirectionState;
  currentPace: MarketPace;
  interrupted: boolean;
  activeEpisode: ActiveMarketEpisode | null;
  pendingCandidate: {
    direction: MarketEpisodeDirection;
    startBoundaryTime: number;
    count: number;
  } | null;
  pendingExitFailureCount: number;
  pendingReversal: {
    toDirection: MarketEpisodeDirection;
    startBoundaryTime: number;
    count: number;
  } | null;
  pendingStrengthen: {
    reason: string;
    count: number;
  } | null;
  pendingWeaken: {
    reason: string;
    count: number;
  } | null;
  pendingResume: {
    direction: MarketEpisodeDirection;
    count: number;
  } | null;
  lastPersistedTime?: number | undefined;
  classifierAlgorithmVersion: string;
  classifierConfigVersion: string;
  movementAlgorithmVersion: string;
  movementConfigVersion: string;
};

export type SerializedMarketEpisodeLifecycleState = Omit<
  MarketEpisodeLifecycleState,
  "lastPersistedTime"
> & {
  serializationVersion: typeof MARKET_EPISODE_STATE_SERIALIZATION_VERSION;
  lastPersistedTime: number | null;
};

export type MarketMovementEvent = {
  eventId: string;
  episodeId: string;
  episodeAlgorithmVersion: string;
  lifecycleConfigVersion: string;
  transition: MarketEpisodeTransitionType;
  transitionReason: string;
  fromDirection: MarketEpisodeDirection | null;
  toDirection: MarketEpisodeDirection | null;
  episodeStartBoundaryTime: number;
  evaluationBoundaryTime: number;
  universeId: string;
  universeVersion: string;
  primaryWindowMinutes: 5;
  provider: "binance-usdm";
  exchange: "binance";
  priceType: "trade";
  direction: MarketEpisodeDirection;
  pace: MarketPace;
  directionalBreadth: number;
  materialBreadth: number;
  medianRawReturn: number | null;
  medianNormalizedMovement: number | null;
  medianAcceleration: number | null;
  accelerationBreadth: number;
  dispersion: number | null;
  rvolSummary: MarketStateEvidence["rvolSummary"];
  outliers: IsolatedMarketOutlier[];
  supportingContracts: string[];
  conflictingContracts: string[];
  configuredUniverse: string[];
  includedSymbols: string[];
  excludedSymbols: Array<{ symbol: string; reasons: SymbolExclusionReason[] }>;
  windowsContext: MarketStateWindowClassification[];
  classifierAlgorithmVersion: string;
  classifierConfigVersion: string;
  movementAlgorithmVersion: string;
  movementConfigVersion: string;
};

export type ProcessMarketEpisodeLifecycleInput = {
  classification: MarketStateClassification;
  movement: MarketMovementEvaluation;
  previousState: MarketEpisodeLifecycleState | null;
  config?: MarketEpisodeLifecycleConfig;
};

export type ProcessMarketEpisodeLifecycleResult = {
  nextState: MarketEpisodeLifecycleState;
  transitions: MarketMovementEvent[];
  currentEvidence: MarketStateEvidence;
  shouldPersistCurrentImmediately: boolean;
};

export function computeEpisodeId(input: {
  universeId: string;
  universeVersion: string;
  primaryWindowMinutes: number;
  direction: MarketEpisodeDirection;
  startBoundaryTime: number;
  movementAlgorithmVersion: string;
  movementConfigVersion: string;
  classifierAlgorithmVersion: string;
  classifierConfigVersion: string;
  episodeAlgorithmVersion: string;
  lifecycleConfigVersion: string;
}): string {
  const payload = [
    "market_episode",
    input.universeId,
    input.universeVersion,
    input.primaryWindowMinutes,
    input.direction,
    input.startBoundaryTime,
    input.movementAlgorithmVersion,
    input.movementConfigVersion,
    input.classifierAlgorithmVersion,
    input.classifierConfigVersion,
    input.episodeAlgorithmVersion,
    input.lifecycleConfigVersion,
  ].join(":");
  const hash = createHash("sha256").update(payload, "utf8").digest("hex").slice(0, 32);
  return `mep_${hash}`;
}

export function computeEventId(input: {
  episodeId: string;
  transition: MarketEpisodeTransitionType;
  evaluationBoundaryTime: number;
  toDirection?: MarketEpisodeDirection | null;
}): string {
  const payload = [
    "market_event",
    input.episodeId,
    input.transition,
    input.evaluationBoundaryTime,
    input.toDirection ?? "",
  ].join(":");
  const hash = createHash("sha256").update(payload, "utf8").digest("hex").slice(0, 32);
  return `mevt_${hash}`;
}

function cloneLifecycleState(state: MarketEpisodeLifecycleState): MarketEpisodeLifecycleState {
  return {
    ...state,
    activeEpisode: state.activeEpisode ? { ...state.activeEpisode } : null,
    pendingCandidate: state.pendingCandidate ? { ...state.pendingCandidate } : null,
    pendingReversal: state.pendingReversal ? { ...state.pendingReversal } : null,
    pendingStrengthen: state.pendingStrengthen ? { ...state.pendingStrengthen } : null,
    pendingWeaken: state.pendingWeaken ? { ...state.pendingWeaken } : null,
    pendingResume: state.pendingResume ? { ...state.pendingResume } : null,
  };
}

export function serializeMarketEpisodeLifecycleState(
  state: MarketEpisodeLifecycleState,
): SerializedMarketEpisodeLifecycleState {
  return {
    ...cloneLifecycleState(state),
    serializationVersion: MARKET_EPISODE_STATE_SERIALIZATION_VERSION,
    lastPersistedTime: state.lastPersistedTime ?? null,
  };
}

export function deserializeMarketEpisodeLifecycleState(
  serialized: SerializedMarketEpisodeLifecycleState,
): MarketEpisodeLifecycleState {
  if (serialized.serializationVersion !== MARKET_EPISODE_STATE_SERIALIZATION_VERSION) {
    throw new Error(`Unsupported market episode state version: ${serialized.serializationVersion}`);
  }
  const { serializationVersion: _serializationVersion, lastPersistedTime, ...state } = serialized;
  return cloneLifecycleState({
    ...state,
    lastPersistedTime: lastPersistedTime ?? undefined,
  });
}

/** Call only after the operational current-state write has succeeded. */
export function markMarketEpisodeStatePersisted(
  state: MarketEpisodeLifecycleState,
): MarketEpisodeLifecycleState {
  return {
    ...cloneLifecycleState(state),
    lastPersistedTime: state.evaluationBoundaryTime,
  };
}

export function extractSupportingAndConflictingSymbols(
  movementWindow: MarketMovementWindowResult,
  direction: MarketEpisodeDirection,
): { supportingContracts: string[]; conflictingContracts: string[] } {
  const supportingContracts: string[] = [];
  const conflictingContracts: string[] = [];

  for (const sym of movementWindow.symbols) {
    if (!sym.included) continue;
    if (direction === "BROAD_RISE") {
      if (sym.direction === "RISING") supportingContracts.push(sym.symbol);
      else if (sym.direction === "FALLING") conflictingContracts.push(sym.symbol);
    } else {
      if (sym.direction === "FALLING") supportingContracts.push(sym.symbol);
      else if (sym.direction === "RISING") conflictingContracts.push(sym.symbol);
    }
  }

  supportingContracts.sort((a, b) => a.localeCompare(b));
  conflictingContracts.sort((a, b) => a.localeCompare(b));

  return { supportingContracts, conflictingContracts };
}

function validateInputs(
  classification: MarketStateClassification,
  movement: MarketMovementEvaluation,
): void {
  if (classification.evaluationBoundaryTime !== movement.evaluationBoundaryTime) {
    throw new Error(
      `Evaluation boundary mismatch: classification=${classification.evaluationBoundaryTime}, movement=${movement.evaluationBoundaryTime}`,
    );
  }
  if (classification.universeId !== movement.universeId) {
    throw new Error(
      `Universe ID mismatch: classification=${classification.universeId}, movement=${movement.universeId}`,
    );
  }
  if (classification.universeVersion !== movement.universeVersion) {
    throw new Error(
      `Universe version mismatch: classification=${classification.universeVersion}, movement=${movement.universeVersion}`,
    );
  }
  if (classification.provider !== movement.provider) {
    throw new Error(
      `Provider mismatch: classification=${classification.provider}, movement=${movement.provider}`,
    );
  }
  if (classification.priceType !== movement.priceType) {
    throw new Error(
      `Price type mismatch: classification=${classification.priceType}, movement=${movement.priceType}`,
    );
  }
  if (classification.movementAlgorithmVersion !== movement.algorithmVersion) {
    throw new Error(
      `Movement algorithm version mismatch: classification=${classification.movementAlgorithmVersion}, movement=${movement.algorithmVersion}`,
    );
  }
  if (classification.movementConfigVersion !== movement.configVersion) {
    throw new Error(
      `Movement config version mismatch: classification=${classification.movementConfigVersion}, movement=${movement.configVersion}`,
    );
  }
}

function buildEvent(input: {
  episodeId: string;
  transition: MarketEpisodeTransitionType;
  transitionReason: string;
  fromDirection: MarketEpisodeDirection | null;
  toDirection: MarketEpisodeDirection | null;
  episodeStartBoundaryTime: number;
  evaluationBoundaryTime: number;
  classification: MarketStateClassification;
  primaryWindow: MarketStateWindowClassification;
  primaryMovementWindow: MarketMovementWindowResult;
  direction: MarketEpisodeDirection;
  lifecycleConfigVersion: string;
  /**
   * When set, episode-scoping fields describe this episode scope instead of the
   * current classification/config (used when terminating an episode on context change).
   */
  episodeScope?: MarketEpisodeScope;
}): MarketMovementEvent {
  const { classification, primaryWindow, primaryMovementWindow, direction, episodeScope } = input;
  const { supportingContracts, conflictingContracts } = extractSupportingAndConflictingSymbols(
    primaryMovementWindow,
    direction,
  );

  const directionalBreadth =
    direction === "BROAD_RISE"
      ? primaryWindow.evidence.risingFraction
      : primaryWindow.evidence.fallingFraction;

  const materialBreadth =
    direction === "BROAD_RISE"
      ? primaryWindow.evidence.materialRisingFraction
      : primaryWindow.evidence.materialFallingFraction;

  const accelerationBreadth =
    direction === "BROAD_RISE"
      ? primaryWindow.evidence.positiveAccelerationFraction
      : primaryWindow.evidence.negativeAccelerationFraction;

  const eventId = computeEventId({
    episodeId: input.episodeId,
    transition: input.transition,
    evaluationBoundaryTime: input.evaluationBoundaryTime,
    toDirection: input.toDirection,
  });

  return {
    eventId,
    episodeId: input.episodeId,
    episodeAlgorithmVersion:
      episodeScope?.episodeAlgorithmVersion ?? MARKET_EPISODE_ALGORITHM_VERSION,
    lifecycleConfigVersion: episodeScope?.lifecycleConfigVersion ?? input.lifecycleConfigVersion,
    transition: input.transition,
    transitionReason: input.transitionReason,
    fromDirection: input.fromDirection,
    toDirection: input.toDirection,
    episodeStartBoundaryTime: input.episodeStartBoundaryTime,
    evaluationBoundaryTime: input.evaluationBoundaryTime,
    universeId: episodeScope?.universeId ?? classification.universeId,
    universeVersion: episodeScope?.universeVersion ?? classification.universeVersion,
    primaryWindowMinutes: episodeScope?.primaryWindowMinutes ?? 5,
    provider: classification.provider,
    exchange: classification.exchange,
    priceType: classification.priceType,
    direction,
    pace: primaryWindow.pace,
    directionalBreadth,
    materialBreadth,
    medianRawReturn: primaryWindow.evidence.medianRawReturn.available
      ? primaryWindow.evidence.medianRawReturn.value
      : null,
    medianNormalizedMovement: primaryWindow.evidence.medianNormalizedMovement.available
      ? primaryWindow.evidence.medianNormalizedMovement.value
      : null,
    medianAcceleration: primaryWindow.evidence.medianAcceleration.available
      ? primaryWindow.evidence.medianAcceleration.value
      : null,
    accelerationBreadth,
    dispersion: primaryWindow.evidence.dispersion.available
      ? primaryWindow.evidence.dispersion.value
      : null,
    rvolSummary: primaryWindow.evidence.rvolSummary,
    outliers: primaryWindow.evidence.isolatedOutliers ?? [],
    supportingContracts,
    conflictingContracts,
    configuredUniverse: [...primaryMovementWindow.configuredUniverse],
    includedSymbols: [...primaryWindow.evidence.includedSymbols],
    excludedSymbols: primaryWindow.evidence.excludedSymbols.map(({ symbol, reasons }) => ({
      symbol,
      reasons: [...reasons],
    })),
    windowsContext: classification.windows,
    classifierAlgorithmVersion:
      episodeScope?.classifierAlgorithmVersion ?? classification.algorithmVersion,
    classifierConfigVersion: episodeScope?.classifierConfigVersion ?? classification.configVersion,
    movementAlgorithmVersion:
      episodeScope?.movementAlgorithmVersion ?? classification.movementAlgorithmVersion,
    movementConfigVersion:
      episodeScope?.movementConfigVersion ?? classification.movementConfigVersion,
  };
}

function checkContinuation(
  direction: MarketEpisodeDirection,
  evidence: MarketStateEvidence,
  config: MarketEpisodeLifecycleConfig,
): boolean {
  if (direction === "BROAD_RISE") {
    return (
      evidence.risingFraction >= config.continuationRisingBreadth &&
      evidence.medianRawReturn.available &&
      evidence.medianRawReturn.value > 0
    );
  }
  return (
    evidence.fallingFraction >= config.continuationFallingBreadth &&
    evidence.medianRawReturn.available &&
    evidence.medianRawReturn.value < 0
  );
}

export function restoreLifecycleStateOnRestart(
  persisted: MarketEpisodeLifecycleState,
): MarketEpisodeLifecycleState {
  return {
    ...persisted,
    interrupted: persisted.activeEpisode !== null,
    currentDirectionState: "WARMING",
    currentPace: "NOT_APPLICABLE",
    pendingCandidate: null,
    pendingExitFailureCount: 0,
    pendingReversal: null,
    pendingStrengthen: null,
    pendingWeaken: null,
    pendingResume: null,
  };
}

/**
 * Pure lifecycle state machine for market episodes (#73).
 * Consumes classified and movement data, outputs next state and emitted transitions.
 */
export function processMarketEpisodeLifecycle(
  input: ProcessMarketEpisodeLifecycleInput,
): ProcessMarketEpisodeLifecycleResult {
  validateInputs(input.classification, input.movement);

  const config = input.config ?? DEFAULT_MARKET_EPISODE_LIFECYCLE_CONFIG;
  const primaryWindow = input.classification.windows.find((w) => w.windowMinutes === 5);
  if (!primaryWindow) {
    throw new Error("Market state classification is missing the 5m primary window");
  }

  const primaryMovementWindow = input.movement.windows.find((w) => w.windowMinutes === 5);
  if (!primaryMovementWindow) {
    throw new Error("Market movement evaluation is missing the 5m primary window");
  }

  const evaluationBoundary = input.classification.evaluationBoundaryTime;
  const currentDirectionState = primaryWindow.directionState;
  const currentPace = primaryWindow.pace;
  const previous = input.previousState;
  const hasEvaluationGap =
    previous !== null &&
    evaluationBoundary > previous.evaluationBoundaryTime + MARKET_EPISODE_EVALUATION_CADENCE_MS;

  if (previous && evaluationBoundary < previous.evaluationBoundaryTime) {
    throw new Error("Market episode evaluations must not move backward in time");
  }
  if (previous && evaluationBoundary === previous.evaluationBoundaryTime) {
    return {
      nextState: cloneLifecycleState(previous),
      transitions: [],
      currentEvidence: primaryWindow.evidence,
      shouldPersistCurrentImmediately: false,
    };
  }

  const transitions: MarketMovementEvent[] = [];
  let shouldPersistImmediately = false;

  // Clone or initialize state
  let activeEpisode: ActiveMarketEpisode | null = previous?.activeEpisode
    ? { ...previous.activeEpisode }
    : null;
  let interrupted = previous?.interrupted ?? false;
  let pendingCandidate = previous?.pendingCandidate ? { ...previous.pendingCandidate } : null;
  let pendingExitFailureCount = previous?.pendingExitFailureCount ?? 0;
  let pendingReversal = previous?.pendingReversal ? { ...previous.pendingReversal } : null;
  let pendingStrengthen = previous?.pendingStrengthen ? { ...previous.pendingStrengthen } : null;
  let pendingWeaken = previous?.pendingWeaken ? { ...previous.pendingWeaken } : null;
  let pendingResume = previous?.pendingResume ? { ...previous.pendingResume } : null;
  const lastPersistedTime = previous?.lastPersistedTime;

  // Check 1: Any context change ends an active episode or clears pending inactive state.
  const priorContext = activeEpisode ?? previous;
  if (priorContext !== null) {
    let changeReason: string | null = null;
    if (priorContext.universeId !== input.classification.universeId) {
      changeReason = "universe_changed";
    } else if (priorContext.universeVersion !== input.classification.universeVersion) {
      changeReason = "universe_version_changed";
    } else if (priorContext.classifierAlgorithmVersion !== input.classification.algorithmVersion) {
      changeReason = "classifier_algorithm_version_changed";
    } else if (priorContext.classifierConfigVersion !== input.classification.configVersion) {
      changeReason = "classifier_config_version_changed";
    } else if (
      priorContext.movementAlgorithmVersion !== input.classification.movementAlgorithmVersion
    ) {
      changeReason = "movement_algorithm_version_changed";
    } else if (priorContext.movementConfigVersion !== input.classification.movementConfigVersion) {
      changeReason = "movement_config_version_changed";
    } else if (priorContext.episodeAlgorithmVersion !== MARKET_EPISODE_ALGORITHM_VERSION) {
      changeReason = "episode_algorithm_version_changed";
    } else if (priorContext.lifecycleConfigVersion !== config.version) {
      changeReason = "lifecycle_config_version_changed";
    }

    if (changeReason !== null && activeEpisode !== null) {
      transitions.push(
        buildEvent({
          episodeId: activeEpisode.episodeId,
          transition: "ENDED",
          transitionReason: changeReason,
          fromDirection: activeEpisode.direction,
          toDirection: null,
          episodeStartBoundaryTime: activeEpisode.startBoundaryTime,
          evaluationBoundaryTime: evaluationBoundary,
          classification: input.classification,
          primaryWindow,
          primaryMovementWindow,
          direction: activeEpisode.direction,
          lifecycleConfigVersion: config.version,
          // The ENDED event describes the episode being terminated, so its
          // universe/config/algorithm scope must come from the old episode.
          episodeScope: activeEpisode,
        }),
      );
      activeEpisode = null;
      interrupted = false;
      pendingCandidate = null;
      pendingExitFailureCount = 0;
      pendingReversal = null;
      pendingStrengthen = null;
      pendingWeaken = null;
      pendingResume = null;
      shouldPersistImmediately = true;
    } else if (changeReason !== null) {
      pendingCandidate = null;
      pendingExitFailureCount = 0;
      pendingReversal = null;
      pendingStrengthen = null;
      pendingWeaken = null;
      pendingResume = null;
    }
  }

  // Check 2: A skipped 5-second boundary breaks every consecutive confirmation sequence.
  if (hasEvaluationGap) {
    pendingCandidate = null;
    pendingExitFailureCount = 0;
    pendingReversal = null;
    pendingStrengthen = null;
    pendingWeaken = null;
    pendingResume = null;

    if (activeEpisode === null) {
      interrupted = false;
      if (currentDirectionState === "BROAD_RISE" || currentDirectionState === "BROAD_DROP") {
        pendingCandidate = {
          direction: currentDirectionState,
          startBoundaryTime: evaluationBoundary,
          count: 1,
        };
      }
    } else {
      interrupted = true;
      shouldPersistImmediately = true;
      const isOpposite =
        (activeEpisode.direction === "BROAD_RISE" && currentDirectionState === "BROAD_DROP") ||
        (activeEpisode.direction === "BROAD_DROP" && currentDirectionState === "BROAD_RISE");
      if (isOpposite) {
        pendingReversal = {
          toDirection: currentDirectionState as MarketEpisodeDirection,
          startBoundaryTime: evaluationBoundary,
          count: 1,
        };
      } else if (currentDirectionState === activeEpisode.direction) {
        pendingResume = { direction: activeEpisode.direction, count: 1 };
      }
    }
  } else if (currentDirectionState === "WARMING" || currentDirectionState === "UNAVAILABLE") {
    if (activeEpisode !== null) {
      if (!interrupted) {
        interrupted = true;
        shouldPersistImmediately = true;
      }
      pendingExitFailureCount = 0;
      pendingReversal = null;
      pendingStrengthen = null;
      pendingWeaken = null;
      pendingResume = null;
    } else {
      pendingCandidate = null;
    }

    const wasDegraded =
      previous?.currentDirectionState === "WARMING" ||
      previous?.currentDirectionState === "UNAVAILABLE";
    if (!wasDegraded) {
      shouldPersistImmediately = true;
    }
  } else if (activeEpisode === null) {
    // Check 3: No active episode -> candidate start tracking
    if (currentDirectionState === "BROAD_RISE" || currentDirectionState === "BROAD_DROP") {
      if (pendingCandidate === null || pendingCandidate.direction !== currentDirectionState) {
        pendingCandidate = {
          direction: currentDirectionState,
          startBoundaryTime: evaluationBoundary,
          count: 1,
        };
      } else {
        pendingCandidate.count += 1;
        if (pendingCandidate.count >= config.startConfirmationCount) {
          // Confirmed broad entry!
          const startBoundaryTime = pendingCandidate.startBoundaryTime;
          const direction = pendingCandidate.direction;
          const episodeId = computeEpisodeId({
            universeId: input.classification.universeId,
            universeVersion: input.classification.universeVersion,
            primaryWindowMinutes: 5,
            direction,
            startBoundaryTime,
            movementAlgorithmVersion: input.classification.movementAlgorithmVersion,
            movementConfigVersion: input.classification.movementConfigVersion,
            classifierAlgorithmVersion: input.classification.algorithmVersion,
            classifierConfigVersion: input.classification.configVersion,
            episodeAlgorithmVersion: MARKET_EPISODE_ALGORITHM_VERSION,
            lifecycleConfigVersion: config.version,
          });

          const currentMaterial =
            direction === "BROAD_RISE"
              ? primaryWindow.evidence.materialRisingFraction
              : primaryWindow.evidence.materialFallingFraction;

          activeEpisode = {
            episodeId,
            episodeAlgorithmVersion: MARKET_EPISODE_ALGORITHM_VERSION,
            lifecycleConfigVersion: config.version,
            universeId: input.classification.universeId,
            universeVersion: input.classification.universeVersion,
            primaryWindowMinutes: 5,
            direction,
            classifierAlgorithmVersion: input.classification.algorithmVersion,
            classifierConfigVersion: input.classification.configVersion,
            movementAlgorithmVersion: input.classification.movementAlgorithmVersion,
            movementConfigVersion: input.classification.movementConfigVersion,
            startBoundaryTime,
            lastEvaluationBoundaryTime: evaluationBoundary,
            confirmedPace: currentPace,
            highMaterialBreadth: currentMaterial >= config.materialStrengthenBreadth,
            lowMaterialBreadth: false,
          };

          transitions.push(
            buildEvent({
              episodeId,
              transition: "STARTED",
              transitionReason: "confirmed_broad_entry",
              fromDirection: null,
              toDirection: direction,
              episodeStartBoundaryTime: startBoundaryTime,
              evaluationBoundaryTime: evaluationBoundary,
              classification: input.classification,
              primaryWindow,
              primaryMovementWindow,
              direction,
              lifecycleConfigVersion: config.version,
            }),
          );

          pendingCandidate = null;
          shouldPersistImmediately = true;
        }
      }
    } else {
      // NEUTRAL breaks pending candidate
      pendingCandidate = null;
    }
  } else if (interrupted) {
    // Check 4: Active episode is currently interrupted -> requires fresh 2-eval confirmation
    const isOpposite =
      (activeEpisode.direction === "BROAD_RISE" && currentDirectionState === "BROAD_DROP") ||
      (activeEpisode.direction === "BROAD_DROP" && currentDirectionState === "BROAD_RISE");

    if (isOpposite) {
      pendingResume = null;
      if (pendingReversal === null || pendingReversal.toDirection !== currentDirectionState) {
        pendingReversal = {
          toDirection: currentDirectionState,
          startBoundaryTime: evaluationBoundary,
          count: 1,
        };
      } else {
        pendingReversal.count += 1;
        if (pendingReversal.count >= config.reversalConfirmationCount) {
          // Reversal confirmed from interrupted episode!
          const newStartBoundary = pendingReversal.startBoundaryTime;
          const toDirection = pendingReversal.toDirection;
          const oldDirection = activeEpisode.direction;

          transitions.push(
            buildEvent({
              episodeId: activeEpisode.episodeId,
              transition: "REVERSED",
              transitionReason: "reversal_confirmed",
              fromDirection: oldDirection,
              toDirection,
              episodeStartBoundaryTime: activeEpisode.startBoundaryTime,
              evaluationBoundaryTime: evaluationBoundary,
              classification: input.classification,
              primaryWindow,
              primaryMovementWindow,
              direction: toDirection,
              lifecycleConfigVersion: config.version,
            }),
          );

          const newEpisodeId = computeEpisodeId({
            universeId: input.classification.universeId,
            universeVersion: input.classification.universeVersion,
            primaryWindowMinutes: 5,
            direction: toDirection,
            startBoundaryTime: newStartBoundary,
            movementAlgorithmVersion: input.classification.movementAlgorithmVersion,
            movementConfigVersion: input.classification.movementConfigVersion,
            classifierAlgorithmVersion: input.classification.algorithmVersion,
            classifierConfigVersion: input.classification.configVersion,
            episodeAlgorithmVersion: MARKET_EPISODE_ALGORITHM_VERSION,
            lifecycleConfigVersion: config.version,
          });

          const currentMaterial =
            toDirection === "BROAD_RISE"
              ? primaryWindow.evidence.materialRisingFraction
              : primaryWindow.evidence.materialFallingFraction;

          activeEpisode = {
            episodeId: newEpisodeId,
            episodeAlgorithmVersion: MARKET_EPISODE_ALGORITHM_VERSION,
            lifecycleConfigVersion: config.version,
            universeId: input.classification.universeId,
            universeVersion: input.classification.universeVersion,
            primaryWindowMinutes: 5,
            direction: toDirection,
            classifierAlgorithmVersion: input.classification.algorithmVersion,
            classifierConfigVersion: input.classification.configVersion,
            movementAlgorithmVersion: input.classification.movementAlgorithmVersion,
            movementConfigVersion: input.classification.movementConfigVersion,
            startBoundaryTime: newStartBoundary,
            lastEvaluationBoundaryTime: evaluationBoundary,
            confirmedPace: currentPace,
            highMaterialBreadth: currentMaterial >= config.materialStrengthenBreadth,
            lowMaterialBreadth: false,
          };

          interrupted = false;
          pendingReversal = null;
          pendingExitFailureCount = 0;
          shouldPersistImmediately = true;
        }
      }
    } else if (currentDirectionState === activeEpisode.direction) {
      pendingReversal = null;
      pendingExitFailureCount = 0;
      if (pendingResume === null || pendingResume.direction !== currentDirectionState) {
        pendingResume = { direction: currentDirectionState, count: 1 };
      } else {
        pendingResume.count += 1;
        if (pendingResume.count >= config.startConfirmationCount) {
          // Fresh broad confirmation complete! Resumed
          interrupted = false;
          pendingResume = null;
          activeEpisode.lastEvaluationBoundaryTime = evaluationBoundary;
          shouldPersistImmediately = true;
        }
      }
    } else {
      // Degraded or neutral
      pendingResume = null;
      pendingReversal = null;
      const continuationHolds = checkContinuation(
        activeEpisode.direction,
        primaryWindow.evidence,
        config,
      );
      if (!continuationHolds) {
        pendingExitFailureCount += 1;
        if (pendingExitFailureCount >= config.exitConfirmationCount) {
          transitions.push(
            buildEvent({
              episodeId: activeEpisode.episodeId,
              transition: "ENDED",
              transitionReason: "continuation_failed",
              fromDirection: activeEpisode.direction,
              toDirection: null,
              episodeStartBoundaryTime: activeEpisode.startBoundaryTime,
              evaluationBoundaryTime: evaluationBoundary,
              classification: input.classification,
              primaryWindow,
              primaryMovementWindow,
              direction: activeEpisode.direction,
              lifecycleConfigVersion: config.version,
            }),
          );
          activeEpisode = null;
          interrupted = false;
          pendingExitFailureCount = 0;
          shouldPersistImmediately = true;
        }
      } else {
        pendingExitFailureCount = 0;
      }
    }
  } else {
    // Check 5: Normal active episode (not interrupted, not degraded)
    activeEpisode.lastEvaluationBoundaryTime = evaluationBoundary;

    const isOpposite =
      (activeEpisode.direction === "BROAD_RISE" && currentDirectionState === "BROAD_DROP") ||
      (activeEpisode.direction === "BROAD_DROP" && currentDirectionState === "BROAD_RISE");

    if (isOpposite) {
      if (pendingReversal === null || pendingReversal.toDirection !== currentDirectionState) {
        pendingReversal = {
          toDirection: currentDirectionState,
          startBoundaryTime: evaluationBoundary,
          count: 1,
        };
      } else {
        pendingReversal.count += 1;
        if (pendingReversal.count >= config.reversalConfirmationCount) {
          // Reversal confirmed!
          const newStartBoundary = pendingReversal.startBoundaryTime;
          const toDirection = pendingReversal.toDirection;
          const oldDirection = activeEpisode.direction;

          transitions.push(
            buildEvent({
              episodeId: activeEpisode.episodeId,
              transition: "REVERSED",
              transitionReason: "reversal_confirmed",
              fromDirection: oldDirection,
              toDirection,
              episodeStartBoundaryTime: activeEpisode.startBoundaryTime,
              evaluationBoundaryTime: evaluationBoundary,
              classification: input.classification,
              primaryWindow,
              primaryMovementWindow,
              direction: toDirection,
              lifecycleConfigVersion: config.version,
            }),
          );

          const newEpisodeId = computeEpisodeId({
            universeId: input.classification.universeId,
            universeVersion: input.classification.universeVersion,
            primaryWindowMinutes: 5,
            direction: toDirection,
            startBoundaryTime: newStartBoundary,
            movementAlgorithmVersion: input.classification.movementAlgorithmVersion,
            movementConfigVersion: input.classification.movementConfigVersion,
            classifierAlgorithmVersion: input.classification.algorithmVersion,
            classifierConfigVersion: input.classification.configVersion,
            episodeAlgorithmVersion: MARKET_EPISODE_ALGORITHM_VERSION,
            lifecycleConfigVersion: config.version,
          });

          const currentMaterial =
            toDirection === "BROAD_RISE"
              ? primaryWindow.evidence.materialRisingFraction
              : primaryWindow.evidence.materialFallingFraction;

          activeEpisode = {
            episodeId: newEpisodeId,
            episodeAlgorithmVersion: MARKET_EPISODE_ALGORITHM_VERSION,
            lifecycleConfigVersion: config.version,
            universeId: input.classification.universeId,
            universeVersion: input.classification.universeVersion,
            primaryWindowMinutes: 5,
            direction: toDirection,
            classifierAlgorithmVersion: input.classification.algorithmVersion,
            classifierConfigVersion: input.classification.configVersion,
            movementAlgorithmVersion: input.classification.movementAlgorithmVersion,
            movementConfigVersion: input.classification.movementConfigVersion,
            startBoundaryTime: newStartBoundary,
            lastEvaluationBoundaryTime: evaluationBoundary,
            confirmedPace: currentPace,
            highMaterialBreadth: currentMaterial >= config.materialStrengthenBreadth,
            lowMaterialBreadth: false,
          };

          pendingReversal = null;
          pendingStrengthen = null;
          pendingWeaken = null;
          pendingExitFailureCount = 0;
          shouldPersistImmediately = true;
        }
      }
    } else {
      pendingReversal = null;
    }

    // Only process continuation and strength transitions if reversal did not just occur
    if (transitions.length === 0) {
      const continuationHolds = checkContinuation(
        activeEpisode.direction,
        primaryWindow.evidence,
        config,
      );

      if (!continuationHolds) {
        pendingExitFailureCount += 1;
        pendingStrengthen = null;
        pendingWeaken = null;

        if (pendingExitFailureCount >= config.exitConfirmationCount) {
          transitions.push(
            buildEvent({
              episodeId: activeEpisode.episodeId,
              transition: "ENDED",
              transitionReason: "continuation_failed",
              fromDirection: activeEpisode.direction,
              toDirection: null,
              episodeStartBoundaryTime: activeEpisode.startBoundaryTime,
              evaluationBoundaryTime: evaluationBoundary,
              classification: input.classification,
              primaryWindow,
              primaryMovementWindow,
              direction: activeEpisode.direction,
              lifecycleConfigVersion: config.version,
            }),
          );
          activeEpisode = null;
          pendingExitFailureCount = 0;
          shouldPersistImmediately = true;
        }
      } else {
        pendingExitFailureCount = 0;

        // Check Strength crossings (STRENGTHENED / WEAKENED)
        const currentMaterial =
          activeEpisode.direction === "BROAD_RISE"
            ? primaryWindow.evidence.materialRisingFraction
            : primaryWindow.evidence.materialFallingFraction;

        const strengthenPace =
          activeEpisode.confirmedPace !== "ACCELERATING" && currentPace === "ACCELERATING";
        const strengthenBreadth =
          !activeEpisode.highMaterialBreadth && currentMaterial >= config.materialStrengthenBreadth;

        const weakenPace =
          activeEpisode.confirmedPace !== "DECELERATING" && currentPace === "DECELERATING";
        const weakenBreadth =
          !activeEpisode.lowMaterialBreadth &&
          currentMaterial <= config.materialWeakenBreadth &&
          continuationHolds;

        const isStrengthening = strengthenPace || strengthenBreadth;
        const isWeakening = weakenPace || weakenBreadth;

        if (isStrengthening && isWeakening) {
          // Conflict: do not emit either; reset pending counters
          pendingStrengthen = null;
          pendingWeaken = null;
        } else if (isStrengthening) {
          pendingWeaken = null;
          const reason =
            strengthenPace && strengthenBreadth
              ? "pace_accelerated_and_material_breadth_expanded"
              : strengthenPace
                ? "pace_accelerated"
                : "material_breadth_expanded";

          if (pendingStrengthen?.reason === reason) {
            pendingStrengthen.count += 1;
            if (pendingStrengthen.count >= config.strengthConfirmationCount) {
              transitions.push(
                buildEvent({
                  episodeId: activeEpisode.episodeId,
                  transition: "STRENGTHENED",
                  transitionReason: reason,
                  fromDirection: activeEpisode.direction,
                  toDirection: null,
                  episodeStartBoundaryTime: activeEpisode.startBoundaryTime,
                  evaluationBoundaryTime: evaluationBoundary,
                  classification: input.classification,
                  primaryWindow,
                  primaryMovementWindow,
                  direction: activeEpisode.direction,
                  lifecycleConfigVersion: config.version,
                }),
              );
              if (strengthenPace) activeEpisode.confirmedPace = "ACCELERATING";
              if (strengthenBreadth) activeEpisode.highMaterialBreadth = true;
              activeEpisode.lowMaterialBreadth = false;
              pendingStrengthen = null;
              shouldPersistImmediately = true;
            }
          } else {
            pendingStrengthen = { reason, count: 1 };
          }
        } else if (isWeakening) {
          pendingStrengthen = null;
          const reason =
            weakenPace && weakenBreadth
              ? "pace_decelerated_and_material_breadth_reduced"
              : weakenPace
                ? "pace_decelerated"
                : "material_breadth_reduced";

          if (pendingWeaken?.reason === reason) {
            pendingWeaken.count += 1;
            if (pendingWeaken.count >= config.strengthConfirmationCount) {
              transitions.push(
                buildEvent({
                  episodeId: activeEpisode.episodeId,
                  transition: "WEAKENED",
                  transitionReason: reason,
                  fromDirection: activeEpisode.direction,
                  toDirection: null,
                  episodeStartBoundaryTime: activeEpisode.startBoundaryTime,
                  evaluationBoundaryTime: evaluationBoundary,
                  classification: input.classification,
                  primaryWindow,
                  primaryMovementWindow,
                  direction: activeEpisode.direction,
                  lifecycleConfigVersion: config.version,
                }),
              );
              if (weakenPace) activeEpisode.confirmedPace = "DECELERATING";
              if (weakenBreadth) activeEpisode.lowMaterialBreadth = true;
              activeEpisode.highMaterialBreadth = false;
              pendingWeaken = null;
              shouldPersistImmediately = true;
            }
          } else {
            pendingWeaken = { reason, count: 1 };
          }
        } else {
          // Neither condition met in this evaluation -> reset pending counters
          pendingStrengthen = null;
          pendingWeaken = null;

          if (currentPace === "MIXED") {
            activeEpisode.confirmedPace = "MIXED";
          }
          if (currentMaterial < config.materialStrengthenBreadth) {
            activeEpisode.highMaterialBreadth = false;
          }
          if (currentMaterial > config.materialWeakenBreadth) {
            activeEpisode.lowMaterialBreadth = false;
          }
        }
      }
    }
  }

  // Periodic persistence check (default: every 30 seconds)
  if (
    !shouldPersistImmediately &&
    (lastPersistedTime === undefined ||
      evaluationBoundary - lastPersistedTime >= config.currentSnapshotCadenceMs)
  ) {
    shouldPersistImmediately = true;
  }

  const nextState: MarketEpisodeLifecycleState = {
    episodeAlgorithmVersion: MARKET_EPISODE_ALGORITHM_VERSION,
    lifecycleConfigVersion: config.version,
    universeId: input.classification.universeId,
    universeVersion: input.classification.universeVersion,
    primaryWindowMinutes: 5,
    provider: input.classification.provider,
    exchange: input.classification.exchange,
    priceType: input.classification.priceType,
    evaluationBoundaryTime: evaluationBoundary,
    currentDirectionState,
    currentPace,
    interrupted,
    activeEpisode,
    pendingCandidate,
    pendingExitFailureCount,
    pendingReversal,
    pendingStrengthen,
    pendingWeaken,
    pendingResume,
    lastPersistedTime,
    classifierAlgorithmVersion: input.classification.algorithmVersion,
    classifierConfigVersion: input.classification.configVersion,
    movementAlgorithmVersion: input.classification.movementAlgorithmVersion,
    movementConfigVersion: input.classification.movementConfigVersion,
  };

  return {
    nextState,
    transitions,
    currentEvidence: primaryWindow.evidence,
    shouldPersistCurrentImmediately: shouldPersistImmediately,
  };
}
