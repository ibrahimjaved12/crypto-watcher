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
const runtimeUrl = await transpile("../src/lib/market/movement-engine.server.ts", {
  "./market-movement-engine": engineUrl,
  "./market-universe": universeUrl,
  "./market-episode-lifecycle": lifecycleUrl,
  "./movement-finalization": finalizationUrl,
  "./movement-metrics": metricsUrl,
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
  }));
}

function bucket(symbol, boundaryTime, endpointPrice) {
  return {
    boundaryTime,
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
          requiredHistoryMs: windowMinutes * 2 * MINUTE,
          availableHistoryMs: windowMinutes * 2 * MINUTE,
        },
      ]),
    ),
  };
}

function createHarness() {
  const state = { now: BASE, boundary: BASE, advanced: [] };
  const control = { failRestore: 0, failHistory: 0, failPersist: false, symbols: [...SYMBOLS] };
  const calls = { restore: 0, history: 0, persist: [] };
  const persistedEvents = new Map();
  const candles = new Map(SYMBOLS.map((symbol) => [symbol, historicalCandles()]));
  let durableCurrent = null;

  const collector = {
    subscribedSymbols: () => [...control.symbols],
    advanceMovementBuckets: (boundary) => state.advanced.push(boundary),
    symbolSourceStatus: () => "LIVE",
    movementLateRejections: () => 0,
    movementSnapshot: (symbol) => snapshotFor(symbol, state.boundary),
  };
  const store = {
    async getMarketStateCurrent() {
      calls.restore += 1;
      if (calls.restore <= control.failRestore) throw new Error("restore read failed");
      return durableCurrent;
    },
    async readMovementCandleHistory() {
      calls.history += 1;
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

test("a required persistence batch is retried until it succeeds without losing its transition", async () => {
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

  // While the batch is pending the engine still retries it before any later
  // boundary, so the transition is persisted exactly once and evaluation resumes.
  harness.state.now = BASE + 10_000;
  harness.state.boundary = BASE + 10_000;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.persist.length, 3, "the pending batch is retried");
  assert.equal(harness.calls.persist[2].events.length, 1);
  assert.equal(
    harness.calls.persist[2].events[0].eventId,
    attempted[0].eventId,
    "the retried batch reuses the deterministic event ID",
  );
  assert.equal(harness.persistedEvents.size, 1, "the transition persists exactly once");
  assert.equal(
    harness.state.advanced.at(-1),
    BASE + 10_000,
    "later boundary processing continues after the retry succeeds",
  );
});

test("a transient lifecycle restore failure defers evaluation and retries with bounded backoff", async () => {
  const harness = createHarness();
  harness.control.failRestore = 1;

  await harness.runtime.runOnce();
  assert.equal(harness.calls.restore, 1);
  assert.equal(harness.state.advanced.length, 0, "no evaluation before restore succeeds");
  assert.equal(harness.calls.history, 0);
  assert.equal(harness.calls.persist.length, 0);

  // Inside the backoff window the read is not retried on every one-second tick.
  harness.state.now = BASE + 1_000;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.restore, 1, "restore is not retried before the backoff elapses");
  assert.equal(harness.state.advanced.length, 0);

  // Once the backoff elapses the read succeeds and evaluation begins.
  harness.state.now = BASE + MOVEMENT_RETRY_BASE_MS + 1_000;
  harness.state.boundary = BASE + 5_000;
  await harness.runtime.runOnce();
  assert.equal(harness.calls.restore, 2);
  assert.ok(harness.state.advanced.length > 0, "evaluation starts only after a successful restore");
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
