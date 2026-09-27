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
const episodeClassificationUrl = await transpile("../src/lib/market/market-episode-classification.ts");
const lifecycleUrl = await transpile("../src/lib/market/market-episode-lifecycle.ts");
const universeUrl = await transpile("../src/lib/market/market-universe.ts", {
  "./market-movement-state": stateUrl,
});
const finalizationUrl = await transpile("../src/lib/market/movement-finalization.ts", {
  "./movement-contract": movementBucketsUrl,
});
const engineUrl = await transpile("../src/lib/market/market-movement-engine.ts", {
  "./movement-contract": movementBucketsUrl,
  "./movement-metrics-contract": metricsUrl,
  "./market-episode-classification": episodeClassificationUrl,
  "./market-episode-lifecycle": lifecycleUrl,
  "./market-universe": universeUrl,
});

const {
  MarketMovementEngine,
  buildMovementCurrentEvidence,
  snapshotEventTimes,
} = await import(engineUrl);
const {
  deriveMovementEngineStatus,
  toMovementEngineDiagnostics,
  toMarketMovementCurrentState,
  MARKET_UNIVERSE_ID,
  MOVEMENT_ENGINE_STALE_AFTER_MS,
} = await import(stateUrl);
const { buildMarketUniverse } = await import(universeUrl);
const {
  finalizableMovementBoundary,
  movementFinalizationConfig,
  MOVEMENT_FINALIZATION_GRACE_MS_DEFAULT,
} = await import(finalizationUrl);

const BASE = 1_800_000_000_000; // aligned to a five-second epoch boundary
const MINUTE_MS = 60_000;
const SYMBOLS = [
  "BTCUSDT",
  "ETHUSDT",
  "DOGEUSDT",
  "SOLUSDT",
  "XRPUSDT",
  "ADAUSDT",
  "BNBUSDT",
  "AVAXUSDT",
  "LINKUSDT",
  "POLUSDT",
];

function warmingSnapshot(symbol) {
  return {
    symbol,
    provider: "binance-usdm",
    instrumentId: `binance-usdm:${symbol}`,
    priceType: "trade",
    bucketMs: 5_000,
    maxLastTradeAgeMs: 15_000,
    buckets: [],
    latestRealTradeTime: null,
    latestRealReceivedAt: null,
    readiness: Object.fromEntries(
      [1, 5, 15].map((windowMinutes) => [
        windowMinutes,
        {
          windowMinutes,
          status: "WARMING",
          state: "warming",
          reason: "insufficient_exact_live_history",
        },
      ]),
    ),
  };
}

function available(value) {
  return { available: true, value, reason: null };
}

/** Candles persist across restart; only the in-memory five-second buckets do not. */
function normalizationHistory(symbols) {
  const map = new Map();
  for (const symbol of symbols) {
    const entry = {
      returns: [0.01, 0.02, -0.01],
      usableCoverageMs: 7 * 24 * 60 * MINUTE_MS,
      previousNotionalVolumes: Array.from({ length: 20 }, () => 1_000),
    };
    map.set(symbol, { 1: { ...entry }, 5: { ...entry }, 15: { ...entry } });
  }
  return map;
}

function unavailable(reason = "MARKET_UNIVERSE_INELIGIBLE") {
  return { available: false, value: null, reason };
}

function fakeEvidence(overrides = {}) {
  return {
    eligibleCount: 8,
    eligibleFraction: 0.8,
    includedSymbols: ["BTCUSDT", "ETHUSDT"],
    excludedSymbols: [{ symbol: "ADAUSDT", reasons: ["STALE_LAST_TRADE"] }],
    flatFraction: 0.1,
    risingFraction: 0.8,
    fallingFraction: 0.1,
    materialRisingFraction: 0.6,
    materialFallingFraction: 0.1,
    medianRawReturn: available(0.01),
    medianNormalizedMovement: available(0.9),
    medianAcceleration: available(0.2),
    positiveAccelerationFraction: 0.8,
    negativeAccelerationFraction: 0.2,
    dispersion: available(0.2),
    rvolSummary: [],
    isolatedOutliers: [],
    ...overrides,
  };
}

function fakeClassification() {
  const movement = broadRiseMovement(buildMarketUniverse(SYMBOLS), BASE);
  const classification = classificationFor(movement, "BROAD_RISE");
  classification.windows[0].directionState = "NEUTRAL";
  classification.windows[0].pace = unavailable("NO_BROAD_DIRECTION");
  classification.windows[2].pace = available("DECELERATING");
  classification.windows[1].includedSymbols = ["BTCUSDT", "ETHUSDT"];
  classification.windows[1].excludedSymbols = [
    { symbol: "ADAUSDT", reasons: ["STALE_LAST_TRADE"] },
  ];
  return classification;
}

test("finalization watermark never finalizes the current wall-clock boundary early", () => {
  const config = { version: "v", graceMs: 2_000 };
  assert.equal(finalizableMovementBoundary(BASE, config), BASE - 5_000);
  assert.equal(finalizableMovementBoundary(BASE + 1_999, config), BASE - 5_000);
  assert.equal(finalizableMovementBoundary(BASE + 2_000, config), BASE);
  assert.equal(finalizableMovementBoundary(BASE + 4_999, config), BASE);
  assert.equal(finalizableMovementBoundary(BASE + 5_000, config), BASE);
  assert.throws(() => finalizableMovementBoundary(BASE, { version: "v", graceMs: -1 }));
});

test("grace is explicit, server-side and versioned", () => {
  const defaultConfig = movementFinalizationConfig({});
  assert.equal(defaultConfig.graceMs, MOVEMENT_FINALIZATION_GRACE_MS_DEFAULT);
  // The effective version must unambiguously identify the grace in force.
  assert.ok(defaultConfig.version.startsWith("movement-finalization-config-v1"));
  assert.ok(defaultConfig.version.includes(String(MOVEMENT_FINALIZATION_GRACE_MS_DEFAULT)));
  const tuned = movementFinalizationConfig({ MOVEMENT_FINALIZATION_GRACE_MS: "5000" });
  assert.equal(tuned.graceMs, 5_000);
  assert.ok(tuned.version.includes("5000"));
  assert.notEqual(tuned.version, defaultConfig.version);
  assert.throws(() => movementFinalizationConfig({ MOVEMENT_FINALIZATION_GRACE_MS: "-1" }));
  assert.throws(() => movementFinalizationConfig({ MOVEMENT_FINALIZATION_GRACE_MS: "nope" }));
});

test("universe version is deterministic and changes only with membership", () => {
  const first = buildMarketUniverse(["btcusdt", "ETHUSDT"]);
  const reordered = buildMarketUniverse(["ETHUSDT", "BTCUSDT"]);
  assert.equal(first.id, MARKET_UNIVERSE_ID);
  assert.deepEqual(first.symbols, ["BTCUSDT", "ETHUSDT"]);
  assert.equal(first.version, reordered.version);
  assert.notEqual(buildMarketUniverse(["BTCUSDT"]).version, first.version);
});

function warmingMovement(universe, boundary) {
  const missing = { available: false, value: null, reason: "MARKET_UNIVERSE_INELIGIBLE" };
  return {
    algorithmVersion: "market-movement-v1", configVersion: "market-movement-config-v1",
    universeId: universe.id, universeVersion: universe.version,
    configuredUniverse: [...universe.symbols],
    provider: "binance-usdm", exchange: "binance", priceType: "trade",
    evaluationBoundaryTime: boundary,
    windows: [1, 5, 15].map((windowMinutes) => ({
      algorithmVersion: "market-movement-v1", configVersion: "market-movement-config-v1",
      universeId: universe.id, universeVersion: universe.version,
      configuredUniverse: [...universe.symbols], includedSymbols: [],
      excludedSymbols: universe.symbols.map((symbol) => ({
        symbol, reasons: ["WARMING_INSUFFICIENT_LIVE_HISTORY"],
      })),
      windowMinutes, provider: "binance-usdm", exchange: "binance", priceType: "trade",
      evaluationBoundaryTime: boundary, historicalLookbackMs: 7 * 24 * 60 * MINUTE_MS,
      minimumHistoricalCoverageMs: 3 * 24 * 60 * MINUTE_MS,
      marketWideEligible: false, eligibleCount: 0, eligibleFraction: 0, symbols: [],
      breadth: { available: false, unavailableReason: "MARKET_UNIVERSE_INELIGIBLE",
        denominator: 0, flatFraction: null, risingFraction: null, fallingFraction: null,
        materialRisingFraction: null, materialFallingFraction: null },
      aggregates: { medianRawReturn: missing, medianNormalizedMovement: missing,
        dispersionMadNormalizedMovement: missing },
    })),
  };
}

function broadRiseMovement(universe, boundary) {
  const movement = warmingMovement(universe, boundary);
  for (const window of movement.windows) {
    window.marketWideEligible = true;
    window.eligibleCount = 8;
    window.eligibleFraction = 0.8;
    window.includedSymbols = universe.symbols.slice(0, 8);
    window.excludedSymbols = [];
    window.breadth = {
      available: true, unavailableReason: null, denominator: 8,
      flatFraction: 0.1, risingFraction: 0.8, fallingFraction: 0.1,
      materialRisingFraction: 0.8, materialFallingFraction: 0.1,
    };
    window.aggregates = {
      medianRawReturn: available(0.02),
      medianNormalizedMovement: available(1),
      dispersionMadNormalizedMovement: available(0.1),
    };
  }
  return movement;
}

// Explicit Python #72 results: these fixtures do not run a TypeScript classifier.
function classificationFor(movement, directionState) {
  const side = (count, fraction) => available({ count, fraction });
  return {
    classifierAlgorithmVersion: "market-state-classifier-v1",
    classifierConfigVersion: "market-state-classifier-config-v1",
    classifierConfig: { version: "market-state-classifier-config-v1",
      directionalBreadth: 0.7, materialBreadth: 0.5, normalizedMovement: 0.5,
      accelerationBreadth: 0.6, isolatedOutlierBreadthDisagreement: 0.5 },
    movementAlgorithmVersion: movement.algorithmVersion,
    movementConfigVersion: movement.configVersion,
    universeId: movement.universeId, universeVersion: movement.universeVersion,
    provider: movement.provider, exchange: movement.exchange, priceType: movement.priceType,
    evaluationBoundaryTime: movement.evaluationBoundaryTime, primaryWindowMinutes: 5,
    windows: movement.windows.map((snapshot) => {
      const broad = directionState === "BROAD_RISE";
      const n = snapshot.eligibleCount;
      const rising = broad ? side(8, 0.8) : unavailable();
      const falling = broad ? side(1, 0.1) : unavailable();
      return {
        windowMinutes: snapshot.windowMinutes,
        horizonRole: snapshot.windowMinutes === 1 ? "RAPID" :
          snapshot.windowMinutes === 5 ? "PRIMARY" : "PERSISTENCE",
        isPrimary: snapshot.windowMinutes === 5,
        directionState,
        pace: broad ? available("ACCELERATING") : unavailable("NO_BROAD_DIRECTION"),
        priorConfirmedEpisodeDirection: null,
        reversalCandidate: null,
        isolatedOutliers: [],
        breadth: { flat: broad ? side(1, 0.1) : unavailable(),
          rising, falling, materialRising: broad ? side(8, 0.8) : unavailable(),
          materialFalling: broad ? side(1, 0.1) : unavailable(), denominator: n },
        medianRawReturn: snapshot.aggregates.medianRawReturn,
        medianNormalizedMovement: snapshot.aggregates.medianNormalizedMovement,
        medianAcceleration: broad ? available(0.1) : unavailable("ACCELERATION_UNAVAILABLE"),
        positiveAccelerationBreadth: broad ? side(8, 1) : unavailable("ACCELERATION_UNAVAILABLE"),
        negativeAccelerationBreadth: broad ? side(0, 0) : unavailable("ACCELERATION_UNAVAILABLE"),
        dispersionMadNormalizedMovement: snapshot.aggregates.dispersionMadNormalizedMovement,
        trimmedMeanNormalizedMovement: unavailable(),
        liquidityWeightedNormalizedMovement: unavailable(),
        liquidityWeights: unavailable(),
        volumeContext: [], marketWideEligible: snapshot.marketWideEligible,
        eligibleCount: n, eligibleFraction: snapshot.eligibleFraction,
        configuredUniverse: snapshot.configuredUniverse,
        includedSymbols: snapshot.includedSymbols,
        excludedSymbols: snapshot.excludedSymbols,
        availabilityReasons: broad ? [] : ["WARMING_INSUFFICIENT_LIVE_HISTORY"],
        classifierAlgorithmVersion: "market-state-classifier-v1",
        classifierConfigVersion: "market-state-classifier-config-v1",
        movementAlgorithmVersion: movement.algorithmVersion,
        movementConfigVersion: movement.configVersion,
        universeId: movement.universeId, universeVersion: movement.universeVersion,
        provider: movement.provider, exchange: movement.exchange, priceType: movement.priceType,
        evaluationBoundaryTime: movement.evaluationBoundaryTime,
        sourceTimeEvidence: snapshot.configuredUniverse.map((symbol) => ({
          symbol, lastRealTradeTimeMs: null, lastRealEventTimeMs: null,
          lastReceivedAtMs: null,
        })),
        movementSnapshot: snapshot,
      };
    }),
  };
}

function assessmentFor(movement, directionState) {
  return { movement, classification: classificationFor(movement, directionState) };
}

test("one shared finalized boundary produces exactly one canonical supplier call", async () => {
  const engine = new MarketMovementEngine();
  const universe = buildMarketUniverse(SYMBOLS);
  const calls = [];
  const input = {
    finalizableBoundary: BASE,
    universe,
    assessmentForBoundary: async (boundary) => {
      calls.push(boundary);
      return assessmentFor(warmingMovement(universe, boundary), "WARMING");
    },
  };

  const first = await engine.advance(input);
  assert.equal(first.length, 1);
  assert.equal(engine.lastEvaluatedBoundary, BASE);
  assert.deepEqual(calls, [BASE]);

  // Re-advancing to the same boundary performs no new market evaluation.
  assert.deepEqual(await engine.advance(input), []);

  // The next boundary evaluates exactly once more.
  const next = await engine.advance({ ...input, finalizableBoundary: BASE + 5_000 });
  assert.equal(next.length, 1);
  assert.equal(engine.lastEvaluatedBoundary, BASE + 5_000);
  assert.deepEqual(calls, [BASE, BASE + 5_000]);
});

test("canonical Python assessment passes unchanged and a fresh restart stays WARMING", async () => {
  const engine = new MarketMovementEngine();
  const universe = buildMarketUniverse(SYMBOLS);
  const movement = warmingMovement(universe, BASE);
  const assessment = assessmentFor(movement, "WARMING");
  const results = await engine.advance({
    finalizableBoundary: BASE,
    universe,
    assessmentForBoundary: async () => assessment,
  });
  assert.equal(results.length, 1);
  assert.strictEqual(results[0].movement, movement);
  assert.strictEqual(results[0].classification, assessment.classification);
  const { classification } = results[0];
  assert.deepEqual(
    classification.windows.map((window) => window.windowMinutes),
    [1, 5, 15],
  );
  for (const window of classification.windows) {
    assert.equal(window.directionState, "WARMING");
    assert.equal(window.eligibleCount, 0);
  }
  // A restart has no persisted five-second history: the live path warms, it does
  // not reconstruct buckets from candles.
  assert.equal(classification.windows.find((w) => w.windowMinutes === 5).directionState, "WARMING");
});

test("failed or mismatched canonical movement never advances lifecycle boundary", async () => {
  const engine = new MarketMovementEngine();
  const universe = buildMarketUniverse(SYMBOLS);
  await assert.rejects(engine.advance({ finalizableBoundary: BASE, universe,
    assessmentForBoundary: async () => { throw new Error("Python unavailable"); } }));
  assert.equal(engine.lastEvaluatedBoundary, null);
  await assert.rejects(engine.advance({ finalizableBoundary: BASE, universe,
    assessmentForBoundary: async () => assessmentFor(
      warmingMovement(universe, BASE + 5_000), "WARMING") }));
  assert.equal(engine.lastEvaluatedBoundary, null);
  await assert.rejects(engine.advance({ finalizableBoundary: BASE, universe,
    assessmentForBoundary: async () => {
      const assessment = assessmentFor(warmingMovement(universe, BASE), "WARMING");
      assessment.classification.windows[1].universeVersion = "wrong";
      return assessment;
    } }));
  assert.equal(engine.lastEvaluatedBoundary, null);
});

test("each catch-up assessment receives the staged prior confirmed direction", async () => {
  const engine = new MarketMovementEngine();
  const universe = buildMarketUniverse(SYMBOLS);
  const supplied = [];
  await engine.advance({ finalizableBoundary: BASE, universe,
    assessmentForBoundary: async (boundary, prior) => {
      supplied.push([boundary, prior]);
      return assessmentFor(broadRiseMovement(universe, boundary), "BROAD_RISE");
    } });
  await engine.advance({ finalizableBoundary: BASE + 10_000, universe,
    assessmentForBoundary: async (boundary, prior) => {
      supplied.push([boundary, prior]);
      return assessmentFor(broadRiseMovement(universe, boundary), "BROAD_RISE");
    } });
  assert.deepEqual(supplied, [
    [BASE, null], [BASE + 5_000, null], [BASE + 10_000, "BROAD_RISE"],
  ]);
});

test("catch-up commits lifecycle and boundary only after every canonical result succeeds", async () => {
  const engine = new MarketMovementEngine();
  const universe = buildMarketUniverse(SYMBOLS);
  await engine.advance({ finalizableBoundary: BASE, universe,
    assessmentForBoundary: async (boundary) => assessmentFor(
      broadRiseMovement(universe, boundary), "BROAD_RISE") });
  const boundaryBefore = engine.lastEvaluatedBoundary;
  const lifecycleBefore = structuredClone(engine.lifecycleState);
  let failSecond = true;
  await assert.rejects(engine.advance({ finalizableBoundary: BASE + 10_000, universe,
    assessmentForBoundary: async (boundary) => {
      if (boundary === BASE + 10_000 && failSecond) throw new Error("Python #71 unavailable");
      return assessmentFor(broadRiseMovement(universe, boundary), "BROAD_RISE");
    } }));
  assert.equal(engine.lastEvaluatedBoundary, boundaryBefore);
  assert.deepEqual(engine.lifecycleState, lifecycleBefore);

  failSecond = false;
  const retry = await engine.advance({ finalizableBoundary: BASE + 10_000, universe,
    assessmentForBoundary: async (boundary) => assessmentFor(
      broadRiseMovement(universe, boundary), "BROAD_RISE") });
  assert.equal(retry.length, 2);
  assert.equal(retry[0].lifecycle.transitions[0]?.transition, "STARTED");
  assert.equal(engine.lastEvaluatedBoundary, BASE + 10_000);
});

test("universe change evaluates the latest finalized boundary from its own session", async () => {
  const engine = new MarketMovementEngine();
  const firstUniverse = buildMarketUniverse(SYMBOLS);
  await engine.advance({ finalizableBoundary: BASE, universe: firstUniverse,
    assessmentForBoundary: async (boundary) => assessmentFor(
      warmingMovement(firstUniverse, boundary), "WARMING") });
  const newUniverse = buildMarketUniverse([...SYMBOLS, "DOTUSDT"]);
  const calls = [];
  await engine.advance({ finalizableBoundary: BASE + 20_000, universe: newUniverse,
    assessmentForBoundary: async (boundary) => {
      calls.push(boundary);
      return assessmentFor(warmingMovement(newUniverse, boundary), "WARMING");
    } });
  assert.deepEqual(calls, [BASE + 20_000]);
});

test("engine status keeps LIVE, WARMING, STALE and UNAVAILABLE distinct", () => {
  const base = { configuredCount: 10, evaluationBoundaryTime: BASE, now: BASE + 1_000 };
  assert.equal(deriveMovementEngineStatus({ ...base, directionState: "NEUTRAL" }), "LIVE");
  assert.equal(deriveMovementEngineStatus({ ...base, directionState: "BROAD_RISE" }), "LIVE");
  assert.equal(deriveMovementEngineStatus({ ...base, directionState: "WARMING" }), "WARMING");
  assert.equal(
    deriveMovementEngineStatus({ ...base, directionState: "UNAVAILABLE" }),
    "UNAVAILABLE",
  );
  assert.equal(
    deriveMovementEngineStatus({
      directionState: "NEUTRAL",
      configuredCount: 4,
      evaluationBoundaryTime: BASE,
      now: BASE + 1_000,
    }),
    "UNAVAILABLE",
  );
  // The staleness threshold must clear the 30s current-state persistence cadence
  // plus finalization lag, so a healthy but recently-written row never flickers.
  assert.ok(MOVEMENT_ENGINE_STALE_AFTER_MS > 30_000);
  assert.equal(
    deriveMovementEngineStatus({ ...base, now: BASE + 30_000, directionState: "NEUTRAL" }),
    "LIVE",
  );
  assert.equal(
    deriveMovementEngineStatus({ ...base, now: BASE + 60_000, directionState: "NEUTRAL" }),
    "LIVE",
  );
  assert.equal(
    deriveMovementEngineStatus({ ...base, now: BASE + 60_001, directionState: "NEUTRAL" }),
    "STALE",
  );
});

test("1m/5m/15m context, universe and timestamps reach the persisted current evidence", () => {
  const universe = buildMarketUniverse(SYMBOLS);
  const classification = fakeClassification();
  const evidence = buildMovementCurrentEvidence({
    evidence: fakeEvidence(),
    classification,
    universe,
    status: "LIVE",
    lateAfterFinalizationCount: 3,
    engineUpdatedAt: BASE + 1_000,
    lastSourceEventTime: BASE + 900,
    lastTradeTime: BASE + 950,
    lastReceivedAt: BASE + 980,
    finalizationConfigVersion: "movement-finalization-config-v1:grace-2000",
    finalizationGraceMs: 2_000,
    mostRecentTransition: null,
  });
  assert.equal(evidence.windowsContext.length, 3);
  assert.deepEqual(
    evidence.windowsContext.map((window) => window.windowMinutes),
    [1, 5, 15],
  );
  assert.equal(evidence.universe.id, MARKET_UNIVERSE_ID);
  assert.equal(evidence.universe.configuredSymbols.length, 10);
  assert.deepEqual(evidence.universe.excludedSymbols, [
    { symbol: "ADAUSDT", reasons: ["STALE_LAST_TRADE"] },
  ]);
  assert.equal(evidence.engine.status, "LIVE");
  assert.equal(evidence.engine.lateAfterFinalizationCount, 3);
  assert.equal(evidence.timestamps.lastTradeTime, BASE + 950);
  assert.equal(evidence.timestamps.lastSourceEventTime, BASE + 900);
  assert.equal(evidence.timestamps.lastReceivedAt, BASE + 980);
  assert.equal(evidence.finalizationConfigVersion, "movement-finalization-config-v1:grace-2000");
  assert.equal(evidence.finalizationGraceMs, 2_000);
});

test("current-state exposure maps persisted evidence without leaking credentials", () => {
  const classification = fakeClassification();
  const currentEvidence = buildMovementCurrentEvidence({
    evidence: fakeEvidence(),
    classification,
    universe: buildMarketUniverse(SYMBOLS),
    status: "LIVE",
    lateAfterFinalizationCount: 3,
    engineUpdatedAt: BASE + 1_000,
    lastSourceEventTime: BASE + 900,
    lastTradeTime: BASE + 950,
    lastReceivedAt: BASE + 980,
    finalizationConfigVersion: "movement-finalization-config-v1:grace-2000",
    finalizationGraceMs: 2_000,
    mostRecentTransition: {
      transition: "STARTED",
      transitionReason: "confirmed_broad_entry",
      episodeId: "mep_test",
      direction: "BROAD_RISE",
      fromDirection: null,
      toDirection: "BROAD_RISE",
      pace: "ACCELERATING",
      episodeStartBoundaryTime: BASE,
      evaluationBoundaryTime: BASE,
    },
  });
  const persisted = {
    universeId: MARKET_UNIVERSE_ID,
    universeVersion: "market-universe-v1:test",
    evaluationBoundaryTime: BASE,
    directionState: "BROAD_RISE",
    pace: "ACCELERATING",
    activeEpisodeId: "mep_test",
    activeDirection: "BROAD_RISE",
    interrupted: false,
    episodeAlgorithmVersion: "market-episode-v1",
    lifecycleConfigVersion: "market-episode-config-v1",
    classifierAlgorithmVersion: "market-state-v1",
    classifierConfigVersion: "market-state-config-v1",
    movementAlgorithmVersion: "market-movement-v1",
    movementConfigVersion: "market-movement-config-v1",
    currentEvidence,
    // Simulated accidental extra column; the mapper must never forward it.
    service_role_key: "super-secret",
  };
  const state = toMarketMovementCurrentState(persisted, { now: BASE + 1_000 });
  assert.equal(state.available, true);
  assert.equal(state.status, "LIVE");
  assert.equal(state.primary.windowMinutes, 5);
  assert.equal(state.primary.directionState, "BROAD_RISE");
  assert.equal(state.context.length, 3);
  assert.equal(state.universe.configuredSymbols.length, 10);
  assert.equal(state.lateAfterFinalizationCount, 3);
  assert.equal(state.mostRecentTransition.transition, "STARTED");
  assert.equal(state.versions.universeVersion, "market-universe-v1:test");
  assert.equal(state.activeEpisode.episodeId, "mep_test");
  assert.equal(JSON.stringify(state).includes("super-secret"), false);
  assert.equal(JSON.stringify(state).includes("service_role_key"), false);
});

test("unavailable current state is exposed explicitly, never as a generic state", () => {
  const state = toMarketMovementCurrentState(null, { now: BASE });
  assert.equal(state.available, false);
  assert.equal(state.status, "UNAVAILABLE");
  assert.equal(state.primary, null);
  assert.deepEqual(state.context, []);
});

test("diagnostics reflect engine status, counts, timestamps and transition", () => {
  const classification = fakeClassification();
  const currentEvidence = buildMovementCurrentEvidence({
    evidence: fakeEvidence(),
    classification,
    universe: buildMarketUniverse(SYMBOLS),
    status: "LIVE",
    lateAfterFinalizationCount: 5,
    engineUpdatedAt: BASE + 1_000,
    lastSourceEventTime: BASE + 900,
    lastTradeTime: BASE + 950,
    lastReceivedAt: BASE + 980,
    finalizationConfigVersion: "movement-finalization-config-v1:grace-2000",
    finalizationGraceMs: 2_000,
    mostRecentTransition: null,
  });
  const diagnostics = toMovementEngineDiagnostics(
    {
      universeId: MARKET_UNIVERSE_ID,
      universeVersion: "market-universe-v1:test",
      evaluationBoundaryTime: BASE,
      directionState: "BROAD_RISE",
      pace: "ACCELERATING",
      activeEpisodeId: null,
      activeDirection: null,
      interrupted: false,
      episodeAlgorithmVersion: "market-episode-v1",
      lifecycleConfigVersion: "market-episode-config-v1",
      classifierAlgorithmVersion: "market-state-v1",
      classifierConfigVersion: "market-state-config-v1",
      movementAlgorithmVersion: "market-movement-v1",
      movementConfigVersion: "market-movement-config-v1",
      currentEvidence,
    },
    { now: BASE + 1_000 },
  );
  assert.equal(diagnostics.status, "LIVE");
  assert.equal(diagnostics.configuredSymbolCount, 10);
  assert.equal(diagnostics.eligibleSymbolCount, 8);
  assert.equal(diagnostics.primaryDirectionState, "BROAD_RISE");
  assert.equal(diagnostics.lastEvaluationBoundaryTime, BASE);
  assert.equal(diagnostics.lastSourceEventTime, BASE + 900);
  assert.equal(diagnostics.lateAfterFinalizationCount, 5);
  assert.equal(diagnostics.movementConfigVersion, "market-movement-config-v1");
  assert.equal(toMovementEngineDiagnostics(null, { now: BASE }), null);
});

test("finalization config/grace and receive time are auditable through the contracts", () => {
  const classification = fakeClassification();
  const currentEvidence = buildMovementCurrentEvidence({
    evidence: classification.windows[1].evidence,
    classification,
    universe: buildMarketUniverse(SYMBOLS),
    status: "LIVE",
    lateAfterFinalizationCount: 0,
    engineUpdatedAt: BASE + 1_000,
    lastSourceEventTime: BASE + 900,
    lastTradeTime: BASE + 950,
    lastReceivedAt: BASE + 980,
    finalizationConfigVersion: "movement-finalization-config-v1:grace-5000",
    finalizationGraceMs: 5_000,
    mostRecentTransition: null,
  });
  const persisted = {
    universeId: MARKET_UNIVERSE_ID,
    universeVersion: "market-universe-v1:test",
    evaluationBoundaryTime: BASE,
    directionState: "BROAD_RISE",
    pace: "ACCELERATING",
    activeEpisodeId: null,
    activeDirection: null,
    interrupted: false,
    episodeAlgorithmVersion: "market-episode-v1",
    lifecycleConfigVersion: "market-episode-config-v1",
    classifierAlgorithmVersion: "market-state-v1",
    classifierConfigVersion: "market-state-config-v1",
    movementAlgorithmVersion: "market-movement-v1",
    movementConfigVersion: "market-movement-config-v1",
    currentEvidence,
  };
  const state = toMarketMovementCurrentState(persisted, { now: BASE + 1_000 });
  assert.equal(state.timestamps.lastReceivedAt, BASE + 980);
  assert.equal(state.finalizationConfigVersion, "movement-finalization-config-v1:grace-5000");
  assert.equal(state.finalizationGraceMs, 5_000);

  const diagnostics = toMovementEngineDiagnostics(persisted, { now: BASE + 1_000 });
  assert.equal(diagnostics.finalizationConfigVersion, "movement-finalization-config-v1:grace-5000");
  assert.equal(diagnostics.finalizationGraceMs, 5_000);
});

test("unavailable current state exposes no finalization config", () => {
  const state = toMarketMovementCurrentState(null, { now: BASE });
  assert.equal(state.finalizationConfigVersion, null);
  assert.equal(state.finalizationGraceMs, null);
});

test("snapshot event times preserve exchange and receive provenance across symbols", () => {
  const times = snapshotEventTimes(
    new Map([
      [
        "BTCUSDT",
        {
          ...warmingSnapshot("BTCUSDT"),
          buckets: [
            {
              boundaryTime: BASE,
              lastRealEventTime: BASE + 500,
              lastRealTradeTime: BASE + 400,
              lastRealReceivedAt: BASE + 600,
            },
          ],
        },
      ],
      [
        "ETHUSDT",
        {
          ...warmingSnapshot("ETHUSDT"),
          buckets: [
            {
              boundaryTime: BASE,
              lastRealEventTime: BASE + 300,
              lastRealTradeTime: BASE + 200,
              lastRealReceivedAt: BASE + 250,
            },
          ],
        },
      ],
    ]),
  );
  assert.equal(times.lastTradeTime, BASE + 400);
  assert.equal(times.lastSourceEventTime, BASE + 500);
  assert.equal(times.lastReceivedAt, BASE + 600);
});

test("evaluation provenance uses the finalized bucket, not a newer pending trade", () => {
  const finalized = BASE + 500;
  const pending = BASE + 5_400; // still sitting in an unfinalized bucket
  const times = snapshotEventTimes(
    new Map([
      [
        "BTCUSDT",
        {
          ...warmingSnapshot("BTCUSDT"),
          // Snapshot-level fields point at the newer, not-yet-finalized trade.
          latestRealTradeTime: pending,
          latestRealReceivedAt: pending + 10,
          buckets: [
            {
              boundaryTime: BASE,
              lastRealEventTime: finalized,
              lastRealTradeTime: finalized - 1,
              lastRealReceivedAt: finalized + 2,
            },
          ],
        },
      ],
    ]),
  );
  assert.equal(times.lastSourceEventTime, finalized);
  assert.equal(times.lastTradeTime, finalized - 1);
  assert.equal(times.lastReceivedAt, finalized + 2);
});

test("snapshot event times expose no receive time when nothing was received", () => {
  const times = snapshotEventTimes(new Map([["BTCUSDT", warmingSnapshot("BTCUSDT")]]));
  assert.equal(times.lastTradeTime, null);
  assert.equal(times.lastSourceEventTime, null);
  assert.equal(times.lastReceivedAt, null);
});
