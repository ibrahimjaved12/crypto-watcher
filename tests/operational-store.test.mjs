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
        "../operational-db/supabase/migrations/20260925090000_operational_store.sql",
        import.meta.url,
      ),
      "utf8",
    ),
  );
  await db.exec(
    await readFile(
      new URL(
        "../operational-db/supabase/migrations/20260925120000_binance_collector.sql",
        import.meta.url,
      ),
      "utf8",
    ),
  );
  await db.exec(
    await readFile(
      new URL(
        "../operational-db/supabase/migrations/20260926120000_collector_subscriptions.sql",
        import.meta.url,
      ),
      "utf8",
    ),
  );
  await db.exec(
    await readFile(
      new URL(
        "../operational-db/supabase/migrations/20260925200000_movement_normalization_history.sql",
        import.meta.url,
      ),
      "utf8",
    ),
  );
  await db.exec(
    await readFile(
      new URL(
        "../operational-db/supabase/migrations/20260926140000_collector_candle_retention.sql",
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

  const event = { episodeStartBoundaryTime: 0, evaluationBoundaryTime: 0 };
  await assert.rejects(store.appendMarketMovementEvent(event), /returned null/);
  status = "unexpected";
  await assert.rejects(store.appendMarketMovementEvent(event), /returned unexpected/);
  status = "appended";
  assert.equal(await store.appendMarketMovementEvent(event), "appended");
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
      source_event_at: new Date(openTime + 60_000).toISOString(),
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

  const candles = (await db.query("SELECT get_collector_ta_candles('btcusdt',15,10) AS candles"))
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
  assert.equal(candles[2].close_time_ms, observed + 2 * duration + duration - 1);
  // The exchange event time is transported exactly as recorded (never rewritten to the
  // completion boundary), and a REST candle honestly reports no exchange event.
  assert.equal(candles[0].source_event_at_ms, null);
  assert.equal(candles[1].source_event_at_ms, observed + duration * 2 - 7);
  assert.equal(candles[2].source_event_at_ms, observed + 2 * duration + duration + 7);
  assert.equal(candles[2].received_at_ms, observed + 2 * duration + duration + 3);

  // Another timeframe or symbol has no history rather than leaking the wrong series.
  assert.deepEqual(
    (await db.query("SELECT get_collector_ta_candles('BTCUSDT',60,10) AS candles")).rows[0].candles,
    [],
  );
  assert.deepEqual(
    (await db.query("SELECT get_collector_ta_candles('ETHUSDT',15,10) AS candles")).rows[0].candles,
    [],
  );
  await assert.rejects(
    db.query("SELECT get_collector_ta_candles('BTCUSDT',7,10)"),
    /Invalid collector TA candle timeframe/,
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
  });
  const rows = [
    row(0, "rest", "/fapi/v1/klines"),
    row(1, "websocket", "wss://fstream.binance.com/market/stream", base + 2 * step - 7),
  ];
  const store = createOperationalStore(
    {
      async rpc(name, args) {
        assert.equal(name, "get_collector_ta_candles");
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
  const result = await store.readCollectorTACandles("btcusdt", 15);
  assert.equal(result.source, "binance-usdm");
  assert.equal(result.instrument.id, "binance-usdm:BTCUSDT");
  assert.equal(result.priceType, "trade");
  assert.equal("endpoint" in result, false);
  assert.equal("retrievedAt" in result, false);
  assert.deepEqual(result.candles, [
    {
      time: base,
      open: 100,
      high: 110,
      low: 90,
      close: 101,
      volume: 3,
      complete: true,
      closeTime: base + step - 1,
      // REST provenance: no exchange event is invented.
      sourceEventTime: null,
      receivedAt: base + step + 120,
      endpoint: "/fapi/v1/klines",
      transport: "rest",
    },
    {
      time: base + step,
      open: 100,
      high: 110,
      low: 90,
      close: 101,
      volume: 3,
      complete: true,
      closeTime: base + 2 * step - 1,
      // WebSocket provenance: even an event before completion survives unchanged.
      sourceEventTime: base + 2 * step - 7,
      receivedAt: base + 2 * step + 120,
      endpoint: "wss://fstream.binance.com/market/stream",
      transport: "websocket",
    },
  ]);

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
    malformed.readCollectorTACandles("BTCUSDT", 15),
    /invalid collector TA candle row/,
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
      invalid.readCollectorTACandles("BTCUSDT", 15),
      /invalid collector TA candle row/,
    );
  }
});

test("collector retention keeps enough canonical history for the longest TA frame", async () => {
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

  // The high-volume one-minute series is still bounded by the day window.
  const step1m = 60_000;
  const recent1m = Math.floor(Date.now() / step1m) * step1m - step1m;
  await db.query("SELECT record_collector_candles($1,$2)", [
    JSON.stringify(series("ETHUSDT", 1, 1000, recent1m - 1000 * step1m)),
    1,
  ]);
  await db.query("SELECT record_collector_candles($1,$2)", [
    JSON.stringify(series("ETHUSDT", 1, 1000, recent1m)),
    1,
  ]);
  const kept1m = (
    await db.query(
      "SELECT count(*)::int AS count FROM collector_recent_candles WHERE timeframe_minutes = 1",
    )
  ).rows[0];
  assert.equal(kept1m.count, 1440);
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
