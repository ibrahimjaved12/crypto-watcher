import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

const stub = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const movementUrl = stub("export const MOVEMENT_OBSERVATION_BATCH_MAX=20000;");
const source = await readFile(new URL("../src/lib/market/collector.ts", import.meta.url), "utf8");
let { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
outputText = outputText.replaceAll(
  JSON.stringify("./symbols"),
  JSON.stringify(
    stub(`export const isSupportedSymbol=(value)=>["BTCUSDT","ETHUSDT"].includes(value);`),
  ),
);
outputText = outputText.replaceAll(
  JSON.stringify("./movement-contract"),
  JSON.stringify(movementUrl),
);
const {
  BinanceFuturesCollector,
  candleIdentity,
  normalizeRestCandles,
  parseBinanceMarketMessage,
  COLLECTOR_BOOTSTRAP_LIMIT,
} = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

const BASE = 1_800_000_000_000;
const intervals = [1, 15, 60, 240];

function canonical(symbol, timeframeMinutes, openTime, transport = "rest") {
  const duration = timeframeMinutes * 60_000;
  return {
    instrumentId: `binance-usdm:${symbol}`,
    symbol,
    nativeSymbol: symbol,
    provider: "binance-usdm",
    endpoint: transport === "rest" ? "/fapi/v1/klines" : "wss://fstream.binance.com/market/stream",
    priceType: "trade",
    timeframeMinutes,
    openTime,
    closeTime: openTime + duration - 1,
    open: 100,
    high: 102,
    low: 99,
    close: 101,
    volume: 12,
    // REST has no exchange event time; a WebSocket fixture uses the event time.
    sourceEventTime: transport === "rest" ? null : openTime + duration,
    receivedAt: BASE,
    transport,
  };
}

function wsKline(
  openTime,
  timeframeMinutes = 1,
  closed = true,
  eventTime = openTime + timeframeMinutes * 60_000,
) {
  const duration = timeframeMinutes * 60_000;
  const interval = { 1: "1m", 15: "15m", 60: "1h", 240: "4h" }[timeframeMinutes];
  return {
    stream: `btcusdt@kline_${interval}`,
    data: {
      e: "kline",
      E: eventTime,
      s: "BTCUSDT",
      st: 1,
      k: {
        t: openTime,
        T: openTime + duration - 1,
        s: "BTCUSDT",
        i: interval,
        o: "100",
        h: "102",
        l: "99",
        c: "101",
        v: "12",
        x: closed,
      },
    },
  };
}

function harness(overrides = {}) {
  const persisted = new Set();
  const writes = [];
  const healthWrites = [];
  const events = [];
  const movementCalls = [];
  let clock = BASE;
  const loadRest = async (request) => {
    if (overrides.loadRest) return overrides.loadRest(request);
    const duration = request.timeframeMinutes * 60_000;
    if (request.startTime !== undefined && request.endTime !== undefined) {
      const rows = [];
      for (let open = request.startTime; open <= request.endTime - duration + 1; open += duration) {
        rows.push(canonical(request.symbol, request.timeframeMinutes, open));
      }
      return rows;
    }
    return [canonical(request.symbol, request.timeframeMinutes, BASE - 2 * duration)];
  };
  const collector = new BinanceFuturesCollector({
    now: () => overrides.now?.() ?? clock,
    queueCapacity: overrides.queueCapacity ?? 16,
    tradeWindowMs: overrides.tradeWindowMs ?? 10_000,
    tradeMaxCount: overrides.tradeMaxCount ?? 10,
    store: {
      async recordCollectorCandles(candles) {
        await overrides.recordCollectorCandles?.(candles);
        const inserted = [];
        for (const candle of candles) {
          const id = candleIdentity(candle);
          writes.push([candle.openTime, candle.transport]);
          if (!persisted.has(id)) {
            persisted.add(id);
            inserted.push(id);
          }
        }
        return inserted;
      },
      async recordCollectorHealth(input) {
        healthWrites.push(input);
        await overrides.recordCollectorHealth?.(input);
      },
    },
    loadRest,
    async onCompleted(event) {
      events.push(event);
    },
    onOverload: overrides.onOverload,
    async advanceMovementBoundary(sessionId, boundaryTime, symbols) {
      movementCalls.push({ sessionId, boundaryTime, symbols });
      if (overrides.advanceMovementBoundary) {
        return overrides.advanceMovementBoundary(sessionId, boundaryTime, symbols);
      }
      return { snapshots: [], lateAfterFinalizationCount: 0 };
    },
  });
  return {
    collector,
    events,
    writes,
    healthWrites,
    movementCalls,
    setClock(value) { clock = value; },
  };
}

async function exerciseCollectorRecoveryTenure(oldOutcome) {
  let recoveryMode = false;
  let recoveryCalls = 0;
  let oldRequest;
  let resolveOld;
  let rejectOld;
  let observeOldStart;
  const oldStarted = new Promise((resolve) => { observeOldStart = resolve; });
  const oldLoad = new Promise((resolve, reject) => {
    resolveOld = resolve;
    rejectOld = reject;
  });
  const { collector, healthWrites } = harness({
    loadRest: async (request) => {
      const duration = request.timeframeMinutes * 60_000;
      if (!recoveryMode) {
        return [canonical(request.symbol, request.timeframeMinutes, BASE - 2 * duration)];
      }
      recoveryCalls += 1;
      if (recoveryCalls === 1) {
        oldRequest = request;
        observeOldStart();
        return oldLoad;
      }
      return [canonical(request.symbol, request.timeframeMinutes, request.startTime)];
    },
  });
  await collector.reconcile(["BTCUSDT"]);
  recoveryMode = true;

  const generationA = collector.beginCandleRecoveryTenure();
  const recoveryA = collector.recoverAfterReconnect(generationA);
  await oldStarted;
  collector.invalidateCandleRecoveryTenure(generationA);
  const generationB = collector.beginCandleRecoveryTenure();
  await collector.recoverAfterReconnect(generationB);
  const healthCountAfterB = healthWrites.length;

  if (oldOutcome === "failure") {
    rejectOld(new Error("old recovery failed"));
    await assert.rejects(recoveryA, /old recovery failed/);
  } else {
    resolveOld([
      canonical(oldRequest.symbol, oldRequest.timeframeMinutes, oldRequest.startTime),
    ]);
    await recoveryA;
  }

  assert.equal(recoveryCalls, 5);
  assert.equal(healthWrites.length, healthCountAfterB);
}

test("developing kline stays in memory and never enters completed persistence", async () => {
  const { collector, events, writes } = harness();
  await collector.reconcile(["BTCUSDT"]);
  events.length = 0;
  writes.length = 0;
  const open = BASE - 60_000;
  assert.equal(collector.accept(wsKline(open, 1, false)), true);
  await collector.waitForIdle();
  assert.equal(writes.length, 0);
  assert.equal(events.length, 0);
  assert.equal(collector.developingCandle("BTCUSDT", 1)?.openTime, open);
});

test("duplicate final and REST/WebSocket overlap are processed once", async () => {
  const { collector, events } = harness();
  await collector.reconcile(["BTCUSDT"]);
  events.length = 0;
  const open = BASE - 60_000;
  collector.accept(wsKline(open));
  collector.accept(wsKline(open));
  await collector.waitForIdle();
  assert.deepEqual(
    events.map((event) => [event.candle.openTime, event.origin]),
    [[open, "live"]],
  );
});

test("a skipped interval triggers chronological bounded REST recovery", async () => {
  const minute = 60_000;
  const requested = [];
  const { collector, events, writes } = harness({
    loadRest: async (request) => {
      if (request.startTime === undefined) {
        return [
          canonical(
            request.symbol,
            request.timeframeMinutes,
            BASE - 4 * request.timeframeMinutes * 60_000,
          ),
        ];
      }
      requested.push([request.startTime, request.endTime]);
      const rows = [];
      for (
        let open = request.startTime;
        open <= request.endTime - request.timeframeMinutes * 60_000 + 1;
        open += request.timeframeMinutes * 60_000
      ) {
        rows.push(canonical(request.symbol, request.timeframeMinutes, open));
      }
      return rows;
    },
  });
  await collector.reconcile(["BTCUSDT"]);
  events.length = 0;
  writes.length = 0;
  collector.accept(wsKline(BASE - minute));
  await collector.waitForIdle();
  assert.deepEqual(requested[0], [BASE - 3 * minute, BASE - minute - 1]);
  assert.deepEqual(
    events.map((event) => [event.candle.openTime, event.origin]),
    [
      [BASE - 3 * minute, "recovery"],
      [BASE - 2 * minute, "recovery"],
      [BASE - minute, "live"],
    ],
  );
  assert.deepEqual(
    writes.slice(-3).map(([open]) => open),
    [BASE - 3 * minute, BASE - 2 * minute, BASE - minute],
  );
});

test("reconnect recovery and repeated final do not duplicate completion", async () => {
  const { collector, events } = harness();
  await collector.reconcile(["BTCUSDT"]);
  events.length = 0;
  const open = BASE - 60_000;
  collector.accept(wsKline(open));
  await collector.waitForIdle();
  collector.noteReconnect();
  await collector.recoverAfterReconnect();
  collector.accept(wsKline(open));
  await collector.waitForIdle();
  assert.equal(events.filter((event) => event.origin === "live").length, 1);
});

test("a new candle recovery generation does not reuse an old failing single-flight", async () => {
  await exerciseCollectorRecoveryTenure("failure");
});

test("stale candle recovery success cannot write health into a newer generation", async () => {
  await exerciseCollectorRecoveryTenure("success");
});

test("stale candle persistence cannot commit latest state or completion side effects", async () => {
  let holdOldWrite = false;
  let releaseOldWrite;
  const oldWrite = new Promise((resolve) => { releaseOldWrite = resolve; });
  const t1 = canonical("BTCUSDT", 1, BASE - 60_000);
  const t2 = canonical("BTCUSDT", 1, BASE);
  const { collector, events } = harness({
    recordCollectorCandles: async (candles) => {
      if (holdOldWrite && candles[0]?.openTime === t1.openTime) await oldWrite;
    },
  });
  await collector.reconcile(["BTCUSDT"]);
  events.length = 0;

  const generationA = collector.beginCandleRecoveryTenure();
  holdOldWrite = true;
  const stalePersist = collector.persist([t1], "recovery", generationA);
  collector.invalidateCandleRecoveryTenure(generationA);
  const generationB = collector.beginCandleRecoveryTenure();
  assert.equal(await collector.persist([t2], "recovery", generationB), true);
  assert.equal(collector.latestCompleted.get("BTCUSDT:1").openTime, t2.openTime);
  assert.deepEqual(events.map((event) => event.candle.openTime), [t2.openTime]);

  releaseOldWrite();
  assert.equal(await stalePersist, false);
  assert.equal(collector.latestCompleted.get("BTCUSDT:1").openTime, t2.openTime);
  assert.deepEqual(events.map((event) => event.candle.openTime), [t2.openTime]);
});

test("stale health completion reasserts the newer persistence-visible health", async () => {
  let holdOldWrite = false;
  let releaseOldWrite;
  const oldWrite = new Promise((resolve) => { releaseOldWrite = resolve; });
  const persistedHealth = [];
  const { collector } = harness({
    recordCollectorHealth: async (input) => {
      if (holdOldWrite && input.errorMessage === "generation A") await oldWrite;
      persistedHealth.push(input);
    },
  });
  await collector.reconcile(["BTCUSDT"]);
  persistedHealth.length = 0;
  const t1 = canonical("BTCUSDT", 1, BASE - 60_000);
  const t2 = canonical("BTCUSDT", 1, BASE);

  const generationA = collector.beginCandleRecoveryTenure();
  holdOldWrite = true;
  const staleHealth = collector.setHealth(
    t1,
    "RECOVERING",
    "generation A",
    generationA,
  );
  collector.invalidateCandleRecoveryTenure(generationA);
  const generationB = collector.beginCandleRecoveryTenure();
  const currentHealth = collector.setHealth(t2, "LIVE", "generation B", generationB);

  releaseOldWrite();
  assert.equal(await staleHealth, false);
  assert.equal(await currentHealth, true);
  assert.equal(collector.health.get("BTCUSDT:1").status, "LIVE");
  assert.equal(collector.health.get("BTCUSDT:1").lastCompletedOpenTime, t2.openTime);
  assert.deepEqual(
    persistedHealth.map((input) => input.errorMessage),
    ["generation A", "generation B"],
  );
});

test("stale health persistence failure cannot poison newer authoritative health", async () => {
  let rejectOldWrite;
  const oldWrite = new Promise((resolve, reject) => { rejectOldWrite = reject; });
  const persistedHealth = [];
  const { collector } = harness({
    recordCollectorHealth: async (input) => {
      if (input.errorMessage === "generation A") await oldWrite;
      persistedHealth.push(input);
    },
  });
  await collector.reconcile(["BTCUSDT"]);
  persistedHealth.length = 0;
  const t1 = canonical("BTCUSDT", 1, BASE - 60_000);
  const t2 = canonical("BTCUSDT", 1, BASE);

  const generationA = collector.beginCandleRecoveryTenure();
  const staleHealth = collector.setHealth(
    t1,
    "RECOVERING",
    "generation A",
    generationA,
  );
  collector.invalidateCandleRecoveryTenure(generationA);
  const generationB = collector.beginCandleRecoveryTenure();
  const currentHealth = collector.setHealth(t2, "LIVE", "generation B", generationB);

  rejectOldWrite(new Error("obsolete health write failed"));
  assert.equal(await staleHealth, false);
  assert.equal(await currentHealth, true);
  assert.equal(collector.health.get("BTCUSDT:1").status, "LIVE");
  assert.equal(collector.health.get("BTCUSDT:1").lastCompletedOpenTime, t2.openTime);
  assert.deepEqual(
    persistedHealth.map((input) => input.errorMessage),
    ["generation B"],
  );
});

test("replacement recovery tenure does not invalidate slow disconnect health", async () => {
  let holdDisconnect = false;
  let releaseDisconnect;
  const disconnectWrite = new Promise((resolve) => { releaseDisconnect = resolve; });
  const persistedHealth = new Map();
  const { collector } = harness({
    recordCollectorHealth: async (input) => {
      if (
        holdDisconnect &&
        input.timeframeMinutes === 1 &&
        input.errorMessage === "fixture disconnect"
      ) {
        await disconnectWrite;
      }
      persistedHealth.set(`${input.symbol}:${input.timeframeMinutes}`, input);
    },
  });
  await collector.reconcile(["BTCUSDT"]);
  await collector.markConnectionStatus("LIVE", null);

  holdDisconnect = true;
  const recovering = collector.markConnectionStatus("RECOVERING", "fixture disconnect");
  assert.equal(collector.health.get("BTCUSDT:1").status, "RECOVERING");
  collector.beginCandleRecoveryTenure();
  assert.equal(collector.health.get("BTCUSDT:1").status, "RECOVERING");

  releaseDisconnect();
  await recovering;
  assert.equal(collector.health.get("BTCUSDT:1").status, "RECOVERING");
  assert.equal(persistedHealth.get("BTCUSDT:1").status, "RECOVERING");
});

test("new event provenance remains monotonic during slow health persistence", async () => {
  let holdHealth = false;
  let releaseHealth;
  const deferredHealth = new Promise((resolve) => { releaseHealth = resolve; });
  const persistedHealth = [];
  const { collector } = harness({
    recordCollectorHealth: async (input) => {
      if (holdHealth && input.errorMessage === "slow health") {
        holdHealth = false;
        await deferredHealth;
      }
      persistedHealth.push(input);
    },
  });
  await collector.reconcile(["BTCUSDT"]);
  persistedHealth.length = 0;
  const open = BASE - 60_000;
  const t1 = BASE - 1_000;
  const t2 = BASE - 500;
  assert.equal(collector.accept(wsKline(open, 1, false, t1)), true);

  holdHealth = true;
  const writing = collector.setHealth(
    canonical("BTCUSDT", 1, open),
    "LIVE",
    "slow health",
  );
  assert.equal(collector.accept(wsKline(open, 1, false, t2)), true);
  assert.equal(collector.health.get("BTCUSDT:1").lastEventAt, t2);

  releaseHealth();
  assert.equal(await writing, true);
  assert.equal(collector.health.get("BTCUSDT:1").lastEventAt, t2);
  assert.deepEqual(
    persistedHealth.map((input) => input.lastEventAt),
    [t1, t2],
  );
});

test("aggregate-trade buffers obey both time and hard-count bounds", async () => {
  const { collector } = harness({ tradeWindowMs: 1_000, tradeMaxCount: 3 });
  await collector.reconcile(["BTCUSDT"]);
  const trade = (id, time) => ({
    stream: "btcusdt@aggTrade",
    data: {
      e: "aggTrade",
      E: time,
      s: "BTCUSDT",
      st: 1,
      a: id,
      p: "101",
      q: "2",
      T: time,
    },
  });
  for (const [id, time] of [
    [1, 1_000],
    [2, 1_500],
    [3, 1_600],
    [4, 1_700],
  ]) {
    collector.accept(trade(id, time), time);
  }
  assert.deepEqual(
    collector.latestTrades("BTCUSDT").map((item) => item.aggregateId),
    [2, 3, 4],
  );
  collector.accept(trade(5, 2_701), 2_701);
  assert.deepEqual(
    collector.latestTrades("BTCUSDT").map((item) => item.aggregateId),
    [5],
  );
});

test("accepted aggregate trades preserve exact decimals for Python movement calculation", async () => {
  const pythonSnapshot = {
    symbol: "BTCUSDT",
    provider: "binance-usdm",
    instrumentId: "binance-usdm:BTCUSDT",
    priceType: "trade",
    bucketMs: 5_000,
    maxLastTradeAgeMs: 15_000,
    buckets: [],
    latestRealTradeTime: BASE,
    latestRealReceivedAt: BASE,
    readiness: {
      1: { windowMinutes: 1, status: "WARMING", state: "warming", reason: "insufficient_exact_live_history" },
      5: { windowMinutes: 5, status: "WARMING", state: "warming", reason: "insufficient_exact_live_history" },
      15: { windowMinutes: 15, status: "WARMING", state: "warming", reason: "insufficient_exact_live_history" },
    },
  };
  const { collector, writes, healthWrites, movementCalls } = harness({
    advanceMovementBoundary: async () => ({
      snapshots: [pythonSnapshot],
      lateAfterFinalizationCount: 0,
    }),
  });
  await collector.reconcile(["BTCUSDT"]);
  writes.length = 0;
  healthWrites.length = 0;
  const accepted = collector.accept(
    {
      stream: "btcusdt@aggTrade",
      data: {
        e: "aggTrade",
        E: BASE,
        s: "BTCUSDT",
        st: 1,
        a: 1,
        p: "101.000000000000000001",
        q: "2.000000000000000009",
        T: BASE,
      },
    },
    BASE,
  );
  await collector.advanceMovementBuckets(BASE);

  assert.equal(accepted, true);
  assert.equal(writes.length, 0);
  assert.equal(healthWrites.length, 0);
  assert.equal(movementCalls.length, 1);
  assert.equal(movementCalls[0].boundaryTime, BASE);
  assert.deepEqual(movementCalls[0].symbols[0].observations, [
    {
      symbol: "BTCUSDT",
      aggregateId: 1,
      price: "101.000000000000000001",
      quantity: "2.000000000000000009",
      eventTime: BASE,
      tradeTime: BASE,
      receivedAt: BASE,
    },
  ]);
  assert.equal(collector.movementSnapshot("BTCUSDT"), pythonSnapshot);
});

test("movement reset fences late replies from the prior collector tenure", async () => {
  let finishOldRequest;
  const oldReply = new Promise((resolve) => { finishOldRequest = resolve; });
  let call = 0;
  const freshSnapshot = { symbol: "BTCUSDT", marker: "fresh-tenure" };
  const { collector, movementCalls } = harness({
    advanceMovementBoundary: async () => {
      call += 1;
      return call === 1
        ? oldReply
        : { snapshots: [freshSnapshot], lateAfterFinalizationCount: 0 };
    },
  });
  await collector.reconcile(["BTCUSDT"]);

  const priorAdvance = collector.advanceMovementBuckets(BASE);
  const priorSession = movementCalls[0].sessionId;
  collector.resetMovementTransportState();
  await collector.advanceMovementBuckets(BASE);
  assert.notEqual(movementCalls[1].sessionId, priorSession);

  finishOldRequest({
    snapshots: [{ symbol: "BTCUSDT", marker: "stale-tenure" }],
    lateAfterFinalizationCount: 0,
  });
  await priorAdvance;
  assert.equal(collector.movementSnapshot("BTCUSDT"), freshSnapshot);
});

test("connection movement state changes synchronously before deferred health persistence", async () => {
  let deferHealth = false;
  let releaseHealth;
  const deferredHealth = new Promise((resolve) => { releaseHealth = resolve; });
  const { collector } = harness({
    recordCollectorHealth: async () => {
      if (!deferHealth) return;
      deferHealth = false;
      await deferredHealth;
    },
  });
  await collector.reconcile(["BTCUSDT", "ETHUSDT"]);
  await collector.markConnectionStatus("LIVE", null);
  assert.equal(collector.movementSourceStatus("BTCUSDT"), "LIVE");
  assert.equal(collector.movementSourceStatus("ETHUSDT"), "LIVE");

  deferHealth = true;
  const recovering = collector.markConnectionStatus("RECOVERING", "fixture reconnect");
  assert.equal(collector.movementSourceStatus("BTCUSDT"), "RECOVERING");
  assert.equal(collector.movementSourceStatus("ETHUSDT"), "RECOVERING");

  releaseHealth();
  await recovering;
});

test("an in-flight boundary response cannot restore a removed movement membership", async () => {
  let finishBoundary;
  const deferredBoundary = new Promise((resolve) => { finishBoundary = resolve; });
  const { collector, movementCalls } = harness({
    advanceMovementBoundary: async () => deferredBoundary,
  });
  await collector.reconcile(["BTCUSDT", "ETHUSDT"]);
  await collector.markConnectionStatus("LIVE", null);
  assert.equal(collector.accept({
    stream: "ethusdt@aggTrade",
    data: {
      e: "aggTrade",
      E: BASE,
      s: "ETHUSDT",
      st: 1,
      a: 7,
      p: "2000.00000001",
      q: "0.25",
      T: BASE,
    },
  }, BASE), true);

  const advancing = collector.advanceMovementBuckets(BASE);
  assert.deepEqual(movementCalls[0].symbols.map((item) => item.symbol), ["BTCUSDT", "ETHUSDT"]);
  await collector.reconcile(["BTCUSDT"]);
  finishBoundary({
    snapshots: [
      { symbol: "BTCUSDT", marker: "current" },
      { symbol: "ETHUSDT", marker: "removed" },
    ],
    lateAfterFinalizationCount: 0,
  });
  await advancing;

  assert.deepEqual(collector.subscribedSymbols(), ["BTCUSDT"]);
  assert.equal(collector.movementSnapshot("BTCUSDT").marker, "current");
  assert.equal(collector.movementSnapshot("ETHUSDT"), null);
  assert.equal(collector.movementResults.has("ETHUSDT"), false);
  assert.equal(collector.movementObservations.has("ETHUSDT"), false);
  assert.equal(collector.movementSeenTrades.has("ETHUSDT"), false);
  assert.equal(collector.movementLastAcceptedTrade.has("ETHUSDT"), false);
  assert.equal(collector.movementSourceTransitions.has("ETHUSDT"), false);
  assert.equal(collector.movementMembershipEpochs.has("ETHUSDT"), false);
  assert.equal(collector.latestPrice("ETHUSDT"), null);
  assert.deepEqual(collector.latestTrades("ETHUSDT"), []);
  assert.equal(collector.movementRetry, null);
});

test("an exact retry preserves the original membership epoch across remove and re-add", async () => {
  let requestCount = 0;
  const oldSnapshot = { symbol: "ETHUSDT", marker: "epoch-1" };
  const { collector, movementCalls } = harness({
    advanceMovementBoundary: async () => {
      requestCount += 1;
      if (requestCount === 1) throw new Error("transient Python failure");
      return { snapshots: [oldSnapshot], lateAfterFinalizationCount: 0 };
    },
  });
  await collector.reconcile(["ETHUSDT"]);
  assert.equal(collector.accept({
    stream: "ethusdt@aggTrade",
    data: {
      e: "aggTrade", E: BASE, s: "ETHUSDT", st: 1,
      a: 1, p: "2000.1", q: "0.1", T: BASE,
    },
  }, BASE), true);

  await assert.rejects(collector.advanceMovementBuckets(BASE), /transient Python failure/);
  const original = movementCalls[0];
  assert.equal(original.symbols[0].membershipEpoch, 1);
  await collector.reconcile([]);
  await collector.reconcile(["ETHUSDT"]);
  assert.equal(collector.movementMembershipEpochs.get("ETHUSDT"), 2);
  assert.equal(collector.accept({
    stream: "ethusdt@aggTrade",
    data: {
      e: "aggTrade", E: BASE + 1, s: "ETHUSDT", st: 1,
      a: 2, p: "2000.2", q: "0.2", T: BASE + 1,
    },
  }, BASE + 1), true);

  await collector.advanceMovementBuckets(BASE);
  const retried = movementCalls[1];
  assert.equal(retried.sessionId, original.sessionId);
  assert.equal(retried.boundaryTime, original.boundaryTime);
  assert.equal(retried.symbols, original.symbols);
  assert.equal(retried.symbols[0].membershipEpoch, 1);
  assert.equal(collector.movementSnapshot("ETHUSDT"), null);
  assert.deepEqual(
    collector.movementObservations.get("ETHUSDT").map((trade) => trade.aggregateId),
    [2],
  );
});

test("source outage within a bucket remains attached after source returns LIVE", async () => {
  const { collector, movementCalls, setClock } = harness();
  await collector.reconcile(["BTCUSDT"]);
  setClock(BASE);
  await collector.markConnectionStatus("LIVE", null);
  setClock(BASE + 1_000);
  await collector.markConnectionStatus("STALE", "short fixture outage");
  setClock(BASE + 2_000);
  await collector.markConnectionStatus("LIVE", null);

  await collector.advanceMovementBuckets(BASE + 5_000);
  assert.equal(movementCalls.at(-1).symbols[0].sourceState, "STALE");
  await collector.advanceMovementBuckets(BASE + 10_000);
  assert.equal(movementCalls.at(-1).symbols[0].sourceState, "LIVE");
});

test("movement reset fences old in-flight replies and starts a new Python session", async () => {
  let completeOld;
  const oldReply = new Promise((resolve) => { completeOld = resolve; });
  let callCount = 0;
  const freshSnapshot = { symbol: "BTCUSDT", marker: "new-tenure" };
  const { collector, movementCalls } = harness({
    advanceMovementBoundary: async () => {
      callCount += 1;
      return callCount === 1
        ? oldReply
        : { snapshots: [freshSnapshot], lateAfterFinalizationCount: 0 };
    },
  });
  await collector.reconcile(["BTCUSDT"]);
  const oldAdvance = collector.advanceMovementBuckets(BASE);
  const oldSession = movementCalls[0].sessionId;

  collector.resetMovementTransportState();
  await collector.advanceMovementBuckets(BASE);
  const newSession = movementCalls[1].sessionId;
  assert.notEqual(newSession, oldSession);
  completeOld({ snapshots: [{ symbol: "BTCUSDT", marker: "old-tenure" }], lateAfterFinalizationCount: 0 });
  await oldAdvance;
  assert.equal(collector.movementSnapshot("BTCUSDT"), freshSnapshot);
});

test("per-symbol candle health remains a separate collector diagnostic", async () => {
  const { collector } = harness();
  await collector.reconcile(["BTCUSDT"]);
  assert.equal(collector.symbolSourceStatus("BTCUSDT"), "RECOVERING");
  assert.equal(collector.symbolSourceStatus("ETHUSDT"), "UNAVAILABLE");
  assert.equal(collector.movementSourceStatus("BTCUSDT"), "RECOVERING");

  collector.accept(wsKline(BASE - 60_000));
  await collector.waitForIdle();
  assert.equal(collector.symbolSourceStatus("BTCUSDT"), "LIVE");
  assert.equal(collector.movementSourceStatus("BTCUSDT"), "RECOVERING");
  await collector.markConnectionStatus("LIVE", null);
  assert.equal(collector.movementSourceStatus("BTCUSDT"), "LIVE");
});

test("unrelated 4h candle recovery does not poison movement source state", async () => {
  const { collector, movementCalls } = harness();
  await collector.reconcile(["BTCUSDT"]);
  await collector.markConnectionStatus("LIVE", null);

  await collector.setHealth(canonical("BTCUSDT", 240, BASE), "RECOVERING", "4h gap repair");
  assert.equal(collector.movementSourceStatus("BTCUSDT"), "LIVE");

  await collector.advanceMovementBuckets(BASE + 5_000);
  assert.equal(movementCalls.at(-1).symbols[0].sourceState, "LIVE");
});

test("movement observation backpressure fences source before the lost trade is dropped", async () => {
  let overloads = 0;
  const { collector, movementCalls } = harness({
    onOverload: () => { overloads += 1; },
  });
  collector.movementObservationMaxCount = 1;
  await collector.reconcile(["BTCUSDT"]);
  await collector.markConnectionStatus("LIVE", null);
  const trade = (aggregateId, tradeTime) => ({
    stream: "btcusdt@aggTrade",
    data: {
      e: "aggTrade", E: tradeTime + 1, s: "BTCUSDT", st: 1,
      a: aggregateId, p: "101", q: "2", T: tradeTime,
    },
  });

  assert.equal(collector.accept(trade(1, BASE + 100), BASE + 100), true);
  assert.equal(collector.accept(trade(2, BASE + 200), BASE + 200), false);
  assert.equal(collector.movementSourceStatus("BTCUSDT"), "UNAVAILABLE");
  assert.equal(overloads, 1);

  await collector.advanceMovementBuckets(BASE + 5_000);
  assert.equal(movementCalls.at(-1).symbols[0].sourceState, "UNAVAILABLE");
  assert.equal(movementCalls.at(-1).symbols[0].observations.length, 1);
});

test("known duplicate aggTrade is ignored but a unique ordering regression fences continuity", async () => {
  const { collector, movementCalls } = harness();
  await collector.reconcile(["BTCUSDT"]);
  await collector.markConnectionStatus("LIVE", null);
  const trade = (aggregateId, tradeTime, price = "101") => ({
    stream: "btcusdt@aggTrade",
    data: {
      e: "aggTrade", E: tradeTime + 10, s: "BTCUSDT", st: 1,
      a: aggregateId, p: price, q: "2", T: tradeTime,
    },
  });

  assert.equal(collector.accept(trade(10, BASE + 200), BASE + 220), true);
  assert.equal(collector.accept(trade(10, BASE + 200), BASE + 230), false);
  assert.equal(collector.movementSourceStatus("BTCUSDT"), "LIVE");
  assert.equal(collector.accept(trade(11, BASE + 100), BASE + 240), false);
  assert.equal(collector.movementSourceStatus("BTCUSDT"), "UNAVAILABLE");

  await collector.advanceMovementBuckets(BASE + 5_000);
  assert.equal(movementCalls.at(-1).symbols[0].sourceState, "UNAVAILABLE");
  assert.equal(movementCalls.at(-1).symbols[0].observations.length, 1);
});

test("malformed attributable aggTrade combined frame fences movement continuity", async () => {
  let overloads = 0;
  const { collector } = harness({ onOverload: () => { overloads += 1; } });
  await collector.reconcile(["BTCUSDT"]);
  await collector.markConnectionStatus("LIVE", null);

  assert.equal(collector.accept({
    stream: "btcusdt@aggTrade",
    data: { e: "wrong-event", s: "", p: "NaN", q: null, a: -1, E: "bad", T: "bad" },
  }), false);
  assert.equal(collector.movementSourceStatus("BTCUSDT"), "UNAVAILABLE");
  assert.equal(overloads, 1);
});

test("unparseable JSON frame fails closed for all subscribed movement sources", async () => {
  const { collector } = harness();
  await collector.reconcile(["BTCUSDT", "ETHUSDT"]);
  await collector.markConnectionStatus("LIVE", null);

  collector.markAllMovementUnavailable();
  assert.equal(collector.movementSourceStatus("BTCUSDT"), "UNAVAILABLE");
  assert.equal(collector.movementSourceStatus("ETHUSDT"), "UNAVAILABLE");
});

test("unseen late trade increments diagnostics without poisoning live source or history", async () => {
  const baseSnapshot = {
    symbol: "BTCUSDT",
    provider: "binance-usdm",
    instrumentId: "binance-usdm:BTCUSDT",
    priceType: "trade",
    bucketMs: 5_000,
    maxLastTradeAgeMs: 15_000,
    buckets: [{ boundaryTime: BASE, endpointPrice: 101 }],
    latestRealTradeTime: BASE,
    latestRealReceivedAt: BASE,
    readiness: {
      1: { windowMinutes: 1, status: "WARMING", state: "warming", reason: "insufficient_exact_live_history" },
      5: { windowMinutes: 5, status: "WARMING", state: "warming", reason: "insufficient_exact_live_history" },
      15: { windowMinutes: 15, status: "WARMING", state: "warming", reason: "insufficient_exact_live_history" },
    },
  };
  const { collector, movementCalls } = harness({
    advanceMovementBoundary: async () => ({
      snapshots: [baseSnapshot],
      lateAfterFinalizationCount: 0,
    }),
  });
  await collector.reconcile(["BTCUSDT"]);
  await collector.markConnectionStatus("LIVE", null);
  await collector.advanceMovementBuckets(BASE);
  const trade = (id, time, eventTime = time + 10) => ({
    stream: "btcusdt@aggTrade",
    data: { e: "aggTrade", E: eventTime, s: "BTCUSDT", st: 1, a: id, p: "999", q: "9", T: time },
  });

  assert.equal(collector.accept(trade(50, BASE - 1, BASE + 8_000), BASE + 9_000), false);
  assert.equal(collector.movementLateRejections(), 1);
  assert.equal(collector.movementSourceStatus("BTCUSDT"), "LIVE");
  assert.equal(collector.movementSnapshot("BTCUSDT").buckets[0].endpointPrice, 101);
  assert.equal(collector.accept(trade(50, BASE - 1, BASE + 8_000), BASE + 10_000), false);
  assert.equal(collector.movementLateRejections(), 1);

  await collector.advanceMovementBuckets(BASE + 5_000);
  assert.equal(collector.movementLateRejections(), 1);
  assert.equal(movementCalls.at(-1).symbols[0].sourceState, "LIVE");
});

test("trade arriving during boundary request is dropped locally before the next Python request", async () => {
  let finishBoundary;
  const deferred = new Promise((resolve) => { finishBoundary = resolve; });
  let requestIndex = 0;
  const { collector, movementCalls } = harness({
    advanceMovementBoundary: async (sessionId, boundaryTime, symbols) => {
      requestIndex += 1;
      if (requestIndex === 1) return deferred;
      // Simulate an evicted/restarted Python movement session for the next request.
      return { snapshots: [], lateAfterFinalizationCount: 0 };
    },
  });
  await collector.reconcile(["BTCUSDT"]);
  await collector.markConnectionStatus("LIVE", null);

  const finalizing = collector.advanceMovementBuckets(BASE);
  const lateTrade = {
    stream: "btcusdt@aggTrade",
    data: { e: "aggTrade", E: BASE + 100, s: "BTCUSDT", st: 1,
      a: 200, p: "102", q: "3", T: BASE - 100 },
  };
  assert.equal(collector.accept(lateTrade, BASE + 120), true);
  finishBoundary({ snapshots: [], lateAfterFinalizationCount: 5 });
  await finalizing;
  assert.equal(collector.movementBoundary, BASE);

  await collector.advanceMovementBuckets(BASE + 5_000);
  assert.equal(collector.movementLateRejections(), 6);
  assert.equal(movementCalls[1].symbols[0].observations.length, 0);
});

test("Python cumulative late diagnostics cannot decrease transport late counts", async () => {
  let responseCount = 0;
  const { collector } = harness({
    advanceMovementBoundary: async () => {
      responseCount += 1;
      return {
        snapshots: [],
        lateAfterFinalizationCount: [0, 2, 0, 1][responseCount - 1] ?? 1,
      };
    },
  });
  await collector.reconcile(["BTCUSDT"]);
  await collector.markConnectionStatus("LIVE", null);
  const late = {
    stream: "btcusdt@aggTrade",
    data: {
      e: "aggTrade", E: BASE - 1, s: "BTCUSDT", st: 1,
      a: 99, p: "100", q: "1", T: BASE - 1,
    },
  };
  await collector.advanceMovementBuckets(BASE);
  assert.equal(collector.accept(late, BASE + 1), false);
  assert.equal(collector.movementLateRejections(), 1);

  await collector.advanceMovementBuckets(BASE + 5_000);
  assert.equal(collector.movementLateRejections(), 3);

  await collector.advanceMovementBuckets(BASE + 10_000);
  assert.equal(collector.movementLateRejections(), 3);

  await collector.advanceMovementBuckets(BASE + 15_000);
  assert.equal(collector.movementLateRejections(), 4);
});

test("WebSocket event and receive times are preserved exactly; REST invents no event", () => {
  const duration = 60_000;
  const openTime = BASE - duration;

  // REST bootstrap/recovery has no exchange event time, so the absence is recorded
  // honestly as null. The completion boundary stays open + timeframe.
  const rest = normalizeRestCandles({
    symbol: "BTCUSDT",
    timeframeMinutes: 1,
    candles: [
      {
        time: openTime,
        open: 100,
        high: 102,
        low: 99,
        close: 101,
        volume: 12,
        complete: true,
      },
    ],
    retrievedAt: BASE,
  })[0];
  assert.equal(rest.transport, "rest");
  assert.equal(rest.endpoint, "/fapi/v1/klines");
  assert.equal(rest.openTime, openTime);
  assert.equal(rest.closeTime, openTime + duration - 1);
  assert.equal(rest.sourceEventTime, null);
  assert.equal(rest.receivedAt, BASE);

  // A completed WebSocket candle keeps the exchange event time verbatim, even when
  // it differs from the deterministic completion boundary, and the raw receive time.
  const eventTime = openTime + duration + 7;
  const receivedAt = BASE + 3;
  const completed = parseBinanceMarketMessage(wsKline(openTime, 1, true, eventTime), receivedAt);
  assert.equal(completed.kind, "completed");
  assert.equal(completed.candle.transport, "websocket");
  assert.equal(completed.candle.endpoint, "wss://fstream.binance.com/market/stream");
  assert.equal(completed.candle.closeTime, openTime + duration - 1);
  assert.equal(completed.candle.sourceEventTime, eventTime);
  assert.notEqual(completed.candle.sourceEventTime, openTime + duration);
  assert.equal(completed.candle.receivedAt, receivedAt);

  // A developing candle preserves the live event and receive times unchanged.
  const developingEvent = BASE - 5;
  const developing = parseBinanceMarketMessage(wsKline(openTime, 1, false, developingEvent), BASE);
  assert.equal(developing.kind, "developing");
  assert.equal(developing.candle.sourceEventTime, developingEvent);
  assert.equal(developing.candle.receivedAt, BASE);
});

test("REST bootstrap rebuilds enough completed history for every TA frame after a reset", async () => {
  const requested = [];
  const { collector, writes } = harness({
    loadRest: async (request) => {
      requested.push([request.timeframeMinutes, request.limit]);
      const duration = request.timeframeMinutes * 60_000;
      const latestClosed = Math.floor((BASE - duration) / duration) * duration;
      const rows = [];
      for (let index = 0; index < request.limit - 1; index++) {
        rows.push(
          canonical(
            request.symbol,
            request.timeframeMinutes,
            latestClosed - (request.limit - 2 - index) * duration,
          ),
        );
      }
      // Binance returns the most recent `limit` klines including the still-developing one,
      // which the bootstrap must exclude.
      rows.push(canonical(request.symbol, request.timeframeMinutes, latestClosed + duration));
      return rows;
    },
  });
  await collector.reconcile(["BTCUSDT"]);
  assert.deepEqual(
    requested.map(([timeframe]) => timeframe),
    intervals,
  );
  // The bootstrap must request enough completed candles for the TA minimum history (200)
  // plus catch-up and the operational retention target (260) once the developing candle is
  // excluded.
  for (const [timeframe, limit] of requested) {
    assert.equal(limit, COLLECTOR_BOOTSTRAP_LIMIT);
    assert.ok(
      limit - 1 >= 260,
      `frame ${timeframe} bootstrap limit ${limit} cannot reach the 260-candle retention target`,
    );
  }
  // Every completed candle returned by bootstrap is persisted; one developing candle per frame
  // is excluded.
  const expectedWrites = requested.reduce((total, [, limit]) => total + limit - 1, 0);
  assert.equal(writes.length, expectedWrites);
  assert.ok(writes.length >= intervals.length * 200);
});
