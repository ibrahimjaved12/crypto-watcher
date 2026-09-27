/** Temporary structural view consumed by the TypeScript #73 lifecycle. */
import type { MovementWindowMinutes } from "./movement-contract";
import type { MarketMovementEvaluation, Metric, SymbolExclusionReason } from "./movement-metrics-contract";
import type {
  ClassifierMetric, IsolatedMarketOutlier,
  MarketClassification, MarketClassificationWindow, MarketDirectionState,
  MarketHorizonRole, MarketPaceValue,
} from "./market-state-contract";

export type { ConfirmedMarketDirection, IsolatedMarketOutlier,
  MarketDirectionState, MarketHorizonRole } from "./market-state-contract";

/** The unavailable sentinel belongs to #73 state and persistence, never #72. */
export type MarketPace = MarketPaceValue | "NOT_APPLICABLE";

export type MarketStateEvidence = {
  eligibleCount: number;
  eligibleFraction: number;
  includedSymbols: string[];
  excludedSymbols: Array<{ symbol: string; reasons: SymbolExclusionReason[] }>;
  flatFraction: number | null;
  risingFraction: number | null;
  fallingFraction: number | null;
  materialRisingFraction: number | null;
  materialFallingFraction: number | null;
  medianRawReturn: Metric;
  medianNormalizedMovement: Metric;
  medianAcceleration: ClassifierMetric<number>;
  positiveAccelerationFraction: number | null;
  negativeAccelerationFraction: number | null;
  dispersion: Metric;
  /** Canonical #72 per-symbol context, with no TypeScript aggregate. */
  rvolSummary: MarketClassificationWindow["volumeContext"];
  isolatedOutliers: IsolatedMarketOutlier[];
};

export type MarketStateWindowClassification = {
  windowMinutes: MovementWindowMinutes;
  horizonRole: MarketHorizonRole;
  directionState: MarketDirectionState;
  pace: MarketPace;
  reversalCandidate: boolean;
  algorithmVersion: string;
  configVersion: string;
  movementAlgorithmVersion: string;
  movementConfigVersion: string;
  universeId: string;
  universeVersion: string;
  evaluationBoundaryTime: number;
  provider: "binance-usdm";
  exchange: "binance";
  priceType: "trade";
  evidence: MarketStateEvidence;
};

export type MarketStateClassification = {
  algorithmVersion: string;
  configVersion: string;
  movementAlgorithmVersion: MarketMovementEvaluation["algorithmVersion"];
  movementConfigVersion: string;
  universeId: string;
  universeVersion: string;
  evaluationBoundaryTime: number;
  provider: "binance-usdm";
  exchange: "binance";
  priceType: "trade";
  primaryWindowMinutes: 5;
  windows: MarketStateWindowClassification[];
  canonicalWindows: MarketClassificationWindow[];
};

function fraction(value: { available: boolean; value: { count: number; fraction: number } | null }): number | null {
  return value.available && value.value !== null ? value.value.fraction : null;
}

/** Copies already-produced Python evidence into #73's existing field layout. */
export function projectClassificationForLifecycle(
  classification: MarketClassification,
): MarketStateClassification {
  return {
    algorithmVersion: classification.classifierAlgorithmVersion,
    configVersion: classification.classifierConfigVersion,
    movementAlgorithmVersion: classification.movementAlgorithmVersion as MarketMovementEvaluation["algorithmVersion"],
    movementConfigVersion: classification.movementConfigVersion,
    universeId: classification.universeId, universeVersion: classification.universeVersion,
    evaluationBoundaryTime: classification.evaluationBoundaryTime,
    provider: classification.provider, exchange: classification.exchange,
    priceType: classification.priceType, primaryWindowMinutes: 5,
    canonicalWindows: classification.windows,
    windows: classification.windows.map((window) => ({
      windowMinutes: window.windowMinutes, horizonRole: window.horizonRole,
      directionState: window.directionState,
      pace: window.pace.available ? window.pace.value : "NOT_APPLICABLE",
      reversalCandidate: window.reversalCandidate !== null,
      algorithmVersion: window.classifierAlgorithmVersion,
      configVersion: window.classifierConfigVersion,
      movementAlgorithmVersion: window.movementAlgorithmVersion,
      movementConfigVersion: window.movementConfigVersion,
      universeId: window.universeId, universeVersion: window.universeVersion,
      evaluationBoundaryTime: window.evaluationBoundaryTime,
      provider: window.provider, exchange: window.exchange, priceType: window.priceType,
      evidence: {
        eligibleCount: window.eligibleCount, eligibleFraction: window.eligibleFraction,
        includedSymbols: window.includedSymbols, excludedSymbols: window.excludedSymbols,
        flatFraction: fraction(window.breadth.flat),
        risingFraction: fraction(window.breadth.rising),
        fallingFraction: fraction(window.breadth.falling),
        materialRisingFraction: fraction(window.breadth.materialRising),
        materialFallingFraction: fraction(window.breadth.materialFalling),
        medianRawReturn: window.medianRawReturn,
        medianNormalizedMovement: window.medianNormalizedMovement,
        medianAcceleration: window.medianAcceleration,
        positiveAccelerationFraction: fraction(window.positiveAccelerationBreadth),
        negativeAccelerationFraction: fraction(window.negativeAccelerationBreadth),
        dispersion: window.dispersionMadNormalizedMovement,
        rvolSummary: window.volumeContext,
        isolatedOutliers: window.isolatedOutliers,
      },
    })),
  };
}
