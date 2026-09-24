import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { after, before, beforeEach, test } from "node:test";
import { PGlite } from "@electric-sql/pglite";

const db = new PGlite();
const user = "00000000-0000-0000-0000-000000000001";
const firstOwner = "00000000-0000-0000-0000-000000000002";
const secondOwner = "00000000-0000-0000-0000-000000000003";

before(async () => {
  await db.exec(`
    CREATE ROLE anon;
    CREATE ROLE authenticated;
    CREATE ROLE service_role BYPASSRLS;
    CREATE SCHEMA auth;
    CREATE TABLE auth.users (id UUID PRIMARY KEY);
    GRANT USAGE ON SCHEMA public, auth TO anon, authenticated, service_role;
    INSERT INTO auth.users VALUES ('${user}');
  `);
  await db.exec(
    await readFile(
      new URL("../supabase/migrations/20260926090000_monitor_run_leases.sql", import.meta.url),
      "utf8",
    ),
  );
});

beforeEach(() => db.exec("TRUNCATE monitor_run_leases"));
after(() => db.close());

async function claim(owner) {
  const { rows } = await db.query("SELECT claim_monitor_run_lease($1,$2,180) AS claimed", [
    user,
    owner,
  ]);
  return rows[0].claimed;
}

test("concurrent monitor attempts have one per-user owner", async () => {
  const claims = await Promise.all([claim(firstOwner), claim(secondOwner)]);
  assert.deepEqual(claims.sort(), [false, true]);
  const { rows } = await db.query("SELECT owner_id FROM monitor_run_leases WHERE user_id=$1", [
    user,
  ]);
  assert.equal(rows.length, 1);
  assert.ok([firstOwner, secondOwner].includes(rows[0].owner_id));
});

test("an expired lease is reclaimed and only its owner can release it", async () => {
  assert.equal(await claim(firstOwner), true);
  await db.exec(`UPDATE monitor_run_leases SET
    acquired_at=clock_timestamp()-interval '181 seconds',
    leased_until=clock_timestamp()-interval '1 second'`);
  assert.equal(await claim(secondOwner), true);

  await db.query("SELECT release_monitor_run_lease($1,$2)", [user, firstOwner]);
  assert.equal((await db.query("SELECT count(*) FROM monitor_run_leases")).rows[0].count, 1);
  await db.query("SELECT release_monitor_run_lease($1,$2)", [user, secondOwner]);
  assert.equal((await db.query("SELECT count(*) FROM monitor_run_leases")).rows[0].count, 0);
});
