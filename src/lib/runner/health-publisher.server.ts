import { supabaseAdmin } from "../../integrations/supabase/client.server";

export async function reportRunnerHealth(
  service: "worker-host" | "runner-jobs",
  status: "online" | "degraded" | "offline",
  services: Record<string, boolean> = {},
): Promise<void> {
  const { error } = await supabaseAdmin
    .rpc("report_runner_health", {
      p_service: service,
      p_status: status,
      p_services: services,
    })
    .abortSignal(AbortSignal.timeout(5_000));
  if (error) throw error;
}
