import assert from "node:assert/strict";
import { test } from "node:test";
import { createModuleLoader } from "./module-loader.mjs";

// #398: pure helpers of the forex Market page. Every quote here is SYNTHETIC.
const moduleUrl = createModuleLoader(import.meta.url);
const fx = await import(await moduleUrl("../src/lib/market/fx-quotes.ts"));

const MIN = 60_000;
const NOW = Date.UTC(2026, 9, 21, 12, 0, 30);

function row(overrides = {}) {
  return { symbol: "EURUSD", instrument_id: "fx:EURUSD", venue: "test-venue", bid: 1.085, ask: 1.08512,
    spread_points: 12, digits: 5, changes: { 5: 0.01, 15: null, 60: 0.12, 240: -0.3, 1440: null },
    chart: [{ time: NOW - 30 * MIN, close: 1.0849 }, { time: NOW - 15 * MIN, close: 1.085 }],
    last_bar_at: new Date(NOW - 90_000).toISOString(), published_at: new Date(NOW - 27_000).toISOString(), ...overrides };
}

test("toFxQuote maps the row and drops malformed values", () => {
  const quote = fx.toFxQuote(row(), NOW);
  assert.deepEqual(quote, {
    symbol: "EURUSD", instrumentId: "fx:EURUSD", venue: "test-venue", bid: 1.085, ask: 1.08512, spreadPoints: 12,
    digits: 5, changes: { 5: 0.01, 15: null, 60: 0.12, 240: -0.3, 1440: null },
    chart: [{ time: NOW - 30 * MIN, close: 1.0849 }, { time: NOW - 15 * MIN, close: 1.085 }],
    lastBarAt: NOW - 90_000, publishedAt: NOW - 27_000, ageMs: 90_000,
  });
  const messy = fx.toFxQuote(row({ changes: { 60: "0.1", 5: Number.NaN, 99: 1 }, chart: [{ time: 1 }, null, { time: 2, close: 3 }] }), NOW);
  assert.deepEqual(messy.changes, { 5: null, 15: null, 60: null, 240: null, 1440: null });
  assert.deepEqual(messy.chart, [{ time: 2, close: 3 }]);
  assert.equal(fx.toFxQuote(row({ changes: [] }), NOW).changes["60"], null);
  for (const bad of [{ bid: 0 }, { ask: 1.08 }, { last_bar_at: "not a time" }, { digits: 5.5 }, { bid: "1.085" }])
    assert.equal(fx.toFxQuote(row(bad), NOW), null, JSON.stringify(bad));
});

test("the four states, with the 3-minute edge", () => {
  const quote = fx.toFxQuote(row(), NOW);
  assert.equal(fx.fxQuoteState(null, NOW, true), "none");
  assert.equal(fx.fxQuoteState(undefined, NOW, false), "none");
  assert.equal(fx.fxQuoteState(quote, NOW, false), "closed");
  assert.equal(fx.fxQuoteState(quote, NOW, true), "live");
  const edge = fx.toFxQuote(row({ last_bar_at: new Date(NOW - 3 * MIN).toISOString() }), NOW);
  assert.equal(fx.fxQuoteState(edge, NOW, true), "live", "exactly 3 minutes is still live");
  assert.equal(fx.fxQuoteState(edge, NOW + 1, true), "stale");
  assert.equal(fx.FX_LIVE_MAX_AGE_MS, 3 * MIN);
});

test("the feed chip: no rows, closed, any live, or delayed", () => {
  assert.equal(fx.fxFeedState(["none", "none"], true), "none");
  assert.equal(fx.fxFeedState([], false), "none");
  assert.equal(fx.fxFeedState(["closed", "none"], false), "closed");
  assert.equal(fx.fxFeedState(["stale", "live", "none"], true), "live");
  assert.equal(fx.fxFeedState(["stale", "none"], true), "delayed");
});

test("formatPrice uses fixed digits and never an exponent", () => {
  assert.equal(fx.formatPrice(1.085, 5), "1.08500");
  assert.equal(fx.formatPrice(1.08512, 5), "1.08512");
  assert.equal(fx.formatPrice(2412.37, 2), "2412.37");
  assert.equal(fx.formatPrice(0.0000123, 7), "0.0000123");
  assert.equal(fx.formatPrice(157.1, 3), "157.100");
  assert.equal(fx.formatPrice(Number.NaN, 5), "–");
  assert.deepEqual(fx.splitPrice("1.08512", 5), ["1.085", "12"]);
  assert.deepEqual(fx.splitPrice("2412.37", 2), ["2412.", "37"]);
  assert.deepEqual(fx.splitPrice("5432", 0), ["5432", ""]);
});

test("spread: 3- and 5-digit symbols in pips, others in points", () => {
  assert.deepEqual(fx.spreadPips(12, 5), { value: 1.2, unit: "pips" });
  assert.deepEqual(fx.spreadPips(15, 3), { value: 1.5, unit: "pips" });
  assert.deepEqual(fx.spreadPips(25, 2), { value: 25, unit: "pts" });
  assert.deepEqual(fx.spreadPips(3, 1), { value: 3, unit: "pts" });
  assert.equal(fx.formatSpread(12, 5), "1.2 pips");
  assert.equal(fx.formatSpread(10, 5), "1.0 pips");
  assert.equal(fx.formatSpread(25, 2), "25 pts");
});

test("change tone and text from the sign", () => {
  assert.equal(fx.changeTone(0.12), "bull");
  assert.equal(fx.changeTone(-0.12), "bear");
  assert.equal(fx.changeTone(0), "muted");
  assert.equal(fx.changeTone(null), "muted");
  assert.equal(fx.changeTone(Number.NaN), "muted");
  assert.equal(fx.formatChange(-0.1234), "0.12%");
  assert.equal(fx.formatChange(null), null);
});
