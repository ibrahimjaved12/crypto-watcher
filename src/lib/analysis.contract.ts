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
const instrument = z.object({
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
});
const windowResult = z.object({
  window_minutes: z.number().int(),
  interval_minutes: z.number().int(),
  status: z.enum(["ok", "unavailable", "stale"]),
  reason: z.string().max(200).nullable(),
  change_pct: decimal.nullable(),
  threshold_met: z.boolean().nullable(),
  start_close_ms: timestamp.nullable(),
  end_close_ms: timestamp.nullable(),
});

export const analysisResponse = z.object({
  schema_version: z.literal(1),
  mode: z.literal("read_only"),
  symbol: z.string().max(16),
  status: z.enum(["ok", "partial", "unavailable"]),
  source: source.nullable(),
  instrument,
  price_type: z.literal("trade"),
  endpoint: z.enum([
    "/fapi/v1/klines",
    "/api/v5/market/candles",
    "/api/charts/v1/trade/:symbol/:resolution",
  ]),
  retrieved_at_ms: timestamp,
  as_of_ms: timestamp,
  price: decimal.nullable(),
  observed_at_ms: timestamp.nullable(),
  threshold_pct: decimal,
  rolling: z.record(z.enum(["5", "15", "60", "240", "1440"]), windowResult),
  baseline: z.object({
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
  }),
  attempts: z
    .array(
      z.object({
        source,
        reason: z.enum(["provider_timeout", "provider_unavailable", "invalid_or_stale_data"]),
      }),
    )
    .max(3),
});

export type PythonAnalysis = z.infer<typeof analysisResponse>;
export type AnalysisReply = { ok: true; analysis: PythonAnalysis } | { ok: false; error: string };
