import { z } from "zod";
import { isSupportedSymbol } from "./market/symbols";

export const analysisInput = z
  .object({
    symbol: z.string().min(5).max(16).refine(isSupportedSymbol, "Unsupported pair"),
  })
  .strict();

const decimal = z
  .string()
  .max(100)
  .regex(/^-?\d+(\.\d+)?(E[+-]?\d+)?$/i)
  .refine((value) => Number.isFinite(Number(value)));
const timestamp = z.number().int().min(0).max(4102444800000);
const source = z.enum(["binance-usdm", "okx-usdt-swap", "kraken-futures"]);
const failureCategory = z.enum([
  "provider_timeout",
  "provider_unavailable",
  "invalid_or_stale_data",
  "invalid_data",
  "stale_data",
  "insufficient_history",
]);
const instrument = z
  .object({
    id: z.string().max(64),
    exchange: z.literal("binance"),
    native_symbol: z.string().max(16),
    market_type: z.literal("futures"),
    contract_type: z.literal("perpetual"),
    base_asset: z.string().max(16),
    quote_asset: z.literal("USDT"),
    margin_asset: z.literal("USDT"),
    settlement_asset: z.literal("USDT"),
    linear: z.literal(true),
    contract_multiplier: z.literal(1),
  })
  .strict();
const sourceInstrument = z
  .object({
    instrument_id: z.string().min(3).max(128),
    exchange: source,
    native_symbol: z.string().min(2).max(64),
    market_type: z.literal("futures"),
    contract_type: z.literal("perpetual"),
  })
  .strict();
const windowResult = z
  .object({
    window_minutes: z.number().int(),
    interval_minutes: z.number().int(),
    status: z.enum(["ok", "unavailable", "stale"]),
    reason: z.string().max(200).nullable(),
    change_pct: decimal.nullable(),
    threshold_met: z.boolean().nullable(),
    start_close_ms: timestamp.nullable(),
    end_close_ms: timestamp.nullable(),
    lag_ms: z.number().int().optional(),
    start_price: decimal.optional(),
    end_price: decimal.optional(),
  })
  .strict();
const factor = z
  .object({
    classification: z.string().min(1).max(64),
    contribution: z.number().finite().nullable(),
    reason: z.string().min(1).max(80),
  })
  .strict();
const technicalResult = z
  .object({
    schema_version: z.literal(1),
    status: z.enum(["ok", "insufficient", "unavailable"]),
    reason: z.string().max(80).nullable(),
    classification: z.enum(["bullish", "bearish", "neutral", "unavailable"]),
    direction: z.enum(["bullish", "bearish", "neutral", "unavailable"]),
    score: z.number().finite().min(-100).max(100).nullable(),
    factor_breakdown: z
      .object({ trend: factor, momentum: factor, patterns: factor, volume: factor })
      .nullable(),
    reasons: z.array(z.string().min(1).max(80)).max(8),
    patterns: z.array(z.string().min(1).max(64)).max(16),
    timeframe_minutes: z.union([z.literal(15), z.literal(60), z.literal(240)]),
    candle_open_time_ms: timestamp.nullable(),
    candle_close_time_ms: timestamp.nullable(),
    source_event_time_ms: timestamp,
    evaluation_time_ms: timestamp,
    detection_time_ms: timestamp,
    ta_version: z.literal("ta-v2"),
    strategy_version: z.literal("interpretation-v1"),
    provenance: z
      .object({
        instrument_id: z.string().min(3).max(128),
        exchange: source,
        native_symbol: z.string().min(2).max(64),
        market_type: z.literal("futures"),
        contract_type: z.literal("perpetual"),
        source,
        price_type: z.literal("trade"),
        candle_count: z.number().int().min(0).max(1_000),
        warmup_candle_count: z.number().int().min(0).max(1_000),
        missing_open_times_ms: z.array(timestamp).max(1_000),
      })
      .strict(),
  })
  .strict();

export const analysisResponse = z
  .object({
    schema_version: z.literal(1),
    mode: z.literal("read_only"),
    symbol: z.string().max(16),
    status: z.enum(["ok", "partial", "unavailable"]),
    source: source.nullable(),
    instrument,
    source_instrument: sourceInstrument.nullable(),
    price_type: z.literal("trade"),
    endpoint: z
      .enum([
        "/fapi/v1/klines",
        "/api/v5/market/candles",
        "/api/charts/v1/trade/:symbol/:resolution",
      ])
      .nullable(),
    retrieved_at_ms: timestamp,
    as_of_ms: timestamp,
    price: decimal.nullable(),
    observed_at_ms: timestamp.nullable(),
    threshold_pct: decimal,
    rolling: z.record(z.enum(["5", "15", "60", "240", "1440"]), windowResult),
    technical: z.record(z.enum(["15", "60", "240"]), technicalResult),
    failure_category: failureCategory.nullable(),
    baseline: z
      .object({
        status: z.enum([
          "unavailable",
          "invalid_state",
          "disabled",
          "baseline_required",
          "baseline_reset_required",
          "already_processed",
          "below_threshold",
          "cooldown",
          "eligible",
        ]),
        change_pct: decimal.nullable(),
        direction: z.enum(["up", "down"]).nullable(),
        baseline_price: decimal.nullable(),
        baseline_at_ms: timestamp.nullable(),
        cooldown_evaluated: z.boolean(),
        cooldown_until_ms: timestamp.nullable(),
        eligibility_evaluated: z.boolean(),
        alert_eligible: z.boolean().nullable(),
      })
      .strict(),
    attempts: z.array(z.object({ source, reason: failureCategory }).strict()).max(3),
  })
  .strict()
  .superRefine((value, context) => {
    if ((value.status === "ok") !== (value.failure_category === null)) {
      context.addIssue({ code: z.ZodIssueCode.custom, message: "Mismatched failure category" });
    }
    if (value.source === null) {
      if (
        value.source_instrument !== null ||
        value.endpoint !== null ||
        Object.keys(value.technical).length > 0
      ) {
        context.addIssue({ code: z.ZodIssueCode.custom, message: "Unexpected source instrument" });
      }
      return;
    }
    const base = value.symbol.replace(/USDT$/, "");
    const expectedNative =
      value.source === "okx-usdt-swap"
        ? `${base}-USDT-SWAP`
        : value.source === "kraken-futures"
          ? `PF_${base === "BTC" ? "XBT" : base}USD`
          : value.symbol;
    const expectedEndpoint = {
      "binance-usdm": "/fapi/v1/klines",
      "kraken-futures": "/api/charts/v1/trade/:symbol/:resolution",
      "okx-usdt-swap": "/api/v5/market/candles",
    }[value.source];
    if (
      value.source_instrument === null ||
      value.endpoint !== expectedEndpoint ||
      value.source_instrument.exchange !== value.source ||
      value.source_instrument.native_symbol !== expectedNative ||
      value.source_instrument.instrument_id !==
        `${value.source}:${value.source_instrument.native_symbol}` ||
      !["15", "60", "240"].every((timeframe) => timeframe in value.technical) ||
      Object.values(value.technical).some(
        (row) =>
          row.provenance.source !== value.source ||
          row.provenance.instrument_id !== value.source_instrument?.instrument_id,
      )
    ) {
      context.addIssue({ code: z.ZodIssueCode.custom, message: "Mismatched source provenance" });
    }
  });

export type PythonAnalysis = z.infer<typeof analysisResponse>;
export type AnalysisReply =
  | {
      ok: true;
      analysis: PythonAnalysis;
      metrics: { duration_ms: number; request_bytes: number; response_bytes: number };
    }
  | { ok: false; error: string; category: string };
