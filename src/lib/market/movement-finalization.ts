/**
 * Live 5-second finalization watermark for the market-movement engine (Issue #74).
 *
 * #70's `advanceTo(boundary)` permanently finalizes exchange-time buckets. The
 * live path must therefore delay finalization by an explicit grace so a quiet or
 * slightly-late contract cannot have a still-open bucket closed early. Wall clock
 * decides only *when* an exchange-time bucket is safe; the bucket identity stays
 * exchange/event-time based.
 */
import { MOVEMENT_BUCKET_MS } from "./movement-buckets";

export const MOVEMENT_FINALIZATION_CONFIG_VERSION = "movement-finalization-config-v1";

/** V1 operating default. This is a lateness watermark, not a trading threshold. */
export const MOVEMENT_FINALIZATION_GRACE_MS_DEFAULT = 2_000;
export const MOVEMENT_FINALIZATION_GRACE_MS_MIN = 0;
export const MOVEMENT_FINALIZATION_GRACE_MS_MAX = 60_000;

export type MovementFinalizationConfig = {
  version: string;
  graceMs: number;
};

export const DEFAULT_MOVEMENT_FINALIZATION_CONFIG: Readonly<MovementFinalizationConfig> = {
  version: MOVEMENT_FINALIZATION_CONFIG_VERSION,
  graceMs: MOVEMENT_FINALIZATION_GRACE_MS_DEFAULT,
};

/** Server-side, explicit and versioned. Never a browser-provided value. */
export function movementFinalizationConfig(
  env: Record<string, string | undefined>,
): MovementFinalizationConfig {
  const raw = env["MOVEMENT_FINALIZATION_GRACE_MS"];
  const graceMs =
    raw === undefined || raw === "" ? MOVEMENT_FINALIZATION_GRACE_MS_DEFAULT : Number(raw);
  if (
    !Number.isInteger(graceMs) ||
    graceMs < MOVEMENT_FINALIZATION_GRACE_MS_MIN ||
    graceMs > MOVEMENT_FINALIZATION_GRACE_MS_MAX
  ) {
    throw new Error(
      `MOVEMENT_FINALIZATION_GRACE_MS must be an integer ${MOVEMENT_FINALIZATION_GRACE_MS_MIN}–${MOVEMENT_FINALIZATION_GRACE_MS_MAX}`,
    );
  }
  return { version: MOVEMENT_FINALIZATION_CONFIG_VERSION, graceMs };
}

/**
 * Latest exchange-time boundary that is safe to finalize.
 *
 * `floor((wallClockNow - grace) / 5000) * 5000`. At exactly a boundary B the
 * result is B - 5000, so the current wall-clock boundary is never finalized
 * immediately.
 */
export function finalizableMovementBoundary(
  wallClockNowMs: number,
  config: MovementFinalizationConfig = DEFAULT_MOVEMENT_FINALIZATION_CONFIG,
): number {
  if (!Number.isSafeInteger(wallClockNowMs) || wallClockNowMs < 0) {
    throw new Error("invalid movement finalization wall clock");
  }
  if (!Number.isInteger(config.graceMs) || config.graceMs < 0) {
    throw new Error("invalid movement finalization grace");
  }
  const shifted = wallClockNowMs - config.graceMs;
  if (shifted < 0) {
    throw new Error("movement wall clock is before the finalization grace");
  }
  return Math.floor(shifted / MOVEMENT_BUCKET_MS) * MOVEMENT_BUCKET_MS;
}
