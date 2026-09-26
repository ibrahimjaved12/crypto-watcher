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
import { getOperationalStore, operationalNativeSymbol } from "../operational/repository.server";
import type { OperationalStore } from "../operational/types";

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

const MONITOR_LEASE_SECONDS = 180;
const MONITOR_LEASE_RENEW_MS = 60_000;

export async function runMonitorForUser(
  supabaseAdmin: AdminClient,
  userId: string,
  settings: MonitorSettings,
  context: MonitorRunContext = createMonitorRunContext(),
  operationalStore: OperationalStore = getOperationalStore(),
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
  const sharedCollectorOwnsMarketData =
    operationalStore.enabled && process.env["BINANCE_COLLECTOR_ENABLED"] === "true";

  for (const symbol of symbols) {
    try {
      const outcome = await context.observation(symbol, (result) => {
        completedObservation(result.minute);
      });
      if (!outcome.ok) throw new Error(outcome.errors.join(" | "));
      const observation = completedObservation(outcome.result.minute);
      source = source ?? outcome.result.source;

      let checkpointStatus: string;
      if (sharedCollectorOwnsMarketData) {
        // The shared collector owns completed-candle/checkpoint state. Movement
        // detection remains in its existing Lovable transaction until migrated whole.
        checkpointStatus = "recorded";
      } else if (operationalStore.enabled) {
        context.metrics.databaseWriteAttempts += 2;
        const nativeSymbol = operationalNativeSymbol(outcome.result.source, symbol);
        const instrumentId = `${outcome.result.source}:${nativeSymbol}`;
        await operationalStore.recordCandles({
          userId,
          instrumentId,
          symbol,
          nativeSymbol,
          source: outcome.result.source,
          endpoint: outcome.result.endpoint,
          priceType: outcome.result.priceType,
          timeframeMinutes: 1,
          retrievedAt: outcome.result.retrievedAt,
          candles: outcome.result.minute,
        });
        checkpointStatus = await operationalStore.recordCheckpoint({
          userId,
          instrumentId,
          symbol,
          nativeSymbol,
          source: outcome.result.source,
          endpoint: outcome.result.endpoint,
          priceType: outcome.result.priceType,
          timeframeMinutes: 1,
          observedAt: observation.observedAt,
          price: observation.price,
        });
      } else {
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
        checkpointStatus = (checkpoint as { status?: string } | null)?.status ?? "";
        if (!checkpointStatus) throw new Error("Missing market checkpoint result");
      }
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
    // Completed-candle TA stays application-owned (#20): TanStack determines due
    // work, calls the shared Python calculation path, validates responses, and is
    // the privileged Lovable `ta_signals` writer. The collector worker only
    // ingests market data, so it never owns TA. When the collector owns canonical
    // candles, the app does not duplicate them in its per-user candle store.
    for (const symbol of symbols) {
      taErrors.push(
        ...(await runTA(
          supabaseAdmin,
          userId,
          symbol,
          context,
          undefined,
          sharedCollectorOwnsMarketData ? null : operationalStore,
        )),
      );
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

/** Shared durable ownership boundary for both manual and scheduled monitor runs. */
export async function runLeasedMonitorForUser(
  supabaseAdmin: AdminClient,
  userId: string,
  settings: MonitorSettings,
  context: MonitorRunContext = createMonitorRunContext(),
  operationalStore: OperationalStore = getOperationalStore(),
): Promise<UserRunResult> {
  const startedAt = performance.now();
  const startingMetrics = metricsSnapshot(context.metrics);
  const ownerId = crypto.randomUUID();
  const leaseResult = (status: UserRunResult["status"], error: string | null): UserRunResult => ({
    userId,
    status,
    symbolsChecked: 0,
    alertsCreated: 0,
    dataSource: null,
    error,
    durationMs: Math.max(0, Math.round(performance.now() - startedAt)),
    metrics: metricsSince(context.metrics, startingMetrics),
  });

  context.metrics.databaseWriteAttempts += 1;
  try {
    const { data: claimed, error: claimError } = await supabaseAdmin.rpc(
      "claim_monitor_run_lease",
      {
        p_user_id: userId,
        p_owner_id: ownerId,
        p_lease_seconds: MONITOR_LEASE_SECONDS,
      },
    );
    if (claimError) {
      return leaseResult("failed", `Monitor lease unavailable: ${claimError.message}`);
    }
    if (!claimed) {
      context.metrics.databaseNoOps += 1;
      return leaseResult("skipped", "A monitor run is already in progress for this account.");
    }
  } catch (error) {
    return leaseResult(
      "failed",
      `Monitor lease unavailable: ${error instanceof Error ? error.message : String(error)}`,
    );
  }

  let leaseFailure: string | null = null;
  let renewal: Promise<void> | null = null;
  const renew = async () => {
    context.metrics.databaseWriteAttempts += 1;
    try {
      const { data, error } = await supabaseAdmin.rpc("renew_monitor_run_lease", {
        p_user_id: userId,
        p_owner_id: ownerId,
        p_lease_seconds: MONITOR_LEASE_SECONDS,
      });
      if (error || !data) {
        leaseFailure = `Monitor lease renewal failed: ${error?.message ?? "ownership lost"}`;
      }
    } catch (error) {
      leaseFailure = `Monitor lease renewal failed: ${error instanceof Error ? error.message : String(error)}`;
    }
  };
  const timer = setInterval(() => {
    if (renewal || leaseFailure) return;
    renewal = renew().finally(() => {
      renewal = null;
    });
  }, MONITOR_LEASE_RENEW_MS);
  timer.unref?.();

  let result: UserRunResult;
  try {
    result = await runMonitorForUser(supabaseAdmin, userId, settings, context, operationalStore);
  } finally {
    clearInterval(timer);
    if (renewal) await renewal;
    context.metrics.databaseWriteAttempts += 1;
    try {
      const { error } = await supabaseAdmin.rpc("release_monitor_run_lease", {
        p_user_id: userId,
        p_owner_id: ownerId,
      });
      if (error) leaseFailure ??= `Monitor lease release failed: ${error.message}`;
    } catch (error) {
      leaseFailure ??= `Monitor lease release failed: ${
        error instanceof Error ? error.message : String(error)
      }`;
    }
  }

  if (!leaseFailure) return result;
  return {
    ...result,
    status: result.status === "success" ? "partial" : result.status,
    error: [result.error, leaseFailure].filter(Boolean).join(" | "),
    metrics: metricsSince(context.metrics, startingMetrics),
  };
}

export async function recordRun(
  supabaseAdmin: AdminClient,
  result: UserRunResult,
  operationalStore: OperationalStore = getOperationalStore(),
): Promise<void> {
  // Include the operational log write itself in the saved measurement.
  result.metrics.databaseWriteAttempts += 1;
  if (operationalStore.enabled) {
    await operationalStore.recordMonitorRun(result.userId, {
      status: result.status,
      symbols_checked: result.symbolsChecked,
      alerts_created: result.alertsCreated,
      data_source: result.dataSource,
      error_message: result.error,
      duration_ms: result.durationMs,
      metrics: result.metrics,
    });
    return;
  }
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
