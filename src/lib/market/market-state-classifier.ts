import type { MovementWindowMinutes } from "./movement-buckets";
import type {
  MarketMovementEvaluation,
  MarketMovementWindowResult,
  Metric,
  MetricUnavailableReason,
  SymbolExclusionReason,
} from "./movement-metrics";

export const MARKET_STATE_CLASSIFIER_VERSION = "market-state-v1";

const CLASSIFIED_WINDOWS = [1, 5, 15] as const satisfies readonly MovementWindowMinutes[];
const MINIMUM_MARKET_UNIVERSE_SIZE = 5;

export type MarketDirectionState =
  "BROAD_RISE" | "BROAD_DROP" | "NEUTRAL" | "WARMING" | "UNAVAILABLE";

export type ConfirmedMarketDirection = Extract<MarketDirectionState, "BROAD_RISE" | "BROAD_DROP">;

export type MarketPace = "ACCELERATING" | "DECELERATING" | "MIXED" | "NOT_APPLICABLE";

export type MarketHorizonRole = "RAPID" | "PRIMARY" | "PERSISTENCE";

export type MarketStateClassifierConfig = {
  version: string;
  broadDirectionalBreadth: number;
  broadMaterialBreadth: number;
  normalizedMarketThreshold: number;
  accelerationBreadth: number;
  minimumAccelerationCoverage: number;
  isolatedOutlierDisagreement: number;
};

export const DEFAULT_MARKET_STATE_CLASSIFIER_CONFIG: Readonly<MarketStateClassifierConfig> = {
  version: "market-state-config-v1",
  broadDirectionalBreadth: 0.7,
  broadMaterialBreadth: 0.5,
  normalizedMarketThreshold: 0.5,
  accelerationBreadth: 0.6,
  minimumAccelerationCoverage: 0.6,
  isolatedOutlierDisagreement: 0.5,
};

export type ClassificationMetricUnavailableReason =
  MetricUnavailableReason | "NO_AVAILABLE_ACCELERATION" | "NO_AVAILABLE_RVOL";

export type ClassificationMetric =
  | { available: true; value: number }
  | { available: false; value: null; reason: ClassificationMetricUnavailableReason };

export type IsolatedMarketOutlier = {
  symbol: string;
  rawReturn: number;
  historicalNormalizedZ: number;
  crossSectionalZ: number;
  direction: "RISING" | "FALLING";
  sameDirectionBreadth: number;
  windowMinutes: MovementWindowMinutes;
  evaluationBoundaryTime: number;
};

export type MarketStateEvidence = {
  eligibleCount: number;
  eligibleFraction: number;
  includedSymbols: string[];
  excludedSymbols: Array<{ symbol: string; reasons: SymbolExclusionReason[] }>;
  flatFraction: number;
  risingFraction: number;
  fallingFraction: number;
  materialRisingFraction: number;
  materialFallingFraction: number;
  medianRawReturn: Metric;
  medianNormalizedMovement: Metric;
  medianAcceleration: ClassificationMetric;
  availableAccelerationCount: number;
  accelerationCoverage: number;
  positiveAccelerationFraction: number;
  negativeAccelerationFraction: number;
  dispersion: Metric;
  rvolSummary: {
    availableCount: number;
    coverage: number;
    median: ClassificationMetric;
    bySymbol: Array<{ symbol: string; rvol: Metric }>;
  };
  isolatedOutliers: IsolatedMarketOutlier[];
};

export type MarketStateWindowClassification = {
  windowMinutes: MovementWindowMinutes;
  horizonRole: MarketHorizonRole;
  directionState: MarketDirectionState;
  pace: MarketPace;
  reversalCandidate: boolean;
  algorithmVersion: typeof MARKET_STATE_CLASSIFIER_VERSION;
  configVersion: string;
  movementAlgorithmVersion: MarketMovementWindowResult["algorithmVersion"];
  movementConfigVersion: string;
  universeId: string;
  universeVersion: string;
  evaluationBoundaryTime: number;
  provider: MarketMovementWindowResult["provider"];
  exchange: MarketMovementWindowResult["exchange"];
  priceType: MarketMovementWindowResult["priceType"];
  evidence: MarketStateEvidence;
};

export type MarketStateClassification = {
  algorithmVersion: typeof MARKET_STATE_CLASSIFIER_VERSION;
  configVersion: string;
  movementAlgorithmVersion: MarketMovementEvaluation["algorithmVersion"];
  movementConfigVersion: string;
  universeId: string;
  universeVersion: string;
  evaluationBoundaryTime: number;
  provider: MarketMovementEvaluation["provider"];
  exchange: MarketMovementEvaluation["exchange"];
  priceType: MarketMovementEvaluation["priceType"];
  primaryWindowMinutes: 5;
  windows: MarketStateWindowClassification[];
};

export type ClassifyMarketStateInput = {
  movement: MarketMovementEvaluation;
  previousConfirmedDirection?: Partial<Record<MovementWindowMinutes, ConfirmedMarketDirection>>;
  config?: MarketStateClassifierConfig;
};

function median(values: readonly number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  if (sorted.length % 2 === 1) return sorted[middle]!;
  return (sorted[middle - 1]! + sorted[middle]!) / 2;
}

function available(value: number): ClassificationMetric {
  return { available: true, value };
}

function unavailable(reason: ClassificationMetricUnavailableReason): ClassificationMetric {
  return { available: false, value: null, reason };
}

function cloneMetric(metric: Metric): Metric {
  return { ...metric };
}

function validateConfig(config: MarketStateClassifierConfig): void {
  if (!config.version) throw new Error("market-state classifier config version is required");
  const fractions = [
    config.broadDirectionalBreadth,
    config.broadMaterialBreadth,
    config.accelerationBreadth,
    config.minimumAccelerationCoverage,
    config.isolatedOutlierDisagreement,
  ];
  if (fractions.some((value) => !Number.isFinite(value) || value <= 0 || value > 1)) {
    throw new Error("market-state classifier fractions must be within (0, 1]");
  }
  if (!Number.isFinite(config.normalizedMarketThreshold) || config.normalizedMarketThreshold <= 0) {
    throw new Error("normalized market threshold must be positive and finite");
  }
}

function horizonRole(windowMinutes: MovementWindowMinutes): MarketHorizonRole {
  if (windowMinutes === 1) return "RAPID";
  if (windowMinutes === 5) return "PRIMARY";
  return "PERSISTENCE";
}

function warmingOnly(window: MarketMovementWindowResult): boolean {
  if (
    window.excludedSymbols.length === 0 ||
    window.configuredUniverse.length < MINIMUM_MARKET_UNIVERSE_SIZE
  ) {
    return false;
  }
  return window.excludedSymbols.every(
    ({ reasons }) =>
      reasons.includes("WARMING_INSUFFICIENT_LIVE_HISTORY") &&
      reasons.every(
        (reason) =>
          reason === "WARMING_INSUFFICIENT_LIVE_HISTORY" || reason === "MISSING_EXACT_BOUNDARY",
      ),
  );
}

function directionState(
  window: MarketMovementWindowResult,
  config: MarketStateClassifierConfig,
): MarketDirectionState {
  if (!window.marketWideEligible) return warmingOnly(window) ? "WARMING" : "UNAVAILABLE";

  const medianRawReturn = window.aggregates.medianRawReturn;
  const medianNormalizedMovement = window.aggregates.medianNormalizedMovement;
  const broadRise =
    window.breadth.risingFraction >= config.broadDirectionalBreadth &&
    window.breadth.materialRisingFraction >= config.broadMaterialBreadth &&
    medianRawReturn.available &&
    medianRawReturn.value > 0 &&
    medianNormalizedMovement.available &&
    medianNormalizedMovement.value >= config.normalizedMarketThreshold;
  if (broadRise) return "BROAD_RISE";

  const broadDrop =
    window.breadth.fallingFraction >= config.broadDirectionalBreadth &&
    window.breadth.materialFallingFraction >= config.broadMaterialBreadth &&
    medianRawReturn.available &&
    medianRawReturn.value < 0 &&
    medianNormalizedMovement.available &&
    medianNormalizedMovement.value <= -config.normalizedMarketThreshold;
  return broadDrop ? "BROAD_DROP" : "NEUTRAL";
}

function accelerationEvidence(window: MarketMovementWindowResult) {
  const values = window.symbols.flatMap((symbol) =>
    symbol.included && symbol.acceleration.available ? [symbol.acceleration.value] : [],
  );
  const valueMedian = median(values);
  const availableCount = values.length;
  return {
    values,
    availableCount,
    coverage: window.eligibleCount === 0 ? 0 : availableCount / window.eligibleCount,
    positiveFraction:
      availableCount === 0 ? 0 : values.filter((value) => value > 0).length / availableCount,
    negativeFraction:
      availableCount === 0 ? 0 : values.filter((value) => value < 0).length / availableCount,
    median:
      valueMedian === null ? unavailable("NO_AVAILABLE_ACCELERATION") : available(valueMedian),
  };
}

function pace(
  direction: MarketDirectionState,
  acceleration: ReturnType<typeof accelerationEvidence>,
  config: MarketStateClassifierConfig,
): MarketPace {
  if (direction !== "BROAD_RISE" && direction !== "BROAD_DROP") return "NOT_APPLICABLE";
  if (
    acceleration.coverage < config.minimumAccelerationCoverage ||
    !acceleration.median.available
  ) {
    return "MIXED";
  }

  if (direction === "BROAD_RISE") {
    if (
      acceleration.positiveFraction >= config.accelerationBreadth &&
      acceleration.median.value > 0
    ) {
      return "ACCELERATING";
    }
    if (
      acceleration.negativeFraction >= config.accelerationBreadth &&
      acceleration.median.value < 0
    ) {
      return "DECELERATING";
    }
    return "MIXED";
  }

  if (
    acceleration.negativeFraction >= config.accelerationBreadth &&
    acceleration.median.value < 0
  ) {
    return "ACCELERATING";
  }
  if (
    acceleration.positiveFraction >= config.accelerationBreadth &&
    acceleration.median.value > 0
  ) {
    return "DECELERATING";
  }
  return "MIXED";
}

function isolatedOutliers(
  window: MarketMovementWindowResult,
  config: MarketStateClassifierConfig,
): IsolatedMarketOutlier[] {
  if (!window.marketWideEligible) return [];
  const result: IsolatedMarketOutlier[] = [];
  for (const symbol of window.symbols) {
    if (
      !symbol.included ||
      !symbol.outlierCandidate ||
      !symbol.currentReturn.available ||
      !symbol.normalizedZ.available ||
      !symbol.crossSectionalZ.available
    ) {
      continue;
    }
    const direction =
      symbol.currentReturn.value > 0 ? "RISING" : symbol.currentReturn.value < 0 ? "FALLING" : null;
    if (direction === null) continue;
    const sameDirectionBreadth =
      direction === "RISING" ? window.breadth.risingFraction : window.breadth.fallingFraction;
    if (sameDirectionBreadth >= config.isolatedOutlierDisagreement) continue;
    result.push({
      symbol: symbol.symbol,
      rawReturn: symbol.currentReturn.value,
      historicalNormalizedZ: symbol.normalizedZ.value,
      crossSectionalZ: symbol.crossSectionalZ.value,
      direction,
      sameDirectionBreadth,
      windowMinutes: window.windowMinutes,
      evaluationBoundaryTime: window.evaluationBoundaryTime,
    });
  }
  return result;
}

function rvolSummary(window: MarketMovementWindowResult): MarketStateEvidence["rvolSummary"] {
  const bySymbol = window.symbols
    .filter((symbol) => symbol.included)
    .map((symbol) => ({ symbol: symbol.symbol, rvol: cloneMetric(symbol.rvol) }));
  const availableValues = bySymbol.flatMap((entry) =>
    entry.rvol.available ? [entry.rvol.value] : [],
  );
  const valueMedian = median(availableValues);
  return {
    availableCount: availableValues.length,
    coverage: window.eligibleCount === 0 ? 0 : availableValues.length / window.eligibleCount,
    median: valueMedian === null ? unavailable("NO_AVAILABLE_RVOL") : available(valueMedian),
    bySymbol,
  };
}

function classifyWindow(
  window: MarketMovementWindowResult,
  previousDirection: ConfirmedMarketDirection | undefined,
  config: MarketStateClassifierConfig,
): MarketStateWindowClassification {
  const direction = directionState(window, config);
  const acceleration = accelerationEvidence(window);
  const reversalCandidate =
    (previousDirection === "BROAD_RISE" && direction === "BROAD_DROP") ||
    (previousDirection === "BROAD_DROP" && direction === "BROAD_RISE");

  return {
    windowMinutes: window.windowMinutes,
    horizonRole: horizonRole(window.windowMinutes),
    directionState: direction,
    pace: pace(direction, acceleration, config),
    reversalCandidate,
    algorithmVersion: MARKET_STATE_CLASSIFIER_VERSION,
    configVersion: config.version,
    movementAlgorithmVersion: window.algorithmVersion,
    movementConfigVersion: window.configVersion,
    universeId: window.universeId,
    universeVersion: window.universeVersion,
    evaluationBoundaryTime: window.evaluationBoundaryTime,
    provider: window.provider,
    exchange: window.exchange,
    priceType: window.priceType,
    evidence: {
      eligibleCount: window.eligibleCount,
      eligibleFraction: window.eligibleFraction,
      includedSymbols: [...window.includedSymbols],
      excludedSymbols: window.excludedSymbols.map(({ symbol, reasons }) => ({
        symbol,
        reasons: [...reasons],
      })),
      flatFraction: window.breadth.flatFraction,
      risingFraction: window.breadth.risingFraction,
      fallingFraction: window.breadth.fallingFraction,
      materialRisingFraction: window.breadth.materialRisingFraction,
      materialFallingFraction: window.breadth.materialFallingFraction,
      medianRawReturn: cloneMetric(window.aggregates.medianRawReturn),
      medianNormalizedMovement: cloneMetric(window.aggregates.medianNormalizedMovement),
      medianAcceleration: acceleration.median,
      availableAccelerationCount: acceleration.availableCount,
      accelerationCoverage: acceleration.coverage,
      positiveAccelerationFraction: acceleration.positiveFraction,
      negativeAccelerationFraction: acceleration.negativeFraction,
      dispersion: cloneMetric(window.aggregates.dispersionMadNormalizedMovement),
      rvolSummary: rvolSummary(window),
      isolatedOutliers: isolatedOutliers(window, config),
    },
  };
}

/** Pure classification entrypoint shared by live evaluation and historical replay. */
export function classifyMarketState(input: ClassifyMarketStateInput): MarketStateClassification {
  const config = input.config ?? DEFAULT_MARKET_STATE_CLASSIFIER_CONFIG;
  validateConfig(config);

  const windowsByMinutes = new Map(
    input.movement.windows.map((window) => [window.windowMinutes, window]),
  );
  if (
    windowsByMinutes.size !== CLASSIFIED_WINDOWS.length ||
    CLASSIFIED_WINDOWS.some((windowMinutes) => !windowsByMinutes.has(windowMinutes))
  ) {
    throw new Error("market-state classification requires exactly one 1m, 5m, and 15m window");
  }

  return {
    algorithmVersion: MARKET_STATE_CLASSIFIER_VERSION,
    configVersion: config.version,
    movementAlgorithmVersion: input.movement.algorithmVersion,
    movementConfigVersion: input.movement.configVersion,
    universeId: input.movement.universeId,
    universeVersion: input.movement.universeVersion,
    evaluationBoundaryTime: input.movement.evaluationBoundaryTime,
    provider: input.movement.provider,
    exchange: input.movement.exchange,
    priceType: input.movement.priceType,
    primaryWindowMinutes: 5,
    windows: CLASSIFIED_WINDOWS.map((windowMinutes) =>
      classifyWindow(
        windowsByMinutes.get(windowMinutes)!,
        input.previousConfirmedDirection?.[windowMinutes],
        config,
      ),
    ),
  };
}
