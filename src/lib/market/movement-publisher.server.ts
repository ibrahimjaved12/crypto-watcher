/**
 * Machine-side publisher of the movement engine's persisted current state (#30). A sibling child of the worker
 * host, like the quote publisher (#323): it reads local operational state only, makes no exchange call, runs on a
 * fixed cadence and catches every failure so it can never block the engine. Unlike the quote publisher, which writes
 * with the hosted service key, it follows the alert-chart upload hook (#371): it POSTs small JSON to a site hook
 * authenticated with the shared machine secret (RUNNER_GATEWAY_SECRET), and the site writes the hosted row. The
 * machine holds no hosted service key for this.
 */
import { MARKET_UNIVERSE_ID, MOVEMENT_PUBLISH_INTERVAL_MS } from "./market-movement-state";
import { MOVEMENT_HOOK_PATH, toPublishedPayload } from "./movement-published";
import type { PersistedMarketStateCurrent } from "../operational/types";
import {
  SLOW_MAX_ATTEMPTS,
  SLOW_RETRY_SPACING_MS,
  SLOW_SETTLE_MS,
  SLOW_UNIVERSE_ID,
  slowBoundaryAtOrBefore,
  type SlowPublishedPayload,
} from "./slow-movement-contract";

export type MovementPublisherDeps = {
  read(): Promise<PersistedMarketStateCurrent | null>;
  /** POSTs one hook body; resolves with the HTTP status and JSON (never throws for a non-2xx status). */
  send(body: Record<string, unknown>): Promise<{ status: number; json: Record<string, unknown> }>;
  now?: () => number;
};

export type PublisherState = {
  /** Evaluation boundary of the last state the site accepted, for this process. */
  lastBoundary: number | null;
  /** Time of the last attempt (success or failure): at most one attempt per interval. */
  lastAttemptAt: number | null;
};

export type PublishOutcome =
  | { kind: "published"; boundary: number }
  | { kind: "heartbeat" }
  | { kind: "too_soon" }
  | { kind: "no_state" }
  | { kind: "failed"; error: string };

export const newPublisherState = (): PublisherState => ({ lastBoundary: null, lastAttemptAt: null });

/**
 * One pass. A changed evaluation boundary publishes the payload; an unchanged one sends only a tiny heartbeat
 * (the payload is skipped, but the site still learns the machine is alive, so a stalled engine reads as STALE
 * and a dead machine reads as offline). Never throws.
 */
export async function publishMovementOnce(deps: MovementPublisherDeps, state: PublisherState): Promise<PublishOutcome> {
  const now = (deps.now ?? Date.now)();
  if (state.lastAttemptAt !== null && now - state.lastAttemptAt < MOVEMENT_PUBLISH_INTERVAL_MS) return { kind: "too_soon" };
  state.lastAttemptAt = now;
  try {
    const current = await deps.read();
    if (!current) return { kind: "no_state" };
    const boundary = current.evaluationBoundaryTime;
    if (state.lastBoundary !== null && boundary === state.lastBoundary) {
      const reply = await deps.send({ op: "heartbeat", universeId: MARKET_UNIVERSE_ID });
      if (reply.status !== 200 || reply.json["ok"] !== true) return { kind: "failed", error: `site answered ${reply.status}` };
      if (reply.json["touched"] === false) state.lastBoundary = null; // the hosted row is gone: publish in full next
      return { kind: "heartbeat" };
    }
    const reply = await deps.send({
      op: "publish", universeId: MARKET_UNIVERSE_ID, evaluationBoundaryMs: boundary,
      payload: toPublishedPayload(current as unknown as Parameters<typeof toPublishedPayload>[0]),
    });
    if (reply.status !== 200 || reply.json["ok"] !== true) return { kind: "failed", error: `site answered ${reply.status}` };
    state.lastBoundary = boundary; // remembered only after the site accepted it, so a failure is retried
    return { kind: "published", boundary };
  } catch (error) {
    return { kind: "failed", error: (error instanceof Error ? error.message : String(error)).replace(/\s+/g, " ").slice(0, 200) };
  }
}

export type MovementPublisherConfig = { origin: string; secret: string };

/** Site origin: MOVEMENT_STATE_HOSTED_URL, else the alert-chart one (same site). Null when not configured. */
export function movementPublisherConfig(env: Record<string, string | undefined> = process.env): MovementPublisherConfig | null {
  if (env["MOVEMENT_STATE_PUBLISH_ENABLED"] === "false") return null;
  const raw = (env["MOVEMENT_STATE_HOSTED_URL"] ?? env["ALERT_CHART_HOSTED_URL"])?.trim();
  if (!raw) return null;
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new Error("MOVEMENT_STATE_HOSTED_URL must be a URL");
  }
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if ((url.protocol !== "https:" && !(url.protocol === "http:" && local)) || url.username || url.password
    || url.search || url.hash || url.pathname !== "/") {
    throw new Error("MOVEMENT_STATE_HOSTED_URL must be an https origin without a path");
  }
  const secret = env["RUNNER_GATEWAY_SECRET"] ?? "";
  if (!/^[A-Za-z0-9_-]{32,256}$/.test(secret)) throw new Error("Publishing the market state needs RUNNER_GATEWAY_SECRET");
  return { origin: url.origin, secret };
}

export function hostSender(config: MovementPublisherConfig, fetchImpl: typeof fetch = (...args) => fetch(...args)): MovementPublisherDeps["send"] {
  return async (body) => {
    const response = await fetchImpl(`${config.origin}${MOVEMENT_HOOK_PATH}`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-runner-secret": config.secret },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(15_000),
    });
    let json: Record<string, unknown> = {};
    try { json = (await response.json()) as Record<string, unknown>; } catch { /* non-JSON error page */ }
    return { status: response.status, json };
  };
}

// ---------------------------------------------------------------- "Longer moves" (slow-movement-v1)

export type SlowPublisherDeps = {
  compute(boundaryMs: number): Promise<SlowPublishedPayload | null>;
  send(body: Record<string, unknown>): Promise<{ status: number; json: Record<string, unknown> }>;
  now?: () => number;
};

export type SlowPublisherState = {
  /** The 15-minute boundary already published (or with nothing to publish). */
  doneBoundary: number | null;
  /** The boundary the attempts below belong to. */
  attemptBoundary: number | null;
  attempts: number;
  nextAttemptAt: number;
};

export type SlowPublishOutcome =
  | { kind: "idle" }
  | { kind: "published"; boundary: number }
  | { kind: "no_universe"; boundary: number }
  | { kind: "retrying"; boundary: number; error: string }
  | { kind: "failed"; boundary: number; error: string };

export const newSlowPublisherState = (): SlowPublisherState => ({
  doneBoundary: null, attemptBoundary: null, attempts: 0, nextAttemptAt: 0,
});

/** Off only when SLOW_MOVEMENT_ENABLED is exactly "false" (compute and publish both stop). */
export const slowMovementEnabled = (env: Record<string, string | undefined> = process.env) =>
  env["SLOW_MOVEMENT_ENABLED"] !== "false";

/**
 * One tick of the slow descriptor: at most once per 15-minute boundary, first 20 s after it, up to 3 attempts 30 s
 * apart, then wait for the next boundary. Independent of the V1 publish state, never throws, and only the final
 * failure of a boundary is reported as "failed" (the caller logs that once).
 */
export async function publishSlowOnce(deps: SlowPublisherDeps, state: SlowPublisherState): Promise<SlowPublishOutcome> {
  const now = (deps.now ?? Date.now)();
  const boundary = slowBoundaryAtOrBefore(now);
  if (state.doneBoundary === boundary || now < boundary + SLOW_SETTLE_MS) return { kind: "idle" };
  if (state.attemptBoundary !== boundary) {
    state.attemptBoundary = boundary;
    state.attempts = 0;
    state.nextAttemptAt = boundary + SLOW_SETTLE_MS;
  }
  if (state.attempts >= SLOW_MAX_ATTEMPTS || now < state.nextAttemptAt) return { kind: "idle" };
  state.attempts += 1;
  state.nextAttemptAt = now + SLOW_RETRY_SPACING_MS;
  try {
    const payload = await deps.compute(boundary);
    if (!payload) {
      state.doneBoundary = boundary;
      return { kind: "no_universe", boundary };
    }
    const reply = await deps.send({ op: "publish_slow", universeId: SLOW_UNIVERSE_ID, evaluationBoundaryMs: boundary, payload });
    if (reply.status !== 200 || reply.json["ok"] !== true) throw new Error(`site answered ${reply.status}`);
    state.doneBoundary = boundary;
    return { kind: "published", boundary };
  } catch (error) {
    const message = (error instanceof Error ? error.message : String(error)).replace(/\s+/g, " ").slice(0, 200);
    return state.attempts >= SLOW_MAX_ATTEMPTS
      ? { kind: "failed", boundary, error: message }
      : { kind: "retrying", boundary, error: message };
  }
}
