import assert from "node:assert/strict";
import { test } from "node:test";
import { createModuleLoader } from "./module-loader.mjs";

// #323: shared quote maths, the published-row mapping with its stale/empty/missing states, and the
// publisher's skip-if-not-advanced logic. Pure functions and in-memory fakes only; no network.
const moduleUrl = createModuleLoader(import.meta.url);
const calc = await import(await moduleUrl("../src/lib/market/quote-calc.ts"));
const published = await import(await moduleUrl("../src/lib/market/published-quotes.ts"));
const publisher = await import(await moduleUrl("../src/lib/market/quote-publisher.server.ts"));
const { isUnavailable, healDelayMs, nextHealDelay, mergeHealed } = await import(await moduleUrl("../src/lib/market/self-heal.ts"));
// Inert placeholders only; all hosted writes in these tests use in-memory fakes.
const hostedEnv = { SUPABASE_URL: "https://example.invalid", SUPABASE_SERVICE_ROLE_KEY: "test-placeholder" };


const MIN = 60_000;
const NOW = Date.UTC(2026, 9, 13, 12, 0, 30);
const LAST_OPEN = Math.floor(NOW / MIN) * MIN - MIN; // newest completed 1m candle

// The pre-#323 implementation, copied verbatim from quotes.server.ts, as the parity reference.
function oldPctFrom(series, barsBack) {
  if (series.length <= barsBack) return null;
  const last = series[series.length - 1];
  const prev = series[series.length - 1 - barsBack];
  if (!last || !prev || !prev.close) return null;
  return ((last.close - prev.close) / prev.close) * 100;
}

function candles(count, stepMs, endOpen, price) {
  return Array.from({ length: count }, (_, i) => ({
    time: endOpen - (count - 1 - i) * stepMs,
    close: price(i),
  }));
}
const wave = (i) => 100 + Math.sin(i / 7) * 3 + i * 0.05;

function healQuote(symbol, patch = {}) {
  return {
    symbol, ok: true, source: "binance-usdm", price: 100, changes: {}, chart: [],
    lastCandleAt: LAST_OPEN, stale: false, error: null, fallbackReasons: [],
    fetchedAt: new Date(NOW).toISOString(), ...patch,
  };
}

test("heal delay backs off to a minute with bounded integer jitter", () => {
  const delays = [2_000, 4_000, 8_000, 15_000, 30_000, 60_000, 60_000, 60_000];
  delays.forEach((base, attempt) => {
    assert.equal(healDelayMs(attempt, () => 0.5), base);
    assert.equal(healDelayMs(attempt, () => 0), base * 0.8);
    assert.equal(healDelayMs(attempt, () => 1), base * 1.2);
    assert.ok(Number.isInteger(healDelayMs(attempt, () => 0.12345)));
  });
  assert.equal(healDelayMs(10_000, () => 0.5), 60_000);
});

test("next heal delay starts at two seconds and counts successful and failed requests", () => {
  const rand = () => 0.5;
  assert.equal(nextHealDelay({ dataUpdateCount: 0, errorUpdateCount: 0 }, rand), 2_000);
  assert.equal(nextHealDelay({ dataUpdateCount: 1, errorUpdateCount: 0 }, rand), 4_000);
  assert.equal(nextHealDelay({ dataUpdateCount: 0, errorUpdateCount: 1 }, rand), 4_000);
  assert.equal(nextHealDelay({ dataUpdateCount: 2, errorUpdateCount: 1 }, rand), 15_000);
  assert.equal(nextHealDelay({ dataUpdateCount: 0, errorUpdateCount: 8 }, rand), 60_000);
});

test("mergeHealed repairs fresh matching pairs without erasing others or advancing fetchedAt", () => {
  const current = {
    quotes: [healQuote("BTCUSDT", { stale: true }), healQuote("ETHUSDT"), healQuote("SOLUSDT", { ok: false })],
    fetchedAt: new Date(NOW - MIN).toISOString(), publishedAt: new Date(NOW - MIN).toISOString(),
  };
  const before = structuredClone(current);
  const refreshed = healQuote("BTCUSDT", { source: "kraken-futures", price: 110 });
  const healed = {
    quotes: [refreshed, healQuote("SOLUSDT", { stale: true }), healQuote("XRPUSDT")],
    fetchedAt: new Date(NOW + MIN).toISOString(), publishedAt: new Date(NOW).toISOString(),
  };
  const result = mergeHealed(current, healed);
  assert.equal(result.quotes[0], refreshed);
  assert.equal(result.quotes[1], current.quotes[1]);
  assert.equal(result.quotes[2], current.quotes[2]);
  assert.equal(result.quotes.length, 3);
  assert.equal(result.fetchedAt, current.fetchedAt);
  assert.equal(result.publishedAt, healed.publishedAt);
  assert.deepEqual(current, before);
  assert.equal(mergeHealed(current, { ...healed, publishedAt: new Date(NOW - 2 * MIN).toISOString() }).publishedAt, current.publishedAt);
  assert.equal(mergeHealed({ ...current, publishedAt: null }, healed).publishedAt, healed.publishedAt);
  assert.equal(mergeHealed(current, { ...healed, publishedAt: null }).publishedAt, current.publishedAt);
});

test("unavailable heal results and unrelated symbols leave the original snapshot untouched", () => {
  const current = { quotes: [healQuote("BTCUSDT", { stale: true })], fetchedAt: "original", publishedAt: null };
  for (const patch of [{ ok: false }, { price: null }, { stale: true }]) {
    const failed = healQuote("BTCUSDT", patch);
    assert.equal(isUnavailable(failed), true);
    assert.equal(mergeHealed(current, { ...current, quotes: [failed] }), current);
  }
  assert.equal(isUnavailable(healQuote("BTCUSDT")), false);
  assert.equal(mergeHealed(current, { ...current, quotes: [healQuote("ETHUSDT")] }), current);
  assert.equal(mergeHealed(undefined, current), undefined);
});

test("hosted publish forwards the batch, returns the row count and propagates RPC errors", async () => {
  const batch = [published.quoteInputFromFigures("BTCUSDT", calc.quoteFigures(
    candles(61, MIN, LAST_OPEN, wave),
    candles(97, 15 * MIN, LAST_OPEN, wave),
  ))];
  const calls = [];
  let result = { data: 1, error: null };
  const publish = publisher.createHostedPublish({
    rpc: async (...args) => { calls.push(args); return result; },
  });
  assert.equal(await publish(batch), 1);
  assert.deepEqual(calls, [["publish_market_quotes", { p_quotes: batch }]]);
  assert.equal(calls[0][1].p_quotes, batch);
  result = { data: null, error: { message: "hosted write rejected" } };
  await assert.rejects(publish(batch), { message: "hosted write rejected" });
});

test("shared quote maths gives the same numbers as the previous pctFrom", () => {
  for (const [minuteCount, quarterCount] of [[61, 97], [61, 40], [30, 97], [3, 2], [1, 1]]) {
    const minute = candles(minuteCount, MIN, LAST_OPEN, wave);
    const quarter = candles(quarterCount, 15 * MIN, LAST_OPEN - 14 * MIN, (i) => wave(i * 3));
    const figures = calc.quoteFigures(minute, quarter);
    assert.deepEqual(figures.changes, {
      5: oldPctFrom(minute, 5),
      15: oldPctFrom(minute, 15),
      60: oldPctFrom(minute, 60),
      240: oldPctFrom(quarter, 16),
      1440: oldPctFrom(quarter, 96),
    });
    assert.equal(figures.price, minute.at(-1).close);
    assert.equal(figures.lastCandleAt, minute.at(-1).time);
    assert.deepEqual(figures.chart, quarter.map(({ time, close }) => ({ time, close })));
  }
  assert.equal(oldPctFrom([{ time: 1, close: 0 }, { time: 2, close: 5 }], 1), null);
  assert.equal(calc.pctFrom([{ time: 1, close: 0 }, { time: 2, close: 5 }], 1), null);
});

test("longer series are cut to the bars the dashboard always used", () => {
  const minute = candles(300, MIN, LAST_OPEN, wave);
  const quarter = candles(260, 15 * MIN, LAST_OPEN, wave);
  const figures = calc.quoteFigures(minute, quarter);
  assert.equal(figures.chart.length, calc.QUOTE_QUARTER_BARS);
  assert.equal(figures.changes[60], oldPctFrom(minute.slice(-61), 60));
  assert.equal(figures.changes[1440], oldPctFrom(quarter.slice(-97), 96));
  assert.equal(calc.quoteFigures([], quarter), null);
  assert.equal(calc.quoteFigures([{ time: 1, close: 0 }], quarter), null);
});

function rowFor(symbol, overrides = {}) {
  const input = published.quoteInputFromFigures(
    symbol,
    calc.quoteFigures(candles(61, MIN, LAST_OPEN, wave), candles(97, 15 * MIN, LAST_OPEN, wave)),
  );
  return { ...input, published_at: new Date(NOW - 20_000).toISOString(), ...overrides };
}

test("a published row maps to the existing SymbolQuote shape", () => {
  const row = rowFor("BTCUSDT");
  const quote = published.quoteFromRow(row, NOW);
  assert.equal(quote.ok, true);
  assert.equal(quote.source, "binance-usdm");
  assert.equal(quote.symbol, "BTCUSDT");
  assert.equal(quote.price, row.price);
  assert.equal(quote.lastCandleAt, LAST_OPEN);
  assert.equal(quote.stale, false);
  assert.equal(quote.error, null);
  assert.deepEqual(quote.fallbackReasons, []);
  assert.deepEqual(Object.keys(quote.changes).map(Number), [5, 15, 60, 240, 1440]);
  assert.equal(quote.chart.length, 97);
  // The JSON round trip the database performs changes nothing.
  const viaJson = published.quoteFromRow(JSON.parse(JSON.stringify(row)), NOW);
  assert.deepEqual(viaJson, quote);
});

test("stale is decided from the candle time and from published_at", () => {
  const old = new Date(NOW - 11 * MIN).toISOString();
  assert.equal(published.quoteFromRow(rowFor("BTCUSDT", { last_candle_at: old }), NOW).stale, true);
  assert.equal(published.quoteFromRow(rowFor("BTCUSDT", { published_at: old }), NOW).stale, true);
  const edge = new Date(NOW - 10 * MIN).toISOString();
  assert.equal(published.quoteFromRow(rowFor("BTCUSDT", { published_at: edge }), NOW).stale, false);
});

test("unreadable or foreign-venue rows fail visibly instead of showing prices", () => {
  const bad = [
    rowFor("BTCUSDT", { source: "kraken-futures" }),
    rowFor("BTCUSDT", { source: "okx-usdt-swap" }),
    rowFor("BTCUSDT", { price_type: "mark" }),
    rowFor("BTCUSDT", { changes: [] }),
    rowFor("BTCUSDT", { changes: { 5: "x", 15: 0, 60: 0, 240: 0, 1440: 0 } }),
    rowFor("BTCUSDT", { chart: [{ time: 1 }] }),
    rowFor("BTCUSDT", { last_candle_at: "not a date" }),
    rowFor("BTCUSDT", { price: 0 }),
  ];
  for (const row of bad) {
    const quote = published.quoteFromRow(row, NOW);
    assert.equal(quote.ok, false);
    assert.equal(quote.price, null);
    assert.equal(quote.source, null);
    assert.ok(quote.error);
  }
});

test("snapshot keeps the requested order and reports missing, empty and stale states", () => {
  const rows = [rowFor("ETHUSDT"), rowFor("BTCUSDT", { published_at: new Date(NOW - 5_000).toISOString() })];
  const snapshot = published.snapshotFromRows(["BTCUSDT", "SOLUSDT", "ETHUSDT"], rows, NOW);
  assert.deepEqual(snapshot.quotes.map((q) => [q.symbol, q.ok]), [["BTCUSDT", true], ["SOLUSDT", false], ["ETHUSDT", true]]);
  assert.match(snapshot.quotes[1].error, /Not collected yet/);
  assert.equal(snapshot.quotes[1].source, null);
  assert.equal(snapshot.publishedAt, new Date(NOW - 5_000).toISOString());
  assert.equal(published.publishedFreshness(snapshot), "fresh");

  const empty = published.snapshotFromRows(["BTCUSDT"], [], NOW);
  assert.equal(empty.publishedAt, null);
  assert.equal(empty.quotes[0].ok, false);
  assert.equal(published.publishedFreshness(empty), "empty");

  const old = new Date(NOW - 30 * MIN).toISOString();
  const stale = published.snapshotFromRows(
    ["BTCUSDT", "ETHUSDT"],
    [rowFor("BTCUSDT", { published_at: old }), rowFor("ETHUSDT", { published_at: old })],
    NOW,
  );
  assert.equal(published.publishedFreshness(stale), "stale");
  assert.ok(stale.quotes.every((q) => q.ok && q.stale && q.source === "binance-usdm"));
});

test("only quotes whose newest candle advanced are published", () => {
  const a = rowFor("BTCUSDT");
  const b = rowFor("ETHUSDT");
  const sent = new Map();
  assert.deepEqual(published.quotesToPublish([a, b], sent).map((q) => q.symbol), ["BTCUSDT", "ETHUSDT"]);
  sent.set("BTCUSDT", Date.parse(a.last_candle_at));
  sent.set("ETHUSDT", Date.parse(b.last_candle_at) - MIN);
  assert.deepEqual(published.quotesToPublish([a, b], sent).map((q) => q.symbol), ["ETHUSDT"]);
  sent.set("ETHUSDT", Date.parse(b.last_candle_at));
  assert.deepEqual(published.quotesToPublish([a, b], sent), []);
});

test("freshness checks warn past 10 minutes and for missing rows", () => {
  const checks = published.publishFreshnessChecks(
    ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    [
      { symbol: "BTCUSDT", published_at: new Date(NOW - 30_000).toISOString() },
      { symbol: "ETHUSDT", published_at: new Date(NOW - 11 * MIN).toISOString() },
    ],
    NOW,
  );
  assert.deepEqual(checks.map((c) => c.level), ["PASS", "WARN", "WARN"]);
  assert.match(checks[0].detail, /30 s ago/);
  assert.match(checks[1].detail, /older than 10 min/);
  assert.match(checks[2].detail, /no row/);
});

function fakeStore(state) {
  return {
    async readCollectorSubscriptions() {
      return state.symbols;
    },
    async readCollectorCompletedCandles(symbol, timeframe, limit) {
      if (state.failing?.has(symbol)) throw new Error("local read failed");
      const open = state.lastOpen[symbol];
      if (open === undefined) return { observations: [] };
      const step = timeframe * MIN;
      const list = candles(limit, step, open - (timeframe === 1 ? 0 : 14 * MIN), wave);
      return { observations: list.map((c) => ({ candle: { openTime: c.time, close: c.close } })) };
    },
  };
}

test("publisher sends a batch once, skips unchanged symbols and retries failed writes", async () => {
  const state = { symbols: ["BTCUSDT", "ETHUSDT"], lastOpen: { BTCUSDT: LAST_OPEN, ETHUSDT: LAST_OPEN } };
  const batches = [];
  let failWrites = false;
  const deps = {
    store: fakeStore(state),
    now: () => NOW,
    async publish(quotes) {
      if (failWrites) throw new Error("hosted down");
      batches.push(quotes.map((q) => q.symbol));
      return quotes.length;
    },
  };
  const memory = publisher.newPublishState();

  failWrites = true;
  let run = await publisher.publishQuotesOnce(deps, memory);
  assert.equal(run.writeError, "hosted down");
  assert.equal(run.published, 0);
  assert.equal(memory.lastPublished.size, 0, "a failed write is not remembered");

  failWrites = false;
  run = await publisher.publishQuotesOnce(deps, memory);
  assert.deepEqual(batches, [["BTCUSDT", "ETHUSDT"]]);
  assert.equal(run.published, 2);
  assert.deepEqual(run.behind, []);

  run = await publisher.publishQuotesOnce(deps, memory);
  assert.equal(run.published, 0);
  assert.equal(batches.length, 1, "no hosted call when no candle advanced");

  state.lastOpen.ETHUSDT = LAST_OPEN + MIN;
  const later = { ...deps, now: () => NOW + MIN };
  run = await publisher.publishQuotesOnce(later, memory);
  assert.deepEqual(batches.at(-1), ["ETHUSDT"]);
  assert.deepEqual(run.behind, ["BTCUSDT"], "BTCUSDT has not stored the new candle yet");
});

test("publisher reports unavailable symbols and never throws", async () => {
  const state = {
    symbols: ["BTCUSDT", "NEWUSDT", "ETHUSDT"],
    lastOpen: { BTCUSDT: LAST_OPEN, ETHUSDT: LAST_OPEN },
    failing: new Set(["ETHUSDT"]),
  };
  const batches = [];
  const deps = {
    store: fakeStore(state),
    now: () => NOW,
    async publish(quotes) {
      batches.push(quotes.map((q) => q.symbol));
      return quotes.length;
    },
  };
  const run = await publisher.publishQuotesOnce(deps, publisher.newPublishState());
  assert.deepEqual(batches, [["BTCUSDT"]]);
  assert.deepEqual(run.unavailable.sort(), ["ETHUSDT", "NEWUSDT"]);

  const broken = { ...deps, store: { ...deps.store, readCollectorSubscriptions: async () => { throw new Error("store down"); } } };
  const failed = await publisher.publishQuotesOnce(broken, publisher.newPublishState());
  assert.equal(failed.published, 0);
  assert.match(failed.unavailable[0], /store down/);
});

test("the standalone publisher keeps its timer referenced until stop clears it", (t) => {
  let referenced = true;
  const timer = {
    hasRef: () => referenced,
    unref: t.mock.fn(() => { referenced = false; }),
  };
  const pending = new Set();
  const schedule = t.mock.method(globalThis, "setTimeout", () => {
    pending.add(timer);
    return timer;
  });
  const clear = t.mock.method(globalThis, "clearTimeout", (handle) => pending.delete(handle));
  const running = publisher.startMarketQuotePublisher({
    env: hostedEnv,
    deps: { store: fakeStore({ symbols: [], lastOpen: {} }), publish: async () => 0 },
    log: () => {},
  });
  try {
    assert.ok(running);
    assert.equal(schedule.mock.callCount(), 1);
    assert.equal(typeof schedule.mock.calls[0].arguments[0], "function");
    assert.equal(schedule.mock.calls[0].arguments[1], 5_000);
    assert.equal(pending.has(timer), true);
    assert.equal(timer.hasRef(), true, "the standalone process must stay alive");
    assert.equal(timer.unref.mock.callCount(), 0);

    running.stop();
    assert.equal(clear.mock.callCount(), 1);
    assert.equal(clear.mock.calls[0].arguments[0], timer);
    assert.equal(pending.size, 0, "stop removes the pending timer so the process can exit");
  } finally {
    running?.stop();
  }
});

test("the publisher declines to start without hosted credentials and never throws", () => {
  const lines = [];
  const options = (env) => ({ env, log: (l) => lines.push(l), error: (l) => lines.push(l) });
  assert.equal(publisher.startMarketQuotePublisher(options({})), null);
  assert.match(lines.at(-1), /SUPABASE_SERVICE_ROLE_KEY are required/);
  assert.equal(publisher.startMarketQuotePublisher(options({ MARKET_QUOTE_PUBLISHER_ENABLED: "false" })), null);
  assert.match(lines.at(-1), /disabled/);
  const running = publisher.startMarketQuotePublisher({
    ...options(hostedEnv),
    deps: { store: fakeStore({ symbols: [], lastOpen: {} }), publish: async () => 0 },
  });
  assert.equal(publisher.startMarketQuotePublisher({
    ...options({}),
    deps: { store: fakeStore({ symbols: [], lastOpen: {} }), publish: async () => 0 },
  }), null);
  assert.match(lines.at(-1), /SUPABASE_SERVICE_ROLE_KEY are required/);
  assert.equal(publisher.startMarketQuotePublisher(options(hostedEnv)), null);
  assert.match(lines.at(-1), /no operational store was provided/);
  assert.ok(running);
  running.stop();
});
