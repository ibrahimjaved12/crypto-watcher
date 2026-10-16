/**
 * "Longer moves" (slow-movement-v1, #30 / #373): the multi-hour market movement descriptor next to the V1
 * 1m/5m/15m engine. Client-safe schemas and constants only; the math lives in Python
 * (python/market_analysis/slow_movement.py). A descriptor of the current market, not a predictor; no alerts.
 */
import { z } from "zod";

import { MARKET_UNIVERSE_ID } from "./market-movement-state";

export const SLOW_MOVEMENT_ALGORITHM_VERSION = "slow-movement-v1";
export const SLOW_MOVEMENT_CONFIG_VERSION = "slow-config-v1";
export const SLOW_PUBLISHED_VERSION = "slow-movement-published-v1";
/** The hosted row of the published descriptor, beside the V1 row in `market_state_published` (no migration). */
export const SLOW_UNIVERSE_ID = `${MARKET_UNIVERSE_ID}:slow-v1`;
export const SLOW_HORIZONS_MIN = [60, 240, 720] as const;
export const SLOW_GRID_STEP_MS = 15 * 60_000;
export const SLOW_GRID_POINTS = 672;
export const SLOW_MAX_SYMBOLS = 20;
/** Compute 20 s after each 15-minute boundary so the last 1m candle has settled. */
export const SLOW_SETTLE_MS = 20_000;
export const SLOW_RETRY_SPACING_MS = 30_000;
export const SLOW_MAX_ATTEMPTS = 3;

export type SlowHorizonMin = (typeof SLOW_HORIZONS_MIN)[number];

export const slowRequestSchema = z
  .object({
    boundaryMs: z.number().int().positive().refine((v) => v % SLOW_GRID_STEP_MS === 0),
    horizonsMin: z.array(z.union([z.literal(60), z.literal(240), z.literal(720)])).min(1).max(3),
    symbols: z
      .array(
        z.object({
          symbol: z.string().regex(/^[A-Z0-9]{2,32}$/),
          grid: z.array(z.number().finite().positive().nullable()).length(SLOW_GRID_POINTS),
        }).strict(),
      )
      .min(1)
      .max(SLOW_MAX_SYMBOLS),
  })
  .strict();
export type SlowMovementRequest = z.infer<typeof slowRequestSchema>;

const finite = z.number().finite();
const symbolRow = z
  .object({
    symbol: z.string().min(2).max(32),
    returnPct: finite.nullable(),
    z: finite.nullable(),
    status: z.enum(["ok", "collecting_history", "unavailable"]),
    referenceSamples: z.number().int().nonnegative(),
  })
  .strict();
const horizon = z
  .object({
    horizonMin: z.union([z.literal(60), z.literal(240), z.literal(720)]),
    included: z.number().int().nonnegative(),
    medianReturnPct: finite.nullable(),
    medianZ: finite.nullable(),
    up: z.number().int().nonnegative(),
    down: z.number().int().nonnegative(),
    neutral: z.number().int().nonnegative(),
    upShare: finite.nullable(),
    downShare: finite.nullable(),
    coordinated: z.enum(["rise", "fall"]).nullable(),
    symbols: z.array(symbolRow).max(SLOW_MAX_SYMBOLS),
  })
  .strict();

export const slowResultSchema = z
  .object({
    algorithmVersion: z.literal(SLOW_MOVEMENT_ALGORITHM_VERSION),
    configVersion: z.literal(SLOW_MOVEMENT_CONFIG_VERSION),
    configHash: z.string().regex(/^[0-9a-f]{16}$/),
    boundaryMs: z.number().int().positive(),
    config: z
      .object({
        gridPoints: z.number().int(),
        referenceSamples: z.number().int(),
        minReferenceSamples: z.number().int(),
        zThreshold: finite,
        coordinatedShare: finite,
        minIncluded: z.number().int(),
      })
      .strict(),
    horizons: z.array(horizon).min(1).max(3),
  })
  .strict();
export type SlowMovementResult = z.infer<typeof slowResultSchema>;
export type SlowHorizon = SlowMovementResult["horizons"][number];

/** What the machine publishes to the `${MARKET_UNIVERSE_ID}:slow-v1` row. */
export const slowPublishedSchema = z
  .object({
    version: z.literal(SLOW_PUBLISHED_VERSION),
    computedAt: z.number().int().positive(),
    result: slowResultSchema,
  })
  .strict();
export type SlowPublishedPayload = z.infer<typeof slowPublishedSchema>;

/** The 15-minute boundary at or before `now`. */
export const slowBoundaryAtOrBefore = (now: number) => now - (now % SLOW_GRID_STEP_MS);
