import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

const stub = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const source = await readFile(new URL("../src/lib/market/movement-python-client.server.ts", import.meta.url), "utf8");
let { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const contractSource = await readFile(new URL("../src/lib/market/market-state-contract.ts", import.meta.url), "utf8");
const contractOutput = ts.transpileModule(contractSource, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText;
outputText = outputText.replaceAll('"./market-state-contract"', JSON.stringify(stub(contractOutput)));
outputText = outputText.replaceAll('"zod"', JSON.stringify(import.meta.resolve("zod")));
outputText = outputText.replaceAll('"../python-service.server"', JSON.stringify(stub(`
  export function pythonServiceConfig(path) {
    return { url: 'http://python.local' + path, token: 'test-token' };
  }
`)));
const { calculatePythonMarketMovement, calculatePythonMarketAssessment,
  registerPythonMovementHistory } = await import(stub(outputText));

const SESSION = "2af3e7c8-b777-4e58-9ad2-18e36daac160";
const BOUNDARY = 1_800_000_000_000;
const universe = { id: "watched", version: "watched-v1", symbols: ["BTCUSDT"] };
const priorEpisode = {
  direction: "BROAD_DROP", universeId: universe.id, universeVersion: universe.version,
  movementAlgorithmVersion: "market-movement-v1",
  movementConfigVersion: "market-movement-config-v1",
  classifierAlgorithmVersion: "market-state-classifier-v1",
  classifierConfigVersion: "market-state-classifier-config-v1",
};
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

const classificationWindow = (minute) => ({
  window_minutes: minute, horizon_role: minute === 1 ? "RAPID" : minute === 5 ? "PRIMARY" : "PERSISTENCE",
  is_primary: minute === 5, direction_state: "WARMING",
  pace: missing("NO_BROAD_DIRECTION"), prior_confirmed_episode_direction: minute === 5 ? "BROAD_DROP" : null,
  reversal_candidate: null, isolated_outliers: [],
  breadth: canonical.evaluation.windows[minute].breadth,
  median_raw_return: missing("MARKET_UNIVERSE_INELIGIBLE"),
  median_normalized_movement: missing("MARKET_UNIVERSE_INELIGIBLE"),
  median_acceleration: missing("ACCELERATION_UNAVAILABLE"),
  positive_acceleration_breadth: missing("ACCELERATION_UNAVAILABLE"),
  negative_acceleration_breadth: missing("ACCELERATION_UNAVAILABLE"),
  dispersion_mad_normalized_movement: missing("MARKET_UNIVERSE_INELIGIBLE"),
  trimmed_mean_normalized_movement: missing("MARKET_UNIVERSE_INELIGIBLE"),
  liquidity_weighted_normalized_movement: missing("MARKET_UNIVERSE_INELIGIBLE"),
  liquidity_weights: missing("MARKET_UNIVERSE_INELIGIBLE"), volume_context: [],
  market_wide_eligible: false, eligible_count: 0, eligible_fraction: 0,
  configured_universe: universe.symbols, included_symbols: [],
  excluded_symbols: canonical.evaluation.windows[minute].excluded_symbols,
  availability_reasons: ["WARMING_INSUFFICIENT_LIVE_HISTORY"],
  classifier_algorithm_version: "market-state-classifier-v1",
  classifier_config_version: "market-state-classifier-config-v1",
  movement_algorithm_version: "market-movement-v1",
  movement_config_version: "market-movement-config-v1",
  universe_id: universe.id, universe_version: universe.version,
  provider: "binance-usdm", exchange: "binance", price_type: "trade",
  evaluation_boundary_time_ms: BOUNDARY,
  source_time_evidence: [{ symbol: "BTCUSDT", last_real_trade_time_ms: null,
    last_real_event_time_ms: null, last_received_at_ms: null }],
  movement_snapshot: canonical.evaluation.windows[minute],
});
const assessment = {
  ...canonical,
  effective_previous_confirmed_primary_direction: "BROAD_DROP",
  classification: {
    classifier_algorithm_version: "market-state-classifier-v1",
    classifier_config_version: "market-state-classifier-config-v1",
    classifier_config: { version: "market-state-classifier-config-v1",
      directional_breadth: 0.7, material_breadth: 0.5, normalized_movement: 0.5,
      acceleration_breadth: 0.6, isolated_outlier_breadth_disagreement: 0.5 },
    movement_algorithm_version: "market-movement-v1",
    movement_config_version: "market-movement-config-v1",
    universe_id: universe.id, universe_version: universe.version,
    provider: "binance-usdm", exchange: "binance", price_type: "trade",
    evaluation_boundary_time_ms: BOUNDARY, primary_window_minutes: 5,
    windows: { 1: classificationWindow(1), 5: classificationWindow(5),
      15: classificationWindow(15) },
  },
};

test("canonical Python assessment transports pace and per-symbol provenance unchanged", async () => {
  const supplied = structuredClone(assessment);
  supplied.classification.windows[5].pace = present("MIXED");
  for (const window of [1, 5, 15]) {
    supplied.classification.windows[window].source_time_evidence[0] = {
      symbol: "BTCUSDT", last_real_trade_time_ms: BOUNDARY - 20,
      last_real_event_time_ms: BOUNDARY - 10, last_received_at_ms: BOUNDARY + 7,
    };
  }
  const sent = [];
  const send = async (url, init) => {
    sent.push({ url, body: JSON.parse(init.body) });
    return response(supplied)();
  };
  const result = await calculatePythonMarketAssessment(
    SESSION, BOUNDARY, "history-v1", universe, "market-movement-config-v1", priorEpisode, {}, send,
  );
  assert.equal(sent[0].url, "http://python.local/v1/movement/classification");
  assert.deepEqual(sent[0].body.previous_confirmed_primary_episode, {
    direction: "BROAD_DROP", universe_id: universe.id, universe_version: universe.version,
    movement_algorithm_version: "market-movement-v1",
    movement_config_version: "market-movement-config-v1",
    classifier_algorithm_version: "market-state-classifier-v1",
    classifier_config_version: "market-state-classifier-config-v1",
  });
  assert.deepEqual(result.classification.windows.map((window) => window.windowMinutes), [1, 5, 15]);
  assert.deepEqual(result.classification.windows[0].pace, missing("NO_BROAD_DIRECTION"));
  assert.deepEqual(result.classification.windows[1].pace, present("MIXED"));
  assert.deepEqual(result.classification.windows[1].sourceTimeEvidence,
    [{ symbol: "BTCUSDT", lastRealTradeTimeMs: BOUNDARY - 20,
      lastRealEventTimeMs: BOUNDARY - 10, lastReceivedAtMs: BOUNDARY + 7 }]);
});

test("distinct Python classifier config versions and finite values survive transport", async () => {
  const supplied = structuredClone(assessment);
  const config = supplied.classification.classifier_config;
  config.version = "market-state-classifier-config-custom-v2";
  config.directional_breadth = 0.9;
  config.material_breadth = 0.45;
  config.normalized_movement = 0.65;
  config.acceleration_breadth = 0.55;
  config.isolated_outlier_breadth_disagreement = 0.4;
  supplied.classification.classifier_config_version = config.version;
  supplied.effective_previous_confirmed_primary_direction = null;
  for (const minute of [1, 5, 15]) {
    supplied.classification.windows[minute].classifier_config_version = config.version;
  }
  supplied.classification.windows[5].prior_confirmed_episode_direction = null;
  const result = await calculatePythonMarketAssessment(
    SESSION, BOUNDARY, "history-v1", universe, "market-movement-config-v1", priorEpisode, {},
    response(supplied),
  );
  assert.equal(result.classification.classifierConfigVersion, config.version);
  assert.deepEqual(result.classification.classifierConfig, {
    version: config.version,
    directionalBreadth: config.directional_breadth,
    materialBreadth: config.material_breadth,
    normalizedMovement: config.normalized_movement,
    accelerationBreadth: config.acceleration_breadth,
    isolatedOutlierBreadthDisagreement: config.isolated_outlier_breadth_disagreement,
  });
  assert.ok(result.classification.windows.every(
    (window) => window.classifierConfigVersion === config.version,
  ));
});

test("Python may reject an out-of-scope prior episode while preserving the assessment", async () => {
  const supplied = structuredClone(assessment);
  supplied.effective_previous_confirmed_primary_direction = null;
  supplied.classification.windows[5].prior_confirmed_episode_direction = null;
  const result = await calculatePythonMarketAssessment(
    SESSION, BOUNDARY, "history-v1", universe, "market-movement-config-v1",
    { ...priorEpisode, universeVersion: "older-universe" }, {}, response(supplied),
  );
  assert.equal(result.classification.windows[1].priorConfirmedEpisodeDirection, null);
  assert.equal(result.classification.windows[1].reversalCandidate, null);
});

test("scoped prior episode request rejects missing, extra and invalid identity fields", async () => {
  const invalid = [
    { ...priorEpisode, universeId: "" },
    { ...priorEpisode, direction: "NEUTRAL" },
    { ...priorEpisode, classifierConfigVersion: undefined },
    { ...priorEpisode, unexpected: "extra" },
  ];
  for (const prior of invalid) {
    await assert.rejects(calculatePythonMarketAssessment(
      SESSION, BOUNDARY, "history-v1", universe, "market-movement-config-v1",
      prior, {}, response(assessment),
    ));
  }
});

test("canonical assessment rejects mismatched identity, provenance and snapshots", async () => {
  const mutations = [
    (item) => { item.history_version = "wrong"; },
    (item) => { item.effective_previous_confirmed_primary_direction = "BROAD_RISE"; },
    (item) => { item.effective_previous_confirmed_primary_direction = null; },
    (item) => { item.classification.classifier_algorithm_version = "wrong"; },
    (item) => { item.classification.classifier_config_version = "wrong"; },
    (item) => { item.classification.classifier_config.version = "wrong"; },
    (item) => { item.classification.windows[5].classifier_config_version = "wrong"; },
    (item) => { item.classification.classifier_config.directional_breadth = null; },
    (item) => { item.classification.evaluation_boundary_time_ms += 5_000; },
    (item) => { item.classification.universe_id = "wrong"; },
    (item) => { item.classification.movement_config_version = "wrong"; },
    (item) => { item.classification.windows[5].configured_universe = ["OTHER"]; },
    (item) => { item.classification.provider = "wrong"; },
    (item) => { item.classification.exchange = "wrong"; },
    (item) => { item.classification.price_type = "wrong"; },
    (item) => { item.classification.windows[1].source_time_evidence[0].symbol = "OTHER"; },
    (item) => { item.classification.windows[1].source_time_evidence.push({
      symbol: "OTHER", last_real_trade_time_ms: null,
      last_real_event_time_ms: null, last_received_at_ms: null }); },
    (item) => { item.classification.windows[5].breadth = {
      ...item.classification.windows[5].breadth,
      denominator: item.classification.windows[5].breadth.denominator + 1,
    }; },
    (item) => { item.classification.windows[15].window_minutes = 5; },
    (item) => { item.classification.windows[5].movement_snapshot.window_minutes = 1; },
  ];
  for (const mutate of mutations) {
    const invalid = structuredClone(assessment);
    mutate(invalid);
    await assert.rejects(calculatePythonMarketAssessment(
      SESSION, BOUNDARY, "history-v1", universe, "market-movement-config-v1", priorEpisode, {},
      response(invalid),
    ));
  }
});

test("canonical assessment rejects per-symbol provenance in the wrong order", async () => {
  const twoSymbols = { ...universe, symbols: ["BTCUSDT", "ETHUSDT"] };
  const swapped = structuredClone(assessment);
  swapped.evaluation.configured_universe = twoSymbols.symbols;
  for (const minute of [1, 5, 15]) {
    swapped.evaluation.windows[minute].configured_universe = twoSymbols.symbols;
    const window = swapped.classification.windows[minute];
    window.configured_universe = twoSymbols.symbols;
    window.movement_snapshot = swapped.evaluation.windows[minute];
    window.source_time_evidence = [
      { symbol: "ETHUSDT", last_real_trade_time_ms: null,
        last_real_event_time_ms: null, last_received_at_ms: null },
      { symbol: "BTCUSDT", last_real_trade_time_ms: null,
        last_real_event_time_ms: null, last_received_at_ms: null },
    ];
  }
  await assert.rejects(calculatePythonMarketAssessment(
    SESSION, BOUNDARY, "history-v1", twoSymbols, "market-movement-config-v1", priorEpisode, {},
    response(swapped),
  ));
});

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

test("history registration transports raw candles and factual compatibility", async () => {
  const sent = [];
  const send = async (url, init) => {
    sent.push({ url, body: JSON.parse(init.body) });
    return new Response(JSON.stringify({ schema_version: 1, session_id: SESSION,
      history_version: "history-v1", universe_id: universe.id,
      universe_version: universe.version, as_of_boundary_time_ms: BOUNDARY }), { status: 200 });
  };
  const config = { version: "market-movement-config-v1", historicalLookbackMs: 604_800_000,
    minimumHistoricalCoverageMs: 259_200_000, flatZ: 0.5, materialZ: 1,
    trimFraction: 0.1, liquidityWeightCap: 0.25, rvolComparisonWindows: 20,
    outlierCrossZ: 3.5, outlierHistoricalZ: 1.5,
    minimumEligibleFraction: 0.6, minimumEligibleCount: 5 };
  const historical = new Map([["BTCUSDT", [{ openTime: BOUNDARY - 120_000,
    close: 101, volume: 2, quoteVolume: 207 }]]]);
  const compatibility = new Map([["BTCUSDT", true]]);
  await registerPythonMovementHistory(SESSION, "history-v1", universe, config, historical,
    compatibility, BOUNDARY, {}, send);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].url, "http://python.local/v1/movement/history");
  assert.deepEqual(sent[0].body.historical[0].candles, [{ open_time_ms: BOUNDARY - 120_000,
    close: 101, volume: 2, quote_volume: 207 }]);
  assert.equal(sent[0].body.historical[0].instrument_compatible, true);
  assert.equal("windows" in sent[0].body.historical[0], false);
  assert.equal(sent[0].body.session_id, SESSION);
  assert.equal(sent[0].body.as_of_boundary_time_ms, BOUNDARY);
});
