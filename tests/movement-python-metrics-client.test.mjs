import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

const stub = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const source = await readFile(new URL("../src/lib/market/movement-python-client.server.ts", import.meta.url), "utf8");
let { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
outputText = outputText.replaceAll('"zod"', JSON.stringify(import.meta.resolve("zod")));
outputText = outputText.replaceAll('"../python-service.server"', JSON.stringify(stub(`
  export function pythonServiceConfig(path) {
    return { url: 'http://python.local' + path, token: 'test-token' };
  }
`)));
const { calculatePythonMarketMovement, registerPythonMovementHistory } = await import(stub(outputText));

const SESSION = "2af3e7c8-b777-4e58-9ad2-18e36daac160";
const BOUNDARY = 1_800_000_000_000;
const universe = { id: "watched", version: "watched-v1", symbols: ["BTCUSDT"] };
const missing = (reason) => ({ available: false, value: null, reason });
const present = (value) => ({ available: true, value, reason: null });
const metricSymbol = (window) => ({
  symbol: "BTCUSDT", instrument_id: "binance-usdm:BTCUSDT",
  provider: "binance-usdm", exchange: "binance", price_type: "trade",
  window_minutes: window, evaluation_boundary_time_ms: BOUNDARY,
  included: false, exclusion_reasons: ["WARMING_INSUFFICIENT_LIVE_HISTORY"],
  current_return: missing("SYMBOL_EXCLUDED"), previous_return: missing("SYMBOL_EXCLUDED"),
  velocity: missing("SYMBOL_EXCLUDED"), previous_velocity: missing("SYMBOL_EXCLUDED"),
  acceleration: missing("SYMBOL_EXCLUDED"), historical_median: present(0),
  historical_mad: present(0.01), normalized_z: missing("SYMBOL_EXCLUDED"),
  direction: missing("SYMBOL_EXCLUDED"), material_rising: false, material_falling: false,
  current_notional_volume: missing("CURRENT_NOTIONAL_UNAVAILABLE"),
  rvol: missing("CURRENT_NOTIONAL_UNAVAILABLE"),
  cross_sectional_z: missing("MARKET_UNIVERSE_INELIGIBLE"), outlier_candidate: false,
});
const windowResult = (window) => ({
  algorithm_version: "market-movement-v1", config_version: "market-movement-config-v1",
  universe_id: universe.id, universe_version: universe.version,
  configured_universe: universe.symbols, included_symbols: [],
  excluded_symbols: [{ symbol: "BTCUSDT", reasons: ["WARMING_INSUFFICIENT_LIVE_HISTORY"] }],
  window_minutes: window, provider: "binance-usdm", exchange: "binance", price_type: "trade",
  evaluation_boundary_time_ms: BOUNDARY, historical_lookback_ms: 604_800_000,
  minimum_historical_coverage_ms: 259_200_000,
  market_wide_eligible: false, eligible_count: 0, eligible_fraction: 0,
  symbols: [metricSymbol(window)],
  breadth: { available: false, reason: "MARKET_UNIVERSE_INELIGIBLE", denominator: 0,
    flat: missing("MARKET_UNIVERSE_INELIGIBLE"), rising: missing("MARKET_UNIVERSE_INELIGIBLE"),
    falling: missing("MARKET_UNIVERSE_INELIGIBLE"),
    material_rising: missing("MARKET_UNIVERSE_INELIGIBLE"),
    material_falling: missing("MARKET_UNIVERSE_INELIGIBLE") },
  aggregates: { median_normalized_movement: missing("MARKET_UNIVERSE_INELIGIBLE"),
    median_raw_return: missing("MARKET_UNIVERSE_INELIGIBLE"),
    trimmed_mean_normalized_movement: missing("MARKET_UNIVERSE_INELIGIBLE"),
    liquidity_weighted_normalized_movement: missing("MARKET_UNIVERSE_INELIGIBLE"),
    liquidity_weights: missing("MARKET_UNIVERSE_INELIGIBLE"),
    dispersion_mad_normalized_movement: missing("MARKET_UNIVERSE_INELIGIBLE") },
});
const canonical = {
  schema_version: 1, session_id: SESSION, history_version: "history-v1",
  evaluation: {
    algorithm_version: "market-movement-v1", config_version: "market-movement-config-v1",
    universe_id: universe.id, universe_version: universe.version,
    configured_universe: universe.symbols, provider: "binance-usdm",
    exchange: "binance", price_type: "trade", evaluation_boundary_time_ms: BOUNDARY,
    historical_lookback_ms: 604_800_000, minimum_historical_coverage_ms: 259_200_000,
    windows: { 1: windowResult(1), 5: windowResult(5), 15: windowResult(15) },
  },
};
const response = (value) => async () => new Response(JSON.stringify(value), { status: 200 });

test("strict Python DTO mapper preserves unavailable reasons and all three windows", async () => {
  const movement = await calculatePythonMarketMovement(
    SESSION, BOUNDARY, "history-v1", universe, "market-movement-config-v1", {},
    response(canonical),
  );
  assert.deepEqual(movement.windows.map((window) => window.windowMinutes), [1, 5, 15]);
  assert.deepEqual(movement.windows[0].symbols[0].normalizedZ,
                   missing("SYMBOL_EXCLUDED"));
  assert.equal(movement.windows[0].breadth.flatFraction, null);
  assert.equal(movement.windows[0].breadth.flat.reason, "MARKET_UNIVERSE_INELIGIBLE");
  assert.equal(movement.windows[0].aggregates.liquidityWeights.reason,
               "MARKET_UNIVERSE_INELIGIBLE");
});

test("mismatched or non-finite Python metrics fail closed without a fallback", async () => {
  await assert.rejects(calculatePythonMarketMovement(
    SESSION, BOUNDARY, "history-v1", universe, "market-movement-config-v1", {},
    response({ ...canonical, history_version: "wrong" }),
  ));
  const invalid = structuredClone(canonical);
  invalid.evaluation.windows["1"].symbols[0].historical_median.value = "NaN";
  await assert.rejects(calculatePythonMarketMovement(
    SESSION, BOUNDARY, "history-v1", universe, "market-movement-config-v1", {},
    response(invalid),
  ));
});

test("history registration sends prepared inputs once with exact identity", async () => {
  const sent = [];
  const send = async (url, init) => {
    sent.push({ url, body: JSON.parse(init.body) });
    return new Response(JSON.stringify({ schema_version: 1, session_id: SESSION,
      history_version: "history-v1", universe_id: universe.id,
      universe_version: universe.version }), { status: 200 });
  };
  const config = { version: "market-movement-config-v1", historicalLookbackMs: 604_800_000,
    minimumHistoricalCoverageMs: 259_200_000, flatZ: 0.5, materialZ: 1,
    trimFraction: 0.1, liquidityWeightCap: 0.25, rvolComparisonWindows: 20,
    outlierCrossZ: 3.5, outlierHistoricalZ: 1.5,
    minimumEligibleFraction: 0.6, minimumEligibleCount: 5 };
  const historical = new Map([["BTCUSDT", { 1: { returns: [0.01, -0.01],
    usableCoverageMs: 259_200_000, previousNotionalVolumes: [100] } }]]);
  await registerPythonMovementHistory(SESSION, "history-v1", universe, config, historical, {}, send);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].url, "http://python.local/v1/movement/history");
  assert.deepEqual(sent[0].body.historical[0].windows["1"].returns, [0.01, -0.01]);
  assert.equal(sent[0].body.session_id, SESSION);
});
