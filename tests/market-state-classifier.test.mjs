import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

const classifierSource = await readFile(
  new URL("../src/lib/market/market-state-classifier.ts", import.meta.url),
  "utf8",
);
const classifierOutput = ts.transpileModule(classifierSource, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText;
const { classifyMarketState } = await import(
  `data:text/javascript;base64,${Buffer.from(classifierOutput).toString("base64")}`
);

const END = 1_800_001_800_000;

const available = (value) => ({ available: true, value });
const unavailable = (reason = "MARKET_UNIVERSE_INELIGIBLE") => ({
  available: false,
  value: null,
  reason,
});

function symbol(index, acceleration = 1, overrides = {}) {
  return {
    symbol: `S${index}USDT`,
    included: true,
    currentReturn: available(0.1),
    normalizedZ: available(1),
    acceleration: acceleration === null ? unavailable("SYMBOL_EXCLUDED") : available(acceleration),
    crossSectionalZ: available(0),
    rvol: available(1),
    outlierCandidate: false,
    ...overrides,
  };
}

function windowFixture({
  windowMinutes = 5,
  marketWideEligible = true,
  configuredCount = 10,
  eligibleCount = configuredCount,
  excludedSymbols = [],
  accelerations = Array(eligibleCount).fill(1),
  breadth = {},
  medianRawReturn = 0,
  medianNormalizedMovement = 0,
  symbols,
} = {}) {
  const configuredUniverse = Array.from({ length: configuredCount }, (_, index) => `S${index}USDT`);
  const included =
    symbols ?? accelerations.map((acceleration, index) => symbol(index, acceleration));
  return {
    algorithmVersion: "market-movement-v1",
    configVersion: "market-movement-config-v1",
    universeId: "top-usdm",
    universeVersion: "2026-09-25",
    configuredUniverse,
    includedSymbols: included.map((value) => value.symbol),
    excludedSymbols,
    windowMinutes,
    provider: "binance-usdm",
    exchange: "binance",
    priceType: "trade",
    evaluationBoundaryTime: END,
    marketWideEligible,
    eligibleCount,
    eligibleFraction: configuredCount === 0 ? 0 : eligibleCount / configuredCount,
    symbols: included,
    breadth: {
      flatFraction: 1,
      risingFraction: 0,
      fallingFraction: 0,
      materialRisingFraction: 0,
      materialFallingFraction: 0,
      ...breadth,
    },
    aggregates: {
      medianRawReturn: marketWideEligible ? available(medianRawReturn) : unavailable(),
      medianNormalizedMovement: marketWideEligible
        ? available(medianNormalizedMovement)
        : unavailable(),
      dispersionMadNormalizedMovement: marketWideEligible ? available(0.2) : unavailable(),
    },
  };
}

function broadRise(options = {}) {
  return windowFixture({
    breadth: {
      flatFraction: 0.1,
      risingFraction: 0.8,
      fallingFraction: 0.1,
      materialRisingFraction: 0.6,
      materialFallingFraction: 0.1,
    },
    medianRawReturn: 0.1,
    medianNormalizedMovement: 0.8,
    ...options,
  });
}

function broadDrop(options = {}) {
  return windowFixture({
    breadth: {
      flatFraction: 0.1,
      risingFraction: 0.1,
      fallingFraction: 0.8,
      materialRisingFraction: 0.1,
      materialFallingFraction: 0.6,
    },
    medianRawReturn: -0.1,
    medianNormalizedMovement: -0.8,
    accelerations: Array(10).fill(-1),
    ...options,
  });
}

function evaluation(windows) {
  return {
    algorithmVersion: "market-movement-v1",
    configVersion: "market-movement-config-v1",
    universeId: "top-usdm",
    universeVersion: "2026-09-25",
    provider: "binance-usdm",
    exchange: "binance",
    priceType: "trade",
    evaluationBoundaryTime: END,
    windows,
  };
}

function classifyPrimary(primaryWindow, previousConfirmedDirection) {
  const input = {
    movement: evaluation([
      windowFixture({ windowMinutes: 1 }),
      { ...primaryWindow, windowMinutes: 5 },
      windowFixture({ windowMinutes: 15 }),
    ]),
  };
  if (previousConfirmedDirection) {
    input.previousConfirmedDirection = { 5: previousConfirmedDirection };
  }
  return classifyMarketState(input).windows.find((window) => window.windowMinutes === 5);
}

test("classifies broad rise, broad drop, and eligible neutral windows", () => {
  assert.equal(classifyPrimary(broadRise()).directionState, "BROAD_RISE");
  assert.equal(classifyPrimary(broadDrop()).directionState, "BROAD_DROP");
  const neutral = classifyPrimary(
    windowFixture({
      breadth: { risingFraction: 0.69, materialRisingFraction: 0.6 },
      medianRawReturn: 0.1,
      medianNormalizedMovement: 0.8,
    }),
  );
  assert.equal(neutral.directionState, "NEUTRAL");
  assert.equal(neutral.pace, "NOT_APPLICABLE");
});

test("classifies directional pace without treating it as a direction forecast", () => {
  assert.equal(classifyPrimary(broadRise()).pace, "ACCELERATING");
  assert.equal(
    classifyPrimary(broadRise({ accelerations: Array(10).fill(-1) })).pace,
    "DECELERATING",
  );
  assert.equal(classifyPrimary(broadDrop()).pace, "ACCELERATING");
  assert.equal(
    classifyPrimary(broadDrop({ accelerations: Array(10).fill(1) })).pace,
    "DECELERATING",
  );
  assert.equal(
    classifyPrimary(broadRise({ accelerations: [-1, -1, -1, -1, -1, 1, 1, 1, 1, 1] })).pace,
    "MIXED",
  );
});

test("insufficient acceleration coverage forces mixed pace and remains explicit", () => {
  const result = classifyPrimary(
    broadRise({ accelerations: [1, 1, 1, 1, 1, null, null, null, null, null] }),
  );
  assert.equal(result.pace, "MIXED");
  assert.equal(result.evidence.accelerationCoverage, 0.5);
  assert.equal(result.evidence.positiveAccelerationFraction, 1);
});

test("marks a breadth-disagreeing positive candidate as an isolated outlier", () => {
  const symbols = Array.from({ length: 10 }, (_, index) => symbol(index));
  symbols[0] = symbol(0, 1, {
    currentReturn: available(0.4),
    normalizedZ: available(4),
    crossSectionalZ: available(5),
    outlierCandidate: true,
  });
  const result = classifyPrimary(
    windowFixture({
      symbols,
      breadth: { flatFraction: 0.5, risingFraction: 0.4, fallingFraction: 0.1 },
    }),
  );
  assert.deepEqual(result.evidence.isolatedOutliers, [
    {
      symbol: "S0USDT",
      rawReturn: 0.4,
      historicalNormalizedZ: 4,
      crossSectionalZ: 5,
      direction: "RISING",
      sameDirectionBreadth: 0.4,
      windowMinutes: 5,
      evaluationBoundaryTime: END,
    },
  ]);
});

test("does not isolate the same candidate when same-direction breadth is at least half", () => {
  const symbols = Array.from({ length: 10 }, (_, index) => symbol(index));
  symbols[0] = symbol(0, 1, {
    currentReturn: available(0.4),
    normalizedZ: available(4),
    crossSectionalZ: available(5),
    outlierCandidate: true,
  });
  const result = classifyPrimary(
    windowFixture({
      symbols,
      breadth: { flatFraction: 0.4, risingFraction: 0.5, fallingFraction: 0.1 },
    }),
  );
  assert.deepEqual(result.evidence.isolatedOutliers, []);
});

test("requires a full opposite broad state and prior confirmed direction for reversal", () => {
  assert.equal(classifyPrimary(broadDrop(), "BROAD_RISE").reversalCandidate, true);
  assert.equal(classifyPrimary(broadDrop()).reversalCandidate, false);
  assert.equal(classifyPrimary(windowFixture(), "BROAD_RISE").reversalCandidate, false);
});

test("distinguishes warming-only ineligibility from hard unavailable failures", () => {
  const warming = windowFixture({
    marketWideEligible: false,
    configuredCount: 6,
    eligibleCount: 4,
    accelerations: Array(4).fill(1),
    excludedSymbols: [
      {
        symbol: "S4USDT",
        reasons: ["WARMING_INSUFFICIENT_LIVE_HISTORY", "MISSING_EXACT_BOUNDARY"],
      },
      { symbol: "S5USDT", reasons: ["WARMING_INSUFFICIENT_LIVE_HISTORY"] },
    ],
  });
  assert.equal(classifyPrimary(warming).directionState, "WARMING");

  for (const reason of [
    "SOURCE_STALE",
    "SOURCE_RECOVERING",
    "INSUFFICIENT_NORMALIZATION_HISTORY",
  ]) {
    const unavailableWindow = windowFixture({
      marketWideEligible: false,
      configuredCount: 6,
      eligibleCount: 4,
      accelerations: Array(4).fill(1),
      excludedSymbols: [
        { symbol: "S4USDT", reasons: [reason] },
        { symbol: "S5USDT", reasons: [reason] },
      ],
    });
    assert.equal(classifyPrimary(unavailableWindow).directionState, "UNAVAILABLE");
  }

  const tooSmall = windowFixture({
    marketWideEligible: false,
    configuredCount: 4,
    eligibleCount: 4,
    accelerations: Array(4).fill(1),
  });
  assert.equal(classifyPrimary(tooSmall).directionState, "UNAVAILABLE");
});

test("labels 1m rapid, 5m primary, and 15m persistence", () => {
  const result = classifyMarketState({
    movement: evaluation([
      windowFixture({ windowMinutes: 1 }),
      windowFixture({ windowMinutes: 5 }),
      windowFixture({ windowMinutes: 15 }),
    ]),
  });
  assert.equal(result.primaryWindowMinutes, 5);
  assert.deepEqual(
    result.windows.map(({ windowMinutes, horizonRole }) => ({ windowMinutes, horizonRole })),
    [
      { windowMinutes: 1, horizonRole: "RAPID" },
      { windowMinutes: 5, horizonRole: "PRIMARY" },
      { windowMinutes: 15, horizonRole: "PERSISTENCE" },
    ],
  );
});
