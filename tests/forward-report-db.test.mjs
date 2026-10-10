import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { after, before, beforeEach, test } from "node:test";
import { PGlite } from "@electric-sql/pglite";

// SQL reports of the forward harness (P20): hand-computed sums, filters, RLS, and the old cap bug.
const db = new PGlite();
const USER = "11111111-1111-4111-8111-111111111111";
const OTHER = "22222222-2222-4222-8222-222222222222";
const id = (n) => n.toString(16).padStart(64, "0");

before(async () => {
  await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
    CREATE SCHEMA auth; CREATE TABLE auth.users(id UUID PRIMARY KEY);
    CREATE FUNCTION auth.uid() RETURNS UUID LANGUAGE SQL AS
      'SELECT nullif(current_setting(''request.jwt.claim.sub'',true),'''')::uuid';
    GRANT USAGE ON SCHEMA public,auth TO anon,authenticated,service_role;
    INSERT INTO auth.users VALUES ('${USER}'),('${OTHER}');`);
  await db.exec(await readFile(new URL("../supabase/migrations/20261009120000_forward_harness.sql", import.meta.url), "utf8"));
  await db.exec(await readFile(new URL("../supabase/migrations/20261010120000_forward_clustered_report.sql", import.meta.url), "utf8"));
  await db.exec(await readFile(new URL("../supabase/migrations/20261010121000_forward_retryable_runs.sql", import.meta.url), "utf8"));
  await db.exec(await readFile(new URL("../supabase/migrations/20261011090000_forward_paired_report.sql", import.meta.url), "utf8"));
});
beforeEach(async () => {
  await db.exec(`RESET ROLE; TRUNCATE paper_runs, forward_signals, forward_setups, forward_outcomes, paper_ledger CASCADE;
    INSERT INTO paper_runs(id,user_id,run_key,trigger,status,boundary_ms)
      VALUES ('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa','${USER}','r','hourly','ok',0),
             ('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb','${OTHER}','r','hourly','ok',0);`);
});
after(() => db.close());

const RUN = { [USER]: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", [OTHER]: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb" };

async function setup(n, strategy, { user = USER, h = 60, rr = "2", status = "T", symbol = "BTCUSDT", side = 1, entry = 100 } = {}) {
  await db.query(
    `INSERT INTO forward_signals(user_id,signal_id,strategy_id,version,symbol,signal_ms,side,horizon_min,run_id)
     VALUES ($1,$2,$3,'v',$4,$5,$6,$7,$8)`,
    [user, id(n), strategy, symbol, entry - 1, side, h, RUN[user]]);
  await db.query(
    `INSERT INTO forward_setups(user_id,setup_id,signal_id,strategy_id,version,symbol,side,horizon_min,signal_ms,entry_ms,k,rr,status,params_hash,payload,run_id)
     VALUES ($1,$2,$2,$3,'v',$4,$5,$6,$7,$8,'2',$9,$10,$11,'{}',$12)`,
    [user, id(n), strategy, symbol, side, h, entry - 1, entry, rr, status, "a".repeat(64), RUN[user]]);
}
async function outcome(n, status, net, exit, { user = USER, cost = 0, fund = 0, final = true } = {}) {
  await db.query(
    `INSERT INTO forward_outcomes(user_id,setup_id,status,final,exit_ms,net_ur,cost_ur,fund_ur,payload,run_id)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'{}',$9)`,
    [user, id(n), status, final, exit, net, cost, fund, RUN[user]]);
}
const as = (user) => db.exec(`SET ROLE authenticated; SELECT set_config('request.jwt.claim.sub','${user}',false);`);
const NIL = "NULL::bigint, NULL::bigint, NULL::text[], NULL::text[], NULL::int[], NULL::int[], NULL::text[]";
const report = async (args = NIL) =>
  (await db.query(`SELECT * FROM forward_outcome_report(${args})`)).rows;

async function fixture() {
  // A: 3 finished trades (T +2 R, S -1 R, ambiguous -1 R) + control (-1 R at 1500, +1 R at 2500).
  const A = "ema_cross_20_50:60";
  await setup(1, A); await outcome(1, "T", 2_000_000, 1000, { cost: 10, fund: -5 });
  await setup(2, A); await outcome(2, "S", -1_000_000, 2000, { cost: 10 });
  await setup(3, A); await outcome(3, "ambiguous", -1_000_000, 3000, { cost: 10 });
  await setup(4, `placebo-v1:${A}`); await outcome(4, "S", -1_000_000, 1500);
  await setup(5, `placebo-v1:${A}`); await outcome(5, "T", 1_000_000, 2500);
  // B: one expired 15 m trade; a non-final outcome row must not count.
  await setup(6, "macd_12_26_9:15", { h: 15, rr: "3/2", side: -1, symbol: "ETHUSDT" }); await outcome(6, "E", 500_000, 5000);
  await setup(7, A); await outcome(7, "pending", null, null, { final: false });
  // Non-trades for A, and one entered setup with no final outcome yet.
  await setup(8, A, { status: "V" }); await setup(9, A, { status: "C" }); await setup(10, A, { status: "T" });
  // Another account's trade must never appear.
  await setup(11, A, { user: OTHER }); await outcome(11, "T", 9_000_000, 1000, { user: OTHER });
}

test("the outcome report equals the hand-computed sums, with the matched control beside each row", async () => {
  await fixture();
  await as(USER);
  const rows = await report();
  assert.equal(rows.length, 2);
  const [a, b] = rows;
  assert.equal(a.strategy_id, "ema_cross_20_50:60");
  assert.deepEqual([a.n, a.n_t, a.n_s, a.n_e, a.n_l, a.n_x, a.n_ambiguous].map(Number), [3, 1, 1, 0, 0, 0, 1]);
  assert.deepEqual([a.sum_net_ur, a.sum_sq_net_ur, a.sum_cost_ur, a.sum_fund_ur].map(Number), [0, 6e12, 30, -5]);
  assert.deepEqual([a.first_exit_ms, a.last_exit_ms].map(Number), [1000, 3000]);
  assert.deepEqual([a.placebo_n, a.placebo_sum_net_ur, a.placebo_sum_sq_net_ur].map(Number), [2, 0, 2e12]);
  assert.deepEqual([b.strategy_id, Number(b.n), Number(b.n_e), Number(b.placebo_n)], ["macd_12_26_9:15", 1, 1, 0]);
  assert.ok(!rows.some((row) => row.strategy_id.startsWith("placebo-v1:")), "controls are not rows of their own");
});

test("filters: exit range, strategy, symbol, side, horizon and rr", async () => {
  await fixture();
  await as(USER);
  const ranged = await report("2000, 4000, NULL::text[], NULL::text[], NULL::int[], NULL::int[], NULL::text[]");
  assert.deepEqual([ranged[0].n, ranged[0].sum_net_ur, ranged[0].placebo_n, ranged[0].placebo_sum_net_ur].map(Number),
    [2, -2_000_000, 1, 1_000_000], "exit_ms in [2000, 4000): trades at 2000 and 3000, control at 2500");
  const one = await report("NULL::bigint, NULL::bigint, ARRAY['macd_12_26_9:15'], NULL::text[], NULL::int[], NULL::int[], NULL::text[]");
  assert.deepEqual(one.map((row) => row.strategy_id), ["macd_12_26_9:15"]);
  const shorts = await report("NULL::bigint, NULL::bigint, NULL::text[], NULL::text[], NULL::int[], ARRAY[-1], NULL::text[]");
  assert.deepEqual(shorts.map((row) => row.strategy_id), ["macd_12_26_9:15"]);
  const rr2 = await report("NULL::bigint, NULL::bigint, NULL::text[], NULL::text[], ARRAY[60], NULL::int[], ARRAY['2']");
  assert.deepEqual(rr2.map((row) => row.strategy_id), ["ema_cross_20_50:60"]);
  assert.equal((await report("NULL::bigint, NULL::bigint, NULL::text[], ARRAY['SOLUSDT'], NULL::int[], NULL::int[], NULL::text[]")).length, 0);
});

test("non-trades are counted per status next to the entered trades that are still open", async () => {
  await fixture();
  await as(USER);
  const { rows } = await db.query(`SELECT * FROM forward_nontrade_report(${NIL})`);
  const a = rows.find((row) => row.strategy_id === "ema_cross_20_50:60");
  assert.deepEqual([a.n_v, a.n_c, a.n_p, a.n_g, a.n_n, a.pending_or_open].map(Number), [1, 1, 0, 0, 0, 2],
    "setup 7 (only a non-final outcome) and setup 10 are open; resolved trades are not");
  assert.equal(rows.some((row) => row.strategy_id.startsWith("placebo-v1:")), false);
  const open = await db.query("SELECT setup_id FROM forward_open_setups ORDER BY setup_id");
  assert.deepEqual(open.rows.map((row) => row.setup_id), [id(7), id(10)]);
  const log = await db.query("SELECT setup_id, status, net_ur FROM forward_outcome_log WHERE strategy_id = 'ema_cross_20_50:60' ORDER BY exit_ms");
  assert.deepEqual(log.rows.map((row) => row.status), ["T", "S", "ambiguous"]);
});

test("nothing is capped: 2,500 setups and 12,000 ledger lines are all reported", async () => {
  const run = RUN[USER];
  await db.exec(`
    INSERT INTO forward_signals(user_id,signal_id,strategy_id,version,symbol,signal_ms,side,horizon_min,run_id)
      SELECT '${USER}', lpad(to_hex(n), 64, '0'), 'rsi_14_reversion:15', 'v', 'BTCUSDT', n, 1, 15, '${run}'
      FROM generate_series(1, 2500) n;
    INSERT INTO forward_setups(user_id,setup_id,signal_id,strategy_id,version,symbol,side,horizon_min,signal_ms,entry_ms,k,rr,status,params_hash,payload,run_id)
      SELECT '${USER}', lpad(to_hex(n), 64, '0'), lpad(to_hex(n), 64, '0'), 'rsi_14_reversion:15', 'v', 'BTCUSDT', 1, 15, n, n + 1, '2', '2', 'T',
             repeat('a', 64), '{}', '${run}' FROM generate_series(1, 2500) n;
    INSERT INTO forward_outcomes(user_id,setup_id,status,final,exit_ms,net_ur,cost_ur,fund_ur,payload,run_id)
      SELECT '${USER}', lpad(to_hex(n), 64, '0'), 'T', true, n + 10, 1000000, 0, 0, '{}', '${run}' FROM generate_series(1, 2500) n;
    INSERT INTO paper_ledger(user_id,seq,ms,type,amount_e8,balance_e8,payload,run_id)
      SELECT '${USER}', n, n * 1000, 'pnl', 1, 1000 + n, '{}', '${run}' FROM generate_series(1, 12000) n;`);
  await as(USER);
  const [row] = await report();
  assert.equal(Number(row.n), 2500, "the oldest setups are still counted");
  assert.equal(Number(row.sum_net_ur), 2500 * 1_000_000);
  const series = (await db.query("SELECT * FROM paper_equity_series(1000, NULL)")).rows;
  assert.equal(series.length, 12000, "one point per bucket, no row cap");
  assert.deepEqual([Number(series.at(-1).seq), Number(series.at(-1).balance_e8)], [12000, 13000]);
  const hourly = (await db.query("SELECT * FROM paper_equity_series(3600000, NULL)")).rows;
  assert.equal(hourly.length, 4);
  assert.equal(Number(hourly.at(-1).seq), 12000, "each bucket keeps its last line");
});

test("SQL clusters by UTC exit day with uneven trade counts, not session timezone", async () => {
  const strategy = "ema_cross_20_50:60";
  const real = [[1, 3], [-2], [2, 2, 0]];
  const control = [[-1, 1], [-2], [0, 0, 2]];
  let n = 100;
  for (const [name, days] of [[strategy, real], [`placebo-v1:${strategy}`, control]]) {
    for (let day = 0; day < days.length; day++) {
      for (const [i, value] of days[day].entries()) {
        await setup(++n, name);
        await outcome(n, value > 0 ? "T" : "S", value * 1e6, (day + 1) * 86400000 + (i % 2 ? 86399999 : 0));
      }
    }
  }
  await as(USER);
  await db.exec("SET TIME ZONE 'Asia/Omsk'");
  const [r] = await report();
  assert.deepEqual([r.n, r.exit_days, r.sum_net_ur, r.cluster_sum_sq_ur].map(Number), [6, 3, 6e6, 14e12]);
  assert.deepEqual([r.placebo_n, r.placebo_exit_days, r.placebo_cluster_sum_sq_ur].map(Number), [6, 3, 8e12]);
  // Filter at an exact UTC boundary; both real and control clusters must be recomputed.
  const [filtered] = await report(`172800000, NULL::bigint, NULL::text[], NULL::text[], NULL::int[], NULL::int[], NULL::text[]`);
  assert.deepEqual([filtered.n, filtered.exit_days, filtered.cluster_sum_sq_ur].map(Number), [4, 2, 12.5e12]);
  await db.exec("SET TIME ZONE 'UTC'");
});

test("skipped and no-sigma attempts preserve audit rows without taking the successful hour claim", async () => {
  const insert = (status) => db.query(`INSERT INTO paper_runs(user_id,run_key,trigger,status,boundary_ms)
    VALUES ($1,'hour:3600000','hourly',$2,3600000)`, [USER, status]);
  await insert("skipped_stale");
  await insert("no_sigma");
  await insert("ok");
  await assert.rejects(insert("ok"), /duplicate key/);
  const result = await db.query("SELECT status FROM paper_runs WHERE run_key = 'hour:3600000'");
  assert.equal(result.rows.length, 3);
});

// Paired-by-exit-day difference (forward_paired_report): hand-computed, with the control absent on one day.
const DAY_MS = 86_400_000;
const dayMs = (d) => (20_000 + d) * DAY_MS + 1_000;
async function addDays(startId, strategy, days) {
  let n = startId;
  for (const [day, nets] of Object.entries(days)) {
    for (const net of nets) {
      await setup(n, strategy);
      await outcome(n, "E", Math.round(net * 1_000_000), dayMs(Number(day)));
      n += 1;
    }
  }
}
const paired = async () => (await db.query(`SELECT * FROM forward_paired_report(${NIL})`)).rows;

test("the paired report equals the hand-computed residual sums over the union of exit days", async () => {
  const A = "ema_cross_20_50:60";
  await addDays(100, A, { 1: [1, 3], 2: [-2], 3: [2, 2, 0] });
  await addDays(200, `placebo-v1:${A}`, { 1: [1], 2: [-1, -1], 4: [2] });
  await as(USER);
  const rows = await paired();
  assert.equal(rows.length, 1);
  assert.deepEqual([rows[0].strategy_id, Number(rows[0].paired_days), Number(rows[0].union_days)], [A, 2, 4]);
  // r_d (R): day 1 7/48, day 2 1/8, day 3 1/6 (no control trade), day 4 -7/16 (no strategy trade).
  assert.ok(Math.abs(Number(rows[0].paired_ss) / 1e12 - 295 / 1152) < 1e-9);
});

test("paired report: a strategy without a control has no paired row; filters and RLS match the outcome report", async () => {
  const A = "ema_cross_20_50:60";
  await addDays(100, A, { 1: [1], 2: [2] });                                   // no control at all
  await addDays(300, "macd_12_26_9:60", { 1: [1], 2: [2] });
  await addDays(400, "placebo-v1:macd_12_26_9:60", { 1: [0], 2: [1] });
  await setup(500, "macd_12_26_9:60", { user: OTHER }); await outcome(500, "T", 9_000_000, dayMs(1), { user: OTHER });
  await as(USER);
  assert.deepEqual((await paired()).map((r) => r.strategy_id), ["macd_12_26_9:60"]);
  const none = (await db.query(
    "SELECT * FROM forward_paired_report(NULL::bigint, $1, NULL::text[], NULL::text[], NULL::int[], NULL::int[], NULL::text[])", [dayMs(0)])).rows;
  assert.equal(none.length, 0, "the exit range filter applies to both series");
});

test("perfectly correlated strategy and control: the paired sum is ~0 while each series' own clustered sum is not", async () => {
  const A = "ema_cross_20_50:60";
  const x = Array.from({ length: 12 }, (_, d) => (d % 5) - 2);
  await addDays(100, A, Object.fromEntries(x.map((v, d) => [d, [v]])));
  await addDays(200, `placebo-v1:${A}`, Object.fromEntries(x.map((v, d) => [d, [v - 0.5]])));
  await as(USER);
  const [row] = await paired();
  assert.equal(Number(row.paired_days), 12);
  assert.ok(Number(row.paired_ss) < 1e-3, `paired sum ${row.paired_ss} (micro-R squared)`);
  const own = (await db.query(`SELECT cluster_sum_sq_ur, placebo_cluster_sum_sq_ur FROM forward_outcome_report(${NIL})`)).rows[0];
  assert.ok(Number(own.cluster_sum_sq_ur) > 1e12 && Number(own.placebo_cluster_sum_sq_ur) > 1e12);
});
