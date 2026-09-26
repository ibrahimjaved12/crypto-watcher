import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

const stub = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const movementSource = await readFile(
  new URL("../src/lib/market/movement-buckets.ts", import.meta.url),
  "utf8",
);
const movementOutput = ts.transpileModule(movementSource, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText;
const movementUrl = `data:text/javascript;base64,${Buffer.from(movementOutput).toString("base64")}`;
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
  JSON.stringify("./movement-buckets"),
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
    now: () => BASE,
    queueCapacity: overrides.queueCapacity ?? 16,
    tradeWindowMs: overrides.tradeWindowMs ?? 10_000,
    tradeMaxCount: overrides.tradeMaxCount ?? 10,
    store: {
      async recordCollectorCandles(candles) {
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
      },
    },
    loadRest,
    async onCompleted(event) {
      events.push(event);
    },
  });
  return { collector, events, writes, healthWrites };
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

test("accepted aggregate trades feed movement buckets without persistence writes", async () => {
  const { collector, writes, healthWrites } = harness();
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
        p: "101",
        q: "2",
        T: BASE,
      },
    },
    BASE,
  );
  collector.advanceMovementBuckets(BASE);

  assert.equal(accepted, true);
  assert.equal(writes.length, 0);
  assert.equal(healthWrites.length, 0);
  assert.equal(collector.movementSnapshot("BTCUSDT").buckets[0].endpointPrice, 101);
});

test("late-after-finalization trades are rejected and counted without rewriting history", async () => {
  const { collector } = harness();
  await collector.reconcile(["BTCUSDT"]);
  const trade = (id, time) => ({
    stream: "btcusdt@aggTrade",
    data: { e: "aggTrade", E: time, s: "BTCUSDT", st: 1, a: id, p: "101", q: "2", T: time },
  });

  assert.equal(collector.accept(trade(1, BASE), BASE), true);
  collector.advanceMovementBuckets(BASE);
  assert.equal(collector.movementSnapshot("BTCUSDT").buckets.at(-1).boundaryTime, BASE);
  assert.equal(collector.movementLateRejections(), 0);

  // A later trade whose exchange time belongs to the finalized bucket is rejected.
  assert.equal(collector.accept(trade(2, BASE), BASE), false);
  assert.equal(collector.movementLateRejections(), 1);
  assert.equal(collector.movementSnapshot("BTCUSDT").buckets.at(-1).endpointPrice, 101);
});

test("per-symbol source status reflects collector health for the movement gate", async () => {
  const { collector } = harness();
  await collector.reconcile(["BTCUSDT"]);
  assert.equal(collector.symbolSourceStatus("BTCUSDT"), "RECOVERING");
  assert.equal(collector.symbolSourceStatus("ETHUSDT"), "UNAVAILABLE");

  collector.accept(wsKline(BASE - 60_000));
  await collector.waitForIdle();
  assert.equal(collector.symbolSourceStatus("BTCUSDT"), "LIVE");
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
