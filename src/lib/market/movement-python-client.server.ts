import { z } from "zod";
import { pythonServiceConfig } from "../python-service.server";
import type {
  MovementBoundaryResult,
  MovementBoundarySymbolInput,
  MovementBucket,
  MovementBucketSnapshot,
  MovementWindowReadiness,
} from "./movement-contract";

const TIMEOUT_MS = 6_000;
const MAX_ATTEMPTS = 2;
const MAX_RESPONSE_BYTES = 4_000_000;
const timestamp = z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER).nullable();
const bucket = z
  .object({
    boundaryTime: z.number().int().nonnegative(),
    sourceState: z.enum(["LIVE", "RECOVERING", "STALE", "UNAVAILABLE"]),
    endpointPrice: z.number().finite().positive().nullable(),
    baseQuantity: z.number().finite().nonnegative(),
    quoteVolume: z.number().finite().nonnegative(),
    tradeCount: z.number().int().nonnegative(),
    lastRealTradeTime: timestamp,
    lastRealEventTime: timestamp,
    lastRealReceivedAt: timestamp,
    carriedForward: z.boolean(),
    provider: z.literal("binance-usdm"),
    instrumentId: z.string().min(3),
    nativeSymbol: z.string().min(2),
    symbol: z.string().min(2),
    marketType: z.literal("futures"),
    contractType: z.literal("perpetual"),
    priceType: z.literal("trade"),
  })
  .strict();
const readiness = z
  .object({
    windowMinutes: z.union([z.literal(1), z.literal(5), z.literal(15)]),
    status: z.enum(["READY", "WARMING", "STALE"]),
    state: z.enum(["ready", "warming", "stale", "missing_history", "unavailable"]),
    reason: z.string().max(80).nullable(),
  })
  .strict();
const snapshot = z
  .object({
    symbol: z.string().min(2),
    provider: z.literal("binance-usdm"),
    instrumentId: z.string().min(3),
    priceType: z.literal("trade"),
    bucketMs: z.literal(5_000),
    maxLastTradeAgeMs: z.literal(15_000),
    buckets: z.array(bucket).max(420),
    latestRealTradeTime: timestamp,
    latestRealReceivedAt: timestamp,
    readiness: z.record(z.enum(["1", "5", "15"]), readiness),
  })
  .strict();
const responseSchema = z
  .object({
    sessionId: z.string().uuid(),
    boundaryTime: z.number().int().nonnegative(),
    lateAfterFinalizationCount: z.number().int().nonnegative(),
    snapshots: z.array(snapshot).max(100),
  })
  .strict();

export async function advancePythonMovementBoundary(
  sessionId: string,
  boundaryTime: number,
  symbols: MovementBoundarySymbolInput[],
  env: Record<string, string | undefined> = process.env,
  send: typeof fetch = fetch,
): Promise<MovementBoundaryResult> {
  const config = pythonServiceConfig("/v1/movement/boundary", env);
  const body = JSON.stringify({
    schema_version: 1,
    session_id: sessionId,
    boundary_time_ms: boundaryTime,
    symbols: symbols.map((item) => ({
      symbol: item.symbol,
      instrument_id: `binance-usdm:${item.symbol}`,
      membership_epoch: item.membershipEpoch,
      source_state: item.sourceState,
      observations: item.observations.map((trade) => ({
        price: trade.price,
        quantity: trade.quantity,
        event_time_ms: trade.eventTime,
        trade_time_ms: trade.tradeTime,
        aggregate_trade_id: trade.aggregateId,
        received_at_ms: trade.receivedAt,
      })),
    })),
  });
  let lastFailure = "unavailable";
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    try {
      const response = await send(config.url, {
        method: "POST",
        redirect: "manual",
        signal: controller.signal,
        headers: {
          "Content-Type": "application/json",
          "Cache-Control": "no-store",
          Authorization: `Bearer ${config.token}`,
        },
        body,
      });
      if (!response.ok) {
        await response.body?.cancel().catch(() => undefined);
        if (attempt < MAX_ATTEMPTS && (response.status === 429 || response.status >= 500)) {
          lastFailure = `HTTP ${response.status}`;
          continue;
        }
        throw new Error(`Python movement service failed (${response.status})`);
      }
      const raw = await response.text();
      if (new TextEncoder().encode(raw).byteLength > MAX_RESPONSE_BYTES) {
        throw new Error("Python movement response is oversized");
      }
      let decoded: unknown;
      try {
        decoded = JSON.parse(raw);
      } catch {
        throw new Error("Python movement service returned invalid JSON");
      }
      const parsed = responseSchema.safeParse(decoded);
      if (
        !parsed.success ||
        parsed.data.sessionId !== sessionId ||
        parsed.data.boundaryTime !== boundaryTime ||
        parsed.data.snapshots.length !== symbols.length
      ) {
        throw new Error("Python movement service returned an invalid response");
      }
      for (const [index, item] of symbols.entries()) {
        const result = parsed.data.snapshots[index]!;
        if (result.symbol !== item.symbol || result.instrumentId !== `binance-usdm:${item.symbol}`) {
          throw new Error("Python movement service returned mismatched provenance");
        }
      }
      return {
        snapshots: parsed.data.snapshots as MovementBucketSnapshot[],
        lateAfterFinalizationCount: parsed.data.lateAfterFinalizationCount,
      };
    } catch (error) {
      lastFailure = controller.signal.aborted
        ? "timed out"
        : error instanceof Error
          ? error.message
          : String(error);
      if (attempt === MAX_ATTEMPTS || /invalid|oversized|mismatched|failed \(4\d\d\)/.test(lastFailure)) {
        break;
      }
    } finally {
      clearTimeout(timer);
    }
  }
  throw new Error(`Python movement ${lastFailure}`);
}

export type { MovementBucket, MovementBucketSnapshot, MovementWindowReadiness };
