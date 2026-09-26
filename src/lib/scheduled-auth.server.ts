import { authenticateCronRequest } from "@/integrations/supabase/cron-auth";

/**
 * Shared authentication for server-side scheduled hooks. The scheduler may present
 * the monitor token or the platform cron secret; invalid requests stay unauthorized.
 * Callers apply their own activity/enablement gates after authentication.
 */
export async function authenticateScheduledRequest(request: Request): Promise<Response | null> {
  const bearer = /^Bearer ([^\s,]+)$/.exec(request.headers.get("authorization") ?? "")?.[1];
  const token = process.env["MONITOR_CRON_TOKEN"];
  if (token && bearer === token) return null;
  return authenticateCronRequest(request);
}
