import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { after, before, beforeEach, test } from "node:test";
import { PGlite } from "@electric-sql/pglite";

// #433 hosted fx_market_quotes: forward-only publish, validation, and reads for forex-entitled users only.
// Same harness and access fixtures as forex-market-db.test.mjs. Every quote here is SYNTHETIC.
const db = new PGlite();
const A = "11111111-1111-4111-8111-111111111111"; // entitled to forex
const B = "22222222-2222-4222-8222-222222222222"; // crypto only
const C = "33333333-3333-4333-8333-333333333333"; // no access row
const T0 = Date.UTC(2026, 9, 21, 12, 0);
const MIN = 60_000;

before(async () => {
  await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
    CREATE SCHEMA auth; CREATE TABLE auth.users(id UUID PRIMARY KEY);
    CREATE FUNCTION auth.uid() RETURNS UUID LANGUAGE SQL AS
      $$SELECT nullif(current_setting('request.jwt.claim.sub',true),'')::uuid$$;
    GRANT USAGE ON SCHEMA public,auth TO anon,authenticated,service_role;
    INSERT INTO auth.users VALUES ('${A}'),('${B}'),('${C}');`);
  for (const file of ["20261009120000_forward_harness.sql", "20261010120000_forward_clustered_report.sql",
    "20261010121000_forward_retryable_runs.sql", "20261011090000_forward_paired_report.sql",
    "20261011130000_forward_sigma_state.sql", "20261011150000_forward_shared_stages.sql",
    "20261016180000_paper_ledger_order_kinds.sql", "20261016190000_paper_account_summary.sql",
    "20261018100000_forex_access_and_market_tables.sql",
    "20261018110000_commit_user_run_market.sql", "20261019090000_fx_stage_defaults_and_rls.sql",
    "20261019100000_access_labels.sql", "20261019150000_fx_market_quotes.sql"])
    await db.exec(await readFile(new URL(`../supabase/migrations/${file}`, import.meta.url), "utf8"));
  await db.query("INSERT INTO user_access(user_id,plan) VALUES($1,'forex'),($2,'crypto')", [A, B]);
});
beforeEach(() => db.exec(`RESET ROLE; SELECT set_config('request.jwt.claim.sub','',false);
  TRUNCATE fx_market_quotes`));
after(() => db.close());

function quote(overrides = {}) {
  return { symbol: "EURUSD", instrument_id: "fx:EURUSD", venue: "test-venue", bid: 1.085, ask: 1.08512,
    spread_points: 12, digits: 5, changes: { 5: 0.01, 15: null, 60: 0.1, 240: null, 1440: null },
    chart: [{ time: T0 - 15 * MIN, close: 1.0849 }, { time: T0, close: 1.085 }],
    last_bar_at: new Date(T0).toISOString(), ...overrides };
}
const publish = async (quotes) =>
  (await db.query("SELECT publish_fx_market_quotes($1) AS n", [JSON.stringify(quotes)])).rows[0].n;
async function asUser(user) {
  await db.exec("RESET ROLE");
  await db.query("SELECT set_config('request.jwt.claim.sub',$1,false)", [user]);
  await db.exec("SET ROLE authenticated");
}

test("publish is a forward-only upsert: older or equal last_bar_at leaves the row untouched", async () => {
  assert.equal(await publish([quote(), quote({ symbol: "XAUUSD", instrument_id: "fx:XAUUSD", bid: 2412.37,
    ask: 2412.62, spread_points: 25, digits: 2 })]), 2);
  assert.equal(await publish([quote({ bid: 1.09, ask: 1.09012 })]), 0, "equal last_bar_at");
  assert.equal(await publish([quote({ bid: 1.08, ask: 1.08012, last_bar_at: new Date(T0 - MIN).toISOString() })]), 0);
  assert.equal(Number((await db.query("SELECT bid FROM fx_market_quotes WHERE symbol='EURUSD'")).rows[0].bid), 1.085);
  assert.equal(await publish([quote({ bid: 1.086, ask: 1.08612, last_bar_at: new Date(T0 + MIN).toISOString() })]), 1);
  const row = (await db.query("SELECT * FROM fx_market_quotes WHERE symbol='EURUSD'")).rows[0];
  assert.deepEqual([Number(row.bid), Number(row.ask), row.spread_points, row.digits], [1.086, 1.08612, 12, 5]);
  assert.equal(new Date(row.last_bar_at).getTime(), T0 + MIN);
  assert.deepEqual(row.changes, { 5: 0.01, 15: null, 60: 0.1, 240: null, 1440: null });
});

test("invalid quotes are rejected and nothing is written", async () => {
  const invalid = [
    quote({ symbol: "eurusd" }),
    quote({ instrument_id: "fx:GBPUSD" }), // symbol must equal its second part
    quote({ instrument_id: "EURUSD" }),
    quote({ venue: "Test Venue" }),
    quote({ bid: 0, ask: 0.00012 }),
    quote({ ask: 1.08 }), // below the bid
    quote({ spread_points: -1 }),
    quote({ digits: 11 }),
    quote({ changes: { 30: 0.1 } }),
    quote({ changes: [] }),
    quote({ chart: {} }),
    quote({ chart: Array.from({ length: 97 }, (_, i) => ({ time: T0 - i * 15 * MIN, close: 1.085 })) }),
    quote({ last_bar_at: null }),
  ];
  for (const bad of invalid) await assert.rejects(publish([quote({ symbol: "XAUUSD", instrument_id: "fx:XAUUSD" }), bad]), Error,
    JSON.stringify(bad).slice(0, 120));
  await assert.rejects(db.query("SELECT publish_fx_market_quotes('{}'::jsonb)"), /Invalid fx market quote batch/);
  assert.equal((await db.query("SELECT count(*)::int AS n FROM fx_market_quotes")).rows[0].n, 0);
  assert.equal(await publish([]), 0);
});

test("only forex-entitled users can read; others and anon get nothing; only service_role writes", async () => {
  await publish([quote()]);
  await asUser(A);
  assert.deepEqual((await db.query("SELECT symbol FROM fx_market_quotes")).rows, [{ symbol: "EURUSD" }]);
  await assert.rejects(publish([quote({ last_bar_at: new Date(T0 + MIN).toISOString() })]), /permission denied/);
  await assert.rejects(db.query("UPDATE fx_market_quotes SET bid = 2"), /permission denied/);
  await asUser(B);
  assert.deepEqual((await db.query("SELECT symbol FROM fx_market_quotes")).rows, [], "crypto-only user");
  await asUser(C);
  assert.deepEqual((await db.query("SELECT symbol FROM fx_market_quotes")).rows, [], "no access row");
  await db.exec("RESET ROLE; SET ROLE anon");
  await assert.rejects(db.query("SELECT symbol FROM fx_market_quotes"), /permission denied/);
  await db.exec("RESET ROLE; SET ROLE service_role");
  assert.equal(await publish([quote({ last_bar_at: new Date(T0 + MIN).toISOString() })]), 1);
  await db.exec("RESET ROLE");
});
