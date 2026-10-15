// Alert setup charts (#299): operational migration and job transitions (PGlite), point-in-time capture
// windows, consumer outcome recording and wiring. Every candle here is SYNTHETIC test data.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, beforeEach, test } from "node:test";
import { PGlite } from "@electric-sql/pglite";

import { createModuleLoader } from "./module-loader.mjs";

const read = (path) => readFile(new URL(path, import.meta.url), "utf8");
const load = createModuleLoader(import.meta.url);
const capture = await import(load("../src/lib/alert-charts/capture.ts"));
const check = await import(load("../src/lib/alert-charts/check.ts"));
const config = await import(load("../src/lib/alert-charts/config.ts"));
const consumer = await import(load("../src/lib/alert-charts/consumer.server.ts"));

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
const enqueue = async (eventMs, { scope = "", maxRows = 100, maxBytes = 10_000_000, win, kind = "pattern" } = {}) =>
  (await db.query(`SELECT * FROM enqueue_alert_chart($1,$2,$3,'BTCUSDT',15,to_timestamp($4/1000.0),'ta-v2',$5,
    'alert-chart-window-v1',$6,$7,$8)`, [kind, `evt-${eventMs}`, scope ? USER : null, eventMs, scope,
    JSON.stringify(win ?? window(eventMs)), maxRows, maxBytes])).rows[0];
const claim = async (worker = "w1", chartId = null, expiry = 15) =>
  (await db.query("SELECT * FROM claim_alert_chart($1,120,$2,$3)", [worker, expiry, chartId])).rows[0];
const finish = async (id, worker, ok, path = "/charts/a.png") =>
  (await db.query("SELECT finish_alert_chart($1,$2,$3,$4,$5,$6,$7) AS ok",
    [id, worker, ok, ok ? path : null, ok ? "a".repeat(64) : null, ok ? 51234 : null, ok ? null : "boom"])).rows[0].ok;
const row = async (id) => (await db.query("SELECT * FROM alert_charts WHERE chart_id = $1", [id])).rows[0];

before(async () => {
  await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
    GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;`);
  await db.exec(await read("../operational-db/supabase/migrations/20261016090000_alert_charts.sql"));
});
beforeEach(() => db.exec("RESET ROLE; TRUNCATE alert_charts"));
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
  const requeued = (await db.query("SELECT requeue_alert_chart($1) AS id", [`evt-${START + Q}`])).rows;
  assert.deepEqual(requeued.map((r) => r.id), [b.chart_id]);
  const job = await claim("w9", b.chart_id);
  assert.equal(job.chart_id, b.chart_id);
  assert.equal(job.attempts, 1);
  assert.equal(await finish(b.chart_id, "w9", false), true);
  assert.equal((await row(b.chart_id)).error, "boom");
  const diag = (await db.query("SELECT * FROM get_alert_chart_diagnostics()")).rows[0];
  assert.equal(Number(diag.chart_rows), 2);
  assert.equal(Number(diag.failed_rows), 2);
});

test("signed-in users and anon cannot read or call the alert chart functions", async () => {
  await enqueue(START);
  for (const role of ["authenticated", "anon"]) {
    await db.exec(`SET ROLE ${role}`);
    await assert.rejects(db.query("SELECT * FROM alert_charts"));
    await assert.rejects(db.query("SELECT * FROM get_alert_chart_diagnostics()"));
    await assert.rejects(db.query("SELECT * FROM claim_alert_chart('x',120,15,NULL)"));
    await db.exec("RESET ROLE");
  }
});

test("the migration is operational-only, commented and never deletes rows", async () => {
  const sql = await read("../operational-db/supabase/migrations/20261016090000_alert_charts.sql");
  assert.doesNotMatch(sql, /\bDELETE\b/i);
  assert.match(sql, /COMMENT ON TABLE public\.alert_charts/);
  assert.match(sql, /ENABLE ROW LEVEL SECURITY/);
  assert.equal(existsSync(new URL("../supabase/migrations/20261016090000_alert_charts.sql", import.meta.url)), false);
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

const venvPython = new URL("../python/.venv/bin/python", import.meta.url).pathname;
test("EMA and RSI match technical.py on the same closes", { skip: !existsSync(venvPython) && "no python/.venv" }, () => {
  const closes = candles(220, HOUR).map((c) => c.close);
  const script = "import json,sys; from market_analysis.technical import _ema,_rsi; v=json.load(sys.stdin); "
    + "print(json.dumps({'e20':_ema(v,20),'e50':_ema(v,50),'rsi':_rsi(v)}))";
  const out = JSON.parse(execFileSync(venvPython, ["-c", script], {
    cwd: new URL("../python", import.meta.url).pathname, input: JSON.stringify(closes) }).toString());
  const e20 = capture.emaSeries(closes, 20);
  const e50 = capture.emaSeries(closes, 50);
  out.e20.forEach((value, i) => assert.ok(value === null ? e20[i] === null : Math.abs(value - e20[i]) < 1e-9));
  out.e50.forEach((value, i) => assert.ok(value === null ? e50[i] === null : Math.abs(value - e50[i]) < 1e-9));
  const rsi = capture.rsiSeries(closes, 14).filter((value) => value !== null);
  assert.equal(rsi.length, out.rsi.length);
  out.rsi.forEach((value, i) => assert.ok(Math.abs(value - rsi[i]) <= 0.01 + 1e-9));
});

// ---------------------------------------------------------------- consumer, config, check, wiring

test("consumer records the PNG digest, or the renderer error, through the lease owner", async () => {
  const dir = await mkdtemp(join(tmpdir(), "alert-charts-"));
  try {
    const finished = [];
    const store = { async finish(id, worker, result) { finished.push({ id, worker, result }); return true; } };
    const job = { chart_id: "0b6f5d55-7a43-4a9e-9d43-7c9f3f0c1a11", event_kind: "pattern", event_id: "s",
      symbol: "BTCUSDT", timeframe_minutes: 60, event_candle_at: "2026-01-02T03:00:00.000Z", candle_window: {}, attempts: 1 };
    const render = async ({ path }) => writeFile(path, "PNG-fake!");
    assert.equal(await consumer.renderClaimedJob(job, { store, workerId: "w", directory: dir, render }), "rendered");
    assert.equal(finished[0].result.path, join(dir, "pattern", "BTCUSDT", "2026-01-02", `${job.chart_id}.png`));
    assert.equal(finished[0].result.bytes, 9);
    assert.match(finished[0].result.sha256, /^[0-9a-f]{64}$/);
    const failing = async () => { throw new Error("WindowError: bad window"); };
    assert.equal(await consumer.renderClaimedJob(job, { store, workerId: "w", directory: dir, render: failing }), "failed");
    assert.deepEqual(finished[1].result, { ok: false, error: "WindowError: bad window" });
    assert.throws(() => consumer.chartPath(dir, { ...job, symbol: "../x" }), /Unsafe/);
    const rejecting = { async finish() { return false; } };
    await assert.rejects(consumer.renderClaimedJob(job, { store: rejecting, workerId: "w", directory: dir, render }), /lease/);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test("config defaults and the mvp:check classification", () => {
  const base = config.alertChartsConfig({});
  assert.equal(base.captureEnabled, true);
  assert.equal(base.directory, null);
  assert.equal(base.pendingExpiryMinutes, 15);
  assert.equal(config.alertChartsConfig({ ALERT_CHARTS_ENABLED: "false" }).captureEnabled, false);
  assert.throws(() => config.alertChartsConfig({ ALERT_CHART_DIR: "relative/dir" }), /absolute/);
  assert.throws(() => config.alertChartsConfig({ ALERT_CHARTS_ENABLED: "yes" }));
  const counts = { chart_rows: 10, window_bytes: 40_000, png_bytes: 800_000, pending_rows: 0, rendering_rows: 0,
    rendered_rows: 10, failed_rows: 0, expired_rows: 0 };
  const dirConfig = { ...base, directory: "/charts" };
  assert.equal(check.classifyAlertCharts(counts, dirConfig).level, "PASS");
  assert.equal(check.classifyAlertCharts({ ...counts, failed_rows: 1 }, dirConfig).level, "WARN");
  assert.equal(check.classifyAlertCharts({ ...counts, pending_rows: 3 }, base).level, "WARN");
  assert.equal(check.classifyAlertCharts(counts, { ...dirConfig, maxRows: 10 }).level, "FAIL");
  assert.equal(check.classifyAlertCharts(counts, { ...dirConfig, maxRows: 12 }).level, "WARN");
});

test("capture is wired after the saved alert and the written TA patterns, best effort", async () => {
  const engine = await read("../src/lib/monitor/engine.server.ts");
  const ta = await read("../src/lib/ta/engine.server.ts");
  const host = await read("../scripts/worker-host.mjs");
  const mvp = await read("../src/worker/mvp-check.ts");
  assert.match(engine, /if \(status === "alerted"\) \{\s+alertsCreated \+= 1;[\s\S]+captureMovementAlertChart\(/);
  assert.match(ta, /\.select\("id, candle_at, patterns"\)[\s\S]+capturePatternCharts\(/);
  assert.match(host, /env\.ALERT_CHART_DIR\?\.trim\(\)\)\s+specs\.push\(nodeJob\("alert-charts", "scripts\/alert-charts\.mjs"\)\)/);
  assert.match(mvp, /checks\.push\(await alertChartsCheck\(\)\)/);
  const server = await read("../src/lib/alert-charts/capture.server.ts");
  assert.match(server, /return \{ status: "failed", error: text \}/); // never throws into the alert path
});
