import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

const stub = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;

async function transpile(path, rewrites = {}) {
  const source = await readFile(new URL(path, import.meta.url), "utf8");
  let { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  });
  for (const [specifier, target] of Object.entries(rewrites)) {
    outputText = outputText.replaceAll(JSON.stringify(specifier), JSON.stringify(target));
  }
  return stub(outputText);
}

const movementBucketsUrl = await transpile("../src/lib/market/movement-contract.ts");
const stateUrl = await transpile("../src/lib/market/market-movement-state.ts");
const metricsUrl = await transpile("../src/lib/market/movement-metrics-contract.ts", {
  "./movement-contract": movementBucketsUrl,
});
const normalizationUrl = await transpile("../src/lib/market/movement-normalization-input.ts", {
  "./movement-contract": movementBucketsUrl,
  "./movement-metrics-contract": metricsUrl,
});
const universeUrl = await transpile("../src/lib/market/market-universe.ts", {
  "./market-movement-state": stateUrl,
});
const finalizationUrl = await transpile("../src/lib/market/movement-finalization.ts", {
  "./movement-contract": movementBucketsUrl,
});
const engineUrl = await transpile("../src/lib/market/market-movement-engine.ts", {
  "./movement-contract": movementBucketsUrl,
  "./movement-metrics-contract": metricsUrl,
  "./market-universe": universeUrl,
});
const runtimeUrl = await transpile("../src/lib/market/movement-engine.server.ts", {
  "./market-movement-engine": engineUrl,
  "./market-universe": universeUrl,
  "./movement-finalization": finalizationUrl,
  "./movement-metrics-contract": metricsUrl,
  "./movement-normalization-input": normalizationUrl,
  "./market-movement-state": stateUrl,
});

const { MovementEngineRuntime, MOVEMENT_RETRY_BASE_MS } = await import(runtimeUrl);
const { buildMarketUniverse } = await import(universeUrl);

const BASE = 1_800_000_000_000; // 15m-aligned epoch boundary
const MINUTE = 60_000;
const SYMBOLS = ["ADAUSDT", "BNBUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT"];

/** Live bucket endpoint prices by offset from the evaluated boundary. */
const OFFSET_PRICES = [
  [-30 * MINUTE, 94],
  [-15 * MINUTE, 96],
  [-10 * MINUTE, 96],
  [-5 * MINUTE, 98],
  [-2 * MINUTE, 98],
  [-1 * MINUTE, 99],
  [0, 100],
];

/**
 * Nearly flat, slightly oscillating one-minute closes: historical returns have a
 * small but strictly positive MAD so normalization stays available, while the live
 * snapshot returns below are large enough to be material.
 */
function historicalCandles() {
  const count = 4_400; // >= #71's three-day minimum coverage
  const start = BASE - count * MINUTE;
  return Array.from({ length: count }, (_, index) => ({
    openTime: start + index * MINUTE,
    close: 100 + Math.sin(index / 5) * 0.005 + index * 0.0000002,
    volume: 1 + (index % 3),
    quoteVolume: 100 + (index % 3),
  }));
}

function bucket(symbol, boundaryTime, endpointPrice) {
  return {
    boundaryTime,
    sourceState: "LIVE",
    endpointPrice,
    baseQuantity: 1,
    quoteVolume: endpointPrice,
    tradeCount: 1,
    lastRealTradeTime: boundaryTime,
    lastRealEventTime: boundaryTime,
    lastRealReceivedAt: boundaryTime + 123,
    carriedForward: false,
    provider: "binance-usdm",
    instrumentId: `binance-usdm:${symbol}`,
    nativeSymbol: symbol,
    symbol,
    marketType: "futures",
    contractType: "perpetual",
    priceType: "trade",
  };
}

function snapshotFor(symbol, boundary) {
  return {
    symbol,
    provider: "binance-usdm",
    instrumentId: `binance-usdm:${symbol}`,
    priceType: "trade",
    bucketMs: 5_000,
    maxLastTradeAgeMs: 15_000,
    buckets: OFFSET_PRICES.map(([offset, price]) => bucket(symbol, boundary + offset, price)).sort(
      (left, right) => left.boundaryTime - right.boundaryTime,
    ),
    latestRealTradeTime: boundary,
    latestRealReceivedAt: boundary + 123,
    readiness: Object.fromEntries(
      [1, 5, 15].map((windowMinutes) => [
        windowMinutes,
        {
          windowMinutes,
          status: "READY",
          state: "ready",
          reason: null,
        },
      ]),
    ),
  };
}

function canonicalMovement(universe, boundary) {
  const value = (number) => ({ available: true, value: number });
  return {
    algorithmVersion: "market-movement-v1", configVersion: "market-movement-config-v1",
    universeId: universe.id, universeVersion: universe.version,
    configuredUniverse: [...universe.symbols],
    provider: "binance-usdm", exchange: "binance", priceType: "trade",
    evaluationBoundaryTime: boundary,
    windows: [1, 5, 15].map((windowMinutes) => ({
      algorithmVersion: "market-movement-v1", configVersion: "market-movement-config-v1",
      universeId: universe.id, universeVersion: universe.version,
      configuredUniverse: [...universe.symbols], includedSymbols: [...universe.symbols],
      excludedSymbols: [], windowMinutes,
      provider: "binance-usdm", exchange: "binance", priceType: "trade",
      evaluationBoundaryTime: boundary, marketWideEligible: true,
      eligibleCount: universe.symbols.length, eligibleFraction: 1,
      breadth: { available: true, flatFraction: 0, risingFraction: 1,
        fallingFraction: 0, materialRisingFraction: 1, materialFallingFraction: 0 },
      aggregates: { medianRawReturn: value(0.01), medianNormalizedMovement: value(1),
        dispersionMadNormalizedMovement: value(0.1) },
      symbols: universe.symbols.map((symbol) => ({
        symbol, included: true, direction: "RISING", currentReturn: value(0.01),
        normalizedZ: value(1), acceleration: value(0.0001), rvol: value(1.2),
        outlierCandidate: false,
      })),
    })),
  };
}

function canonicalAssessment(universe, boundary) {
  const movement = canonicalMovement(universe, boundary);
  const value = (number) => ({ available: true, value: number });
  const side = (count, fraction) => value({ count, fraction });
  return {
    movement,
    classification: {
      classifierAlgorithmVersion: "market-state-classifier-v1",
      classifierConfigVersion: "market-state-classifier-config-v1",
      classifierConfig: { version: "market-state-classifier-config-v1",
        directionalBreadth: 0.7, materialBreadth: 0.5, normalizedMovement: 0.5,
        accelerationBreadth: 0.6, isolatedOutlierBreadthDisagreement: 0.5 },
      movementAlgorithmVersion: movement.algorithmVersion,
      movementConfigVersion: movement.configVersion,
      universeId: movement.universeId, universeVersion: movement.universeVersion,
      provider: movement.provider, exchange: movement.exchange, priceType: movement.priceType,
      evaluationBoundaryTime: boundary, primaryWindowMinutes: 5,
      windows: movement.windows.map((snapshot) => ({
        windowMinutes: snapshot.windowMinutes,
        horizonRole: snapshot.windowMinutes === 1 ? "RAPID" :
          snapshot.windowMinutes === 5 ? "PRIMARY" : "PERSISTENCE",
        isPrimary: snapshot.windowMinutes === 5,
        directionState: "BROAD_RISE", pace: value("ACCELERATING"),
        priorConfirmedEpisodeDirection: null,
        reversalCandidate: null, isolatedOutliers: [],
        breadth: { flat: side(0, 0), rising: side(universe.symbols.length, 1),
          falling: side(0, 0), materialRising: side(universe.symbols.length, 1),
          materialFalling: side(0, 0), denominator: universe.symbols.length },
        medianRawReturn: snapshot.aggregates.medianRawReturn,
        medianNormalizedMovement: snapshot.aggregates.medianNormalizedMovement,
        medianAcceleration: value(0.0001),
        positiveAccelerationBreadth: side(universe.symbols.length, 1),
        negativeAccelerationBreadth: side(0, 0),
        dispersionMadNormalizedMovement: snapshot.aggregates.dispersionMadNormalizedMovement,
        trimmedMeanNormalizedMovement: value(1),
        liquidityWeightedNormalizedMovement: value(1), liquidityWeights: value([]),
        volumeContext: [], marketWideEligible: true,
        eligibleCount: snapshot.eligibleCount, eligibleFraction: 1,
        configuredUniverse: snapshot.configuredUniverse,
        includedSymbols: snapshot.includedSymbols, excludedSymbols: [],
        availabilityReasons: [],
        classifierAlgorithmVersion: "market-state-classifier-v1",
        classifierConfigVersion: "market-state-classifier-config-v1",
        movementAlgorithmVersion: movement.algorithmVersion,
        movementConfigVersion: movement.configVersion,
        universeId: movement.universeId, universeVersion: movement.universeVersion,
        provider: movement.provider, exchange: movement.exchange, priceType: movement.priceType,
        evaluationBoundaryTime: boundary,
        sourceTimeEvidence: universe.symbols.map((symbol) => ({ symbol,
          lastRealTradeTimeMs: boundary, lastRealEventTimeMs: boundary,
          lastReceivedAtMs: boundary + 123 })),
        movementSnapshot: snapshot,
      })),
    },
  };
}

function canonicalLifecycle(universe, boundary, transitions = []) {
  const assessment = canonicalAssessment(universe, boundary);
  return {
    ...assessment,
    lifecycle: {
      serializedState: { serialization_version: "market-episode-state-v1",
        opaque_marker: boundary },
      stateSummary: {
        evaluationBoundaryTime: boundary, currentDirectionState: "BROAD_RISE",
        currentPace: { available: true, value: "ACCELERATING", reason: null },
        activeEpisodeId: transitions.length ? "episode-fixture" : null,
        activeEpisodeDirection: transitions.length ? "BROAD_RISE" : null,
        interrupted: false,
        lifecycleAlgorithmVersion: "market-episode-lifecycle-v1",
        lifecycleConfigVersion: "market-episode-lifecycle-config-v1",
        universeId: universe.id, universeVersion: universe.version,
        primaryWindowMinutes: 5,
        classifierAlgorithmVersion: "market-state-classifier-v1",
        classifierConfigVersion: "market-state-classifier-config-v1",
        movementAlgorithmVersion: "market-movement-v1",
        movementConfigVersion: "market-movement-config-v1",
        provider: "binance-usdm", exchange: "binance", priceType: "trade",
      },
      transitions,
    },
  };
}

function canonicalTransition(boundary) {
  const universe = buildMarketUniverse(SYMBOLS);
  const scope = {
    lifecycle_algorithm_version: "market-episode-lifecycle-v1",
    lifecycle_config_version: "market-episode-lifecycle-config-v1",
    universe_id: universe.id, universe_version: universe.version,
    primary_window_minutes: 5,
    classifier_algorithm_version: "market-state-classifier-v1",
    classifier_config_version: "market-state-classifier-config-v1",
    movement_algorithm_version: "market-movement-v1",
    movement_config_version: "market-movement-config-v1",
    provider: "binance-usdm", exchange: "binance", price_type: "trade",
  };
  const lifecycleConfig = {
    version: "market-episode-lifecycle-config-v1", evaluation_cadence_ms: 5_000,
    start_confirmation_count: 2, end_confirmation_count: 3,
    reversal_confirmation_count: 2, strengthen_confirmation_count: 2,
    weaken_confirmation_count: 2, resume_confirmation_count: 2,
    continuation_breadth: 0.55, material_strengthen_breadth: 0.7,
    material_weaken_breadth: 0.5,
  };
  const metric = (value) => ({ available: true, value, reason: null });
  return {
    event_id: `event-${boundary}`, episode_id: "episode-fixture",
    previous_episode_id: null, transition: "STARTED",
    transition_reason: "confirmed_broad_entry", from_direction: null,
    to_direction: "BROAD_RISE", event_family: "BROAD_MOVE",
    episode_direction: "BROAD_RISE",
    episode_start_boundary_time_ms: boundary - 5_000,
    evaluation_boundary_time_ms: boundary,
    episode_scope: scope, evaluation_scope: scope,
    episode_lifecycle_config: lifecycleConfig, evaluation_lifecycle_config: lifecycleConfig,
    windows_context: [1, 5, 15].map((window_minutes) => ({ window_minutes })),
    source_time_evidence: SYMBOLS.map((symbol) => ({
      symbol, last_real_trade_time_ms: boundary,
      last_real_event_time_ms: boundary, last_received_at_ms: boundary + 123,
    })),
    directional_breadth: metric({ count: 5, fraction: 1 }),
    material_breadth: metric({ count: 5, fraction: 1 }),
    median_raw_return: metric(0.01), median_normalized_movement: metric(1),
    median_acceleration: metric(0.0001),
    acceleration_breadth: metric({ count: 5, fraction: 1 }),
    pace: metric("ACCELERATING"), dispersion_mad_normalized_movement: metric(0.1),
    volume_context: [], isolated_outliers: [], supporting_contracts: [],
    conflicting_contracts: [],
    configured_universe: SYMBOLS, included_symbols: SYMBOLS,
    excluded_symbols: [],
    classification: { evaluation_boundary_time_ms: boundary },
  };
}

function createHarness(persistenceConfig) {
  const state = { now: BASE, boundary: BASE, advanced: [], sessionId: crypto.randomUUID() };
  const control = { failRestore: 0, failHistory: 0, failPersist: false,
    failLifecycle: false, rotateDuringLifecycle: false, changeUniverseDuringLifecycle: false,
    transitionBoundaries: new Set([BASE + 5_000]),
    symbols: [...SYMBOLS] };
  const calls = { restore: 0, history: 0, historyReads: [], register: [], lifecycle: [], persist: [] };
  const persistedEvents = new Map();
  const candles = new Map(SYMBOLS.map((symbol) => [symbol, historicalCandles()]));
  let durableCurrent = null;

  const collector = {
    subscribedSymbols: () => [...control.symbols],
    advanceMovementBuckets: (boundary) => state.advanced.push(boundary),
    movementSourceStatus: () => "LIVE",
    movementLateRejections: () => 0,
    movementSnapshot: (symbol) => snapshotFor(symbol, state.boundary),
    currentMovementSessionId: () => state.sessionId,
  };
  const store = {
    async getMarketStateCurrent() {
      calls.restore += 1;
      if (calls.restore <= control.failRestore) throw new Error("restore read failed");
      return durableCurrent;
    },
    async readMovementCandleHistory(symbols, sinceMs, beforeBoundaryMs) {
      calls.history += 1;
      calls.historyReads.push({symbols, sinceMs, beforeBoundaryMs});
      if (calls.history <= control.failHistory) throw new Error("history read failed");
      return candles;
    },
    async persistMarketEpisodeLifecycleStep(current, events) {
      calls.persist.push({ current, events });
      if (control.failPersist) {
        control.failPersist = false;
        throw new Error("persist write failed");
      }
      return events.map((event) => {
        const exists = persistedEvents.has(event.event_id);
        if (!exists) persistedEvents.set(event.event_id, event);
        return { eventId: event.event_id, status: exists ? "already_exists" : "appended" };
      });
    },
  };
  const setDurableCurrent = (current) => {
    durableCurrent = current;
  };
  const runtime = new MovementEngineRuntime({
    store,
    collector,
    instrumentCompatibility: async () => true,
    async registerHistory(sessionId, historyVersion, universe, config, historical,
      compatibility, asOfBoundary) {
      calls.register.push({sessionId, historyVersion, universe, config, historical,
        compatibility, asOfBoundary});
    },
    async calculateLifecycle(sessionId, boundary, historyVersion, universe,
      configVersion, previousLifecycleState, interruptPreviousState) {
      calls.lifecycle.push({sessionId, boundary, historyVersion, universe,
        configVersion, previousLifecycleState, interruptPreviousState});
      if (control.failLifecycle) throw new Error("Python #71/#72/#73 lifecycle unavailable");
      if (control.rotateDuringLifecycle) state.sessionId = crypto.randomUUID();
      if (control.changeUniverseDuringLifecycle) control.symbols = [...SYMBOLS, "XRPUSDT"];
      return canonicalLifecycle(universe, boundary,
        control.transitionBoundaries.has(boundary) ? [canonicalTransition(boundary)] : []);
    },
    finalization: { version: "movement-finalization-config-v1:grace-0", graceMs: 0 },
    persistenceConfig,
    now: () => state.now,
  });
  return { runtime, state, control, calls, persistedEvents, setDurableCurrent };
}

/** Builds a durable current-state row advanced to `boundary` from a captured one. */
function advancedCurrent(current, boundary) {
  return {
    ...current,
    evaluationBoundaryTime: boundary,
    lifecycleState: { serialization_version: "market-episode-state-v1",
      opaque_marker: boundary },
  };
}

test("runtime persists the effective finalization config and receive-time provenance", async () => {
  const harness = createHarness();
  await harness.runtime.runOnce();
  assert.equal(harness.calls.persist.length, 1);
  const evidence = harness.calls.persist[0].current.currentEvidence;
  assert.equal(evidence.finalizationConfigVersion, "movement-finalization-config-v1:grace-0");
  assert.equal(evidence.finalizationGraceMs, 0);
  assert.equal(evidence.persistenceConfigVersion, "market-episode-persistence-v1");
  assert.equal(evidence.currentSnapshotCadenceMs, 30_000);
  assert.equal(evidence.timestamps.lastReceivedAt, BASE + 123);
  assert.deepEqual(harness.calls.persist[0].current.lifecycleState,
    { serialization_version: "market-episode-state-v1", opaque_marker: BASE });
  assert.equal("lastPersistedTime" in harness.calls.persist[0].current.lifecycleState, false);
});

test("transition-free current state follows the application 30-second persistence cadence", async () => {
  const harness = createHarness();
  harness.control.transitionBoundaries.clear();
  await harness.runtime.runOnce();
  assert.equal(harness.calls.persist.length, 1);
  harness.state.now = BASE + 25_000;
  harness.state.boundary = harness.state.now;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.persist.length, 1);
  harness.state.now = BASE + 30_000;
  harness.state.boundary = harness.state.now;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.persist.length, 2);
  assert.equal(harness.calls.persist[1].current.evaluationBoundaryTime, BASE + 30_000);
  assert.deepEqual(harness.calls.persist[1].events, []);
});

test("alternate application persistence cadence is auditable without changing Python state", async () => {
  const harness = createHarness({
    version: "market-episode-persistence-test-v2", currentSnapshotCadenceMs: 10_000,
  });
  harness.control.transitionBoundaries.clear();
  await harness.runtime.runOnce();
  assert.equal(harness.calls.persist.length, 1);
  harness.state.now = BASE + 5_000;
  harness.state.boundary = harness.state.now;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.persist.length, 1);
  harness.state.now = BASE + 10_000;
  harness.state.boundary = harness.state.now;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.persist.length, 2);
  const current = harness.calls.persist[1].current;
  assert.equal(current.evaluationBoundaryTime, BASE + 10_000);
  assert.equal(current.currentEvidence.persistenceConfigVersion,
    "market-episode-persistence-test-v2");
  assert.equal(current.currentEvidence.currentSnapshotCadenceMs, 10_000);
  assert.deepEqual(current.lifecycleState,
    { serialization_version: "market-episode-state-v1", opaque_marker: BASE + 10_000 });
  assert.equal("persistenceConfigVersion" in current.lifecycleState, false);
  assert.equal("currentSnapshotCadenceMs" in current.lifecycleState, false);
  assert.deepEqual(harness.calls.persist[1].events, []);
});

test("application persistence config rejects missing versions and invalid cadences", () => {
  assert.throws(() => createHarness({ version: " ", currentSnapshotCadenceMs: 10_000 }));
  for (const cadence of [0, -5_000, 1.5, Infinity]) {
    assert.throws(() => createHarness({
      version: "market-episode-persistence-test-v2", currentSnapshotCadenceMs: cadence,
    }));
  }
});

test("raw history registers for each completed-minute cutoff and the canonical session", async () => {
  const harness = createHarness();
  await harness.runtime.runOnce();
  assert.equal(harness.calls.register.length, 1);
  assert.equal(harness.calls.lifecycle.length, 1);
  assert.equal(harness.calls.register[0].asOfBoundary, BASE);
  assert.equal(harness.calls.historyReads[0].sinceMs,
    BASE - harness.calls.register[0].config.historicalLookbackMs - 16 * MINUTE);
  assert.equal(harness.calls.historyReads[0].beforeBoundaryMs, BASE);
  assert.equal(harness.calls.lifecycle[0].sessionId, harness.calls.register[0].sessionId);
  assert.equal(harness.calls.lifecycle[0].historyVersion, harness.calls.register[0].historyVersion);
  assert.deepEqual(harness.calls.register[0].historical.get("BTCUSDT"), historicalCandles());
  assert.equal(harness.calls.register[0].compatibility.get("BTCUSDT"), true);
  harness.state.now = BASE + 5_000;
  harness.state.boundary = BASE + 5_000;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.register.length, 2);
  assert.equal(harness.calls.lifecycle.length, 2);
  harness.state.sessionId = crypto.randomUUID();
  harness.state.now = BASE + 10_000;
  harness.state.boundary = BASE + 10_000;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.register.length, 3);
  assert.equal(harness.calls.register[2].sessionId, harness.state.sessionId);
  harness.state.now = BASE + 15 * MINUTE;
  harness.state.boundary = harness.state.now;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.register.length, 4);
  harness.control.symbols = [...SYMBOLS, "XRPUSDT"];
  harness.state.now += 5_000;
  harness.state.boundary = harness.state.now;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.register.length, 5);
});

test("raw candles are reused within a completed-minute cutoff while each boundary is evaluated", async () => {
  const harness = createHarness();
  await harness.runtime.runOnce();
  harness.state.now = BASE + 5_000;
  harness.state.boundary = BASE + 5_000;
  await harness.runtime.runOnce();
  harness.state.now = BASE + 10_000;
  harness.state.boundary = BASE + 10_000;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.history, 2);
  assert.equal(harness.calls.register.length, 2);
  assert.deepEqual(harness.calls.lifecycle.map((call) => call.boundary),
    [BASE, BASE + 5_000, BASE + 10_000]);
});

test("Python lifecycle failure does not stop #70 finalization or advance #73", async () => {
  const harness = createHarness();
  harness.control.failLifecycle = true;
  await harness.runtime.runOnce();
  assert.equal(harness.runtime.engine.lastEvaluatedBoundary, null);
  harness.state.now = BASE + 5_000;
  harness.state.boundary = BASE + 5_000;
  await harness.runtime.runOnce();
  assert.equal(harness.state.advanced.at(-1), BASE + 5_000);
  assert.equal(harness.runtime.engine.lastEvaluatedBoundary, null);
  assert.equal(harness.calls.persist.length, 0);
  harness.control.failLifecycle = false;
  await harness.runtime.runOnce();
  assert.equal(harness.runtime.engine.lastEvaluatedBoundary, BASE + 5_000);
});

test("old session and changed universe responses are discarded before #72/#73", async () => {
  for (const change of ["rotateDuringLifecycle", "changeUniverseDuringLifecycle"]) {
    const harness = createHarness();
    harness.control[change] = true;
    await harness.runtime.runOnce();
    assert.equal(harness.runtime.engine.lastEvaluatedBoundary, null);
    assert.equal(harness.calls.persist.length, 0);
  }
});

test("pending lifecycle persistence does not block newer bucket finalization", async () => {
  const harness = createHarness();

  // Boundary one: candidate accumulation only, so no transition is emitted yet.
  await harness.runtime.runOnce();
  assert.equal(harness.calls.persist.length, 1);
  assert.equal(harness.persistedEvents.size, 0);

  // Boundary two: the episode is confirmed (STARTED) but the write fails.
  harness.state.now = BASE + 5_000;
  harness.state.boundary = BASE + 5_000;
  harness.control.failPersist = true;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.persist.length, 2);
  const attempted = harness.calls.persist[1].events;
  assert.equal(attempted.length, 1);
  assert.equal(attempted[0].transition, "STARTED");
  assert.deepEqual(attempted[0], canonicalTransition(BASE + 5_000));
  assert.equal(harness.persistedEvents.size, 0, "a failed write must not record the event");

  // A newer safe #70 boundary is finalized even when the required #73 retry
  // fails again. Downstream evaluation must remain at the pending boundary.
  harness.state.now = BASE + 10_000;
  harness.state.boundary = BASE + 10_000;
  harness.control.failPersist = true;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.persist.length, 3, "the pending batch is retried");
  assert.equal(harness.calls.persist[2].events.length, 1);
  assert.strictEqual(harness.calls.persist[2].events, attempted);
  assert.strictEqual(harness.calls.persist[2].current, harness.calls.persist[1].current);
  assert.equal(
    harness.calls.persist[2].events[0].event_id,
    attempted[0].event_id,
    "the retried batch reuses the deterministic event ID",
  );
  assert.equal(harness.state.advanced.at(-1), BASE + 10_000);
  assert.equal(harness.runtime.engine.lastEvaluatedBoundary, BASE + 5_000);
  assert.equal(harness.persistedEvents.size, 0);

  // Once the exact pending batch succeeds, deterministic downstream processing
  // resumes without having evaluated the newer boundary during the outage.
  await harness.runtime.runOnce();
  assert.equal(harness.persistedEvents.size, 1, "the transition persists exactly once");
  assert.equal(harness.runtime.engine.lastEvaluatedBoundary, BASE + 10_000);
});

test("start installs the #70 timer without waiting for lifecycle restore", async () => {
  const harness = createHarness();
  try {
    await harness.runtime.start();
    assert.notEqual(harness.runtime.timer, null);
    assert.equal(harness.calls.restore, 0);
  } finally {
    await harness.runtime.stop();
  }
});

test("a transient lifecycle restore failure blocks downstream work but not #70", async () => {
  const harness = createHarness();
  harness.control.failRestore = 1;

  await harness.runtime.runOnce();
  assert.equal(harness.calls.restore, 1);
  assert.deepEqual(harness.state.advanced, [BASE]);
  assert.equal(harness.runtime.engine.lastEvaluatedBoundary, null);
  assert.equal(harness.calls.history, 0);
  assert.equal(harness.calls.persist.length, 0);

  // Inside the backoff window the read is not retried on every one-second tick.
  harness.state.now = BASE + 1_000;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.restore, 1, "restore is not retried before the backoff elapses");
  assert.equal(harness.state.advanced.length, 2);
  assert.equal(harness.runtime.engine.lastEvaluatedBoundary, null);
  assert.equal(harness.calls.history, 0);
  assert.equal(harness.calls.persist.length, 0);

  // Once the backoff elapses the read succeeds and evaluation begins.
  harness.state.now = BASE + MOVEMENT_RETRY_BASE_MS + 1_000;
  harness.state.boundary = BASE + 5_000;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.restore, 2);
  assert.equal(harness.state.advanced.at(-1), BASE + 5_000);
  assert.equal(harness.runtime.engine.lastEvaluatedBoundary, BASE + 5_000);
  assert.ok(harness.calls.persist.length > 0);
});

test("a failed normalization-history load is retried with backoff, not every tick", async () => {
  const harness = createHarness();
  harness.control.failHistory = 1;

  await harness.runtime.runOnce();
  assert.equal(harness.calls.history, 1);

  harness.state.now = BASE + 1_000;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.history, 1, "history is not retried on the next one-second tick");

  harness.state.now = BASE + MOVEMENT_RETRY_BASE_MS + 1_000;
  harness.state.boundary = BASE + 5_000;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.history, 2, "history is retried after the bounded backoff");
});

test("a changed universe forces an immediate raw-history refresh", async () => {
  const harness = createHarness();
  await harness.runtime.runOnce();
  assert.equal(harness.calls.history, 1);

  // A newly completed minute changes the raw candle cutoff.
  harness.state.now = BASE + 5_000;
  harness.state.boundary = BASE + 5_000;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.history, 2);

  // A newly watched symbol changes the universe version immediately.
  harness.control.symbols = [...SYMBOLS, "LINKUSDT"];
  harness.state.now = BASE + 10_000;
  harness.state.boundary = BASE + 10_000;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.history, 3, "a universe change forces an immediate refresh");
});

test("completed collector backfill can force normalization history to refresh immediately", async () => {
  const harness = createHarness();
  await harness.runtime.runOnce();
  assert.equal(harness.calls.history, 1);

  harness.runtime.requestNormalizationHistoryRefresh();
  harness.state.now = BASE + 5_000;
  harness.state.boundary = BASE + 5_000;
  await harness.runtime.runOnce();

  assert.equal(harness.calls.history, 2);
  assert.equal(harness.calls.register.length, 2);
  assert.equal(harness.calls.historyReads[1].beforeBoundaryMs, BASE + 5_000);
});

test("reacquiring the lease reloads durable state and discards the previous tenure's batch", async () => {
  const harness = createHarness();
  try {
    // Tenure A evaluates at T and confirms a STARTED transition at T+5s whose write fails.
    await harness.runtime.runOnce();
    const firstCurrent = harness.calls.persist[0].current;
    harness.state.now = BASE + 5_000;
    harness.state.boundary = BASE + 5_000;
    harness.control.failPersist = true;
    await harness.runtime.runOnce();
    assert.equal(harness.calls.persist.length, 2);
    assert.equal(harness.persistedEvents.size, 0);
    const staleEventId = harness.calls.persist[1].events[0].event_id;

    // Lease lost: A discards its in-memory ownership, including the pending batch.
    await harness.runtime.stop();

    // Another owner advanced durable state to T+N while A was inactive.
    const advancedAt = BASE + 30_000;
    harness.setDurableCurrent(advancedCurrent(firstCurrent, advancedAt));

    // A reacquires the lease and must reload T+N rather than resume from T.
    await harness.runtime.start();
    assert.equal(harness.calls.persist.length, 2, "reacquisition itself writes nothing");

    // A boundary before T+N must not be evaluated, and the stale batch must not be written.
    harness.state.now = BASE + 5_000;
    harness.state.boundary = BASE + 5_000;
    await harness.runtime.runOnce();
    assert.equal(
      harness.calls.persist.length,
      2,
      "must not resume from the previous tenure's last evaluated boundary",
    );
    assert.equal(
      harness.persistedEvents.has(staleEventId),
      false,
      "the previous tenure's pending batch is not written after reacquisition",
    );

    // Evaluation resumes with the exact restored Python state and one restart flag.
    const restoredCurrent = advancedCurrent(firstCurrent, advancedAt);
    harness.state.now = advancedAt + 5_000;
    harness.state.boundary = advancedAt + 5_000;
    await harness.runtime.runOnce();
    assert.deepEqual(harness.calls.lifecycle.at(-1).previousLifecycleState,
      restoredCurrent.lifecycleState);
    assert.equal(harness.calls.lifecycle.at(-1).interruptPreviousState, true);
    assert.equal(harness.calls.persist.length, 2, "cadence is based on the restored boundary");

    harness.state.now = advancedAt + 10_000;
    harness.state.boundary = advancedAt + 10_000;
    await harness.runtime.runOnce();
    assert.equal(harness.calls.lifecycle.at(-1).interruptPreviousState, false);
    assert.equal(harness.calls.persist.length, 2);

    harness.state.now = advancedAt + 30_000;
    harness.state.boundary = advancedAt + 30_000;
    await harness.runtime.runOnce();
    assert.equal(harness.calls.persist.length, 3);
    assert.equal(harness.calls.persist[2].current.evaluationBoundaryTime, advancedAt + 30_000);
    assert.deepEqual(harness.calls.persist[2].events, []);
  } finally {
    await harness.runtime.stop();
  }
});
