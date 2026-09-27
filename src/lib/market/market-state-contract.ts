/** Transport contract for canonical Python #72 classification. No classification runs here. */
import type { MovementWindowMinutes } from "./movement-contract";
import type { MarketMovementWindowResult, Metric, BreadthSide,
  SymbolExclusionReason } from "./movement-metrics-contract";

export const MARKET_STATE_CLASSIFIER_VERSION = "market-state-classifier-v1";

export type MarketDirectionState =
  "BROAD_RISE" | "BROAD_DROP" | "NEUTRAL" | "WARMING" | "UNAVAILABLE";
export type ConfirmedMarketDirection = Extract<MarketDirectionState, "BROAD_RISE" | "BROAD_DROP">;
export type MarketPaceValue = "ACCELERATING" | "DECELERATING" | "MIXED";
export type MarketHorizonRole = "RAPID" | "PRIMARY" | "PERSISTENCE";
export type ClassifierMetric<T> =
  | { available: true; value: T; reason: null }
  | { available: false; value: null; reason: string };

export type MarketClassifierConfig = {
  version: string;
  directionalBreadth: number;
  materialBreadth: number;
  normalizedMovement: number;
  accelerationBreadth: number;
  isolatedOutlierBreadthDisagreement: number;
};

export type SymbolSourceTimeEvidence = {
  symbol: string;
  lastRealTradeTimeMs: number | null;
  lastRealEventTimeMs: number | null;
  lastReceivedAtMs: number | null;
};

export type IsolatedMarketOutlier = {
  symbol: string;
  direction: "RISING" | "FALLING";
  rawReturn: number;
  historicalZ: number;
  crossSectionalZ: number;
  sameDirectionBreadthCount: number;
  sameDirectionBreadthFraction: number;
  sameDirectionBreadthDenominator: number;
};

export type ReversalCandidate = {
  priorConfirmedEpisodeDirection: ConfirmedMarketDirection;
  currentDirection: ConfirmedMarketDirection;
  directionalBreadth: BreadthSide;
  materialBreadth: BreadthSide;
  medianRawReturn: number;
  medianNormalizedMovement: number;
};

export type MarketClassificationWindow = {
  windowMinutes: MovementWindowMinutes;
  horizonRole: MarketHorizonRole;
  isPrimary: boolean;
  directionState: MarketDirectionState;
  pace: ClassifierMetric<MarketPaceValue>;
  priorConfirmedEpisodeDirection: ConfirmedMarketDirection | null;
  reversalCandidate: ReversalCandidate | null;
  isolatedOutliers: IsolatedMarketOutlier[];
  breadth: MarketMovementWindowResult["breadth"];
  medianRawReturn: Metric;
  medianNormalizedMovement: Metric;
  medianAcceleration: ClassifierMetric<number>;
  positiveAccelerationBreadth: ClassifierMetric<BreadthSide>;
  negativeAccelerationBreadth: ClassifierMetric<BreadthSide>;
  dispersionMadNormalizedMovement: Metric;
  trimmedMeanNormalizedMovement: Metric;
  liquidityWeightedNormalizedMovement: Metric;
  liquidityWeights: ClassifierMetric<Array<[string, number]>>;
  volumeContext: Array<{ symbol: string; currentNotionalVolume: Metric; rvol: Metric }>;
  marketWideEligible: boolean;
  eligibleCount: number;
  eligibleFraction: number;
  configuredUniverse: string[];
  includedSymbols: string[];
  excludedSymbols: Array<{ symbol: string; reasons: SymbolExclusionReason[] }>;
  availabilityReasons: string[];
  classifierAlgorithmVersion: typeof MARKET_STATE_CLASSIFIER_VERSION;
  classifierConfigVersion: string;
  movementAlgorithmVersion: string;
  movementConfigVersion: string;
  universeId: string;
  universeVersion: string;
  provider: "binance-usdm";
  exchange: "binance";
  priceType: "trade";
  evaluationBoundaryTime: number;
  sourceTimeEvidence: SymbolSourceTimeEvidence[];
  movementSnapshot: MarketMovementWindowResult;
};

export type MarketClassification = {
  classifierAlgorithmVersion: typeof MARKET_STATE_CLASSIFIER_VERSION;
  classifierConfigVersion: string;
  classifierConfig: MarketClassifierConfig;
  movementAlgorithmVersion: string;
  movementConfigVersion: string;
  universeId: string;
  universeVersion: string;
  provider: "binance-usdm";
  exchange: "binance";
  priceType: "trade";
  evaluationBoundaryTime: number;
  primaryWindowMinutes: 5;
  windows: MarketClassificationWindow[];
};
