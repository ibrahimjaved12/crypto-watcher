import { logActivity } from "@/lib/activity-controls";
import { supabase } from "@/integrations/supabase/client";
import type { TaRowInput } from "./assessment";

/**
 * Each watched pair's own newest saved TA snapshot for one timeframe: one round trip per timeframe through the
 * `latest_ta_signals` SQL function (DISTINCT ON symbol, SECURITY INVOKER so RLS scopes it to the caller), so a
 * pair that has fallen behind the others still returns its newest row.
 */
export async function loadLatestTa(timeframe: number, symbols: readonly string[]): Promise<TaRowInput[]> {
  if (symbols.length === 0) return [];
  logActivity(import.meta.env["VITE_ACTIVITY_DIAGNOSTICS"], "ta-history", "request-started");
  const { data, error } = await supabase.rpc("latest_ta_signals", { p_timeframe: timeframe, p_symbols: [...symbols] });
  if (error) throw error;
  return (data ?? []) as unknown as TaRowInput[];
}
