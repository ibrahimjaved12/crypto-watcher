import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { after, before, beforeEach, test } from "node:test";
import { PGlite } from "@electric-sql/pglite";

const db = new PGlite();
const USER = "11111111-1111-4111-8111-111111111111";
const OTHER = "22222222-2222-4222-8222-222222222222";
const DAY = 86400000;
const LAST = Math.floor(Date.now() / DAY) * DAY - DAY;
const hash = "a".repeat(64);

before(async () => {
  await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
    CREATE SCHEMA auth; CREATE TABLE auth.users(id UUID PRIMARY KEY);
    CREATE FUNCTION auth.uid() RETURNS UUID LANGUAGE SQL AS
      'SELECT nullif(current_setting(''request.jwt.claim.sub'',true),'''')::uuid';
    GRANT USAGE ON SCHEMA public,auth TO anon,authenticated,service_role;
    INSERT INTO auth.users VALUES ('${USER}'),('${OTHER}');`);
  for (const path of [
    "20261009120000_forward_harness.sql",
    "20261010090000_forward_trend_track.sql",
  ])
    await db.exec(
      await readFile(new URL(`../supabase/migrations/${path}`, import.meta.url), "utf8"),
    );
});
beforeEach(async () => {
  await db.exec(
    "RESET ROLE; TRUNCATE forward_trend_state,forward_trend_ledger,forward_trend_weights CASCADE;",
  );
});
after(() => db.close());

function sample(n) {
  return { n_days: n };
}
function response() {
  const decision = {
    track: "x",
    day_ms: LAST,
    decided_from_close_ms: LAST - DAY,
    decided_at_ms: Date.now() - 10000,
    weights: { BTCUSDT: 0.5 },
    defined: { BTCUSDT: true },
  };
  const row = {
    track: "x",
    day_ms: LAST,
    daily_ppm: 100,
    equity_ppm: 1000100,
    sample_kind: "retrospective",
    sample_equity_ppm: 1000100,
    turnover_ppm: 0,
    gross_ppm: 500000,
    symbols_active: 1,
    weights: { BTCUSDT: 0.5 },
  };
  return {
    schema_version: 1,
    versions: { trend_track: "trend-track-v2" },
    params_hash: hash,
    symbols: ["BTCUSDT"],
    history_start_ms: Date.UTC(2020, 0, 1),
    track_start_ms: LAST,
    through_day_ms: LAST,
    weights: [decision],
    ledger: [row],
    states: {
      x: {
        last_day_ms: LAST,
        n_days: 1,
        samples: { prospective: sample(0), retrospective: sample(1) },
      },
    },
    tracks: [{ name: "x", kind: "variant", control: null, config_hash: hash }],
    funding_unavailable: [],
    assumptions: [],
  };
}
async function commit(value, prior = null, key = "first", user = USER) {
  const row = {
    run_key: key,
    trigger: "daily",
    status: "ok",
    last_complete_day_ms: LAST,
    counts: { bars_inserted: 0 },
  };
  return (
    await db.query("SELECT commit_forward_trend_run($1,$2,$3,$4) AS counts", [
      user,
      prior,
      JSON.stringify(value),
      JSON.stringify(row),
    ])
  ).rows[0].counts;
}
async function latest() {
  return (
    (
      await db.query(
        "SELECT id FROM forward_trend_state WHERE committed ORDER BY revision DESC LIMIT 1",
      )
    ).rows[0]?.id ?? null
  );
}
async function counts() {
  return (
    await db.query(`SELECT
  (SELECT count(*)::int FROM forward_trend_weights) AS weights,
  (SELECT count(*)::int FROM forward_trend_ledger) AS ledger,
  (SELECT count(*)::int FROM forward_trend_state WHERE committed) AS states`)
  ).rows[0];
}

test("transaction rolls back every write when a ledger payload or checkpoint fails", async () => {
  const value = response();
  value.ledger[0].weights.BTCUSDT = 0.7;
  await assert.rejects(() => commit(value), /Ledger does not match/);
  assert.deepEqual(await counts(), { weights: 0, ledger: 0, states: 0 });
  value.ledger[0].weights.BTCUSDT = 0.5;
  value.states.x.n_days = 2;
  await assert.rejects(() => commit(value), /state does not match/);
  assert.deepEqual(await counts(), { weights: 0, ledger: 0, states: 0 });
});

test("a slower run using an obsolete prior snapshot cannot supersede a committed run", async () => {
  const value = response();
  assert.deepEqual(await commit(value), { bars_inserted: 0, weights: 1, ledger: 1 });
  const prior = await latest();
  await assert.rejects(() => commit(value, null, "slow"), /state changed/);
  assert.equal(await latest(), prior);
  assert.deepEqual(await counts(), { weights: 1, ledger: 1, states: 1 });
});

test("identical duplicates are verified; conflicting decisions and ledgers abort atomically", async () => {
  const value = response();
  await commit(value);
  assert.deepEqual(await commit(value, await latest(), "repeat"), {
    bars_inserted: 0,
    weights: 0,
    ledger: 0,
  });
  const prior = await latest();
  const conflict = structuredClone(value);
  conflict.weights[0].weights.BTCUSDT = 0.7;
  await assert.rejects(
    () => commit(conflict, prior, "bad-decision"),
    /Conflicting immutable trend decision/,
  );
  const ledger = structuredClone(value);
  ledger.ledger[0].daily_ppm = 101;
  await assert.rejects(
    () => commit(ledger, prior, "bad-ledger"),
    /Conflicting immutable trend ledger/,
  );
  assert.equal(await latest(), prior);
  assert.deepEqual(await counts(), { weights: 1, ledger: 1, states: 2 });
});

test("resume rejects a changed parameter hash or frozen universe", async () => {
  const value = response();
  await commit(value);
  const prior = await latest();
  const changed = structuredClone(value);
  changed.params_hash = "b".repeat(64);
  await assert.rejects(() => commit(changed, prior, "changed-hash"), /configuration identity/);
  changed.params_hash = hash;
  changed.symbols.push("ETHUSDT");
  await assert.rejects(() => commit(changed, prior, "changed-universe"), /configuration identity/);
});

test("recording timestamps classify backfill as reconstruction and preserve first recording", async () => {
  const value = response();
  await commit(value);
  const first = (
    await db.query("SELECT decided_at_ms,recorded_at_ms,sample_kind FROM forward_trend_weights")
  ).rows[0];
  assert.equal(first.sample_kind, "retrospective");
  assert.ok(first.recorded_at_ms >= first.decided_at_ms);
  await commit(value, await latest(), "repeat");
  assert.deepEqual(
    (await db.query("SELECT decided_at_ms,recorded_at_ms,sample_kind FROM forward_trend_weights"))
      .rows[0],
    first,
  );
});

test("an actually recorded decision for the running day is prospective before its close", async () => {
  const value = response();
  value.weights[0].day_ms = LAST + DAY;
  value.weights[0].decided_from_close_ms = LAST;
  value.weights[0].decided_at_ms = Date.now() - 1000;
  value.ledger = [];
  value.through_day_ms = null;
  value.states.x = {
    last_day_ms: null,
    n_days: 0,
    samples: { prospective: sample(0), retrospective: sample(0) },
  };
  assert.deepEqual(await commit(value), { bars_inserted: 0, weights: 1, ledger: 0 });
  const row = (
    await db.query("SELECT sample_kind,day_ms,recorded_at_ms FROM forward_trend_weights")
  ).rows[0];
  assert.equal(row.sample_kind, "prospective");
  assert.ok(row.recorded_at_ms < row.day_ms + DAY);
});

test("a later startup diagnostic cannot become a checkpoint", async () => {
  const value = response();
  await commit(value);
  const prior = await latest();
  await db.query(
    `INSERT INTO forward_trend_state(user_id,run_key,trigger,status,reason,last_complete_day_ms,
    history_start_ms,track_start_ms,symbols,states) VALUES ($1,'diagnostic','daily','partial','startup failed',$2,$3,$2,$4,'{}')`,
    [USER, LAST, Date.UTC(2020, 0, 1), JSON.stringify(["BTCUSDT"])],
  );
  assert.equal(await latest(), prior);
  assert.deepEqual(await commit(value, prior, "after-diagnostic"), {
    bars_inserted: 0,
    weights: 0,
    ledger: 0,
  });
});

test("RLS exposes only the owner's rows; clients cannot invoke the commit RPC", async () => {
  await commit(response());
  await commit(response(), null, "other", OTHER);
  await db.exec("SET ROLE authenticated;");
  await db.query("SELECT set_config('request.jwt.claim.sub',$1,false)", [USER]);
  assert.equal(
    (await db.query("SELECT count(*)::int AS n FROM forward_trend_ledger")).rows[0].n,
    1,
  );
  await assert.rejects(() => commit(response(), null, "forbidden"), /permission denied/);
  await db.exec("RESET ROLE;");
});

test("the service role can commit the fresh schema without extra sequence grants", async () => {
  await db.exec("SET ROLE service_role;");
  assert.deepEqual(await commit(response()), { bars_inserted: 0, weights: 1, ledger: 1 });
  await db.exec("RESET ROLE;");
});
