import { logActivity } from "@/lib/activity-controls";
import { supabase } from "@/integrations/supabase/client";
import type { TaRowInput } from "./assessment";

/**
 * Newest saved TA snapshots of the watched pairs for one timeframe (RLS-scoped, one query per timeframe for all
 * pairs). The limit leaves room for a pair that has fallen a few candles behind the others.
 */
export async function loadLatestTa(timeframe: number, symbols: readonly string[]): Promise<TaRowInput[]> {
  if (symbols.length === 0) return [];
  logActivity(import.meta.env["VITE_ACTIVITY_DIAGNOSTICS"], "ta-history", "request-started");
  const { data, error } = await supabase
    .from("ta_signals")
    .select("symbol, timeframe, candle_at, detected_at, price, classification, score, atr_pct, indicators, factor_breakdown, patterns")
    .eq("timeframe", timeframe)
    .in("symbol", [...symbols])
    .order("candle_at", { ascending: false })
    .limit(symbols.length * 3);
  if (error) throw error;
  return (data ?? []) as unknown as TaRowInput[];
}
