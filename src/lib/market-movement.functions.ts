import { createServerFn } from "@tanstack/react-start";

import { requireSupabaseAuth } from "@/integrations/supabase/auth-middleware";
import {
  MARKET_UNIVERSE_ID,
  toMarketMovementCurrentState,
  type MarketMovementCurrentState,
} from "./market/market-movement-state";
import type { MovementReadStatus } from "./market/assessment";

export type { MarketMovementCurrentState };
/** The persisted state plus whether it could be read at all (the dashboard names the difference). */
export type MarketMovementRead = MarketMovementCurrentState & { readStatus: MovementReadStatus };

/**
 * Authenticated server-side read of the bounded operational market-movement
 * current state for later #30/#31 consumers. Returns structured evidence only;
 * the service-role credential never leaves the server.
 */
export const getMarketMovementCurrentState = createServerFn({ method: "GET" })
  .middleware([requireSupabaseAuth])
  .handler(async (): Promise<MarketMovementRead> => {
    const { getOperationalStore } = await import("./operational/repository.server");
    const store = getOperationalStore();
    if (!store.enabled) {
      return { ...toMarketMovementCurrentState(null, { now: Date.now() }), readStatus: "store_disabled" };
    }
    let failed = false;
    const current = await store.getMarketStateCurrent(MARKET_UNIVERSE_ID).catch(() => {
      failed = true;
      return null;
    });
    return { ...toMarketMovementCurrentState(current, { now: Date.now() }), readStatus: failed ? "read_failed" : "ok" };
  });
