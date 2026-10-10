import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { after, before, beforeEach, test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";
import { PGlite } from "@electric-sql/pglite";

async function moduleUrl(path, imports = {}) {
  const source = await readFile(new URL(path, import.meta.url), "utf8");
  let { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  });
  for (const [specifier, target] of Object.entries(imports))
    outputText = outputText.replaceAll(JSON.stringify(specifier), JSON.stringify(target));
  return `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`;
}
const stub = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;

// The operational repository now resolves canonical futures identity at runtime.
const symbolsStub = stub(`
  export const MARKET_SOURCE = "binance-usdm";
  export const MARKET_PRICE_TYPE = "trade";
  export function futuresInstrument(symbol) {
    const native = symbol.toUpperCase();
    return {
      id: "binance-usdm:" + native, exchange: "binance", nativeSymbol: native,
      marketType: "futures", contractType: "perpetual", baseAsset: native.replace(/USDT$/, ""),
      quoteAsset: "USDT", marginAsset: "USDT", settlementAsset: "USDT", linear: true,
      contractMultiplier: 1,
    };
  }
`);
const repositoryStubs = {
  "../market/completed-candle-contract": await moduleUrl("../src/lib/market/completed-candle-contract.ts"),
  "@supabase/supabase-js": stub("export const createClient=()=>({});"),
  "./config.server": stub("export const operationalDbConfig=()=>({enabled:false});"),
  "../market/symbols": symbolsStub,
};

const db = new PGlite();
const user = "00000000-0000-0000-0000-000000000001";
const other = "00000000-0000-0000-0000-000000000002";
const worker = "00000000-0000-0000-0000-000000000003";

before(async () => {
  await db.exec(`
    CREATE ROLE anon;
    CREATE ROLE authenticated;
    CREATE ROLE service_role BYPASSRLS;
    GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
  `);
  await db.exec(
    await readFile(
      new URL(
        "../operational-db/supabase/migrations/20261004000000_operational_schema.sql",
        import.meta.url,
      ),
      "utf8",
    ),
  );
});
beforeEach(async () => {
  await db.exec(`RESET ROLE;
    TRUNCATE sync_outbox, operational_results, monitor_runs,
      market_data_checkpoints, recent_candles, collector_recent_candles,
      collector_health, collector_leases, collector_subscriptions CASCADE;`);
});
after(() => db.close());

function candleRows(openTime = Math.floor(Date.now() / 60_000) * 60_000 - 60_000) {
  const closeTime = openTime + 60_000;
  return [
    {
      instrument_id: "binance-usdm:BTCUSDT",
      symbol: "BTCUSDT",
      native_symbol: "BTCUSDT",
      source: "binance-usdm",
      endpoint: "/fapi/v1/klines",
      price_type: "trade",
      timeframe_minutes: 1,
      open_time: new Date(openTime).toISOString(),
      close_time: new Date(closeTime).toISOString(),
      open: 100,
      high: 102,
      low: 99,
      close: 101,
      volume: 10,
      source_event_at: new Date(closeTime).toISOString(),
      collected_at: new Date(closeTime + 1_000).toISOString(),
    },
  ];
}

async function recordCandles(uid, rows = candleRows()) {
  return db.query("SELECT record_recent_candles($1,$2,7) AS changed", [uid, JSON.stringify(rows)]);
}

test("completed candles are account-scoped, idempotent and reject identity conflicts", async () => {
  await recordCandles(user);
  await recordCandles(user);
  await recordCandles(other);
  assert.equal((await db.query("SELECT count(*) FROM recent_candles")).rows[0].count, 2);
  const conflict = candleRows();
  conflict[0].close = 100.5;
  await assert.rejects(recordCandles(user, conflict), /Conflicting completed candle/);
  assert.equal(
    Number(
      (await db.query("SELECT close FROM recent_candles WHERE user_id=$1", [user])).rows[0].close,
    ),
    101,
  );
});

test("checkpoints are monotonic and recent-candle retention is bounded per account", async () => {
  const observed = Math.floor(Date.now() / 60_000) * 60_000 - 60_000;
  const checkpoint = (uid, at, price) =>
    db.query(
      `SELECT record_market_data_checkpoint(
        $1,'binance-usdm:BTCUSDT','BTCUSDT','BTCUSDT','binance-usdm',
        '/fapi/v1/klines','trade',1,$2,$3
      ) AS result`,
      [uid, new Date(at).toISOString(), price],
    );
  assert.equal((await checkpoint(user, observed, 100)).rows[0].result.status, "recorded");
  assert.equal(
    (await checkpoint(user, observed - 60_000, 90)).rows[0].result.status,
    "already_processed",
  );
  assert.equal(
    Number((await db.query("SELECT price FROM market_data_checkpoints")).rows[0].price),
    100,
  );

  const expired = candleRows(observed - 8 * 24 * 60 * 60_000);
  await db.query(
    `INSERT INTO recent_candles (
      user_id,instrument_id,symbol,native_symbol,source,endpoint,price_type,timeframe_minutes,
      open_time,close_time,open,high,low,close,volume,source_event_at,collected_at
    ) SELECT $1,x.* FROM jsonb_to_recordset($2) AS x(
      instrument_id text,symbol text,native_symbol text,source text,endpoint text,price_type text,
      timeframe_minutes int,open_time timestamptz,close_time timestamptz,open float8,high float8,
      low float8,close float8,volume float8,source_event_at timestamptz,collected_at timestamptz)`,
    [user, JSON.stringify(expired)],
  );
  await recordCandles(user);
  assert.equal(
    (await db.query("SELECT count(*) FROM recent_candles WHERE user_id=$1", [user])).rows[0].count,
    1,
  );
});

async function stage(resultId, eventId, uid = user, payload = { score: 1 }) {
  return db.query("SELECT stage_durable_result($1,$2,$3,'future.result',$4)", [
    resultId,
    eventId,
    uid,
    JSON.stringify(payload),
  ]);
}

test("result and outbox staging is atomic, stable and duplicate-safe", async () => {
  const result = "10000000-0000-0000-0000-000000000001";
  const event = "20000000-0000-0000-0000-000000000001";
  await stage(result, event);
  await stage(result, event);
  assert.equal((await db.query("SELECT count(*) FROM operational_results")).rows[0].count, 1);
  assert.equal((await db.query("SELECT count(*) FROM sync_outbox")).rows[0].count, 1);
  await assert.rejects(stage(result, event, other), /Stable result ID conflict/);
  assert.equal((await db.query("SELECT count(*) FROM operational_results")).rows[0].count, 1);
});

test("outbox leases retry safely, dead-letter, and confirm only after delivery", async () => {
  const firstResult = "10000000-0000-0000-0000-000000000001";
  const firstEvent = "20000000-0000-0000-0000-000000000001";
  await stage(firstResult, firstEvent);
  let claimed = (await db.query("SELECT * FROM claim_sync_outbox($1,10)", [worker])).rows;
  assert.equal(claimed.length, 1);
  assert.equal(claimed[0].attempts, 1);
  await db.query("SELECT mark_sync_outbox_failed($1,$2,'destination down',3)", [
    firstEvent,
    worker,
  ]);
  assert.equal((await db.query("SELECT status FROM sync_outbox")).rows[0].status, "failed");
  await db.exec("UPDATE sync_outbox SET available_at=clock_timestamp()-interval '1 second'");
  claimed = (await db.query("SELECT * FROM claim_sync_outbox($1,10)", [worker])).rows;
  assert.equal(claimed[0].attempts, 2);
  await db.query("SELECT mark_sync_outbox_delivered($1,$2)", [firstEvent, worker]);
  assert.equal((await db.query("SELECT status FROM sync_outbox")).rows[0].status, "delivered");
  await assert.rejects(
    db.query("SELECT mark_sync_outbox_delivered($1,$2)", [firstEvent, worker]),
    /lease is not owned/,
  );

  const deadResult = "10000000-0000-0000-0000-000000000002";
  const deadEvent = "20000000-0000-0000-0000-000000000002";
  await stage(deadResult, deadEvent);
  await db.query("SELECT * FROM claim_sync_outbox($1,10)", [worker]);
  await db.query("SELECT mark_sync_outbox_failed($1,$2,'invalid destination',1)", [
    deadEvent,
    worker,
  ]);
  assert.equal(
    (await db.query("SELECT status FROM sync_outbox WHERE event_id=$1", [deadEvent])).rows[0]
      .status,
    "dead",
  );
});

test("cleanup never removes unsynced durable results and diagnostics are measurable", async () => {
  const deliveredResult = "10000000-0000-0000-0000-000000000001";
  const deliveredEvent = "20000000-0000-0000-0000-000000000001";
  const pendingResult = "10000000-0000-0000-0000-000000000002";
  const pendingEvent = "20000000-0000-0000-0000-000000000002";
  await stage(deliveredResult, deliveredEvent);
  await stage(pendingResult, pendingEvent);
  await db.query("SELECT * FROM claim_sync_outbox($1,1)", [worker]);
  await db.query("SELECT mark_sync_outbox_delivered($1,$2)", [deliveredEvent, worker]);
  await db.exec(
    "UPDATE sync_outbox SET delivered_at=clock_timestamp()-interval '31 days' WHERE status='delivered'",
  );
  await db.query("SELECT purge_delivered_results(30)");
  assert.equal((await db.query("SELECT count(*) FROM operational_results")).rows[0].count, 1);
  assert.equal((await db.query("SELECT id FROM operational_results")).rows[0].id, pendingResult);
  const diagnostics = (await db.query("SELECT * FROM get_storage_diagnostics($1)", [user])).rows[0];
  assert.equal(diagnostics.pending_outbox_rows, 1);
  assert.ok(diagnostics.oldest_undelivered_at);
});

test("browser roles cannot read or mutate the service-role-only operational schema", async () => {
  await recordCandles(user);
  await db.exec("SET ROLE authenticated");
  try {
    await assert.rejects(db.query("SELECT * FROM recent_candles"), /permission denied/);
    await assert.rejects(recordCandles(user), /permission denied/);
    await assert.rejects(db.query("SELECT * FROM collector_subscriptions"), /permission denied/);
  } finally {
    await db.exec("RESET ROLE");
  }
});

test("the collector subscription universe is normalized and replaced as one shared set", async () => {
  await db.query(
    `SELECT assign_collector_subscriptions('{" ethusdt ","BTCUSDT","btcusdt"}'::text[])`,
  );
  const assigned = await db.query("SELECT get_collector_subscriptions() AS symbols");
  assert.deepEqual(assigned.rows[0].symbols, ["BTCUSDT", "ETHUSDT"]);
  // A later assignment replaces the set rather than accumulating.
  await db.query(`SELECT assign_collector_subscriptions('{"SOLUSDT"}'::text[])`);
  const replaced = await db.query("SELECT get_collector_subscriptions() AS symbols");
  assert.deepEqual(replaced.rows[0].symbols, ["SOLUSDT"]);
  // The application owns the set, so an empty universe is a valid assignment.
  await db.query(`SELECT assign_collector_subscriptions('{}'::text[])`);
  const empty = await db.query("SELECT get_collector_subscriptions() AS symbols");
  assert.deepEqual(empty.rows[0].symbols, []);
});

async function recordCollectorHealth(symbol, status = "LIVE") {
  return db.query("SELECT record_collector_health($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)", [
    `binance-usdm:${symbol}`, symbol, 1, status,
    "2026-09-26T12:01:00Z", "2026-09-26T12:00:00Z",
    250, 4, 3, "worker health detail",
  ]);
}

async function collectorHealth(symbol) {
  return (await db.query("SELECT * FROM collector_health WHERE symbol=$1", [symbol])).rows[0];
}

test("removing the last subscription invalidates current health and preserves factual history", async () => {
  await db.query("SELECT assign_collector_subscriptions($1)", [["BTCUSDT"]]);
  await recordCollectorHealth("BTCUSDT");
  const before = await collectorHealth("BTCUSDT");
  assert.equal(before.status, "LIVE");
  // Repair an old persisted row even though it was never in this assignment.
  await db.query(`INSERT INTO collector_health
    (instrument_id, symbol, timeframe_minutes, status)
    VALUES ('binance-usdm:ETHUSDT', 'ETHUSDT', 15, 'LIVE')`);
  const openTime = Math.floor(Date.now() / 60_000) * 60_000 - 60_000;
  await db.query("SELECT record_collector_candles($1,$2)", [JSON.stringify([{
    instrument_id: "binance-usdm:BTCUSDT", symbol: "BTCUSDT", native_symbol: "BTCUSDT",
    provider: "binance-usdm", endpoint: "/fapi/v1/klines", price_type: "trade",
    timeframe_minutes: 1, open_time: new Date(openTime).toISOString(),
    close_time: new Date(openTime + 59_999).toISOString(),
    open: 100, high: 102, low: 99, close: 101, volume: 10, quote_volume: 1010,
    source_event_at: null, received_at: new Date(openTime + 60_000).toISOString(),
    transport: "rest",
  }]), 8]);
  const candles = (await db.query("SELECT * FROM collector_recent_candles")).rows;

  await db.query("SELECT assign_collector_subscriptions($1)", [[]]);
  assert.deepEqual((await db.query("SELECT get_collector_subscriptions() AS symbols")).rows[0].symbols, []);
  const after = await collectorHealth("BTCUSDT");
  assert.equal(after.status, "UNAVAILABLE");
  assert.equal(after.error_message, "collection disabled/unsubscribed");
  assert.equal(after.lag_ms, null);
  assert.equal(after.queue_depth, 0);
  const orphan = await collectorHealth("ETHUSDT");
  assert.equal(orphan.status, "UNAVAILABLE");
  assert.equal(orphan.error_message, "collection disabled/unsubscribed");
  for (const field of ["last_event_at", "last_completed_open_time", "reconnect_count"])
    assert.deepEqual(after[field], before[field]);
  assert.ok(after.updated_at >= before.updated_at);
  assert.equal(candles.length, 1);
  assert.deepEqual((await db.query("SELECT * FROM collector_recent_candles")).rows, candles);
});

test("removing one subscription leaves another symbol's active health unchanged", async () => {
  await db.query("SELECT assign_collector_subscriptions($1)", [["BTCUSDT", "ETHUSDT"]]);
  await recordCollectorHealth("BTCUSDT");
  await recordCollectorHealth("ETHUSDT");
  const eth = await collectorHealth("ETHUSDT");
  assert.equal(eth.status, "LIVE");
  await db.query("SELECT assign_collector_subscriptions($1)", [["ETHUSDT"]]);
  assert.equal((await collectorHealth("BTCUSDT")).status, "UNAVAILABLE");
  assert.equal((await collectorHealth("BTCUSDT")).error_message, "collection disabled/unsubscribed");
  assert.deepEqual(await collectorHealth("ETHUSDT"), eth);
});

test("late health intents cannot resurrect an unsubscribed symbol", async () => {
  // No assignment row is also an empty authoritative universe.
  await recordCollectorHealth("ETHUSDT");
  assert.equal((await collectorHealth("ETHUSDT")).status, "UNAVAILABLE");
  await db.query("SELECT assign_collector_subscriptions($1)", [["BTCUSDT"]]);
  await recordCollectorHealth("BTCUSDT");
  const factual = await collectorHealth("BTCUSDT");
  assert.equal(factual.status, "LIVE");
  await db.query("SELECT assign_collector_subscriptions($1)", [[]]);
  for (const status of ["LIVE", "RECOVERING", "STALE", "UNAVAILABLE"]) {
    await recordCollectorHealth("BTCUSDT", status);
    const health = await collectorHealth("BTCUSDT");
    assert.equal(health.status, "UNAVAILABLE");
    assert.equal(health.error_message, "collection disabled/unsubscribed");
    assert.equal(health.lag_ms, null);
    assert.equal(health.queue_depth, 0);
    for (const field of ["last_event_at", "last_completed_open_time", "reconnect_count"])
      assert.deepEqual(health[field], factual[field]);
  }
});

test("reassignment leaves health unavailable until the worker reports recovery and live evidence", async () => {
  await db.query("SELECT assign_collector_subscriptions($1)", [["BTCUSDT"]]);
  await recordCollectorHealth("BTCUSDT");
  await db.query("SELECT assign_collector_subscriptions($1)", [[]]);
  const unavailable = await collectorHealth("BTCUSDT");
  assert.equal(unavailable.status, "UNAVAILABLE");
  assert.equal(unavailable.error_message, "collection disabled/unsubscribed");
  await db.query("SELECT assign_collector_subscriptions($1)", [["BTCUSDT"]]);
  assert.deepEqual(await collectorHealth("BTCUSDT"), unavailable);
  for (const status of ["RECOVERING", "LIVE"]) {
    await recordCollectorHealth("BTCUSDT", status);
    const health = await collectorHealth("BTCUSDT");
    assert.equal(health.status, status);
    assert.equal(health.error_message, "worker health detail");
    assert.equal(Number(health.lag_ms), 250);
    assert.equal(health.queue_depth, 4);
  }
});

test("server config is opt-in, bounded and requires a separate secure target", async () => {
  const configUrl = await moduleUrl("../src/lib/operational/config.server.ts");
  const { operationalDbConfig } = await import(configUrl);
  assert.deepEqual(operationalDbConfig({}), { enabled: false });
  assert.throws(() => operationalDbConfig({ OPERATIONAL_DB_ENABLED: "yes" }), /true or false/);
  assert.throws(() => operationalDbConfig({ OPERATIONAL_DB_ENABLED: "true" }), /are required/);
  const configured = operationalDbConfig({
    APP_PROFILE: "local",
    OPERATIONAL_DB_ENABLED: "true",
    OPERATIONAL_SUPABASE_URL: "http://127.0.0.1:55321",
    OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY: "service-only",
  });
  assert.equal(configured.enabled, true);
  assert.equal(configured.candleRetentionDays, 8);
  assert.throws(
    () =>
      operationalDbConfig({
        APP_PROFILE: "local",
        OPERATIONAL_DB_ENABLED: "true",
        SUPABASE_URL: "http://127.0.0.1:55321",
        OPERATIONAL_SUPABASE_URL: "http://127.0.0.1:55321",
        OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY: "service-only",
      }),
    /must be separate/,
  );
});

test("repository reads and writes always carry the authenticated user scope", async () => {
  const repositoryUrl = await moduleUrl(
    "../src/lib/operational/repository.server.ts",
    repositoryStubs,
  );
  const { createOperationalStore } = await import(repositoryUrl);
  const seen = { predicates: [], rpc: [] };
  const query = {
    select() {
      return this;
    },
    eq(column, value) {
      seen.predicates.push([column, value]);
      return this;
    },
    order() {
      return this;
    },
    async limit() {
      return { data: [], error: null };
    },
  };
  const client = {
    from: () => query,
    async rpc(name, args) {
      seen.rpc.push([name, args]);
      return name === "get_storage_diagnostics"
        ? {
            data: {
              recent_candle_rows: 0,
              checkpoint_rows: 0,
              monitor_run_rows: 0,
              pending_outbox_rows: 0,
              failed_outbox_rows: 0,
              dead_outbox_rows: 0,
              oldest_candle_at: null,
              oldest_undelivered_at: null,
            },
            error: null,
          }
        : { data: null, error: null };
    },
  };
  const store = createOperationalStore(client, {
    candleRetentionDays: 7,
    monitorRunRetentionDays: 30,
    outboxMaxAttempts: 10,
  });
  await store.listMonitorRuns(user);
  await store.diagnostics(user);
  assert.deepEqual(seen.predicates, [["user_id", user]]);
  assert.equal(seen.rpc[0][1].p_user_id, user);
});

test("market movement append rejects null and unexpected RPC statuses", async () => {
  const repositoryUrl = await moduleUrl(
    "../src/lib/operational/repository.server.ts",
    repositoryStubs,
  );
  const { createOperationalStore } = await import(repositoryUrl);
  let status = null;
  const store = createOperationalStore(
    {
      async rpc() {
        return { data: status, error: null };
      },
    },
    { candleRetentionDays: 7, monitorRunRetentionDays: 30, outboxMaxAttempts: 10 },
  );

  const event = { event_id: "example-event" };
  await assert.rejects(store.appendMarketMovementEvent(event), /returned null/);
  status = "unexpected";
  await assert.rejects(store.appendMarketMovementEvent(event), /returned unexpected/);
  status = "appended";
  assert.equal(await store.appendMarketMovementEvent(event), "appended");
});

test("market movement repository preserves canonical lifecycle JSON and event evidence", async () => {
  const repositoryUrl = await moduleUrl(
    "../src/lib/operational/repository.server.ts", repositoryStubs,
  );
  const { createOperationalStore } = await import(repositoryUrl);
  const calls = [];
  const boundary = 1_800_000_000_000;
  const event = {
    event_id: "event-null-acceleration", episode_id: "episode-null-acceleration",
    evaluation_boundary_time_ms: boundary + 5_000,
    median_acceleration: { available: false, value: null, reason: "ACCELERATION_UNAVAILABLE" },
    acceleration_breadth: { available: false, value: null, reason: "ACCELERATION_UNAVAILABLE" },
    source_time_evidence: [{ symbol: "BTCUSDT", last_real_trade_time_ms: null }],
  };
  const row = { event_id: event.event_id, canonical_event: event };
  const client = {
    async rpc(name, args) {
      calls.push({ name, args });
      return { data: name === "persist_market_episode_lifecycle_step"
        ? [{ eventId: row.event_id, status: "appended" }] : "appended", error: null };
    },
    from(name) {
      assert.equal(name, "market_movement_events");
      return { select: () => ({ eq: () => ({ order: async () => ({
        data: [row], error: null,
      }) }) }) };
    },
  };
  const store = createOperationalStore(client, {
    candleRetentionDays: 7, monitorRunRetentionDays: 30, outboxMaxAttempts: 10,
  });
  assert.equal(await store.appendMarketMovementEvent(event), "appended");
  assert.deepEqual(calls[0].args.p_canonical_event, event);
  const lifecycleState = { serialization_version: "market-episode-state-v1",
    opaque: { pending_reversal: [1, 2] } };
  await store.persistMarketEpisodeLifecycleStep(
    { evaluationBoundaryTime: boundary + 5_000, lifecycleState }, [event],
  );
  assert.deepEqual(calls[1].args.p_current_state.lifecycleState, lifecycleState);
  assert.deepEqual(calls[1].args.p_events[0], event);
  const [read] = await store.listMarketMovementEvents(event.episode_id);
  assert.deepEqual(read, event);
});

test("atomic lifecycle SQL keeps canonical JSON and nullable flattened evidence", async () => {
  const boundary = 1_800_000_000_000;
  const oldUniverse = `old-${crypto.randomUUID()}`;
  const newUniverse = `new-${crypto.randomUUID()}`;
  const scope = {
    lifecycle_algorithm_version: "market-episode-lifecycle-v1",
    lifecycle_config_version: "market-episode-lifecycle-config-v1",
    universe_id: oldUniverse, universe_version: "v1", primary_window_minutes: 5,
    classifier_algorithm_version: "market-state-classifier-v1",
    classifier_config_version: "market-state-classifier-config-v1",
    movement_algorithm_version: "market-movement-v1",
    movement_config_version: "market-movement-config-v1",
    provider: "binance-usdm", exchange: "binance", price_type: "trade",
  };
  const missing = (reason) => ({ available: false, value: null, reason });
  const event = {
    event_id: `event-${crypto.randomUUID()}`, episode_id: `episode-${crypto.randomUUID()}`,
    previous_episode_id: null, transition: "ENDED", transition_reason: "universe_changed",
    from_direction: "BROAD_RISE", to_direction: null, event_family: "BROAD_MOVE",
    episode_direction: "BROAD_RISE", episode_start_boundary_time_ms: boundary - 5_000,
    evaluation_boundary_time_ms: boundary, episode_scope: scope,
    evaluation_scope: { ...scope, universe_id: newUniverse, universe_version: "v2" },
    pace: missing("NO_BROAD_DIRECTION"),
    directional_breadth: missing("MARKET_UNIVERSE_INELIGIBLE"),
    material_breadth: missing("MARKET_UNIVERSE_INELIGIBLE"),
    median_raw_return: missing("MARKET_UNIVERSE_INELIGIBLE"),
    median_normalized_movement: missing("MARKET_UNIVERSE_INELIGIBLE"),
    median_acceleration: missing("ACCELERATION_UNAVAILABLE"),
    acceleration_breadth: missing("ACCELERATION_UNAVAILABLE"),
    dispersion_mad_normalized_movement: missing("MARKET_UNIVERSE_INELIGIBLE"),
    volume_context: [], isolated_outliers: [], supporting_contracts: [],
    conflicting_contracts: [], configured_universe: ["BTCUSDT"], included_symbols: [],
    excluded_symbols: [{ symbol: "BTCUSDT", reasons: ["WARMING_INSUFFICIENT_LIVE_HISTORY"] }],
    windows_context: [{ window_minutes: 1 }, { window_minutes: 5 }, { window_minutes: 15 }],
    source_time_evidence: [{ symbol: "BTCUSDT", last_real_trade_time_ms: null,
      last_real_event_time_ms: null, last_received_at_ms: null }],
    classification: { evaluation_boundary_time_ms: boundary },
  };
  const lifecycleState = { serialization_version: "market-episode-state-v1",
    opaque: { pending_start: ["BTCUSDT", 1] } };
  const current = {
    universeId: newUniverse, primaryWindowMinutes: 5, universeVersion: "v2",
    provider: "binance-usdm", exchange: "binance", priceType: "trade",
    evaluationBoundaryTime: boundary, directionState: "WARMING", pace: "NOT_APPLICABLE",
    activeEpisodeId: null, activeDirection: null, interrupted: false,
    episodeAlgorithmVersion: scope.lifecycle_algorithm_version,
    lifecycleConfigVersion: scope.lifecycle_config_version,
    classifierAlgorithmVersion: scope.classifier_algorithm_version,
    classifierConfigVersion: scope.classifier_config_version,
    movementAlgorithmVersion: scope.movement_algorithm_version,
    movementConfigVersion: scope.movement_config_version,
    lifecycleState, currentEvidence: { primaryWindow: { directionState: "WARMING" } },
  };
  const persisted = await db.query(
    "SELECT public.persist_market_episode_lifecycle_step($1::jsonb, $2::jsonb) AS statuses",
    [JSON.stringify(current), JSON.stringify([event])],
  );
  assert.deepEqual(persisted.rows[0].statuses,
    [{ eventId: event.event_id, status: "appended" }]);
  const savedCurrent = await db.query(
    "SELECT lifecycle_state FROM public.market_state_current WHERE universe_id = $1",
    [newUniverse],
  );
  assert.deepEqual(savedCurrent.rows[0].lifecycle_state, lifecycleState);
  const savedEvent = await db.query(
    "SELECT canonical_event, previous_episode_id, directional_breadth, material_breadth, " +
      "median_acceleration, acceleration_breadth, pace FROM public.market_movement_events " +
      "WHERE event_id = $1",
    [event.event_id],
  );
  assert.deepEqual(savedEvent.rows[0].canonical_event, event);
  assert.equal(savedEvent.rows[0].previous_episode_id, null);
  assert.equal(savedEvent.rows[0].directional_breadth, null);
  assert.equal(savedEvent.rows[0].material_breadth, null);
  assert.equal(savedEvent.rows[0].median_acceleration, null);
  assert.equal(savedEvent.rows[0].acceleration_breadth, null);
  assert.equal(savedEvent.rows[0].pace, "NOT_APPLICABLE");
  const replay = await db.query(
    "SELECT public.append_market_movement_event($1::jsonb) AS status", [JSON.stringify(event)],
  );
  assert.equal(replay.rows[0].status, "already_exists");
});

test("outbox delivery confirms only after sink success", async () => {
  const outboxUrl = await moduleUrl("../src/lib/operational/outbox.server.ts");
  const { deliverOutboxBatch } = await import(outboxUrl);
  const marked = [];
  const failed = [];
  const store = {
    enabled: true,
    claimOutbox: async () => [
      { eventId: "one", payload: {}, attempts: 1 },
      { eventId: "two", payload: {}, attempts: 1 },
    ],
    markOutboxDelivered: async (id) => marked.push(id),
    markOutboxFailed: async (id) => failed.push(id),
  };
  let batch;
  let result = await deliverOutboxBatch(
    store,
    {
      deliver: async (events) => {
        batch = events.map((event) => event.eventId);
      },
    },
    worker,
  );
  assert.deepEqual(batch, ["one", "two"]);
  assert.deepEqual(result, { claimed: 2, delivered: 2, failed: 0 });
  assert.deepEqual(marked, ["one", "two"]);
  assert.deepEqual(failed, []);

  marked.length = 0;
  result = await deliverOutboxBatch(
    store,
    { deliver: async () => Promise.reject(new Error("destination down")) },
    worker,
  );
  assert.deepEqual(result, { claimed: 2, delivered: 0, failed: 2 });
  assert.deepEqual(marked, []);
  assert.deepEqual(failed, ["one", "two"]);
});

test("movement normalization history RPC returns compact one-minute candles per symbol", async () => {
  const observed = Math.floor(Date.now() / 60_000) * 60_000 - 3 * 60_000;
  const rows = [0, 1, 2].map((index) => {
    const openTime = observed + index * 60_000;
    return {
      instrument_id: "binance-usdm:BTCUSDT",
      symbol: "BTCUSDT",
      native_symbol: "BTCUSDT",
      provider: "binance-usdm",
      endpoint: "/fapi/v1/klines",
      price_type: "trade",
      timeframe_minutes: 1,
      open_time: new Date(openTime).toISOString(),
      close_time: new Date(openTime + 60_000 - 1).toISOString(),
      open: 100,
      high: 110,
      low: 90,
      close: 100 + index,
      volume: 5,
      quote_volume: 551 + index,
      source_event_at: null,
      received_at: new Date(openTime + 60_000).toISOString(),
      transport: "rest",
    };
  });
  await db.query("SELECT record_collector_candles($1,$2)", [JSON.stringify(rows), 7]);

  const history = (
    await db.query("SELECT get_collector_movement_candles($1,$2,$3) AS history", [
      ["BTCUSDT"],
      new Date(observed - 60_000).toISOString(),
      new Date(observed + 10 * 60_000).toISOString(),
    ])
  ).rows[0].history;
  assert.equal(history.BTCUSDT.length, 3);
  assert.equal(history.BTCUSDT[0][0], observed);
  assert.equal(history.BTCUSDT[0][1], 100);
  assert.equal(history.BTCUSDT[2][1], 102);
  assert.equal(history.BTCUSDT[0][2], 5);
  assert.equal(history.BTCUSDT[0][3], 551);

  const empty = (
    await db.query("SELECT get_collector_movement_candles($1,$2,$3) AS history", [
      [],
      new Date(observed).toISOString(),
      new Date(observed + 10 * 60_000).toISOString(),
    ])
  ).rows[0].history;
  assert.deepEqual(empty, {});
});

test("collector persistence binds transport, endpoint and source event without clock ordering", async () => {
  const opening = Math.floor(Date.now() / 60_000) * 60_000 - 60_000;
  const rest = {
    provider: "binance-usdm", instrument_id: "binance-usdm:BTCUSDT", symbol: "BTCUSDT",
    native_symbol: "BTCUSDT", price_type: "trade", timeframe_minutes: 1,
    open_time: new Date(opening).toISOString(), close_time: new Date(opening + 59_999).toISOString(),
    open: 100, high: 102, low: 99, close: 101, volume: 2, quote_volume: 202,
    transport: "rest", endpoint: "/fapi/v1/klines", source_event_at: null,
    received_at: new Date(opening + 60_100).toISOString(),
  };
  const websocket = { ...rest, transport: "websocket", endpoint: "wss://fstream.binance.com/market/stream",
    source_event_at: new Date(opening + 60_107).toISOString() };
  for (const bad of [
    { ...rest, endpoint: websocket.endpoint },
    { ...websocket, endpoint: rest.endpoint },
    { ...rest, source_event_at: websocket.source_event_at },
    { ...websocket, source_event_at: null },
  ]) {
    await assert.rejects(db.query("SELECT record_collector_candles($1,7)", [JSON.stringify([bad])]),
      /collector_candle_provenance/);
    assert.equal((await db.query("SELECT count(*)::int AS count FROM collector_recent_candles")).rows[0].count, 0);
  }
  // Correct duplicates are admitted only after malformed first observations failed.
  await db.query("SELECT record_collector_candles($1,7)", [JSON.stringify([rest])]);
  await db.query("DELETE FROM collector_recent_candles");
  await db.query("SELECT record_collector_candles($1,7)", [JSON.stringify([websocket])]);
  const stored = (await db.query("SELECT get_collector_completed_candles('BTCUSDT',1,1) AS candles")).rows[0].candles[0];
  assert.equal(stored.source_event_at_ms, opening + 60_107);
  assert.equal(stored.received_at_ms, opening + 60_100);
  assert.ok(stored.received_at_ms < stored.source_event_at_ms);
});

// P15 candle conflict policy: stored candles are immutable, a conflict never fails the batch, it is
// recorded once as append-only evidence, health only counts it, and revisions never touch the stored row.
async function resetConflicts() {
  await db.exec("TRUNCATE collector_candle_revisions, collector_candle_conflicts;");
}

function collectorCandle(opening, index, overrides = {}, minutes = 1) {
  const duration = minutes * 60_000;
  return {
    provider: "binance-usdm", instrument_id: "binance-usdm:BTCUSDT", symbol: "BTCUSDT",
    native_symbol: "BTCUSDT", price_type: "trade", timeframe_minutes: minutes,
    open_time: new Date(opening + index * duration).toISOString(),
    close_time: new Date(opening + index * duration + duration - 1).toISOString(),
    open: 100, high: 102, low: 99, close: 101, volume: 2, quote_volume: 202,
    transport: "rest", endpoint: "/fapi/v1/klines", source_event_at: null,
    received_at: new Date(opening + index * duration + duration + 100).toISOString(), ...overrides,
  };
}

const countRows = async (table) =>
  (await db.query(`SELECT count(*)::int AS count FROM ${table}`)).rows[0].count;

test("a batch with one conflicting row writes the rest and records exactly one conflict (idempotent replay)", async () => {
  await resetConflicts();
  const opening = Math.floor(Date.now() / 60_000) * 60_000 - 5 * 60_000;
  const candle = (index, overrides) => collectorCandle(opening, index, overrides);
  await db.query("SELECT record_collector_candles($1,7)", [JSON.stringify([candle(0)])]);
  // Same identity, different volume (stream candle that missed trades vs. the REST candle).
  const conflicting = candle(0, { volume: 2.5, quote_volume: 252.5 });
  const batch = JSON.stringify([conflicting, candle(1), candle(0), candle(2)]);
  const written = (await db.query("SELECT * FROM record_collector_candles($1,7)", [batch])).rows;
  assert.equal(written.length, 2, "both new, non-conflicting candles are written");
  const rows = (await db.query("SELECT open_time, volume FROM collector_recent_candles ORDER BY open_time")).rows;
  assert.deepEqual(rows.map((row) => row.volume), [2, 2, 2], "the stored candle is never overwritten");
  assert.equal(await countRows("collector_candle_conflicts"), 1);
  const recorded = (await db.query("SELECT * FROM collector_candle_conflicts")).rows[0];
  assert.equal(recorded.stored.volume, 2);
  assert.equal(recorded.offered.volume, 2.5);
  assert.deepEqual(recorded.differing_fields, ["volume", "quote_volume"]);
  assert.equal(recorded.stored_transport, "rest");
  assert.equal(recorded.rest_side, "both");
  assert.match(recorded.stored_hash, /^[0-9a-f]{32}$/);
  assert.notEqual(recorded.stored_hash, recorded.offered_hash);
  // A retried recovery re-submits the same conflicting candle: no duplicate, still no failure.
  const replay = (await db.query("SELECT * FROM record_collector_candles($1,7)", [batch])).rows;
  assert.equal(replay.length, 0);
  assert.equal(await countRows("collector_candle_conflicts"), 1);
  await assert.rejects(db.query("DELETE FROM collector_candle_conflicts"), /append-only/);
  await assert.rejects(db.query("UPDATE collector_candle_conflicts SET rest_side = 'stored'"), /append-only/);
});

test("the observed failure: stream 15m candle then a REST candle with another volume stays LIVE", async () => {
  await resetConflicts();
  await db.query("SELECT assign_collector_subscriptions($1)", [["BTCUSDT"]]);
  const duration = 15 * 60_000;
  const opening = Math.floor(Date.now() / duration) * duration - 2 * duration;
  const stream = collectorCandle(opening, 0, {
    volume: 1006.95, quote_volume: 1006.95 * 101, transport: "websocket",
    endpoint: "wss://fstream.binance.com/market/stream",
    source_event_at: new Date(opening + duration + 10).toISOString(),
  }, 15);
  await db.query("SELECT record_collector_candles($1,7)", [JSON.stringify([stream])]);
  await db.query("SELECT record_collector_health($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)", [
    "binance-usdm:BTCUSDT", "BTCUSDT", 15, "LIVE", new Date().toISOString(),
    new Date(opening).toISOString(), 250, 0, 0, null]);
  // Recovery: REST batch with the same open time (different volume) and the next candle.
  const rest = collectorCandle(opening, 0, { volume: 1007.427, quote_volume: 1007.427 * 101 }, 15);
  const next = collectorCandle(opening, 1, {}, 15);
  const written = (await db.query("SELECT * FROM record_collector_candles($1,7)",
    [JSON.stringify([rest, next])])).rows;
  assert.equal(written.length, 1, "the recovery batch does not fail; its other candle is written");
  const stored = (await db.query(
    "SELECT volume, transport FROM collector_recent_candles WHERE timeframe_minutes = 15 ORDER BY open_time")).rows;
  assert.deepEqual(stored.map((row) => [row.volume, row.transport]), [[1006.95, "websocket"], [2, "rest"]]);
  const conflict = (await db.query("SELECT * FROM collector_candle_conflicts")).rows[0];
  assert.equal(conflict.rest_side, "offered");
  assert.equal(conflict.offered_transport, "rest");
  const health = (await db.query(
    "SELECT * FROM collector_health WHERE symbol = 'BTCUSDT' AND timeframe_minutes = 15")).rows[0];
  assert.equal(health.status, "LIVE", "a conflict never degrades health");
  assert.equal(health.conflict_count_24h, 1);
  assert.ok(health.last_conflict_at instanceof Date);
  // A later health write keeps LIVE and keeps the counters.
  await db.query("SELECT record_collector_health($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)", [
    "binance-usdm:BTCUSDT", "BTCUSDT", 15, "LIVE", new Date().toISOString(),
    new Date(opening + duration).toISOString(), 250, 0, 0, null]);
  const again = (await db.query(
    "SELECT status, conflict_count_24h FROM collector_health WHERE symbol = 'BTCUSDT' AND timeframe_minutes = 15")).rows[0];
  assert.deepEqual([again.status, again.conflict_count_24h], ["LIVE", 1]);

  // Reconcile: only conflicts older than the window; within tolerance; appended, never applied.
  assert.equal((await db.query("SELECT reconcile_conflicts(24) AS n")).rows[0].n, 0);
  assert.equal((await db.query("SELECT reconcile_conflicts(0) AS n")).rows[0].n, 1);
  assert.equal((await db.query("SELECT reconcile_conflicts(0) AS n")).rows[0].n, 0, "idempotent");
  const revision = (await db.query("SELECT * FROM collector_candle_revisions")).rows[0];
  assert.equal(revision.source, "rest");
  assert.equal(revision.supersedes, conflict.stored_hash);
  assert.equal(revision.fields.volume, 1007.427);
  assert.equal(revision.fields.candle_hash, conflict.offered_hash);
  const after = (await db.query(
    "SELECT volume, transport FROM collector_recent_candles WHERE timeframe_minutes = 15 ORDER BY open_time")).rows;
  assert.deepEqual(after, stored, "a revision never modifies the stored candle");
  await assert.rejects(db.query("DELETE FROM collector_candle_revisions"), /append-only/);
});

test("reconciliation skips conflicts outside the tolerance or without a REST offer", async () => {
  await resetConflicts();
  const opening = Math.floor(Date.now() / 60_000) * 60_000 - 10 * 60_000;
  const ws = (index, overrides = {}) => collectorCandle(opening, index, {
    transport: "websocket", endpoint: "wss://fstream.binance.com/market/stream",
    source_event_at: new Date(opening + index * 60_000 + 60_010).toISOString(), ...overrides });
  await db.query("SELECT record_collector_candles($1,7)", [JSON.stringify([ws(0), ws(1), ws(2)])]);
  await db.query("SELECT record_collector_candles($1,7)", [JSON.stringify([
    collectorCandle(opening, 0, { volume: 2.5, quote_volume: 252.5 }),  // REST, volume 25 % off: outside 2 %
    collectorCandle(opening, 1, { open: 100.5 }),  // REST, the open differs: never reconciled
    ws(2, { volume: 2.01, quote_volume: 203.01 }),  // stream vs stream: no REST side
  ])]);
  assert.equal(await countRows("collector_candle_conflicts"), 3);
  assert.equal((await db.query("SELECT reconcile_conflicts(0) AS n")).rows[0].n, 0);
  const sides = (await db.query("SELECT rest_side FROM collector_candle_conflicts ORDER BY open_time")).rows;
  assert.deepEqual(sides.map((row) => row.rest_side), ["offered", "offered", "neither"]);
  const versions = (await db.query(
    "SELECT get_collector_forward_minutes('BTCUSDT', $1, $2) AS rows",
    [new Date(opening).toISOString(), new Date(opening + 3 * 60_000).toISOString()])).rows[0].rows;
  assert.equal(versions.length, 3);
  assert.ok(versions.every((row) => row.length === 9 && /^[0-9a-f]{32}$/.test(row[8])));
});

test("collector TA candle RPC returns full ascending provenance for one frame", async () => {
  const duration = 15 * 60_000;
  const observed = Math.floor(Date.now() / duration) * duration - duration;
  const rows = [0, 1, 2].map((index) => {
    const openTime = observed + index * duration;
    return {
      instrument_id: "binance-usdm:BTCUSDT",
      symbol: "BTCUSDT",
      native_symbol: "BTCUSDT",
      provider: "binance-usdm",
      endpoint: index === 0 ? "/fapi/v1/klines" : "wss://fstream.binance.com/market/stream",
      price_type: "trade",
      timeframe_minutes: 15,
      open_time: new Date(openTime).toISOString(),
      close_time: new Date(openTime + duration - 1).toISOString(),
      open: 100,
      high: 110,
      low: 90,
      close: 100 + index,
      volume: 5,
      quote_volume: 505 + index,
      // REST bootstrap/recovery has no exchange event; WebSocket candles keep the
      // exchange's actual event time, which need not equal the completion boundary.
      source_event_at:
        index === 0 ? null : new Date(openTime + duration + (index === 1 ? -7 : 7)).toISOString(),
      // Row 2's receive time precedes its exchange event time: the obsolete
      // `received_at >= source_event_at` constraint is gone, so the raw receive time is
      // stored and transported unchanged instead of being clamped.
      received_at: new Date(openTime + duration + (index === 2 ? 3 : 250)).toISOString(),
      transport: index === 0 ? "rest" : "websocket",
    };
  });
  await db.query("SELECT record_collector_candles($1,$2)", [JSON.stringify(rows), 7]);

  const candles = (await db.query("SELECT get_collector_completed_candles('btcusdt',15,10) AS candles"))
    .rows[0].candles;
  assert.equal(candles.length, 3);
  // Ascending order, and every candle keeps its recorded provenance rather than a
  // market-level endpoint or a fabricated retrieval time.
  assert.deepEqual(
    candles.map((candle) => [
      candle.open_time_ms,
      candle.open,
      candle.high,
      candle.low,
      candle.close,
      candle.volume,
    ]),
    [
      [observed, 100, 110, 90, 100, 5],
      [observed + duration, 100, 110, 90, 101, 5],
      [observed + 2 * duration, 100, 110, 90, 102, 5],
    ],
  );
  assert.equal(candles[0].endpoint, "/fapi/v1/klines");
  assert.equal(candles[0].transport, "rest");
  assert.equal(candles[2].endpoint, "wss://fstream.binance.com/market/stream");
  assert.equal(candles[2].transport, "websocket");
  assert.equal(candles[2].provider, "binance-usdm");
  assert.equal(candles[2].instrument_id, "binance-usdm:BTCUSDT");
  assert.equal(candles[2].native_symbol, "BTCUSDT");
  assert.equal(candles[2].price_type, "trade");
  assert.equal(candles[2].symbol, "BTCUSDT");
  assert.equal(candles[2].market_type, "futures");
  assert.equal(candles[2].contract_type, "perpetual");
  assert.equal(candles[2].timeframe_minutes, 15);
  assert.equal(candles[2].quote_volume, 507);
  assert.equal(candles[2].close_time_ms, observed + 2 * duration + duration - 1);
  // The exchange event time is transported exactly as recorded (never rewritten to the
  // completion boundary), and a REST candle honestly reports no exchange event.
  assert.equal(candles[0].source_event_at_ms, null);
  assert.equal(candles[1].source_event_at_ms, observed + duration * 2 - 7);
  assert.equal(candles[2].source_event_at_ms, observed + 2 * duration + duration + 7);
  assert.equal(candles[2].received_at_ms, observed + 2 * duration + duration + 3);

  // Another timeframe or symbol has no history rather than leaking the wrong series.
  assert.deepEqual(
    (await db.query("SELECT get_collector_completed_candles('BTCUSDT',60,10) AS candles")).rows[0].candles,
    [],
  );
  assert.deepEqual(
    (await db.query("SELECT get_collector_completed_candles('ETHUSDT',15,10) AS candles")).rows[0].candles,
    [],
  );
  await assert.rejects(
    db.query("SELECT get_collector_completed_candles('BTCUSDT',7,10)"),
    /Invalid collector completed candle timeframe/,
  );
});

test("repository collector TA read transports recorded provenance without fabricating it", async () => {
  const repositoryUrl = await moduleUrl(
    "../src/lib/operational/repository.server.ts",
    repositoryStubs,
  );
  const { createOperationalStore } = await import(repositoryUrl);
  const base = 1_800_000_000_000;
  const step = 900_000;
  const row = (index, transport, endpoint, sourceEventTime = undefined) => ({
    provider: "binance-usdm",
    instrument_id: "binance-usdm:BTCUSDT",
    native_symbol: "BTCUSDT",
    symbol: "BTCUSDT", market_type: "futures", contract_type: "perpetual", timeframe_minutes: 15,
    price_type: "trade",
    endpoint,
    transport,
    open_time_ms: base + index * step,
    close_time_ms: base + index * step + step - 1,
    // REST rows have no exchange event; WebSocket rows carry the actual event time,
    // deliberately offset from the completion boundary to prove it is not rewritten.
    source_event_at_ms:
      sourceEventTime ?? (transport === "rest" ? null : base + index * step + step + 7),
    received_at_ms: base + index * step + step + 120,
    open: 100,
    high: 110,
    low: 90,
    close: 101,
    volume: 3,
    quote_volume: 303.5,
  });
  const rows = [
    row(0, "rest", "/fapi/v1/klines"),
    row(1, "websocket", "wss://fstream.binance.com/market/stream", base + 2 * step - 7),
  ];
  const store = createOperationalStore(
    {
      async rpc(name, args) {
        assert.equal(name, "get_collector_completed_candles");
        assert.deepEqual(args, {
          p_symbol: "BTCUSDT",
          p_timeframe_minutes: 15,
          p_limit: 260,
        });
        return { data: rows, error: null };
      },
    },
    { candleRetentionDays: 7, monitorRunRetentionDays: 30, outboxMaxAttempts: 10 },
  );
  const result = await store.readCollectorCompletedCandles("btcusdt", 15);
  assert.equal(result.identity.provider, "binance-usdm");
  assert.equal(result.identity.instrumentId, "binance-usdm:BTCUSDT");
  assert.equal(result.identity.timeframeMinutes, 15);
  assert.deepEqual(result.observations, rows.map((row) => ({
    candle: { openTime: row.open_time_ms, closeTime: row.close_time_ms, open: row.open,
      high: row.high, low: row.low, close: row.close, baseVolume: row.volume,
      quoteVolume: row.quote_volume },
    provenance: row.transport === "rest"
      ? { sourceKind: "rest", endpoint: row.endpoint, retrievedAt: row.received_at_ms }
      : { sourceKind: "websocket", endpoint: row.endpoint,
          sourceEventTime: row.source_event_at_ms, receivedAt: row.received_at_ms },
  })));
  assert.equal("sourceEventTime" in result.observations[0].provenance, false);

  const malformed = createOperationalStore(
    {
      async rpc() {
        return {
          data: [{ ...row(0, "rest", "/fapi/v1/klines"), close: "bad" }],
          error: null,
        };
      },
    },
    { candleRetentionDays: 7, monitorRunRetentionDays: 30, outboxMaxAttempts: 10 },
  );
  await assert.rejects(
    malformed.readCollectorCompletedCandles("BTCUSDT", 15),
    /invalid collector completed candle row/,
  );

  // Structural corruption remains rejected instead of being silently normalised.
  for (const corrupt of [{ ...row(0, "rest", "/fapi/v1/klines"), transport: "carrier-pigeon" }]) {
    const invalid = createOperationalStore(
      {
        async rpc() {
          return { data: [corrupt], error: null };
        },
      },
      { candleRetentionDays: 7, monitorRunRetentionDays: 30, outboxMaxAttempts: 10 },
    );
    await assert.rejects(
      invalid.readCollectorCompletedCandles("BTCUSDT", 15),
      /invalid collector completed candle row/,
    );
  }
});

test("collector retention preserves TA history and bounds the separate forward minute window", async (t) => {
  // Keep fixture construction and SQL retention on the same clock even if the
  // one-minute batches would otherwise straddle a real minute boundary.
  const now = Date.parse("2026-10-04T12:00:30.000Z");
  t.mock.timers.enable({ apis: ["Date"], now });
  const databaseClock = await db.query(
    "SELECT (extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms",
  );
  assert.equal(Number(databaseClock.rows[0].now_ms), now);

  const series = (symbol, timeframeMinutes, count, endOpenTime) => {
    const step = timeframeMinutes * 60_000;
    return Array.from({ length: count }, (_, index) => {
      const openTime = endOpenTime - (count - 1 - index) * step;
      return {
        instrument_id: `binance-usdm:${symbol}`,
        symbol,
        native_symbol: symbol,
        provider: "binance-usdm",
        endpoint: "wss://fstream.binance.com/market/stream",
        price_type: "trade",
        timeframe_minutes: timeframeMinutes,
        open_time: new Date(openTime).toISOString(),
        close_time: new Date(openTime + step - 1).toISOString(),
        open: 100,
        high: 110,
        low: 90,
        close: 101,
        volume: 5,
        quote_volume: 505,
        source_event_at: new Date(openTime + step).toISOString(),
        received_at: new Date(openTime + step).toISOString(),
        transport: "websocket",
      };
    });
  };

  // A long frame keeps its newest 260 candles even though that spans ~43 days,
  // far beyond the seven-day window that bounds the bulk of the store.
  const step4h = 240 * 60_000;
  const recent4h = Math.floor(Date.now() / step4h) * step4h - step4h;
  await db.query("SELECT record_collector_candles($1,$2)", [
    JSON.stringify(series("BTCUSDT", 240, 300, recent4h)),
    7,
  ]);
  const kept4h = (
    await db.query(
      "SELECT count(*)::int AS count, min(open_time) AS oldest FROM collector_recent_candles WHERE timeframe_minutes = 240",
    )
  ).rows[0];
  assert.equal(kept4h.count, 260);
  assert.equal(new Date(kept4h.oldest).getTime(), recent4h - 259 * step4h);

  // Minute history uses its own window even when the general retention is one
  // day. Straddle the cutoff to verify both retention and pruning, with enough
  // recent rows that the 260-candle TA floor cannot mask an incorrect cutoff.
  const step1m = 60_000;
  const recent1m = Math.floor(Date.now() / step1m) * step1m - step1m;
  for (const [symbol, days, explicit] of [
    ["ETHUSDT", 140, false],
    ["SOLUSDT", 31, true],
    ["BNBUSDT", 90, true],
  ]) {
    const cutoffOpen = Math.floor(Date.now() / step1m) * step1m - days * 24 * 60 * step1m;
    const sql = explicit
      ? "SELECT record_collector_candles($1,$2,$3)"
      : "SELECT record_collector_candles($1,$2)";
    for (const endOpen of [cutoffOpen + 499 * step1m, recent1m]) {
      const args = [JSON.stringify(series(symbol, 1, 1000, endOpen)), 1];
      if (explicit) args.push(days);
      await db.query(sql, args);
    }
    const kept1m = (
      await db.query(
        "SELECT count(*)::int AS count, min(open_time) AS oldest FROM collector_recent_candles WHERE timeframe_minutes = 1 AND symbol = $1",
        [symbol],
      )
    ).rows[0];
    assert.equal(kept1m.count, 1500, `${days}-day minute retention`);
    assert.equal(new Date(kept1m.oldest).getTime(), cutoffOpen);
  }

  for (const days of [30, 201]) {
    await assert.rejects(
      db.query("SELECT record_collector_candles($1,$2,$3)", [
        JSON.stringify(series("ETHUSDT", 1, 1, recent1m)),
        1,
        days,
      ]),
      /Invalid collector candle batch/,
    );
  }
});

test("repository movement history read maps compact rows and skips malformed entries", async () => {
  const repositoryUrl = await moduleUrl(
    "../src/lib/operational/repository.server.ts",
    repositoryStubs,
  );
  const { createOperationalStore } = await import(repositoryUrl);
  const store = createOperationalStore(
    {
      async rpc(name) {
        assert.equal(name, "get_collector_movement_candles");
        return {
          data: { BTCUSDT: [[1000, 101.5, 2, 400], [2000, 102, 3, null], ["bad"]], ETHUSDT: "nope" },
          error: null,
        };
      },
    },
    { candleRetentionDays: 7, monitorRunRetentionDays: 30, outboxMaxAttempts: 10 },
  );
  const history = await store.readMovementCandleHistory(["btcusdt", "ethusdt"], 0, 5000);
  assert.deepEqual(history.get("BTCUSDT"), [
    { openTime: 1000, close: 101.5, volume: 2, quoteVolume: 400 },
  ]);
  assert.equal(history.has("ETHUSDT"), false);
});

test("market-state-current read forwards only known fields and never service credentials", async () => {
  const baseTime = 1_800_000_000_000;
  const repositoryUrl = await moduleUrl(
    "../src/lib/operational/repository.server.ts",
    repositoryStubs,
  );
  const { createOperationalStore } = await import(repositoryUrl);
  const row = {
    universe_id: "binance-usdm-public-market",
    primary_window_minutes: 5,
    universe_version: "market-universe-v1:test",
    provider: "binance-usdm",
    exchange: "binance",
    price_type: "trade",
    evaluation_boundary_time: new Date(baseTime).toISOString(),
    direction_state: "BROAD_RISE",
    pace: "ACCELERATING",
    active_episode_id: null,
    active_direction: null,
    interrupted: false,
    episode_algorithm_version: "market-episode-v1",
    lifecycle_config_version: "market-episode-config-v1",
    classifier_algorithm_version: "market-state-v1",
    classifier_config_version: "market-state-config-v1",
    movement_algorithm_version: "market-movement-v1",
    movement_config_version: "market-movement-config-v1",
    lifecycle_state: { serializationVersion: "market-episode-state-v1" },
    current_evidence: { risingFraction: 0.8 },
    updated_at: new Date(baseTime).toISOString(),
    service_role_key: "super-secret",
  };
  const query = {
    select() {
      return this;
    },
    eq() {
      return this;
    },
    async maybeSingle() {
      return { data: row, error: null };
    },
  };
  const store = createOperationalStore(
    {
      from: () => query,
      async rpc() {
        return { data: null, error: null };
      },
    },
    { candleRetentionDays: 7, monitorRunRetentionDays: 30, outboxMaxAttempts: 10 },
  );
  const current = await store.getMarketStateCurrent("binance-usdm-public-market");
  assert.equal(current.directionState, "BROAD_RISE");
  assert.equal(current.universeVersion, "market-universe-v1:test");
  assert.equal(current.evaluationBoundaryTime, baseTime);
  assert.equal("service_role_key" in current, false);
  assert.equal(JSON.stringify(current).includes("super-secret"), false);
});
