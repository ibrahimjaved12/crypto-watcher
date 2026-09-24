import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

const stub = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
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
const { BinanceFuturesCollector, candleIdentity } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`
);

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
    sourceEventTime: openTime + duration - 1,
    receivedAt: BASE,
    transport,
  };
}

function wsKline(openTime, timeframeMinutes = 1, closed = true) {
  const duration = timeframeMinutes * 60_000;
  const interval = { 1: "1m", 15: "15m", 60: "1h", 240: "4h" }[timeframeMinutes];
  return {
    stream: `btcusdt@kline_${interval}`,
    data: {
      e: "kline",
      E: openTime + duration,
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
      async recordCollectorHealth() {},
    },
    loadRest,
    async onCompleted(event) {
      events.push(event);
    },
  });
  return { collector, events, writes };
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
