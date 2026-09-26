import { createFileRoute } from "@tanstack/react-router";

import { authenticateScheduledRequest } from "@/lib/scheduled-auth.server";
import { syncCollectorUniverse } from "@/lib/market/collector-subscriptions.server";

/**
 * Application-owned reconciliation of the collector's shared subscription universe.
 * This authenticated hook is the initial and ongoing reconciliation mechanism the
 * deployment schedules; the server's first-request pass is only a safety net. It is
 * deliberately independent of `SCHEDULED_MONITOR_ENABLED`: the collector's input must
 * stay current even while scheduled monitoring is paused. The scheduler authenticates
 * the request; the app remains the only privileged Lovable reader.
 */
async function handle(request: Request) {
  const unauthorized = await authenticateScheduledRequest(request);
  if (unauthorized) return unauthorized;

  const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
  const { getOperationalStore } = await import("@/lib/operational/repository.server");

  try {
    const result = await syncCollectorUniverse(supabaseAdmin, getOperationalStore());
    return Response.json({ ok: true, ...result });
  } catch (error) {
    return Response.json(
      { ok: false, error: error instanceof Error ? error.message : String(error) },
      { status: 500 },
    );
  }
}

export const Route = createFileRoute("/api/public/hooks/sync-collector-subscriptions")({
  server: {
    handlers: {
      POST: ({ request }) => handle(request),
      GET: ({ request }) => handle(request),
    },
  },
});
