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

const movementBucketsUrl = await transpile("../src/lib/market/movement-buckets.ts");
const stateUrl = await transpile("../src/lib/market/market-movement-state.ts");
const metricsUrl = await transpile("../src/lib/market/movement-metrics.ts", {
  "./movement-buckets": movementBucketsUrl,
});
const classifierUrl = await transpile("../src/lib/market/market-state-classifier.ts");
const lifecycleUrl = await transpile("../src/lib/market/market-episode-lifecycle.ts");
const universeUrl = await transpile("../src/lib/market/market-universe.ts", {
  "./market-movement-state": stateUrl,
});
const finalizationUrl = await transpile("../src/lib/market/movement-finalization.ts", {
  "./movement-buckets": movementBucketsUrl,
});
const engineUrl = await transpile("../src/lib/market/market-movement-engine.ts", {
  "./movement-buckets": movementBucketsUrl,
  "./movement-metrics": metricsUrl,
  "./market-state-classifier": classifierUrl,
  "./market-episode-lifecycle": lifecycleUrl,
  "./market-universe": universeUrl,
});

const {
  MarketMovementEngine,
  buildMovementCurrentEvidence,
  buildMovementNormalizationHistory,
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
          requiredHistoryMs: windowMinutes * 2 * MINUTE_MS,
          availableHistoryMs: 0,
        },
      ]),
    ),
  };
}

function available(value) {
  return { available: true, value };
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
    availableAccelerationCount: 8,
    accelerationCoverage: 1,
    positiveAccelerationFraction: 0.8,
    negativeAccelerationFraction: 0.2,
    dispersion: available(0.2),
    rvolSummary: { availableCount: 8, coverage: 1, median: available(1.2), bySymbol: [] },
    isolatedOutliers: [],
    ...overrides,
  };
}

function fakeWindow(windowMinutes, directionState, pace) {
  const horizonRole =
    windowMinutes === 1 ? "RAPID" : windowMinutes === 5 ? "PRIMARY" : "PERSISTENCE";
  return {
    windowMinutes,
    horizonRole,
    directionState,
    pace,
    reversalCandidate: false,
    algorithmVersion: "market-state-v1",
    configVersion: "market-state-config-v1",
    movementAlgorithmVersion: "market-movement-v1",
    movementConfigVersion: "market-movement-config-v1",
    universeId: MARKET_UNIVERSE_ID,
    universeVersion: "market-universe-v1:test",
    evaluationBoundaryTime: BASE,
    provider: "binance-usdm",
    exchange: "binance",
    priceType: "trade",
    evidence: fakeEvidence(),
  };
}

function fakeClassification() {
  return {
    algorithmVersion: "market-state-v1",
    configVersion: "market-state-config-v1",
    movementAlgorithmVersion: "market-movement-v1",
    movementConfigVersion: "market-movement-config-v1",
    universeId: MARKET_UNIVERSE_ID,
    universeVersion: "market-universe-v1:test",
    evaluationBoundaryTime: BASE,
    provider: "binance-usdm",
    exchange: "binance",
    priceType: "trade",
    primaryWindowMinutes: 5,
    windows: [
      fakeWindow(1, "NEUTRAL", "NOT_APPLICABLE"),
      fakeWindow(5, "BROAD_RISE", "ACCELERATING"),
      fakeWindow(15, "BROAD_RISE", "DECELERATING"),
    ],
  };
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

test("one shared finalized boundary produces exactly one evaluation", () => {
  const engine = new MarketMovementEngine();
  const universe = buildMarketUniverse(SYMBOLS);
  const snapshots = new Map(SYMBOLS.map((symbol) => [symbol, warmingSnapshot(symbol)]));
  const sourceStatus = new Map(SYMBOLS.map((symbol) => [symbol, "LIVE"]));
  const input = {
    finalizableBoundary: BASE,
    snapshots,
    sourceStatus,
    historical: new Map(),
    universe,
  };

  const first = engine.advance(input);
  assert.equal(first.length, 1);
  assert.equal(engine.lastEvaluatedBoundary, BASE);

  // Re-advancing to the same boundary performs no new market evaluation.
  assert.deepEqual(engine.advance(input), []);

  // The next boundary evaluates exactly once more.
  const next = engine.advance({ ...input, finalizableBoundary: BASE + 5_000 });
  assert.equal(next.length, 1);
  assert.equal(engine.lastEvaluatedBoundary, BASE + 5_000);
});

test("all three windows are evaluated and a fresh restart stays WARMING", () => {
  const engine = new MarketMovementEngine();
  const universe = buildMarketUniverse(SYMBOLS);
  const snapshots = new Map(SYMBOLS.map((symbol) => [symbol, warmingSnapshot(symbol)]));
  const results = engine.advance({
    finalizableBoundary: BASE,
    snapshots,
    sourceStatus: new Map(SYMBOLS.map((symbol) => [symbol, "LIVE"])),
    historical: normalizationHistory(SYMBOLS),
    universe,
  });
  assert.equal(results.length, 1);
  const { classification } = results[0];
  assert.deepEqual(
    classification.windows.map((window) => window.windowMinutes),
    [1, 5, 15],
  );
  for (const window of classification.windows) {
    assert.equal(window.directionState, "WARMING");
    assert.equal(window.evidence.eligibleCount, 0);
  }
  // A restart has no persisted five-second history: the live path warms, it does
  // not reconstruct buckets from candles.
  assert.equal(classification.windows.find((w) => w.windowMinutes === 5).directionState, "WARMING");
});

test("normalization history is derived from contiguous one-minute candles only", () => {
  const minutes = 200;
  const candles = Array.from({ length: minutes }, (_, index) => ({
    openTime: BASE + index * MINUTE_MS,
    close: 100 + index * 0.1,
    volume: 2,
  }));
  const history = buildMovementNormalizationHistory(new Map([["BTCUSDT", candles]]));
  const entry = history.get("BTCUSDT");
  assert.equal(entry[1].returns.length, minutes - 1);
  assert.equal(entry[1].previousNotionalVolumes.length, 20);
  assert.equal(entry[1].usableCoverageMs, minutes * MINUTE_MS);
  assert.equal(entry[5].returns.length, 39);
  assert.equal(entry[15].returns.length, 12);
  const expected = Math.log(candles[1].close / candles[0].close);
  assert.ok(Math.abs(entry[1].returns[0] - expected) < 1e-12);
});

test("a history gap drops non-comparable earlier candles", () => {
  const minutes = 200;
  const candles = Array.from({ length: minutes }, (_, index) => ({
    openTime: BASE + index * MINUTE_MS,
    close: 100 + index * 0.1,
    volume: 1,
  }));
  const gapped = [...candles.slice(0, 100), ...candles.slice(101)];
  const history = buildMovementNormalizationHistory(new Map([["BTCUSDT", gapped]]));
  const entry = history.get("BTCUSDT");
  // Only the trailing contiguous run (99 candles) is trusted.
  assert.equal(entry[1].returns.length, 98);
  assert.equal(entry[1].usableCoverageMs, 99 * MINUTE_MS);
});

test("5m/15m history sampling is exchange-time aligned and phase-independent", () => {
  const minutes = 130;
  const candleAt = (offsetMinutes) => ({
    openTime: BASE + offsetMinutes * MINUTE_MS,
    close: 100 + offsetMinutes * 0.1 + (offsetMinutes % 5) * 0.01,
    volume: 1 + (offsetMinutes % 3),
  });
  // The first three minutes are irrelevant to the 5m/15m population: dropping
  // them must not shift the aligned endpoints or their returns/notionals.
  const withLeading = Array.from({ length: minutes }, (_, index) => candleAt(index));
  const withoutLeading = Array.from({ length: minutes - 3 }, (_, index) => candleAt(index + 3));

  const withLeadingHistory = buildMovementNormalizationHistory(
    new Map([["BTCUSDT", withLeading]]),
  ).get("BTCUSDT");
  const withoutLeadingHistory = buildMovementNormalizationHistory(
    new Map([["BTCUSDT", withoutLeading]]),
  ).get("BTCUSDT");

  for (const windowMinutes of [5, 15]) {
    assert.deepEqual(
      withoutLeadingHistory[windowMinutes].returns,
      withLeadingHistory[windowMinutes].returns,
      `${windowMinutes}m returns must not shift with irrelevant leading candles`,
    );
    assert.deepEqual(
      withoutLeadingHistory[windowMinutes].previousNotionalVolumes,
      withLeadingHistory[windowMinutes].previousNotionalVolumes,
      `${windowMinutes}m notional windows must use the same aligned intervals`,
    );
  }
  // 1m remains canonical one-minute sampling.
  assert.equal(withLeadingHistory[1].returns.length, minutes - 1);
});

test("5m history uses the 1m close completing at the boundary, not the boundary-open candle", () => {
  const boundary = BASE; // 5m-aligned; call it 12:05 for the regression narrative
  // openTimes are boundary-relative minutes: -6 = 11:59, -1 = 12:04, 0 = 12:05.
  const closes = [
    [-6, 200], // 11:59-open, the close immediately preceding the 12:00 boundary
    [-5, 1],
    [-4, 1],
    [-3, 1],
    [-2, 1],
    [-1, 100], // 12:04-open, the close immediately preceding the 12:05 boundary
    [0, 999], // 12:05-open, must never represent the 12:05 boundary
  ];
  const candles = closes.map(([offset, close]) => ({
    openTime: boundary + offset * MINUTE_MS,
    close,
    volume: 1,
  }));

  const returns = buildMovementNormalizationHistory(new Map([["BTCUSDT", candles]])).get(
    "BTCUSDT",
  )[5].returns;
  assert.equal(returns.length, 1);
  // 12:05 boundary uses the 12:04-open close (100) against the 11:59-open close (200).
  assert.ok(Math.abs(returns[0] - Math.log(100 / 200)) < 1e-12);
  assert.notEqual(returns[0], Math.log(999 / 1));
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
    evidence: classification.windows[1].evidence,
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
    evidence: classification.windows[1].evidence,
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
    evidence: classification.windows[1].evidence,
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
