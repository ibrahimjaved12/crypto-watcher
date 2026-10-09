import { createServerFn } from "@tanstack/react-start";
import { requireSupabaseAuth } from "@/integrations/supabase/auth-middleware";
import { loadForwardDashboard } from "./forward/forward-dashboard";

/** On-demand forward run for the signed-in account (#239 P11); the hourly job uses the same path. */
export const runForwardNow = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .handler(async ({ context }) => {
    const { forwardDeps, FORWARD_STRATEGY_IDS } = await import("./forward/forward-deps.server");
    const { runForward } = await import("./forward/forward-run.server");
    return runForward(await forwardDeps(), {
      userId: context.userId,
      trigger: "on_demand",
      strategyIds: FORWARD_STRATEGY_IDS,
    });
  });

/** Dashboard data, read with the user's own client (RLS: own rows only). */
export const getForwardDashboard = createServerFn({ method: "GET" })
  .middleware([requireSupabaseAuth])
  .handler(async ({ context }) => loadForwardDashboard(context.supabase as never));
