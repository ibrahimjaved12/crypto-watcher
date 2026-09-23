/**
 * Monitoring orchestrator. Runs server-side for scheduled and manual checks.
 *
 * The database transaction owns baseline state, directional cooldowns and alert
 * insertion. Python has the same state transition for future replay/backtesting.
 */
import { completedObservation } from "./observation";
import { runTA } from "../ta/engine.server";
import {
  createMonitorRunContext,
  metricsSince,
  metricsSnapshot,
  type MonitorMetrics,
  type MonitorRunContext,
} from "./run-context";
import type { SupabaseClient } from "@supabase/supabase-js";
import type { Database } from "@/integrations/supabase/types";

export type MonitorSettings = {
  user_id: string;
  threshold_pct: number;
  window_minutes: number;
  cooldown_minutes: number;
  monitoring_enabled: boolean;
  market_data_collection_enabled: boolean;
  completed_candle_ta_enabled: boolean;
  movement_alerts_enabled: boolean;
  developing_setup_evaluation_enabled: boolean;
  paper_trading_enabled: boolean;
};

export const DEFAULT_SETTINGS = {
  threshold_pct: 2,
  window_minutes: 15,
  cooldown_minutes: 15,
  monitoring_enabled: true,
  market_data_collection_enabled: true,
  completed_candle_ta_enabled: true,
  movement_alerts_enabled: true,
  // These fail closed until their later roadmap issues add real engines.
  developing_setup_evaluation_enabled: false,
  paper_trading_enabled: false,
};

export type UserRunResult = {
  userId: string;
  status: "success" | "partial" | "failed" | "skipped";
  symbolsChecked: number;
  alertsCreated: number;
  dataSource: string | null;
  error: string | null;
  durationMs: number;
  metrics: MonitorMetrics;
};

type AdminClient = Pick<SupabaseClient<Database>, "from" | "rpc">;

export async function runMonitorForUser(
  supabaseAdmin: AdminClient,
  userId: string,
  settings: MonitorSettings,
  context: MonitorRunContext = createMonitorRunContext(),
): Promise<UserRunResult> {
  const startedAt = performance.now();
  const startingMetrics = metricsSnapshot(context.metrics);
  const base: UserRunResult = {
    userId,
    status: "success",
    symbolsChecked: 0,
    alertsCreated: 0,
    dataSource: null,
    error: null,
    durationMs: 0,
    metrics: metricsSince(context.metrics, startingMetrics),
  };

  const finish = (result: Omit<UserRunResult, "durationMs" | "metrics">): UserRunResult => ({
    ...result,
    durationMs: Math.max(0, Math.round(performance.now() - startedAt)),
    metrics: metricsSince(context.metrics, startingMetrics),
  });

  if (!settings.monitoring_enabled) {
    return finish({ ...base, status: "skipped" });
  }

  if (!settings.market_data_collection_enabled) {
    return finish({ ...base, status: "skipped" });
  }

  context.metrics.databaseReads += 1;
  const { data: items, error: itemsError } = await supabaseAdmin
    .from("watchlist_items")
    .select("symbol")
    .eq("user_id", userId);

  if (itemsError) {
    return finish({ ...base, status: "failed", error: itemsError.message });
  }

  const symbols: string[] = (items ?? []).map((i: { symbol: string }) => i.symbol);
  if (symbols.length === 0) return finish({ ...base, status: "skipped" });

  const failures: string[] = [];
  let alertsCreated = 0;
  let source: string | null = null;

  for (const symbol of symbols) {
    try {
      const outcome = await context.observation(symbol, (result) => {
        completedObservation(result.minute);
      });
      if (!outcome.ok) throw new Error(outcome.errors.join(" | "));
      const observation = completedObservation(outcome.result.minute);
      source = source ?? outcome.result.source;

      context.metrics.databaseWriteAttempts += 1;
      const { data: checkpoint, error: checkpointError } = await supabaseAdmin.rpc(
        "record_market_data_checkpoint",
        {
          p_user_id: userId,
          p_symbol: symbol,
          p_price: observation.price,
          p_observed_at: observation.observedAt,
          p_source: outcome.result.source,
        },
      );
      if (checkpointError) throw new Error(checkpointError.message);
      const checkpointStatus = (checkpoint as { status?: string } | null)?.status;
      if (!checkpointStatus) throw new Error("Missing market checkpoint result");
      if (["already_processed", "not_watched", "disabled"].includes(checkpointStatus)) {
        context.metrics.databaseNoOps += 1;
      }
      if (checkpointStatus === "not_watched" || checkpointStatus === "disabled") continue;

      if (settings.movement_alerts_enabled) {
        // Settings are re-read inside the transaction, so a concurrent pause or edit
        // cannot save an alert using a stale threshold. No in-memory cooldown cache.
        context.metrics.databaseWriteAttempts += 1;
        const { data, error } = await supabaseAdmin.rpc("process_cumulative_observation", {
          p_user_id: userId,
          p_symbol: symbol,
          p_price: observation.price,
          p_observed_at: observation.observedAt,
          p_source: outcome.result.source,
        });
        if (error) throw new Error(error.message);
        const status = (data as { status?: string } | null)?.status;
        if (!status) throw new Error("Missing baseline transition result");
        if (["already_processed", "not_watched", "disabled"].includes(status)) {
          context.metrics.databaseNoOps += 1;
        }
        if (status === "alerted") alertsCreated += 1;
      }
    } catch (error) {
      failures.push(`${symbol}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  const taErrors: string[] = [];
  if (settings.completed_candle_ta_enabled) {
    for (const symbol of symbols) {
      taErrors.push(...(await runTA(supabaseAdmin, userId, symbol, context)));
    }
  }

  const collectionPathFailed = failures.length >= symbols.length;
  const taFailed = settings.completed_candle_ta_enabled && taErrors.length >= symbols.length * 3;
  const allEnabledWorkFailed =
    collectionPathFailed && (!settings.completed_candle_ta_enabled || taFailed);
  const status: UserRunResult["status"] = allEnabledWorkFailed
    ? "failed"
    : failures.length || taErrors.length
      ? "partial"
      : "success";

  return finish({
    userId,
    status,
    symbolsChecked: symbols.length,
    alertsCreated,
    dataSource: source,
    error: [...failures, ...taErrors].join(" | ") || null,
  });
}

export async function recordRun(supabaseAdmin: AdminClient, result: UserRunResult): Promise<void> {
  // Include the operational log write itself in the saved measurement.
  result.metrics.databaseWriteAttempts += 1;
  const { error } = await supabaseAdmin.from("monitor_runs").insert({
    user_id: result.userId,
    status: result.status,
    symbols_checked: result.symbolsChecked,
    alerts_created: result.alertsCreated,
    data_source: result.dataSource,
    error_message: result.error,
    duration_ms: result.durationMs,
    metrics: result.metrics,
  });
  if (error) throw new Error(`Could not record monitoring run: ${error.message}`);
}
