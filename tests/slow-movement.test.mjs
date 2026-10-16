// "Longer moves" (slow-movement-v1, #30 / #373): grid building, orchestration, the hook op, the publisher cadence
// and the read view. Every number here is SYNTHETIC. The math itself is tested in python/tests/test_slow_movement.py.
import assert from "node:assert/strict";
import { test } from "node:test";

import { createModuleLoader } from "./module-loader.mjs";

const load = createModuleLoader(import.meta.url);
const contract = await import(load("../src/lib/market/slow-movement-contract.ts"));
const slow = await import(load("../src/lib/market/slow-movement.server.ts"));
const published = await import(load("../src/lib/market/movement-published.ts"));
const publisher = await import(load("../src/lib/market/movement-publisher.server.ts"));
const view = await import(load("../src/lib/market/slow-movement-view.ts"));

const STEP = 15 * 60_000;
const B = Date.parse("2026-10-16T12:00:00Z");
const candle = (openTime, close) => ({ openTime, close, volume: 1, quoteVolume: 1 });

test("grid: g_k is the close of the 1m candle that closed at B - 15 min x k; later candles and gaps stay null", () => {
  const candles = [candle(B - 60_000, 101), candle(B, 999), candle(B - STEP - 60_000, 100), candle(B - STEP, 555)];
  const grid = slow.buildSlowGrid(candles, B);
  assert.equal(grid.length, 672);
  assert.equal(grid[0], 101); // closed exactly at B
  assert.equal(grid[1], 100);
  assert.equal(grid[2], null); // missing, not filled
  assert.ok(!grid.includes(999)); // closes after B: never used
});

function result(symbols, boundaryMs = B) {
  const row = (symbol) => ({ symbol, returnPct: -1, z: -2.5, status: "ok", referenceSamples: 576 });
  return {
    algorithmVersion: "slow-movement-v1", configVersion: "slow-config-v1", configHash: "0123456789abcdef", boundaryMs,
    config: { gridPoints: 672, referenceSamples: 576, minReferenceSamples: 288, zThreshold: 2, coordinatedShare: 0.7, minIncluded: 5 },
    horizons: [60, 240, 720].map((horizonMin) => ({ horizonMin, included: symbols.length, medianReturnPct: -1, medianZ: -2.5,
      up: 0, down: symbols.length, neutral: 0, upShare: 0, downShare: 1, coordinated: symbols.length >= 5 ? "fall" : null,
      symbols: symbols.map(row) })),
  };
}

test("orchestration: universe symbols, read window, one request per boundary, validated answer", async () => {
  const reads = [];
  let request;
  const payload = await slow.computeSlowMovement({
    readUniverse: async () => ["ETHUSDT", "BTCUSDT"],
    readCandles: async (symbol, since, boundary) => { reads.push([symbol, since, boundary]); return [candle(B - 60_000, 10)]; },
    evaluate: async (req) => { request = req; return result(["BTCUSDT", "ETHUSDT"]); },
    now: () => B + 20_000,
  }, B);
  assert.deepEqual(reads.map((r) => r[0]), ["BTCUSDT", "ETHUSDT"]);
  assert.equal(reads[0][1], B - 7 * 86_400_000 - 120_000);
  assert.equal(reads[0][2], B);
  assert.deepEqual(request.horizonsMin, [60, 240, 720]);
  assert.equal(request.symbols[0].grid.length, 672);
  assert.equal(payload.version, "slow-movement-published-v1");
  assert.equal(payload.computedAt, B + 20_000);
  await assert.rejects(slow.computeSlowMovement({ readUniverse: async () => ["BTCUSDT"], readCandles: async () => [],
    evaluate: async () => result(["ETHUSDT"]), now: () => B }, B), /mismatched symbols/);
  assert.equal(await slow.computeSlowMovement({ readUniverse: async () => [], readCandles: async () => [],
    evaluate: async () => { throw new Error("never"); }, now: () => B }, B), null);
});

test("hook: publish_slow writes its own row through publish_market_state; bad payloads are refused", async () => {
  const payload = { version: "slow-movement-published-v1", computedAt: B + 20_000, result: result(["BTCUSDT"]) };
  const input = published.parseMovementHookInput({ op: "publish_slow", universeId: contract.SLOW_UNIVERSE_ID, evaluationBoundaryMs: B, payload });
  assert.equal(input.op, "publish_slow");
  assert.equal(contract.SLOW_UNIVERSE_ID, "binance-usdm-public-market:slow-v1");
  const calls = [];
  const reply = await published.handleMovementHook({ rpc: async (name, args) => { calls.push([name, args]); return { data: true, error: null }; } }, input);
  assert.equal(reply.status, 200);
  assert.deepEqual([calls[0][0], calls[0][1].p_universe_id, calls[0][1].p_boundary_ms], ["publish_market_state", contract.SLOW_UNIVERSE_ID, B]);
  const bad = (over) => published.parseMovementHookInput({ op: "publish_slow", universeId: contract.SLOW_UNIVERSE_ID, evaluationBoundaryMs: B, payload, ...over });
  assert.equal(bad({ universeId: "binance-usdm-public-market" }), null);
  assert.equal(bad({ evaluationBoundaryMs: B + 60_000 }), null);
  assert.equal(bad({ evaluationBoundaryMs: B + STEP }), null); // payload boundary must match
  assert.equal(bad({ payload: { ...payload, version: "x" } }), null);
  assert.equal(published.parseMovementHookInput({ op: "heartbeat", universeId: contract.SLOW_UNIVERSE_ID }), null); // no slow heartbeat
});

test("publisher: once per boundary, 20 s after it, 3 attempts 30 s apart, failure reported once", async () => {
  let now = B + 10_000;
  let fail = true;
  let computes = 0;
  const deps = {
    now: () => now,
    compute: async () => { computes += 1; if (fail) throw new Error("python down"); return { version: "slow-movement-published-v1", computedAt: now, result: result(["BTCUSDT"]) }; },
    send: async () => ({ status: 200, json: { ok: true } }),
  };
  const state = publisher.newSlowPublisherState();
  assert.equal((await publisher.publishSlowOnce(deps, state)).kind, "idle"); // before the settle
  now = B + 20_000;
  assert.equal((await publisher.publishSlowOnce(deps, state)).kind, "retrying");
  now = B + 40_000;
  assert.equal((await publisher.publishSlowOnce(deps, state)).kind, "idle"); // spacing
  now = B + 50_000;
  assert.equal((await publisher.publishSlowOnce(deps, state)).kind, "retrying");
  now = B + 80_000;
  assert.equal((await publisher.publishSlowOnce(deps, state)).kind, "failed");
  now = B + 200_000;
  assert.equal((await publisher.publishSlowOnce(deps, state)).kind, "idle"); // gave up until the next boundary
  assert.equal(computes, 3);
  fail = false;
  now = B + STEP + 20_000;
  assert.deepEqual(await publisher.publishSlowOnce(deps, state), { kind: "published", boundary: B + STEP });
  now = B + STEP + 300_000;
  assert.equal((await publisher.publishSlowOnce(deps, state)).kind, "idle");
  assert.equal(publisher.slowMovementEnabled({ SLOW_MOVEMENT_ENABLED: "false" }), false);
  assert.equal(publisher.slowMovementEnabled({}), true);
});

test("view: missing row is warming, then current, catching up and late by boundary age; plain words", () => {
  assert.equal(view.slowStatus(view.readSlowRow(null), B), "warming");
  assert.equal(view.slowStatus(view.readSlowRow({ payload: { nonsense: 1 }, published_at: new Date(B).toISOString() }), B), "unavailable");
  const read = view.readSlowRow({ payload: { version: "slow-movement-published-v1", computedAt: B + 20_000, result: result(["A1USDT", "B2USDT", "C3USDT", "D4USDT", "E5USDT"]) },
    published_at: new Date(B + 21_000).toISOString() });
  assert.equal(view.slowStatus(read, B + 20 * 60_000), "current");
  assert.equal(view.slowStatus(read, B + 20 * 60_000 + 1), "catching_up");
  assert.equal(view.slowStatus(read, B + 35 * 60_000 + 1), "late");
  const h = read.result.horizons[1];
  assert.equal(view.slowBreadthLine(h), "5 of 5 pairs fell");
  assert.equal(view.slowCoordinatedSentence(h), "Most pairs are falling together over 4 hours");
  assert.deepEqual([view.slowUnusualWord(3.1), view.slowUnusualWord(-2), view.slowUnusualWord(1.9), view.slowUnusualWord(null)], ["Very unusual", "Unusual", "Normal", null]);
  assert.deepEqual(view.slowPairRows(read.result, "A1USDT").map((r) => [r.label, r.returnPct]), [["1h", -1], ["4h", -1], ["12h", -1]]);
  assert.deepEqual(view.slowPairRows(read.result, "ZZUSDT").map((r) => r.returnPct), [null, null, null]);
});
