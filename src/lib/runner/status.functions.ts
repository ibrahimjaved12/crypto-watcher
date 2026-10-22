import { createServerFn } from "@tanstack/react-start";
import { requireSupabaseAuth } from "@/integrations/supabase/auth-middleware";
import {
  resolveRunnerStatus,
  unavailableRunnerStatus,
  type GatewayCheck,
  type RunnerStatus,
} from "./status-core";

export const getRunnerStatus = createServerFn({ method: "GET" })
  .middleware([requireSupabaseAuth])
  .handler(async ({ context }): Promise<RunnerStatus> => {
    const gatewayEnabled = process.env["RUNNER_GATEWAY_ENABLED"] === "true";
    const jobsEnabled = process.env["RUNNER_JOBS_ENABLED"] === "true";
    const configured = gatewayEnabled || jobsEnabled;
    if (!configured) return unavailableRunnerStatus(false);
    try {
      const [health, gateway] = await Promise.all([
        context.supabase
          .from("runner_health")
          .select("service, status, services, services_seen, updated_at, last_seen_at")
          .abortSignal(AbortSignal.timeout(4_000)),
        gatewayEnabled ? gatewayAvailable() : Promise.resolve(null),
      ]);
      if (health.error)
        return { ...unavailableRunnerStatus(true, jobsEnabled), reason: "check-failed" };
      return resolveRunnerStatus(health.data, Date.now(), {
        gatewayEnabled,
        jobsEnabled,
        gatewayReachable: gateway ? gateway.reachable : null,
        gatewayReason: gateway ? gateway.reason : null,
      });
    } catch {
      return { ...unavailableRunnerStatus(true, jobsEnabled), reason: "check-failed" };
    }
  });

const ATTEMPT_TIMEOUT_MS = 4_000;
const TOTAL_BUDGET_MS = 6_000;

/**
 * Worker-to-Funnel liveness of the runner gateway, with the reason when it fails. One retry on a timeout or a
 * network error (a cold first request after idle, a transient drop); never on config, redirect or HTTP errors.
 * Per attempt 4 s, 6 s in total. The URL and the secret are never logged.
 */
async function gatewayAvailable(): Promise<GatewayCheck> {
  const started = Date.now();
  let url: URL;
  try {
    const { runnerConfig } = await import("./client.server");
    const config = runnerConfig();
    if (!config) return fail({ reachable: false, reason: "config", ms: Date.now() - started });
    url = new URL("/health", config.url);
  } catch {
    return fail({ reachable: false, reason: "config", ms: Date.now() - started });
  }
  let result: GatewayCheck = { reachable: false, reason: "network", ms: 0 };
  for (let attempt = 0; attempt < 2; attempt++) {
    const left = TOTAL_BUDGET_MS - (Date.now() - started);
    if (left <= 0) break;
    result = await probe(url, Math.min(ATTEMPT_TIMEOUT_MS, left), started);
    if (result.reachable || (result.reason !== "timeout" && result.reason !== "network")) break;
  }
  return result.reachable ? result : fail(result);
}

async function probe(url: URL, timeoutMs: number, started: number): Promise<GatewayCheck> {
  try {
    // Public liveness endpoint; send neither the shared secret nor the user token.
    const response = await fetch(url, { redirect: "manual", signal: AbortSignal.timeout(timeoutMs) });
    await response.body?.cancel();
    const ms = Date.now() - started;
    if (response.ok) return { reachable: true, reason: "ok", status: response.status, ms };
    const redirect = response.type === "opaqueredirect" || (response.status >= 300 && response.status < 400);
    return { reachable: false, reason: redirect ? "redirect" : "http_error", status: response.status, ms };
  } catch (error) {
    const name = error instanceof Error ? error.name : "";
    return {
      reachable: false,
      reason: name === "TimeoutError" || name === "AbortError" ? "timeout" : "network",
      ms: Date.now() - started,
    };
  }
}

function fail(result: GatewayCheck): GatewayCheck {
  console.warn(
    `[runner-status] gateway unreachable reason=${result.reason} status=${result.status ?? "-"} ms=${result.ms}`,
  );
  return result;
}
