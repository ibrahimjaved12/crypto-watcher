/**
 * Orchestration of "Longer moves" (slow-movement-v1, #30 / #373): builds the 15-minute price grid from completed
 * 1m candles, asks the Python descriptor for the math, validates the answer and wraps it as the published payload.
 * No analytics here and no TypeScript fallback: if Python fails, nothing is published for that boundary.
 */
import { pythonServiceConfig } from "../python-service.server";
import type { MovementCandle } from "./market-movement-state";
import {
  SLOW_GRID_POINTS,
  SLOW_GRID_STEP_MS,
  SLOW_HORIZONS_MIN,
  SLOW_MAX_SYMBOLS,
  SLOW_PUBLISHED_VERSION,
  slowRequestSchema,
  slowResultSchema,
  type SlowMovementRequest,
  type SlowMovementResult,
  type SlowPublishedPayload,
} from "./slow-movement-contract";

const MINUTE_MS = 60_000;
const LOOKBACK_MS = 7 * 24 * 60 * MINUTE_MS;
const TIMEOUT_MS = 6_000;
const MAX_ATTEMPTS = 2;
const MAX_RESPONSE_BYTES = 1_000_000;

/**
 * g_k = close of the completed 1m candle whose openTime is B - 15 min x k - 60 s (the price at B - 15 min x k),
 * or null when that candle is missing. Candles that close after B are ignored; nothing is filled.
 */
export function buildSlowGrid(candles: readonly MovementCandle[], boundaryMs: number): Array<number | null> {
  const closeByOpen = new Map<number, number>();
  for (const candle of candles) {
    if (candle.openTime + MINUTE_MS <= boundaryMs && Number.isFinite(candle.close) && candle.close > 0) {
      closeByOpen.set(candle.openTime, candle.close);
    }
  }
  return Array.from({ length: SLOW_GRID_POINTS }, (_, k) => closeByOpen.get(boundaryMs - SLOW_GRID_STEP_MS * k - MINUTE_MS) ?? null);
}

export type SlowMovementDeps = {
  /** The included symbols of the current persisted V1 movement state (the engine's own universe). */
  readUniverse(): Promise<string[]>;
  /** Completed 1m candles of one symbol with openTime >= sinceMs and openTime + 60 s <= boundaryMs. */
  readCandles(symbol: string, sinceMs: number, boundaryMs: number): Promise<MovementCandle[]>;
  evaluate(request: SlowMovementRequest): Promise<SlowMovementResult>;
  now(): number;
};

/** One evaluation at the 15-minute boundary `boundaryMs`; throws on any failure (the caller retries). */
export async function computeSlowMovement(deps: SlowMovementDeps, boundaryMs: number): Promise<SlowPublishedPayload | null> {
  if (boundaryMs % SLOW_GRID_STEP_MS !== 0) throw new Error("slow movement boundary must be a multiple of 15 minutes");
  const symbols = [...new Set((await deps.readUniverse()).map((s) => s.toUpperCase()))].sort().slice(0, SLOW_MAX_SYMBOLS);
  if (symbols.length === 0) return null;
  const sinceMs = boundaryMs - LOOKBACK_MS - 2 * MINUTE_MS;
  const grids: SlowMovementRequest["symbols"] = [];
  // One symbol at a time keeps each read to about 10,000 rows.
  for (const symbol of symbols) {
    grids.push({ symbol, grid: buildSlowGrid(await deps.readCandles(symbol, sinceMs, boundaryMs), boundaryMs) });
  }
  const request = slowRequestSchema.parse({ boundaryMs, horizonsMin: [...SLOW_HORIZONS_MIN], symbols: grids });
  const result = await deps.evaluate(request);
  if (result.boundaryMs !== boundaryMs) throw new Error("Python slow movement answered a different boundary");
  for (const h of result.horizons) {
    if (h.symbols.length !== symbols.length || h.symbols.some((row, i) => row.symbol !== symbols[i])) {
      throw new Error("Python slow movement answered mismatched symbols");
    }
  }
  return { version: SLOW_PUBLISHED_VERSION, computedAt: deps.now(), result };
}

/** POST /v1/movement/slow on the analysis service: 6 s timeout, 2 attempts, response byte cap. */
export async function evaluatePythonSlowMovement(
  request: SlowMovementRequest,
  env: Record<string, string | undefined> = process.env,
  send: typeof fetch = fetch,
): Promise<SlowMovementResult> {
  const config = pythonServiceConfig("/v1/movement/slow", env);
  const body = JSON.stringify(request);
  let lastFailure = "unavailable";
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    try {
      const response = await send(config.url, {
        method: "POST",
        redirect: "manual",
        signal: controller.signal,
        headers: { "Content-Type": "application/json", "Cache-Control": "no-store", Authorization: `Bearer ${config.token}` },
        body,
      });
      if (!response.ok) {
        await response.body?.cancel().catch(() => undefined);
        if (attempt < MAX_ATTEMPTS && (response.status === 429 || response.status >= 500)) {
          lastFailure = `HTTP ${response.status}`;
          continue;
        }
        throw new Error(`service failed (${response.status})`);
      }
      const raw = await response.text();
      if (new TextEncoder().encode(raw).byteLength > MAX_RESPONSE_BYTES) throw new Error("response is oversized");
      let decoded: unknown;
      try {
        decoded = JSON.parse(raw);
      } catch {
        throw new Error("service returned invalid JSON");
      }
      const parsed = slowResultSchema.safeParse(decoded);
      if (!parsed.success) throw new Error("service returned an invalid response");
      return parsed.data;
    } catch (error) {
      lastFailure = controller.signal.aborted ? "timed out" : error instanceof Error ? error.message : String(error);
      if (attempt === MAX_ATTEMPTS || /invalid|oversized|failed \(4\d\d\)/.test(lastFailure)) break;
    } finally {
      clearTimeout(timer);
    }
  }
  throw new Error(`Python slow movement ${lastFailure}`);
}
