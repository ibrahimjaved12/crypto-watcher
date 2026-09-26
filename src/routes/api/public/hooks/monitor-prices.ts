import { createFileRoute } from "@tanstack/react-router";
import { activityEnabled, logActivity } from "@/lib/activity-controls";

import { authenticateCronRequest } from "@/integrations/supabase/cron-auth";
import {
  DEFAULT_SETTINGS,
  recordRun,
  runLeasedMonitorForUser,
  type MonitorSettings,
} from "@/lib/monitor/engine.server";
import { createMonitorRunContext } from "@/lib/monitor/run-context";

/**
 * Scheduled price monitoring. When explicitly enabled, the backend scheduler can
 * call this route every five minutes so monitoring runs with no browser open.
 */
async function handle(request: Request) {
  // The scheduler authenticates with a shared token; the platform cron secret
  // is accepted too so either caller works.
  const bearer = /^Bearer ([^\s,]+)$/.exec(request.headers.get("authorization") ?? "")?.[1];
  const token = process.env["MONITOR_CRON_TOKEN"];
  if (!token || bearer !== token) {
    const unauthorized = await authenticateCronRequest(request);
    if (unauthorized) return unauthorized;
  }

  if (!activityEnabled(process.env["SCHEDULED_MONITOR_ENABLED"], false)) {
    logActivity(process.env["ACTIVITY_DIAGNOSTICS"], "scheduled-monitor", "skipped");
    return Response.json({
      ok: true,
      status: "skipped",
      reason: "Scheduled monitoring disabled",
      users: 0,
      results: [],
    });
  }

  const { supabaseAdmin } = await import("@/integrations/supabase/client.server");

  // Every user with at least one watched symbol gets checked.
  const { data: watchers, error } = await supabaseAdmin
    .from("watchlist_items")
    .select("user_id, symbol");

  if (error) {
    return Response.json({ ok: false, error: error.message }, { status: 500 });
  }

  const userIds = [...new Set((watchers ?? []).map((w) => w.user_id))];

  // The application owns watchlists; it assigns the shared collector subscription
  // universe as derived operational input. The collector worker reads this set from
  // the operational database and never reads Lovable user tables itself.
  if (process.env["BINANCE_COLLECTOR_ENABLED"] === "true") {
    const { getOperationalStore } = await import("@/lib/operational/repository.server");
    const operationalStore = getOperationalStore();
    if (operationalStore.enabled) {
      const universe = [...new Set((watchers ?? []).map((w) => w.symbol))].sort();
      await operationalStore.assignCollectorSubscriptions(universe);
    }
  }

  const { data: settingsRows, error: settingsError } = await supabaseAdmin
    .from("monitor_settings")
    .select(
      "user_id, threshold_pct, window_minutes, cooldown_minutes, monitoring_enabled, market_data_collection_enabled, completed_candle_ta_enabled, movement_alerts_enabled, developing_setup_evaluation_enabled, paper_trading_enabled",
    )
    .in("user_id", userIds.length ? userIds : ["00000000-0000-0000-0000-000000000000"]);
  if (settingsError) {
    return Response.json({ ok: false, error: settingsError.message }, { status: 500 });
  }

  const byUser = new Map<string, MonitorSettings>(
    (settingsRows ?? []).map((s) => [s.user_id, s as unknown as MonitorSettings]),
  );

  const results = [];
  const runContext = createMonitorRunContext();
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
      const result = {
        userId,
        status: "failed" as const,
        symbolsChecked: 0,
        alertsCreated: 0,
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

  return Response.json({ ok: true, users: results.length, results });
}

export const Route = createFileRoute("/api/public/hooks/monitor-prices")({
  server: {
    handlers: {
      POST: ({ request }) => handle(request),
      GET: ({ request }) => handle(request),
    },
  },
});
