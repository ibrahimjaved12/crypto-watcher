/** Transport and orchestration contract for canonical Python #71 results. */
import type { MovementWindowMinutes } from "./movement-contract";

export const MARKET_MOVEMENT_ALGORITHM_VERSION = "market-movement-v1";
const DAY_MS = 24 * 60 * 60 * 1_000;

export type MarketMovementConfig = {
  version: string;
  historicalLookbackMs: number;
  minimumHistoricalCoverageMs: number;
  flatZ: number;
  materialZ: number;
  trimFraction: number;
  liquidityWeightCap: number;
  rvolComparisonWindows: number;
  outlierCrossZ: number;
  outlierHistoricalZ: number;
  minimumEligibleFraction: number;
  minimumEligibleCount: number;
};

export const DEFAULT_MARKET_MOVEMENT_CONFIG: Readonly<MarketMovementConfig> = {
  version: "market-movement-config-v1",
  historicalLookbackMs: 7 * DAY_MS,
  minimumHistoricalCoverageMs: 3 * DAY_MS,
  flatZ: 0.5,
  materialZ: 1,
  trimFraction: 0.1,
  liquidityWeightCap: 0.25,
  rvolComparisonWindows: 20,
  outlierCrossZ: 3.5,
  outlierHistoricalZ: 1.5,
  minimumEligibleFraction: 0.6,
  minimumEligibleCount: 5,
};

export type SymbolExclusionReason =
  | "MISSING_SYMBOL_INPUT" | "SOURCE_RECOVERING" | "SOURCE_STALE"
  | "SOURCE_UNAVAILABLE" | "UNSUPPORTED_INSTRUMENT"
  | "WARMING_INSUFFICIENT_LIVE_HISTORY" | "STALE_LAST_TRADE"
  | "MISSING_EXACT_BOUNDARY" | "MOVEMENT_HISTORY_UNAVAILABLE"
  | "INVALID_ENDPOINT_PRICE" | "INSUFFICIENT_NORMALIZATION_HISTORY"
  | "INVALID_NORMALIZATION_HISTORY" | "NORMALIZATION_MAD_UNAVAILABLE";

export type MetricUnavailableReason =
  | SymbolExclusionReason | "SYMBOL_EXCLUDED" | "MARKET_UNIVERSE_INELIGIBLE"
  | "TOO_FEW_VALUES_TO_TRIM" | "LIQUIDITY_WEIGHT_CAP_UNSATISFIABLE"
  | "CURRENT_NOTIONAL_UNAVAILABLE" | "RVOL_HISTORY_UNAVAILABLE"
  | "RVOL_DENOMINATOR_INVALID" | "CROSS_SECTIONAL_MAD_UNAVAILABLE"
  | "NUMERIC_RESULT_UNAVAILABLE";

export type Metric<T = number> =
  | { available: true; value: T }
  | { available: false; value: null; reason: MetricUnavailableReason };
export type SymbolDirection = "FLAT" | "RISING" | "FALLING";
export type BreadthSide = { count: number; fraction: number };

export type SymbolMovementMetrics = {
  symbol: string;
  instrumentId: string | null;
  provider: "binance-usdm";
  exchange: "binance";
  priceType: "trade";
  windowMinutes: MovementWindowMinutes;
  evaluationBoundaryTime: number;
  included: boolean;
  exclusionReasons: SymbolExclusionReason[];
  currentReturn: Metric;
  previousReturn: Metric;
  velocity: Metric;
  previousVelocity: Metric;
  acceleration: Metric;
  historicalMedian: Metric;
  historicalMad: Metric;
  normalizedZ: Metric;
  direction: SymbolDirection | null;
  directionAvailability: Metric<SymbolDirection>;
  materialRising: boolean;
  materialFalling: boolean;
  currentNotionalVolume: Metric;
  rvol: Metric;
  crossSectionalZ: Metric;
  outlierCandidate: boolean;
};

export type MarketMovementWindowResult = {
  algorithmVersion: typeof MARKET_MOVEMENT_ALGORITHM_VERSION;
  configVersion: string;
  universeId: string;
  universeVersion: string;
  configuredUniverse: string[];
  includedSymbols: string[];
  excludedSymbols: Array<{ symbol: string; reasons: SymbolExclusionReason[] }>;
  windowMinutes: MovementWindowMinutes;
  provider: "binance-usdm";
  exchange: "binance";
  priceType: "trade";
  evaluationBoundaryTime: number;
  historicalLookbackMs: number;
  minimumHistoricalCoverageMs: number;
  marketWideEligible: boolean;
  eligibleCount: number;
  eligibleFraction: number;
  symbols: SymbolMovementMetrics[];
  breadth: {
    available: boolean;
    unavailableReason: "MARKET_UNIVERSE_INELIGIBLE" | null;
    denominator: number;
    flat: Metric<BreadthSide>;
    rising: Metric<BreadthSide>;
    falling: Metric<BreadthSide>;
    materialRising: Metric<BreadthSide>;
    materialFalling: Metric<BreadthSide>;
    flatCount: number | null;
    risingCount: number | null;
    fallingCount: number | null;
    materialRisingCount: number | null;
    materialFallingCount: number | null;
    flatFraction: number | null;
    risingFraction: number | null;
    fallingFraction: number | null;
    materialRisingFraction: number | null;
    materialFallingFraction: number | null;
  };
  aggregates: {
    medianNormalizedMovement: Metric;
    medianRawReturn: Metric;
    trimmedMeanNormalizedMovement: Metric;
    liquidityWeightedNormalizedMovement: Metric;
    liquidityWeights: Metric<Array<{ symbol: string; weight: number }>>;
    dispersionMadNormalizedMovement: Metric;
  };
};

export type MarketMovementEvaluation = {
  algorithmVersion: typeof MARKET_MOVEMENT_ALGORITHM_VERSION;
  configVersion: string;
  universeId: string;
  universeVersion: string;
  configuredUniverse: string[];
  provider: "binance-usdm";
  exchange: "binance";
  priceType: "trade";
  evaluationBoundaryTime: number;
  historicalLookbackMs: number;
  minimumHistoricalCoverageMs: number;
  windows: MarketMovementWindowResult[];
};
