// Alert setup charts (#299): operational migration and job transitions (PGlite), point-in-time capture
// windows, capture failures and retry. Every candle here is SYNTHETIC test data.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { after, before, beforeEach, test } from "node:test";
import { PGlite } from "@electric-sql/pglite";

import { createModuleLoader } from "./module-loader.mjs";

const read = (path) => readFile(new URL(path, import.meta.url), "utf8");
const load = createModuleLoader(import.meta.url);
const capture = await import(load("../src/lib/alert-charts/capture.ts"));
const check = await import(load("../src/lib/alert-charts/check.ts"));
const config = await import(load("../src/lib/alert-charts/config.ts"));
const stub = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const server = await import(load("../src/lib/alert-charts/capture.server.ts", {
  "./store.server": stub("export const getAlertChartStore = () => null;"),
  "../market/market-movement-state": stub(
    "export const MARKET_UNIVERSE_ID = 'u'; export const toMarketMovementCurrentState = () => ({ status: 'UNAVAILABLE', primary: null });"),
}));

const HOUR = 3_600_000;
const Q = 15 * 60_000;
const START = Date.UTC(2026, 0, 1);
const USER = "11111111-1111-4111-8111-111111111111";

/** SYNTHETIC deterministic candles (a sine walk), not market data. */
function candles(count, timeframeMs, start = START) {
  let price = 100;
  return Array.from({ length: count }, (_, i) => {
    const open = price;
    const close = open + Math.sin(i / 5) * 0.8 + (i % 3) * 0.05;
    price = close;
    return { time: start + i * timeframeMs, open, high: Math.max(open, close) + 0.3,
      low: Math.min(open, close) - 0.3, close, volume: 100 + (i % 7) * 10 };
  });
}

// ---------------------------------------------------------------- migration and job transitions

const db = new PGlite();
const window = (eventMs, extra = []) => ({ version: "alert-chart-window-v1",
  candles: [[eventMs - Q, 1, 2, 0.5, 1.5, 3], [eventMs, 1.5, 2, 1, 1.2, 4], ...extra] });
const enqueue = async (eventMs, { scope = "", maxRows = 100, maxBytes = 10_000_000, win, kind = "pattern", eventId } = {}) =>
  (await db.query(`SELECT * FROM enqueue_alert_chart($1,$2,$3,'BTCUSDT',15,to_timestamp($4/1000.0),'ta-v2',$5,
    'alert-chart-window-v1',$6,$7,$8)`, [kind, eventId ?? `evt-${eventMs}`, scope ? USER : null, eventMs, scope,
    JSON.stringify(win ?? window(eventMs)), maxRows, maxBytes])).rows[0];
const claim = async (worker = "w1", chartId = null, expiry = 15) =>
  (await db.query("SELECT * FROM claim_alert_chart($1,120,$2,$3)", [worker, expiry, chartId])).rows[0];
const finish = async (id, worker, ok, path = "/charts/a.png") =>
  (await db.query("SELECT finish_alert_chart($1,$2,$3,$4,$5,$6,$7) AS ok",
    [id, worker, ok, ok ? path : null, ok ? "a".repeat(64) : null, ok ? 51234 : null, ok ? null : "boom"])).rows[0].ok;
const asOwner = async (fn) => { await db.exec("RESET ROLE"); try { return await fn(); } finally { await db.exec("SET ROLE service_role"); } };
const row = async (id) => (await db.query("SELECT * FROM alert_charts WHERE chart_id = $1", [id])).rows[0];

before(async () => {
  await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
    GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;`);
  await db.exec(await read("../operational-db/supabase/migrations/20261016090000_alert_charts.sql"));
  await db.exec(await read("../operational-db/supabase/migrations/20261016120000_alert_chart_followups.sql"));
  await db.exec(await read("../operational-db/supabase/migrations/20261016150000_alert_chart_upload.sql"));
});
beforeEach(() => db.exec("RESET ROLE; TRUNCATE alert_charts, alert_chart_event_ids, alert_chart_capture_failures"));
after(() => db.close());

test("capture is idempotent on the event identity and scoped per account for movement alerts", async () => {
  await db.exec("SET ROLE service_role");
  const first = await enqueue(START);
  assert.equal(first.created, true);
  const again = await enqueue(START);
  assert.deepEqual(again, { chart_id: first.chart_id, created: false });
  const user = await enqueue(START, { scope: USER, kind: "movement_alert" });
  assert.equal(user.created, true);
  assert.equal((await db.query("SELECT count(*)::int AS n FROM alert_charts")).rows[0].n, 2);
  const saved = await row(first.chart_id);
  assert.equal(saved.status, "pending");
  assert.ok(saved.window_bytes > 0);
});

test("a window with a candle after the event candle is refused", async () => {
  await assert.rejects(enqueue(START, { win: window(START, [[START + Q, 1, 2, 0.5, 1, 1]]) }), /not point-in-time/);
  await assert.rejects(enqueue(START, { win: { candles: [] } }), /no candles/);
});

test("the cap refuses new charts but never deletes and still answers a repeated event", async () => {
  await enqueue(START, { maxRows: 1 });
  await assert.rejects(enqueue(START + Q, { maxRows: 1 }), /cap reached/);
  assert.equal((await enqueue(START, { maxRows: 1 })).created, false);
  assert.equal((await db.query("SELECT count(*)::int AS n FROM alert_charts")).rows[0].n, 1);
});

test("claim, render and finish; only the lease owner can finish; a rendered row needs its PNG", async () => {
  await db.exec("SET ROLE service_role");
  const { chart_id } = await enqueue(START);
  const job = await claim("w1");
  assert.equal(job.chart_id, chart_id);
  assert.equal(job.status, "rendering");
  assert.equal(job.attempts, 1);
  assert.equal(await claim("w2"), undefined); // one pending row, already claimed
  assert.equal(await finish(chart_id, "w2", true), false);
  await assert.rejects(db.query("SELECT finish_alert_chart($1,'w1',true,NULL,NULL,NULL,NULL)", [chart_id]));
  assert.equal(await finish(chart_id, "w1", true), true);
  const done = await row(chart_id);
  assert.equal(done.status, "rendered");
  assert.equal(done.png_bytes, 51234);
  assert.ok(done.rendered_at);
  assert.equal(await finish(chart_id, "w1", true), false); // no second completion
});

test("runner-job rules: an elapsed lease fails, stale pending rows expire, requeue by event id renders again", async () => {
  const a = await enqueue(START);
  const b = await enqueue(START + Q);
  await claim("w1");
  await db.query("UPDATE alert_charts SET lease_expires_at = now() - interval '1 second' WHERE chart_id = $1", [a.chart_id]);
  await db.query("UPDATE alert_charts SET queued_at = now() - interval '20 minutes' WHERE chart_id = $1", [b.chart_id]);
  assert.equal(await claim("w1"), undefined);
  assert.equal((await row(a.chart_id)).status, "failed");
  assert.equal((await row(a.chart_id)).error, "Runner restarted before finishing");
  assert.equal((await row(b.chart_id)).status, "expired");
  const requeued = (await db.query("SELECT * FROM requeue_alert_chart($1)", [`evt-${START + Q}`])).rows;
  assert.deepEqual(requeued.map((r) => [r.chart_id, r.outcome]), [[b.chart_id, "requeued"]]);
  const job = await claim("w9", b.chart_id);
  assert.equal(job.chart_id, b.chart_id);
  assert.equal(job.attempts, 1);
  assert.equal(await finish(b.chart_id, "w9", false), true);
  assert.equal((await row(b.chart_id)).error, "boom");
  const diag = (await db.query("SELECT * FROM get_alert_chart_diagnostics()")).rows[0];
  assert.equal(Number(diag.chart_rows), 2);
  assert.equal(Number(diag.failed_rows), 2);
});

test("requeue sweeps an elapsed lease, reports a valid lease, and tells 'not found' apart", async () => {
  await db.exec("SET ROLE service_role");
  const stuck = await enqueue(START);
  await claim("w1");
  // lease still valid: not requeued, reported as rendering with its lease end
  let rows = (await db.query("SELECT * FROM requeue_alert_chart($1)", [`evt-${START}`])).rows;
  assert.equal(rows.length, 1);
  assert.equal(rows[0].outcome, "rendering");
  assert.ok(rows[0].lease_expires_at);
  assert.equal((await row(stuck.chart_id)).status, "rendering");
  // lease elapsed: the same call sweeps it to failed first, then requeues it
  await asOwner(() => db.query("UPDATE alert_charts SET lease_expires_at = now() - interval '1 second' WHERE chart_id = $1", [stuck.chart_id]));
  rows = (await db.query("SELECT * FROM requeue_alert_chart($1)", [`evt-${START}`])).rows;
  assert.deepEqual(rows.map((r) => [r.chart_id, r.outcome]), [[stuck.chart_id, "requeued"]]);
  const after = await row(stuck.chart_id);
  assert.equal(after.status, "pending");
  assert.equal(after.error, null);
  // unknown id: nothing at all
  assert.equal((await db.query("SELECT * FROM requeue_alert_chart('nope')")).rows.length, 0);
});

test("a market-wide pattern chart is found by every account's signal id, in both lookups", async () => {
  await db.exec("SET ROLE service_role");
  const first = await enqueue(START, { eventId: "signal-first" });
  const repeat = await enqueue(START, { eventId: "signal-second" }); // another account, same candle
  assert.deepEqual(repeat, { chart_id: first.chart_id, created: false });
  const ids = (await db.query("SELECT event_id FROM alert_chart_event_ids WHERE chart_id = $1 ORDER BY 1", [first.chart_id]))
    .rows.map((r) => r.event_id);
  assert.deepEqual(ids, ["signal-first", "signal-second"]);
  // lookup 1: alert_charts.event_id (the first account's signal id)
  let rows = (await db.query("SELECT * FROM requeue_alert_chart('signal-first')")).rows;
  assert.deepEqual(rows.map((r) => r.chart_id), [first.chart_id]);
  // lookup 2: the mapping table (any other account's signal id)
  rows = (await db.query("SELECT * FROM requeue_alert_chart('signal-second')")).rows;
  assert.deepEqual(rows.map((r) => [r.chart_id, r.outcome]), [[first.chart_id, "requeued"]]);
  // a refused capture (cap) maps nothing
  await assert.rejects(enqueue(START + Q, { maxRows: 1, eventId: "signal-third" }), /cap reached/);
  assert.equal((await db.query("SELECT * FROM requeue_alert_chart('signal-third')")).rows.length, 0);
});

test("capture failures are recorded once per event identity and counted in the diagnostics", async () => {
  await db.exec("SET ROLE service_role");
  const record = async (error) => (await db.query(
    "SELECT record_alert_chart_capture_failure('movement_alert','alert-1','BTCUSDT',1,to_timestamp($1/1000.0),$2) AS saved",
    [START, error])).rows[0].saved;
  assert.equal(await record("first error"), true);
  assert.equal(await record("second error"), false); // idempotent on (kind, symbol, timeframe, candle)
  const saved = (await db.query("SELECT * FROM alert_chart_capture_failures")).rows;
  assert.equal(saved.length, 1);
  assert.equal(saved[0].error, "first error");
  await db.query("SELECT record_alert_chart_capture_failure('pattern','sig-long','SOLUSDT',60,to_timestamp($1/1000.0),$2)",
    [START, "x".repeat(900)]);
  assert.equal((await db.query("SELECT length(error) AS n FROM alert_chart_capture_failures WHERE event_id = 'sig-long'")).rows[0].n, 500);
  await asOwner(async () => {
    await db.query("DELETE FROM alert_chart_capture_failures WHERE event_id = 'sig-long'");
    await db.query("UPDATE alert_chart_capture_failures SET created_at = now() - interval '2 days'");
  });
  await db.query("SELECT record_alert_chart_capture_failure('pattern','sig-9','ETHUSDT',60,to_timestamp($1/1000.0),'later')", [START + HOUR]);
  const diag = (await db.query("SELECT * FROM get_alert_chart_diagnostics()")).rows[0];
  assert.equal(Number(diag.capture_failures_24h), 1);
  assert.equal(Number(diag.capture_failures_total), 2);
});

test("hosted upload (#298): a failed upload keeps the rendered chart and retries with backoff; refusal is permanent", async () => {
  await db.exec("SET ROLE service_role");
  const q = async (sql, args = []) => (await db.query(sql, args)).rows;
  const { chart_id } = await enqueue(START, { kind: "movement_alert", scope: USER });
  await claim("w1");
  await finish(chart_id, "w1", true);
  assert.equal((await row(chart_id)).hosted_status, "not_applicable"); // nothing is queued until the consumer asks
  assert.equal((await q("SELECT queue_alert_chart_upload($1) AS ok", [chart_id]))[0].ok, true);
  assert.equal((await q("SELECT * FROM claim_alert_chart_upload()"))[0].chart_id, chart_id);
  assert.equal((await q("SELECT * FROM claim_alert_chart_upload()")).length, 0); // leased: not claimed twice
  await q("SELECT finish_alert_chart_upload($1,false,false,'offline')", [chart_id]);
  const failed = await row(chart_id);
  assert.deepEqual([failed.status, failed.hosted_status, failed.upload_attempts, failed.upload_error, failed.png_path],
    ["rendered", "pending", 1, "offline", "/charts/a.png"]); // the local chart is untouched
  assert.equal((await q("SELECT count(*)::int AS n FROM alert_charts WHERE chart_id = $1 AND upload_next_at > now() + interval '50 seconds' AND upload_next_at < now() + interval '70 seconds'", [chart_id]))[0].n, 1);
  assert.equal((await q("SELECT * FROM claim_alert_chart_upload()")).length, 0); // backing off
  await asOwner(() => db.query("UPDATE alert_charts SET upload_next_at = now() - interval '1 second' WHERE chart_id = $1", [chart_id]));
  assert.equal((await q("SELECT * FROM claim_alert_chart_upload()"))[0].chart_id, chart_id);
  await q("SELECT finish_alert_chart_upload($1,false,false,'offline')", [chart_id]);
  assert.equal((await row(chart_id)).upload_attempts, 2); // 2 min next
  await asOwner(() => db.query("UPDATE alert_charts SET upload_next_at = now() - interval '1 second' WHERE chart_id = $1", [chart_id]));
  await q("SELECT * FROM claim_alert_chart_upload()");
  await q("SELECT finish_alert_chart_upload($1,true,false,NULL)", [chart_id]);
  const uploaded = await row(chart_id);
  assert.deepEqual([uploaded.hosted_status, uploaded.upload_error, uploaded.upload_next_at], ["uploaded", null, null]);
  assert.ok(uploaded.uploaded_at);
  // a permanent refusal (cap, deleted alert) is final until the event is re-queued
  const second = await enqueue(START + Q, { kind: "movement_alert", scope: USER });
  await claim("w1");
  await finish(second.chart_id, "w1", true);
  await q("SELECT queue_alert_chart_upload($1)", [second.chart_id]);
  await q("SELECT * FROM claim_alert_chart_upload()");
  await q("SELECT finish_alert_chart_upload($1,false,true,'The per-account chart limit is reached')", [second.chart_id]);
  assert.deepEqual([(await row(second.chart_id)).hosted_status], ["refused"]);
  // pattern charts are never uploaded
  const pattern = await enqueue(START + 2 * Q);
  await claim("w1");
  await finish(pattern.chart_id, "w1", true);
  assert.equal((await q("SELECT queue_alert_chart_upload($1) AS ok", [pattern.chart_id]))[0].ok, false);
});

// ---------------------------------------------------------------- capture windows

test("pattern window: point-in-time, ends at the event candle, indicators match the full prefix", () => {
  const history = candles(300, HOUR);
  const event = history[250].time;
  const result = capture.buildPatternCapture({ eventId: "sig-1", symbol: "BTCUSDT", source: "binance-usdm",
    timeframe: 60, eventCandleAt: event, patterns: ["volume_spike", "doji"], ruleVersion: "ta-v2", candles: history });
  const w = result.window;
  assert.equal(w.candles.length, 60);
  assert.equal(w.candles.at(-1)[0], event);
  assert.ok(w.candles.every((c) => c[0] <= event));
  assert.deepEqual(w.patterns, ["doji", "volume_spike"]);
  assert.equal(w.indicators.ema20.length, 60);
  assert.equal(w.indicators.history_candles, 251);
  const closes = history.slice(0, 251).map((c) => c.close);
  const expected = capture.emaSeries(closes, 20).at(-1);
  assert.ok(Math.abs(w.indicators.ema20.at(-1) - expected) < 1e-9);
  assert.equal(result.timeframeMinutes, 60);
  assert.equal(result.eventCandleAt, new Date(event).toISOString());
  assert.equal(result.scopeKey, "");
  assert.throws(() => capture.buildPatternCapture({ eventId: "x", symbol: "BTCUSDT", source: "b", timeframe: 60,
    eventCandleAt: event + 30 * 60_000, patterns: ["doji"], ruleVersion: "ta-v2", candles: history }), /not in the chart history/);
});

test("movement window: 24 h of 15m candles completed by the alert, never the developing one", () => {
  const history = candles(200, Q);
  const lastCompleted = history[150];
  const observedAt = new Date(lastCompleted.time + Q + 4 * 60_000).toISOString(); // 4 minutes into the next 15m candle
  const minute = { time: Date.parse(observedAt) - 60_000, open: 1, high: 2, low: 0.5, close: 1.5, volume: 1 };
  const input = { alertId: "a-1", userId: USER, symbol: "ETHUSDT", source: "binance-usdm", direction: "up",
    thresholdPct: 2, changePct: 2.3, baselinePrice: 100, baselineAt: new Date(history[120].time).toISOString(),
    alertPrice: 102.3, observedAt, alertMinute: minute, candles15: history, breadth: null,
    breadthUnavailableReason: "movement_state_stale" };
  const result = capture.buildMovementCapture(input);
  const w = result.window;
  assert.equal(w.candles.length, 96);
  assert.equal(w.candles.at(-1)[0], lastCompleted.time);
  assert.ok(w.candles.every((c) => c[0] + Q <= Date.parse(observedAt)));
  assert.equal(w.event_candle_ms, lastCompleted.time);
  assert.equal(w.alert.alert_minute[0], minute.time);
  assert.equal(w.breadth_unavailable_reason, "movement_state_stale");
  assert.equal(result.timeframeMinutes, 1);
  assert.equal(result.eventCandleAt, new Date(minute.time).toISOString());
  assert.equal(result.scopeKey, USER);
  assert.equal(capture.captureIdentity(result), capture.captureIdentity(capture.buildMovementCapture(input)));
  assert.notEqual(capture.captureIdentity(result),
    capture.captureIdentity(capture.buildMovementCapture({ ...input, userId: "22222222-2222-4222-8222-222222222222" })));
  assert.throws(() => capture.buildMovementCapture({ ...input, alertMinute: { ...minute, time: minute.time - 60_000 } }));
});

test("breadth is attached only from a LIVE state at the alert time", () => {
  const side = (count) => ({ available: true, value: { count, fraction: count / 6 } });
  const state = (status, boundary) => ({ status, primary: { windowMinutes: 5, directionState: "RISING", evidence: {
    evaluationBoundaryTime: boundary, breadth: { denominator: 6, rising: side(5), falling: side(0), flat: side(1),
      materialRising: side(4), materialFalling: { available: false, value: null } } } } });
  const t = START + 10 * HOUR;
  assert.equal(capture.breadthAtAlert(state("LIVE", t + 5_000), t).breadth.rising, 5);
  assert.equal(capture.breadthAtAlert(state("LIVE", t + 5_000), t).breadth.material_falling, null);
  assert.equal(capture.breadthAtAlert(state("STALE", t), t).reason, "movement_state_stale");
  assert.equal(capture.breadthAtAlert(state("LIVE", t + 10 * 60_000), t).reason, "movement_state_not_at_alert_time");
  assert.equal(capture.breadthAtAlert(null, t).reason, "movement_state_unavailable");
});

// ---------------------------------------------------------------- config and check

test("config defaults and the mvp:check classification", () => {
  const base = config.alertChartsConfig({});
  assert.equal(base.captureEnabled, true);
  assert.equal(base.directory, null);
  assert.equal(base.pendingExpiryMinutes, 10_080); // 7 days: the runner-job 15 minutes is relaxed on purpose
  assert.equal(config.alertChartsConfig({ ALERT_CHART_PENDING_EXPIRY_MINUTES: "15" }).pendingExpiryMinutes, 15);
  assert.throws(() => config.alertChartsConfig({ ALERT_CHART_PENDING_EXPIRY_MINUTES: "10081" }));
  assert.equal(config.alertChartsConfig({ ALERT_CHARTS_ENABLED: "false" }).captureEnabled, false);
  assert.throws(() => config.alertChartsConfig({ ALERT_CHART_DIR: "relative/dir" }), /absolute/);
  assert.throws(() => config.alertChartsConfig({ ALERT_CHARTS_ENABLED: "yes" }));
  const counts = { chart_rows: 10, window_bytes: 40_000, png_bytes: 800_000, pending_rows: 0, rendering_rows: 0,
    rendered_rows: 10, failed_rows: 0, expired_rows: 0, capture_failures_24h: 0, capture_failures_total: 0 };
  const dirConfig = { ...base, directory: "/charts" };
  assert.equal(check.classifyAlertCharts(counts, dirConfig).level, "PASS");
  assert.equal(check.classifyAlertCharts({ ...counts, failed_rows: 1 }, dirConfig).level, "WARN");
  assert.equal(check.classifyAlertCharts({ ...counts, pending_rows: 3 }, base).level, "WARN");
  assert.equal(check.classifyAlertCharts(counts, { ...dirConfig, maxRows: 10 }).level, "FAIL");
  assert.equal(check.classifyAlertCharts(counts, { ...dirConfig, maxRows: 12 }).level, "WARN");
  // capture failures: WARN in the last 24 h, PASS when only old ones exist, never FAIL by themselves
  assert.equal(check.classifyAlertCharts({ ...counts, capture_failures_24h: 2, capture_failures_total: 5 }, dirConfig).level, "WARN");
  assert.match(check.classifyAlertCharts({ ...counts, capture_failures_24h: 2, capture_failures_total: 5 }, dirConfig).detail, /2 in 24 h, 5 total/);
  assert.equal(check.classifyAlertCharts({ ...counts, capture_failures_24h: 0, capture_failures_total: 5 }, dirConfig).level, "PASS");
  assert.equal(check.classifyAlertCharts({ ...counts, capture_failures_24h: 99, capture_failures_total: 99 }, dirConfig).level, "WARN");
});

// ---------------------------------------------------------------- capture failures, retry, deferral

function captureHarness({ alertRow, readFailures = 0, candleFailures = 0, enqueue, recordFailure, badCandles = false } = {}) {
  const history = candles(200, Q);
  const observedAt = new Date(history[150].time + Q + 4 * 60_000).toISOString();
  const row = alertRow ?? { id: "alert-1", symbol: "ETHUSDT", price: 102.3, change_pct: 2.3, threshold_pct: 2, baseline_price: 100,
    baseline_at: new Date(history[120].time).toISOString(), observed_at: observedAt, data_source: "binance-usdm", rule: "r" };
  const calls = { alertReads: 0, candleReads: 0, failures: [], enqueued: [] };
  const db = { from: () => ({ select() { return this; }, eq() { return this; }, async single() {
    calls.alertReads += 1;
    if (calls.alertReads <= readFailures) return { data: null, error: { message: "network blip" } };
    return { data: row, error: null };
  } }) };
  const operationalStore = { enabled: false, async readCollectorCompletedCandles() {
    calls.candleReads += 1;
    if (calls.candleReads <= candleFailures) throw new Error("operational read failed");
    return { observations: history.map((c) => ({ candle: { openTime: c.time + (badCandles ? 1 : 0), open: c.open, high: c.high, low: c.low,
      close: c.close, baseVolume: c.volume } })) };
  } };
  const store = {
    async enqueue(capture) { calls.enqueued.push(capture); return enqueue ? enqueue(capture) : { chartId: "c1", created: true }; },
    async recordFailure(failure) { calls.failures.push(failure); if (recordFailure) throw recordFailure; },
  };
  const deps = { db, operationalStore, context: {}, userId: USER, symbol: "ETHUSDT", alertId: "alert-1", observedAt, minute: [],
    collectorOwnsMarketData: true, store, config: config.alertChartsConfig({}), retryDelayMs: 0 };
  return { deps, calls, observedAt };
}

test("a failed capture is recorded with its identity and never throws, even if recording fails", async () => {
  const quiet = console.warn;
  console.warn = () => {};
  try {
    const failing = captureHarness({ enqueue: () => { throw new Error("Alert chart cap reached"); } });
    const outcome = await server.captureMovementAlertChart(failing.deps);
    assert.equal(outcome.status, "failed");
    assert.equal(failing.calls.failures.length, 1);
    const failure = failing.calls.failures[0];
    assert.equal(failure.eventKind, "movement_alert");
    assert.equal(failure.eventId, "alert-1");
    assert.equal(failure.symbol, "ETHUSDT");
    assert.equal(failure.timeframeMinutes, 1);
    assert.equal(failure.eventCandleAt, new Date(Date.parse(failing.observedAt) - 60_000).toISOString());
    assert.match(failure.error, /cap reached/);
    const broken = captureHarness({ enqueue: () => { throw new Error("x"); }, recordFailure: new Error("db down") });
    assert.equal((await server.captureMovementAlertChart(broken.deps)).status, "failed"); // resolved, not rejected
    assert.equal(broken.calls.failures.length, 1);
    const ok = captureHarness();
    assert.equal((await server.captureMovementAlertChart(ok.deps)).status, "captured");
    assert.equal(ok.calls.failures.length, 0);
  } finally {
    console.warn = quiet;
  }
});

test("only transient read errors are retried, once; validation errors are not", async () => {
  const quiet = console.warn;
  console.warn = () => {};
  try {
    const blip = captureHarness({ readFailures: 1 });
    assert.equal((await server.captureMovementAlertChart(blip.deps)).status, "captured");
    assert.equal(blip.calls.alertReads, 2);
    const candleBlip = captureHarness({ candleFailures: 1 });
    assert.equal((await server.captureMovementAlertChart(candleBlip.deps)).status, "captured");
    assert.equal(candleBlip.calls.candleReads, 2);
    const down = captureHarness({ readFailures: 2 });
    assert.equal((await server.captureMovementAlertChart(down.deps)).status, "failed");
    assert.equal(down.calls.alertReads, 2); // one retry, then final
    assert.equal(down.calls.failures.length, 1);
    const noBaseline = captureHarness({ alertRow: { id: "alert-1", symbol: "ETHUSDT", price: 1, change_pct: 2, threshold_pct: 2,
      baseline_price: null, baseline_at: null, observed_at: new Date(START).toISOString(), data_source: "b", rule: "r" } });
    assert.equal((await server.captureMovementAlertChart(noBaseline.deps)).status, "failed");
    assert.equal(noBaseline.calls.alertReads, 1); // not transient: no retry
    assert.equal(noBaseline.calls.candleReads, 0);
    assert.equal(noBaseline.calls.failures.length, 1);
    const invalid = captureHarness({ badCandles: true }); // the read succeeds, the window validation fails
    assert.equal((await server.captureMovementAlertChart(invalid.deps)).status, "failed");
    assert.equal(invalid.calls.candleReads, 1); // validation errors are not transient: no retry
    assert.equal(invalid.calls.enqueued.length, 0);
    assert.equal(invalid.calls.failures.length, 1);
    const enqueueError = captureHarness({ enqueue: () => { throw new Error("db down"); } });
    await server.captureMovementAlertChart(enqueueError.deps);
    assert.equal(enqueueError.calls.alertReads, 1); // the enqueue write is not a read step either
    assert.equal(enqueueError.calls.candleReads, 1);
  } finally {
    console.warn = quiet;
  }
});

test("pattern capture failures are recorded too, without a retry", async () => {
  const quiet = console.warn;
  console.warn = () => {};
  try {
    const failures = [];
    const store = { async enqueue() { throw new Error("cap reached"); }, async recordFailure(f) { failures.push(f); } };
    const history = candles(80, HOUR);
    const outcomes = await server.capturePatternCharts({ symbol: "BTCUSDT", source: "binance-usdm", timeframe: 60,
      ruleVersion: "ta-v2", candles: history, store, config: config.alertChartsConfig({}),
      events: [{ id: "sig-1", candleAt: history[70].time, patterns: ["doji"] }] });
    assert.equal(outcomes[0].status, "failed");
    assert.deepEqual(failures.map((f) => [f.eventKind, f.eventId, f.symbol, f.timeframeMinutes]), [["pattern", "sig-1", "BTCUSDT", 60]]);
  } finally {
    console.warn = quiet;
  }
});

test("deferred captures: starting is immediate, settling is capped, abandoned ones are recorded and write nothing later", async () => {
  const quiet = console.warn;
  console.warn = () => {};
  try {
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    const slow = captureHarness({ enqueue: async () => { await gate; return { chartId: "c1", created: true }; } });
    const fast = captureHarness();
    fast.deps.symbol = "BTCUSDT";
    const started = Date.now();
    const pending = [server.startMovementCapture(slow.deps), server.startMovementCapture(fast.deps)];
    assert.ok(Date.now() - started < 100); // start does not wait for the capture
    const result = await server.settleMovementCaptures(pending, { capMs: 50 });
    assert.deepEqual(result.abandoned, ["ETHUSDT"]);
    assert.equal(result.settled, 1);
    assert.equal(fast.calls.failures.length, 0);
    assert.equal(slow.calls.failures.length, 1);
    assert.match(slow.calls.failures[0].error, /abandoned after 50 ms/);
    assert.equal(slow.calls.failures[0].symbol, "ETHUSDT");
    assert.equal(slow.calls.failures[0].timeframeMinutes, 1);
    assert.equal((await server.settleMovementCaptures([], { capMs: 1 })).settled, 0);
    // a capture already abandoned when it starts reads nothing, does not wait for a retry and enqueues nothing
    const early = captureHarness({ readFailures: 0 });
    const control = { abandoned: true };
    const outcome = await server.captureMovementAlertChart({ ...early.deps, control });
    assert.equal(outcome.status, "failed");
    assert.equal(early.calls.alertReads, 0);
    assert.equal(early.calls.enqueued.length, 0);
    release();
  } finally {
    console.warn = quiet;
  }
});

test("failed capture and unavailable settle paths never throw into the run", async () => {
  const quiet = console.warn;
  console.warn = () => {};
  try {
    const store = { async enqueue() { return { chartId: "c", created: true }; }, async recordFailure() { throw new Error("db down"); } };
    const stuck = captureHarness();
    stuck.deps.store = store;
    stuck.deps.db = { from: () => ({ select() { return this; }, eq() { return this; }, single: () => new Promise(() => {}) }) };
    const pending = [server.startMovementCapture(stuck.deps)];
    const result = await server.settleMovementCaptures(pending, { capMs: 20 }); // recording the abandonment fails: still resolves
    assert.deepEqual(result.abandoned, ["ETHUSDT"]);
  } finally {
    console.warn = quiet;
  }
});
