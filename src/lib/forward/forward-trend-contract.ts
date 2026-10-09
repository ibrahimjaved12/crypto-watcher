import { z } from "zod";

/**
 * Contracts of the forward trend track (#239 P14): the public Binance daily klines that feed it and
 * the Python `POST /v1/forward/trend` response, validated before anything is stored.
 */
export const DAY_MS = 86_400_000;
const sha256 = z.string().regex(/^[0-9a-f]{64}$/);
const dayMs = z.number().int().nonnegative().refine((value) => value % DAY_MS === 0, "not a UTC day start");
const decimalText = z.string().regex(/^\d+(\.\d+)?$/);
const symbol = z.string().regex(/^[A-Z0-9]{5,16}$/);

/** One `GET /fapi/v1/klines?interval=1d` row: [openTime, open, high, low, close, volume, closeTime, quoteVolume, ...]. */
const binanceKlineSchema = z
  .tuple([z.number().int().nonnegative(), decimalText, decimalText, decimalText, decimalText, decimalText,
    z.number().int().nonnegative(), decimalText])
  .rest(z.unknown());
export const binanceKlinesResponseSchema = z.array(binanceKlineSchema).max(1500);

export type DailyBar = {
  symbol: string;
  day_ms: number;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
  quote_volume: string;
};

/**
 * Typed, value-free validation of a Binance daily kline response. Only COMPLETED days are returned:
 * a day is complete once 00:00 UTC of the next day has passed (`day + 1 day <= nowMs`), so the
 * running day Binance always includes last is dropped and never stored.
 */
export function parseBinanceDailyKlines(symbolName: string, value: unknown, nowMs: number): DailyBar[] {
  const parsed = binanceKlinesResponseSchema.safeParse(value);
  if (!parsed.success) throw new Error("Invalid Binance kline response");
  const out: DailyBar[] = [];
  let previous = -1;
  for (const row of parsed.data) {
    const [openTime, open, high, low, close, volume, closeTime, quoteVolume] = row;
    if (openTime % DAY_MS !== 0 || openTime <= previous || closeTime !== openTime + DAY_MS - 1) {
      throw new Error("Invalid Binance kline response");
    }
    const [o, h, l, c] = [open, high, low, close].map(Number) as [number, number, number, number];
    if (!(o > 0 && c > 0 && l > 0 && l <= Math.min(o, c) && Math.max(o, c) <= h)) {
      throw new Error("Invalid Binance kline response");
    }
    previous = openTime;
    if (openTime + DAY_MS > nowMs) continue;  // the running day: not complete yet
    out.push({ symbol: symbolName, day_ms: openTime, open, high, low, close, volume, quote_volume: quoteVolume });
  }
  return out;
}

const weightMap = z.record(z.string(), z.number().finite());

export const trendLedgerRowSchema = z
  .object({
    track: z.string().min(1).max(64),
    day_ms: dayMs,
    daily_ppm: z.number().int(),
    equity_ppm: z.number().int().nonnegative(),
    turnover_ppm: z.number().int().nonnegative(),
    gross_ppm: z.number().int().nonnegative(),
    symbols_active: z.number().int().nonnegative(),
    weights: weightMap,
  })
  .strict();

export const trendWeightRowSchema = z
  .object({
    track: z.string().min(1).max(64),
    day_ms: dayMs,
    decided_from_close_ms: z.number().int(),
    weights: weightMap,
    defined: z.record(z.string(), z.boolean()),
  })
  .strict()
  .refine((row) => row.decided_from_close_ms === row.day_ms - DAY_MS, "weight decided after its day");

export const trendStateSchema = z
  .object({
    version: z.string(),
    track: z.string(),
    last_day_ms: dayMs.nullable(),
    equity_ppm: z.number().int(),
    n_days: z.number().int().nonnegative(),
    sum_ppm: z.number().int(),
    sum_sq_ppm: z.number().int().nonnegative(),
    peak_equity_ppm: z.number().int(),
    max_drawdown_ppm: z.number().int().nonnegative(),
    weights: weightMap,
    mu_min_daily: z.number().nullable(),
  })
  .strict();

export const forwardTrendResponseSchema = z.object({
  schema_version: z.literal(1),
  versions: z.record(z.string(), z.string()),
  params_hash: sha256,
  symbols: z.array(symbol),
  track_start_ms: dayMs,
  tracks: z.array(z.object({ name: z.string(), kind: z.enum(["variant", "buy_hold_vt", "equal_weight_long"]),
    control: z.string().nullable(), config_hash: sha256 })),
  through_day_ms: dayMs.nullable(),
  ledger: z.array(trendLedgerRowSchema),
  weights: z.array(trendWeightRowSchema),
  states: z.record(z.string(), trendStateSchema),
  funding_unavailable: z.array(z.string()),
  reasons: z.record(z.string(), z.string()),
  assumptions: z.array(z.string()),
});

export type ForwardTrendResponse = z.infer<typeof forwardTrendResponseSchema>;
export type TrendState = z.infer<typeof trendStateSchema>;
export type TrendLedgerRow = z.infer<typeof trendLedgerRowSchema>;

/** Throws a short, value-free error when the Python response breaks the contract. */
export function validateTrendResponse(value: unknown): ForwardTrendResponse {
  const parsed = forwardTrendResponseSchema.safeParse(value);
  if (!parsed.success) {
    const path = parsed.error.issues[0]?.path.join(".") ?? "";
    throw new Error(`Invalid forward trend response at ${path || "root"}`);
  }
  const keys = parsed.data.ledger.map((row) => `${row.track}|${row.day_ms}`);
  if (new Set(keys).size !== keys.length) throw new Error("Invalid forward trend response: duplicate ledger day");
  return parsed.data;
}
