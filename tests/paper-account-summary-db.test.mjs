import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { after, before, test } from "node:test";
import { PGlite } from "@electric-sql/pglite";

// #36 Part 3: paper_account_summary() totals the caller's current wallet account in SQL, over every ledger line.
const db = new PGlite();
const A = "11111111-1111-4111-8111-111111111111";
const B = "22222222-2222-4222-8222-222222222222";
const C = "55555555-5555-4555-8555-555555555555";
const RUN_A = "33333333-3333-4333-8333-333333333333";
const RUN_B = "44444444-4444-4444-8444-444444444444";
const migrations = [
  "20261009120000_forward_harness.sql", "20261010120000_forward_clustered_report.sql",
  "20261010121000_forward_retryable_runs.sql", "20261011090000_forward_paired_report.sql",
  "20261011130000_forward_sigma_state.sql", "20261011150000_forward_shared_stages.sql",
  "20261016180000_paper_ledger_order_kinds.sql", "20261016190000_paper_account_summary.sql",
];
const counters = new Map();

async function line(user, account, run, type, setup, amount, ms, payload = {}) {
  const seq = (counters.get(`${user}/${account}`) ?? 0) + 1;
  counters.set(`${user}/${account}`, seq);
  await db.query(
    `INSERT INTO paper_ledger(user_id,paper_account,seq,ms,type,setup_id,symbol,amount_e8,balance_e8,payload,run_id)
     VALUES($1,$2,$3,$4,$5,$6,'BTCUSDT',$7,0,$8,$9)`,
    [user, account, seq, ms, type, setup, amount, JSON.stringify(payload), run],
  );
}

before(async () => {
  await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
    CREATE SCHEMA auth; CREATE TABLE auth.users(id UUID PRIMARY KEY);
    CREATE FUNCTION auth.uid() RETURNS UUID LANGUAGE SQL AS
      $$SELECT nullif(current_setting('request.jwt.claim.sub',true),'')::uuid$$;
    GRANT USAGE ON SCHEMA public,auth TO anon,authenticated,service_role;
    INSERT INTO auth.users VALUES ('${A}'),('${B}'),('${C}');`);
  for (const file of migrations)
    await db.exec(await readFile(new URL(`../supabase/migrations/${file}`, import.meta.url), "utf8"));
  await db.query("SELECT ensure_forward_account($1, 0)", [C]); // an account that has no ledger line yet
  for (const [user, run] of [[A, RUN_A], [B, RUN_B]]) {
    await db.query("SELECT ensure_forward_account($1, 0)", [user]);
    await db.query(
      `INSERT INTO paper_runs(id,user_id,paper_account,run_key,trigger,status,boundary_ms)
       VALUES($1,$2,$3,'hour:0','hourly','ok',0)`, [run, user, user]);
  }
  const a = (type, setup, amount, ms, payload) => line(A, A, RUN_A, type, setup, amount, ms, payload);
  // t1 target: +100 gross, 10 + 10 fees -> net 80
  await a("open_fee", "t1", -10, 1000); await a("pnl", "t1", 100, 2000, { outcome: "T" }); await a("close_fee", "t1", -10, 2000);
  // t2 stopped, with funding: -50 -5 -10 -10 -> net -75
  await a("open_fee", "t2", -10, 1100); await a("funding", "t2", -5, 1500);
  await a("pnl", "t2", -50, 2100, { outcome: "S" }); await a("close_fee", "t2", -10, 2100);
  // t3 liquidated: -10 -200 -> net -210
  await a("open_fee", "t3", -10, 1200); await a("liquidation", "t3", -200, 1800);
  // t4 ambiguous, net exactly 0 (neither a win nor a loss)
  await a("open_fee", "t4", -10, 1300); await a("pnl", "t4", 20, 2200, { outcome: "ambiguous" }); await a("close_fee", "t4", -10, 2200);
  // t5 only partly reduced: not a closed trade yet
  await a("open_fee", "t5", -10, 1400); await a("pnl", "t5", 5, 2300, { outcome: "T", position_event: "reduced" });
  await a("rejected", "r1", 0, 3000, { reason: "cap:max_positions" }); await a("rejected", "r2", 0, 3100, { reason: "over_close" });
  await a("order_rejected", "r3", 0, 3200, { reason: "limit_not_passive" });
  // an old account of the same user and another user's account must not count
  await line(A, "default", RUN_A, "pnl", "old", 999999, 10, { outcome: "T" });
  await line(B, B, RUN_B, "pnl", "b1", 777777, 20, { outcome: "T" });
  await line(B, B, RUN_B, "rejected", "b2", 0, 30, { reason: "x" });
});
after(() => db.close());

async function summary(user) {
  await db.query("SELECT set_config('request.jwt.claim.sub',$1,false)", [user]);
  await db.exec("SET ROLE authenticated");
  try {
    const { rows } = await db.query("SELECT * FROM paper_account_summary()");
    assert.equal(rows.length, 1);
    return Object.fromEntries(Object.entries(rows[0]).map(([key, value]) => [key, Number(value)]));
  } finally {
    await db.exec("RESET ROLE");
  }
}

test("closed trades are counted by outcome and their net, over every ledger line", async () => {
  const s = await summary(A);
  assert.deepEqual(
    [s.trades, s.target, s.stopped, s.expired, s.liquidated, s.ambiguous, s.unresolved],
    [4, 1, 1, 0, 1, 1, 0],
  );
  assert.deepEqual([s.wins, s.losses], [1, 2]);
  assert.deepEqual([s.sum_net_e8, s.gross_wins_e8, s.gross_losses_e8], [-205, 80, -285]);
});

test("rejections and every money kind are totalled; only the current account counts", async () => {
  const s = await summary(A);
  assert.deepEqual([s.rejected, s.order_rejected], [2, 1]);
  assert.deepEqual(
    [s.open_fees_e8, s.close_fees_e8, s.funding_e8, s.liquidation_e8, s.pnl_e8],
    [-50, -30, -5, -200, 75],
  );
  assert.deepEqual([s.lines, s.first_ms, s.last_ms], [17, 1000, 3200]);
});

test("another account sees only its own rows (RLS) and an empty account sees zeros", async () => {
  const b = await summary(B);
  assert.deepEqual([b.trades, b.lines, b.rejected, b.sum_net_e8], [1, 2, 1, 777777]);
  const empty = await summary(C);
  assert.deepEqual([empty.trades, empty.lines, empty.rejected, empty.sum_net_e8, empty.gross_losses_e8], [0, 0, 0, 0, 0]);
});
