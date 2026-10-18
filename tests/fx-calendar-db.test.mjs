// Economic calendar store (#401 part 1, part of #374): operational migration on PGlite. Every event here
// is SYNTHETIC test data, not a real schedule.
import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import { after, before, beforeEach, test } from "node:test";
import { PGlite } from "@electric-sql/pglite";

const MIGRATIONS = new URL("../operational-db/supabase/migrations/", import.meta.url);
const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const NFP = Date.UTC(2026, 10, 6, 13, 30);
const EVENT_KEYS = ["currency", "event_id", "first_seen_ms", "impact", "source", "tags", "title", "ts_ms"];
const TABLES = ["fx_calendar_observations", "fx_calendar_fetches"];

const db = new PGlite();

before(async () => {
  await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
    GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;`);
  // The baseline and every later operational migration (forex bars included), in file-name order.
  const files = (await readdir(MIGRATIONS)).filter((name) => name.endsWith(".sql")).sort();
  assert.ok(files.includes("20261019120000_fx_bars.sql"));
  assert.ok(files.includes("20261019130000_fx_calendar.sql"));
  for (const file of files) await db.exec(await readFile(new URL(file, MIGRATIONS), "utf8"));
});
beforeEach(() => db.exec(`RESET ROLE; TRUNCATE ${TABLES.join(", ")} RESTART IDENTITY;`));
after(() => db.close());

/** One SYNTHETIC observation as the fetcher sends it to record_fx_calendar. */
const observation = (overrides = {}) => ({ source: "test-feed", source_event_key: "nfp-2026-11",
  event_ts: new Date(NFP).toISOString(), currency: "USD", impact: "high", title: "Non-Farm Payrolls",
  detail: { forecast: "120K", previous: "95K" }, ...overrides });
const call = async (sql, args = []) => Object.values((await db.query(sql, args)).rows[0])[0];
const record = (rows) => call("SELECT record_fx_calendar($1)", [JSON.stringify(rows)]);
const events = (fromMs, toMs, asOfMs = null) =>
  call("SELECT get_fx_calendar_events($1, $2, $3)", [fromMs, toMs, asOfMs]);
const fetchLog = (source, status, count, coveredTo = null, error = null) =>
  db.query("SELECT record_fx_calendar_fetch($1, $2, $3, $4, $5)",
    [source, status, count, coveredTo === null ? null : new Date(coveredTo).toISOString(), error]);
const count = async (table) => (await db.query(`SELECT count(*)::int AS n FROM ${table}`)).rows[0].n;

/** Run `fn` with the JavaScript (and so PGlite's) clock at `ms`. */
async function at(t, ms, fn) {
  t.mock.timers.enable({ apis: ["Date"], now: ms });
  try {
    assert.equal(Number(await call("SELECT (extract(epoch FROM clock_timestamp()) * 1000)::bigint")), ms);
    return await fn();
  } finally {
    t.mock.timers.reset();
  }
}

test("record_fx_calendar returns the number of new observations; a replay returns 0", async () => {
  const rows = [observation(), observation({ source_event_key: "pmi", currency: "EUR", impact: "medium",
    title: "PMI", event_ts: new Date(NFP + DAY).toISOString(), detail: undefined })];
  assert.equal(await record(rows), 2);
  assert.equal(await record(rows), 0);
  // A duplicate inside one batch counts once; a missing or null detail stores {}.
  assert.equal(await record([observation({ source_event_key: "cpi", detail: null }), observation({ source_event_key: "cpi", detail: null })]), 1);
  assert.equal(await count("fx_calendar_observations"), 3);
  const stored = (await db.query("SELECT source_event_key, detail FROM fx_calendar_observations ORDER BY observation_id")).rows;
  assert.deepEqual(stored.map((row) => row.detail), [{ forecast: "120K", previous: "95K" }, {}, {}]);
  // An explicit offset is the same instant.
  assert.equal(await record([observation({ event_ts: "2026-11-06T08:30:00-05:00" })]), 0);
});

test("a rescheduled or re-rated event is a new observation with its own event id", async () => {
  await record([observation()]);
  assert.equal(await record([observation({ event_ts: new Date(NFP + DAY).toISOString() })]), 1, "reschedule");
  assert.equal(await record([observation({ impact: "medium" })]), 1, "re-rating at the same time");
  const rows = await events(NFP - DAY, NFP + 2 * DAY);
  assert.deepEqual(rows.map((row) => [row.event_id, row.ts_ms, row.impact]), [
    [`test-feed:nfp-2026-11:${NFP}`, NFP, "high"],
    // The re-rating repeats source:key:ts, so it carries its observation id (EventIndex needs unique ids).
    [`test-feed:nfp-2026-11:${NFP}:o3`, NFP, "medium"],
    [`test-feed:nfp-2026-11:${NFP + DAY}`, NFP + DAY, "high"],
  ]);
  assert.equal(new Set(rows.map((row) => row.event_id)).size, 3);
});

test("observations are append-only", async () => {
  await record([observation()]);
  for (const sql of ["UPDATE fx_calendar_observations SET impact = 'low'", "DELETE FROM fx_calendar_observations"]) {
    await assert.rejects(db.query(sql), (error) => error.code === "55000" && /append-only/.test(error.message));
  }
  assert.equal(await count("fx_calendar_observations"), 1);
});

test("an invalid item rejects the whole batch", async () => {
  const invalid = [
    observation({ source: "Test Feed" }),
    observation({ source_event_key: "" }),
    observation({ source_event_key: "k".repeat(201) }),
    observation({ event_ts: "2026-11-06T13:30:00" }),
    observation({ event_ts: "2026-13-06T13:30:00Z" }),
    observation({ event_ts: "infinity" }),
    observation({ event_ts: NFP }),
    observation({ currency: "usd" }),
    observation({ currency: "USDT" }),
    observation({ impact: "critical" }),
    observation({ impact: "HIGH" }),
    observation({ title: "" }),
    observation({ title: "t".repeat(301) }),
    observation({ title: 7 }),
    observation({ detail: ["forecast"] }),
    observation({ source: undefined }),
    "not an object",
  ];
  for (const bad of invalid) {
    await assert.rejects(record([observation({ source_event_key: "fine" }), bad]), /Invalid fx calendar batch/,
      JSON.stringify(bad));
  }
  await assert.rejects(record([]), /Invalid fx calendar batch/);
  await assert.rejects(record(Array.from({ length: 2001 }, (_, i) => observation({ source_event_key: `k${i}` }))),
    /Invalid fx calendar batch/);
  await assert.rejects(db.query("SELECT record_fx_calendar('{}'::jsonb)"), /Invalid fx calendar batch/);
  assert.equal(await count("fx_calendar_observations"), 0, "nothing is partly written");
});

test("get_fx_calendar_events: inclusive range, 120-day cap, point in time and the exact key set", async (t) => {
  const seen1 = NFP - 14 * DAY;
  const seen2 = NFP - 2 * DAY;
  await at(t, seen1, () => record([observation(), observation({ source_event_key: "late", currency: "EUR",
    event_ts: new Date(NFP + 3 * DAY).toISOString(), title: "ECB" })]));
  await at(t, seen2, () => record([observation({ event_ts: new Date(NFP + HOUR).toISOString() })]));

  const all = await events(NFP, NFP + 3 * DAY);
  assert.deepEqual(all.map((row) => row.ts_ms), [NFP, NFP + HOUR, NFP + 3 * DAY], "both ends are inclusive");
  for (const row of all) assert.deepEqual(Object.keys(row).sort(), EVENT_KEYS);
  assert.deepEqual(all[0], { event_id: `test-feed:nfp-2026-11:${NFP}`, ts_ms: NFP, currency: "USD", impact: "high",
    title: "Non-Farm Payrolls", first_seen_ms: seen1, source: "test-feed", tags: [] });
  assert.equal(all[1].first_seen_ms, seen2);
  assert.deepEqual((await events(NFP + 1, NFP + 3 * DAY - 1)).map((row) => row.ts_ms), [NFP + HOUR]);
  assert.deepEqual(await events(NFP + 4 * DAY, NFP + 5 * DAY), []);

  // As of a time before the reschedule was first seen, only the original schedule exists.
  assert.deepEqual((await events(NFP, NFP + 3 * DAY, seen2 - 1)).map((row) => row.ts_ms), [NFP, NFP + 3 * DAY]);
  assert.deepEqual((await events(NFP, NFP + 3 * DAY, seen2)).map((row) => row.ts_ms), [NFP, NFP + HOUR, NFP + 3 * DAY]);
  assert.deepEqual(await events(NFP, NFP + 3 * DAY, seen1 - 1), []);

  assert.equal((await events(NFP, NFP + 120 * DAY)).length, 3, "exactly 120 days is allowed");
  await assert.rejects(events(NFP, NFP + 120 * DAY + 1), /Invalid fx calendar request/);
  await assert.rejects(events(NFP + 1, NFP), /Invalid fx calendar request/);
  await assert.rejects(db.query("SELECT get_fx_calendar_events(NULL, 1)"), /Invalid fx calendar request/);
});

test("record_fx_calendar_fetch validates status, counts and coverage", async () => {
  await fetchLog("test-feed", "ok", 42, NFP + 5 * DAY);
  await fetchLog("test-feed", "error", 0, null, "HTTP 503");
  for (const args of [
    ["test-feed", "ok", 42, null],
    ["test-feed", "error", 0, NFP],
    ["test-feed", "partial", 1, NFP],
    ["test-feed", "ok", -1, NFP],
    ["test-feed", "ok", null, NFP],
    ["Test Feed", "ok", 1, NFP],
    [null, "ok", 1, NFP],
  ]) {
    await assert.rejects(fetchLog(...args), /Invalid fx calendar fetch/, JSON.stringify(args));
  }
  await assert.rejects(db.query("SELECT record_fx_calendar_fetch('test-feed', 'ok', 1, 'infinity')"),
    /Invalid fx calendar fetch/);
  assert.equal(await count("fx_calendar_fetches"), 2);
});

test("get_fx_calendar_status reflects the latest ok and the latest error per source; the log keeps 90 days", async (t) => {
  assert.deepEqual(await call("SELECT get_fx_calendar_status()"), []);
  const t0 = NFP - 30 * DAY;
  await at(t, t0, () => fetchLog("test-feed", "ok", 10, t0 + 7 * DAY));
  await at(t, t0 + HOUR, () => fetchLog("test-feed", "error", 0, null, "timeout"));
  await at(t, t0 + 2 * HOUR, () => fetchLog("test-feed", "ok", 12, t0 + 8 * DAY));
  await at(t, t0 + 3 * HOUR, () => fetchLog("test-feed", "error", 0, null, "HTTP 503"));
  await at(t, t0 + 3 * HOUR, () => fetchLog("other-feed", "error", 0, null, "DNS"));
  assert.deepEqual(await call("SELECT get_fx_calendar_status()"), [
    { source: "other-feed", last_ok_ms: null, last_ok_covered_to_ms: null, last_error_ms: t0 + 3 * HOUR,
      last_error: "DNS" },
    { source: "test-feed", last_ok_ms: t0 + 2 * HOUR, last_ok_covered_to_ms: t0 + 8 * DAY,
      last_error_ms: t0 + 3 * HOUR, last_error: "HTTP 503" },
  ]);

  // 91 days later the old rows are gone: the old ok no longer counts (never fetched -> fail closed).
  await at(t, t0 + 91 * DAY, () => fetchLog("other-feed", "ok", 3, t0 + 98 * DAY));
  assert.deepEqual(await call("SELECT get_fx_calendar_status()"), [
    { source: "other-feed", last_ok_ms: t0 + 91 * DAY, last_ok_covered_to_ms: t0 + 98 * DAY, last_error_ms: null,
      last_error: null },
  ]);
  assert.equal(await count("fx_calendar_fetches"), 1);
});

test("browser roles have no access to the calendar tables or functions; service_role has", async () => {
  await record([observation()]);
  const calls = [
    ["SELECT record_fx_calendar($1)", [JSON.stringify([observation({ source_event_key: "role" })])]],
    ["SELECT record_fx_calendar_fetch('test-feed', 'ok', 1, now())", []],
    ["SELECT get_fx_calendar_events($1, $2)", [NFP - DAY, NFP + DAY]],
    ["SELECT get_fx_calendar_status()", []],
  ];
  for (const role of ["anon", "authenticated"]) {
    await db.exec(`SET ROLE ${role}`);
    try {
      for (const table of TABLES) {
        await assert.rejects(db.query(`SELECT * FROM ${table}`), /permission denied/, `${role} ${table}`);
      }
      await assert.rejects(db.query("INSERT INTO fx_calendar_fetches (source, status, event_count) VALUES ('x-feed', 'error', 0)"),
        /permission denied/);
      for (const [sql, args] of calls) await assert.rejects(db.query(sql, args), /permission denied/, `${role} ${sql}`);
    } finally {
      await db.exec("RESET ROLE");
    }
  }
  await db.exec("SET ROLE service_role");
  try {
    for (const table of TABLES) await db.query(`SELECT count(*) FROM ${table}`);
    for (const [sql, args] of calls) await db.query(sql, args);
    assert.equal(await count("fx_calendar_observations"), 2);
  } finally {
    await db.exec("RESET ROLE");
  }
});
