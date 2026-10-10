import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { before, after, beforeEach, test } from "node:test";
import { PGlite } from "@electric-sql/pglite";

const db = new PGlite();
const A = "11111111-1111-4111-8111-111111111111";
const B = "22222222-2222-4222-8222-222222222222";
const STAGE = "33333333-3333-4333-8333-333333333333";
const LEGACY = "44444444-4444-4444-8444-444444444444";
const HOUR = 3_600_000;
const hash = "a".repeat(64);
const seed = { symbol: "BTCUSDT", sigma_version: "forward-sigma-v1", as_of_ms: HOUR - 60000,
  payload: {}, checksum: hash };
const stage = { id: STAGE, boundary_ms: HOUR, versions: {}, params_hash: hash,
  payload: { processed_to_ms: { BTCUSDT: HOUR - 60000 } } };
const event = { stage_run_id: STAGE, symbol: "BTCUSDT", sequence: 0, ms: 60000,
  type: "funding", rate: "0.001", mark: 100 };
const records = { signals: [], setups: [], outcomes: [], events: [event], adverse_paths: [] };
const run = { status: "ok", trigger: "hourly", wallet_state: { balance_e8: 1000, seq: 1 } };
let migrated;

before(async () => {
  await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
    CREATE SCHEMA auth; CREATE TABLE auth.users(id UUID PRIMARY KEY);
    CREATE FUNCTION auth.uid() RETURNS UUID LANGUAGE SQL AS
      $$SELECT nullif(current_setting('request.jwt.claim.sub',true),'')::uuid$$;
    GRANT USAGE ON SCHEMA public,auth TO anon,authenticated,service_role;
    INSERT INTO auth.users VALUES ('${A}'),('${B}');`);
  for (const file of ["20261009120000_forward_harness.sql", "20261010120000_forward_clustered_report.sql",
    "20261010121000_forward_retryable_runs.sql", "20261011090000_forward_paired_report.sql",
    "20261011130000_forward_sigma_state.sql"])
    await db.exec(await readFile(new URL(`../supabase/migrations/${file}`, import.meta.url), "utf8"));
  // Seed the same old market fact for two users and preserve the owner's default account.
  for (const [user, id] of [[A, LEGACY], [B, STAGE]]) {
    await db.query(`INSERT INTO paper_runs(id,user_id,run_key,trigger,status,boundary_ms,wallet_config,wallet_state)
      VALUES($1,$2,'hour:0','hourly','ok',0,'{"leverage_cap":5}','{"balance_e8":123,"seq":1}')`, [id, user]);
    await db.query(`INSERT INTO forward_signals(user_id,signal_id,strategy_id,version,symbol,signal_ms,side,horizon_min,run_id)
      VALUES($1,$2,'test','v1','BTCUSDT',0,1,15,$3)`, [user, hash, id]);
  }
  await db.exec(await readFile(new URL("../supabase/migrations/20261011150000_forward_shared_stages.sql", import.meta.url), "utf8"));
  migrated = {
    signals: (await db.query("SELECT * FROM forward_market_signals")).rows,
    accounts: (await db.query("SELECT * FROM forward_wallet_accounts ORDER BY user_id")).rows,
    old: (await db.query("SELECT count(*)::int AS n FROM forward_signals")).rows[0].n,
  };
});
beforeEach(() => db.exec(`RESET ROLE; TRUNCATE forward_market_stages,forward_wallet_accounts,
  paper_runs,forward_signals,forward_setups,forward_outcomes,paper_ledger,forward_sigma_state CASCADE`));
after(() => db.close());

async function market(value = stage, output = records, states = [seed], expected = { BTCUSDT: null }) {
  return (await db.query("SELECT commit_market_stage($1,$2,$3,$4) AS id",
    [value, output, states, expected].map(value => JSON.stringify(value)))).rows[0].id;
}
async function account(user, boundary = 0, cap = 5) {
  await db.query("SELECT ensure_forward_account($1,$2)", [user, boundary]);
  await db.query("UPDATE forward_wallet_accounts SET config=$1 WHERE user_id=$2", [{ leverage_cap: cap }, user]);
}
function ledger(amount) {
  return [{ seq: 1, ms: 60000, type: "funding", symbol: "BTCUSDT", amount_e8: amount,
    balance_e8: 1000 + amount, assumptions: ["flat-tier"] }];
}
async function userCommit(user, lines = [], previous = null, value = run, stageId = STAGE) {
  return (await db.query("SELECT commit_user_run($1,$2,$3,$4,$5) AS id",
    [user, stageId, JSON.stringify(value), JSON.stringify(lines), previous])).rows[0].id;
}
async function count(table) { return (await db.query(`SELECT count(*)::int AS n FROM ${table}`)).rows[0].n; }

test("additive migration deduplicates facts and retains legacy account/config and rows", () => {
  assert.equal(migrated.signals.length, 1);
  assert.equal(migrated.old, 2);
  assert.ok(!("user_id" in migrated.signals[0]));
  assert.equal(migrated.accounts[0].account_id, "default");
  assert.equal(migrated.accounts[0].config.leverage_cap, 5);
});

test("two user configs and ledgers stay independent; each hour is a no-op on replay", async () => {
  await db.exec("SET ROLE service_role");
  await account(A, 0, 5); await account(B, 0, 20);
  assert.equal(await market(), STAGE);
  const checkpoint = (await db.query("SELECT * FROM forward_sigma_state")).rows;
  assert.equal(await market({ ...stage, id: LEGACY }), STAGE);
  assert.deepEqual((await db.query("SELECT * FROM forward_sigma_state")).rows, checkpoint);
  const a = await userCommit(A, ledger(-10));
  const b = await userCommit(B, ledger(-20));
  assert.equal(await userCommit(A, ledger(-10)), a);
  assert.equal(await userCommit(B, ledger(-20)), b);
  assert.equal(await count("forward_market_events"), 1);
  assert.equal(await count("paper_runs"), 2);
  assert.deepEqual((await db.query("SELECT amount_e8 FROM paper_ledger ORDER BY user_id")).rows.map(r => Number(r.amount_e8)), [-10, -20]);
  assert.deepEqual((await db.query("SELECT config->>'leverage_cap' AS cap FROM forward_wallet_accounts ORDER BY user_id")).rows.map(r => r.cap), ["5", "20"]);
});

test("RLS exposes shared facts but only the caller's wallet, with no client writes", async () => {
  await account(A); await account(B); await market();
  await userCommit(A, ledger(-10)); await userCommit(B, ledger(-20));
  await db.query("SELECT set_config('request.jwt.claim.sub',$1,false)", [A]);
  await db.exec("SET ROLE authenticated");
  assert.equal(await count("forward_market_events"), 1);
  assert.deepEqual((await db.query("SELECT user_id FROM paper_ledger")).rows, [{ user_id: A }]);
  assert.deepEqual((await db.query("SELECT user_id FROM paper_runs")).rows, [{ user_id: A }]);
  assert.deepEqual((await db.query("SELECT user_id FROM forward_wallet_accounts")).rows, [{ user_id: A }]);
  await assert.rejects(userCommit(B));
  await assert.rejects(db.query("UPDATE forward_wallet_accounts SET config='{}'"));
  await assert.rejects(market());
  await assert.rejects(db.query("INSERT INTO paper_ledger DEFAULT VALUES"));
});

test("failed user insertion rolls back that wallet only, preserving market and other user", async () => {
  await account(A); await account(B); await market();
  await userCommit(A, ledger(-10));
  await assert.rejects(userCommit(B, [{ ...ledger(-20)[0], seq: 0 }]));
  assert.equal(await count("paper_runs"), 1);
  assert.equal(await count("paper_ledger"), 1);
  assert.equal(await count("forward_market_stages"), 1);
  assert.equal(await count("forward_market_events"), 1);
  assert.equal(await count("forward_sigma_state"), 1);
  await userCommit(B, ledger(-20));
  assert.equal(await count("paper_runs"), 2);
});

test("new account starts now; retries do not consume cursor; stale expected runs fail", async () => {
  await market(); await account(A, HOUR); await account(B);
  await assert.rejects(userCommit(A));
  for (const status of ["no_sigma", "skipped_stale"])
    await userCommit(B, [], null, { ...run, status });
  assert.equal((await db.query("SELECT count(*)::int AS n FROM paper_runs WHERE consumed_boundary_ms IS NOT NULL")).rows[0].n, 0);
  const prior = await userCommit(B);
  const next = { ...stage, id: LEGACY, boundary_ms: 2 * HOUR };
  await market(next, { ...records, events: [] }, [{ ...seed, as_of_ms: 2 * HOUR - 60000, checksum: "b".repeat(64) }], { BTCUSDT: hash });
  await assert.rejects(userCommit(B, [], null, run, LEGACY), /Wallet changed/);
  await userCommit(B, [], prior, run, LEGACY);
});

test("market output or checksum failure never leaves partial shared rows or advanced sigma", async () => {
  await assert.rejects(market(stage, { ...records, signals: [{ signal_id: "invalid" }] }));
  assert.equal(await count("forward_market_stages"), 0);
  assert.equal(await count("forward_sigma_state"), 0);
  await assert.rejects(market(stage, records, [seed], { BTCUSDT: hash }));
  assert.equal(await count("forward_market_events"), 0);
});
