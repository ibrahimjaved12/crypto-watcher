import { createServerFn } from "@tanstack/react-start";

import { requireSupabaseAuth } from "@/integrations/supabase/auth-middleware";
import {
  MARKET_UNIVERSE_ID,
  toMarketMovementCurrentState,
  type MarketMovementCurrentState,
} from "./market/market-movement-state";
import { readPublishedState, type MarketMovementRead } from "./market/movement-published";

export type { MarketMovementCurrentState, MarketMovementRead };

/**
 * Authenticated server-side read of the bounded operational market-movement
 * current state for later #30/#31 consumers. Returns structured evidence only;
 * the service-role credential never leaves the server.
 */
export const getMarketMovementCurrentState = createServerFn({ method: "GET" })
  .middleware([requireSupabaseAuth])
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
