import { createFileRoute } from "@tanstack/react-router";
import { activityEnabled, logActivity } from "@/lib/activity-controls";

import { authenticateCronRequest } from "@/integrations/supabase/cron-auth";
import {
  DEFAULT_SETTINGS,
  recordRun,
  runMonitorForUser,
  type MonitorSettings,
} from "@/lib/monitor/engine.server";

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
  const { data: watchers, error } = await supabaseAdmin.from("watchlist_items").select("user_id");

  if (error) {
    return Response.json({ ok: false, error: error.message }, { status: 500 });
  }

  const userIds = [...new Set((watchers ?? []).map((w) => w.user_id))];

  const { data: settingsRows, error: settingsError } = await supabaseAdmin
    .from("monitor_settings")
    .select("*")
    .in("user_id", userIds.length ? userIds : ["00000000-0000-0000-0000-000000000000"]);
  if (settingsError) {
    return Response.json({ ok: false, error: settingsError.message }, { status: 500 });
  }

  const byUser = new Map<string, MonitorSettings>(
    (settingsRows ?? []).map((s) => [s.user_id, s as unknown as MonitorSettings]),
  );

  const results = [];
  for (const userId of userIds) {
    const settings = {
      ...DEFAULT_SETTINGS,
      ...byUser.get(userId),
      user_id: userId,
    };
    try {
      const result = await runMonitorForUser(supabaseAdmin, userId, settings);
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
