import type { SupabaseClient } from "@supabase/supabase-js";
import { FINAL_STATUSES, type ForwardResolution, type WalletState } from "./forward-contract";
import { newOutcomeRows, type StoredOpenSetup } from "./forward-run.server";
import type { MarketStage, SharedRepository } from "./forward-stages.server";

function fail(error: { message: string } | null, operation: string) {
  if (error) throw new Error(`Forward ${operation} failed: ${error.message}`);
}

/** Service-only writes; authenticated reads are enforced separately by table RLS. */
export function createSharedRepository(client: SupabaseClient): SharedRepository {
  // Keyset pagination avoids PostgREST's row cap truncating a stage or open setup set.
  async function pages(table: string, key: string, build: (q: ReturnType<typeof client.from>) => any) {
    const all: Record<string, any>[] = [];
    let cursor: string | number | undefined;
    for (;;) {
      let query = build(client.from(table)).order(key, { ascending: true }).limit(500);
      if (cursor !== undefined) query = query.gt(key, cursor);
      const { data, error } = await query;
      fail(error, `${table} read`);
      if (!data?.length) return all;
      all.push(...data);
      cursor = data[data.length - 1][key];
      if (data.length < 500) return all;
    }
  }
  async function hydrate(row: Record<string, any>): Promise<MarketStage> {
    // Sequence is per symbol; paginate each symbol separately.
    const events: MarketStage["events"] = [];
    for (const symbol of Object.keys(row["payload"]["processed_to_ms"])) {
      const rows = await pages("forward_market_events", "sequence", (q) => q.select("sequence,payload")
        .eq("stage_run_id", row["id"]).eq("symbol", symbol));
      events.push(...rows.map((r) => r["payload"]));
    }
    const paths = await pages("forward_market_paths", "setup_id", (q) => q.select("setup_id,payload").eq("stage_run_id", row["id"]));
    return { id: row["id"], boundary_ms: Number(row["boundary_ms"]), response: row["payload"],
      freshness: row["freshness"], events, adverse_paths: paths.map((r) => r["payload"]) };
  }
  return {
    async stage(boundary) {
      const { data, error } = await client.from("forward_market_stages").select("*").eq("boundary_ms", boundary).maybeSingle();
      fail(error, "market stage read");
      return data ? hydrate(data) : null;
    },
    async latestStage() {
      const { data, error } = await client.from("forward_market_stages").select("*")
        .order("boundary_ms", { ascending: false }).limit(1).maybeSingle();
      fail(error, "latest market stage read");
      return data ? hydrate(data) : null;
    },
    async stagesAfter(cursor, boundary) {
      const records = await pages("forward_market_stages", "boundary_ms", (q) => q.select("*")
        .gt("boundary_ms", cursor).lte("boundary_ms", boundary));
      const stages: MarketStage[] = [];
      for (const row of records) stages.push(await hydrate(row));
      return stages;
    },
    async openSetups() {
      const setups = await pages("forward_market_open_setups", "setup_id", (q) => q.select("setup_id,payload"));
      const open: StoredOpenSetup[] = [];
      for (const row of setups) {
        const { data, error } = await client.from("forward_market_outcomes").select("status,payload")
          .eq("setup_id", row["setup_id"]).order("created_at", { ascending: false });
        fail(error, "market outcomes read");
        if ((data ?? []).some((r) => FINAL_STATUSES.has(r.status))) continue;
        open.push({ setup: row["payload"], resolution: (data?.[0]?.payload as ForwardResolution | undefined) ?? null });
      }
      return open;
    },
    async commitMarket(stage, previous, candleVersions, states, expected) {
      const response = stage.response;
      const records = {
        signals: response.signals.map((signal) => ({ ...signal, candle_version: candleVersions.get(signal.signal_id) ?? null })),
        setups: response.setups.map((setup) => ({
          setup_id: setup.setup_id, signal_id: setup.signal_id, strategy_id: setup.strategy_id,
          version: setup.version, symbol: setup.symbol, side: setup.side, horizon_min: setup.horizon_min,
          signal_ms: setup.signal_ms, entry_ms: setup.entry_ms, k: setup.k, rr: setup.rr, status: setup.status,
          params_hash: setup.params_hash, candle_version: candleVersions.get(setup.signal_id) ?? null, payload: setup,
        })),
        outcomes: newOutcomeRows(response, previous), events: stage.events, adverse_paths: stage.adverse_paths,
      };
      const { error } = await client.rpc("commit_market_stage", {
        p_stage: { id: stage.id, boundary_ms: stage.boundary_ms, versions: response.versions,
          params_hash: response.params_hash, payload: response, freshness: stage.freshness },
        p_records: records, p_states: states, p_expected: expected,
      });
      fail(error, "market commit");
    },
    async account(userId, boundary) {
      const { error: initError } = await client.rpc("ensure_forward_account", { p_user_id: userId, p_boundary_ms: boundary });
      fail(initError, "account initialization");
      const { data: account, error } = await client.from("forward_wallet_accounts").select("*").eq("user_id", userId).single();
      fail(error, "account read");
      const { data: latest, error: runError } = await client.from("paper_runs")
        .select("id,boundary_ms,consumed_boundary_ms,wallet_state").eq("user_id", userId).eq("paper_account", account.account_id)
        .eq("status", "ok").order("boundary_ms", { ascending: false }).order("created_at", { ascending: false })
        .order("id", { ascending: false }).limit(1).maybeSingle();
      fail(runError, "user cursor read");
      return { userId, accountId: account.account_id, config: account.config,
        cursor: Math.max(Number(account.start_boundary_ms), Number(latest?.consumed_boundary_ms ?? latest?.boundary_ms ?? 0)),
        startBoundary: Number(account.start_boundary_ms), previousRunId: latest?.id ?? null, state: (latest?.wallet_state as WalletState | null) ?? null };
    },
    async commitUser(account, stage, response, trigger) {
      const { error } = await client.rpc("commit_user_run", {
        p_user_id: account.userId, p_stage_run_id: stage.id, p_expected_run_id: account.previousRunId,
        p_run: { trigger, status: "ok", processed_to_ms: response.processed_to_ms, versions: response.versions,
          params_hash: response.params_hash, wallet_config: response.wallet_config, wallet_state: response.wallet_state,
          assumptions: response.assumptions, reasons: response.reasons, freshness: stage.freshness },
        p_ledger: response.ledger,
      });
      fail(error, "user commit");
    },
    async retryable(userId, boundary, status, reason) {
      const { data: account, error: accountError } = await client.from("forward_wallet_accounts")
        .select("account_id").eq("user_id", userId).single();
      fail(accountError, "retry account read");
      const { error } = await client.from("paper_runs").insert({ user_id: userId, paper_account: account.account_id,
        run_key: `hour:${boundary}`, boundary_ms: boundary, status, trigger: "hourly", reason: reason.slice(0, 2000) });
      fail(error, "retry audit");
    },
  };
}
