import type { SupabaseClient } from "@supabase/supabase-js";
import { FINAL_STATUSES, type ForwardResolution, type WalletState } from "./forward-contract";
import { newOutcomeRows, type StoredOpenSetup } from "./forward-run.server";
import type { MarketStage, SharedRepository } from "./forward-stages.server";
import { createHostedTraffic, jsonBytes, type HostedTraffic } from "./forward-hosted-traffic.server";

// The hourly dependencies are recreated, but the client (and these hydrated stages) live for the process.
const stageCaches = new WeakMap<SupabaseClient, Map<number, MarketStage>>();

function fail(error: { message: string } | null, operation: string) {
  if (error) throw new Error(`Forward ${operation} failed: ${error.message}`);
}

function processedCursor(value: unknown): Record<string, number> {
  if (value == null) return {};
  if (typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid market processed cursor");
  const cursor: Record<string, number> = {};
  for (const [symbol, ms] of Object.entries(value)) {
    if (typeof ms !== "number" || !Number.isSafeInteger(ms) || ms < 0)
      throw new Error(`Invalid market processed cursor: ${symbol}`);
    cursor[symbol] = ms;
  }
  return cursor;
}

/** Service-only writes; authenticated reads are enforced separately by table RLS. */
export function createSharedRepository(client: SupabaseClient, traffic: HostedTraffic = createHostedTraffic(),
  log: (line: string) => void = console.log): SharedRepository {
  let cache = stageCaches.get(client);
  if (!cache) { cache = new Map(); stageCaches.set(client, cache); }
  const stages = cache;
  function remember(stage: MarketStage) {
    stages.set(stage.boundary_ms, stage);
    // Keep two days of normal hourly replays without unbounded event/path memory growth.
    while (stages.size > 48) stages.delete(Math.min(...stages.keys()));
    return stage;
  }
  // Keyset pagination avoids PostgREST's row cap truncating a stage or open setup set.
  async function pages(table: string, key: string, build: (q: ReturnType<typeof client.from>) => any) {
    const all: Record<string, any>[] = [];
    let cursor: string | number | undefined;
    for (;;) {
      let query = build(client.from(table)).order(key, { ascending: true }).limit(500);
      if (cursor !== undefined) query = query.gt(key, cursor);
      const { data, error } = await query;
      traffic.read(data);
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
    return remember({ id: row["id"], boundary_ms: Number(row["boundary_ms"]), response: row["payload"],
      freshness: row["freshness"], events, adverse_paths: paths.map((r) => r["payload"]) });
  }
  async function stage(boundary: number) {
    const cached = stages.get(boundary);
    if (cached) return cached;
    const { data, error } = await client.from("forward_market_stages").select("*").eq("boundary_ms", boundary).maybeSingle();
    traffic.read(data);
    fail(error, "market stage read");
    return data ? hydrate(data) : null;
  }
  return {
    traffic,
    stage,
    async stageId(boundary) {
      const { data, error } = await client.from("forward_market_stages").select("id").eq("boundary_ms", boundary).maybeSingle();
      traffic.read(data);
      fail(error, "market stage existence read");
      return data?.id ?? null;
    },
    async latestStage() {
      const { data, error } = await client.from("forward_market_stages").select("id,boundary_ms,processed_to_ms:payload->processed_to_ms")
        .order("boundary_ms", { ascending: false }).limit(1).maybeSingle();
      traffic.read(data);
      fail(error, "latest market stage read");
      return data ? { id: data.id, boundary_ms: Number(data.boundary_ms), processed_to_ms: processedCursor(data.processed_to_ms) } : null;
    },
    async stagesAfter(cursor, boundary, current) {
      if (current) remember(current);
      const records = await pages("forward_market_stages", "boundary_ms", (q) => q.select("id,boundary_ms")
        .gt("boundary_ms", cursor).lte("boundary_ms", boundary));
      const stages: MarketStage[] = [];
      for (const row of records) {
        const value = await stage(Number(row["boundary_ms"]));
        if (!value || value.id !== row["id"]) throw new Error("Market stage missing or changed during replay");
        stages.push(value);
      }
      return stages;
    },
    async openSetups() {
      const setups = await pages("forward_market_open_setups", "setup_id", (q) => q.select("setup_id,payload"));
      const open: StoredOpenSetup[] = [];
      for (const row of setups) {
        const { data, error } = await client.from("forward_market_outcomes").select("status,payload")
          .eq("setup_id", row["setup_id"]).order("created_at", { ascending: false });
        traffic.read(data);
        fail(error, "market outcomes read");
        if ((data ?? []).some((r) => FINAL_STATUSES.has(r.status))) continue;
        open.push({ setup: row["payload"], resolution: (data?.[0]?.payload as ForwardResolution | undefined) ?? null });
      }
      return open;
    },
    async commitMarket(stage, previous, candleVersions) {
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
      const args = {
        p_stage: { id: stage.id, boundary_ms: stage.boundary_ms, versions: response.versions,
          params_hash: response.params_hash, payload: response, freshness: stage.freshness },
        p_records: records, p_states: [], p_expected: {},
      };
      stage.freshness["records_bytes"] = jsonBytes(records);
      const before = traffic.snapshot();
      stage.freshness["read_bytes"] = before.read_bytes;
      // Sizes include the freshness fields themselves. Iterate until their decimal widths settle.
      stage.freshness["stage_bytes"] = 0;
      stage.freshness["write_bytes"] = before.write_bytes;
      for (;;) {
        const stageBytes = jsonBytes(args.p_stage), writeBytes = before.write_bytes + jsonBytes(args);
        if (stage.freshness["stage_bytes"] === stageBytes && stage.freshness["write_bytes"] === writeBytes) break;
        stage.freshness["stage_bytes"] = stageBytes;
        stage.freshness["write_bytes"] = writeBytes;
      }
      traffic.write(args);
      const started = Date.now();
      try {
        const { data, error } = await client.rpc("commit_market_stage", args);
        fail(error, "market commit");
        if (typeof data !== "string") throw new Error("Market commit returned no stage id");
        if (data === stage.id) remember(stage);
        return data;
      } finally {
        log(`[forward-commit] boundary=${new Date(stage.boundary_ms).toISOString()} stage_bytes=${stage.freshness["stage_bytes"]}` +
          ` records_bytes=${stage.freshness["records_bytes"]} events=${stage.events.length}` +
          ` adverse_paths=${stage.adverse_paths.length} rpc_ms=${Date.now() - started}`);
      }
    },
    async account(userId, boundary) {
      const init = { p_user_id: userId, p_boundary_ms: boundary };
      traffic.write(init);
      const { error: initError } = await client.rpc("ensure_forward_account", init);
      fail(initError, "account initialization");
      const { data: account, error } = await client.from("forward_wallet_accounts").select("*").eq("user_id", userId).single();
      traffic.read(account);
      fail(error, "account read");
      if (!account) throw new Error("Forward wallet account missing after initialization");
      const { data: latest, error: runError } = await client.from("paper_runs")
        .select("id,boundary_ms,consumed_boundary_ms,wallet_state").eq("user_id", userId).eq("paper_account", account.account_id)
        .eq("status", "ok").order("boundary_ms", { ascending: false }).order("created_at", { ascending: false })
        .order("id", { ascending: false }).limit(1).maybeSingle();
      traffic.read(latest); // Includes wallet_state, which can grow with account history.
      fail(runError, "user cursor read");
      return { userId, accountId: account.account_id, config: account.config,
        cursor: Math.max(Number(account.start_boundary_ms), Number(latest?.consumed_boundary_ms ?? latest?.boundary_ms ?? 0)),
        startBoundary: Number(account.start_boundary_ms), previousRunId: latest?.id ?? null, state: (latest?.wallet_state as WalletState | null) ?? null };
    },
    async commitUser(account, stage, response, trigger) {
      const freshness = { ...stage.freshness, ...traffic.snapshot() };
      const args = {
        p_user_id: account.userId, p_stage_run_id: stage.id, p_expected_run_id: account.previousRunId,
        p_run: { trigger, status: "ok", processed_to_ms: response.processed_to_ms, versions: response.versions,
          params_hash: response.params_hash, wallet_config: response.wallet_config, wallet_state: response.wallet_state,
          assumptions: response.assumptions, reasons: response.reasons, freshness },
        p_ledger: response.ledger,
      };
      const before = traffic.snapshot().write_bytes;
      for (;;) {
        const bytes = before + jsonBytes(args);
        if (freshness.write_bytes === bytes) break;
        freshness.write_bytes = bytes;
      }
      traffic.write(args);
      const { error } = await client.rpc("commit_user_run", args);
      fail(error, "user commit");
    },
    async retryable(userId, boundary, status, reason) {
      const { data: account, error: accountError } = await client.from("forward_wallet_accounts")
        .select("account_id").eq("user_id", userId).single();
      traffic.read(account);
      fail(accountError, "retry account read");
      if (!account) throw new Error("Forward retry wallet account missing");
      const row = { user_id: userId, paper_account: account.account_id,
        run_key: `hour:${boundary}`, boundary_ms: boundary, status, trigger: "hourly", reason: reason.slice(0, 2000) };
      traffic.write(row);
      const { error } = await client.from("paper_runs").insert(row);
      fail(error, "retry audit");
    },
  };
}
