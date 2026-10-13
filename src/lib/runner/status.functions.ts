import { createServerFn } from "@tanstack/react-start";
import { requireSupabaseAuth } from "@/integrations/supabase/auth-middleware";
import { resolveRunnerStatus, unavailableRunnerStatus, type RunnerStatus } from "./status-core";

export const getRunnerStatus = createServerFn({ method: "GET" })
  .middleware([requireSupabaseAuth])
  .handler(async ({ context }): Promise<RunnerStatus> => {
    const gatewayEnabled = process.env["RUNNER_GATEWAY_ENABLED"] === "true";
    const jobsEnabled = process.env["RUNNER_JOBS_ENABLED"] === "true";
    const configured = gatewayEnabled || jobsEnabled;
    if (!configured) return unavailableRunnerStatus(false);
    try {
      const [health, gatewayReachable] = await Promise.all([
        context.supabase
          .from("runner_health")
          .select("service, status, services, updated_at, last_seen_at")
          .abortSignal(AbortSignal.timeout(4_000)),
        gatewayEnabled ? gatewayAvailable() : Promise.resolve(null),
      ]);
      if (health.error) return unavailableRunnerStatus(true, jobsEnabled);
      return resolveRunnerStatus(health.data, Date.now(), {
        gatewayEnabled,
        jobsEnabled,
        gatewayReachable,
      });
    } catch {
      return unavailableRunnerStatus(true, jobsEnabled);
    }
  });

async function gatewayAvailable(): Promise<boolean> {
  try {
    const { runnerConfig } = await import("./client.server");
    const config = runnerConfig();
    if (!config) return false;
    // Public liveness endpoint; send neither the shared secret nor the user token.
    const response = await fetch(new URL("/health", config.url), {
      redirect: "manual",
      signal: AbortSignal.timeout(2_000),
    });
    await response.body?.cancel();
    return response.ok;
  } catch {
    return false;
  }
}
