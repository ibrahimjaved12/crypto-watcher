import { createServerFn } from "@tanstack/react-start";

import { requireSupabaseAuth } from "@/integrations/supabase/auth-middleware";

/** Runs the same monitoring pass the scheduler runs, but only for the caller. */
export const runMyMonitorCheck = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .handler(async ({ context }) => {
    const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
    const { runManualMonitorCheck } = await import("@/lib/monitor/manual-check.server");
    return runManualMonitorCheck(context.supabase, supabaseAdmin, context.userId);
  });
