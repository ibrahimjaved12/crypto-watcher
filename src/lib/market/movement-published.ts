/**
 * Published copy of the movement engine's persisted current state (#30). Pure and client-safe: payload building
 * and validation, the hosted read mapping, and the site hook's handler (with injected dependencies).
 *
 * The machine publishes the SAME payload `getMarketMovementCurrentState` maps from (the persisted current state
 * without its lifecycle internals) to one hosted row per universe, overwritten in place. Market data only.
 */
import {
  MARKET_UNIVERSE_ID,
  toMarketMovementCurrentState,
  type MarketMovementCurrentState,
  type PersistedCurrentLike,
} from "./market-movement-state";
import type { MovementReadStatus } from "./assessment";
import {
  SLOW_GRID_STEP_MS,
  SLOW_UNIVERSE_ID,
  slowPublishedSchema,
  type SlowPublishedPayload,
} from "./slow-movement-contract";

export const PUBLISHED_STATE_VERSION = "market-state-published-v1";
export const MOVEMENT_HOOK_PATH = "/api/public/hooks/market-state-publish";
/** The payload (a few windows x a few pairs) is far below this; the hosted row is bounded to 512 KiB too. */
export const PUBLISHED_PAYLOAD_MAX_BYTES = 524_288;
export const MOVEMENT_HOOK_BODY_CAP = 600_000;

/** What the machine sends and the hosted row stores: the mapper's input, without lifecycle internals. */
export function toPublishedPayload(current: PersistedCurrentLike & Record<string, unknown>): PersistedCurrentLike {
  return {
    universeId: current.universeId,
    universeVersion: current.universeVersion,
    evaluationBoundaryTime: current.evaluationBoundaryTime,
    directionState: current.directionState,
    pace: current.pace,
    activeEpisodeId: current.activeEpisodeId,
    activeDirection: current.activeDirection,
    interrupted: current.interrupted,
    episodeAlgorithmVersion: current.episodeAlgorithmVersion,
    lifecycleConfigVersion: current.lifecycleConfigVersion,
    classifierAlgorithmVersion: current.classifierAlgorithmVersion,
    classifierConfigVersion: current.classifierConfigVersion,
    movementAlgorithmVersion: current.movementAlgorithmVersion,
    movementConfigVersion: current.movementConfigVersion,
    currentEvidence: current.currentEvidence,
  };
}

const isObject = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);

/** Shape check of a payload (published or read back): enough for the mapper not to throw. Null when malformed. */
export function parsePublishedPayload(value: unknown): PersistedCurrentLike | null {
  if (!isObject(value)) return null;
  const evidence = value["currentEvidence"];
  if (
    typeof value["universeId"] !== "string" || typeof value["universeVersion"] !== "string" ||
    typeof value["evaluationBoundaryTime"] !== "number" || !Number.isFinite(value["evaluationBoundaryTime"]) ||
    typeof value["directionState"] !== "string" || typeof value["pace"] !== "string" ||
    !isObject(evidence) || !isObject(evidence["universe"]) || !Array.isArray(evidence["windowsContext"]) ||
    !Array.isArray((evidence["universe"] as Record<string, unknown>)["configuredSymbols"]) ||
    !isObject(evidence["timestamps"])
  ) {
    return null;
  }
  return value as unknown as PersistedCurrentLike;
}

export type MarketMovementRead = MarketMovementCurrentState & {
  readStatus: MovementReadStatus;
  /** For a published copy: when the machine last reported (publish or heartbeat), ms; else null. */
  publishedAt: number | null;
};

const emptyState = (now: number) => toMarketMovementCurrentState(null, { now });

/**
 * Maps the hosted row to the consumer contract. The engine's own STALE rule applies to the evaluation time as the
 * machine saw it: the mapper's clock is `published_at`, so the publish and read delays never make a healthy engine
 * look stale. The page separately compares `publishedAt` with the machine-offline window. A missing row and a
 * malformed payload are named, never shown as live.
 */
export function readPublishedState(
  row: { payload: unknown; published_at: string } | null,
  now: number,
): MarketMovementRead {
  if (!row) return { ...emptyState(now), readStatus: "published_none", publishedAt: null };
  const payload = parsePublishedPayload(row.payload);
  const publishedAt = Date.parse(row.published_at);
  if (!payload || !Number.isFinite(publishedAt)) return { ...emptyState(now), readStatus: "read_failed", publishedAt: null };
  return { ...toMarketMovementCurrentState(payload, { now: publishedAt }), readStatus: "published", publishedAt };
}

// ---------------------------------------------------------------- the site hook

export type MovementHookInput =
  | { op: "publish"; universeId: string; evaluationBoundaryMs: number; payload: PersistedCurrentLike }
  | { op: "heartbeat"; universeId: string }
  | { op: "publish_slow"; universeId: string; evaluationBoundaryMs: number; payload: SlowPublishedPayload };

export function parseMovementHookInput(value: unknown): MovementHookInput | null {
  if (isObject(value) && value["op"] === "publish_slow") {
    // "Longer moves" (slow-movement-v1): its own row beside the V1 row; no heartbeat for it.
    if (value["universeId"] !== SLOW_UNIVERSE_ID) return null;
    const boundary = value["evaluationBoundaryMs"];
    if (!Number.isSafeInteger(boundary) || (boundary as number) <= 0 || (boundary as number) % SLOW_GRID_STEP_MS !== 0) return null;
    const parsed = slowPublishedSchema.safeParse(value["payload"]);
    if (!parsed.success || parsed.data.result.boundaryMs !== boundary) return null;
    if (JSON.stringify(parsed.data).length > PUBLISHED_PAYLOAD_MAX_BYTES) return null;
    return { op: "publish_slow", universeId: SLOW_UNIVERSE_ID, evaluationBoundaryMs: boundary as number, payload: parsed.data };
  }
  if (!isObject(value) || value["universeId"] !== MARKET_UNIVERSE_ID) return null;
  if (value["op"] === "heartbeat") return { op: "heartbeat", universeId: MARKET_UNIVERSE_ID };
  if (value["op"] !== "publish") return null;
  const boundary = value["evaluationBoundaryMs"];
  const payload = parsePublishedPayload(value["payload"]);
  if (!Number.isSafeInteger(boundary) || (boundary as number) <= 0 || !payload) return null;
  // The payload must describe the same universe and evaluation the envelope announces.
  if (payload.universeId !== MARKET_UNIVERSE_ID || payload.evaluationBoundaryTime !== boundary) return null;
  if (JSON.stringify(payload).length > PUBLISHED_PAYLOAD_MAX_BYTES) return null;
  return { op: "publish", universeId: MARKET_UNIVERSE_ID, evaluationBoundaryMs: boundary as number, payload };
}

export type MovementHookDeps = {
  rpc(name: string, args: Record<string, unknown>): Promise<{ data: unknown; error: { message: string } | null }>;
};

export async function handleMovementHook(
  deps: MovementHookDeps,
  input: MovementHookInput,
): Promise<{ status: number; body: Record<string, unknown> }> {
  try {
    if (input.op === "heartbeat") {
      const { data, error } = await deps.rpc("touch_market_state", { p_universe_id: input.universeId });
      if (error) return { status: 500, body: { ok: false, category: "internal", error: "Could not record the heartbeat." } };
      // touched:false = no row yet (or it was removed): the publisher sends a full publish next.
      return { status: 200, body: { ok: true, touched: data === true } };
    }
    const { data, error } = await deps.rpc("publish_market_state", {
      p_universe_id: input.universeId, p_boundary_ms: input.evaluationBoundaryMs, p_payload: input.payload,
    });
    if (error) return { status: 500, body: { ok: false, category: "internal", error: "Could not publish the state." } };
    // stored:false = an equal-or-newer evaluation was already stored; harmless.
    return { status: 200, body: { ok: true, stored: data === true } };
  } catch {
    return { status: 500, body: { ok: false, category: "internal", error: "Request failed." } };
  }
}
