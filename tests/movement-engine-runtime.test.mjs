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
const classifierUrl = await transpile("../src/lib/market/market-state-classifier.ts");
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
  "./market-state-classifier": classifierUrl,
  "./market-episode-lifecycle": lifecycleUrl,
  "./market-universe": universeUrl,
});
const runtimeUrl = await transpile("../src/lib/market/movement-engine.server.ts", {
  "./market-movement-engine": engineUrl,
  "./market-universe": universeUrl,
  "./market-episode-lifecycle": lifecycleUrl,
  "./movement-finalization": finalizationUrl,
  "./movement-metrics-contract": metricsUrl,
  "./movement-normalization-input": normalizationUrl,
  "./market-state-classifier": classifierUrl,
  "./market-movement-state": stateUrl,
});

const { MovementEngineRuntime, MOVEMENT_RETRY_BASE_MS } = await import(runtimeUrl);

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

function createHarness() {
  const state = { now: BASE, boundary: BASE, advanced: [], sessionId: crypto.randomUUID() };
  const control = { failRestore: 0, failHistory: 0, failPersist: false,
    failMetrics: false, rotateDuringMetrics: false, changeUniverseDuringMetrics: false,
    symbols: [...SYMBOLS] };
  const calls = { restore: 0, history: 0, historyReads: [], register: [], metrics: [], persist: [] };
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
        const exists = persistedEvents.has(event.eventId);
        if (!exists) persistedEvents.set(event.eventId, event);
        return { eventId: event.eventId, status: exists ? "already_exists" : "appended" };
      });
    },
  };
  const setDurableCurrent = (current) => {
    durableCurrent = current;
  };
  const runtime = new MovementEngineRuntime({
    store,
    collector,
    async registerHistory(sessionId, historyVersion, universe, config, historical, asOfBoundary) {
      calls.register.push({sessionId, historyVersion, universe, config, historical, asOfBoundary});
    },
    async calculateMovement(sessionId, boundary, historyVersion, universe) {
      calls.metrics.push({sessionId, boundary, historyVersion, universe});
      if (control.failMetrics) throw new Error("Python #71 unavailable");
      if (control.rotateDuringMetrics) state.sessionId = crypto.randomUUID();
      if (control.changeUniverseDuringMetrics) control.symbols = [...SYMBOLS, "XRPUSDT"];
      return canonicalMovement(universe, boundary);
    },
    finalization: { version: "movement-finalization-config-v1:grace-0", graceMs: 0 },
    now: () => state.now,
  });
  return { runtime, state, control, calls, persistedEvents, setDurableCurrent };
}

/** Builds a durable current-state row advanced to `boundary` from a captured one. */
function advancedCurrent(current, boundary) {
  return {
    ...current,
    evaluationBoundaryTime: boundary,
    lifecycleState: {
      ...current.lifecycleState,
      evaluationBoundaryTime: boundary,
      lastPersistedTime: boundary,
    },
  };
}

test("runtime persists the effective finalization config and receive-time provenance", async () => {
  const harness = createHarness();
  await harness.runtime.runOnce();
  assert.equal(harness.calls.persist.length, 1);
  const evidence = harness.calls.persist[0].current.currentEvidence;
  assert.equal(evidence.finalizationConfigVersion, "movement-finalization-config-v1:grace-0");
  assert.equal(evidence.finalizationGraceMs, 0);
  assert.equal(evidence.timestamps.lastReceivedAt, BASE + 123);
});

test("history registers once per refresh and metrics use the same canonical session", async () => {
  const harness = createHarness();
  await harness.runtime.runOnce();
  assert.equal(harness.calls.register.length, 1);
  assert.equal(harness.calls.metrics.length, 1);
  assert.equal(harness.calls.register[0].asOfBoundary, BASE);
  assert.equal(harness.calls.historyReads[0].sinceMs,
    BASE - harness.calls.register[0].config.historicalLookbackMs);
  assert.equal(harness.calls.historyReads[0].beforeBoundaryMs, BASE);
  assert.equal(harness.calls.metrics[0].sessionId, harness.calls.register[0].sessionId);
  assert.equal(harness.calls.metrics[0].historyVersion, harness.calls.register[0].historyVersion);
  harness.state.now = BASE + 5_000;
  harness.state.boundary = BASE + 5_000;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.register.length, 1);
  assert.equal(harness.calls.metrics.length, 2);
  harness.state.sessionId = crypto.randomUUID();
  harness.state.now = BASE + 10_000;
  harness.state.boundary = BASE + 10_000;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.register.length, 2);
  assert.equal(harness.calls.register[1].sessionId, harness.state.sessionId);
  harness.state.now = BASE + 15 * MINUTE;
  harness.state.boundary = harness.state.now;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.register.length, 3);
  harness.control.symbols = [...SYMBOLS, "XRPUSDT"];
  harness.state.now += 5_000;
  harness.state.boundary = harness.state.now;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.register.length, 4);
});

test("Python #71 failure does not stop later #70 finalization or advance lifecycle", async () => {
  const harness = createHarness();
  harness.control.failMetrics = true;
  await harness.runtime.runOnce();
  assert.equal(harness.runtime.engine.lastEvaluatedBoundary, null);
  harness.state.now = BASE + 5_000;
  harness.state.boundary = BASE + 5_000;
  await harness.runtime.runOnce();
  assert.equal(harness.state.advanced.at(-1), BASE + 5_000);
  assert.equal(harness.runtime.engine.lastEvaluatedBoundary, null);
  assert.equal(harness.calls.persist.length, 0);
  harness.control.failMetrics = false;
  await harness.runtime.runOnce();
  assert.equal(harness.runtime.engine.lastEvaluatedBoundary, BASE + 5_000);
});

test("old session and changed universe responses are discarded before #72/#73", async () => {
  for (const change of ["rotateDuringMetrics", "changeUniverseDuringMetrics"]) {
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
  assert.equal(harness.persistedEvents.size, 0, "a failed write must not record the event");

  // A newer safe #70 boundary is finalized even when the required #73 retry
  // fails again. Downstream evaluation must remain at the pending boundary.
  harness.state.now = BASE + 10_000;
  harness.state.boundary = BASE + 10_000;
  harness.control.failPersist = true;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.persist.length, 3, "the pending batch is retried");
  assert.equal(harness.calls.persist[2].events.length, 1);
  assert.equal(
    harness.calls.persist[2].events[0].eventId,
    attempted[0].eventId,
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

test("a changed universe forces an immediate normalization-history refresh", async () => {
  const harness = createHarness();
  await harness.runtime.runOnce();
  assert.equal(harness.calls.history, 1);

  // Same universe within the refresh cadence: no reload.
  harness.state.now = BASE + 5_000;
  harness.state.boundary = BASE + 5_000;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.history, 1);

  // A newly watched symbol changes the universe version and must not wait 15m.
  harness.control.symbols = [...SYMBOLS, "LINKUSDT"];
  harness.state.now = BASE + 10_000;
  harness.state.boundary = BASE + 10_000;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.history, 2, "a universe change forces an immediate refresh");
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
    const staleEventId = harness.calls.persist[1].events[0].eventId;

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

    // Evaluation resumes from the reloaded boundary: candidate at T+N+5s, STARTED at T+N+10s.
    harness.state.now = advancedAt + 5_000;
    harness.state.boundary = advancedAt + 5_000;
    await harness.runtime.runOnce();
    assert.equal(harness.calls.persist.length, 2, "candidate accumulation alone does not persist");

    harness.state.now = advancedAt + 10_000;
    harness.state.boundary = advancedAt + 10_000;
    await harness.runtime.runOnce();
    assert.equal(harness.calls.persist.length, 3);
    assert.equal(harness.calls.persist[2].current.evaluationBoundaryTime, advancedAt + 10_000);
    assert.equal(harness.calls.persist[2].events[0].transition, "STARTED");
  } finally {
    await harness.runtime.stop();
  }
});
