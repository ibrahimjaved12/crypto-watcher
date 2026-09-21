import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { after, before, beforeEach, test } from "node:test";
import { PGlite } from "@electric-sql/pglite";

// Local, in-memory PostgreSQL only. Never reads Supabase environment variables.
const db = new PGlite();
const user = "00000000-0000-0000-0000-000000000001";
const other = "00000000-0000-0000-0000-000000000002";
let start;
before(async () => {
  await db.exec(`
    CREATE ROLE anon;
    CREATE ROLE authenticated;
    CREATE ROLE service_role BYPASSRLS;
    CREATE SCHEMA auth;
    CREATE TABLE auth.users (id uuid PRIMARY KEY);
    CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql AS
      'SELECT nullif(current_setting(''request.jwt.claim.sub'', true), '''')::uuid';
    GRANT USAGE ON SCHEMA public, auth TO anon, authenticated, service_role;
  `);
  for (const name of [
    "20260913170352_3d6925ba-68c4-4528-aea1-da9439eb127e.sql",
    "20260915090000_cumulative_monitor.sql",
    "20260921090000_activity_domains.sql",
  ]) {
    await db.exec(
      await readFile(new URL(`../supabase/migrations/${name}`, import.meta.url), "utf8"),
    );
  }
});
beforeEach(async () => {
  await db.exec("RESET ROLE; DELETE FROM auth.users;");
  await db.query("INSERT INTO auth.users VALUES ($1), ($2)", [user, other]);
  await db.query(
    "INSERT INTO public.watchlist_items(user_id,symbol) VALUES ($1,'BTCUSDT'),($2,'BTCUSDT')",
    [user, other],
  );
  start = Math.floor(Date.now() / 60_000) * 60_000 - 9 * 60_000;
});
after(() => db.close());

async function observe(price, step, source = "Binance", uid = user) {
  const { rows } = await db.query(
    "SELECT public.process_cumulative_observation($1,'BTCUSDT',$2,$3,$4) AS result",
    [uid, price, new Date(start + step * 60_000).toISOString(), source],
  );
  return rows[0].result;
}
async function checkpoint(price, step, source = "Binance", uid = user) {
  const { rows } = await db.query(
    "SELECT public.record_market_data_checkpoint($1,'BTCUSDT',$2,$3,$4) AS result",
    [uid, price, new Date(start + step * 60_000).toISOString(), source],
  );
  return rows[0].result;
}
async function state() {
  return (await db.query("SELECT * FROM monitor_baselines WHERE user_id=$1", [user])).rows[0];
}

test("initializes without an alert; gradual net gains reach exact 2%", async () => {
  assert.equal((await observe("100", 0)).status, "initialized");
  // The baseline may be older than the historical candle response / 15m window.
  await db.query("UPDATE monitor_baselines SET baseline_at=baseline_at-interval '1 day'");
  for (const [step, price] of [
    [1, "101"],
    [2, "99"],
    [3, "101.99"],
  ]) {
    assert.equal((await observe(price, step)).status, "below_threshold");
  }
  assert.equal((await observe("102", 4)).status, "alerted");
  const alert = (await db.query("SELECT * FROM alerts")).rows[0];
  assert.equal(Number(alert.change_pct), 2);
  assert.equal(alert.comparison_mode, "baseline");
  assert.equal(alert.window_minutes, null);
  assert.equal(Number(alert.baseline_price), 100);
  assert.equal(Number((await state()).baseline_price), 102);
});

test("falling prices reach exact negative boundary", async () => {
  await observe("100", 0);
  assert.equal((await observe("98.000001", 1)).status, "below_threshold");
  const result = await observe("98", 2);
  assert.equal(result.status, "alerted");
  assert.equal(result.direction, "down");
  assert.equal(result.change_pct, -2);
});

test("repeated or out-of-order observations cannot duplicate alerts", async () => {
  await observe("100", 0);
  const responses = await Promise.all([observe("102", 1), observe("102", 1)]);
  assert.deepEqual(responses.map((r) => r.status).sort(), ["alerted", "already_processed"]);
  assert.equal((await observe("150", 0)).status, "already_processed");
  assert.equal((await db.query("SELECT count(*) FROM alerts")).rows[0].count, 1);
});

test("upward cooldown does not suppress a downward reversal", async () => {
  await observe("100", 0);
  await observe("102", 1);
  const result = await observe("99.96", 2);
  assert.equal(result.status, "alerted");
  assert.equal(result.direction, "down");
});

test("same-direction cooldown retains baseline and later evaluates retained gain", async () => {
  await observe("100", 0);
  await observe("102", 1);
  assert.equal((await observe("104.04", 2)).status, "cooldown");
  assert.equal(Number((await state()).baseline_price), 102);
  await db.exec(
    "UPDATE monitor_baselines SET last_up_alert_at=clock_timestamp()-interval '15 minutes'",
  );
  assert.equal((await observe("104.04", 3)).status, "alerted");
  assert.equal(Number((await state()).baseline_price), 104.04);
});

test("unchanged elevated price produces no repeated level alert", async () => {
  await observe("100", 0);
  await observe("102", 1);
  await db.exec(
    "UPDATE monitor_baselines SET last_up_alert_at=clock_timestamp()-interval '1 hour'",
  );
  assert.equal((await observe("102", 2)).status, "below_threshold");
});

test("provider and threshold changes reinitialize without comparing unlike baselines", async () => {
  await observe("100", 0);
  assert.equal((await observe("150", 1, "OKX")).status, "reinitialized");
  await db.query("INSERT INTO monitor_settings(user_id,threshold_pct) VALUES ($1,3)", [user]);
  assert.equal((await observe("200", 2, "OKX")).status, "reinitialized");
  assert.equal(Number((await state()).baseline_price), 200);
});

test("failed alert insertion rolls back baseline and observation advancement", async () => {
  await observe("100", 0);
  await db.exec(`CREATE FUNCTION reject_test_alert() RETURNS trigger LANGUAGE plpgsql AS
    $$ BEGIN RAISE EXCEPTION 'fixture insert failure'; END; $$;
    CREATE TRIGGER reject_test_alert BEFORE INSERT ON alerts FOR EACH ROW EXECUTE FUNCTION reject_test_alert();`);
  try {
    await assert.rejects(observe("102", 1), /fixture insert failure/);
    assert.equal(Number((await state()).baseline_price), 100);
    assert.equal(new Date((await state()).last_observed_at).getTime(), start);
  } finally {
    await db.exec("DROP TRIGGER reject_test_alert ON alerts; DROP FUNCTION reject_test_alert();");
  }
  assert.equal((await observe("102", 1)).status, "alerted");
});

test("pause, watchlist deletion and re-addition", async () => {
  await observe("100", 0);
  await db.query("INSERT INTO monitor_settings(user_id,monitoring_enabled) VALUES ($1,false)", [
    user,
  ]);
  assert.equal((await observe("102", 1)).status, "disabled");
  await db.query("DELETE FROM watchlist_items WHERE user_id=$1", [user]);
  assert.equal(await state(), undefined);
  assert.equal((await observe("102", 1)).status, "not_watched");
  await db.query("UPDATE monitor_settings SET monitoring_enabled=true WHERE user_id=$1", [user]);
  await db.query("INSERT INTO watchlist_items(user_id,symbol) VALUES ($1,'BTCUSDT')", [user]);
  assert.equal((await observe("102", 1)).status, "initialized");
});

test("movement and collection controls pause alerts without changing the baseline", async () => {
  await observe("100", 0);
  await db.query(
    "INSERT INTO monitor_settings(user_id,movement_alerts_enabled) VALUES ($1,false)",
    [user],
  );
  assert.equal((await observe("102", 1)).status, "disabled");
  assert.equal(Number((await state()).baseline_price), 100);

  await db.query(
    "UPDATE monitor_settings SET movement_alerts_enabled=true, market_data_collection_enabled=false WHERE user_id=$1",
    [user],
  );
  assert.equal((await observe("102", 2)).status, "disabled");
  assert.equal((await checkpoint("102", 2)).status, "disabled");
  assert.equal(Number((await state()).baseline_price), 100);

  await db.query(
    "UPDATE monitor_settings SET market_data_collection_enabled=true WHERE user_id=$1",
    [user],
  );
  assert.equal((await observe("102", 3)).status, "alerted");
});

test("market collection checkpoints advance independently and reject older observations", async () => {
  assert.equal((await checkpoint("100", 0)).status, "recorded");
  assert.equal((await checkpoint("102", 2, "OKX")).status, "recorded");
  assert.equal((await checkpoint("101", 1)).status, "already_processed");
  const row = (await db.query("SELECT * FROM market_data_checkpoints WHERE user_id=$1", [user]))
    .rows[0];
  assert.equal(Number(row.price), 102);
  assert.equal(row.data_source, "OKX");

  await db.query("DELETE FROM watchlist_items WHERE user_id=$1", [user]);
  assert.equal(
    (await db.query("SELECT count(*) FROM market_data_checkpoints WHERE user_id=$1", [user]))
      .rows[0].count,
    0,
  );
  assert.equal((await checkpoint("103", 3)).status, "not_watched");
});

test("current activities default on and future execution controls fail closed", async () => {
  await db.query("INSERT INTO monitor_settings(user_id) VALUES ($1)", [user]);
  const settings = (
    await db.query(
      `SELECT market_data_collection_enabled, completed_candle_ta_enabled,
        movement_alerts_enabled, developing_setup_evaluation_enabled,
        paper_trading_enabled FROM monitor_settings WHERE user_id=$1`,
      [user],
    )
  ).rows[0];
  assert.equal(settings.market_data_collection_enabled, true);
  assert.equal(settings.completed_candle_ta_enabled, true);
  assert.equal(settings.movement_alerts_enabled, true);
  assert.equal(settings.developing_setup_evaluation_enabled, false);
  assert.equal(settings.paper_trading_enabled, false);
});

test("notification delivery preferences are channel-specific and account-scoped", async () => {
  await db.query("SELECT set_config('request.jwt.claim.sub',$1,false)", [user]);
  await db.exec("SET ROLE authenticated");
  try {
    await db.query(
      "INSERT INTO notification_channel_preferences(user_id,channel,delivery_enabled) VALUES ($1,'email',true)",
      [user],
    );
    await assert.rejects(
      db.query(
        "INSERT INTO notification_channel_preferences(user_id,channel,delivery_enabled) VALUES ($1,'whatsapp',true)",
        [other],
      ),
      /row-level security/,
    );
    const rows = (await db.query("SELECT * FROM notification_channel_preferences")).rows;
    assert.equal(rows.length, 1);
    assert.equal(rows[0].channel, "email");
  } finally {
    await db.exec("RESET ROLE");
  }
});

test("invalid, future, stale observations and invalid settings fail closed", async () => {
  for (const price of ["0", "-1", "NaN", "Infinity"]) {
    await assert.rejects(observe(price, 0), /Invalid or stale/);
  }
  await assert.rejects(observe("100", -2), /Invalid or stale/);
  await assert.rejects(observe("100", 11), /Invalid or stale/);
  await db.query("INSERT INTO monitor_settings(user_id,threshold_pct) VALUES ($1,-2)", [user]);
  await assert.rejects(observe("100", 0), /Invalid monitor/);
  assert.equal(await state(), undefined);
});

test("state is account-scoped and authenticated users cannot write state or call RPC", async () => {
  await observe("100", 0);
  await observe("200", 0, "Binance", other);
  await checkpoint("100", 0);
  await checkpoint("200", 0, "Binance", other);
  await db.query("SELECT set_config('request.jwt.claim.sub',$1,false)", [user]);
  await db.exec("SET ROLE authenticated");
  try {
    const rows = (await db.query("SELECT * FROM monitor_baselines")).rows;
    assert.equal(rows.length, 1);
    assert.equal(rows[0].user_id, user);
    assert.equal((await db.query("SELECT * FROM market_data_checkpoints")).rows.length, 1);
    await assert.rejects(
      db.query("UPDATE monitor_baselines SET baseline_price=1"),
      /permission denied/,
    );
    await assert.rejects(observe("102", 1), /permission denied/);
    await assert.rejects(checkpoint("102", 1), /permission denied/);
  } finally {
    await db.exec("RESET ROLE");
  }
  await db.exec("SET ROLE service_role");
  try {
    assert.equal((await observe("102", 1)).status, "alerted");
  } finally {
    await db.exec("RESET ROLE");
  }
});
