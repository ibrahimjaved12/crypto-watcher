export type FreshnessState = "FRESH" | "DELAYED" | "STALE" | "UNAVAILABLE";

/** Derives health only from a persisted source/domain timestamp and an explicit clock. */
export function timestampFreshness(
  timestamp: string | null,
  options: {
    available: boolean;
    now: number;
    freshForMs: number;
    delayedForMs: number;
  },
): FreshnessState {
  if (!options.available || !timestamp) return "UNAVAILABLE";
  const time = Date.parse(timestamp);
  if (!Number.isFinite(time)) return "UNAVAILABLE";
  const age = Math.max(0, options.now - time);
  if (age <= options.freshForMs) return "FRESH";
  if (age <= options.delayedForMs) return "DELAYED";
  return "STALE";
}
