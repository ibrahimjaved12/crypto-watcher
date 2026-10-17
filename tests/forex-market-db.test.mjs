import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { before, after, beforeEach, test } from "node:test";
import { PGlite } from "@electric-sql/pglite";

// #374 forex database: access rules, fx market facts with their own cursor, per-market paper accounts.
const db = new PGlite();
const A = "11111111-1111-4111-8111-111111111111";
const B = "22222222-2222-4222-8222-222222222222";
const FX1 = "33333333-3333-4333-8333-333333333333";
const FX2 = "44444444-4444-4444-8444-444444444444";
const FX3 = "55555555-5555-4555-8555-555555555555";
const CRYPTO = "66666666-6666-4666-8666-666666666666";
const HOUR = 3_600_000;
const hex = (c) => c.repeat(64);
const run = { status: "ok", trigger: "hourly", wallet_state: { balance_e8: 1000, seq: 1 } };
const FX_TABLES = ["fx_market_stages", "fx_market_signals", "fx_market_suppressed", "fx_market_setups",
  "fx_market_outcomes", "fx_market_events"];

before(async () => {
  await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
    CREATE SCHEMA auth; CREATE TABLE auth.users(id UUID PRIMARY KEY);
    CREATE FUNCTION auth.uid() RETURNS UUID LANGUAGE SQL AS
      $$SELECT nullif(current_setting('request.jwt.claim.sub',true),'')::uuid$$;
    GRANT USAGE ON SCHEMA public,auth TO anon,authenticated,service_role;
    INSERT INTO auth.users VALUES ('${A}'),('${B}');`);
  for (const file of ["20261009120000_forward_harness.sql", "20261010120000_forward_clustered_report.sql",
    "20261010121000_forward_retryable_runs.sql", "20261011090000_forward_paired_report.sql",
    "20261011130000_forward_sigma_state.sql", "20261011150000_forward_shared_stages.sql",
    "20261016180000_paper_ledger_order_kinds.sql", "20261016190000_paper_account_summary.sql",
    "20261018100000_forex_access_and_market_tables.sql"])
    await db.exec(await readFile(new URL(`../supabase/migrations/${file}`, import.meta.url), "utf8"));
});
beforeEach(() => db.exec(`RESET ROLE; SELECT set_config('request.jwt.claim.sub','',false);
  TRUNCATE user_access,fx_market_stages,forward_market_stages,forward_wallet_accounts,paper_runs,paper_ledger,
  forward_sigma_state CASCADE`));
after(() => db.close());

async function asUser(user) {
  await db.exec("RESET ROLE");
  await db.query("SELECT set_config('request.jwt.claim.sub',$1,false)", [user]);
  await db.exec("SET ROLE authenticated");
}
async function grant(user, fields) {
  const cols = ["user_id", ...Object.keys(fields)];
  await db.query(`INSERT INTO user_access(${cols.join(",")}) VALUES(${cols.map((_, i) => `$${i + 1}`).join(",")})`,
    [user, ...Object.values(fields)]);
}
async function markets(user) { return (await db.query("SELECT markets_of($1) AS m", [user])).rows[0].m; }
async function count(table, where = "true") {
  return (await db.query(`SELECT count(*)::int AS n FROM ${table} WHERE ${where}`)).rows[0].n;
}
const sqlstate = (code) => (error) => error.code === code;

function stage(id, boundary) {
  return { id, boundary_ms: boundary, versions: {}, params_hash: hex("d"), payload: {} };
}
function records(stageId) {
  const signal = { signal_id: hex("a"), strategy_id: "ema_cross_20_50:60", version: "ta-v1", symbol: "EURUSD",
    instrument_id: "fx:EURUSD", signal_ms: 0, side: 1, horizon_min: 60, sessions: ["london"] };
  return {
    signals: [signal],
    suppressed: [{ ...signal, signal_id: hex("b"), reason: "news_blackout", detail: { event: "NFP" } }],
    setups: [{ setup_id: hex("c"), signal_id: hex("a"), strategy_id: signal.strategy_id, version: "ta-v1",
      symbol: "EURUSD", side: 1, horizon_min: 60, signal_ms: 0, entry_ms: 0, k: "2", rr: "2", status: "T",
      geometry_version: "fx-atr-v1", params_hash: hex("e"), payload: { p0: 108_010_000, d_ticks: 200 } }],
    outcomes: [{ setup_id: hex("c"), status: "T", final: true, exit_ms: 60000, net_ur: 2, cost_ur: 0, fund_ur: 0,
      payload: {} }],
    events: [{ stage_run_id: stageId, symbol: "EURUSD", sequence: 0, ms: 60000, type: "swap", multiplier: 1 }],
  };
}
async function fxStage(id, boundary, output = { signals: [], suppressed: [], setups: [], outcomes: [], events: [] }) {
  return (await db.query("SELECT commit_fx_stage($1,$2) AS id",
    [JSON.stringify(stage(id, boundary)), JSON.stringify(output)])).rows[0].id;
}
async function cryptoStage(id = CRYPTO, boundary = HOUR) {
  await db.query("INSERT INTO forward_market_stages(id,boundary_ms,versions,payload) VALUES($1,$2,'{}','{}')", [id, boundary]);
}
async function fxCommit(user, stageId, lines = [], previous = null) {
  return (await db.query("SELECT commit_fx_user_run($1,$2,$3,$4,$5) AS id",
    [user, stageId, JSON.stringify(run), JSON.stringify(lines), previous])).rows[0].id;
}
async function cryptoCommit(user, stageId, lines = [], previous = null) {
  return (await db.query("SELECT commit_user_run($1,$2,$3,$4,$5) AS id",
    [user, stageId, JSON.stringify(run), JSON.stringify(lines), previous])).rows[0].id;
}
async function accounts(user) {
  await db.query("SELECT ensure_forward_account($1,0)", [user]);
  await db.query("SELECT ensure_fx_account($1,0)", [user]);
}
const line = (seq, type, amount, balance, extra = {}) =>
  ({ seq, ms: 60000 * seq, type, symbol: "EURUSD", amount_e8: amount, balance_e8: balance, ...extra });

test("access: defaults, roles, plans, extras and expiry; users read only their own row and cannot write it", async () => {
  assert.deepEqual(await markets(A), ["crypto"]);
  await grant(A, { role: "owner" });
  assert.deepEqual(await markets(A), ["crypto", "forex"]);
  await db.query("UPDATE user_access SET role='member',plan='forex' WHERE user_id=$1", [A]);
  assert.deepEqual(await markets(A), ["forex"]);
  await db.query("UPDATE user_access SET plan='crypto',extra_markets='{forex}' WHERE user_id=$1", [A]);
  assert.deepEqual(await markets(A), ["crypto", "forex"]);
  await grant(B, { plan: "pro", expires_at: new Date(Date.now() - 86_400_000).toISOString() });
  assert.deepEqual(await markets(B), ["crypto"]);
  await db.query("UPDATE user_access SET expires_at=NULL WHERE user_id=$1", [B]);
  assert.deepEqual(await markets(B), ["crypto", "forex"]);
  await db.query("UPDATE user_access SET plan='forex' WHERE user_id=$1", [B]);

  await asUser(B);
  assert.deepEqual((await db.query("SELECT my_markets() AS m")).rows[0].m, ["forex"]);
  assert.equal((await db.query("SELECT has_market('crypto') AS h")).rows[0].h, false);
  assert.deepEqual((await db.query("SELECT user_id FROM user_access")).rows, [{ user_id: B }]);
  await assert.rejects(db.query("SELECT markets_of($1)", [A]), /permission denied/);
  await assert.rejects(db.query("INSERT INTO user_access(user_id,role) VALUES($1,'owner')", [B]), /permission denied/);
  await assert.rejects(db.query("UPDATE user_access SET role='owner' WHERE user_id=$1", [B]), /permission denied/);
  await asUser(A);
  assert.deepEqual((await db.query("SELECT my_markets() AS m")).rows[0].m, ["crypto", "forex"]);
});

test("RLS: fx facts are visible only to forex-entitled users and are append-only", async () => {
  await fxStage(FX1, HOUR, records(FX1));
  await grant(A, { plan: "forex" });
  await asUser(B);
  for (const table of FX_TABLES) assert.equal(await count(table), 0, table);
  assert.equal(await count("fx_market_outcome_log"), 0);
  await asUser(A);
  for (const table of FX_TABLES) assert.equal(await count(table), 1, table);
  assert.equal(await count("fx_market_outcome_log"), 1);
  assert.equal(await count("fx_market_open_setups"), 0);  // its only setup has a final outcome
  await db.exec("RESET ROLE");
  for (const table of FX_TABLES) {
    await assert.rejects(db.query(`UPDATE ${table} SET payload=payload`), /append-only/, table);
    await assert.rejects(db.query(`DELETE FROM ${table}`), /append-only/, table);
  }
});

test("commit_fx_stage: idempotent per boundary, own cursor, event stage check, independent of crypto stages", async () => {
  await cryptoStage(CRYPTO, HOUR);
  assert.equal(await fxStage(FX1, HOUR, records(FX1)), FX1);
  assert.equal(await fxStage(FX2, HOUR, records(FX2)), FX1);  // same boundary: the existing id, nothing new
  for (const table of FX_TABLES) assert.equal(await count(table), 1, table);
  assert.equal((await db.query("SELECT sessions FROM fx_market_signals")).rows[0].sessions[0], "london");
  assert.equal((await db.query("SELECT reason FROM fx_market_suppressed")).rows[0].reason, "news_blackout");
  assert.equal((await db.query("SELECT geometry_version FROM fx_market_setups")).rows[0].geometry_version, "fx-atr-v1");
  assert.equal((await db.query("SELECT type FROM fx_market_events")).rows[0].type, "swap");
  assert.equal(await count("forward_market_stages"), 1);

  assert.equal(await fxStage(FX2, 3 * HOUR), FX2);
  await assert.rejects(fxStage(FX3, 2 * HOUR), sqlstate("40001"));
  const wrong = { ...records(FX1), signals: [], suppressed: [], setups: [], outcomes: [] };
  await assert.rejects(fxStage(FX3, 4 * HOUR, wrong), /Event stage mismatch/);
  assert.equal(await count("fx_market_stages"), 2);  // the failed commit left nothing behind
});

test("ensure_fx_account: refuses non-entitled users; a forex account sits next to the crypto one", async () => {
  await assert.rejects(db.query("SELECT ensure_fx_account($1,0)", [B]), sqlstate("42501"));
  assert.equal(await count("forward_wallet_accounts"), 0);
  await grant(A, { plan: "forex" });
  await accounts(A);
  await accounts(A);  // idempotent
  const rows = (await db.query("SELECT market,account_id FROM forward_wallet_accounts WHERE user_id=$1 ORDER BY market",
    [A])).rows;
  assert.deepEqual(rows, [{ market: "crypto", account_id: A }, { market: "forex", account_id: `${A}:forex` }]);
});

test("commit_fx_user_run: own runs, cursor and ledger; the crypto wallet of the same user is unaffected", async () => {
  await grant(A, { role: "owner" });
  await accounts(A);
  await cryptoStage();
  await fxStage(FX1, HOUR);
  const first = await fxCommit(A, FX1, [line(1, "funding", -3, 997, { kind: "swap" })]);
  assert.equal(await fxCommit(A, FX1, [line(1, "funding", -3, 997)]), first);
  const row = (await db.query("SELECT * FROM paper_runs WHERE id=$1", [first])).rows[0];
  assert.equal(row.market, "forex");
  assert.equal(row.fx_stage_run_id, FX1);
  assert.equal(row.stage_run_id, null);
  assert.equal(row.run_key, `fx-hour:${HOUR}`);
  assert.equal(row.paper_account, `${A}:forex`);

  await fxStage(FX2, 2 * HOUR);
  await fxStage(FX3, 3 * HOUR);
  await assert.rejects(fxCommit(A, FX2, [], null), sqlstate("40001"));
  await assert.rejects(fxCommit(A, FX3, [], first), /Wallet stages must be consumed in order/);
  await assert.rejects(db.query(`INSERT INTO paper_runs(user_id,paper_account,run_key,trigger,status,boundary_ms,market,stage_run_id)
    VALUES($1,$2,'bad','hourly','ok',$3,'forex',$4)`, [A, `${A}:forex`, 2 * HOUR, CRYPTO]), /paper_runs_market_stage/);

  const crypto = await cryptoCommit(A, CRYPTO, [{ ...line(1, "funding", -5, 995), symbol: "BTCUSDT" }]);
  assert.notEqual(crypto, first);
  const ledgers = (await db.query("SELECT paper_account,seq FROM paper_ledger WHERE user_id=$1 ORDER BY paper_account",
    [A])).rows.map((r) => [r.paper_account, Number(r.seq)]);
  assert.deepEqual(ledgers, [[A, 1], [`${A}:forex`, 1]]);
  assert.ok(await fxCommit(A, FX2, [], first));  // the forex cursor still advances in order
});

test("reports: paper_account_summary and paper_equity_series never mix markets; forex needs entitlement", async () => {
  for (const user of [A, B]) await grant(user, { role: "owner" });
  for (const user of [A, B]) await accounts(user);
  await cryptoStage();
  await fxStage(FX1, HOUR);
  const cryptoLines = [line(1, "open_fee", -1, 999, { setup_id: "s1", symbol: "BTCUSDT" }),
    line(2, "pnl", 11, 1010, { setup_id: "s1", symbol: "BTCUSDT", outcome: "T" })];
  for (const user of [A, B]) {
    await cryptoCommit(user, CRYPTO, cryptoLines);
    await fxCommit(user, FX1, [line(1, "funding", -3, 997, { kind: "swap" })]);
  }
  await db.query("DELETE FROM user_access WHERE user_id=$1", [B]);  // B keeps rows but loses forex

  await asUser(A);
  const summary = async (args = "") => (await db.query(`SELECT lines::int,trades::int FROM paper_account_summary(${args})`)).rows[0];
  assert.deepEqual(await summary(), { lines: 2, trades: 1 });
  assert.deepEqual(await summary("'forex'"), { lines: 1, trades: 0 });
  const balances = async (args = "") =>
    (await db.query(`SELECT balance_e8 FROM paper_equity_series(${args})`)).rows.map((r) => Number(r.balance_e8));
  assert.deepEqual(await balances(), [1010]);
  assert.deepEqual(await balances(`${HOUR},NULL,'forex'`), [997]);

  await asUser(B);
  assert.deepEqual(await summary(), { lines: 2, trades: 1 });
  assert.deepEqual(await summary("'forex'"), { lines: 0, trades: 0 });
  assert.deepEqual(await balances(`${HOUR},NULL,'forex'`), []);
});
