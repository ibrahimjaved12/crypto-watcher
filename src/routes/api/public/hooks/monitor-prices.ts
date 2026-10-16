import { createFileRoute } from "@tanstack/react-router";
import { activityEnabled, logActivity } from "@/lib/activity-controls";

import { authenticateScheduledRequest } from "@/lib/scheduled-auth.server";
import { runScheduledMonitorPass, ScheduledPassLoadError } from "@/lib/monitor/scheduled-pass.server";

/**
 * Scheduled price monitoring through the Worker cron. Off in production (SCHEDULED_MONITOR_ENABLED=false and
 * the Worker has no operational database): the worker-host child `monitor-scheduler` owns scheduled checks on
 * the machine. Both call the same `runScheduledMonitorPass`.
 *
 * The collector's shared subscription universe is reconciled separately by
 * `/api/public/hooks/sync-collector-subscriptions`, so pausing scheduled monitoring
 * never leaves the collector empty or stale. A disabled request still returns before
 * loading the privileged database client (#22).
 */
async function handle(request: Request) {
  const unauthorized = await authenticateScheduledRequest(request);
  if (unauthorized) return unauthorized;

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

  try {
    const results = await runScheduledMonitorPass(supabaseAdmin);
    return Response.json({ ok: true, users: results.length, results });
  } catch (error) {
    if (error instanceof ScheduledPassLoadError) {
      return Response.json({ ok: false, error: error.message }, { status: 500 });
    }
    throw error;
  }
}

export const Route = createFileRoute("/api/public/hooks/monitor-prices")({
  server: {
    handlers: {
      POST: ({ request }) => handle(request),
      GET: ({ request }) => handle(request),
    },
  },
});
