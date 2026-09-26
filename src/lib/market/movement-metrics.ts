import {
  MOVEMENT_BUCKET_MS,
  MOVEMENT_WINDOWS_MINUTES,
  type MovementBucket,
  type MovementBucketSnapshot,
  type MovementWindowMinutes,
} from "./movement-contract";
import type { CollectorHealthStatus } from "../operational/types";

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

export type HistoricalMovementWindowInput = {
  /** Returns selected by the caller from the configured historical lookback. */
  returns: readonly number[];
  /** Duration for which the caller has verified usable historical coverage. */
  usableCoverageMs: number;
  /** Oldest to newest comparable, completed-window notional volumes. */
  previousNotionalVolumes: readonly number[];
};

export type MarketMovementSymbolInput = {
  symbol: string;
  sourceStatus: CollectorHealthStatus;
  instrumentCompatible: boolean;
  snapshot: MovementBucketSnapshot | null;
  historical: Partial<Record<MovementWindowMinutes, HistoricalMovementWindowInput>>;
};

export type CalculateMarketMovementInput = {
  evaluationBoundaryTime: number;
  universe: {
    id: string;
    version: string;
    symbols: readonly string[];
  };
  symbols: readonly MarketMovementSymbolInput[];
  config?: MarketMovementConfig;
};

export type SymbolExclusionReason =
  | "MISSING_SYMBOL_INPUT"
  | "SOURCE_RECOVERING"
  | "SOURCE_STALE"
  | "SOURCE_UNAVAILABLE"
  | "UNSUPPORTED_INSTRUMENT"
  | "WARMING_INSUFFICIENT_LIVE_HISTORY"
  | "STALE_LAST_TRADE"
  | "MISSING_EXACT_BOUNDARY"
  | "MOVEMENT_HISTORY_UNAVAILABLE"
  | "INVALID_ENDPOINT_PRICE"
  | "INSUFFICIENT_NORMALIZATION_HISTORY"
  | "INVALID_NORMALIZATION_HISTORY"
  | "NORMALIZATION_MAD_UNAVAILABLE";

export type MetricUnavailableReason =
  | SymbolExclusionReason
  | "SYMBOL_EXCLUDED"
  | "MARKET_UNIVERSE_INELIGIBLE"
  | "TOO_FEW_VALUES_TO_TRIM"
  | "LIQUIDITY_WEIGHT_CAP_UNSATISFIABLE"
  | "CURRENT_NOTIONAL_UNAVAILABLE"
  | "RVOL_HISTORY_UNAVAILABLE"
  | "RVOL_DENOMINATOR_INVALID"
  | "CROSS_SECTIONAL_MAD_UNAVAILABLE";

export type Metric =
  | { available: true; value: number }
  | { available: false; value: null; reason: MetricUnavailableReason };

export type SymbolDirection = "FLAT" | "RISING" | "FALLING";

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
    flatCount: number;
    risingCount: number;
    fallingCount: number;
    materialRisingCount: number;
    materialFallingCount: number;
    flatFraction: number;
    risingFraction: number;
    fallingFraction: number;
    materialRisingFraction: number;
    materialFallingFraction: number;
  };
  aggregates: {
    medianNormalizedMovement: Metric;
    medianRawReturn: Metric;
    trimmedMeanNormalizedMovement: Metric;
    liquidityWeightedNormalizedMovement: Metric;
    liquidityWeights: Array<{ symbol: string; weight: number }>;
    dispersionMadNormalizedMovement: Metric;
  };
};

export type MarketMovementEvaluation = {
  algorithmVersion: typeof MARKET_MOVEMENT_ALGORITHM_VERSION;
  configVersion: string;
  universeId: string;
  universeVersion: string;
  provider: "binance-usdm";
  exchange: "binance";
  priceType: "trade";
  evaluationBoundaryTime: number;
  windows: MarketMovementWindowResult[];
};

type WorkingSymbol = SymbolMovementMetrics & {
  rawCurrentReturn: number | null;
  rawNormalizedZ: number | null;
  rawCurrentNotional: number | null;
};

function available(value: number): Metric {
  return { available: true, value };
}

function unavailable(reason: MetricUnavailableReason): Metric {
  return { available: false, value: null, reason };
}

function median(values: readonly number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  if (sorted.length % 2 === 1) return sorted[middle]!;
  return (sorted[middle - 1]! + sorted[middle]!) / 2;
}

function mad(values: readonly number[], center = median(values)): number | null {
  if (center === null) return null;
  return median(values.map((value) => Math.abs(value - center)));
}

function addReason(reasons: SymbolExclusionReason[], reason: SymbolExclusionReason): void {
  if (!reasons.includes(reason)) reasons.push(reason);
}

function exactBucket(
  snapshot: MovementBucketSnapshot,
  boundaryTime: number,
): MovementBucket | null {
  return snapshot.buckets.find((bucket) => bucket.boundaryTime === boundaryTime) ?? null;
}

function canonicalBucket(bucket: MovementBucket, symbol: string): boolean {
  return (
    bucket.provider === "binance-usdm" &&
    bucket.instrumentId === `binance-usdm:${symbol}` &&
    bucket.nativeSymbol === symbol &&
    bucket.symbol === symbol &&
    bucket.marketType === "futures" &&
    bucket.contractType === "perpetual" &&
    bucket.priceType === "trade"
  );
}

function currentNotionalVolume(
  snapshot: MovementBucketSnapshot,
  startBoundary: number,
  endBoundary: number,
): number | null {
  const byBoundary = new Map(snapshot.buckets.map((bucket) => [bucket.boundaryTime, bucket]));
  let total = 0;
  for (
    let boundary = startBoundary + snapshot.bucketMs;
    boundary <= endBoundary;
    boundary += snapshot.bucketMs
  ) {
    const bucket = byBoundary.get(boundary);
    if (!bucket || !Number.isFinite(bucket.quoteVolume) || bucket.quoteVolume < 0) return null;
    total += bucket.quoteVolume;
  }
  return total;
}

function firstReason(reasons: readonly SymbolExclusionReason[]): MetricUnavailableReason {
  return reasons[0] ?? "SYMBOL_EXCLUDED";
}

function emptyWorkingSymbol(
  symbol: string,
  windowMinutes: MovementWindowMinutes,
  evaluationBoundaryTime: number,
  instrumentId: string | null,
): WorkingSymbol {
  return {
    symbol,
    instrumentId,
    provider: "binance-usdm",
    exchange: "binance",
    priceType: "trade",
    windowMinutes,
    evaluationBoundaryTime,
    included: false,
    exclusionReasons: [],
    currentReturn: unavailable("SYMBOL_EXCLUDED"),
    previousReturn: unavailable("SYMBOL_EXCLUDED"),
    velocity: unavailable("SYMBOL_EXCLUDED"),
    previousVelocity: unavailable("SYMBOL_EXCLUDED"),
    acceleration: unavailable("SYMBOL_EXCLUDED"),
    historicalMedian: unavailable("SYMBOL_EXCLUDED"),
    historicalMad: unavailable("SYMBOL_EXCLUDED"),
    normalizedZ: unavailable("SYMBOL_EXCLUDED"),
    direction: null,
    materialRising: false,
    materialFalling: false,
    currentNotionalVolume: unavailable("CURRENT_NOTIONAL_UNAVAILABLE"),
    rvol: unavailable("RVOL_HISTORY_UNAVAILABLE"),
    crossSectionalZ: unavailable("SYMBOL_EXCLUDED"),
    outlierCandidate: false,
    rawCurrentReturn: null,
    rawNormalizedZ: null,
    rawCurrentNotional: null,
  };
}

function calculateSymbol(
  symbol: string,
  input: MarketMovementSymbolInput | undefined,
  windowMinutes: MovementWindowMinutes,
  evaluationBoundaryTime: number,
  config: MarketMovementConfig,
): WorkingSymbol {
  const result = emptyWorkingSymbol(
    symbol,
    windowMinutes,
    evaluationBoundaryTime,
    input?.snapshot?.instrumentId ?? null,
  );
  if (!input) {
    addReason(result.exclusionReasons, "MISSING_SYMBOL_INPUT");
    result.currentReturn = unavailable("MISSING_SYMBOL_INPUT");
    return result;
  }
  if (input.sourceStatus !== "LIVE") {
    addReason(result.exclusionReasons, `SOURCE_${input.sourceStatus}`);
  }
  if (!input.instrumentCompatible || !input.snapshot) {
    addReason(
      result.exclusionReasons,
      input.snapshot ? "UNSUPPORTED_INSTRUMENT" : "MISSING_SYMBOL_INPUT",
    );
  }
  const snapshot = input.snapshot;
  if (!snapshot) {
    const reason = firstReason(result.exclusionReasons);
    result.currentReturn = unavailable(reason);
    return result;
  }
  if (
    snapshot.symbol !== symbol ||
    snapshot.provider !== "binance-usdm" ||
    snapshot.instrumentId !== `binance-usdm:${symbol}` ||
    snapshot.priceType !== "trade" ||
    snapshot.bucketMs !== MOVEMENT_BUCKET_MS
  ) {
    addReason(result.exclusionReasons, "UNSUPPORTED_INSTRUMENT");
  }

  const readiness = snapshot.readiness[windowMinutes];
  switch (readiness.state) {
    case "warming":
      addReason(result.exclusionReasons, "WARMING_INSUFFICIENT_LIVE_HISTORY");
      break;
    case "stale":
      if (readiness.reason === "collector_stale") {
        addReason(result.exclusionReasons, "SOURCE_STALE");
      } else if (readiness.reason === "last_real_trade_expired") {
        addReason(result.exclusionReasons, "STALE_LAST_TRADE");
      } else {
        addReason(result.exclusionReasons, "MOVEMENT_HISTORY_UNAVAILABLE");
      }
      break;
    case "missing_history":
      addReason(result.exclusionReasons, "MISSING_EXACT_BOUNDARY");
      break;
    case "unavailable":
      if (readiness.reason === "collector_recovering") {
        addReason(result.exclusionReasons, "SOURCE_RECOVERING");
      } else if (readiness.reason === "collector_unavailable" ||
             readiness.reason === "source_unavailable_in_required_history") {
        addReason(result.exclusionReasons, "SOURCE_UNAVAILABLE");
      } else {
        addReason(result.exclusionReasons, "MOVEMENT_HISTORY_UNAVAILABLE");
      }
      break;
  }

  const windowMs = windowMinutes * 60_000;
  const current = exactBucket(snapshot, evaluationBoundaryTime);
  const previous = exactBucket(snapshot, evaluationBoundaryTime - windowMs);
  const beforePrevious = exactBucket(snapshot, evaluationBoundaryTime - 2 * windowMs);
  if (!current || !previous || !beforePrevious) {
    addReason(result.exclusionReasons, "MISSING_EXACT_BOUNDARY");
  }
  if (
    current &&
    (current.lastRealTradeTime === null ||
      evaluationBoundaryTime - current.lastRealTradeTime < 0 ||
      evaluationBoundaryTime - current.lastRealTradeTime > snapshot.maxLastTradeAgeMs)
  ) {
    addReason(result.exclusionReasons, "STALE_LAST_TRADE");
  }
  const requiredBuckets = [current, previous, beforePrevious];
  if (
    requiredBuckets.some(
      (bucket) =>
        bucket !== null &&
        (!canonicalBucket(bucket, symbol) || bucket.instrumentId !== snapshot.instrumentId),
    )
  ) {
    addReason(result.exclusionReasons, "UNSUPPORTED_INSTRUMENT");
  }
  if (
    requiredBuckets.some(
      (bucket) =>
        bucket !== null &&
        (bucket.endpointPrice === null ||
          !Number.isFinite(bucket.endpointPrice) ||
          bucket.endpointPrice <= 0),
    )
  ) {
    addReason(result.exclusionReasons, "INVALID_ENDPOINT_PRICE");
  }

  if (
    current?.endpointPrice !== null &&
    current?.endpointPrice !== undefined &&
    previous?.endpointPrice !== null &&
    previous?.endpointPrice !== undefined &&
    beforePrevious?.endpointPrice !== null &&
    beforePrevious?.endpointPrice !== undefined &&
    [current.endpointPrice, previous.endpointPrice, beforePrevious.endpointPrice].every(
      (price) => Number.isFinite(price) && price > 0,
    )
  ) {
    const currentReturn = Math.log(current.endpointPrice / previous.endpointPrice);
    const previousReturn = Math.log(previous.endpointPrice / beforePrevious.endpointPrice);
    const durationSeconds = windowMinutes * 60;
    const velocity = currentReturn / durationSeconds;
    const previousVelocity = previousReturn / durationSeconds;
    result.rawCurrentReturn = currentReturn;
    result.currentReturn = available(currentReturn);
    result.previousReturn = available(previousReturn);
    result.velocity = available(velocity);
    result.previousVelocity = available(previousVelocity);
    result.acceleration = available((velocity - previousVelocity) / durationSeconds);
  }

  const notional = currentNotionalVolume(
    snapshot,
    evaluationBoundaryTime - windowMs,
    evaluationBoundaryTime,
  );
  if (notional !== null) {
    result.rawCurrentNotional = notional;
    result.currentNotionalVolume = available(notional);
  }

  const historical = input.historical[windowMinutes];
  if (
    !historical ||
    !Number.isFinite(historical.usableCoverageMs) ||
    historical.usableCoverageMs < config.minimumHistoricalCoverageMs ||
    historical.returns.length === 0
  ) {
    addReason(result.exclusionReasons, "INSUFFICIENT_NORMALIZATION_HISTORY");
  } else if (historical.returns.some((value) => !Number.isFinite(value))) {
    addReason(result.exclusionReasons, "INVALID_NORMALIZATION_HISTORY");
  } else {
    const historicalMedian = median(historical.returns)!;
    const historicalMad = mad(historical.returns, historicalMedian)!;
    result.historicalMedian = available(historicalMedian);
    if (!Number.isFinite(historicalMad) || historicalMad <= 0) {
      addReason(result.exclusionReasons, "NORMALIZATION_MAD_UNAVAILABLE");
      result.historicalMad = unavailable("NORMALIZATION_MAD_UNAVAILABLE");
    } else {
      result.historicalMad = available(historicalMad);
      if (result.rawCurrentReturn !== null) {
        const normalizedZ = (0.6745 * (result.rawCurrentReturn - historicalMedian)) / historicalMad;
        result.rawNormalizedZ = normalizedZ;
        result.normalizedZ = available(normalizedZ);
        if (Math.abs(normalizedZ) < config.flatZ) result.direction = "FLAT";
        else if (result.rawCurrentReturn > 0) result.direction = "RISING";
        else if (result.rawCurrentReturn < 0) result.direction = "FALLING";
        else result.direction = null;
        result.materialRising =
          result.rawCurrentReturn > 0 && Math.abs(normalizedZ) >= config.materialZ;
        result.materialFalling =
          result.rawCurrentReturn < 0 && Math.abs(normalizedZ) >= config.materialZ;
      }
    }
  }

  if (result.rawCurrentNotional === null) {
    result.rvol = unavailable("CURRENT_NOTIONAL_UNAVAILABLE");
  } else if (historical) {
    const volumes = historical.previousNotionalVolumes.slice(-config.rvolComparisonWindows);
    if (
      volumes.length === config.rvolComparisonWindows &&
      volumes.every((value) => Number.isFinite(value) && value >= 0)
    ) {
      const denominator = median(volumes);
      result.rvol =
        denominator !== null && denominator > 0
          ? available(result.rawCurrentNotional / denominator)
          : unavailable("RVOL_DENOMINATOR_INVALID");
    }
  }

  result.included = result.exclusionReasons.length === 0 && result.rawNormalizedZ !== null;
  if (!result.included) {
    const reason = firstReason(result.exclusionReasons);
    if (!result.currentReturn.available && result.rawCurrentReturn === null) {
      result.currentReturn = unavailable(reason);
      result.previousReturn = unavailable(reason);
      result.velocity = unavailable(reason);
      result.previousVelocity = unavailable(reason);
      result.acceleration = unavailable(reason);
    }
    if (!result.normalizedZ.available) result.normalizedZ = unavailable(reason);
  }
  return result;
}

function trimmedMean(values: readonly number[], fraction: number): number | null {
  const trimCount = Math.floor(values.length * fraction);
  if (trimCount < 1 || trimCount * 2 >= values.length) return null;
  const sorted = [...values].sort((left, right) => left - right);
  const retained = sorted.slice(trimCount, sorted.length - trimCount);
  return retained.reduce((sum, value) => sum + value, 0) / retained.length;
}

function cappedLiquidityWeights(
  values: readonly WorkingSymbol[],
  cap: number,
): Array<{ symbol: string; weight: number }> | null {
  const raw = values.map((value) => ({
    symbol: value.symbol,
    weight: Math.sqrt(value.rawCurrentNotional ?? 0),
  }));
  const positive = raw.filter((entry) => Number.isFinite(entry.weight) && entry.weight > 0);
  if (positive.length * cap < 1 - Number.EPSILON) return null;

  const weights = new Map(raw.map((entry) => [entry.symbol, 0]));
  let active = positive;
  let remaining = 1;
  while (active.length > 0 && remaining > Number.EPSILON) {
    const totalRaw = active.reduce((sum, entry) => sum + entry.weight, 0);
    const capped = active.filter((entry) => (remaining * entry.weight) / totalRaw > cap);
    if (capped.length === 0) {
      for (const entry of active) {
        weights.set(entry.symbol, (remaining * entry.weight) / totalRaw);
      }
      remaining = 0;
      break;
    }
    for (const entry of capped) weights.set(entry.symbol, cap);
    remaining -= capped.length * cap;
    const cappedSymbols = new Set(capped.map((entry) => entry.symbol));
    active = active.filter((entry) => !cappedSymbols.has(entry.symbol));
  }
  if (remaining > 1e-10) return null;
  return raw.map((entry) => ({ symbol: entry.symbol, weight: weights.get(entry.symbol) ?? 0 }));
}

function calculateWindow(
  configuredUniverse: string[],
  inputBySymbol: Map<string, MarketMovementSymbolInput>,
  input: CalculateMarketMovementInput,
  config: MarketMovementConfig,
  windowMinutes: MovementWindowMinutes,
): MarketMovementWindowResult {
  const working = configuredUniverse.map((symbol) =>
    calculateSymbol(
      symbol,
      inputBySymbol.get(symbol),
      windowMinutes,
      input.evaluationBoundaryTime,
      config,
    ),
  );
  const included = working.filter((value) => value.included);
  const eligibleCount = included.length;
  const eligibleFraction =
    configuredUniverse.length === 0 ? 0 : eligibleCount / configuredUniverse.length;
  const marketWideEligible =
    eligibleCount >= config.minimumEligibleCount &&
    eligibleFraction >= config.minimumEligibleFraction;
  const gateReason = "MARKET_UNIVERSE_INELIGIBLE" as const;
  const normalizedValues = included.map((value) => value.rawNormalizedZ!);
  const rawReturns = included.map((value) => value.rawCurrentReturn!);

  let medianNormalizedMovement: Metric = unavailable(gateReason);
  let medianRawReturn: Metric = unavailable(gateReason);
  let trimmedMeanNormalizedMovement: Metric = unavailable(gateReason);
  let liquidityWeightedNormalizedMovement: Metric = unavailable(gateReason);
  let dispersionMadNormalizedMovement: Metric = unavailable(gateReason);
  let liquidityWeights: Array<{ symbol: string; weight: number }> = [];
  if (marketWideEligible) {
    medianNormalizedMovement = available(median(normalizedValues)!);
    medianRawReturn = available(median(rawReturns)!);
    const trimmed = trimmedMean(normalizedValues, config.trimFraction);
    trimmedMeanNormalizedMovement =
      trimmed === null ? unavailable("TOO_FEW_VALUES_TO_TRIM") : available(trimmed);
    const weights = cappedLiquidityWeights(included, config.liquidityWeightCap);
    if (weights) {
      liquidityWeights = weights;
      const bySymbol = new Map(included.map((value) => [value.symbol, value.rawNormalizedZ!]));
      liquidityWeightedNormalizedMovement = available(
        weights.reduce((sum, item) => sum + item.weight * bySymbol.get(item.symbol)!, 0),
      );
    } else {
      liquidityWeightedNormalizedMovement = unavailable("LIQUIDITY_WEIGHT_CAP_UNSATISFIABLE");
    }
    dispersionMadNormalizedMovement = available(mad(normalizedValues)!);

    const crossMedian = median(rawReturns)!;
    const crossMad = mad(rawReturns, crossMedian)!;
    for (const symbol of included) {
      if (crossMad > 0 && Number.isFinite(crossMad)) {
        const crossZ = (0.6745 * (symbol.rawCurrentReturn! - crossMedian)) / crossMad;
        symbol.crossSectionalZ = available(crossZ);
        symbol.outlierCandidate =
          Math.abs(crossZ) >= config.outlierCrossZ &&
          Math.abs(symbol.rawNormalizedZ!) >= config.outlierHistoricalZ;
      } else {
        symbol.crossSectionalZ = unavailable("CROSS_SECTIONAL_MAD_UNAVAILABLE");
      }
    }
  } else {
    for (const symbol of included) {
      symbol.crossSectionalZ = unavailable(gateReason);
    }
  }

  const count = (predicate: (value: WorkingSymbol) => boolean) => included.filter(predicate).length;
  const ratio = (value: number) => (eligibleCount === 0 ? 0 : value / eligibleCount);
  const flatCount = count((value) => value.direction === "FLAT");
  const risingCount = count((value) => value.direction === "RISING");
  const fallingCount = count((value) => value.direction === "FALLING");
  const materialRisingCount = count((value) => value.materialRising);
  const materialFallingCount = count((value) => value.materialFalling);

  const symbols: SymbolMovementMetrics[] = working.map(
    ({ rawCurrentReturn: _return, rawNormalizedZ: _z, rawCurrentNotional: _notional, ...value }) =>
      value,
  );
  return {
    algorithmVersion: MARKET_MOVEMENT_ALGORITHM_VERSION,
    configVersion: config.version,
    universeId: input.universe.id,
    universeVersion: input.universe.version,
    configuredUniverse: [...configuredUniverse],
    includedSymbols: included.map((value) => value.symbol),
    excludedSymbols: working
      .filter((value) => !value.included)
      .map((value) => ({ symbol: value.symbol, reasons: [...value.exclusionReasons] })),
    windowMinutes,
    provider: "binance-usdm",
    exchange: "binance",
    priceType: "trade",
    evaluationBoundaryTime: input.evaluationBoundaryTime,
    historicalLookbackMs: config.historicalLookbackMs,
    minimumHistoricalCoverageMs: config.minimumHistoricalCoverageMs,
    marketWideEligible,
    eligibleCount,
    eligibleFraction,
    symbols,
    breadth: {
      available: marketWideEligible,
      unavailableReason: marketWideEligible ? null : gateReason,
      denominator: eligibleCount,
      flatCount,
      risingCount,
      fallingCount,
      materialRisingCount,
      materialFallingCount,
      flatFraction: ratio(flatCount),
      risingFraction: ratio(risingCount),
      fallingFraction: ratio(fallingCount),
      materialRisingFraction: ratio(materialRisingCount),
      materialFallingFraction: ratio(materialFallingCount),
    },
    aggregates: {
      medianNormalizedMovement,
      medianRawReturn,
      trimmedMeanNormalizedMovement,
      liquidityWeightedNormalizedMovement,
      liquidityWeights,
      dispersionMadNormalizedMovement,
    },
  };
}

function validateConfig(config: MarketMovementConfig): void {
  if (!config.version) throw new Error("movement config version is required");
  const positive = [
    config.historicalLookbackMs,
    config.minimumHistoricalCoverageMs,
    config.flatZ,
    config.materialZ,
    config.trimFraction,
    config.liquidityWeightCap,
    config.rvolComparisonWindows,
    config.outlierCrossZ,
    config.outlierHistoricalZ,
    config.minimumEligibleFraction,
    config.minimumEligibleCount,
  ];
  if (positive.some((value) => !Number.isFinite(value) || value <= 0)) {
    throw new Error("movement config values must be positive and finite");
  }
  if (config.minimumHistoricalCoverageMs > config.historicalLookbackMs) {
    throw new Error("minimum historical coverage exceeds lookback");
  }
  if (config.materialZ < config.flatZ) {
    throw new Error("material Z threshold must be at least the flat Z threshold");
  }
  if (config.trimFraction >= 0.5) throw new Error("trim fraction must be below 0.5");
  if (config.liquidityWeightCap > 1) throw new Error("liquidity weight cap exceeds 1");
  if (config.minimumEligibleFraction > 1) {
    throw new Error("minimum eligible fraction exceeds 1");
  }
  if (!Number.isSafeInteger(config.rvolComparisonWindows)) {
    throw new Error("RVOL comparison windows must be an integer");
  }
  if (!Number.isSafeInteger(config.minimumEligibleCount)) {
    throw new Error("minimum eligible count must be an integer");
  }
}

/** Pure calculation entrypoint shared by future live evaluation and historical replay. */
export function calculateMarketMovement(
  input: CalculateMarketMovementInput,
): MarketMovementEvaluation {
  const config = input.config ?? DEFAULT_MARKET_MOVEMENT_CONFIG;
  validateConfig(config);
  if (
    !Number.isSafeInteger(input.evaluationBoundaryTime) ||
    input.evaluationBoundaryTime < 0 ||
    input.evaluationBoundaryTime % MOVEMENT_BUCKET_MS !== 0
  ) {
    throw new Error("movement evaluation time must be an aligned exchange boundary");
  }
  if (!input.universe.id || !input.universe.version) {
    throw new Error("movement universe ID and version are required");
  }
  const configuredUniverse = input.universe.symbols.map((symbol) => symbol.toUpperCase());
  if (new Set(configuredUniverse).size !== configuredUniverse.length) {
    throw new Error("movement universe contains duplicate symbols");
  }
  const inputBySymbol = new Map<string, MarketMovementSymbolInput>();
  for (const symbolInput of input.symbols) {
    const symbol = symbolInput.symbol.toUpperCase();
    if (inputBySymbol.has(symbol)) throw new Error("duplicate movement symbol input");
    inputBySymbol.set(symbol, symbolInput);
  }
  return {
    algorithmVersion: MARKET_MOVEMENT_ALGORITHM_VERSION,
    configVersion: config.version,
    universeId: input.universe.id,
    universeVersion: input.universe.version,
    provider: "binance-usdm",
    exchange: "binance",
    priceType: "trade",
    evaluationBoundaryTime: input.evaluationBoundaryTime,
    windows: MOVEMENT_WINDOWS_MINUTES.map((windowMinutes) =>
      calculateWindow(configuredUniverse, inputBySymbol, input, config, windowMinutes),
    ),
  };
}
