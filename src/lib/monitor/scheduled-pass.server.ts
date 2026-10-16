import type { SupabaseClient } from "@supabase/supabase-js";
import type { Database } from "@/integrations/supabase/types";
import {
  DEFAULT_SETTINGS,
  recordRun,
  runLeasedMonitorForUser,
  type MonitorSettings,
  type UserRunResult,
} from "./engine.server";
import { createMonitorRunContext, type MonitorRunContext } from "./run-context";

type AdminClient = Pick<SupabaseClient<Database>, "from" | "rpc">;

/** Thrown when the user list or the settings cannot be loaded; the caller decides how to report it. */
export class ScheduledPassLoadError extends Error {}

/**
 * One scheduled monitoring pass over every user with at least one watched symbol (#30). Shared by the
 * Worker cron hook (`/api/public/hooks/monitor-prices`) and the worker-host child `monitor-scheduler`.
 * Each user runs under the per-user lease of `runLeasedMonitorForUser`, so a pass and a manual check never
 * double-run; a "skipped" result is normal and is not recorded.
 */
export async function runScheduledMonitorPass(
  supabaseAdmin: AdminClient,
  runContext: MonitorRunContext = createMonitorRunContext(),
): Promise<UserRunResult[]> {
  const { data: watchers, error } = await supabaseAdmin
    .from("watchlist_items")
    .select("user_id, symbol");
  if (error) throw new ScheduledPassLoadError(error.message);

  const userIds = [...new Set((watchers ?? []).map((w) => w.user_id))];

  const { data: settingsRows, error: settingsError } = await supabaseAdmin
    .from("monitor_settings")
    .select(
      "user_id, threshold_pct, window_minutes, cooldown_minutes, monitoring_enabled, market_data_collection_enabled, completed_candle_ta_enabled, movement_alerts_enabled, developing_setup_evaluation_enabled, paper_trading_enabled",
    )
    .in("user_id", userIds.length ? userIds : ["00000000-0000-0000-0000-000000000000"]);
  if (settingsError) throw new ScheduledPassLoadError(settingsError.message);

  const byUser = new Map<string, MonitorSettings>(
    (settingsRows ?? []).map((s) => [s.user_id, s as unknown as MonitorSettings]),
  );

  const results: UserRunResult[] = [];
  for (const userId of userIds) {
    const settings = {
      ...DEFAULT_SETTINGS,
      ...byUser.get(userId),
      user_id: userId,
    };
    try {
      const result = await runLeasedMonitorForUser(supabaseAdmin, userId, settings, runContext);
      if (result.status !== "skipped") await recordRun(supabaseAdmin, result);
      results.push(result);
    } catch (err) {
      const result: UserRunResult = {
        userId,
        status: "failed",
        symbolsChecked: 0,
        alertsCreated: 0,
        taChecks: 0,
        taFailures: 0,
        dataSource: null,
        error: err instanceof Error ? err.message : String(err),
        durationMs: 0,
        metrics: {
          exchangeRequests: 0,
          candleRows: 0,
          marketCacheHits: 0,
          taCalculations: 0,
          taSignalsSaved: 0,
          taOutcomesUpdated: 0,
          databaseReads: 0,
          databaseWriteAttempts: 0,
          databaseNoOps: 0,
        },
      };
      await recordRun(supabaseAdmin, result);
      results.push(result);
    }
  }
  return results;
}
