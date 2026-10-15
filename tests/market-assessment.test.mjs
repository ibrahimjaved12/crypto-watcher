// "Current assessment" view models (#30): market strip, per-pair movement, TA per timeframe, freshness.
// Every number here is SYNTHETIC test data; the mappers invent no thresholds (they use the engine's own).
import assert from "node:assert/strict";
import { test } from "node:test";

import { createModuleLoader } from "./module-loader.mjs";

const load = createModuleLoader(import.meta.url);
const a = await import(load("../src/lib/market/assessment.ts"));

const NOW = Date.parse("2026-10-16T12:00:00Z");
const SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"];
const CONFIG = "market-movement-config-v1";
const count = (n, den = 6) => ({ available: true, value: { count: n, fraction: n / den } });
const num = (value) => ({ available: true, value });
const off = (reason) => ({ available: false, value: null, reason });
const Z = { BTCUSDT: -1.4, ETHUSDT: 0.2, SOLUSDT: 0.7, BNBUSDT: -0.1, XRPUSDT: -1.1, DOGEUSDT: -2.0 };

function symbolMetrics(symbol, { excluded = false, outlier = false } = {}) {
  const z = Z[symbol];
  return { symbol, included: !excluded, exclusionReasons: excluded ? ["STALE_LAST_TRADE"] : [],
    normalizedZ: excluded ? off("STALE_LAST_TRADE") : num(z), materialRising: !excluded && z >= 1, materialFalling: !excluded && z <= -1,
    rvol: excluded ? off("SYMBOL_EXCLUDED") : num(1.8), outlierCandidate: outlier };
}

function win(minutes, { fallingMaterial = 4, excluded = [], configVersion = CONFIG, outliers = [], breadthOk = true, stream = {} } = {}) {
  const den = 6 - excluded.length;
  return {
    windowMinutes: minutes, horizonRole: "PRIMARY", isPrimary: minutes === 5, directionState: "BROAD_DROP",
    pace: { available: true, value: "ACCELERATING", reason: null }, isolatedOutliers: outliers,
    breadth: breadthOk
      ? { available: true, unavailableReason: null, denominator: den, flat: count(1, den), rising: count(1, den), falling: count(4, den),
          materialRising: count(0, den), materialFalling: count(fallingMaterial, den) }
      : { available: false, unavailableReason: "MARKET_UNIVERSE_INELIGIBLE", denominator: 0 },
    medianRawReturn: num(-0.004), medianNormalizedMovement: num(-1.2), dispersionMadNormalizedMovement: num(0.6),
    availabilityReasons: breadthOk ? [] : ["MARKET_UNIVERSE_INELIGIBLE"], movementConfigVersion: configVersion,
    sourceTimeEvidence: SYMBOLS.map((symbol) => ({ symbol, lastRealTradeTimeMs: NOW - 5000, lastRealEventTimeMs: NOW - 5000,
      lastReceivedAtMs: symbol in stream ? stream[symbol] : NOW - 4000 })),
    movementSnapshot: { symbols: SYMBOLS.map((s) => symbolMetrics(s, { excluded: excluded.includes(s), outlier: outliers.some((o) => o.symbol === s) })) },
  };
}

function state({ status = "LIVE", boundaryAgo = 10_000, symbols = SYMBOLS, excluded = [], direction = "BROAD_DROP", ...windowOptions } = {}) {
  const context = [1, 5, 15].map((m) => win(m, { excluded, ...windowOptions }));
  return {
    available: true, status,
    universe: { id: "u", version: "v", configuredSymbols: symbols, includedSymbols: symbols.filter((s) => !excluded.includes(s)),
      excludedSymbols: excluded.map((symbol) => ({ symbol, reasons: ["STALE_LAST_TRADE"] })) },
    primary: { windowMinutes: 5, directionState: direction, pace: "ACCELERATING", reversalCandidate: false, horizonRole: "PRIMARY", evidence: context[1] },
    context,
    timestamps: { evaluationBoundaryTime: NOW - boundaryAgo, lastSourceEventTime: NOW - 6000, lastTradeTime: NOW - 6000,
      lastReceivedAt: NOW - 4000, engineUpdatedAt: NOW - 3000 },
    mostRecentTransition: null,
  };
}
const empty = { available: false, status: "UNAVAILABLE", universe: null, primary: null, context: [], timestamps: null };

// ---------------------------------------------------------------- market strip

test("LIVE: breadth sentence uses the engine's own material threshold, medians, universe and times", () => {
  const view = a.buildMarketStrip(state(), NOW);
  assert.deepEqual([view.status, view.tone, view.statusLabel, view.current], ["LIVE", "live", "Live", true]);
  assert.deepEqual([view.direction.label, view.pace], ["Broad drop", "speeding up"]);
  const w15 = view.windows.find((w) => w.minutes === 15);
  assert.ok(w15.sentence.startsWith("4 of 6 pairs fell materially in 15 min"));
  assert.match(w15.sentence, /1 rising, 4 falling of 6/);
  assert.match(w15.materialNote, /at least 1 usual-sized moves/); // read from the config of the saved evidence
  assert.match(w15.materialNote, /flat is under 0.5/);
  assert.ok(Math.abs(w15.medianMovePct - -0.3992) < 0.001);
  assert.deepEqual([w15.medianNormalized, w15.dispersion], [-1.2, 0.6]);
  assert.deepEqual(view.windows.map((w) => w.minutes), [1, 5, 15]);
  assert.equal(view.universe.included.length, 6);
  assert.deepEqual(view.times, { evaluationBoundary: NOW - 10_000, lastReceivedAt: NOW - 4000, lastSourceEvent: NOW - 6000, engineUpdatedAt: NOW - 3000 });
  assert.match(view.explanation, /Evaluated 10 s ago on 6 of 6/);
});

test("a saved config this build does not know gets no invented threshold", () => {
  const view = a.buildMarketStrip(state({ configVersion: "market-movement-config-v9" }), NOW);
  assert.match(view.windows[0].materialNote, /does not know the value/);
  assert.doesNotMatch(view.windows[0].materialNote, /at least 1/);
  assert.equal(a.engineThresholds("market-movement-config-v9"), null);
});

test("no pair moved materially, and a window without breadth, are stated plainly", () => {
  const calm = a.buildMarketStrip(state({ fallingMaterial: 0 }), NOW);
  assert.match(calm.windows[0].sentence, /^No pair moved materially in 1 min/);
  const none = a.buildMarketStrip(state({ breadthOk: false }), NOW);
  assert.equal(none.windows[0].available, false);
  assert.match(none.windows[0].sentence, /market universe ineligible/i);
});

test("isolated outliers are listed with direction and move", () => {
  const view = a.buildMarketStrip(state({ outliers: [{ symbol: "DOGEUSDT", direction: "FALLING", rawReturn: -0.02, historicalZ: -2, crossSectionalZ: -4,
    sameDirectionBreadthCount: 1, sameDirectionBreadthFraction: 0.2, sameDirectionBreadthDenominator: 5 }] }), NOW);
  assert.deepEqual(view.windows[1].outliers.map((o) => [o.symbol, o.direction, Math.round(o.movePct * 100) / 100]), [["DOGEUSDT", "falling", -1.98]]);
});

test("WARMING is provisional, never presented as live", () => {
  const view = a.buildMarketStrip(state({ status: "WARMING", direction: "WARMING" }), NOW);
  assert.deepEqual([view.tone, view.statusLabel, view.current], ["warming", "Warming up", false]);
  assert.match(view.explanation, /collecting live 5-second history/);
});

test("STALE keeps the old numbers but says so, and is never shown as live", () => {
  const view = a.buildMarketStrip(state({ status: "STALE", boundaryAgo: 5 * 60_000 }), NOW);
  assert.deepEqual([view.tone, view.statusLabel, view.current], ["stale", "Stale", false]);
  assert.match(view.explanation, /Not live: the last evaluation is 5 min old/);
  assert.ok(view.windows.length === 3); // the as-of numbers remain visible
  assert.notEqual(view.statusLabel, "Live");
});

test("UNAVAILABLE says why: store not reachable, read failed, not loaded, nothing saved, fewer than 5 contracts", () => {
  const disabled = a.buildMarketStrip(empty, NOW, "store_disabled");
  assert.deepEqual([disabled.tone, disabled.windows.length], ["unavailable", 0]);
  assert.match(disabled.explanation, /cannot reach the market engine/);
  assert.match(a.buildMarketStrip(empty, NOW, "read_failed").explanation, /could not be read/);
  assert.match(a.buildMarketStrip(empty, NOW, "not_loaded").explanation, /Not loaded yet/);
  assert.match(a.buildMarketStrip(empty, NOW, "ok").explanation, /not saved a market state yet/);
  const few = state({ status: "UNAVAILABLE", symbols: SYMBOLS.slice(0, 4) });
  const view = a.buildMarketStrip(few, NOW);
  assert.equal(view.status, "UNAVAILABLE");
  assert.match(view.explanation, /at least 5 watched contracts; the engine has 4/);
  assert.equal(view.universe.configured.length, 4);
  // an engine that says LIVE is shown as unavailable when the page could not read it
  assert.equal(a.buildMarketStrip(state(), NOW, "read_failed").status, "UNAVAILABLE");
});

test("an excluded symbol is named with its reason in the universe", () => {
  const view = a.buildMarketStrip(state({ excluded: ["DOGEUSDT"] }), NOW);
  assert.deepEqual(view.universe.excluded, [{ symbol: "DOGEUSDT", reasons: ["Stale last trade"] }]);
  assert.equal(view.universe.included.length, 5);
  assert.match(view.windows[2].sentence, /of 5 pairs/);
});

// ---------------------------------------------------------------- per pair

test("per-pair movement: normalized move with the engine's bands, volume and outlier badge", () => {
  const s = state({ outliers: [{ symbol: "BTCUSDT", direction: "FALLING", rawReturn: -0.01, historicalZ: -2, crossSectionalZ: -4,
    sameDirectionBreadthCount: 1, sameDirectionBreadthFraction: 0.2, sameDirectionBreadthDenominator: 5 }] });
  const btc = a.buildPairMovement("BTCUSDT", s);
  assert.equal(btc.kind, "ok");
  assert.deepEqual(btc.windows.map((w) => [w.minutes, w.zText, w.label, w.tone, w.material]),
    [[1, "-1.40", "unusually sharp drop", "down", true], [5, "-1.40", "unusually sharp drop", "down", true], [15, "-1.40", "unusually sharp drop", "down", true]]);
  assert.equal(btc.rvol.text, "1.80× its usual volume");
  assert.deepEqual([btc.outlier.direction, btc.outlier.window], ["falling", 5]);
  const eth = a.buildPairMovement("ETHUSDT", s);
  assert.deepEqual([eth.windows[0].label, eth.windows[0].tone], ["about normal", "flat"]);
  assert.equal(eth.outlier, null);
  assert.equal(a.buildPairMovement("SOLUSDT", s).windows[0].label, "a little stronger than usual");
  assert.equal(a.buildPairMovement("BTCUSDT", state({ configVersion: "market-movement-config-v9" })).windows[0].label, "below its normal (config unknown)");
});

test("an excluded symbol shows its reasons; a symbol outside the universe and an unreadable engine are explicit", () => {
  const s = state({ excluded: ["DOGEUSDT"] });
  const doge = a.buildPairMovement("DOGEUSDT", s);
  assert.deepEqual(doge, { kind: "excluded", reasons: ["Stale last trade"] });
  assert.equal(a.buildPairMovement("BTCUSDT", s).kind, "ok");
  const outside = a.buildPairMovement("ADAUSDT", s);
  assert.equal(outside.kind, "unavailable");
  assert.match(outside.reason, /Not in the engine's market universe/);
  assert.match(a.buildPairMovement("BTCUSDT", empty, "store_disabled").reason, /cannot reach the market engine/);
});

test("with fewer than 5 contracts the market line is unavailable but per-pair rows still render", () => {
  const few = state({ status: "UNAVAILABLE", symbols: SYMBOLS.slice(0, 4) });
  assert.equal(a.buildMarketStrip(few, NOW).status, "UNAVAILABLE");
  assert.equal(a.buildPairMovement("BTCUSDT", few).kind, "ok");
});

// ---------------------------------------------------------------- TA per timeframe

const iso = (ms) => new Date(ms).toISOString();
const taRow = (timeframe, candleAgoMs, extra = {}) => ({ symbol: "BTCUSDT", timeframe, candle_at: iso(NOW - candleAgoMs),
  detected_at: iso(NOW - candleAgoMs + timeframe * 60_000 + 60_000), price: 100, classification: "bullish", score: 35, atr_pct: 0.8,
  indicators: { rsi14: 61.234 }, factor_breakdown: { trend: { classification: "bullish" } }, patterns: ["doji"], ...extra });

test("TA: ok, stale, missing, failed and not loaded are each explicit", () => {
  const ok = a.buildTaFrame(taRow(15, 20 * 60_000), 15, NOW);
  assert.deepEqual([ok.status, ok.lean, ok.leanText, ok.score, ok.label, ok.trend, ok.patterns], ["ok", "bullish", "Bullish lean", 35, "15m", "Bullish", ["Doji"]]);
  assert.equal(ok.rsi, 61.234);
  assert.match(ok.note, /Candle closed 5 min ago, snapshot saved 4 min ago/);
  const stale = a.buildTaFrame(taRow(15, 3 * 3_600_000), 15, NOW);
  assert.equal(stale.status, "stale");
  assert.match(stale.note, /^Stale: the candle closed/);
  const justDue = a.buildTaFrame(taRow(60, 60 * 60_000 + 60 * 60_000 + 5 * 60_000), 60, NOW); // closed 65 min ago: within 1 h + 10 min
  assert.equal(justDue.status, "ok");
  assert.equal(a.buildTaFrame(taRow(60, 60 * 60_000 + 80 * 60_000), 60, NOW).status, "stale"); // closed 80 min ago
  const missing = a.buildTaFrame(undefined, 240, NOW);
  assert.deepEqual([missing.status, missing.label, missing.lean], ["missing", "4h", "unknown"]);
  assert.match(missing.note, /No snapshot saved yet/);
  assert.equal(a.buildTaFrame(undefined, 60, NOW, "failed").status, "error");
  assert.equal(a.buildTaFrame(undefined, 60, NOW, "not_loaded").status, "not_loaded");
  const partial = a.buildTaFrame(taRow(15, 20 * 60_000, { score: null, indicators: {}, classification: "weird" }), 15, NOW);
  assert.deepEqual([partial.score, partial.rsi, partial.lean], [null, null, "unknown"]);
});

test("latest TA per symbol and timeframe picks the newest candle", () => {
  const rows = [taRow(15, 40 * 60_000), taRow(15, 20 * 60_000, { score: 99 }), taRow(60, 90 * 60_000)];
  const latest = a.latestTaBySymbol(rows);
  assert.equal(latest.get("BTCUSDT|15").score, 99);
  assert.equal(latest.size, 2);
});

// ---------------------------------------------------------------- freshness

const quotes = (over = {}) => SYMBOLS.map((symbol) => ({ symbol, ok: true, stale: false, lastCandleAt: NOW - 60_000, ...over[symbol] }));
const fullTa = () => SYMBOLS.flatMap((symbol) => [15, 60, 240].map((tf) => taRow(tf, 20 * 60_000, { symbol })));

test("freshness: four separate sources, all fresh when they are", () => {
  const items = a.buildFreshness({ state: state(), read: "ok", symbols: SYMBOLS, quotes: quotes(), ta: fullTa() }, NOW);
  assert.deepEqual(items.map((i) => i.id), ["stream", "movement", "candles", "ta"]);
  assert.deepEqual(items.map((i) => i.status), ["fresh", "fresh", "fresh", "fresh"]);
});

test("freshness: each stale or missing source is named even when the page just rendered", () => {
  const stream = a.buildFreshness({ state: state({ stream: { DOGEUSDT: NOW - 5 * 60_000 } }), read: "ok", symbols: SYMBOLS, quotes: quotes(), ta: fullTa() }, NOW);
  assert.equal(stream[0].status, "stale");
  assert.match(stream[0].detail, /DOGE/);
  const movement = a.buildFreshness({ state: state({ status: "STALE", boundaryAgo: 5 * 60_000 }), read: "ok", symbols: SYMBOLS, quotes: quotes(), ta: fullTa() }, NOW);
  assert.equal(movement[1].status, "stale");
  assert.match(movement[1].detail, /5 min ago.*engine status stale/);
  const candles = a.buildFreshness({ state: state(), read: "ok", symbols: SYMBOLS, quotes: quotes({ ETHUSDT: { stale: true } }), ta: fullTa() }, NOW);
  assert.equal(candles[2].status, "stale");
  assert.match(candles[2].detail, /ETH/);
  const rows = fullTa().filter((r) => !(r.symbol === "SOLUSDT" && r.timeframe === 240));
  rows.push(taRow(15, 5 * 3_600_000, { symbol: "XRPUSDT" }));
  const staleTa = a.buildFreshness({ state: state(), read: "ok", symbols: SYMBOLS, quotes: quotes(), ta: rows.filter((r) => !(r.symbol === "XRPUSDT" && r.timeframe === 15 && r.candle_at !== iso(NOW - 5 * 3_600_000))) }, NOW);
  assert.equal(staleTa[3].status, "stale");
  assert.match(staleTa[3].detail, /no snapshot: SOL 4h/);
  assert.match(staleTa[3].detail, /stale: XRP 15m/);
});

test("freshness: unreadable engine and unloaded TA are unavailable, not blank", () => {
  const items = a.buildFreshness({ state: empty, read: "store_disabled", symbols: SYMBOLS, quotes: [], ta: undefined }, NOW);
  assert.deepEqual(items.map((i) => i.status), ["unavailable", "unavailable", "unavailable", "unavailable"]);
  assert.ok(items.every((i) => i.detail.length > 0));
  const none = a.buildFreshness({ state: state(), read: "ok", symbols: SYMBOLS, quotes: quotes(), ta: [] }, NOW);
  assert.equal(none[3].status, "unavailable"); // every pair lacks every snapshot
});

test("age formatting", () => {
  assert.deepEqual([a.formatAge(10_000), a.formatAge(5 * 60_000), a.formatAge(3 * 3_600_000), a.formatAge(72 * 3_600_000)], ["10 s", "5 min", "3 h", "3 days"]);
});
