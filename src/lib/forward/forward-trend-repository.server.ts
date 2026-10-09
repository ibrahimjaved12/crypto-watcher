import type { SupabaseClient } from "@supabase/supabase-js";
import type { ForwardTrendResponse, TrendState } from "./forward-trend-contract";
import type { TrendRepository } from "./forward-trend-run.server";

/**
 * Application-DB persistence of the forward trend track (#239 P14), service role, account-scoped.
 * Inserts only; ledger and weight rows ignore duplicates on (user_id, track, day_ms), so a re-run is a
 * no-op. The state row of a run is written last.
 */
function fail(error: { message: string } | null, operation: string): void {
  if (error) throw new Error(`Forward trend ${operation} failed: ${error.message}`);
}

export function createTrendRepository(client: SupabaseClient): TrendRepository {
  async function insert(table: string, rows: Record<string, unknown>[]) {
    for (let i = 0; i < rows.length; i += 500) {
      const { error } = await client.from(table).upsert(rows.slice(i, i + 500),
        { onConflict: "user_id,track,day_ms", ignoreDuplicates: true });
      fail(error, `${table} insert`);
    }
    return rows.length;
  }

  return {
    async findOkRun(userId, runKey) {
      const { data, error } = await client.from("forward_trend_state").select("id")
        .eq("user_id", userId).eq("run_key", runKey).eq("status", "ok").maybeSingle();
      fail(error, "run read");
      return Boolean(data);
    },
    async latestStates(userId) {
      const { data, error } = await client.from("forward_trend_state").select("states")
        .eq("user_id", userId).order("created_at", { ascending: false }).limit(1).maybeSingle();
      fail(error, "state read");
      return ((data as { states: Record<string, TrendState> } | null)?.states) ?? null;
    },
    async persistRows(userId, runKey, response: ForwardTrendResponse) {
      const version = response.versions["trend_track"] ?? "unknown";
      const common = { user_id: userId, version, params_hash: response.params_hash, run_key: runKey };
      const ledger = await insert("forward_trend_ledger", response.ledger.map((row) => ({ ...row, ...common })));
      const weights = await insert("forward_trend_weights", response.weights.map((row) => ({ ...row, ...common })));
      return { ledger, weights };
    },
    async insertState(userId, row) {
      const { error } = await client.from("forward_trend_state").insert({ ...row, user_id: userId });
      fail(error, "state insert");
    },
  };
}
