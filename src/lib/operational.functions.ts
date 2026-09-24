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
  activityFreshness: {
    marketCheckpoint: {
      available: boolean;
      observedAt: string | null;
    };
    movement: {
      available: boolean;
      evaluatedThrough: string | null;
    };
    ta: Array<{
      timeframeMinutes: number;
      available: boolean;
      evaluatedAt: string | null;
      completedCandleAt: string | null;
    }>;
  };
};

/** Authenticated read-through; the service-role credential never reaches the browser. */
export const getOperationalState = createServerFn({ method: "GET" })
  .middleware([requireSupabaseAuth])
  .handler(async ({ context }): Promise<OperationalState> => {
    const loadActivityFreshness = async (): Promise<OperationalState["activityFreshness"]> => {
      const [checkpoint, movement, ...ta] = await Promise.all([
        context.supabase
          .from("market_data_checkpoints")
          .select("observed_at")
          .eq("user_id", context.userId)
          .order("observed_at", { ascending: false })
          .limit(1)
          .maybeSingle(),
        context.supabase
          .from("monitor_baselines")
          .select("last_observed_at")
          .eq("user_id", context.userId)
          .order("last_observed_at", { ascending: false })
          .limit(1)
          .maybeSingle(),
        ...[15, 60, 240].map((timeframe) =>
          context.supabase
            .from("ta_signals")
            .select("evaluated_at, candle_at")
            .eq("user_id", context.userId)
            .eq("timeframe", timeframe)
            .order("evaluated_at", { ascending: false })
            .limit(1)
            .maybeSingle(),
        ),
      ]);
      return {
        marketCheckpoint: {
          available: checkpoint.error === null,
          observedAt: checkpoint.data?.observed_at ?? null,
        },
        movement: {
          available: movement.error === null,
          evaluatedThrough: movement.data?.last_observed_at ?? null,
        },
        ta: [15, 60, 240].map((timeframeMinutes, index) => ({
          timeframeMinutes,
          available: ta[index]?.error === null,
          evaluatedAt: ta[index]?.data?.evaluated_at ?? null,
          completedCandleAt: ta[index]?.data?.candle_at ?? null,
        })),
      };
    };

    const { getOperationalStore } = await import("./operational/repository.server");
    const store = getOperationalStore();
    if (store.enabled) {
      const [
        runs,
        diagnostics,
        collectorDiagnostics,
        watched,
        activityFreshness,
        operationalCheckpoint,
      ] = await Promise.all([
        store.listMonitorRuns(context.userId, 25),
        store.diagnostics(context.userId),
        store.collectorDiagnostics(),
        context.supabase.from("watchlist_items").select("symbol").eq("user_id", context.userId),
        loadActivityFreshness(),
        store
          .latestCheckpoint(context.userId)
          .then((observedAt) => ({ available: true, observedAt }))
          .catch(() => ({ available: false, observedAt: null })),
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
        activityFreshness: {
          ...activityFreshness,
          marketCheckpoint: operationalCheckpoint,
        },
      };
    }

    const [{ data, error }, activityFreshness] = await Promise.all([
      context.supabase
        .from("monitor_runs")
        .select(
          "id, ran_at, status, symbols_checked, alerts_created, data_source, error_message, duration_ms, metrics",
        )
        .eq("user_id", context.userId)
        .order("ran_at", { ascending: false })
        .limit(25),
      loadActivityFreshness(),
    ]);
    if (error) throw new Error(error.message);
    return {
      owner: "lovable",
      runs: (data ?? []) as unknown as MonitorRun[],
      diagnostics: null,
      collectorHealth: [],
      collectorDiagnostics: null,
      activityFreshness,
    };
  });
