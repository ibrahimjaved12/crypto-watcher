import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

async function moduleUrl(path, imports = {}) {
  const source = await readFile(new URL(path, import.meta.url), "utf8");
  let { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  });
  for (const [specifier, target] of Object.entries(imports))
    outputText = outputText.replaceAll(JSON.stringify(specifier), JSON.stringify(target));
  return `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`;
}

const zod = import.meta.resolve("zod");
const contractUrl = await moduleUrl("../src/lib/forward/forward-contract.ts", { zod });
const trendContractUrl = await moduleUrl("../src/lib/forward/forward-trend-contract.ts", { zod });
const runUrl = await moduleUrl("../src/lib/forward/forward-run.server.ts", { "./forward-contract": contractUrl });
const trendRunUrl = await moduleUrl("../src/lib/forward/forward-trend-run.server.ts", {
  "./forward-run.server": runUrl, "./forward-trend-contract": trendContractUrl });
const contract = await import(trendContractUrl);
const trend = await import(trendRunUrl);
const { createTrendRepository } = await import(await moduleUrl("../src/lib/forward/forward-trend-repository.server.ts"));
const { summarizeTrack } = await import(await moduleUrl("../src/lib/forward/forward-trend-dashboard.ts"));

const DAY = 86_400_000;
const NOW = Date.UTC(2026, 9, 9, 0, 7);  // 00:07 UTC: 2026-10-08 is the last completed day
const LAST = Date.UTC(2026, 9, 8);
const USER = "11111111-1111-4111-8111-111111111111";
const hex = (c) => c.repeat(64);

function kline(day, close = "101.5") {
  return [day, "100.0", "103.0", "99.0", close, "10.5", day + DAY - 1, "1050.25", 7, "5", "500", "0"];
}

test("only completed UTC days are kept from a Binance daily kline response", () => {
  const rows = contract.parseBinanceDailyKlines("BTCUSDT", [kline(LAST - DAY), kline(LAST), kline(LAST + DAY)], NOW);
  assert.deepEqual(rows.map((row) => row.day_ms), [LAST - DAY, LAST]);  // the running day is dropped
  assert.equal(rows[0].close, "101.5");
  assert.equal(contract.parseBinanceDailyKlines("BTCUSDT", [kline(LAST)], LAST + DAY - 1).length, 0);
  assert.throws(() => contract.parseBinanceDailyKlines("BTCUSDT", [kline(LAST + 1)], NOW), /Invalid/);
  assert.throws(() => contract.parseBinanceDailyKlines("BTCUSDT", [kline(LAST), kline(LAST)], NOW), /Invalid/);
  const bad = kline(LAST);
  bad[3] = "200.0";  // low above the open
  assert.throws(() => contract.parseBinanceDailyKlines("BTCUSDT", [bad], NOW), /Invalid/);
});

function state(track, lastDay) {
  return { version: "trend-track-v1", track, last_day_ms: lastDay, equity_ppm: 1_000_000, n_days: 1, sum_ppm: 0,
    sum_sq_ppm: 0, peak_equity_ppm: 1_000_000, max_drawdown_ppm: 0, weights: { BTCUSDT: 0.5 }, mu_min_daily: null };
}

function response(patch = {}) {
  return {
    schema_version: 1, versions: { trend_track: "trend-track-v1", strategy: "trend-v1" }, params_hash: hex("a"),
    symbols: ["BTCUSDT"], track_start_ms: trend.TREND_TRACK_START_MS,
    tracks: [{ name: "ens_ls_25", kind: "variant", control: "bh_vt_std90_25", config_hash: hex("b") }],
    through_day_ms: LAST,
    ledger: [{ track: "ens_ls_25", day_ms: LAST, daily_ppm: 120, equity_ppm: 1_000_120, turnover_ppm: 0,
      gross_ppm: 500_000, symbols_active: 1, weights: { BTCUSDT: 0.5 } }],
    weights: [{ track: "ens_ls_25", day_ms: LAST + DAY, decided_from_close_ms: LAST, weights: { BTCUSDT: 0.5 },
      defined: { BTCUSDT: true } }],
    states: { ens_ls_25: state("ens_ls_25", LAST) }, funding_unavailable: [], reasons: {},
    assumptions: ["hypothetical-no-margin"], ...patch,
  };
}

function fakeDeps({ fundingFails = false, stored = [], reply = (body) => response() } = {}) {
  const calls = { python: [], recorded: [], fetches: [], funding: [], persisted: [], states: [] };
  const okRuns = new Set();
  const repository = {
    async findOkRun(userId, runKey) { return okRuns.has(`${userId}|${runKey}`); },
    async latestStates() { return calls.states.length ? calls.states[calls.states.length - 1].states : null; },
    async persistRows(userId, runKey, value) { calls.persisted.push({ userId, runKey, value }); return { ledger: 1 }; },
    async insertState(userId, row) {
      calls.states.push({ userId, ...row });
      if (row.status === "ok") okRuns.add(`${userId}|${row.run_key}`);
    },
  };
  const db = [...stored];
  return { calls, deps: {
    now: () => NOW,
    readDailyBars: async (symbol, since) => db.filter((bar) => bar.symbol === symbol && bar.day_ms >= since),
    recordDailyBars: async (bars) => { calls.recorded.push(bars); db.push(...bars); return bars.length; },
    fetchDailyKlines: async (symbol, startMs, nowMs) => {
      calls.fetches.push({ symbol, startMs });
      const rows = [];
      for (let day = startMs; day <= LAST + DAY; day += DAY) rows.push(kline(day));
      return contract.parseBinanceDailyKlines(symbol, rows, nowMs);
    },
    fetchFunding: async (symbol, startMs) => {
      calls.funding.push({ symbol, startMs });
      if (fundingFails) throw new Error("HTTP 429");
      return [{ calc_time_ms: LAST + 8 * 3_600_000, rate: "0.0001" }];
    },
    callPython: async (body) => { calls.python.push(body); return reply(body); },
    repository,
  } };
}

test("first run backfills from the fixed history start, sends completed days only and is idempotent", async () => {
  const { calls, deps } = fakeDeps();
  const input = { userId: USER, trigger: "daily", symbols: ["BTCUSDT"] };
  const first = await trend.runForwardTrend(deps, input);
  assert.equal(first.status, "ok");
  assert.equal(first.runKey, `day:${LAST}`);
  assert.equal(calls.fetches[0].startMs, trend.TREND_HISTORY_START_MS);
  assert.ok(trend.TREND_TRACK_START_MS - trend.TREND_HISTORY_START_MS >= 400 * DAY);
  const sent = calls.python[0].symbols[0].bars;
  assert.equal(sent[sent.length - 1].day_ms, LAST);  // the running day is never stored or sent
  assert.equal(sent.length, (LAST - trend.TREND_HISTORY_START_MS) / DAY + 1);
  assert.equal(calls.python[0].through_day_ms, LAST);
  assert.equal(calls.python[0].states, null);
  assert.equal(calls.funding[0].startMs, trend.TREND_TRACK_START_MS);
  assert.equal(calls.persisted[0].userId, USER);
  assert.equal(calls.states[0].userId, USER);
  const again = await trend.runForwardTrend(deps, input);
  assert.equal(again.status, "already_done");
  assert.equal(calls.python.length, 1);
  // A later day appends only the new day and passes the stored states on.
  const later = { ...deps, now: () => NOW + DAY };
  const next = await trend.runForwardTrend(later, input);
  assert.equal(calls.fetches[calls.fetches.length - 1].startMs, LAST + DAY);
  assert.deepEqual(calls.recorded[calls.recorded.length - 1].map((bar) => bar.day_ms), [LAST + DAY]);
  assert.deepEqual(calls.python[1].states, response().states);
  assert.equal(calls.funding[calls.funding.length - 1].startMs, LAST + DAY);
  assert.equal(next.status, "partial");  // the fake reply is still through LAST
});

test("a failed funding fetch finalises nothing and records funding_unavailable", async () => {
  const { calls, deps } = fakeDeps({ fundingFails: true, reply: () => response({ ledger: [], through_day_ms: null,
    states: {}, funding_unavailable: ["BTCUSDT"], reasons: { ens_ls_25: "funding_unavailable:BTCUSDT" } }) });
  const summary = await trend.runForwardTrend(deps, { userId: USER, trigger: "daily", symbols: ["BTCUSDT"] });
  assert.equal(calls.python[0].symbols[0].funding_available, false);
  assert.deepEqual(calls.python[0].symbols[0].funding, []);  // never zeros
  assert.equal(summary.status, "partial");
  assert.match(summary.reason, /funding_unavailable: BTCUSDT/);
  assert.equal(calls.states[0].status, "partial");
  assert.deepEqual(calls.states[0].funding_unavailable, ["BTCUSDT"]);
  assert.notEqual(calls.states[0].run_key, `day:${LAST}`);  // a later run may still complete the day
});

test("a symbol whose feed lags holds back finalisation for every symbol", async () => {
  const stored = [{ symbol: "ETHUSDT", day_ms: trend.TREND_HISTORY_START_MS, open: "1", high: "1", low: "1",
    close: "1", volume: "0", quote_volume: "0" }];
  const { calls, deps } = fakeDeps({ stored });
  deps.fetchDailyKlines = async (symbol, startMs, nowMs) => {
    if (symbol === "ETHUSDT") throw new Error("HTTP 418");
    const rows = [];
    for (let day = startMs; day <= LAST; day += DAY) rows.push(kline(day));
    return contract.parseBinanceDailyKlines(symbol, rows, nowMs);
  };
  const summary = await trend.runForwardTrend(deps, { userId: USER, trigger: "daily", symbols: ["BTCUSDT", "ETHUSDT"] });
  assert.equal(calls.python[0].through_day_ms, trend.TREND_HISTORY_START_MS);
  assert.equal(summary.status, "partial");
  assert.match(summary.reason, /ETHUSDT: daily kline fetch failed/);
});

test("the Python trend response contract is validated", () => {
  assert.equal(contract.validateTrendResponse(response()).ledger.length, 1);
  assert.throws(() => contract.validateTrendResponse(response({ schema_version: 2 })), /schema_version/);
  const late = response();
  late.weights[0].decided_from_close_ms = late.weights[0].day_ms;  // a weight decided on its own day's close
  assert.throws(() => contract.validateTrendResponse(late), /weights/);
  const twice = response();
  twice.ledger.push(twice.ledger[0]);
  assert.throws(() => contract.validateTrendResponse(twice), /duplicate ledger day/);
});

test("the repository scopes every row to the account and ignores duplicates per (track, day)", async () => {
  const writes = [];
  const client = { from(table) {
    return {
      upsert(rows, options) { writes.push({ table, rows, options }); return Promise.resolve({ error: null }); },
      insert(row) { writes.push({ table, rows: [row] }); return Promise.resolve({ error: null }); },
    };
  } };
  const repository = createTrendRepository(client);
  const counts = await repository.persistRows(USER, `day:${LAST}`, contract.validateTrendResponse(response()));
  assert.deepEqual(counts, { ledger: 1, weights: 1 });
  await repository.insertState(USER, { run_key: `day:${LAST}`, status: "ok" });
  for (const write of writes) {
    for (const row of write.rows) assert.equal(row.user_id, USER);
    if (write.options) {
      assert.equal(write.options.ignoreDuplicates, true);
      assert.equal(write.options.onConflict, "user_id,track,day_ms");
    }
  }
  assert.deepEqual(writes.map((w) => w.table), ["forward_trend_ledger", "forward_trend_weights", "forward_trend_state"]);
  assert.equal(writes[0].rows[0].params_hash, hex("a"));
  assert.equal(writes[0].rows[0].version, "trend-track-v1");
});

test("dashboard summary: mu_min from the live sample and the days-needed line", () => {
  const rows = [100, -50, 200, 30, 80].map((daily_ppm, i) => ({ track: "ens_ls_25", day_ms: LAST + i * DAY, daily_ppm,
    equity_ppm: 1_000_000 + daily_ppm, turnover_ppm: 10_000, gross_ppm: 500_000 }));
  const summary = summarizeTrack({ name: "ens_ls_25", kind: "variant", control: "bh_vt_std90_25" }, rows);
  assert.equal(summary.days, 5);
  const values = rows.map((row) => row.daily_ppm / 1e6);
  const mean = values.reduce((a, b) => a + b, 0) / 5;
  const sd = Math.sqrt(values.reduce((a, b) => a + (b - mean) ** 2, 0) / 4);
  assert.ok(Math.abs(summary.muMinDaily - (2.801585218 * sd) / Math.sqrt(5)) < 1e-15);
  assert.match(summary.significanceLine, /^5 days, not significant/);
  assert.equal(summary.daysNeeded, Math.ceil(((2.801585218 * sd) / mean) ** 2));
  assert.equal(summarizeTrack({ name: "x", kind: "variant", control: null }, rows).days, 0);
});

test("the daily job runs at 00:05 UTC", () => {
  assert.equal(trend.msUntilNextDailyRun(Date.UTC(2026, 9, 9, 0, 0)), 5 * 60_000);
  assert.equal(trend.msUntilNextDailyRun(Date.UTC(2026, 9, 9, 0, 5)), DAY);
});
