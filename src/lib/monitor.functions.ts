import { createServerFn } from "@tanstack/react-start";

import { requireSupabaseAuth } from "@/integrations/supabase/auth-middleware";

/** Runs the same monitoring pass the scheduler runs, but only for the caller. */
export const runMyMonitorCheck = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .handler(async ({ context }) => {
    const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
    const { DEFAULT_SETTINGS, recordRun, runMonitorForUser } =
      await import("@/lib/monitor/engine.server");

    const userId = context.userId;
    const { data: settings, error: settingsError } = await context.supabase
      .from("monitor_settings")
      .select("*")
      .eq("user_id", userId)
      .maybeSingle();
    if (settingsError) throw new Error(settingsError.message);

    const result = await runMonitorForUser(supabaseAdmin, userId, {
      user_id: userId,
      threshold_pct: Number(settings?.threshold_pct ?? DEFAULT_SETTINGS.threshold_pct),
      window_minutes: settings?.window_minutes ?? DEFAULT_SETTINGS.window_minutes,
      cooldown_minutes: settings?.cooldown_minutes ?? DEFAULT_SETTINGS.cooldown_minutes,
      monitoring_enabled: settings?.monitoring_enabled ?? DEFAULT_SETTINGS.monitoring_enabled,
    });

    if (result.status !== "skipped") await recordRun(supabaseAdmin, result);

    return {
      status: result.status,
      symbolsChecked: result.symbolsChecked,
      alertsCreated: result.alertsCreated,
      dataSource: result.dataSource,
      error: result.error,
    };
  });
