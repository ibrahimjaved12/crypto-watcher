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
import { MARKET_STATE_CLASSIFIER_CONFIG_VERSION, MARKET_STATE_CLASSIFIER_VERSION,
  type ConfirmedMarketDirection, type MarketClassification } from "./market-state-contract";
import type { MovementInstrumentCompatibility, MovementRawHistory } from "./movement-normalization-input";

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
const classifierMetric = <T extends z.ZodTypeAny>(value: T, missing: z.ZodTypeAny) =>
  z.discriminatedUnion("available", [
    z.object({ available: z.literal(true), value, reason: z.null() }).strict(),
    z.object({ available: z.literal(false), value: z.null(), reason: missing }).strict(),
  ]);
const accelerationMetric = classifierMetric(number, z.literal("ACCELERATION_UNAVAILABLE"));
const accelerationBreadthMetric = classifierMetric(
  z.object({ count: nonnegativeInteger, fraction: number }).strict(),
  z.literal("ACCELERATION_UNAVAILABLE"),
);
const classifierConfigSchema = z.object({
  version: z.literal(MARKET_STATE_CLASSIFIER_CONFIG_VERSION),
  directional_breadth: z.literal(0.70), material_breadth: z.literal(0.50),
  normalized_movement: z.literal(0.50), acceleration_breadth: z.literal(0.60),
  isolated_outlier_breadth_disagreement: z.literal(0.50),
}).strict();
const classificationWindowSchema = z.object({
  window_minutes: windowMinutes,
  horizon_role: z.enum(["RAPID", "PRIMARY", "PERSISTENCE"]),
  is_primary: z.boolean(),
  direction_state: z.enum(["BROAD_RISE", "BROAD_DROP", "NEUTRAL", "WARMING", "UNAVAILABLE"]),
  pace: classifierMetric(z.enum(["ACCELERATING", "DECELERATING", "MIXED"]),
    z.enum(["NO_BROAD_DIRECTION", "ACCELERATION_UNAVAILABLE"])),
  prior_confirmed_episode_direction: z.enum(["BROAD_RISE", "BROAD_DROP"]).nullable(),
  reversal_candidate: z.object({
    prior_confirmed_episode_direction: z.enum(["BROAD_RISE", "BROAD_DROP"]),
    current_direction: z.enum(["BROAD_RISE", "BROAD_DROP"]),
    directional_breadth: z.object({ count: nonnegativeInteger, fraction: number }).strict(),
    material_breadth: z.object({ count: nonnegativeInteger, fraction: number }).strict(),
    median_raw_return: number, median_normalized_movement: number,
  }).strict().nullable(),
  isolated_outliers: z.array(z.object({
    symbol: z.string().min(1), direction: z.enum(["RISING", "FALLING"]),
    raw_return: number, historical_z: number, cross_sectional_z: number,
    same_direction_breadth_count: nonnegativeInteger,
    same_direction_breadth_fraction: number,
    same_direction_breadth_denominator: nonnegativeInteger,
  }).strict()),
  breadth: windowMetrics.shape.breadth,
  median_raw_return: numericMetric, median_normalized_movement: numericMetric,
  median_acceleration: accelerationMetric,
  positive_acceleration_breadth: accelerationBreadthMetric,
  negative_acceleration_breadth: accelerationBreadthMetric,
  dispersion_mad_normalized_movement: numericMetric,
  trimmed_mean_normalized_movement: numericMetric,
  liquidity_weighted_normalized_movement: numericMetric,
  liquidity_weights: windowMetrics.shape.aggregates.shape.liquidity_weights,
  volume_context: z.array(z.object({
    symbol: z.string().min(1), current_notional_volume: numericMetric, rvol: numericMetric,
  }).strict()),
  market_wide_eligible: z.boolean(), eligible_count: nonnegativeInteger,
  eligible_fraction: number, configured_universe: z.array(z.string().min(1)),
  included_symbols: z.array(z.string().min(1)),
  excluded_symbols: windowMetrics.shape.excluded_symbols,
  availability_reasons: z.array(z.string().min(1)),
  classifier_algorithm_version: z.literal(MARKET_STATE_CLASSIFIER_VERSION),
  classifier_config_version: z.literal(MARKET_STATE_CLASSIFIER_CONFIG_VERSION),
  movement_algorithm_version: z.literal("market-movement-v1"),
  movement_config_version: z.string().min(1),
  universe_id: z.string().min(1), universe_version: z.string().min(1),
  provider: z.literal("binance-usdm"), exchange: z.literal("binance"),
  price_type: z.literal("trade"), evaluation_boundary_time_ms: nonnegativeInteger,
  source_time_evidence: z.array(z.object({
    symbol: z.string().min(1), last_real_trade_time_ms: timestamp,
    last_real_event_time_ms: timestamp, last_received_at_ms: timestamp,
  }).strict()),
  movement_snapshot: windowMetrics,
}).strict();
const classificationSchema = z.object({
  classifier_algorithm_version: z.literal(MARKET_STATE_CLASSIFIER_VERSION),
  classifier_config_version: z.literal(MARKET_STATE_CLASSIFIER_CONFIG_VERSION),
  classifier_config: classifierConfigSchema,
  movement_algorithm_version: z.literal("market-movement-v1"),
  movement_config_version: z.string().min(1),
  universe_id: z.string().min(1), universe_version: z.string().min(1),
  provider: z.literal("binance-usdm"), exchange: z.literal("binance"),
  price_type: z.literal("trade"), evaluation_boundary_time_ms: nonnegativeInteger,
  primary_window_minutes: z.literal(5),
  windows: z.record(z.enum(["1", "5", "15"]), classificationWindowSchema),
}).strict();
const assessmentResponse = z.object({
  schema_version: z.literal(1), session_id: z.string().uuid(),
  history_version: z.string().min(1), evaluation: evaluationSchema,
  classification: classificationSchema,
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
  config: MarketMovementConfig, historical: MovementRawHistory,
  compatibility: MovementInstrumentCompatibility,
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
      instrument_compatible: compatibility.get(symbol) ?? null,
      candles: (historical.get(symbol) ?? []).map((candle) => ({
        open_time_ms: candle.openTime, close: candle.close,
        volume: candle.volume, quote_volume: candle.quoteVolume,
      })),
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

export async function calculatePythonMarketAssessment(
  sessionId: string, boundaryTime: number, historyVersion: string,
  universe: MarketUniverse, movementConfigVersion: string,
  previousConfirmedPrimaryDirection: ConfirmedMarketDirection | null,
  env: Record<string, string | undefined> = process.env, send: typeof fetch = fetch,
): Promise<{ movement: MarketMovementEvaluation; classification: MarketClassification }> {
  const response = await postCanonicalMovement("/v1/movement/classification", {
    schema_version: 1, session_id: sessionId,
    evaluation_boundary_time_ms: boundaryTime, history_version: historyVersion,
    universe_id: universe.id, universe_version: universe.version,
    previous_confirmed_primary_direction: previousConfirmedPrimaryDirection,
  }, env, send);
  const parsed = assessmentResponse.safeParse(response);
  if (!parsed.success) throw new Error("Python movement assessment response is invalid");
  const { evaluation: rawMovement, classification: rawClassification } = parsed.data;
  const ordered = (symbols: string[]) =>
    symbols.length === universe.symbols.length &&
    symbols.every((symbol, index) => symbol === universe.symbols[index]);
  const same = (left: unknown, right: unknown) => JSON.stringify(left) === JSON.stringify(right);
  if (parsed.data.session_id !== sessionId || parsed.data.history_version !== historyVersion ||
      rawMovement.evaluation_boundary_time_ms !== boundaryTime ||
      rawMovement.universe_id !== universe.id || rawMovement.universe_version !== universe.version ||
      rawMovement.config_version !== movementConfigVersion || !ordered(rawMovement.configured_universe) ||
      rawClassification.evaluation_boundary_time_ms !== boundaryTime ||
      rawClassification.universe_id !== universe.id ||
      rawClassification.universe_version !== universe.version ||
      rawClassification.movement_algorithm_version !== rawMovement.algorithm_version ||
      rawClassification.movement_config_version !== rawMovement.config_version ||
      rawClassification.classifier_config.version !== rawClassification.classifier_config_version ||
      Object.keys(rawMovement.windows).length !== 3 ||
      Object.keys(rawClassification.windows).length !== 3) {
    throw new Error("Python movement assessment has mismatched identity");
  }
  for (const minute of [1, 5, 15] as const) {
    const key = String(minute) as "1" | "5" | "15";
    const movementWindow = rawMovement.windows[key];
    const window = rawClassification.windows[key];
    const role = minute === 1 ? "RAPID" : minute === 5 ? "PRIMARY" : "PERSISTENCE";
    if (!movementWindow || !window || movementWindow.window_minutes !== minute ||
        window.window_minutes !== minute || window.horizon_role !== role ||
        window.is_primary !== (minute === 5) ||
        window.prior_confirmed_episode_direction !==
          (minute === 5 ? previousConfirmedPrimaryDirection : null) ||
        movementWindow.evaluation_boundary_time_ms !== boundaryTime ||
        window.evaluation_boundary_time_ms !== boundaryTime ||
        movementWindow.config_version !== movementConfigVersion ||
        window.movement_algorithm_version !== rawMovement.algorithm_version ||
        window.movement_config_version !== rawMovement.config_version ||
        window.classifier_algorithm_version !== rawClassification.classifier_algorithm_version ||
        window.classifier_config_version !== rawClassification.classifier_config_version ||
        window.universe_id !== universe.id || window.universe_version !== universe.version ||
        movementWindow.universe_id !== universe.id || movementWindow.universe_version !== universe.version ||
        !ordered(movementWindow.configured_universe) || !ordered(window.configured_universe) ||
        window.provider !== movementWindow.provider ||
        window.exchange !== movementWindow.exchange ||
        window.price_type !== movementWindow.price_type ||
        window.market_wide_eligible !== movementWindow.market_wide_eligible ||
        window.eligible_count !== movementWindow.eligible_count ||
        window.eligible_fraction !== movementWindow.eligible_fraction ||
        !same(window.included_symbols, movementWindow.included_symbols) ||
        !same(window.excluded_symbols, movementWindow.excluded_symbols) ||
        !same(window.breadth, movementWindow.breadth) ||
        !same(window.median_raw_return, movementWindow.aggregates.median_raw_return) ||
        !same(window.median_normalized_movement,
          movementWindow.aggregates.median_normalized_movement) ||
        !same(window.dispersion_mad_normalized_movement,
          movementWindow.aggregates.dispersion_mad_normalized_movement) ||
        !same(window.trimmed_mean_normalized_movement,
          movementWindow.aggregates.trimmed_mean_normalized_movement) ||
        !same(window.liquidity_weighted_normalized_movement,
          movementWindow.aggregates.liquidity_weighted_normalized_movement) ||
        !same(window.liquidity_weights, movementWindow.aggregates.liquidity_weights) ||
        window.source_time_evidence.length !== universe.symbols.length ||
        window.source_time_evidence.some((item, index) => item.symbol !== universe.symbols[index]) ||
        !same(window.movement_snapshot, movementWindow)) {
      throw new Error(`Python movement assessment has mismatched ${minute}m evidence`);
    }
  }
  const movement = transportEvaluation(rawMovement);
  const classification: MarketClassification = {
    classifierAlgorithmVersion: rawClassification.classifier_algorithm_version,
    classifierConfigVersion: rawClassification.classifier_config_version,
    classifierConfig: {
      version: rawClassification.classifier_config.version,
      directionalBreadth: rawClassification.classifier_config.directional_breadth,
      materialBreadth: rawClassification.classifier_config.material_breadth,
      normalizedMovement: rawClassification.classifier_config.normalized_movement,
      accelerationBreadth: rawClassification.classifier_config.acceleration_breadth,
      isolatedOutlierBreadthDisagreement:
        rawClassification.classifier_config.isolated_outlier_breadth_disagreement,
    },
    movementAlgorithmVersion: rawClassification.movement_algorithm_version,
    movementConfigVersion: rawClassification.movement_config_version,
    universeId: rawClassification.universe_id, universeVersion: rawClassification.universe_version,
    provider: rawClassification.provider, exchange: rawClassification.exchange,
    priceType: rawClassification.price_type,
    evaluationBoundaryTime: rawClassification.evaluation_boundary_time_ms,
    primaryWindowMinutes: 5,
    windows: ([1, 5, 15] as const).map((minute) => {
      const item = rawClassification.windows[String(minute) as "1" | "5" | "15"]!;
      const movementWindow = movement.windows.find((window) => window.windowMinutes === minute)!;
      return {
        windowMinutes: minute, horizonRole: item.horizon_role, isPrimary: item.is_primary,
        directionState: item.direction_state, pace: item.pace,
        priorConfirmedEpisodeDirection: item.prior_confirmed_episode_direction,
        reversalCandidate: item.reversal_candidate && {
          priorConfirmedEpisodeDirection: item.reversal_candidate.prior_confirmed_episode_direction,
          currentDirection: item.reversal_candidate.current_direction,
          directionalBreadth: item.reversal_candidate.directional_breadth,
          materialBreadth: item.reversal_candidate.material_breadth,
          medianRawReturn: item.reversal_candidate.median_raw_return,
          medianNormalizedMovement: item.reversal_candidate.median_normalized_movement,
        },
        isolatedOutliers: item.isolated_outliers.map((outlier) => ({
          symbol: outlier.symbol, direction: outlier.direction, rawReturn: outlier.raw_return,
          historicalZ: outlier.historical_z, crossSectionalZ: outlier.cross_sectional_z,
          sameDirectionBreadthCount: outlier.same_direction_breadth_count,
          sameDirectionBreadthFraction: outlier.same_direction_breadth_fraction,
          sameDirectionBreadthDenominator: outlier.same_direction_breadth_denominator,
        })),
        breadth: movementWindow.breadth,
        medianRawReturn: item.median_raw_return,
        medianNormalizedMovement: item.median_normalized_movement,
        medianAcceleration: item.median_acceleration,
        positiveAccelerationBreadth: item.positive_acceleration_breadth,
        negativeAccelerationBreadth: item.negative_acceleration_breadth,
        dispersionMadNormalizedMovement: item.dispersion_mad_normalized_movement,
        trimmedMeanNormalizedMovement: item.trimmed_mean_normalized_movement,
        liquidityWeightedNormalizedMovement: item.liquidity_weighted_normalized_movement,
        liquidityWeights: item.liquidity_weights,
        volumeContext: item.volume_context.map((value) => ({
          symbol: value.symbol, currentNotionalVolume: value.current_notional_volume, rvol: value.rvol,
        })),
        marketWideEligible: item.market_wide_eligible,
        eligibleCount: item.eligible_count, eligibleFraction: item.eligible_fraction,
        configuredUniverse: item.configured_universe, includedSymbols: item.included_symbols,
        excludedSymbols: item.excluded_symbols, availabilityReasons: item.availability_reasons,
        classifierAlgorithmVersion: item.classifier_algorithm_version,
        classifierConfigVersion: item.classifier_config_version,
        movementAlgorithmVersion: item.movement_algorithm_version,
        movementConfigVersion: item.movement_config_version,
        universeId: item.universe_id, universeVersion: item.universe_version,
        provider: item.provider, exchange: item.exchange, priceType: item.price_type,
        evaluationBoundaryTime: item.evaluation_boundary_time_ms,
        sourceTimeEvidence: item.source_time_evidence.map((value) => ({
          symbol: value.symbol,
          lastRealTradeTimeMs: value.last_real_trade_time_ms,
          lastRealEventTimeMs: value.last_real_event_time_ms,
          lastReceivedAtMs: value.last_received_at_ms,
        })),
        movementSnapshot: movementWindow,
      };
    }),
  };
  return { movement, classification };
}
