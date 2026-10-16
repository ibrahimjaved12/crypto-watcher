/**
 * Client-safe view model of "Longer moves" (slow-movement-v1). Maps the published row to what the dashboard shows.
 * A descriptor of the current market, not a forecast.
 */
import {
  SLOW_GRID_STEP_MS,
  slowPublishedSchema,
  type SlowHorizon,
  type SlowMovementResult,
} from "./slow-movement-contract";

/** Within one boundary plus 5 minutes the reading is current; up to 15 minutes beyond that it is catching up. */
export const SLOW_CURRENT_MS = SLOW_GRID_STEP_MS + 5 * 60_000;
export const SLOW_CATCHING_UP_MS = SLOW_CURRENT_MS + 15 * 60_000;
/** A late reading turns amber only after this long. */
export const SLOW_ATTENTION_MS = 60 * 60_000;

export type SlowStatus = "current" | "catching_up" | "late" | "warming" | "unavailable";

export type SlowMovementRead = {
  /** "missing" = no row yet (collecting history), "failed" = the read failed or the row is malformed. */
  read: "ok" | "missing" | "failed";
  result: SlowMovementResult | null;
  boundaryMs: number | null;
  publishedAt: number | null;
};

/** Maps the hosted row (payload + published_at) to the read model; never throws. */
export function readSlowRow(row: { payload: unknown; published_at: string } | null): SlowMovementRead {
  if (!row) return { read: "missing", result: null, boundaryMs: null, publishedAt: null };
  const parsed = slowPublishedSchema.safeParse(row.payload);
  const publishedAt = Date.parse(row.published_at);
  if (!parsed.success || !Number.isFinite(publishedAt)) return { read: "failed", result: null, boundaryMs: null, publishedAt: null };
  return { read: "ok", result: parsed.data.result, boundaryMs: parsed.data.result.boundaryMs, publishedAt };
}

export function slowStatus(read: SlowMovementRead | undefined, now: number): SlowStatus {
  if (!read || read.read === "missing") return "warming";
  if (read.read === "failed" || read.boundaryMs === null) return "unavailable";
  const age = now - read.boundaryMs;
  if (age <= SLOW_CURRENT_MS) return "current";
  if (age <= SLOW_CATCHING_UP_MS) return "catching_up";
  return "late";
}

export const SLOW_HORIZON_LABEL: Record<number, { long: string; short: string; tile: string }> = {
  60: { long: "1 hour", short: "1h", tile: "1 HOUR" },
  240: { long: "4 hours", short: "4h", tile: "4 HOURS" },
  720: { long: "12 hours", short: "12h", tile: "12 HOURS" },
};

/** "Very unusual" when |median z| >= 3, "Unusual" when >= 2, else "Normal". */
export function slowUnusualWord(medianZ: number | null): string | null {
  if (medianZ === null) return null;
  const size = Math.abs(medianZ);
  return size >= 3 ? "Very unusual" : size >= 2 ? "Unusual" : "Normal";
}

/** "6 of 9 pairs fell" in the direction of the median move, from the included pairs' own returns. */
export function slowBreadthLine(h: SlowHorizon): string {
  const ok = h.symbols.filter((row) => row.status === "ok" && row.returnPct !== null);
  if (ok.length === 0) return "No pair has enough history yet";
  const falling = (h.medianReturnPct ?? 0) < 0;
  const count = ok.filter((row) => (falling ? row.returnPct! < 0 : row.returnPct! > 0)).length;
  return `${count} of ${ok.length} pairs ${falling ? "fell" : "rose"}`;
}

/** "Most pairs are falling together over 4 hours", or null when not coordinated. */
export function slowCoordinatedSentence(h: SlowHorizon): string | null {
  if (!h.coordinated) return null;
  return `Most pairs are ${h.coordinated === "fall" ? "falling" : "rising"} together over ${SLOW_HORIZON_LABEL[h.horizonMin]?.long ?? `${h.horizonMin} min`}`;
}

/** Per pair: return and z for each horizon (null where the pair is not included). */
export function slowPairRows(result: SlowMovementResult | null, symbol: string) {
  if (!result) return [];
  return result.horizons.map((h) => {
    const row = h.symbols.find((s) => s.symbol === symbol);
    return {
      horizonMin: h.horizonMin,
      label: SLOW_HORIZON_LABEL[h.horizonMin]?.short ?? `${h.horizonMin}m`,
      returnPct: row?.status === "ok" ? row.returnPct : null,
      z: row?.status === "ok" ? row.z : null,
      status: row?.status ?? null,
    };
  });
}
