import { createFileRoute } from "@tanstack/react-router";

import { hookAuthorized } from "@/lib/alert-charts/hosted.server";
import {
  MOVEMENT_HOOK_BODY_CAP,
  handleMovementHook,
  parseMovementHookInput,
} from "@/lib/market/movement-published";

/**
 * Machine -> site publication of the movement engine's persisted state (#30). Authenticated with the shared
 * machine secret (RUNNER_GATEWAY_SECRET), like the alert-chart upload hook; the site is the privileged writer of
 * the one market-wide hosted row. The machine holds no hosted service key for this.
 */
async function handle(request: Request): Promise<Response> {
  if (!hookAuthorized(request.headers.get("x-runner-secret"))) {
    return Response.json({ ok: false, category: "unauthorized", error: "Unauthorized." }, { status: 401 });
  }
  if (Number(request.headers.get("content-length") ?? 0) > MOVEMENT_HOOK_BODY_CAP) {
    return Response.json({ ok: false, category: "bad_request", error: "Too large." }, { status: 413 });
  }
  let body: unknown;
  try {
    const text = await request.text();
    if (text.length > MOVEMENT_HOOK_BODY_CAP) throw new Error("too large");
    body = JSON.parse(text);
  } catch {
    return Response.json({ ok: false, category: "bad_request", error: "Invalid JSON." }, { status: 400 });
  }
  const input = parseMovementHookInput(body);
  if (!input) return Response.json({ ok: false, category: "bad_request", error: "Invalid request." }, { status: 400 });
  const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
  const rpc = supabaseAdmin.rpc as unknown as (
    name: string, args: Record<string, unknown>,
  ) => Promise<{ data: unknown; error: { message: string } | null }>;
  const reply = await handleMovementHook({ rpc: (name, args) => rpc.call(supabaseAdmin, name, args) }, input);
  return Response.json(reply.body, { status: reply.status });
}

export const Route = createFileRoute("/api/public/hooks/market-state-publish")({
  server: { handlers: { POST: ({ request }) => handle(request) } },
});
