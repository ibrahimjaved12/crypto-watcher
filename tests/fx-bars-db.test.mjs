// Forex 1-minute bar tables (#395, part of #374): operational migration on PGlite. Every bar here is
// SYNTHETIC test data, not a market quote.
import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import { after, before, beforeEach, test } from "node:test";
import { PGlite } from "@electric-sql/pglite";

const MIGRATIONS = new URL("../operational-db/supabase/migrations/", import.meta.url);
const MINUTE = 60_000;
const DAY = 86_400_000;
const VENUE = "mt5-demo";
const EURUSD = "fx:EURUSD";
const GBPUSD = "fx:GBPUSD";
const A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const ROW_KEYS = ["close", "high", "instrument_id", "low", "native_symbol", "open", "open_time_ms",
  "received_at_ms", "spread_points", "symbol", "tick_volume", "time_basis", "transport", "venue"];
const FX_TABLES = ["fx_recent_bars", "fx_bar_conflicts", "fx_collector_health", "fx_subscriptions",
  "fx_collector_leases"];

const db = new PGlite();

before(async () => {
  await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
    GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;`);
  // The baseline and every later operational migration, in file-name order.
  const files = (await readdir(MIGRATIONS)).filter((name) => name.endsWith(".sql")).sort();
  assert.ok(files.includes("20261019120000_fx_bars.sql"));
  for (const file of files) await db.exec(await readFile(new URL(file, MIGRATIONS), "utf8"));
});
beforeEach(() => db.exec(`RESET ROLE; TRUNCATE ${FX_TABLES.join(", ")} RESTART IDENTITY;`));
after(() => db.close());

const recentMinute = () => Math.floor(Date.now() / MINUTE) * MINUTE - MINUTE;
const iso = (ms) => new Date(ms).toISOString();

/** One SYNTHETIC gateway-shaped bar as the collector sends it to record_fx_bars. */
function bar(openMs, overrides = {}, instrumentId = EURUSD) {
  const symbol = instrumentId.split(":")[1];
  return { venue: VENUE, instrument_id: instrumentId, symbol, native_symbol: symbol, open_time: iso(openMs),
    open: 1.0825, high: 1.0831, low: 1.0822, close: 1.0829, spread_points: 12, tick_volume: 57,
    transport: "poll", received_at: iso(openMs + MINUTE + 1_500), ...overrides };
}
const series = (count, endOpenMs, instrumentId = EURUSD) =>
  Array.from({ length: count }, (_, i) => bar(endOpenMs - (count - 1 - i) * MINUTE, {}, instrumentId));

async function record(rows, retentionDays) {
  const sql = retentionDays === undefined
    ? "SELECT bar_identity FROM record_fx_bars($1)"
    : "SELECT bar_identity FROM record_fx_bars($1, $2)";
  const args = retentionDays === undefined ? [JSON.stringify(rows)] : [JSON.stringify(rows), retentionDays];
  return (await db.query(sql, args)).rows.map((row) => row.bar_identity);
}
const count = async (table, where = "true", args = []) =>
  (await db.query(`SELECT count(*)::int AS n FROM ${table} WHERE ${where}`, args)).rows[0].n;
const getBars = async (instrumentId, fromMs = null, limit = 1000) =>
  (await db.query("SELECT get_fx_bars($1, $2, $3, $4) AS rows", [VENUE, instrumentId, fromMs, limit])).rows[0].rows;
const health = async (rows) => db.query("SELECT record_fx_collector_health($1)", [JSON.stringify(rows)]);
const LAST_BAR = Date.UTC(2026, 9, 19, 11, 59);
const healthRow = (status, extra = {}) => ({ venue: VENUE, instrument_id: EURUSD, status,
  last_bar_open_time: iso(LAST_BAR), lag_ms: 1_200, error_message: null, ...extra });
const call = async (sql, args = []) => Object.values((await db.query(sql, args)).rows[0])[0];

test("a valid batch inserts and returns identities; replaying it stores nothing new", async () => {
  const end = recentMinute();
  const rows = series(3, end);
  const identities = await record(rows);
  assert.deepEqual(identities.sort(), [end - 2 * MINUTE, end - MINUTE, end].map((ms) => `${VENUE}|${EURUSD}|${ms}`));
  assert.equal(await count("fx_recent_bars"), 3);
  assert.deepEqual(await record(rows), []);
  // A copy with the same values from another transport is the same bar, not a conflict.
  assert.deepEqual(await record(rows.map((row) => ({ ...row, transport: "history" }))), []);
  assert.equal(await count("fx_recent_bars"), 3);
  assert.equal(await count("fx_recent_bars", "transport = 'poll'"), 3);
  assert.equal(await count("fx_bar_conflicts"), 0);
});

test("a conflicting offer keeps the stored bar, is recorded once and never fails the batch", async () => {
  const end = recentMinute();
  await record(series(3, end - MINUTE));
  await health([healthRow("LIVE")]);
  const target = end - 2 * MINUTE;
  const offer = [bar(target, { close: 1.0827, spread_points: 15, transport: "history" }), bar(end)];
  assert.deepEqual(await record(offer), [`${VENUE}|${EURUSD}|${end}`]);

  const stored = (await db.query("SELECT close, spread_points, transport FROM fx_recent_bars WHERE open_time = $1",
    [iso(target)])).rows[0];
  assert.deepEqual({ ...stored, close: Number(stored.close) }, { close: 1.0829, spread_points: 12, transport: "poll" });
  const conflicts = (await db.query("SELECT * FROM fx_bar_conflicts")).rows;
  assert.equal(conflicts.length, 1);
  const [k] = conflicts;
  assert.deepEqual(k.differing_fields, ["close", "spread_points"]);
  assert.equal(k.stored_transport, "poll");
  assert.equal(k.offered_transport, "history");
  assert.equal(k.offered.close, 1.0827);
  assert.equal("received_at" in k.offered, false);
  assert.equal("inserted_at" in k.stored, false);
  const storedHash = await call(
    "SELECT fx_bar_hash(venue, instrument_id, open_time, open, high, low, close, spread_points, tick_volume) FROM fx_recent_bars WHERE open_time = $1",
    [iso(target)]);
  assert.equal(k.stored_hash, storedHash);
  assert.notEqual(k.offered_hash, k.stored_hash);
  assert.match(k.offered_hash, /^[0-9a-f]{32}$/);

  // Replaying the same offer adds no second conflict; a different offer for the minute does.
  assert.deepEqual(await record(offer), []);
  assert.equal(await count("fx_bar_conflicts"), 1);
  await record([bar(target, { tick_volume: 99 })]);
  assert.equal(await count("fx_bar_conflicts"), 2);
  // ON CONFLICT DO NOTHING still draws an identity value, so take the newest row, not conflict_id = 2.
  assert.deepEqual((await db.query("SELECT differing_fields FROM fx_bar_conflicts ORDER BY conflict_id DESC LIMIT 1"))
    .rows[0].differing_fields, ["tick_volume"]);
  assert.equal(await count("fx_recent_bars"), 4);

  // Conflicts feed the counters only; the measured status is unchanged.
  const [row] = await call("SELECT get_fx_collector_health()");
  assert.equal(row.status, "LIVE");
  assert.equal(row.conflict_count_24h, 2);
  assert.equal(typeof row.last_conflict_at_ms, "number");
});

test("fx_bar_conflicts is append-only", async () => {
  const end = recentMinute();
  await record([bar(end)]);
  await record([bar(end, { open: 1.0826 })]);
  for (const sql of ["UPDATE fx_bar_conflicts SET differing_fields = ARRAY['high']", "DELETE FROM fx_bar_conflicts"]) {
    await assert.rejects(db.query(sql), (error) => error.code === "55000" && /append-only/.test(error.message));
  }
  assert.equal(await count("fx_bar_conflicts"), 1);
});

test("record_fx_bars rejects invalid bars and batches", async () => {
  const end = recentMinute();
  const invalid = [
    bar(end + 30_000),
    bar(end, { high: 1.08, low: 1.09 }),
    bar(end, { open: 0 }),
    bar(end, { spread_points: -1 }),
    bar(end, { transport: "rest" }),
    bar(end, { tick_volume: -1 }),
    bar(end, { symbol: "GBPUSD" }),
    bar(end, { instrument_id: "EURUSD", symbol: "EURUSD" }),
    bar(end, { venue: "MT5 Demo" }),
  ];
  for (const row of invalid) await assert.rejects(record([row]), Error, JSON.stringify(row));
  await assert.rejects(record([]), /Invalid fx bar batch/);
  await assert.rejects(record(series(1001, end)), /Invalid fx bar batch/);
  for (const days of [30, 201]) await assert.rejects(record([bar(end)], days), /Invalid fx bar batch/);
  await assert.rejects(db.query("SELECT record_fx_bars('{}'::jsonb)"), Error);
  assert.equal(await count("fx_recent_bars"), 0);
  // Bounds themselves are accepted.
  for (const days of [31, 200]) assert.equal((await record([bar(end - days * MINUTE)], days)).length, 1);
});

test("retention deletes bars older than the window but keeps the newest 2000 of each series", async (t) => {
  // Fixture and SQL retention share one clock (PGlite reads the JavaScript clock).
  const now = Date.parse("2026-10-19T12:00:30.000Z");
  t.mock.timers.enable({ apis: ["Date"], now });
  assert.equal(Number(await call("SELECT (extract(epoch FROM clock_timestamp()) * 1000)::bigint")), now);
  const days = 31;
  const recent = Math.floor(now / MINUTE) * MINUTE - MINUTE;
  const cutoffOpen = Math.floor(now / MINUTE) * MINUTE - days * DAY;

  const kept = async (instrumentId) => {
    const row = (await db.query("SELECT count(*)::int AS n, min(open_time) AS oldest FROM fx_recent_bars WHERE instrument_id = $1",
      [instrumentId])).rows[0];
    return [row.n, new Date(row.oldest).getTime()];
  };

  // One EURUSD series exercises both rules with 2600 rows. 1000 bars straddle the cutoff (500 end
  // before it), then 1000 recent bars: 2000 in all, so the floor keeps everything.
  await record(series(1000, cutoffOpen + 499 * MINUTE), days);
  await record(series(1000, recent - 600 * MINUTE), days);
  assert.deepEqual(await kept(EURUSD), [2000, cutoffOpen - 500 * MINUTE], "at the floor nothing goes");
  // 200 more recent bars: the floor (2000th newest) is now cutoffOpen - 300 min, so only the 200 bars
  // below it go; 300 bars older than the window stay because they are among the newest 2000 (a long
  // closure or outage never empties the runner's warm-up).
  await record(series(200, recent - 400 * MINUTE), days);
  assert.deepEqual(await kept(EURUSD), [2000, cutoffOpen - 300 * MINUTE], "the floor keeps old bars");
  // 400 more: the floor moves past the cutoff, so the window decides: every bar whose minute ended
  // before the cutoff goes and the oldest kept bar opens exactly at the cutoff minute.
  await record(series(400, recent), days);
  assert.deepEqual(await kept(EURUSD), [2100, cutoffOpen], "the window bounds the deletion");

  // Series are independent: three ancient GBPUSD bars are below their own floor and stay.
  const ancient = cutoffOpen - 10 * DAY;
  await record(series(3, ancient, GBPUSD), days);
  await record([bar(recent, {}, GBPUSD)], days);
  assert.deepEqual(await kept(GBPUSD), [4, ancient - 2 * MINUTE]);

  const coverage = await call("SELECT get_fx_bar_coverage()");
  assert.deepEqual(coverage, [
    { venue: VENUE, instrument_id: EURUSD, bar_count: 2100, oldest_open_time_ms: cutoffOpen, newest_open_time_ms: recent },
    { venue: VENUE, instrument_id: GBPUSD, bar_count: 4, oldest_open_time_ms: ancient - 2 * MINUTE,
      newest_open_time_ms: recent },
  ]);
});

test("get_fx_bars returns the newest bars oldest first in the exact gateway row shape", async () => {
  const end = recentMinute();
  await record(series(10, end));
  await record(series(3, end, GBPUSD));
  const all = await getBars(EURUSD);
  assert.equal(all.length, 10);
  assert.deepEqual(all.map((row) => row.open_time_ms), Array.from({ length: 10 }, (_, i) => end - (9 - i) * MINUTE));
  for (const row of all) {
    assert.deepEqual(Object.keys(row).sort(), ROW_KEYS);
    for (const key of ["open", "high", "low", "close", "spread_points", "tick_volume", "open_time_ms", "received_at_ms"])
      assert.equal(typeof row[key], "number", key);
    assert.equal(row.time_basis, "utc");
    assert.equal(row.instrument_id, EURUSD);
  }
  assert.deepEqual(all[9], { time_basis: "utc", venue: VENUE, instrument_id: EURUSD, symbol: "EURUSD",
    native_symbol: "EURUSD", transport: "poll", open_time_ms: end, open: 1.0825, high: 1.0831, low: 1.0822,
    close: 1.0829, spread_points: 12, tick_volume: 57, received_at_ms: end + MINUTE + 1_500 });

  const newest = await getBars(EURUSD, null, 4);
  assert.deepEqual(newest.map((row) => row.open_time_ms), [3, 2, 1, 0].map((i) => end - i * MINUTE));
  const from = await getBars(EURUSD, end - 2 * MINUTE);
  assert.deepEqual(from.map((row) => row.open_time_ms), [end - 2 * MINUTE, end - MINUTE, end]);
  const fromLimited = await getBars(EURUSD, end - 5 * MINUTE, 2);
  assert.deepEqual(fromLimited.map((row) => row.open_time_ms), [end - MINUTE, end]);
  assert.deepEqual(await getBars("fx:USDJPY"), []);
  for (const limit of [0, 20001]) await assert.rejects(getBars(EURUSD, null, limit), /Invalid fx bar request/);
});

test("collector health upserts, accepts MARKET_CLOSED and LOGIN_FAILED, rejects unknown status", async () => {
  await health([healthRow("LIVE"), healthRow("RECOVERING", { instrument_id: GBPUSD })]);
  await health([healthRow("MARKET_CLOSED", { lag_ms: null })]);
  let rows = await call("SELECT get_fx_collector_health()");
  assert.deepEqual(rows.map((row) => [row.instrument_id, row.status]), [[EURUSD, "MARKET_CLOSED"], [GBPUSD, "RECOVERING"]]);
  assert.deepEqual(Object.keys(rows[0]).sort(), ["conflict_count_24h", "error_message", "instrument_id", "lag_ms",
    "last_bar_open_time_ms", "last_conflict_at_ms", "status", "updated_at_ms", "venue"]);
  assert.equal(rows[0].lag_ms, null);
  assert.equal(rows[0].last_bar_open_time_ms, LAST_BAR);
  assert.equal(typeof rows[0].updated_at_ms, "number");
  assert.equal(rows[0].conflict_count_24h, 0);
  assert.equal(rows[0].last_conflict_at_ms, null);
  await health([healthRow("LOGIN_FAILED", { error_message: "authorization failed", last_bar_open_time: null })]);
  rows = await call("SELECT get_fx_collector_health()");
  assert.equal(rows[0].status, "LOGIN_FAILED");
  assert.equal(rows[0].error_message, "authorization failed");

  for (const bad of [[healthRow("CLOSED")], [healthRow("LIVE", { instrument_id: "EURUSD" })], [],
    [healthRow("LIVE"), healthRow("STALE")], [healthRow("LIVE", { lag_ms: -1 })]]) {
    await assert.rejects(health(bad), Error, JSON.stringify(bad));
  }
  assert.equal(await count("fx_collector_health"), 2);
});

test("subscriptions are validated, normalized and unassigned health becomes UNAVAILABLE", async () => {
  assert.deepEqual(await call("SELECT get_fx_subscriptions()"), []);
  const eur = { venue: VENUE, instrument_id: EURUSD, native_symbol: "EURUSD" };
  const gbp = { venue: VENUE, instrument_id: GBPUSD, native_symbol: "GBPUSD.r" };
  await db.query("SELECT assign_fx_subscriptions($1)", [JSON.stringify([gbp, { ...eur, extra: 1 }, eur])]);
  assert.deepEqual(await call("SELECT get_fx_subscriptions()"), [eur, gbp]);
  await health([healthRow("LIVE"), healthRow("LIVE", { instrument_id: GBPUSD })]);
  await db.query("SELECT assign_fx_subscriptions($1)", [JSON.stringify([eur])]);
  assert.deepEqual(await call("SELECT get_fx_subscriptions()"), [eur]);
  const rows = await call("SELECT get_fx_collector_health()");
  assert.deepEqual(rows.map((row) => [row.instrument_id, row.status]), [[EURUSD, "LIVE"], [GBPUSD, "UNAVAILABLE"]]);
  assert.equal(rows[1].error_message, "collection disabled/unsubscribed");

  for (const bad of [[{ venue: VENUE, instrument_id: "EURUSD", native_symbol: "EURUSD" }],
    [{ venue: VENUE, instrument_id: EURUSD }], [{ ...eur, native_symbol: 7 }], [{ ...eur, venue: "MT5" }], ["fx:EURUSD"],
    Array.from({ length: 101 }, (_, i) => ({ ...eur, instrument_id: `fx:S${i}` }))]) {
    await assert.rejects(db.query("SELECT assign_fx_subscriptions($1)", [JSON.stringify(bad)]), /Invalid fx subscription set/);
  }
  await assert.rejects(db.query("SELECT assign_fx_subscriptions('{}'::jsonb)"), Error);
  assert.deepEqual(await call("SELECT get_fx_subscriptions()"), [eur]);
});

test("the gateway lease is exclusive, renewable, releasable and taken over after expiry", async () => {
  assert.equal(await call("SELECT claim_fx_collector_lease($1, 30)", [A]), true);
  assert.equal(await call("SELECT claim_fx_collector_lease($1, 30)", [B]), false);
  assert.equal(await call("SELECT claim_fx_collector_lease($1, 30)", [A]), true, "the holder may re-claim");
  assert.equal(await call("SELECT renew_fx_collector_lease($1, 60)", [A]), true);
  assert.equal(await call("SELECT renew_fx_collector_lease($1, 60)", [B]), false);
  await db.exec("UPDATE fx_collector_leases SET leased_until = clock_timestamp() - interval '1 second'");
  assert.equal(await call("SELECT renew_fx_collector_lease($1, 60)", [A]), false, "an expired lease is not renewed");
  assert.equal(await call("SELECT claim_fx_collector_lease($1, 30)", [B]), true, "expiry allows a takeover");
  assert.equal((await db.query("SELECT instance_id, lease_name FROM fx_collector_leases")).rows[0].instance_id, B);
  await db.query("SELECT release_fx_collector_lease($1)", [A]);
  assert.equal(await count("fx_collector_leases"), 1, "only the holder releases");
  await db.query("SELECT release_fx_collector_lease($1)", [B]);
  assert.equal(await count("fx_collector_leases"), 0);
  assert.equal(await call("SELECT claim_fx_collector_lease($1, 30)", [A]), true);
  for (const seconds of [29, 301]) {
    await assert.rejects(db.query("SELECT claim_fx_collector_lease($1, $2)", [A, seconds]), /Invalid fx collector lease/);
    await assert.rejects(db.query("SELECT renew_fx_collector_lease($1, $2)", [A, seconds]), /Invalid fx collector lease/);
  }
  // The crypto lease is a separate row in a separate table.
  assert.equal(await count("collector_leases"), 0);
});

test("browser roles have no access to forex tables or functions; service_role has", async () => {
  const end = recentMinute();
  await record([bar(end)]);
  const calls = [
    ["SELECT record_fx_bars($1)", [JSON.stringify([bar(end + MINUTE)])]],
    ["SELECT get_fx_bars($1, $2)", [VENUE, EURUSD]],
    ["SELECT get_fx_bar_coverage()", []],
    ["SELECT record_fx_collector_health($1)", [JSON.stringify([healthRow("LIVE")])]],
    ["SELECT get_fx_collector_health()", []],
    ["SELECT assign_fx_subscriptions('[]'::jsonb)", []],
    ["SELECT get_fx_subscriptions()", []],
    ["SELECT claim_fx_collector_lease($1)", [A]],
    ["SELECT renew_fx_collector_lease($1)", [A]],
    ["SELECT release_fx_collector_lease($1)", [A]],
    ["SELECT refresh_fx_conflict_counters()", []],
    ["SELECT fx_bar_hash('v', 'fx:X', now(), 1, 1, 1, 1, 0, 0)", []],
  ];
  for (const role of ["anon", "authenticated"]) {
    await db.exec(`SET ROLE ${role}`);
    try {
      for (const table of FX_TABLES) {
        await assert.rejects(db.query(`SELECT * FROM ${table}`), /permission denied/, `${role} ${table}`);
      }
      await assert.rejects(db.query("DELETE FROM fx_recent_bars"), /permission denied/);
      for (const [sql, args] of calls) await assert.rejects(db.query(sql, args), /permission denied/, `${role} ${sql}`);
    } finally {
      await db.exec("RESET ROLE");
    }
  }
  await db.exec("SET ROLE service_role");
  try {
    for (const table of FX_TABLES) await db.query(`SELECT count(*) FROM ${table}`);
    for (const [sql, args] of calls) await db.query(sql, args);
    assert.equal(await count("fx_recent_bars"), 2);
  } finally {
    await db.exec("RESET ROLE");
  }
});
