import assert from "node:assert/strict";
import { test } from "node:test";
import { createModuleLoader } from "./module-loader.mjs";

// #433: forex quote maths and the publisher pass with a fake store. No network, no database. Every bar
// and spec here is SYNTHETIC.
const moduleUrl = createModuleLoader(import.meta.url);
const calc = await import(await moduleUrl("../src/lib/fx/fx-quote-calc.ts"));
const publisher = await import(await moduleUrl("../src/lib/fx/fx-quote-publisher.server.ts"));

const MIN = 60_000;
const VENUE = "test-venue";
const MONDAY = Date.UTC(2026, 9, 19, 0, 0); // the FX week opened Sun 21:00 UTC
const NOON = Date.UTC(2026, 9, 21, 12, 7); // a Wednesday, not on a 15-minute boundary

function bar(openMs, close, { spread = 12, instrumentId = "fx:EURUSD" } = {}) {
  const symbol = instrumentId.split(":")[1];
  return { time_basis: "utc", venue: VENUE, instrument_id: instrumentId, symbol, native_symbol: `${symbol}m`,
    transport: "poll", open_time_ms: openMs, open: close, high: close, low: close, close, spread_points: spread,
    tick_volume: 10, received_at_ms: openMs + MIN + 500 };
}
/** `count` bars one minute apart ending at `endOpen`, closes from `closeOf(index)`. */
const run = (count, endOpen, closeOf, options) =>
  Array.from({ length: count }, (_, i) => bar(endOpen - (count - 1 - i) * MIN, closeOf(i), options));
function spec(overrides = {}) {
  return { venue: VENUE, instrumentId: "fx:EURUSD", nativeSymbol: "EURUSDm", digits: 5, point: 0.00001,
    tickSize: 0.00001, tickValue: 1, contractSize: 100000, volumeMin: 0.01, volumeMax: 200, volumeStep: 0.01,
    baseCurrency: "EUR", profitCurrency: "USD", marginCurrency: "EUR", accountCurrency: "USD", tradeMode: 4,
    swapMode: 1, swapLong: -7.3, swapShort: 2.1, swapRollover3days: 3, specHash: "a".repeat(64), observedAt: 0,
    ...overrides };
}
const near = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-9, `${actual} != ${expected}`);

test("ask is bid plus spread points on integer points, exactly", () => {
  assert.equal(calc.fxAsk(1.085, 12, 5), 1.08512);
  assert.equal(calc.fxAsk(1.08500, 0, 5), 1.085);
  assert.equal(calc.fxAsk(0.1, 2, 5), 0.10002); // 0.1 is not exact in binary; the result still is
  assert.equal(calc.fxAsk(2412.37, 25, 2), 2412.62); // a 2-digit metal
  assert.equal(calc.fxAsk(157.123, 15, 3), 157.138);
  const quote = calc.fxQuoteFromBars([bar(NOON, 1.085)], spec());
  assert.equal(quote.bid, 1.085);
  assert.equal(quote.ask, 1.08512);
  assert.equal(quote.spread_points, 12);
  assert.equal(quote.digits, 5);
  assert.deepEqual([quote.symbol, quote.instrument_id, quote.venue], ["EURUSD", "fx:EURUSD", VENUE]);
  assert.equal(quote.last_bar_at, new Date(NOON).toISOString());
  const gold = calc.fxQuoteFromBars([bar(NOON, 2412.37, { spread: 25, instrumentId: "fx:XAUUSD" })],
    spec({ instrumentId: "fx:XAUUSD", digits: 2, point: 0.01 }));
  assert.deepEqual([gold.bid, gold.ask], [2412.37, 2412.62]);
  // Bars of another instrument or venue than the spec never become a quote.
  assert.equal(calc.fxQuoteFromBars([bar(NOON, 1.085)], spec({ instrumentId: "fx:GBPUSD" })), null);
  assert.equal(calc.fxQuoteFromBars([bar(NOON, 1.085)], spec({ venue: "other-venue" })), null);
  assert.equal(calc.fxQuoteFromBars([], spec()), null);
});

test("change windows compare the bid with the latest bar at or before the target", () => {
  // 1500 contiguous minutes: closes 1.25 everywhere, newest 1.5, so every window is +20 %.
  const bars = run(1500, NOON, (i) => (i === 1499 ? 1.5 : 1.25));
  const changes = calc.fxChanges(bars);
  assert.deepEqual(Object.keys(changes), ["5", "15", "60", "240", "1440"]);
  for (const value of Object.values(changes)) near(value, 20);
  // A reference bar 45 minutes before the 240-minute target still counts (<= 60 min old); with only
  // older bars the window is null, never a stale comparison.
  const target240 = NOON - 240 * MIN;
  const gapped = [bar(target240 - 45 * MIN, 1.25), bar(NOON - 30 * MIN, 1.25), bar(NOON, 1.5)];
  const gappedChanges = calc.fxChanges(gapped);
  near(gappedChanges["240"], 20);
  assert.equal(gappedChanges["1440"], null);
  assert.equal(calc.fxChanges([bar(target240 - 61 * MIN, 1.25), bar(NOON, 1.5)])["240"], null);
  assert.equal(calc.fxChanges([bar(target240 - 60 * MIN, 1.25), bar(NOON, 1.5)])["240"] !== null, true);
  assert.deepEqual(calc.fxChanges([]), { 5: null, 15: null, 60: null, 240: null, 1440: null });
});

test("a weekend gap gives null for 1440 (and 240), never a Friday comparison", () => {
  const friday = run(360, Date.UTC(2026, 9, 16, 20, 59), () => 1.2);
  const sunday = run(181, MONDAY, (i) => (i === 180 ? 1.5 : 1.25)); // Sun 21:00 .. Mon 00:00
  const changes = calc.fxChanges([...friday, ...sunday]);
  assert.equal(changes["1440"], null);
  assert.equal(changes["240"], null);
  near(changes["60"], 20);
  near(changes["5"], 20);
});

test("the chart holds the last 1m close of each 15-minute bucket, oldest first, at most 96", () => {
  const bars = run(1600, NOON, (i) => 1 + i / 100000);
  const chart = calc.fxChart(bars);
  assert.equal(chart.length, 96);
  const lastBucket = Math.floor(NOON / (15 * MIN)) * 15 * MIN;
  assert.deepEqual(chart.at(-1), { time: lastBucket, close: bars.at(-1).close });
  assert.equal(chart[0].time, lastBucket - 95 * 15 * MIN);
  for (let i = 1; i < chart.length; i++) assert.equal(chart[i].time - chart[i - 1].time, 15 * MIN);
  // A full bucket's close is its last minute (open time bucket + 14 min).
  const full = chart.at(-2);
  assert.equal(full.close, bars.find((b) => b.open_time_ms === full.time + 14 * MIN).close);
  // Buckets without a bar are omitted.
  const sparse = [bar(NOON - 120 * MIN, 1.1), bar(NOON - 118 * MIN, 1.2), bar(NOON, 1.3)];
  assert.deepEqual(calc.fxChart(sparse).map((p) => p.close), [1.2, 1.3]);
  assert.deepEqual(calc.fxChart([]), []);
});

function fakeStore({ subscriptions = [], bars = {}, specs = [] } = {}) {
  const calls = { bars: [] };
  return {
    calls,
    store: {
      async getFxSubscriptions() { return subscriptions; },
      async getFxSymbolSpecs() { return specs; },
      async getFxBars(venue, instrumentId, options) {
        calls.bars.push([venue, instrumentId, options]);
        return bars[instrumentId] ?? [];
      },
    },
  };
}
const sub = (instrumentId) => ({ venue: VENUE, instrumentId, nativeSymbol: `${instrumentId.split(":")[1]}m` });

test("one batch per minute; an unchanged bar is not sent again; a failed write is retried", async () => {
  const fake = fakeStore({
    subscriptions: [sub("fx:EURUSD"), sub("fx:XAUUSD")],
    specs: [spec(), spec({ instrumentId: "fx:XAUUSD", digits: 2, point: 0.01 })],
    bars: { "fx:EURUSD": run(30, NOON, () => 1.085),
      "fx:XAUUSD": run(30, NOON, () => 2412.37, { instrumentId: "fx:XAUUSD", spread: 25 }) },
  });
  const sent = [];
  let fail = false;
  const deps = { store: fake.store, log: () => {}, async publish(batch) {
    if (fail) throw new Error("hosted down");
    sent.push(batch);
    return batch.length;
  } };
  const state = publisher.newFxPublishState();
  const first = await publisher.publishFxQuotesOnce(deps, state);
  assert.equal(sent.length, 1, "one batch");
  assert.deepEqual(sent[0].map((q) => q.symbol), ["EURUSD", "XAUUSD"]);
  assert.equal(first.published, 2);
  assert.deepEqual(fake.calls.bars[0][2], { limit: 2000 });
  // The same newest bar: nothing to send (the hosted function is forward-only as well).
  await publisher.publishFxQuotesOnce(deps, state);
  assert.equal(sent.length, 1);
  // A new bar: one batch again; a failed write is not remembered, so the next pass resends it.
  fake.store.getFxBars = async (venue, id) => run(31, NOON + MIN, () => (id === "fx:EURUSD" ? 1.086 : 2412.4),
    id === "fx:XAUUSD" ? { instrumentId: id } : {});
  fail = true;
  assert.equal((await publisher.publishFxQuotesOnce(deps, state)).writeError, "hosted down");
  fail = false;
  await publisher.publishFxQuotesOnce(deps, state);
  assert.equal(sent.length, 2);
  assert.equal(sent[1][0].bid, 1.086);
});

test("a symbol without a spec is skipped and logged once per process", async () => {
  const fake = fakeStore({ subscriptions: [sub("fx:EURUSD"), sub("fx:GBPUSD")], specs: [spec()],
    bars: { "fx:EURUSD": [bar(NOON, 1.085)], "fx:GBPUSD": [bar(NOON, 1.27, { instrumentId: "fx:GBPUSD" })] } });
  const logs = [];
  const sent = [];
  const deps = { store: fake.store, log: (line) => logs.push(line), async publish(batch) { sent.push(batch); return batch.length; } };
  const state = publisher.newFxPublishState();
  const first = await publisher.publishFxQuotesOnce(deps, state);
  await publisher.publishFxQuotesOnce(deps, state);
  assert.deepEqual(first.noSpec, ["GBPUSD"]);
  assert.deepEqual(logs, ["[fx-quote] GBPUSD: no symbol spec yet"]);
  assert.deepEqual(sent.map((batch) => batch.map((q) => q.symbol)), [["EURUSD"]]);
  assert.ok(!fake.calls.bars.some(([, id]) => id === "fx:GBPUSD"), "no bar read without a spec");
});

test("an empty or failing store publishes nothing", async () => {
  let published = 0;
  const publish = async (batch) => { published += batch.length; return batch.length; };
  const state = publisher.newFxPublishState();
  await publisher.publishFxQuotesOnce({ store: fakeStore().store, publish, log: () => {} }, state);
  const noBars = fakeStore({ subscriptions: [sub("fx:EURUSD")], specs: [spec()] });
  const run1 = await publisher.publishFxQuotesOnce({ store: noBars.store, publish, log: () => {} }, state);
  assert.deepEqual(run1.unavailable, ["EURUSD"]);
  const broken = { ...fakeStore().store, async getFxSubscriptions() { throw new Error("operational database down"); } };
  const run2 = await publisher.publishFxQuotesOnce({ store: broken, publish, log: () => {} }, state);
  assert.equal(run2.unavailable.length, 1);
  assert.equal(published, 0);
});

test("the hosted publish calls publish_fx_market_quotes and surfaces errors", async () => {
  const calls = [];
  const ok = publisher.createHostedFxPublish({ async rpc(name, args) { calls.push([name, args]); return { data: 2, error: null }; } });
  assert.equal(await ok([{ symbol: "EURUSD" }]), 2);
  assert.deepEqual(calls, [["publish_fx_market_quotes", { p_quotes: [{ symbol: "EURUSD" }] }]]);
  const bad = publisher.createHostedFxPublish({ async rpc() { return { data: null, error: { message: "denied" } }; } });
  await assert.rejects(bad([]), /denied/);
  assert.equal(publisher.startFxQuotePublisher({ env: {}, log: () => {} }), null, "needs hosted credentials");
});
