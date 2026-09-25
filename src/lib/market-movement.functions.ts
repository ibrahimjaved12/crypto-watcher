import { createServerFn } from "@tanstack/react-start";

import { requireSupabaseAuth } from "@/integrations/supabase/auth-middleware";
import {
  MARKET_UNIVERSE_ID,
  toMarketMovementCurrentState,
  type MarketMovementCurrentState,
} from "./market/market-movement-state";

export type { MarketMovementCurrentState };

/**
 * Authenticated server-side read of the bounded operational market-movement
 * current state for later #30/#31 consumers. Returns structured evidence only;
 * the service-role credential never leaves the server.
 */
export const getMarketMovementCurrentState = createServerFn({ method: "GET" })
  .middleware([requireSupabaseAuth])
  .handler(async (): Promise<MarketMovementCurrentState> => {
    const { getOperationalStore } = await import("./operational/repository.server");
    const store = getOperationalStore();
    if (!store.enabled) return toMarketMovementCurrentState(null, { now: Date.now() });
    const current = await store.getMarketStateCurrent(MARKET_UNIVERSE_ID).catch(() => null);
    return toMarketMovementCurrentState(current, { now: Date.now() });
  });
