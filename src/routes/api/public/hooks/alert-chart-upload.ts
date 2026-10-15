import { createFileRoute } from "@tanstack/react-router";

import { HOOK_BODY_CAP, handleChartHook, hookAuthorized, parseHookInput } from "@/lib/alert-charts/hosted.server";

/**
 * Machine -> site upload protocol for the hosted copy of movement-alert charts (#298). Authenticated with the
 * shared machine secret (RUNNER_GATEWAY_SECRET). The request body is small JSON; the PNG itself goes from the
 * machine straight to the private bucket with a one-time signed upload URL, so the Worker never touches image bytes.
 */
async function handle(request: Request): Promise<Response> {
  if (!hookAuthorized(request.headers.get("x-runner-secret"))) {
    return Response.json({ ok: false, category: "unauthorized", error: "Unauthorized." }, { status: 401 });
  }
  const length = Number(request.headers.get("content-length") ?? 0);
  if (length > HOOK_BODY_CAP) return Response.json({ ok: false, category: "bad_request", error: "Too large." }, { status: 413 });
  let body: unknown;
  try {
    const text = await request.text();
    if (text.length > HOOK_BODY_CAP) throw new Error("too large");
    body = JSON.parse(text);
  } catch {
    return Response.json({ ok: false, category: "bad_request", error: "Invalid JSON." }, { status: 400 });
  }
  const input = parseHookInput(body);
  if (!input) return Response.json({ ok: false, category: "bad_request", error: "Invalid request." }, { status: 400 });
  const { hookDeps } = await import("@/lib/alert-charts/hosted-deps.server");
  const reply = await handleChartHook(hookDeps(), input);
  return Response.json(reply.body, { status: reply.status });
}

export const Route = createFileRoute("/api/public/hooks/alert-chart-upload")({
  server: { handlers: { POST: ({ request }) => handle(request) } },
});
