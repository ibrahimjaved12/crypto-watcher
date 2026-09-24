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
});
beforeEach(async () => {
  await db.exec(`RESET ROLE;
    TRUNCATE sync_outbox, operational_results, monitor_runs,
      market_data_checkpoints, recent_candles, collector_recent_candles,
      collector_health, collector_leases CASCADE;`);
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
  } finally {
    await db.exec("RESET ROLE");
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
  assert.equal(configured.candleRetentionDays, 7);
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
  const repositoryUrl = await moduleUrl("../src/lib/operational/repository.server.ts", {
    "@supabase/supabase-js": stub("export const createClient=()=>({});"),
    "./config.server": stub("export const operationalDbConfig=()=>({enabled:false});"),
  });
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
