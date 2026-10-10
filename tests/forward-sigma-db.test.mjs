import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { before, after, beforeEach, test } from "node:test";
import { PGlite } from "@electric-sql/pglite";

const db = new PGlite();
const USER = "11111111-1111-4111-8111-111111111111";
const checksum = "a".repeat(64);
const seed = { symbol: "BTCUSDT", sigma_version: "forward-sigma-v1", as_of_ms: 3540000, payload: {}, checksum };
const row = { run_key: "hour:3600000", trigger: "hourly", status: "ok", boundary_ms: 3600000 };
const empty = { signals: [], setups: [], outcomes: [], ledger: [] };

before(async () => {
  await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
    CREATE SCHEMA auth; CREATE TABLE auth.users(id UUID PRIMARY KEY);
    CREATE FUNCTION auth.uid() RETURNS UUID LANGUAGE SQL AS 'SELECT NULL::uuid';
    GRANT USAGE ON SCHEMA public,auth TO anon,authenticated,service_role;
    INSERT INTO auth.users VALUES ('${USER}');`);
  for (const file of ["20261009120000_forward_harness.sql", "20261010121000_forward_retryable_runs.sql",
                      "20261011130000_forward_sigma_state.sql"])
    await db.exec(await readFile(new URL(`../supabase/migrations/${file}`, import.meta.url), "utf8"));
});
beforeEach(() => db.exec("RESET ROLE; TRUNCATE paper_runs,forward_signals,forward_setups,forward_outcomes,paper_ledger,forward_sigma_state CASCADE"));
after(() => db.close());

async function commit(run = row, records = empty, states = [seed], expected = { BTCUSDT: null }) {
  return (await db.query("SELECT commit_forward_sigma_run($1,$2,$3,$4,$5,NULL) AS result",
    [USER, JSON.stringify(run), JSON.stringify(records), JSON.stringify(states), JSON.stringify(expected)])).rows[0].result;
}

test("run and sigma state commit once; replay does not touch updated_at", async () => {
  await db.exec("SET ROLE service_role");
  await commit();
  const before = (await db.query("SELECT * FROM forward_sigma_state")).rows;
  assert.equal((await commit()).already_done, 1);
  assert.deepEqual((await db.query("SELECT * FROM forward_sigma_state")).rows, before);
  assert.equal((await db.query("SELECT count(*)::int AS n FROM paper_runs")).rows[0].n, 1);
});

test("failed output insertion rolls back both run and checkpoint", async () => {
  await assert.rejects(commit(row, { ...empty, signals: [{ signal_id: "invalid" }] }));
  assert.equal((await db.query("SELECT count(*)::int AS n FROM paper_runs")).rows[0].n, 0);
  assert.equal((await db.query("SELECT count(*)::int AS n FROM forward_sigma_state")).rows[0].n, 0);
});

test("retryable runs cannot advance state; corrupt state can be replaced under its expected checksum", async () => {
  await assert.rejects(commit({ ...row, status: "no_sigma" }));
  await commit({ ...row, status: "no_sigma" }, empty, []);
  await db.query("SELECT seed_forward_sigma_state($1)", [JSON.stringify({ ...seed, checksum: "0".repeat(64) })]);
  await assert.rejects(commit()); // concurrent/checkpoint mismatch
  await commit(row, empty, [seed], { BTCUSDT: "0".repeat(64) });
  assert.equal((await db.query("SELECT checksum FROM forward_sigma_state")).rows[0].checksum, checksum);
});

test("seed import is idempotent and refuses overwriting a live checkpoint", async () => {
  await db.query("SELECT seed_forward_sigma_state($1)", [JSON.stringify(seed)]);
  await db.query("SELECT seed_forward_sigma_state($1)", [JSON.stringify(seed)]);
  await assert.rejects(db.query("SELECT seed_forward_sigma_state($1)", [JSON.stringify({ ...seed, checksum: "b".repeat(64) })]));
  await db.exec("SET ROLE authenticated");
  await assert.rejects(db.query("SELECT * FROM forward_sigma_state"));
});
