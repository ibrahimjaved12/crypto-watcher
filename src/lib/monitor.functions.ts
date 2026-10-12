import { createServerFn } from "@tanstack/react-start";

import { requireSupabaseAuth } from "@/integrations/supabase/auth-middleware";
import type { runManualMonitorCheck as LocalMonitorCheck } from "@/lib/monitor/manual-check.server";

type MonitorCheckReply = Awaited<ReturnType<typeof LocalMonitorCheck>>;

/** Runs the same monitoring pass the scheduler runs, but only for the caller. */
export const runMyMonitorCheck = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .handler(async ({ context }) => {
    const { runnerConfig, callRunner } = await import("./runner/client.server");
    if (runnerConfig() !== null) {
      const reply = await callRunner<MonitorCheckReply>(
        "/v1/monitor/check",
        context.accessToken,
        {},
      );
      if (!reply.ok) throw new Error(reply.error);
      return reply.data;
    }
    const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
    const { runManualMonitorCheck } = await import("@/lib/monitor/manual-check.server");
    return runManualMonitorCheck(context.supabase, supabaseAdmin, context.userId);
  });
