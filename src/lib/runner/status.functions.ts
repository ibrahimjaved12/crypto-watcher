import { createServerFn } from "@tanstack/react-start";
import { requireSupabaseAuth } from "@/integrations/supabase/auth-middleware";

type RunnerStatus = { configured: boolean; online: boolean | null };

export const getRunnerStatus = createServerFn({ method: "GET" })
  .middleware([requireSupabaseAuth])
  .handler(async (): Promise<RunnerStatus> => {
    try {
      const { runnerConfig } = await import("./client.server");
      const config = runnerConfig();
      if (config === null) return { configured: false, online: null };
      // Health is public on the gateway: send neither the shared secret nor the user token.
      const response = await fetch(new URL("/health", config.url), {
        method: "GET",
        redirect: "manual",
        signal: AbortSignal.timeout(2_000),
      });
      try {
        await response.body?.cancel();
      } catch {
        // The status already establishes reachability.
      }
      return { configured: true, online: response.ok };
    } catch {
      return { configured: true, online: false };
    }
  });
