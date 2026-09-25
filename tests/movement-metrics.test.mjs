import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

const movementSource = await readFile(
  new URL("../src/lib/market/movement-buckets.ts", import.meta.url),
  "utf8",
);
const movementOutput = ts.transpileModule(movementSource, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText;
const movementUrl = `data:text/javascript;base64,${Buffer.from(movementOutput).toString("base64")}`;
const metricsSource = await readFile(
  new URL("../src/lib/market/movement-metrics.ts", import.meta.url),
  "utf8",
);
let { outputText } = ts.transpileModule(metricsSource, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
outputText = outputText.replaceAll(
  JSON.stringify("./movement-buckets"),
  JSON.stringify(movementUrl),
);
const { calculateMarketMovement, DEFAULT_MARKET_MOVEMENT_CONFIG } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`
);

const BUCKET_MS = 5_000;
const DAY_MS = 24 * 60 * 60 * 1_000;
const END = 1_800_001_800_000;
const WINDOWS = [1, 5, 15];

function approx(actual, expected, epsilon = 1e-12) {
  assert.ok(Math.abs(actual - expected) <= epsilon, `${actual} != ${expected}`);
}

function snapshot(symbol, returnsByWindow = {}, notionalPerBucket = 10) {
  const endpointByBoundary = new Map();
  for (const windowMinutes of WINDOWS) {
    const [currentReturn = 0.1, previousReturn = 0.05] = returnsByWindow[windowMinutes] ?? [];
    const windowMs = windowMinutes * 60_000;
    const previousPrice = 100 / Math.exp(currentReturn);
    endpointByBoundary.set(END, 100);
    endpointByBoundary.set(END - windowMs, previousPrice);
    endpointByBoundary.set(END - 2 * windowMs, previousPrice / Math.exp(previousReturn));
  }
  const buckets = [];
  for (let boundaryTime = END - 30 * 60_000; boundaryTime <= END; boundaryTime += BUCKET_MS) {
    buckets.push({
      boundaryTime,
      endpointPrice: endpointByBoundary.get(boundaryTime) ?? 90,
      baseQuantity: 1,
      quoteVolume: notionalPerBucket,
      tradeCount: 1,
      lastRealTradeTime: boundaryTime,
      lastRealEventTime: boundaryTime,
      carriedForward: false,
      provider: "binance-usdm",
      instrumentId: `binance-usdm:${symbol}`,
      nativeSymbol: symbol,
      symbol,
      marketType: "futures",
      contractType: "perpetual",
      priceType: "trade",
    });
  }
  return {
    symbol,
    provider: "binance-usdm",
    instrumentId: `binance-usdm:${symbol}`,
    priceType: "trade",
    bucketMs: BUCKET_MS,
    maxLastTradeAgeMs: 15_000,
    buckets,
    latestRealTradeTime: END,
    readiness: Object.fromEntries(
      WINDOWS.map((windowMinutes) => [
        windowMinutes,
        {
          windowMinutes,
          status: "READY",
          requiredHistoryMs: windowMinutes * 2 * 60_000,
          availableHistoryMs: 30 * 60_000,
        },
      ]),
    ),
  };
}

function history(windowMinutes, notionalPerBucket = 10, returns = [-0.6745, 0, 0.6745]) {
  const currentNotional = (windowMinutes * 60_000 * notionalPerBucket) / BUCKET_MS;
  return {
    returns,
    usableCoverageMs: 3 * DAY_MS,
    previousNotionalVolumes: Array(20).fill(currentNotional / 2),
  };
}

function symbolInput(symbol, returnsByWindow = {}, notionalPerBucket = 10) {
  return {
    symbol,
    sourceStatus: "LIVE",
    instrumentCompatible: true,
    snapshot: snapshot(symbol, returnsByWindow, notionalPerBucket),
    historical: Object.fromEntries(
      WINDOWS.map((windowMinutes) => [windowMinutes, history(windowMinutes, notionalPerBucket)]),
    ),
  };
}

function evaluate(symbols, configured = symbols.map((value) => value.symbol)) {
  return calculateMarketMovement({
    evaluationBoundaryTime: END,
    universe: { id: "top-usdm", version: "2026-09-25", symbols: configured },
    symbols,
  });
}

function windowResult(result, windowMinutes) {
  return result.windows.find((value) => value.windowMinutes === windowMinutes);
}

test("rejects a material threshold below the flat threshold", () => {
  assert.throws(
    () =>
      calculateMarketMovement({
        evaluationBoundaryTime: END,
        universe: { id: "top-usdm", version: "2026-09-25", symbols: [] },
        symbols: [],
        config: { ...DEFAULT_MARKET_MOVEMENT_CONFIG, flatZ: 1, materialZ: 0.5 },
      }),
    /material Z threshold must be at least the flat Z threshold/,
  );
});

test("uses exact boundaries for current and previous returns, velocity, and acceleration", () => {
  const returns = { 1: [0.12, -0.03], 5: [-0.2, 0.08], 15: [0.4, 0.1] };
  const result = evaluate([symbolInput("BTCUSDT", returns)]);
  for (const windowMinutes of WINDOWS) {
    const metrics = windowResult(result, windowMinutes).symbols[0];
    const [current, previous] = returns[windowMinutes];
    approx(metrics.currentReturn.value, current);
    approx(metrics.previousReturn.value, previous);
    approx(metrics.velocity.value, current / (windowMinutes * 60));
    approx(metrics.previousVelocity.value, previous / (windowMinutes * 60));
    approx(
      metrics.acceleration.value,
      (current / (windowMinutes * 60) - previous / (windowMinutes * 60)) / (windowMinutes * 60),
    );
  }
});

test("calculates MAD normalization, raw-sign breadth, robust aggregates, and outliers", () => {
  const returns = [-1.2, -0.3, -0.2, -0.1, 0, 0.6, 0.8, 1, 1.2, 5];
  const symbols = returns.map((currentReturn, index) =>
    symbolInput(`S${index}USDT`, { 1: [currentReturn, 0] }, (index + 1) ** 2),
  );
  const result = windowResult(evaluate(symbols), 1);

  assert.equal(result.marketWideEligible, true);
  approx(result.aggregates.medianNormalizedMovement.value, 0.3);
  approx(result.aggregates.medianRawReturn.value, 0.3);
  approx(result.aggregates.trimmedMeanNormalizedMovement.value, 0.375);
  assert.equal(result.breadth.flatCount, 4);
  assert.equal(result.breadth.risingCount, 5);
  assert.equal(result.breadth.fallingCount, 1);
  assert.equal(result.breadth.materialRisingCount, 3);
  assert.equal(result.breadth.materialFallingCount, 1);
  assert.equal(result.symbols.at(-1).outlierCandidate, true);
  assert.ok(result.symbols.at(-1).crossSectionalZ.value >= 3.5);

  const positiveRawNegativeZ = symbolInput("RAWUPUSDT", { 1: [0.1, 0] });
  positiveRawNegativeZ.historical[1] = history(1, 10, [0.2, 0.3, 0.4]);
  const direction = windowResult(evaluate([positiveRawNegativeZ]), 1).symbols[0];
  assert.ok(direction.normalizedZ.value < 0);
  assert.equal(direction.direction, "RISING");
});

test("caps liquidity weights at 25 percent and calculates RVOL", () => {
  const symbols = Array.from({ length: 5 }, (_, index) =>
    symbolInput(`L${index}USDT`, { 1: [0.1 + index / 10, 0] }, (index + 1) ** 2),
  );
  const result = windowResult(evaluate(symbols), 1);
  assert.equal(result.aggregates.liquidityWeightedNormalizedMovement.available, true);
  approx(
    result.aggregates.liquidityWeights.reduce((sum, item) => sum + item.weight, 0),
    1,
  );
  assert.ok(result.aggregates.liquidityWeights.every((item) => item.weight <= 0.25 + 1e-12));
  for (const value of result.symbols) approx(value.rvol.value, 2);

  for (const input of symbols.slice(3)) {
    for (const bucket of input.snapshot.buckets) bucket.quoteVolume = 0;
  }
  const unavailableResult = windowResult(evaluate(symbols), 1);
  assert.deepEqual(unavailableResult.aggregates.liquidityWeightedNormalizedMovement, {
    available: false,
    value: null,
    reason: "LIQUIDITY_WEIGHT_CAP_UNSATISFIABLE",
  });
});

test("zero historical or cross-sectional MAD is explicit and never uses epsilon", () => {
  const zeroHistory = symbolInput("ZEROUSDT", { 1: [0.2, 0] });
  zeroHistory.historical[1] = history(1, 10, [0.1, 0.1, 0.1]);
  const excluded = windowResult(evaluate([zeroHistory]), 1);
  assert.deepEqual(excluded.excludedSymbols[0].reasons, ["NORMALIZATION_MAD_UNAVAILABLE"]);
  assert.equal(excluded.symbols[0].normalizedZ.available, false);

  const sameReturns = Array.from({ length: 5 }, (_, index) =>
    symbolInput(`Z${index}USDT`, { 1: [0.2, 0] }),
  );
  const cross = windowResult(evaluate(sameReturns), 1);
  assert.equal(cross.marketWideEligible, true);
  assert.ok(
    cross.symbols.every(
      (value) =>
        !value.crossSectionalZ.available &&
        value.crossSectionalZ.reason === "CROSS_SECTIONAL_MAD_UNAVAILABLE" &&
        !value.outlierCandidate,
    ),
  );
});

test("stale, warming, recovering, unsupported, and insufficient history stay distinct", () => {
  const stale = symbolInput("STALEUSDT");
  stale.snapshot.readiness[15].status = "STALE";
  const latest = stale.snapshot.buckets.at(-1);
  latest.endpointPrice = null;
  latest.lastRealTradeTime = END - 20_000;

  const warming = symbolInput("WARMUSDT");
  warming.snapshot.readiness[15].status = "WARMING";
  warming.snapshot.buckets = warming.snapshot.buckets.filter(
    (bucket) => bucket.boundaryTime !== END - 30 * 60_000,
  );

  const recovering = symbolInput("RECOVERUSDT");
  recovering.sourceStatus = "RECOVERING";
  const unsupported = symbolInput("SPOTUSDT");
  unsupported.instrumentCompatible = false;
  const insufficient = symbolInput("NEWUSDT");
  insufficient.historical[15].usableCoverageMs = DAY_MS;

  const result = windowResult(
    evaluate([stale, warming, recovering, unsupported, insufficient]),
    15,
  );
  const reasons = Object.fromEntries(
    result.excludedSymbols.map((value) => [value.symbol, value.reasons]),
  );
  assert.ok(reasons.STALEUSDT.includes("STALE_LAST_TRADE"));
  assert.ok(reasons.WARMUSDT.includes("WARMING_INSUFFICIENT_LIVE_HISTORY"));
  assert.ok(reasons.RECOVERUSDT.includes("SOURCE_RECOVERING"));
  assert.ok(reasons.SPOTUSDT.includes("UNSUPPORTED_INSTRUMENT"));
  assert.ok(reasons.NEWUSDT.includes("INSUFFICIENT_NORMALIZATION_HISTORY"));
});

test("fewer than five eligible symbols makes market-wide metrics unavailable", () => {
  const symbols = Array.from({ length: 4 }, (_, index) =>
    symbolInput(`SMALL${index}USDT`, { 1: [0.1 + index / 100, 0] }),
  );
  const result = windowResult(evaluate(symbols), 1);
  assert.equal(result.eligibleCount, 4);
  assert.equal(result.eligibleFraction, 1);
  assert.equal(result.marketWideEligible, false);
  assert.deepEqual(result.aggregates.medianNormalizedMovement, {
    available: false,
    value: null,
    reason: "MARKET_UNIVERSE_INELIGIBLE",
  });
  assert.equal(result.breadth.available, false);
});

test("missing exact endpoints are excluded instead of using a nearby bucket", () => {
  const input = symbolInput("GAPUSDT", { 5: [0.2, 0.1] });
  input.snapshot.buckets = input.snapshot.buckets.filter(
    (bucket) => bucket.boundaryTime !== END - 5 * 60_000,
  );
  const result = windowResult(evaluate([input]), 5);
  assert.ok(result.excludedSymbols[0].reasons.includes("MISSING_EXACT_BOUNDARY"));
  assert.equal(result.symbols[0].currentReturn.available, false);
  assert.equal(result.configVersion, DEFAULT_MARKET_MOVEMENT_CONFIG.version);
  assert.equal(result.universeId, "top-usdm");
  assert.equal(result.provider, "binance-usdm");
  assert.equal(result.priceType, "trade");
  assert.equal(result.evaluationBoundaryTime, END);
});
