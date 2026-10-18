import assert from "node:assert/strict";
import { test } from "node:test";
import { createModuleLoader } from "./module-loader.mjs";

// #397: the hourly forex stage runner with injected deps (no network, no database, no Python). Every bar, spec
// and stage here is SYNTHETIC.
const moduleUrl = createModuleLoader(import.meta.url);
const fx = await import(await moduleUrl("../src/lib/fx/fx-stage.server.ts"));
const { pythonServiceConfig } = await import(await moduleUrl("../src/lib/python-service.server.ts"));

const HOUR = 3_600_000;
const MIN = 60_000;
const BOUNDARY = Date.UTC(2026, 9, 13, 7, 0);
const NOW = BOUNDARY + 3 * MIN;
const VENUE = "test-venue";

function spec(instrumentId, overrides = {}) {
  const symbol = instrumentId.split(":")[1];
  return { venue: VENUE, instrumentId, nativeSymbol: `${symbol}m`, digits: 5, point: 0.00001, tickSize: 0.00001,
    tickValue: 1, contractSize: 100000, volumeMin: 0.01, volumeMax: 200, volumeStep: 0.01,
    baseCurrency: symbol.slice(0, 3), profitCurrency: symbol.slice(3), marginCurrency: symbol.slice(0, 3),
    accountCurrency: "USD", tradeMode: 4, swapMode: 1, swapLong: -7.3, swapShort: 2.1, swapRollover3days: 3,
    specHash: "a".repeat(64), observedAt: 0, ...overrides };
}
function bar(instrumentId, openMs, close = 1.085, spread = 12) {
  const symbol = instrumentId.split(":")[1];
  return { time_basis: "utc", venue: VENUE, instrument_id: instrumentId, symbol, native_symbol: `${symbol}m`,
    transport: "poll", open_time_ms: openMs, open: close, high: close, low: close, close, spread_points: spread,
    tick_volume: 3, received_at_ms: openMs + MIN };
}
const sub = (instrumentId) => ({ venue: VENUE, instrumentId, nativeSymbol: `${instrumentId.split(":")[1]}m` });
const health = (instrumentId, overrides = {}) => ({ venue: VENUE, instrument_id: instrumentId, status: "LIVE",
  last_bar_open_time_ms: BOUNDARY, lag_ms: 500, error_message: null, conflict_count_24h: 0, last_conflict_at_ms: null,
  updated_at_ms: NOW - 20_000, ...overrides });

function stageResponse(body) {
  return { schema_version: 1, versions: { fx_stage: "fx-stage-v1" }, params_hash: "p".repeat(64),
    processed_to_ms: body.boundary_ms,
    stage: { id: `stage-${body.boundary_ms}-${body.symbols.map((s) => s.symbol).join(",")}`, boundary_ms: body.boundary_ms,
      versions: {}, params_hash: "p".repeat(64), payload: {}, freshness: {} },
    signals: [{ signal_id: "s1" }], suppressed: [], setups: [], outcomes: [], events: [] };
}

function fakeDeps({ subscriptions = ["fx:EURUSD"], specs, healthRows, calendarTo = BOUNDARY + 2 * 24 * HOUR,
                    recent = [], committed = null, bars = {} } = {}) {
  const calls = { stage: [], commit: [], logs: [], errors: [], events: [] };
  const ids = subscriptions;
  const deps = {
    now: () => NOW,
    store: {
      async getFxSubscriptions() { return ids.map(sub); },
      async getFxSymbolSpecs() { return specs ?? ids.map((id) => spec(id)); },
      async getFxCollectorHealth() { return healthRows ?? ids.map((id) => health(id)); },
      async getFxCalendarStatus() {
        return [{ source: "forexfactory", last_ok_ms: NOW - HOUR, last_ok_covered_to_ms: calendarTo, last_error_ms: null, last_error: null }];
      },
      async getFxCalendarEvents(fromMs, toMs, asOfMs) { calls.events.push([fromMs, toMs, asOfMs]); return []; },
      async getFxBars(venue, instrumentId, options) {
        if (bars[instrumentId]) return bars[instrumentId].slice(options?.limit === 1 ? -1 : 0);
        return [bar(instrumentId, BOUNDARY - MIN), bar(instrumentId, BOUNDARY)];
      },
    },
    hosted: {
      async recentStages() { return recent; },
      async openSetups() { return []; },
      async outcomes() { return []; },
      async commit(stage, records) { calls.commit.push([stage, records]); return committed ?? stage.id; },
    },
    async callStage(body) { calls.stage.push(body); return stageResponse(body); },
    log: (line) => calls.logs.push(line),
    error: (line) => calls.errors.push(line),
  };
  return { deps, calls };
}

test("a stale collector refuses the whole stage (retryable, nothing evaluated or committed)", async () => {
  for (const row of [health("fx:EURUSD", { status: "STALE" }), health("fx:EURUSD", { updated_at_ms: NOW - 10 * MIN }),
    health("fx:EURUSD", { last_bar_open_time_ms: BOUNDARY - 10 * MIN })]) {
    const { deps, calls } = fakeDeps({ healthRows: [row] });
    const result = await fx.runFxHourly(deps);
    assert.equal(result.status, "skipped_stale");
    assert.match(result.reason, /EURUSD: (collector STALE|collector health \d+s old|newest bar older than the boundary)/);
    assert.deepEqual([calls.stage.length, calls.commit.length], [0, 0]);
    assert.match(calls.logs.at(-1), /^\[forex-hourly\] hour:\d+ skipped_stale: /);
  }
  // A closed market is not stale.
  const closed = fakeDeps({ healthRows: [health("fx:EURUSD", { status: "MARKET_CLOSED", last_bar_open_time_ms: BOUNDARY - 60 * HOUR })] });
  assert.equal((await fx.runFxHourly(closed.deps)).status, "ok");
  // Missing health or a missing spec refuses too.
  assert.equal((await fx.runFxHourly(fakeDeps({ healthRows: [] }).deps)).status, "skipped_stale");
  const noSpec = await fx.runFxHourly(fakeDeps({ specs: [] }).deps);
  assert.equal(noSpec.status, "skipped_stale");
  assert.match(noSpec.reason, /EURUSD: no symbol spec/);
});

test("calendar coverage short of the boundary plus the longest window refuses the stage", async () => {
  const needed = BOUNDARY + fx.FX_LONGEST_WINDOW_MS;
  assert.equal(fx.FX_LONGEST_WINDOW_MS, 16 * HOUR);
  const short = await fx.runFxHourly(fakeDeps({ calendarTo: needed - 1 }).deps);
  assert.equal(short.status, "skipped_stale");
  assert.match(short.reason, /calendar: covered to/);
  assert.equal((await fx.runFxHourly(fakeDeps({ calendarTo: needed }).deps)).status, "ok");
  const never = fakeDeps();
  never.deps.store.getFxCalendarStatus = async () => [];
  assert.match((await fx.runFxHourly(never.deps)).reason, /calendar: never fetched/);
});

test("a committed boundary is already_done; a replayed commit returning another id is already_done too", async () => {
  const done = fakeDeps({ recent: [{ boundary_ms: BOUNDARY, freshness: {} }] });
  assert.equal((await fx.runFxHourly(done.deps)).status, "already_done");
  assert.deepEqual([done.calls.stage.length, done.calls.commit.length], [0, 0]);
  assert.match(done.calls.logs.at(-1), /already_done/);
  // A second runner raced us: commit_fx_stage returns the stage already stored for the boundary.
  const raced = fakeDeps({ committed: "another-stage-id" });
  const result = await fx.runFxHourly(raced.deps);
  assert.equal(result.status, "already_done");
  assert.equal(raced.calls.commit.length, 1);
  // The same inputs give the same request (the Python stage id is deterministic).
  const a = fakeDeps();
  const b = fakeDeps();
  await fx.runFxHourly(a.deps);
  await fx.runFxHourly(b.deps);
  assert.deepEqual(a.calls.stage, b.calls.stage);
  assert.deepEqual(a.calls.commit, b.calls.commit);
});

test("one refused symbol does not stop the others, and the refusal is recorded in the stage freshness", async () => {
  // GBPJPY's quote currency (JPY) needs a JPY->USD conversion price, and neither JPYUSD nor USDJPY is collected.
  const { deps, calls } = fakeDeps({ subscriptions: ["fx:EURUSD", "fx:GBPJPY"],
    specs: [spec("fx:EURUSD"), spec("fx:GBPJPY", { digits: 3, point: 0.001, tickSize: 0.001 })] });
  const result = await fx.runFxHourly(deps);
  assert.equal(result.status, "ok");
  assert.deepEqual(result.evaluated, ["EURUSD"]);
  assert.match(result.failed["GBPJPY"], /no conversion price from JPY to USD/);
  assert.deepEqual(calls.stage[0].symbols.map((s) => s.symbol), ["EURUSD"]);
  assert.deepEqual(calls.commit[0][0].freshness, { refused: { GBPJPY: result.failed["GBPJPY"] }, evaluated: ["EURUSD"] });
  assert.match(calls.errors[0], /GBPJPY refused/);
  // The next stage starts its window before the incomplete one.
  const recent = [{ boundary_ms: BOUNDARY - HOUR, freshness: { refused: { GBPJPY: "x" } } },
    { boundary_ms: BOUNDARY - 2 * HOUR, freshness: { refused: {} } }];
  assert.equal(fx.decisionStart(BOUNDARY, recent), BOUNDARY - 2 * HOUR);
  assert.equal(fx.decisionStart(BOUNDARY, []), BOUNDARY - HOUR);
  assert.equal(fx.decisionStart(BOUNDARY, [{ boundary_ms: BOUNDARY - HOUR, freshness: {} }]), BOUNDARY - HOUR);
});

test("the request carries exact strings: venue symbol, contract, swaps in points, quote_to_account", async () => {
  const { deps, calls } = fakeDeps({ subscriptions: ["fx:EURUSD", "fx:EURJPY", "fx:USDJPY"],
    specs: [spec("fx:EURUSD"), spec("fx:EURJPY", { digits: 3, point: 0.001, tickSize: 0.001 }),
      spec("fx:USDJPY", { digits: 3, point: 0.001, tickSize: 0.001, swapMode: 0 })],
    bars: { "fx:USDJPY": [bar("fx:USDJPY", BOUNDARY, 157.123, 15)] } });
  await fx.runFxHourly(deps);
  const [eurusd, eurjpy, usdjpy] = calls.stage[0].symbols;
  assert.deepEqual(eurusd.venue_symbol, { venue: VENUE, native_symbol: "EURUSDm", digits: 5, tick_size: "0.00001" });
  assert.deepEqual(eurusd.contract, { contract_size: "100000", tick_size: "0.00001", min_lot: "0.01", lot_step: "0.01",
    base: "EUR", quote: "USD" });
  assert.deepEqual(eurusd.swap_rates, { long_per_unit: "-73/1000000", short_per_unit: "21/1000000" });
  assert.equal(eurusd.quote_to_account, "1");
  assert.deepEqual(Object.keys(eurusd.bars[0]).sort(), ["close", "high", "low", "native_symbol", "open", "open_time_ms",
    "spread_points", "tick_volume", "time_basis", "transport", "venue"]);
  // JPY -> USD through USDJPY's mid (157.123 + 15 points / 2 = 157.1305), inverted, exactly.
  assert.equal(eurjpy.quote_to_account, "2000/314261");
  assert.deepEqual(usdjpy.swap_rates, { long_per_unit: "0", short_per_unit: "0" });
  // The calendar is read point in time at the boundary.
  assert.equal(calls.events[0][2], BOUNDARY);
});

test("exact helpers and the forward route", () => {
  assert.equal(fx.exactDecimal(0.00001), "0.00001");
  assert.equal(fx.exactDecimal(1e-7), "0.0000001");
  assert.equal(fx.exactDecimal(1.5e-10), "0.00000000015");
  assert.equal(fx.exactDecimal(100000), "100000");
  assert.equal(fx.exactDecimal(1e21), "1000000000000000000000");
  assert.equal(fx.exactDecimal(-7.3), "-7.3");
  assert.deepEqual(fx.midFraction({ close: 1.085, spread_points: 12 }, 5), [217012n, 200000n]);
  const env = { PYTHON_ANALYSIS_ENABLED: "true", PYTHON_ANALYSIS_TOKEN: "t".repeat(40),
    PYTHON_ANALYSIS_URL: "http://127.0.0.1:8000", PYTHON_FORWARD_URL: "http://127.0.0.1:8001" };
  assert.equal(pythonServiceConfig("/v1/fx/forward/stage", env).url, "http://127.0.0.1:8001/v1/fx/forward/stage");
});

test("an unsupported swap mode or other broker currencies refuse that symbol only", async () => {
  const { deps } = fakeDeps({ subscriptions: ["fx:EURUSD", "fx:GBPUSD"],
    specs: [spec("fx:EURUSD", { swapMode: 4 }), spec("fx:GBPUSD", { baseCurrency: "USD" })] });
  const result = await fx.runFxHourly(deps);
  assert.equal(result.status, "failed");
  assert.match(result.failed["EURUSD"], /swap mode 4 is not supported yet/);
  assert.match(result.failed["GBPUSD"], /broker currencies USD\/USD differ from GBP\/USD/);
});

// ---------------------------------------------------------------- bar paging (#440)

/** A store with `count` contiguous 1m bars ending at BOUNDARY that honours limit, fromMs and the EXCLUSIVE toMs. */
function pagedStore(count, { overlap = false } = {}) {
  const all = Array.from({ length: count }, (_, i) => bar("fx:EURUSD", BOUNDARY - (count - 1 - i) * MIN));
  const calls = [];
  return {
    calls,
    deps: { store: { async getFxBars(venue, instrumentId, options = {}) {
      calls.push(options);
      let rows = all;
      if (options.fromMs !== undefined) rows = rows.filter((row) => row.open_time_ms >= options.fromMs);
      if (options.toMs !== undefined) rows = rows.filter((row) => (overlap ? row.open_time_ms <= options.toMs : row.open_time_ms < options.toMs));
      return rows.slice(-(options.limit ?? 1000));
    } } },
  };
}
const times = (rows) => rows.map((row) => row.open_time_ms);

test("warm-up constants: 403 candles of 240 minutes plus a day, capped at 60000 bars", () => {
  assert.equal(fx.FX_WARMUP_CANDLES_240, 403);
  assert.equal(fx.FX_WARMUP_MINUTES, 403 * 240 + 1440);
  assert.equal(fx.FX_MAX_BARS_PER_SYMBOL, 60_000);
  assert.equal(fx.FX_BAR_PAGE_SIZE, 20_000);
});

test("paging stops when the history ends (a short page)", async () => {
  const { deps, calls } = pagedStore(45_000);
  const bars = await fx.readBarsPaged(deps, VENUE, "fx:EURUSD", 60_000);
  assert.equal(bars.length, 45_000);
  assert.equal(calls.length, 3, "20000 + 20000 + a short 5000");
  assert.deepEqual(times(bars), Array.from({ length: 45_000 }, (_, i) => BOUNDARY - (44_999 - i) * MIN), "oldest first, contiguous");
  // A single short page is one read.
  const small = pagedStore(1_500);
  assert.equal((await fx.readBarsPaged(small.deps, VENUE, "fx:EURUSD", 60_000)).length, 1_500);
  assert.equal(small.calls.length, 1);
  // History that is an exact multiple of the page size costs one extra (empty) read, then ends.
  const exact = pagedStore(40_000);
  assert.equal((await fx.readBarsPaged(exact.deps, VENUE, "fx:EURUSD", 60_000)).length, 40_000);
  assert.equal(exact.calls.length, 3);
  assert.deepEqual(await fx.readBarsPaged(pagedStore(0).deps, VENUE, "fx:EURUSD", 60_000), []);
});

test("paging stops at neededBars and keeps the newest of them", async () => {
  const { deps, calls } = pagedStore(100_000);
  const bars = await fx.readBarsPaged(deps, VENUE, "fx:EURUSD", 30_000);
  assert.equal(calls.length, 2, "two pages reach 30000");
  assert.equal(bars.length, 30_000);
  assert.equal(bars.at(-1).open_time_ms, BOUNDARY);
  assert.equal(bars[0].open_time_ms, BOUNDARY - 29_999 * MIN);
  // Within one page nothing more is read.
  const one = pagedStore(100_000);
  assert.equal((await fx.readBarsPaged(one.deps, VENUE, "fx:EURUSD", 5_000)).length, 5_000);
  assert.equal(one.calls.length, 1);
});

test("toMs is exclusive: each page starts before the oldest row of the previous one and no row repeats", async () => {
  const { deps, calls } = pagedStore(50_000);
  const bars = await fx.readBarsPaged(deps, VENUE, "fx:EURUSD", 60_000);
  assert.deepEqual(calls[0], { limit: 20_000 });
  assert.deepEqual(calls[1], { limit: 20_000, toMs: BOUNDARY - 19_999 * MIN });
  assert.deepEqual(calls[2], { limit: 20_000, toMs: BOUNDARY - 39_999 * MIN });
  assert.equal(new Set(times(bars)).size, bars.length);
  assert.equal(bars.length, 50_000);
});

test("duplicate open times are dropped, and a page of only duplicates ends the paging", async () => {
  // A store whose upper bound is inclusive repeats the boundary row of every page.
  const overlap = pagedStore(45_000, { overlap: true });
  const bars = await fx.readBarsPaged(overlap.deps, VENUE, "fx:EURUSD", 60_000);
  assert.equal(new Set(times(bars)).size, bars.length);
  assert.equal(bars.length, 45_000);
  // A store that ignores toMs entirely returns the same full page: no endless loop.
  const stuck = { store: { async getFxBars() { return Array.from({ length: 20_000 }, (_, i) => bar("fx:EURUSD", BOUNDARY - (19_999 - i) * MIN)); } } };
  assert.equal((await fx.readBarsPaged(stuck, VENUE, "fx:EURUSD", 60_000)).length, 20_000);
});

test("the cap holds at 60000 bars whatever is asked", async () => {
  const { deps, calls } = pagedStore(100_000);
  const bars = await fx.readBarsPaged(deps, VENUE, "fx:EURUSD", 500_000);
  assert.equal(bars.length, 60_000);
  assert.equal(calls.length, 3);
  assert.equal(bars.at(-1).open_time_ms, BOUNDARY);
  // The stage asks for the full warm-up and so is bounded by the cap too.
  const long = fakeDeps({ bars: { "fx:EURUSD": undefined } });
  const all = Array.from({ length: 100_000 }, (_, i) => bar("fx:EURUSD", BOUNDARY - (99_999 - i) * MIN));
  long.deps.store.getFxBars = async (venue, instrumentId, options = {}) => {
    let rows = all;
    if (options.toMs !== undefined) rows = rows.filter((row) => row.open_time_ms < options.toMs);
    return rows.slice(-(options.limit ?? 1000));
  };
  assert.equal((await fx.runFxHourly(long.deps)).status, "ok");
  assert.equal(long.calls.stage[0].symbols[0].bars.length, 60_000);
});
