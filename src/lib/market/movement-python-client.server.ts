import { z } from "zod";
import { pythonServiceConfig } from "../python-service.server";
import type {
  MovementBoundaryResult,
  MovementBoundarySymbolInput,
  MovementBucket,
  MovementBucketSnapshot,
  MovementWindowReadiness,
} from "./movement-contract";
import type { MarketUniverse } from "./market-universe";
import type { MarketMovementConfig, MarketMovementEvaluation } from "./movement-metrics-contract";
import type { MovementNormalizationHistory } from "./movement-normalization-input";

const TIMEOUT_MS = 6_000;
const MAX_ATTEMPTS = 2;
const MAX_RESPONSE_BYTES = 4_000_000;
const timestamp = z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER).nullable();
const bucket = z
  .object({
    boundaryTime: z.number().int().nonnegative(),
    sourceState: z.enum(["LIVE", "RECOVERING", "STALE", "UNAVAILABLE"]),
    endpointPrice: z.number().finite().positive().nullable(),
    baseQuantity: z.number().finite().nonnegative(),
    quoteVolume: z.number().finite().nonnegative(),
    tradeCount: z.number().int().nonnegative(),
    lastRealTradeTime: timestamp,
    lastRealEventTime: timestamp,
    lastRealReceivedAt: timestamp,
    carriedForward: z.boolean(),
    provider: z.literal("binance-usdm"),
    instrumentId: z.string().min(3),
    nativeSymbol: z.string().min(2),
    symbol: z.string().min(2),
    marketType: z.literal("futures"),
    contractType: z.literal("perpetual"),
    priceType: z.literal("trade"),
  })
  .strict();
const readiness = z
  .object({
    windowMinutes: z.union([z.literal(1), z.literal(5), z.literal(15)]),
    status: z.enum(["READY", "WARMING", "STALE"]),
    state: z.enum(["ready", "warming", "stale", "missing_history", "unavailable"]),
    reason: z.string().max(80).nullable(),
  })
  .strict();
const snapshot = z
  .object({
    symbol: z.string().min(2),
    provider: z.literal("binance-usdm"),
    instrumentId: z.string().min(3),
    priceType: z.literal("trade"),
    bucketMs: z.literal(5_000),
    maxLastTradeAgeMs: z.literal(15_000),
    buckets: z.array(bucket).max(420),
    latestRealTradeTime: timestamp,
    latestRealReceivedAt: timestamp,
    readiness: z.record(z.enum(["1", "5", "15"]), readiness),
  })
  .strict();
const responseSchema = z
  .object({
    sessionId: z.string().uuid(),
    boundaryTime: z.number().int().nonnegative(),
    lateAfterFinalizationCount: z.number().int().nonnegative(),
    snapshots: z.array(snapshot).max(100),
  })
  .strict();

export async function advancePythonMovementBoundary(
  sessionId: string,
  boundaryTime: number,
  symbols: MovementBoundarySymbolInput[],
  env: Record<string, string | undefined> = process.env,
  send: typeof fetch = fetch,
): Promise<MovementBoundaryResult> {
  const config = pythonServiceConfig("/v1/movement/boundary", env);
  const body = JSON.stringify({
    schema_version: 1,
    session_id: sessionId,
    boundary_time_ms: boundaryTime,
    symbols: symbols.map((item) => ({
      symbol: item.symbol,
      instrument_id: `binance-usdm:${item.symbol}`,
      membership_epoch: item.membershipEpoch,
      source_state: item.sourceState,
      observations: item.observations.map((trade) => ({
        price: trade.price,
        quantity: trade.quantity,
        event_time_ms: trade.eventTime,
        trade_time_ms: trade.tradeTime,
        aggregate_trade_id: trade.aggregateId,
        received_at_ms: trade.receivedAt,
      })),
    })),
  });
  let lastFailure = "unavailable";
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    try {
      const response = await send(config.url, {
        method: "POST",
        redirect: "manual",
        signal: controller.signal,
        headers: {
          "Content-Type": "application/json",
          "Cache-Control": "no-store",
          Authorization: `Bearer ${config.token}`,
        },
        body,
      });
      if (!response.ok) {
        await response.body?.cancel().catch(() => undefined);
        if (attempt < MAX_ATTEMPTS && (response.status === 429 || response.status >= 500)) {
          lastFailure = `HTTP ${response.status}`;
          continue;
        }
        throw new Error(`Python movement service failed (${response.status})`);
      }
      const raw = await response.text();
      if (new TextEncoder().encode(raw).byteLength > MAX_RESPONSE_BYTES) {
        throw new Error("Python movement response is oversized");
      }
      let decoded: unknown;
      try {
        decoded = JSON.parse(raw);
      } catch {
        throw new Error("Python movement service returned invalid JSON");
      }
      const parsed = responseSchema.safeParse(decoded);
      if (
        !parsed.success ||
        parsed.data.sessionId !== sessionId ||
        parsed.data.boundaryTime !== boundaryTime ||
        parsed.data.snapshots.length !== symbols.length
      ) {
        throw new Error("Python movement service returned an invalid response");
      }
      for (const [index, item] of symbols.entries()) {
        const result = parsed.data.snapshots[index]!;
        if (result.symbol !== item.symbol || result.instrumentId !== `binance-usdm:${item.symbol}`) {
          throw new Error("Python movement service returned mismatched provenance");
        }
      }
      return {
        snapshots: parsed.data.snapshots as MovementBucketSnapshot[],
        lateAfterFinalizationCount: parsed.data.lateAfterFinalizationCount,
      };
    } catch (error) {
      lastFailure = controller.signal.aborted
        ? "timed out"
        : error instanceof Error
          ? error.message
          : String(error);
      if (attempt === MAX_ATTEMPTS || /invalid|oversized|mismatched|failed \(4\d\d\)/.test(lastFailure)) {
        break;
      }
    } finally {
      clearTimeout(timer);
    }
  }
  throw new Error(`Python movement ${lastFailure}`);
}

export type { MovementBucket, MovementBucketSnapshot, MovementWindowReadiness };

const exclusionReason = z.enum([
  "MISSING_SYMBOL_INPUT", "SOURCE_RECOVERING", "SOURCE_STALE", "SOURCE_UNAVAILABLE",
  "UNSUPPORTED_INSTRUMENT", "WARMING_INSUFFICIENT_LIVE_HISTORY", "STALE_LAST_TRADE",
  "MISSING_EXACT_BOUNDARY", "MOVEMENT_HISTORY_UNAVAILABLE", "INVALID_ENDPOINT_PRICE",
  "INSUFFICIENT_NORMALIZATION_HISTORY", "INVALID_NORMALIZATION_HISTORY",
  "NORMALIZATION_MAD_UNAVAILABLE",
]);
const reason = z.union([exclusionReason, z.enum([
  "SYMBOL_EXCLUDED", "MARKET_UNIVERSE_INELIGIBLE",
  "TOO_FEW_VALUES_TO_TRIM", "LIQUIDITY_WEIGHT_CAP_UNSATISFIABLE",
  "CURRENT_NOTIONAL_UNAVAILABLE", "RVOL_HISTORY_UNAVAILABLE", "RVOL_DENOMINATOR_INVALID",
  "CROSS_SECTIONAL_MAD_UNAVAILABLE", "NUMERIC_RESULT_UNAVAILABLE",
])]);
const number = z.number().finite();
const nonnegativeInteger = z.number().int().nonnegative();
const windowMinutes = z.union([z.literal(1), z.literal(5), z.literal(15)]);
const metric = <T extends z.ZodTypeAny>(value: T) => z.discriminatedUnion("available", [
  z.object({ available: z.literal(true), value, reason: z.null() }).strict(),
  z.object({ available: z.literal(false), value: z.null(), reason }).strict(),
]);
const numericMetric = metric(number);
const directionMetric = metric(z.enum(["FLAT", "RISING", "FALLING"]));
const breadthSide = metric(z.object({ count: nonnegativeInteger, fraction: number }).strict());
const symbolMetrics = z.object({
  symbol: z.string().min(1), instrument_id: z.string().nullable(),
  provider: z.literal("binance-usdm"), exchange: z.literal("binance"),
  price_type: z.literal("trade"), window_minutes: windowMinutes,
  evaluation_boundary_time_ms: nonnegativeInteger, included: z.boolean(),
  exclusion_reasons: z.array(exclusionReason), current_return: numericMetric,
  previous_return: numericMetric, velocity: numericMetric,
  previous_velocity: numericMetric, acceleration: numericMetric,
  historical_median: numericMetric, historical_mad: numericMetric,
  normalized_z: numericMetric, direction: directionMetric,
  material_rising: z.boolean(), material_falling: z.boolean(),
  current_notional_volume: numericMetric, rvol: numericMetric,
  cross_sectional_z: numericMetric, outlier_candidate: z.boolean(),
}).strict();
const windowMetrics = z.object({
  algorithm_version: z.literal("market-movement-v1"), config_version: z.string().min(1),
  universe_id: z.string().min(1), universe_version: z.string().min(1),
  configured_universe: z.array(z.string().min(1)), included_symbols: z.array(z.string().min(1)),
  excluded_symbols: z.array(z.object({symbol: z.string().min(1), reasons: z.array(exclusionReason)}).strict()),
  window_minutes: windowMinutes, provider: z.literal("binance-usdm"),
  exchange: z.literal("binance"), price_type: z.literal("trade"),
  evaluation_boundary_time_ms: nonnegativeInteger,
  historical_lookback_ms: nonnegativeInteger,
  minimum_historical_coverage_ms: nonnegativeInteger,
  market_wide_eligible: z.boolean(), eligible_count: nonnegativeInteger,
  eligible_fraction: number, symbols: z.array(symbolMetrics),
  breadth: z.object({
    available: z.boolean(), reason: z.literal("MARKET_UNIVERSE_INELIGIBLE").nullable(),
    denominator: nonnegativeInteger,
    flat: breadthSide, rising: breadthSide, falling: breadthSide,
    material_rising: breadthSide, material_falling: breadthSide,
  }).strict(),
  aggregates: z.object({
    median_normalized_movement: numericMetric, median_raw_return: numericMetric,
    trimmed_mean_normalized_movement: numericMetric,
    liquidity_weighted_normalized_movement: numericMetric,
    liquidity_weights: metric(z.array(z.tuple([z.string().min(1), number]))),
    dispersion_mad_normalized_movement: numericMetric,
  }).strict(),
}).strict();
const evaluationSchema = z.object({
  algorithm_version: z.literal("market-movement-v1"), config_version: z.string().min(1),
  universe_id: z.string().min(1), universe_version: z.string().min(1),
  configured_universe: z.array(z.string().min(1)),
  provider: z.literal("binance-usdm"), exchange: z.literal("binance"),
  price_type: z.literal("trade"), evaluation_boundary_time_ms: nonnegativeInteger,
  historical_lookback_ms: nonnegativeInteger,
  minimum_historical_coverage_ms: nonnegativeInteger,
  windows: z.record(z.enum(["1", "5", "15"]), windowMetrics),
}).strict();
const historyResponse = z.object({
  schema_version: z.literal(1), session_id: z.string().uuid(),
  history_version: z.string().min(1), universe_id: z.string().min(1),
  universe_version: z.string().min(1),
  as_of_boundary_time_ms: z.number().int().nonnegative().max(4_102_444_800_000),
}).strict();
const metricsResponse = z.object({
  schema_version: z.literal(1), session_id: z.string().uuid(),
  history_version: z.string().min(1), evaluation: evaluationSchema,
}).strict();

async function postCanonicalMovement(
  path: string, body: unknown, env: Record<string, string | undefined>, send: typeof fetch,
): Promise<unknown> {
  const config = pythonServiceConfig(path, env);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const response = await send(config.url, {
      method: "POST", redirect: "manual", signal: controller.signal,
      headers: { "Content-Type": "application/json", "Cache-Control": "no-store",
        Authorization: `Bearer ${config.token}` },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      await response.body?.cancel().catch(() => undefined);
      throw new Error(`Python movement service failed (${response.status})`);
    }
    const raw = await response.text();
    if (new TextEncoder().encode(raw).byteLength > MAX_RESPONSE_BYTES) {
      throw new Error("Python movement response is oversized");
    }
    return JSON.parse(raw) as unknown;
  } finally {
    clearTimeout(timer);
  }
}

export async function registerPythonMovementHistory(
  sessionId: string, historyVersion: string, universe: MarketUniverse,
  config: MarketMovementConfig, historical: MovementNormalizationHistory,
  asOfBoundaryTime: number,
  env: Record<string, string | undefined> = process.env, send: typeof fetch = fetch,
): Promise<void> {
  const response = await postCanonicalMovement("/v1/movement/history", {
    schema_version: 1, session_id: sessionId, history_version: historyVersion,
    universe_id: universe.id, universe_version: universe.version,
    as_of_boundary_time_ms: asOfBoundaryTime,
    symbols: universe.symbols,
    config: {
      version: config.version, historical_lookback_ms: config.historicalLookbackMs,
      minimum_historical_coverage_ms: config.minimumHistoricalCoverageMs,
      flat_z: config.flatZ, material_z: config.materialZ, trim_fraction: config.trimFraction,
      liquidity_weight_cap: config.liquidityWeightCap,
      rvol_comparison_windows: config.rvolComparisonWindows,
      outlier_cross_z: config.outlierCrossZ, outlier_historical_z: config.outlierHistoricalZ,
      minimum_eligible_fraction: config.minimumEligibleFraction,
      minimum_eligible_count: config.minimumEligibleCount,
    },
    historical: universe.symbols.map((symbol) => ({
      symbol,
      windows: Object.fromEntries(Object.entries(historical.get(symbol) ?? {}).map(([window, entry]) =>
        [window, { returns: entry!.returns, usable_coverage_ms: entry!.usableCoverageMs,
          previous_notional_volumes: entry!.previousNotionalVolumes }])),
    })),
  }, env, send);
  const parsed = historyResponse.safeParse(response);
  if (!parsed.success || parsed.data.session_id !== sessionId ||
      parsed.data.history_version !== historyVersion ||
      parsed.data.universe_id !== universe.id || parsed.data.universe_version !== universe.version ||
      parsed.data.as_of_boundary_time_ms !== asOfBoundaryTime) {
    throw new Error("Python movement history response has mismatched provenance");
  }
}

function transportEvaluation(raw: z.infer<typeof evaluationSchema>): MarketMovementEvaluation {
  const windows = ([1, 5, 15] as const).map((minute) => {
    const item = raw.windows[String(minute) as "1" | "5" | "15"];
    if (!item) throw new Error(`Python movement metrics response is missing ${minute}m window`);
    const side = item.breadth;
    const weightMetric = item.aggregates.liquidity_weights;
    const liquidityWeights = weightMetric.available
      ? { available: true as const,
          value: weightMetric.value.map(([symbol, weight]) => ({ symbol, weight })) }
      : { available: false as const, value: null, reason: weightMetric.reason };
    return {
      algorithmVersion: item.algorithm_version, configVersion: item.config_version,
      universeId: item.universe_id, universeVersion: item.universe_version,
      configuredUniverse: item.configured_universe, includedSymbols: item.included_symbols,
      excludedSymbols: item.excluded_symbols, windowMinutes: item.window_minutes,
      provider: item.provider, exchange: item.exchange, priceType: item.price_type,
      evaluationBoundaryTime: item.evaluation_boundary_time_ms,
      historicalLookbackMs: item.historical_lookback_ms,
      minimumHistoricalCoverageMs: item.minimum_historical_coverage_ms,
      marketWideEligible: item.market_wide_eligible, eligibleCount: item.eligible_count,
      eligibleFraction: item.eligible_fraction,
      symbols: item.symbols.map((symbol) => ({
        symbol: symbol.symbol, instrumentId: symbol.instrument_id,
        provider: symbol.provider, exchange: symbol.exchange, priceType: symbol.price_type,
        windowMinutes: symbol.window_minutes,
        evaluationBoundaryTime: symbol.evaluation_boundary_time_ms,
        included: symbol.included, exclusionReasons: symbol.exclusion_reasons,
        currentReturn: symbol.current_return, previousReturn: symbol.previous_return,
        velocity: symbol.velocity, previousVelocity: symbol.previous_velocity,
        acceleration: symbol.acceleration, historicalMedian: symbol.historical_median,
        historicalMad: symbol.historical_mad, normalizedZ: symbol.normalized_z,
        direction: symbol.direction.available ? symbol.direction.value : null,
        directionAvailability: symbol.direction,
        materialRising: symbol.material_rising, materialFalling: symbol.material_falling,
        currentNotionalVolume: symbol.current_notional_volume, rvol: symbol.rvol,
        crossSectionalZ: symbol.cross_sectional_z, outlierCandidate: symbol.outlier_candidate,
      })),
      breadth: {
        available: side.available,
        unavailableReason: side.reason,
        denominator: side.denominator,
        flat: side.flat, rising: side.rising, falling: side.falling,
        materialRising: side.material_rising, materialFalling: side.material_falling,
        flatCount: side.flat.available ? side.flat.value.count : null,
        risingCount: side.rising.available ? side.rising.value.count : null,
        fallingCount: side.falling.available ? side.falling.value.count : null,
        materialRisingCount: side.material_rising.available ? side.material_rising.value.count : null,
        materialFallingCount: side.material_falling.available ? side.material_falling.value.count : null,
        flatFraction: side.flat.available ? side.flat.value.fraction : null,
        risingFraction: side.rising.available ? side.rising.value.fraction : null,
        fallingFraction: side.falling.available ? side.falling.value.fraction : null,
        materialRisingFraction: side.material_rising.available ? side.material_rising.value.fraction : null,
        materialFallingFraction: side.material_falling.available ? side.material_falling.value.fraction : null,
      },
      aggregates: {
        medianNormalizedMovement: item.aggregates.median_normalized_movement,
        medianRawReturn: item.aggregates.median_raw_return,
        trimmedMeanNormalizedMovement: item.aggregates.trimmed_mean_normalized_movement,
        liquidityWeightedNormalizedMovement: item.aggregates.liquidity_weighted_normalized_movement,
        liquidityWeights,
        dispersionMadNormalizedMovement: item.aggregates.dispersion_mad_normalized_movement,
      },
    };
  });
  return {
    algorithmVersion: raw.algorithm_version, configVersion: raw.config_version,
    universeId: raw.universe_id, universeVersion: raw.universe_version,
    configuredUniverse: raw.configured_universe,
    provider: raw.provider, exchange: raw.exchange, priceType: raw.price_type,
    evaluationBoundaryTime: raw.evaluation_boundary_time_ms,
    historicalLookbackMs: raw.historical_lookback_ms,
    minimumHistoricalCoverageMs: raw.minimum_historical_coverage_ms,
    windows,
  };
}

export async function calculatePythonMarketMovement(
  sessionId: string, boundaryTime: number, historyVersion: string,
  universe: MarketUniverse, configVersion: string,
  env: Record<string, string | undefined> = process.env, send: typeof fetch = fetch,
): Promise<MarketMovementEvaluation> {
  const response = await postCanonicalMovement("/v1/movement/metrics", {
    schema_version: 1, session_id: sessionId,
    evaluation_boundary_time_ms: boundaryTime, history_version: historyVersion,
    universe_id: universe.id, universe_version: universe.version,
  }, env, send);
  const parsed = metricsResponse.safeParse(response);
  if (!parsed.success) throw new Error("Python movement metrics response is invalid");
  const { evaluation } = parsed.data;
  if (parsed.data.session_id !== sessionId || parsed.data.history_version !== historyVersion ||
      evaluation.evaluation_boundary_time_ms !== boundaryTime ||
      evaluation.universe_id !== universe.id || evaluation.universe_version !== universe.version ||
      evaluation.config_version !== configVersion ||
      evaluation.configured_universe.length !== universe.symbols.length ||
      evaluation.configured_universe.some((symbol, index) => symbol !== universe.symbols[index]) ||
      ([1, 5, 15] as const).some((window) => {
        const item = evaluation.windows[String(window) as "1" | "5" | "15"];
        return !item || item.window_minutes !== window ||
          item.evaluation_boundary_time_ms !== boundaryTime ||
          item.universe_id !== universe.id || item.universe_version !== universe.version ||
          item.config_version !== configVersion ||
          item.configured_universe.length !== universe.symbols.length ||
          item.configured_universe.some((symbol, index) => symbol !== universe.symbols[index]);
      })) {
    throw new Error("Python movement metrics response has mismatched provenance");
  }
  return transportEvaluation(evaluation);
}
