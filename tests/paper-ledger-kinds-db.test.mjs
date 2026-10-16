import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { after, before, test } from "node:test";
import { PGlite } from "@electric-sql/pglite";

// #36 Part 2: the paper ledger accepts the wallet-v3 order-lifecycle kinds, still rejects anything else, and stays
// append-only. The migration finds the old CHECK by its definition, so it works on the real migration chain.
const db = new PGlite();
const USER = "11111111-1111-4111-8111-111111111111";
const RUN = "22222222-2222-4222-8222-222222222222";
const KINDS = ["open_fee", "pnl", "close_fee", "funding", "liquidation", "rejected", "order_submitted",
  "order_accepted", "order_rejected", "order_cancelled", "order_expired", "order_filled",
  "order_partially_filled", "order_ambiguous", "stop_moved", "target_moved"];
let seq = 0;

async function line(type) {
  seq += 1;
  return db.query(
    `INSERT INTO paper_ledger(user_id,seq,ms,type,setup_id,symbol,amount_e8,balance_e8,payload,run_id)
     VALUES($1,$2,0,$3,'s1','BTCUSDT',0,100,'{"order_id":"o1"}',$4)`,
    [USER, seq, type, RUN],
  );
}

before(async () => {
  await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
    CREATE SCHEMA auth; CREATE TABLE auth.users(id UUID PRIMARY KEY);
    CREATE FUNCTION auth.uid() RETURNS UUID LANGUAGE SQL AS
      $$SELECT nullif(current_setting('request.jwt.claim.sub',true),'')::uuid$$;
    GRANT USAGE ON SCHEMA public,auth TO anon,authenticated,service_role;
    INSERT INTO auth.users VALUES ('${USER}');`);
  for (const file of ["20261009120000_forward_harness.sql", "20261016180000_paper_ledger_order_kinds.sql"])
    await db.exec(await readFile(new URL(`../supabase/migrations/${file}`, import.meta.url), "utf8"));
  await db.query(
    `INSERT INTO paper_runs(id,user_id,run_key,trigger,status,boundary_ms) VALUES($1,$2,'hour:0','hourly','ok',0)`,
    [RUN, USER],
  );
});
after(() => db.close());

test("every wallet-v2 and wallet-v3 ledger kind is accepted, including the two reserved for #221", async () => {
  for (const type of KINDS) await line(type);
  const { rows } = await db.query("SELECT count(*)::int AS n FROM paper_ledger");
  assert.equal(rows[0].n, KINDS.length);
});

test("an unknown kind is still rejected by the check", async () => {
  await assert.rejects(line("order_teleported"), /paper_ledger_type_check|check constraint/);
});

test("the ledger stays append-only after the constraint was replaced", async () => {
  await assert.rejects(db.query("UPDATE paper_ledger SET amount_e8 = 1"), /append-only/);
  await assert.rejects(db.query("DELETE FROM paper_ledger"), /append-only/);
});

test("the constraint is defined once, by the migration, with exactly the listed kinds", async () => {
  const { rows } = await db.query(
    `SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint
     WHERE conrelid = 'public.paper_ledger'::regclass AND contype = 'c' AND pg_get_constraintdef(oid) LIKE '%open_fee%'`,
  );
  assert.equal(rows.length, 1);
  assert.equal(rows[0].conname, "paper_ledger_type_check");
  const listed = [...rows[0].def.matchAll(/'([a-z_]+)'/g)].map((match) => match[1]).sort();
  assert.deepEqual(listed, [...KINDS].sort());
});
