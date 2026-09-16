/**
 * Monitoring orchestrator. Runs server-side for scheduled and manual checks.
 *
 * The database transaction owns baseline state, directional cooldowns and alert
 * insertion. Python has the same state transition for future replay/backtesting.
 */
import { loadCandles } from "@/lib/market/providers.server";
import { completedObservation } from "./observation";
import { runTA } from "../ta/engine.server";
import type { SupabaseClient } from "@supabase/supabase-js";
import type { Database } from "@/integrations/supabase/types";

export type MonitorSettings = {
  user_id: string;
  threshold_pct: number;
  window_minutes: number;
  cooldown_minutes: number;
  monitoring_enabled: boolean;
};

export const DEFAULT_SETTINGS = {
  threshold_pct: 2,
  window_minutes: 15,
  cooldown_minutes: 15,
  monitoring_enabled: true,
};

export type UserRunResult = {
  userId: string;
  status: "success" | "partial" | "failed" | "skipped";
  symbolsChecked: number;
  alertsCreated: number;
  dataSource: string | null;
  error: string | null;
};

type AdminClient = Pick<SupabaseClient<Database>, "from" | "rpc">;

export async function runMonitorForUser(
  supabaseAdmin: AdminClient,
  userId: string,
  settings: MonitorSettings,
): Promise<UserRunResult> {
  const base: UserRunResult = {
    userId,
    status: "success",
    symbolsChecked: 0,
    alertsCreated: 0,
    dataSource: null,
    error: null,
  };

  if (!settings.monitoring_enabled) {
    return { ...base, status: "skipped" };
  }

  const { data: items, error: itemsError } = await supabaseAdmin
    .from("watchlist_items")
    .select("symbol")
    .eq("user_id", userId);

  if (itemsError) {
    return { ...base, status: "failed", error: itemsError.message };
  }

  const symbols: string[] = (items ?? []).map((i: { symbol: string }) => i.symbol);
  if (symbols.length === 0) return { ...base, status: "skipped" };

  const failures: string[] = [];
  let alertsCreated = 0;
  let source: string | null = null;

  for (const symbol of symbols) {
    try {
      const outcome = await loadCandles(symbol, (result) => {
        completedObservation(result.minute);
      });
      if (!outcome.ok) throw new Error(outcome.errors.join(" | "));
      const observation = completedObservation(outcome.result.minute);
      source = source ?? outcome.result.source;
      // Settings are re-read inside the transaction, so a concurrent pause or edit
      // cannot save an alert using a stale threshold. No in-memory cooldown cache.
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
      if (status === "alerted") alertsCreated += 1;
    } catch (error) {
      failures.push(`${symbol}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  const taErrors: string[] = [];
  for (const symbol of symbols) taErrors.push(...(await runTA(supabaseAdmin, userId, symbol)));

  const status: UserRunResult["status"] =
    failures.length >= symbols.length
      ? "failed"
      : failures.length || taErrors.length
        ? "partial"
        : "success";

  return {
    userId,
    status,
    symbolsChecked: symbols.length,
    alertsCreated,
    dataSource: source,
    error: [...failures, ...taErrors].join(" | ") || null,
  };
}

export async function recordRun(supabaseAdmin: AdminClient, result: UserRunResult): Promise<void> {
  const { error } = await supabaseAdmin.from("monitor_runs").insert({
    user_id: result.userId,
    status: result.status,
    symbols_checked: result.symbolsChecked,
    alerts_created: result.alertsCreated,
    data_source: result.dataSource,
    error_message: result.error,
  });
  if (error) throw new Error(`Could not record monitoring run: ${error.message}`);
}
