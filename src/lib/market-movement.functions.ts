import { createServerFn } from "@tanstack/react-start";

import { requireMarketAccess } from "@/integrations/supabase/auth-middleware";
import {
  MARKET_UNIVERSE_ID,
  toMarketMovementCurrentState,
  type MarketMovementCurrentState,
} from "./market/market-movement-state";
import { readPublishedState, type MarketMovementRead } from "./market/movement-published";
import { SLOW_UNIVERSE_ID } from "./market/slow-movement-contract";
import { readSlowRow, type SlowMovementRead } from "./market/slow-movement-view";

export type { MarketMovementCurrentState, MarketMovementRead };

/**
 * Authenticated server-side read of the bounded operational market-movement
 * current state for later #30/#31 consumers. Returns structured evidence only;
 * the service-role credential never leaves the server.
 */
export const getMarketMovementCurrentState = createServerFn({ method: "GET" })
  .middleware([requireMarketAccess])
  .handler(async ({ context }): Promise<MarketMovementRead> => {
    const { getOperationalStore } = await import("./operational/repository.server");
    const store = getOperationalStore();
    if (!store.enabled) {
      // The deployed site cannot reach the machine's operational database: read the copy the machine publishes
      // (one market-wide row, RLS read for signed-in users; no service key involved).
      const { data, error } = await context.supabase
        .from("market_state_published")
        .select("payload, published_at")
        .eq("universe_id", MARKET_UNIVERSE_ID)
        .maybeSingle();
      if (error) {
        return { ...toMarketMovementCurrentState(null, { now: Date.now() }), readStatus: "read_failed", publishedAt: null };
      }
      return readPublishedState(data, Date.now());
    }
    let failed = false;
    const current = await store.getMarketStateCurrent(MARKET_UNIVERSE_ID).catch(() => {
      failed = true;
      return null;
    });
    return {
      ...toMarketMovementCurrentState(current, { now: Date.now() }),
      readStatus: failed ? "read_failed" : "ok",
      publishedAt: null,
    };
  });

/**
 * "Longer moves" (slow-movement-v1): the published `${MARKET_UNIVERSE_ID}:slow-v1` row, read with the caller's own
 * session (RLS read for signed-in users). A missing row means the machine is still collecting history; it is never
 * an error. The page grades freshness itself against its own clock (`slowStatus`).
 */
export const getSlowMovementState = createServerFn({ method: "GET" })
  .middleware([requireMarketAccess])
  .handler(async ({ context }): Promise<SlowMovementRead> => {
    const { data, error } = await context.supabase
      .from("market_state_published")
      .select("payload, published_at")
      .eq("universe_id", SLOW_UNIVERSE_ID)
      .maybeSingle();
    if (error) return { read: "failed", result: null, boundaryMs: null, publishedAt: null };
    return readSlowRow(data);
  });
