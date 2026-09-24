import { createServerFn } from "@tanstack/react-start";
import { requireSupabaseAuth } from "@/integrations/supabase/auth-middleware";
import type { MonitorRun } from "./db";
import type { StorageDiagnostics } from "./operational/types";

export type OperationalState = {
  owner: "operational" | "lovable";
  runs: MonitorRun[];
  diagnostics: StorageDiagnostics | null;
};

/** Authenticated read-through; the service-role credential never reaches the browser. */
export const getOperationalState = createServerFn({ method: "GET" })
  .middleware([requireSupabaseAuth])
  .handler(async ({ context }): Promise<OperationalState> => {
    const { getOperationalStore } = await import("./operational/repository.server");
    const store = getOperationalStore();
    if (store.enabled) {
      const [runs, diagnostics] = await Promise.all([
        store.listMonitorRuns(context.userId, 25),
        store.diagnostics(context.userId),
      ]);
      return { owner: "operational", runs: runs as MonitorRun[], diagnostics };
    }

    const { data, error } = await context.supabase
      .from("monitor_runs")
      .select(
        "id, ran_at, status, symbols_checked, alerts_created, data_source, error_message, duration_ms, metrics",
      )
      .eq("user_id", context.userId)
      .order("ran_at", { ascending: false })
      .limit(25);
    if (error) throw new Error(error.message);
    return { owner: "lovable", runs: (data ?? []) as unknown as MonitorRun[], diagnostics: null };
  });
