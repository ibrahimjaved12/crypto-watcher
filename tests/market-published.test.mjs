// Published movement state (#30): payload and read mapping, the site hook, the machine publisher, and the hosted
// migrations (market_state_published and latest_ta_signals) in PGlite. All numbers are SYNTHETIC.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { after, before, beforeEach, test } from "node:test";
import { PGlite } from "@electric-sql/pglite";

import { createModuleLoader } from "./module-loader.mjs";

const load = createModuleLoader(import.meta.url);
const published = await import(load("../src/lib/market/movement-published.ts"));
const publisher = await import(load("../src/lib/market/movement-publisher.server.ts"));
const stateLib = await import(load("../src/lib/market/market-movement-state.ts"));
const assess = await import(load("../src/lib/market/assessment.ts"));

const NOW = Date.parse("2026-10-16T12:00:00Z");
const UNIVERSE = "binance-usdm-public-market";
const SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"];

function payload(boundaryAgo = 10_000, over = {}) {
  return {
    universeId: UNIVERSE, universeVersion: "market-universe-v1:abc", evaluationBoundaryTime: NOW - boundaryAgo,
    directionState: "BROAD_DROP", pace: "ACCELERATING", activeEpisodeId: null, activeDirection: null, interrupted: false,
    episodeAlgorithmVersion: "e", lifecycleConfigVersion: "l", classifierAlgorithmVersion: "c", classifierConfigVersion: "cc",
    movementAlgorithmVersion: "m", movementConfigVersion: "market-movement-config-v1",
    currentEvidence: {
      primaryWindow: {}, windowsContext: [{ windowMinutes: 5, horizonRole: "PRIMARY", reversalCandidate: null, eligibleCount: 6 }],
      universe: { id: UNIVERSE, version: "v", configuredSymbols: SYMBOLS, includedSymbols: SYMBOLS, excludedSymbols: [] },
      engine: { status: "LIVE", lateAfterFinalizationCount: 0 },
      timestamps: { evaluationBoundaryTime: NOW - boundaryAgo, lastSourceEventTime: NOW - 6000, lastTradeTime: NOW - 6000, lastReceivedAt: NOW - 4000, engineUpdatedAt: NOW - 3000 },
      finalizationConfigVersion: "f", finalizationGraceMs: 2000, persistenceConfigVersion: "p", currentSnapshotCadenceMs: 30000, mostRecentTransition: null,
    },
    ...over,
  };
}

// ---------------------------------------------------------------- payload and read mapping

test("the published payload is the mapper's input without lifecycle internals", () => {
  const full = { ...payload(), lifecycleState: { big: "x".repeat(1000) }, updatedAt: "2026-10-16T12:00:00Z" };
  const out = published.toPublishedPayload(full);
  assert.equal("lifecycleState" in out, false);
  assert.equal("updatedAt" in out, false);
  assert.equal(out.currentEvidence, full.currentEvidence);
  assert.deepEqual(Object.keys(out).sort(), Object.keys(payload()).sort());
});

test("hosted read: live copy, stale evaluation, no row and a malformed row are different results", () => {
  const live = published.readPublishedState({ payload: payload(10_000), published_at: new Date(NOW - 5000).toISOString() }, NOW);
  assert.deepEqual([live.readStatus, live.status, live.available, live.publishedAt], ["published", "LIVE", true, NOW - 5000]);
  const stale = published.readPublishedState({ payload: payload(5 * 60_000), published_at: new Date(NOW - 5000).toISOString() }, NOW);
  assert.deepEqual([stale.readStatus, stale.status], ["published", "STALE"]); // the engine's own rule on the evaluation time
  const none = published.readPublishedState(null, NOW);
  assert.deepEqual([none.readStatus, none.status, none.available, none.publishedAt], ["published_none", "UNAVAILABLE", false, null]);
  assert.equal(published.readPublishedState({ payload: { nonsense: true }, published_at: new Date(NOW).toISOString() }, NOW).readStatus, "read_failed");
  assert.equal(published.readPublishedState({ payload: payload(), published_at: "not a date" }, NOW).readStatus, "read_failed");
});

test("the operational path's mapping is unchanged: the published read maps through the same function", () => {
  const direct = stateLib.toMarketMovementCurrentState(payload(), { now: NOW });
  const { readStatus, publishedAt, ...copy } = published.readPublishedState({ payload: payload(), published_at: new Date(NOW).toISOString() }, NOW);
  assert.deepEqual(copy, direct);
  assert.equal([readStatus, publishedAt].length, 2);
  assert.equal(stateLib.toMarketMovementCurrentState(null, { now: NOW }).status, "UNAVAILABLE");
});

test("end to end: a published copy of an offline machine is never shown as live", () => {
  const row = { payload: payload(10_000), published_at: new Date(NOW - 10 * 60_000).toISOString() };
  const read = published.readPublishedState(row, NOW);
  assert.equal(read.status, "LIVE"); // read at NOW the evaluation was only 10 s old...
  // ...but the page later renders it 10 minutes on: the machine stopped reporting, so it is offline, never live.
  const strip = assess.buildMarketStrip(read, NOW + 10 * 60_000, read.readStatus, read.publishedAt);
  assert.deepEqual([strip.tone, strip.current, strip.machineOffline], ["offline", false, true]);
});

// ---------------------------------------------------------------- the site hook

test("hook input is validated: universe, envelope against payload, size, ops", () => {
  const good = { op: "publish", universeId: UNIVERSE, evaluationBoundaryMs: NOW - 10_000, payload: payload() };
  assert.equal(published.parseMovementHookInput(good).op, "publish");
  assert.deepEqual(published.parseMovementHookInput({ op: "heartbeat", universeId: UNIVERSE }), { op: "heartbeat", universeId: UNIVERSE });
  assert.equal(published.parseMovementHookInput({ ...good, universeId: "other" }), null);
  assert.equal(published.parseMovementHookInput({ ...good, evaluationBoundaryMs: NOW }), null); // envelope != payload
  assert.equal(published.parseMovementHookInput({ ...good, payload: { ...payload(), universeId: "other" } }), null);
  assert.equal(published.parseMovementHookInput({ ...good, payload: { nope: 1 } }), null);
  assert.equal(published.parseMovementHookInput({ ...good, evaluationBoundaryMs: -5 }), null);
  assert.equal(published.parseMovementHookInput({ op: "delete", universeId: UNIVERSE }), null);
  assert.equal(published.parseMovementHookInput(null), null);
  const huge = payload(10_000, { padding: "x".repeat(published.PUBLISHED_PAYLOAD_MAX_BYTES) });
  assert.equal(published.parseMovementHookInput({ ...good, payload: huge }), null);
});

test("the hook handler calls the right function and reports stored / touched without throwing", async () => {
  const calls = [];
  const deps = (data = true, error = null) => ({ rpc: async (name, args) => { calls.push([name, args]); return { data, error }; } });
  const pub = await published.handleMovementHook(deps(), { op: "publish", universeId: UNIVERSE, evaluationBoundaryMs: NOW - 10_000, payload: payload() });
  assert.deepEqual([pub.status, pub.body], [200, { ok: true, stored: true }]);
  assert.equal(calls[0][0], "publish_market_state");
  assert.equal(calls[0][1].p_boundary_ms, NOW - 10_000);
  const hb = await published.handleMovementHook(deps(false), { op: "heartbeat", universeId: UNIVERSE });
  assert.deepEqual([hb.status, hb.body], [200, { ok: true, touched: false }]);
  assert.equal(calls[1][0], "touch_market_state");
  const failed = await published.handleMovementHook(deps(null, { message: "db down" }), { op: "heartbeat", universeId: UNIVERSE });
  assert.deepEqual([failed.status, failed.body.ok], [500, false]);
  assert.ok(!JSON.stringify(failed.body).includes("db down")); // no internals
  const thrown = await published.handleMovementHook({ rpc: async () => { throw new Error("boom"); } }, { op: "heartbeat", universeId: UNIVERSE });
  assert.equal(thrown.status, 500);
});

// ---------------------------------------------------------------- the machine publisher

function publisherHarness(over = {}) {
  const sent = [];
  let clock = NOW;
  let current = { ...payload(10_000), lifecycleState: { x: 1 } };
  const deps = {
    read: async () => { if (over.readError) throw new Error("operational down"); return over.empty ? null : current; },
    send: async (body) => { sent.push(body); return over.reply ? over.reply(body) : { status: 200, json: { ok: true, stored: true, touched: true } }; },
    now: () => clock,
  };
  return { deps, sent, tick: (ms) => { clock += ms; }, set: (next) => { current = next; } };
}

test("publisher: publishes a changed evaluation, heartbeats an unchanged one, at most once per 30 s", async () => {
  const h = publisherHarness();
  const state = publisher.newPublisherState();
  assert.equal((await publisher.publishMovementOnce(h.deps, state)).kind, "published");
  assert.equal(h.sent[0].op, "publish");
  assert.equal("lifecycleState" in h.sent[0].payload, false);
  assert.equal(h.sent[0].evaluationBoundaryMs, NOW - 10_000);
  h.tick(10_000);
  assert.equal((await publisher.publishMovementOnce(h.deps, state)).kind, "too_soon"); // inside the interval
  assert.equal(h.sent.length, 1);
  h.tick(25_000); // 35 s since the last attempt, evaluation unchanged
  assert.equal((await publisher.publishMovementOnce(h.deps, state)).kind, "heartbeat");
  assert.deepEqual(h.sent[1], { op: "heartbeat", universeId: UNIVERSE }); // the payload is skipped
  h.tick(31_000);
  h.set({ ...payload(5_000) }); // the engine moved on
  assert.equal((await publisher.publishMovementOnce(h.deps, state)).kind, "published");
  assert.equal(h.sent[2].evaluationBoundaryMs, NOW - 5_000);
});

test("publisher: failures never throw, are retried in full, and a vanished hosted row is republished", async () => {
  let fail = true;
  const h = publisherHarness({ reply: () => (fail ? { status: 503, json: {} } : { status: 200, json: { ok: true, touched: true } }) });
  const state = publisher.newPublisherState();
  const first = await publisher.publishMovementOnce(h.deps, state);
  assert.deepEqual([first.kind, first.error], ["failed", "site answered 503"]);
  assert.equal(state.lastBoundary, null); // not remembered: the next pass sends the payload again
  h.tick(31_000);
  fail = false;
  assert.equal((await publisher.publishMovementOnce(h.deps, state)).kind, "published");
  h.tick(31_000);
  const gone = publisherHarness({ reply: (body) => ({ status: 200, json: body.op === "heartbeat" ? { ok: true, touched: false } : { ok: true, stored: true } }) });
  const s2 = publisher.newPublisherState();
  await publisher.publishMovementOnce(gone.deps, s2);
  gone.tick(31_000);
  assert.equal((await publisher.publishMovementOnce(gone.deps, s2)).kind, "heartbeat");
  assert.equal(s2.lastBoundary, null);
  gone.tick(31_000);
  assert.equal((await publisher.publishMovementOnce(gone.deps, s2)).kind, "published");
  assert.equal((await publisher.publishMovementOnce(publisherHarness({ empty: true }).deps, publisher.newPublisherState())).kind, "no_state");
  const read = await publisher.publishMovementOnce(publisherHarness({ readError: true }).deps, publisher.newPublisherState());
  assert.deepEqual([read.kind, read.error], ["failed", "operational down"]);
  const offline = await publisher.publishMovementOnce({ read: async () => payload(), send: async () => { throw new Error("fetch failed"); } }, publisher.newPublisherState());
  assert.equal(offline.kind, "failed");
});

test("publisher config: its own URL, else the alert-chart one, needs the shared secret, can be turned off", () => {
  const secret = "s".repeat(40);
  assert.equal(publisher.movementPublisherConfig({}), null);
  assert.deepEqual(publisher.movementPublisherConfig({ ALERT_CHART_HOSTED_URL: "https://site.example", RUNNER_GATEWAY_SECRET: secret }), { origin: "https://site.example", secret });
  assert.equal(publisher.movementPublisherConfig({ MOVEMENT_STATE_HOSTED_URL: "https://a.example", ALERT_CHART_HOSTED_URL: "https://b.example", RUNNER_GATEWAY_SECRET: secret }).origin, "https://a.example");
  assert.equal(publisher.movementPublisherConfig({ MOVEMENT_STATE_PUBLISH_ENABLED: "false", ALERT_CHART_HOSTED_URL: "https://site.example", RUNNER_GATEWAY_SECRET: secret }), null);
  assert.throws(() => publisher.movementPublisherConfig({ MOVEMENT_STATE_HOSTED_URL: "https://site.example" }), /RUNNER_GATEWAY_SECRET/);
  assert.throws(() => publisher.movementPublisherConfig({ MOVEMENT_STATE_HOSTED_URL: "http://site.example", RUNNER_GATEWAY_SECRET: secret }), /https origin/);
});

// ---------------------------------------------------------------- hosted migrations (PGlite)

const db = new PGlite();
const USER_A = "11111111-1111-4111-8111-111111111111";
const USER_B = "22222222-2222-4222-8222-222222222222";
const query = async (sql, args = []) => (await db.query(sql, args)).rows;
before(async () => {
  await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
    CREATE SCHEMA auth; CREATE TABLE auth.users(id UUID PRIMARY KEY);
    CREATE FUNCTION auth.uid() RETURNS UUID LANGUAGE SQL AS $$ SELECT NULLIF(current_setting('test.uid', true), '')::uuid $$;
    GRANT USAGE ON SCHEMA public, auth TO anon, authenticated, service_role;
    INSERT INTO auth.users VALUES ('${USER_A}'), ('${USER_B}');
    CREATE TABLE public.ta_signals (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), user_id UUID NOT NULL REFERENCES auth.users,
      symbol TEXT NOT NULL, timeframe INTEGER NOT NULL, candle_at TIMESTAMPTZ NOT NULL, detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      price DOUBLE PRECISION NOT NULL, classification TEXT NOT NULL, score DOUBLE PRECISION, atr_pct DOUBLE PRECISION,
      indicators JSONB NOT NULL DEFAULT '{}', factor_breakdown JSONB NOT NULL DEFAULT '{}', patterns TEXT[] NOT NULL DEFAULT '{}',
      source TEXT NOT NULL DEFAULT 'binance-usdm', version TEXT NOT NULL DEFAULT 'ta-v2');
    ALTER TABLE public.ta_signals ENABLE ROW LEVEL SECURITY;
    GRANT SELECT ON public.ta_signals TO authenticated; GRANT ALL ON public.ta_signals TO service_role;
    CREATE POLICY "own TA signals" ON public.ta_signals FOR SELECT TO authenticated USING (auth.uid() = user_id);`);
  await db.exec(await readFile(new URL("../supabase/migrations/20261016160000_latest_ta_signals.sql", import.meta.url), "utf8"));
  await db.exec(await readFile(new URL("../supabase/migrations/20261016170000_market_state_published.sql", import.meta.url), "utf8"));
});
beforeEach(() => db.exec("RESET ROLE; TRUNCATE public.ta_signals; TRUNCATE public.market_state_published"));
after(() => db.close());

const addTa = (user, symbol, timeframe, minutesAgo, score = 1) =>
  db.query(`INSERT INTO public.ta_signals (user_id, symbol, timeframe, candle_at, price, classification, score)
    VALUES ($1,$2,$3, now() - make_interval(mins => $4), 100, 'bullish', $5)`, [user, symbol, timeframe, minutesAgo, score]);
const asUser = async (user) => { await db.exec(`SET ROLE authenticated; SET test.uid = '${user}'`); };

test("latest_ta_signals: a pair that fell behind still returns its own newest row, one row per pair", async () => {
  for (const minutes of [15, 30, 45]) await addTa(USER_A, "BTCUSDT", 60, minutes, 100 + minutes);
  await addTa(USER_A, "ETHUSDT", 60, 300, 7); // five hours behind the others
  await addTa(USER_A, "ETHUSDT", 60, 600, 8);
  await addTa(USER_A, "SOLUSDT", 15, 5, 9); // another timeframe: not returned
  await asUser(USER_A);
  const rows = await query("SELECT symbol, score FROM latest_ta_signals(60, ARRAY['BTCUSDT','ETHUSDT','SOLUSDT'])");
  assert.deepEqual(rows.sort((x, y) => x.symbol.localeCompare(y.symbol)), [{ symbol: "BTCUSDT", score: 115 }, { symbol: "ETHUSDT", score: 7 }]);
  const columns = Object.keys((await query("SELECT * FROM latest_ta_signals(60, ARRAY['BTCUSDT'])"))[0]);
  assert.deepEqual(columns, ["symbol", "timeframe", "candle_at", "detected_at", "price", "classification", "score", "atr_pct", "indicators", "factor_breakdown", "patterns"]);
});

test("latest_ta_signals: RLS scopes it to the caller; unknown symbols and an empty list return nothing", async () => {
  await addTa(USER_A, "BTCUSDT", 60, 10, 1);
  await addTa(USER_B, "BTCUSDT", 60, 5, 2); // newer, but another user's
  await asUser(USER_A);
  assert.deepEqual((await query("SELECT score FROM latest_ta_signals(60, ARRAY['BTCUSDT'])")).map((r) => r.score), [1]);
  assert.equal((await query("SELECT * FROM latest_ta_signals(60, ARRAY['NOPEUSDT'])")).length, 0);
  assert.equal((await query("SELECT * FROM latest_ta_signals(60, ARRAY[]::TEXT[])")).length, 0);
  await db.exec("RESET ROLE");
  await asUser(USER_B);
  assert.deepEqual((await query("SELECT score FROM latest_ta_signals(60, ARRAY['BTCUSDT'])")).map((r) => r.score), [2]);
});

test("latest_ta_signals is bounded to the watchlist limit and closed to anon", async () => {
  await asUser(USER_A);
  const eleven = Array.from({ length: 11 }, (_, i) => `'S${i}USDT'`).join(",");
  await assert.rejects(query(`SELECT * FROM latest_ta_signals(60, ARRAY[${eleven}])`), /At most 10 symbols/);
  assert.equal((await query(`SELECT * FROM latest_ta_signals(60, ARRAY[${eleven.split(",").slice(0, 10).join(",")}])`)).length, 0);
  await db.exec("RESET ROLE; SET ROLE anon");
  await assert.rejects(query("SELECT * FROM latest_ta_signals(60, ARRAY['BTCUSDT'])"));
});

const body = (extra = {}) => JSON.stringify({ k: "v", ...extra });
const publish = async (boundary, extra) => (await query("SELECT publish_market_state($1,$2,$3) AS ok", [UNIVERSE, boundary, body(extra)]))[0].ok;

test("market_state_published: one overwritten row; an older evaluation never replaces a newer one; heartbeat moves published_at only", async () => {
  await db.exec("SET ROLE service_role");
  assert.equal(await publish(1000), true);
  assert.equal(await publish(2000, { v: 2 }), true);
  assert.equal(await publish(1500, { v: 3 }), false); // older boundary refused
  assert.equal(await publish(2000, { v: 4 }), true); // equal boundary accepted
  const rows = await query("SELECT evaluation_boundary_ms, payload, payload_bytes, version FROM market_state_published");
  assert.equal(rows.length, 1);
  assert.deepEqual([Number(rows[0].evaluation_boundary_ms), rows[0].payload.v, rows[0].version], [2000, 4, "market-state-published-v1"]);
  assert.ok(rows[0].payload_bytes > 0);
  await db.exec("RESET ROLE");
  await db.query("UPDATE market_state_published SET published_at = now() - interval '1 hour'");
  await db.exec("SET ROLE service_role");
  assert.equal((await query("SELECT touch_market_state($1) AS ok", [UNIVERSE]))[0].ok, true);
  const touched = (await query("SELECT evaluation_boundary_ms, payload, published_at > now() - interval '1 minute' AS fresh FROM market_state_published"))[0];
  assert.deepEqual([Number(touched.evaluation_boundary_ms), touched.payload.v, touched.fresh], [2000, 4, true]);
  assert.equal((await query("SELECT touch_market_state('unknown') AS ok"))[0].ok, false);
});

test("market_state_published: bounded payload, signed-in users read, nobody but the service role writes", async () => {
  await db.exec("SET ROLE service_role");
  await assert.rejects(publish(1, { big: "x".repeat(530_000) }), /too large/);
  await assert.rejects(query("SELECT publish_market_state($1, 1, '[1]'::jsonb)", [UNIVERSE]), /must be an object/);
  await publish(5000);
  await db.exec("RESET ROLE");
  await asUser(USER_A);
  assert.equal((await query("SELECT * FROM market_state_published")).length, 1); // market-wide: every signed-in user reads it
  await assert.rejects(query("INSERT INTO market_state_published (universe_id, evaluation_boundary_ms, payload, payload_bytes) VALUES ('x', 1, '{}', 2)"));
  await assert.rejects(query("UPDATE market_state_published SET evaluation_boundary_ms = 1"));
  await assert.rejects(query("DELETE FROM market_state_published"));
  await assert.rejects(query("SELECT publish_market_state($1, 9, '{}'::jsonb)", [UNIVERSE]));
  await assert.rejects(query("SELECT touch_market_state($1)", [UNIVERSE]));
  await db.exec("RESET ROLE; SET ROLE anon");
  await assert.rejects(query("SELECT * FROM market_state_published"));
});
