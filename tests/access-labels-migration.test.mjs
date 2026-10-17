import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { before, after, test } from "node:test";
import { PGlite } from "@electric-sql/pglite";

// #411: the access-labels migration locks every account (no backfill) and leaves only crypto/forex/all.
const db = new PGlite();
const A = "11111111-1111-4111-8111-111111111111";
const B = "22222222-2222-4222-8222-222222222222";
const sql = (file) => readFile(new URL(`../supabase/migrations/${file}`, import.meta.url), "utf8");
const markets = async (user) => (await db.query("SELECT markets_of($1) AS m", [user])).rows[0].m;

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
    "20261018100000_forex_access_and_market_tables.sql",
    "20261018110000_commit_user_run_market.sql", "20261019090000_fx_stage_defaults_and_rls.sql"])
    await db.exec(await sql(file));
  // Old shape: an owner role and a pro plan.
  await db.query("INSERT INTO user_access(user_id,role) VALUES($1,'owner')", [A]);
  await db.query("INSERT INTO user_access(user_id,plan) VALUES($1,'pro')", [B]);
  assert.deepEqual(await markets(A), ["crypto", "forex"]);
  await db.exec(await sql("20261019100000_access_labels.sql"));
});
after(() => db.close());

test("every account is locked, roles/extras and free/pro are gone, crypto/forex/all remain", async () => {
  assert.equal((await db.query("SELECT count(*)::int AS n FROM user_access")).rows[0].n, 0);
  assert.deepEqual(await markets(A), []);
  assert.deepEqual(await markets(B), []);
  const columns = (await db.query(`SELECT column_name FROM information_schema.columns
    WHERE table_schema='public' AND table_name='user_access'`)).rows.map((r) => r.column_name);
  assert.ok(!columns.includes("role") && !columns.includes("extra_markets"), columns.join(","));
  const plans = (await db.query(`SELECT p.plan, array_agg(m.market ORDER BY m.market) AS markets
    FROM plans p JOIN plan_markets m USING (plan) GROUP BY p.plan ORDER BY p.plan`)).rows;
  assert.deepEqual(plans, [{ plan: "all", markets: ["crypto", "forex"] }, { plan: "crypto", markets: ["crypto"] },
    { plan: "forex", markets: ["forex"] }]);
  assert.equal((await db.query("SELECT count(*)::int AS n FROM plans WHERE plan IN ('free','pro')")).rows[0].n, 0);
  await assert.rejects(db.query("INSERT INTO user_access(user_id) VALUES($1)", [A]), /null value/);
  await db.query("INSERT INTO user_access(user_id,plan) VALUES($1,'all')", [A]);
  assert.deepEqual(await markets(A), ["crypto", "forex"]);
});
