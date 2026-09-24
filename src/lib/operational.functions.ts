import { createServerFn } from "@tanstack/react-start";
import { requireSupabaseAuth } from "@/integrations/supabase/auth-middleware";
import type { MonitorRun } from "./db";
import type {
  CollectorHealth,
  CollectorStorageDiagnostics,
  StorageDiagnostics,
} from "./operational/types";

export type OperationalState = {
  owner: "operational" | "lovable";
  runs: MonitorRun[];
  diagnostics: StorageDiagnostics | null;
  collectorHealth: CollectorHealth[];
  collectorDiagnostics: CollectorStorageDiagnostics | null;
};

/** Authenticated read-through; the service-role credential never reaches the browser. */
export const getOperationalState = createServerFn({ method: "GET" })
  .middleware([requireSupabaseAuth])
  .handler(async ({ context }): Promise<OperationalState> => {
    const { getOperationalStore } = await import("./operational/repository.server");
    const store = getOperationalStore();
    if (store.enabled) {
      const [runs, diagnostics, collectorDiagnostics, watched] = await Promise.all([
        store.listMonitorRuns(context.userId, 25),
        store.diagnostics(context.userId),
        store.collectorDiagnostics(),
        context.supabase.from("watchlist_items").select("symbol").eq("user_id", context.userId),
      ]);
      if (watched.error) throw new Error(watched.error.message);
      const symbols = [...new Set((watched.data ?? []).map((row) => row.symbol))];
      const collectorHealth = await store.listCollectorHealth(symbols);
      return {
        owner: "operational",
        runs: runs as MonitorRun[],
        diagnostics,
        collectorHealth,
        collectorDiagnostics,
      };
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
    return {
      owner: "lovable",
      runs: (data ?? []) as unknown as MonitorRun[],
      diagnostics: null,
      collectorHealth: [],
      collectorDiagnostics: null,
    };
  });
