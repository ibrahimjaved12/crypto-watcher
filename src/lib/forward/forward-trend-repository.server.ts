import { readTrendPages } from "./forward-trend-pages";
import type { SupabaseClient } from "@supabase/supabase-js";
import type { StoredTrendDecision } from "./forward-trend-contract";
import type { TrendRepository, TrendSnapshot } from "./forward-trend-run.server";

function fail(error: { message: string } | null, operation: string): void {
  if (error) throw new Error(`Forward trend ${operation} failed: ${error.message}`);
}

export function createTrendRepository(client: SupabaseClient): TrendRepository {
  return {
    async findOkRun(userId, runKey) {
      const { data, error } = await client
        .from("forward_trend_state")
        .select("id")
        .eq("user_id", userId)
        .eq("run_key", runKey)
        .eq("committed", true)
        .eq("status", "ok")
        .maybeSingle();
      fail(error, "run read");
      return Boolean(data);
    },
    async latestSnapshot(userId) {
      const { data, error } = await client
        .from("forward_trend_state")
        .select("id, params_hash, symbols, history_start_ms, track_start_ms, states")
        .eq("user_id", userId)
        .eq("committed", true)
        .order("revision", { ascending: false })
        .limit(1)
        .maybeSingle();
      fail(error, "state read");
      return data as TrendSnapshot | null;
    },
    readDecisions(userId, fromMs) {
      return readTrendPages<StoredTrendDecision>((offset) =>
        client
          .from("forward_trend_weights")
          .select(
            "track, day_ms, decided_from_close_ms, decided_at_ms, recorded_at_ms, weights, defined, sample_kind",
          )
          .eq("user_id", userId)
          .gte("day_ms", fromMs)
          .order("day_ms")
          .order("track")
          .range(offset, offset + 499),
      );
    },
    async commitRun(userId, expectedStateId, response, row) {
      const { data, error } = await client.rpc("commit_forward_trend_run", {
        p_user_id: userId,
        p_expected_state_id: expectedStateId,
        p_response: response,
        p_run: row,
      });
      fail(error, "transactional commit");
      return data as Record<string, number>;
    },
    async recordDiagnostic(userId, row) {
      const { error } = await client
        .from("forward_trend_state")
        .insert({ ...row, user_id: userId, committed: false });
      fail(error, "diagnostic insert");
    },
  };
}
