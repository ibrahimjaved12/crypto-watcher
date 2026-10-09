import { z } from "zod";

/** Response contract of the Python `POST /v1/forward/evaluate` (#239 P10), validated before persistence. */
const sha256 = z.string().regex(/^[0-9a-f]{64}$/);
const ms = z.number().int().nonnegative();
const intOrNull = z.number().int().nullable();
const bigText = z.string().regex(/^[0-9]{1,30}$/).nullable();

export const forwardSignalSchema = z.object({
  signal_id: sha256,
  strategy_id: z.string().min(1).max(128),
  version: z.string().min(1).max(64),
  symbol: z.string().regex(/^[A-Z0-9]{5,16}$/),
  signal_ms: ms,
  side: z.union([z.literal(1), z.literal(-1)]),
  horizon_min: z.union([z.literal(15), z.literal(60), z.literal(240)]),
});

export const forwardSetupSchema = z
  .object({
    setup_id: sha256,
    signal_id: sha256,
    strategy_id: z.string().min(1).max(128),
    version: z.string().min(1).max(64),
    symbol: z.string().regex(/^[A-Z0-9]{5,16}$/),
    side: z.union([z.literal(1), z.literal(-1)]),
    horizon_min: z.number().int(),
    signal_ms: ms,
    entry_ms: ms,
    k: z.string().min(1),
    rr: z.string().min(1),
    status: z.enum(["T", "V", "C", "P", "G", "N"]),
    half_life_days: z.number().int(),
    window_minutes: z.number().int().positive(),
    // Integers above 2**53 travel as exact decimal text (JSON numbers would lose precision).
    var: bigText,
    factor_weight: bigText,
    sigma: bigText,
    p0: intOrNull,
    tick: intOrNull,
    d_ticks: intOrNull,
    stop: intOrNull,
    target: intOrNull,
    label_leverage: intOrNull,
    params_hash: sha256,
    versions: z.record(z.string(), z.string()),
  })
  .strict();

export const forwardResolutionSchema = z.object({
  setup_id: sha256,
  status: z.enum(["pending", "open", "T", "S", "E", "L", "X", "ambiguous"]),
  resolved_through_ms: ms.nullable(),
  exit_offset: intOrNull,
  exit_ms: ms.nullable(),
  exit_ref_price: intOrNull,
  net_ur: intOrNull,
  cost_ur: intOrNull,
  fund_ur: intOrNull,
  optimistic: z.record(z.string(), z.unknown()).nullable(),
  label_leverage: intOrNull,
  wallet_ur: intOrNull,
});

export const FINAL_STATUSES = new Set(["T", "S", "E", "L", "X", "ambiguous"]);

export const ledgerEntrySchema = z
  .object({
    seq: z.number().int().positive(),
    ms,
    type: z.enum(["open_fee", "pnl", "close_fee", "funding", "liquidation", "rejected"]),
    amount_e8: z.number().int(),
    balance_e8: z.number().int(),
    assumptions: z.array(z.string()),
    setup_id: z.string().optional(),
    symbol: z.string().optional(),
  })
  .passthrough();

export const walletStateSchema = z.object({
  version: z.string(),
  balance_e8: z.number().int(),
  used_margin_e8: z.number().int().nonnegative(),
  seq: z.number().int().nonnegative(),
  positions: z.record(z.string(), z.record(z.string(), z.unknown())),
  done: z.array(z.string()),
});

export const forwardEvaluateResponseSchema = z.object({
  schema_version: z.literal(1),
  versions: z.record(z.string(), z.string()),
  params_hash: sha256,
  wallet_config: z.record(z.string(), z.unknown()),
  assumptions: z.array(z.string()),
  processed_to_ms: z.record(z.string(), ms),
  funding_unavailable: z.array(z.string()).default([]),
  signals: z.array(forwardSignalSchema),
  setups: z.array(forwardSetupSchema),
  resolutions: z.array(forwardResolutionSchema),
  reasons: z.record(z.string(), z.record(z.string(), z.string())),
  ledger: z.array(ledgerEntrySchema),
  wallet_state: walletStateSchema,
});

/** One public Binance `GET /fapi/v1/fundingRate` row (unauthenticated history). */
export const binanceFundingRowSchema = z.object({
  symbol: z.string().regex(/^[A-Z0-9]{5,16}$/),
  fundingTime: z.number().int().nonnegative(),
  fundingRate: z.string().regex(/^-?\d+(\.\d+)?(e-?\d+)?$/i),
  markPrice: z.string().optional(),
});
export const binanceFundingResponseSchema = z.array(binanceFundingRowSchema).max(1000);

export type FundingEvent = { calc_time_ms: number; rate: string };

/** Typed, value-free validation of a Binance funding history response for one symbol. */
export function parseBinanceFunding(symbol: string, value: unknown): FundingEvent[] {
  const parsed = binanceFundingResponseSchema.safeParse(value);
  if (!parsed.success) throw new Error("Invalid Binance funding response");
  let previous = -1;
  return parsed.data.map((row) => {
    if (row.symbol !== symbol || row.fundingTime <= previous) throw new Error("Invalid Binance funding response");
    previous = row.fundingTime;
    return { calc_time_ms: row.fundingTime, rate: row.fundingRate };
  });
}

export type ForwardEvaluateResponse = z.infer<typeof forwardEvaluateResponseSchema>;
export type ForwardSetup = z.infer<typeof forwardSetupSchema>;
export type ForwardResolution = z.infer<typeof forwardResolutionSchema>;
export type WalletState = z.infer<typeof walletStateSchema>;

/** Throws a short, value-free error when the Python response breaks the contract. */
export function validateForwardResponse(value: unknown): ForwardEvaluateResponse {
  const parsed = forwardEvaluateResponseSchema.safeParse(value);
  if (!parsed.success) {
    const path = parsed.error.issues[0]?.path.join(".") ?? "";
    throw new Error(`Invalid forward evaluation response at ${path || "root"}`);
  }
  const response = parsed.data;
  const signalIds = new Set(response.signals.map((signal) => signal.signal_id));
  if (response.setups.some((setup) => !signalIds.has(setup.signal_id))) {
    throw new Error("Invalid forward evaluation response: setup without its signal");
  }
  if (new Set(response.ledger.map((line) => line.seq)).size !== response.ledger.length) {
    throw new Error("Invalid forward evaluation response: duplicate ledger sequence");
  }
  return response;
}
