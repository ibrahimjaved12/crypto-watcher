import { z } from "zod";
import type { Candle } from "../market/providers.server";
import type { MarketSource } from "../market/symbols";

const timestamp = z.number().int().min(0).max(4_102_444_800_000);
const finite = z.number().finite();
const nullableFinite = finite.nullable();
const source = z.enum(["binance-usdm", "okx-usdt-swap", "kraken-futures"]);
const indicatorCandle = z
  .object({
    open_ms: timestamp,
    open: finite.positive(),
    high: finite.positive(),
    low: finite.positive(),
    close: finite.positive(),
    volume: finite.nonnegative(),
    complete: z.boolean(),
  })
  .strict();
const factor = z
  .object({
    classification: z.string().min(1).max(64),
    contribution: nullableFinite,
    reason: z.string().min(1).max(80),
  })
  .strict();
const indicators = z
  .object({
    candle: indicatorCandle,
    previous_candle: indicatorCandle,
    ema20: finite,
    ema50: finite,
    ema200: nullableFinite,
    macd: z
      .object({
        line: nullableFinite,
        signal: nullableFinite,
        histogram: nullableFinite,
        previous_histogram: nullableFinite,
      })
      .strict(),
    bollinger: z
      .object({
        middle: nullableFinite,
        upper: nullableFinite,
        lower: nullableFinite,
        bandwidth_pct: nullableFinite,
        percent_b: nullableFinite,
      })
      .strict(),
    adx14: nullableFinite,
    plus_di14: nullableFinite,
    minus_di14: nullableFinite,
    range20: z.object({ low: finite, high: finite }).strict(),
    volume_average20: finite.nonnegative(),
    volume_ratio: nullableFinite,
    candle_count: z.number().int().min(200).max(1_000),
    rsi14: finite.min(0).max(100),
    atr14: finite.nonnegative(),
    volume_change_pct: nullableFinite,
    patterns: z.array(z.string().min(1).max(64)).max(16),
  })
  .strict();

export const technicalAnalysisResult = z
  .object({
    schema_version: z.literal(1),
    status: z.enum(["ok", "insufficient", "unavailable"]),
    reason: z.string().max(80).nullable(),
    classification: z.enum(["bullish", "bearish", "neutral", "unavailable"]),
    direction: z.enum(["bullish", "bearish", "neutral", "unavailable"]),
    score: finite.min(-100).max(100).nullable(),
    atr_pct: finite.nonnegative().nullable(),
    factor_breakdown: z
      .object({ trend: factor, momentum: factor, patterns: factor, volume: factor })
      .strict()
      .nullable(),
    reasons: z.array(z.string().min(1).max(80)).max(8),
    indicators: indicators.nullable(),
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
  .strict()
  .superRefine((value, context) => {
    if (value.direction !== value.classification) {
      context.addIssue({ code: z.ZodIssueCode.custom, message: "Mismatched direction" });
    }
    if (
      value.provenance.exchange !== value.provenance.source ||
      value.provenance.instrument_id !==
        `${value.provenance.exchange}:${value.provenance.native_symbol}` ||
      value.source_event_time_ms > value.evaluation_time_ms ||
      value.detection_time_ms > value.evaluation_time_ms
    ) {
      context.addIssue({ code: z.ZodIssueCode.custom, message: "Invalid provenance" });
    }
    if (value.status === "ok") {
      if (
        value.reason !== null ||
        value.indicators === null ||
        value.factor_breakdown === null ||
        value.candle_open_time_ms === null ||
        value.candle_close_time_ms === null ||
        value.provenance.candle_count < 200 ||
        (value.indicators !== null &&
          JSON.stringify(value.patterns) !== JSON.stringify(value.indicators.patterns))
      ) {
        context.addIssue({ code: z.ZodIssueCode.custom, message: "Incomplete TA result" });
      }
    } else if (
      value.reason === null ||
      value.indicators !== null ||
      value.factor_breakdown !== null ||
      value.candle_open_time_ms !== null ||
      value.candle_close_time_ms !== null
    ) {
      context.addIssue({ code: z.ZodIssueCode.custom, message: "Invalid unavailable result" });
    }
  });

export const technicalAnalysisBatchResponse = z
  .object({
    schema_version: z.literal(1),
    results: z.array(technicalAnalysisResult).min(1).max(8),
  })
  .strict();

export type TechnicalAnalysisResult = z.infer<typeof technicalAnalysisResult>;

export type TechnicalAnalysisRequest = {
  schema_version: 1;
  instrument: {
    instrument_id: string;
    exchange: MarketSource;
    native_symbol: string;
    market_type: "futures";
    contract_type: "perpetual";
  };
  timeframe_minutes: 15 | 60 | 240;
  candles: Array<{
    open_ms: number;
    open: number;
    high: number;
    low: number;
    close: number;
    volume: number;
    complete: boolean;
  }>;
  warmup_candles: [];
  missing_open_times_ms: [];
  source: MarketSource;
  source_event_time_ms: number;
  evaluation_time_ms: number;
  detection_time_ms: number;
  price_type: "trade";
  target_candle_open_time_ms: number;
  config: {
    ta_version: "ta-v2";
    interpretation_version: "interpretation-v1";
    minimum_history: 200;
  };
};

export function requestCandles(candles: Candle[]): TechnicalAnalysisRequest["candles"] {
  return candles.map((candle) => ({
    open_ms: candle.time,
    open: candle.open,
    high: candle.high,
    low: candle.low,
    close: candle.close,
    volume: candle.volume,
    complete: candle.complete === true,
  }));
}
