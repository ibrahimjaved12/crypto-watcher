import { createServerFn } from "@tanstack/react-start";

import { requireSupabaseAuth } from "@/integrations/supabase/auth-middleware";

/** Runs the same monitoring pass the scheduler runs, but only for the caller. */
export const runMyMonitorCheck = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .handler(async ({ context }) => {
    const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
    const { DEFAULT_SETTINGS, recordRun, runLeasedMonitorForUser } =
      await import("@/lib/monitor/engine.server");

    const userId = context.userId;

    // Safety net: make sure the collector is subscribed before checking, so a run right after
    // start-up does not find an empty candle store. Best effort; a failure surfaces per symbol
    // below as missing history.
    try {
      const { syncCollectorUniverse } = await import("@/lib/market/collector-subscriptions.server");
      const { getOperationalStore } = await import("@/lib/operational/repository.server");
      await syncCollectorUniverse(supabaseAdmin, getOperationalStore());
    } catch (error) {
      console.error(
        `[collector-universe] sync before manual check failed: ${
          error instanceof Error ? error.message : String(error)
        }`,
      );
    }
    const { data: settings, error: settingsError } = await context.supabase
      .from("monitor_settings")
      .select(
        "threshold_pct, window_minutes, cooldown_minutes, monitoring_enabled, market_data_collection_enabled, completed_candle_ta_enabled, movement_alerts_enabled, developing_setup_evaluation_enabled, paper_trading_enabled",
      )
      .eq("user_id", userId)
      .maybeSingle();
    if (settingsError) throw new Error(settingsError.message);

    const result = await runLeasedMonitorForUser(supabaseAdmin, userId, {
      user_id: userId,
      threshold_pct: Number(settings?.threshold_pct ?? DEFAULT_SETTINGS.threshold_pct),
      window_minutes: settings?.window_minutes ?? DEFAULT_SETTINGS.window_minutes,
      cooldown_minutes: settings?.cooldown_minutes ?? DEFAULT_SETTINGS.cooldown_minutes,
      monitoring_enabled: settings?.monitoring_enabled ?? DEFAULT_SETTINGS.monitoring_enabled,
      market_data_collection_enabled:
        settings?.market_data_collection_enabled ?? DEFAULT_SETTINGS.market_data_collection_enabled,
      completed_candle_ta_enabled:
        settings?.completed_candle_ta_enabled ?? DEFAULT_SETTINGS.completed_candle_ta_enabled,
      movement_alerts_enabled:
        settings?.movement_alerts_enabled ?? DEFAULT_SETTINGS.movement_alerts_enabled,
      developing_setup_evaluation_enabled:
        settings?.developing_setup_evaluation_enabled ??
        DEFAULT_SETTINGS.developing_setup_evaluation_enabled,
      paper_trading_enabled:
        settings?.paper_trading_enabled ?? DEFAULT_SETTINGS.paper_trading_enabled,
    });

    if (result.status !== "skipped") await recordRun(supabaseAdmin, result);

    return {
      status: result.status,
      symbolsChecked: result.symbolsChecked,
      alertsCreated: result.alertsCreated,
      dataSource: result.dataSource,
      error: result.error,
      durationMs: result.durationMs,
      metrics: result.metrics,
    };
  });
