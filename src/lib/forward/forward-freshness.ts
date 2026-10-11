export const FORWARD_STALE_AFTER_MS = 2 * 60 * 60 * 1000;

/** Use the processed run boundary, not a new retry's insertion time. */
export function forwardDataStale(boundaryMs: number | null | undefined, now: number): boolean {
  return typeof boundaryMs === "number" && Number.isFinite(boundaryMs) &&
    now - boundaryMs > FORWARD_STALE_AFTER_MS;
}
