import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

const source = await readFile(
  new URL("../src/lib/market/movement-buckets.ts", import.meta.url),
  "utf8",
);
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const {
  FuturesMovementBuckets,
  MAX_LAST_TRADE_AGE_MS,
  MOVEMENT_BUCKET_CAPACITY,
  MOVEMENT_BUCKET_MS,
} = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

const BASE = Date.parse("2026-09-25T12:00:00.000Z");

function trade(tradeTime, price = 100, quantity = 1) {
  return {
    symbol: "BTCUSDT",
    price,
    quantity,
    tradeTime,
    eventTime: tradeTime + 1,
  };
}

function movement() {
  const buckets = new FuturesMovementBuckets();
  buckets.reconcile(["BTCUSDT"]);
  return buckets;
}

test("five-second exchange-time buckets aggregate trades and retain the latest endpoint", () => {
  const buckets = movement();
  buckets.accept(trade(BASE + 1_000, 100, 2));
  buckets.accept(trade(BASE + 4_999, 101, 3));
  buckets.accept(trade(BASE + 5_000, 102, 1));
  buckets.advanceTo(BASE + MOVEMENT_BUCKET_MS);

  const bucket = buckets.snapshot("BTCUSDT").buckets[0];
  assert.equal(bucket.boundaryTime, BASE + 5_000);
  assert.equal(bucket.endpointPrice, 102);
  assert.equal(bucket.baseQuantity, 6);
  assert.equal(bucket.quoteVolume, 605);
  assert.equal(bucket.tradeCount, 3);
  assert.equal(bucket.lastRealTradeTime, BASE + 5_000);
  assert.equal(bucket.carriedForward, false);
  assert.equal(bucket.provider, "binance-usdm");
  assert.equal(bucket.instrumentId, "binance-usdm:BTCUSDT");
  assert.equal(bucket.priceType, "trade");
});

test("empty buckets carry a fresh endpoint but expose staleness after fifteen seconds", () => {
  const buckets = movement();
  buckets.accept(trade(BASE, 100, 2));
  buckets.advanceTo(BASE + MAX_LAST_TRADE_AGE_MS);

  let history = buckets.snapshot("BTCUSDT").buckets;
  assert.equal(history.at(-1).boundaryTime, BASE + 15_000);
  assert.equal(history.at(-1).endpointPrice, 100);
  assert.equal(history.at(-1).tradeCount, 0);
  assert.equal(history.at(-1).carriedForward, true);
  assert.equal(history.at(-1).lastRealTradeTime, BASE);

  buckets.advanceTo(BASE + 20_000);
  history = buckets.snapshot("BTCUSDT").buckets;
  assert.equal(history.at(-1).endpointPrice, null);
  assert.equal(history.at(-1).carriedForward, false);
  assert.equal(buckets.snapshot("BTCUSDT").readiness[1].status, "STALE");
});

test("the bounded history supports two adjacent fifteen-minute windows", () => {
  const buckets = movement();
  for (let index = 0; index <= 450; index += 1) {
    buckets.accept(trade(BASE + index * MOVEMENT_BUCKET_MS, 100 + index / 100, 1));
  }
  buckets.advanceTo(BASE + 450 * MOVEMENT_BUCKET_MS);

  const snapshot = buckets.snapshot("BTCUSDT");
  assert.equal(snapshot.buckets.length, MOVEMENT_BUCKET_CAPACITY);
  assert.equal(snapshot.readiness[1].status, "READY");
  assert.equal(snapshot.readiness[5].status, "READY");
  assert.equal(snapshot.readiness[15].status, "READY");
  assert.ok(snapshot.readiness[15].availableHistoryMs >= 30 * 60_000);
});

test("a new instance starts warming without fabricated intraminute history", () => {
  const buckets = movement();
  const snapshot = buckets.snapshot("BTCUSDT");
  assert.deepEqual(snapshot.buckets, []);
  assert.equal(snapshot.latestRealTradeTime, null);
  assert.deepEqual(
    Object.values(snapshot.readiness).map((entry) => entry.status),
    ["WARMING", "WARMING", "WARMING"],
  );
});
