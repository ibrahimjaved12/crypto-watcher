// Forex symbol specs store (#432, part of #374): operational migration on PGlite. Every spec here is
// SYNTHETIC test data, not a broker's real contract details.
import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import { after, before, beforeEach, test } from "node:test";
import { PGlite } from "@electric-sql/pglite";

const MIGRATIONS = new URL("../operational-db/supabase/migrations/", import.meta.url);
const HOUR = 3_600_000;
const T0 = Date.UTC(2026, 9, 19, 12, 0);
const COLUMNS = ["account_currency", "base_currency", "contract_size", "digits", "instrument_id", "margin_currency",
  "native_symbol", "point", "profit_currency", "swap_long", "swap_mode", "swap_rollover3days", "swap_short", "tick_size",
  "tick_value", "trade_mode", "venue", "volume_max", "volume_min", "volume_step"];
const READ_KEYS = [...COLUMNS, "observed_at_ms", "spec_hash"].sort();

const db = new PGlite();

before(async () => {
  await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
    GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;`);
  // The baseline and every later operational migration, in file-name order.
  const files = (await readdir(MIGRATIONS)).filter((name) => name.endsWith(".sql")).sort();
  assert.ok(files.includes("20261019140000_fx_symbol_specs.sql"));
  for (const file of files) await db.exec(await readFile(new URL(file, MIGRATIONS), "utf8"));
});
beforeEach(() => db.exec("RESET ROLE; TRUNCATE fx_symbol_spec_observations RESTART IDENTITY;"));
after(() => db.close());

/** One SYNTHETIC 5-digit pair spec as the gateway would send it. */
const spec = (overrides = {}) => ({ venue: "test-venue", instrument_id: "fx:EURUSD", native_symbol: "EURUSDm",
  digits: 5, point: 0.00001, tick_size: 0.00001, tick_value: 1, contract_size: 100000, volume_min: 0.01,
  volume_max: 200, volume_step: 0.01, base_currency: "EUR", profit_currency: "USD", margin_currency: "EUR",
  account_currency: "USD", trade_mode: 4, swap_mode: 1, swap_long: -7.3, swap_short: 2.1, swap_rollover3days: 3,
  ...overrides });
const gold = (overrides = {}) => spec({ instrument_id: "fx:XAUUSD", native_symbol: "XAUUSDm", digits: 2, point: 0.01,
  tick_size: 0.01, contract_size: 100, base_currency: "XAU", margin_currency: "XAU", ...overrides });
const call = async (sql, args = []) => Object.values((await db.query(sql, args)).rows[0])[0];
const record = (rows) => call("SELECT record_fx_symbol_specs($1)", [JSON.stringify(rows)]);
const read = (asOfMs = null) => call("SELECT get_fx_symbol_specs($1)", [asOfMs]);
const count = async () => (await db.query("SELECT count(*)::int AS n FROM fx_symbol_spec_observations")).rows[0].n;

/** Run `fn` with the JavaScript (and so PGlite's) clock at `ms`. */
async function at(t, ms, fn) {
  t.mock.timers.enable({ apis: ["Date"], now: ms });
  try {
    return await fn();
  } finally {
    t.mock.timers.reset();
  }
}

test("record then read returns the latest spec per instrument with exact numbers", async (t) => {
  assert.deepEqual(await read(), []);
  assert.equal(await at(t, T0, () => record([spec(), gold()])), 2);
  const rows = await read();
  assert.deepEqual(rows.map((row) => row.instrument_id), ["fx:EURUSD", "fx:XAUUSD"]);
  for (const row of rows) assert.deepEqual(Object.keys(row).sort(), READ_KEYS);
  const { spec_hash: hash, observed_at_ms: observedAt, ...values } = rows[0];
  assert.deepEqual(values, spec());
  assert.match(hash, /^[0-9a-f]{64}$/);
  assert.equal(observedAt, T0);
  assert.equal(rows[1].point, 0.01);
  assert.equal(rows[1].base_currency, "XAU");
});

test("the same spec twice is stored once; trailing zeros and the native name do not make it new", async () => {
  assert.equal(await record([spec()]), 1);
  assert.equal(await record([spec()]), 0);
  assert.equal(await record([spec(), spec()]), 0);
  // 1.0 and 1 are the same value; the hash trims the scale.
  const text = JSON.stringify([spec()]).replace('"tick_value":1,', '"tick_value":1.0,');
  assert.equal(await call("SELECT record_fx_symbol_specs($1::jsonb)", [text]), 0);
  // native_symbol is not an economic field.
  assert.equal(await record([spec({ native_symbol: "EURUSD.r" })]), 0);
  assert.equal(await count(), 1);
});

test("a changed swap is a new observation; as_of before the change returns the older one", async (t) => {
  await at(t, T0, () => record([spec()]));
  await at(t, T0 + HOUR, () => record([spec({ swap_long: -7.9 })]));
  assert.equal(await count(), 2);
  assert.equal((await read())[0].swap_long, -7.9);
  assert.equal((await read(T0 + HOUR))[0].swap_long, -7.9);
  const older = await read(T0 + HOUR - 1);
  assert.equal(older.length, 1);
  assert.equal(older[0].swap_long, -7.3);
  assert.equal(older[0].observed_at_ms, T0);
  assert.notEqual(older[0].spec_hash, (await read())[0].spec_hash);
  assert.deepEqual(await read(T0 - 1), []);
  // LIMITATION of UNIQUE (venue, instrument_id, spec_hash), as specified in #432: changing BACK to an
  // earlier spec is not a new row, so the latest read keeps the intermediate value (see the PR body).
  assert.equal(await at(t, T0 + 2 * HOUR, () => record([spec()])), 0);
  assert.equal((await read())[0].swap_long, -7.9);
});

test("observations are append-only", async () => {
  await record([spec()]);
  for (const sql of ["UPDATE fx_symbol_spec_observations SET swap_long = 0", "DELETE FROM fx_symbol_spec_observations"]) {
    await assert.rejects(db.query(sql), (error) => error.code === "55000" && /append-only/.test(error.message));
  }
  assert.equal(await count(), 1);
});

test("every validation failure raises one generic error, echoes nothing and writes nothing", async () => {
  const marker = "ZZQ";
  const withoutSwapLong = spec();
  delete withoutSwapLong.swap_long;
  const invalid = [
    {},
    withoutSwapLong,
    spec({ spec_hash: "a".repeat(64) }),
    spec({ observed_at: "2026-10-19T12:00:00Z" }),
    spec({ extra: marker }),
    spec({ point: "0.00001" }),
    spec({ digits: true }),
    spec({ digits: 5.5 }),
    spec({ digits: 11, point: 1e-11 }),
    spec({ point: 0.0001 }), // the point CHECK: not 10^-digits
    spec({ tick_size: 0 }),
    spec({ tick_value: -1 }),
    spec({ contract_size: 0 }),
    spec({ volume_min: 300 }), // above volume_max
    spec({ volume_step: 0 }),
    spec({ venue: `Bad ${marker}` }),
    spec({ instrument_id: `${marker}USD` }),
    spec({ instrument_id: "fx:" }),
    spec({ native_symbol: "" }),
    spec({ native_symbol: marker.repeat(14) }),
    spec({ base_currency: "" }),
    spec({ profit_currency: `${marker}TOOLONGCODE` }),
    spec({ account_currency: "usd" }),
    spec({ swap_rollover3days: 7 }),
    spec({ trade_mode: -1 }),
    spec({ swap_mode: 70000 }),
    spec({ swap_long: null }),
    spec({ margin_currency: 5 }),
    "not an object",
  ];
  for (const bad of invalid) {
    await assert.rejects(record([spec({ instrument_id: "fx:GBPUSD", native_symbol: "GBPUSDm" }), bad]), (error) => {
      assert.equal(error.message, "Invalid fx symbol spec batch", JSON.stringify(bad));
      assert.ok(!JSON.stringify(error).includes(marker), "the error never echoes a value");
      return true;
    });
  }
  await assert.rejects(record([]), /Invalid fx symbol spec batch/);
  await assert.rejects(record(Array.from({ length: 501 }, (_, i) => spec({ instrument_id: `fx:S${i}` }))),
    /Invalid fx symbol spec batch/);
  await assert.rejects(db.query("SELECT record_fx_symbol_specs('{}'::jsonb)"), /Invalid fx symbol spec batch/);
  assert.equal(await count(), 0, "nothing is partly written");
  // The bounds themselves are accepted: 500 items, digits 0 and 10, rollover day 0 and 6.
  assert.equal(await record(Array.from({ length: 500 }, (_, i) => spec({ instrument_id: `fx:S${i}` }))), 500);
  assert.equal(await record([spec({ instrument_id: "fx:IDX", digits: 0, point: 1, tick_size: 1, swap_rollover3days: 0 }),
    spec({ instrument_id: "fx:TINY", digits: 10, point: 1e-10, tick_size: 1e-10, swap_rollover3days: 6 })]), 2);
});

test("the point CHECK on the table rejects a wrong point even without the function", async () => {
  await assert.rejects(db.query(`INSERT INTO fx_symbol_spec_observations (venue, instrument_id, native_symbol, digits,
    point, tick_size, tick_value, contract_size, volume_min, volume_max, volume_step, base_currency, profit_currency,
    margin_currency, account_currency, trade_mode, swap_mode, swap_long, swap_short, swap_rollover3days, spec_hash)
    VALUES ('test-venue', 'fx:EURUSD', 'EURUSDm', 5, 0.0001, 0.00001, 1, 100000, 0.01, 200, 0.01, 'EUR', 'USD',
    'EUR', 'USD', 4, 1, -7.3, 2.1, 3, '${"a".repeat(64)}')`), /check constraint/);
  assert.equal(await record([spec({ point: 0.000010 })]), 1, "0.000010 equals 10^-5");
});

test("service_role can execute; anon and authenticated cannot", async () => {
  await record([spec()]);
  for (const role of ["anon", "authenticated"]) {
    await db.exec(`SET ROLE ${role}`);
    try {
      await assert.rejects(db.query("SELECT * FROM fx_symbol_spec_observations"), /permission denied/);
      await assert.rejects(record([gold()]), /permission denied/);
      await assert.rejects(read(), /permission denied/);
    } finally {
      await db.exec("RESET ROLE");
    }
  }
  await db.exec("SET ROLE service_role");
  try {
    assert.equal(await record([gold()]), 1);
    assert.equal((await read()).length, 2);
  } finally {
    await db.exec("RESET ROLE");
  }
});
