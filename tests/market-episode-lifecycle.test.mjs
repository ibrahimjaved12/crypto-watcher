import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { after, before, test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";
import { PGlite } from "./node_modules/@electric-sql/pglite/dist/index.js";

// Transpile and load market-episode-lifecycle
const lifecycleSource = await readFile(
  new URL("../src/lib/market/market-episode-lifecycle.ts", import.meta.url),
  "utf8",
);
const lifecycleOutput = ts.transpileModule(lifecycleSource, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText;
const {
  processMarketEpisodeLifecycle,
  restoreLifecycleStateOnRestart,
  computeEpisodeId,
  computeEventId,
  extractSupportingAndConflictingSymbols,
  serializeMarketEpisodeLifecycleState,
  deserializeMarketEpisodeLifecycleState,
  markMarketEpisodeStatePersisted,
} = await import(`data:text/javascript;base64,${Buffer.from(lifecycleOutput).toString("base64")}`);

const BASE_TIME = 1_800_000_000_000;

function lifecycleConfig(version) {
  return {
    version,
    currentSnapshotCadenceMs: 30_000,
    startConfirmationCount: 2,
    exitConfirmationCount: 3,
    reversalConfirmationCount: 2,
    strengthConfirmationCount: 2,
    continuationRisingBreadth: 0.55,
    continuationFallingBreadth: 0.55,
    materialStrengthenBreadth: 0.7,
    materialWeakenBreadth: 0.5,
  };
}

const available = (value) => ({ available: true, value });
const unavailable = (reason = "MARKET_UNIVERSE_INELIGIBLE") => ({
  available: false,
  value: null,
  reason,
});

// Explicit #72 acceleration evidence supplied to #73 fixtures; no classification runs here.
const ACCELERATION_EVIDENCE = {
  RISE_ACCELERATING: { medianAcceleration: available(1),
    positiveAccelerationFraction: 1, negativeAccelerationFraction: 0 },
  RISE_DECELERATING: { medianAcceleration: available(-1),
    positiveAccelerationFraction: 0, negativeAccelerationFraction: 1 },
  DROP_ACCELERATING: { medianAcceleration: available(-1),
    positiveAccelerationFraction: 0, negativeAccelerationFraction: 1 },
  DROP_DECELERATING: { medianAcceleration: available(1),
    positiveAccelerationFraction: 1, negativeAccelerationFraction: 0 },
  MIXED: { medianAcceleration: available(0),
    positiveAccelerationFraction: 0.5, negativeAccelerationFraction: 0.5 },
  UNAVAILABLE: { medianAcceleration: unavailable("ACCELERATION_UNAVAILABLE"),
    positiveAccelerationFraction: null, negativeAccelerationFraction: null },
};

function makeSymbol(index, overrides = {}) {
  const isRising = overrides.direction === "RISING";
  const isFalling = overrides.direction === "FALLING";
  const defaultReturn = isRising ? 0.1 : isFalling ? -0.1 : 0;
  const defaultZ = isRising ? 1.0 : isFalling ? -1.0 : 0;

  return {
    symbol: `S${index}USDT`,
    instrumentId: `binance-usdm:S${index}USDT`,
    provider: "binance-usdm",
    exchange: "binance",
    priceType: "trade",
    windowMinutes: 5,
    evaluationBoundaryTime: BASE_TIME,
    included: true,
    exclusionReasons: [],
    currentReturn: available(defaultReturn),
    previousReturn: available(0),
    velocity: available(defaultReturn),
    previousVelocity: available(0),
    acceleration: available(isRising ? 1 : isFalling ? -1 : 0),
    historicalMedian: available(0),
    historicalMad: available(0.05),
    normalizedZ: available(defaultZ),
    direction: "FLAT",
    materialRising: isRising,
    materialFalling: isFalling,
    currentNotionalVolume: available(1000),
    rvol: available(1),
    crossSectionalZ: available(0),
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
  boundaryTime = BASE_TIME,
  universeId = "top-usdm",
  universeVersion = "2026-09-25",
  classificationDirection = "NEUTRAL",
  classificationPace = "NOT_APPLICABLE",
  classificationAccelerationEvidence = ACCELERATION_EVIDENCE.RISE_ACCELERATING,
} = {}) {
  const configuredUniverse = Array.from({ length: configuredCount }, (_, index) => `S${index}USDT`);
  const included =
    symbols ??
    accelerations.map((acceleration, index) =>
      makeSymbol(index, {
        acceleration:
          acceleration === null ? unavailable("SYMBOL_EXCLUDED") : available(acceleration),
        direction: acceleration > 0 ? "RISING" : acceleration < 0 ? "FALLING" : "FLAT",
      }),
    );

  return {
    algorithmVersion: "market-movement-v1",
    configVersion: "market-movement-config-v1",
    universeId,
    universeVersion,
    classificationDirection,
    classificationPace,
    classificationAccelerationEvidence,
    configuredUniverse,
    includedSymbols: included.filter((s) => s.included).map((s) => s.symbol),
    excludedSymbols,
    windowMinutes,
    provider: "binance-usdm",
    exchange: "binance",
    priceType: "trade",
    evaluationBoundaryTime: boundaryTime,
    marketWideEligible,
    eligibleCount,
    eligibleFraction: configuredCount === 0 ? 0 : eligibleCount / configuredCount,
    symbols: included,
    breadth: {
      available: marketWideEligible,
      unavailableReason: marketWideEligible ? null : "MARKET_UNIVERSE_INELIGIBLE",
      denominator: eligibleCount,
      flatCount: 1,
      risingCount: 8,
      fallingCount: 1,
      materialRisingCount: 6,
      materialFallingCount: 1,
      flatFraction: 0.1,
      risingFraction: 0.8,
      fallingFraction: 0.1,
      materialRisingFraction: 0.6,
      materialFallingFraction: 0.1,
      ...breadth,
    },
    aggregates: {
      medianRawReturn: marketWideEligible ? available(medianRawReturn) : unavailable(),
      medianNormalizedMovement: marketWideEligible
        ? available(medianNormalizedMovement)
        : unavailable(),
      trimmedMeanNormalizedMovement: available(0),
      liquidityWeightedNormalizedMovement: available(0),
      liquidityWeights: [],
      dispersionMadNormalizedMovement: marketWideEligible ? available(0.2) : unavailable(),
    },
  };
}

function broadRiseWindow(options = {}) {
  return windowFixture({
    classificationDirection: "BROAD_RISE",
    classificationPace: "ACCELERATING",
    classificationAccelerationEvidence: ACCELERATION_EVIDENCE.RISE_ACCELERATING,
    breadth: {
      flatFraction: 0.1,
      risingFraction: 0.8,
      fallingFraction: 0.1,
      materialRisingFraction: 0.6,
      materialFallingFraction: 0.1,
    },
    medianRawReturn: 0.1,
    medianNormalizedMovement: 0.8,
    accelerations: Array(10).fill(1),
    ...options,
  });
}

function broadDropWindow(options = {}) {
  return windowFixture({
    classificationDirection: "BROAD_DROP",
    classificationPace: "ACCELERATING",
    classificationAccelerationEvidence: ACCELERATION_EVIDENCE.DROP_ACCELERATING,
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

function neutralWindow(options = {}) {
  return windowFixture({
    classificationAccelerationEvidence: ACCELERATION_EVIDENCE.MIXED,
    breadth: {
      flatFraction: 0.4,
      risingFraction: 0.3,
      fallingFraction: 0.3,
      materialRisingFraction: 0.2,
      materialFallingFraction: 0.2,
    },
    medianRawReturn: 0.01,
    medianNormalizedMovement: 0.1,
    accelerations: [-1, -1, -1, -1, -1, 1, 1, 1, 1, 1],
    ...options,
  });
}

function makePair({
  primaryWindow,
  boundaryTime = BASE_TIME,
  universeId = "top-usdm",
  universeVersion = "2026-09-25",
}) {
  const pw = {
    ...primaryWindow,
    windowMinutes: 5,
    evaluationBoundaryTime: boundaryTime,
    universeId,
    universeVersion,
  };
  const movement = {
    algorithmVersion: "market-movement-v1",
    configVersion: "market-movement-config-v1",
    universeId,
    universeVersion,
    provider: "binance-usdm",
    exchange: "binance",
    priceType: "trade",
    evaluationBoundaryTime: boundaryTime,
    windows: [
      windowFixture({ windowMinutes: 1, boundaryTime, universeId, universeVersion }),
      pw,
      windowFixture({ windowMinutes: 15, boundaryTime, universeId, universeVersion }),
    ],
  };
  // #73 fixtures carry explicit canonical #72 labels. No classifier runs in TS.
  const classification = {
    algorithmVersion: "market-state-classifier-v1",
    configVersion: "market-state-classifier-config-v1",
    movementAlgorithmVersion: movement.algorithmVersion,
    movementConfigVersion: movement.configVersion,
    universeId, universeVersion,
    evaluationBoundaryTime: boundaryTime,
    provider: movement.provider, exchange: movement.exchange, priceType: movement.priceType,
    primaryWindowMinutes: 5,
    canonicalWindows: movement.windows,
    windows: movement.windows.map((snapshot) => ({
      windowMinutes: snapshot.windowMinutes,
      horizonRole: snapshot.windowMinutes === 1 ? "RAPID" :
        snapshot.windowMinutes === 5 ? "PRIMARY" : "PERSISTENCE",
      directionState: snapshot.classificationDirection,
      pace: snapshot.classificationPace,
      reversalCandidate: false,
      algorithmVersion: "market-state-classifier-v1",
      configVersion: "market-state-classifier-config-v1",
      movementAlgorithmVersion: movement.algorithmVersion,
      movementConfigVersion: movement.configVersion,
      universeId, universeVersion,
      evaluationBoundaryTime: boundaryTime,
      provider: movement.provider, exchange: movement.exchange, priceType: movement.priceType,
      evidence: {
        eligibleCount: snapshot.eligibleCount,
        eligibleFraction: snapshot.eligibleFraction,
        includedSymbols: snapshot.includedSymbols,
        excludedSymbols: snapshot.excludedSymbols,
        flatFraction: snapshot.breadth.flatFraction,
        risingFraction: snapshot.breadth.risingFraction,
        fallingFraction: snapshot.breadth.fallingFraction,
        materialRisingFraction: snapshot.breadth.materialRisingFraction,
        materialFallingFraction: snapshot.breadth.materialFallingFraction,
        medianRawReturn: snapshot.aggregates.medianRawReturn,
        medianNormalizedMovement: snapshot.aggregates.medianNormalizedMovement,
        medianAcceleration: snapshot.classificationAccelerationEvidence.medianAcceleration,
        positiveAccelerationFraction:
          snapshot.classificationAccelerationEvidence.positiveAccelerationFraction,
        negativeAccelerationFraction:
          snapshot.classificationAccelerationEvidence.negativeAccelerationFraction,
        dispersion: snapshot.aggregates.dispersionMadNormalizedMovement,
        rvolSummary: [], isolatedOutliers: [],
      },
    })),
  };
  return { movement, classification };
}

function persistedCurrent(result) {
  const state = result.nextState;
  const persistedState = markMarketEpisodeStatePersisted(state);
  return {
    universeId: state.universeId,
    primaryWindowMinutes: state.primaryWindowMinutes,
    universeVersion: state.universeVersion,
    provider: state.provider,
    exchange: state.exchange,
    priceType: state.priceType,
    evaluationBoundaryTime: state.evaluationBoundaryTime,
    directionState: state.currentDirectionState,
    pace: state.currentPace,
    activeEpisodeId: state.activeEpisode?.episodeId ?? null,
    activeDirection: state.activeEpisode?.direction ?? null,
    interrupted: state.interrupted,
    episodeAlgorithmVersion: state.episodeAlgorithmVersion,
    lifecycleConfigVersion: state.lifecycleConfigVersion,
    classifierAlgorithmVersion: state.classifierAlgorithmVersion,
    classifierConfigVersion: state.classifierConfigVersion,
    movementAlgorithmVersion: state.movementAlgorithmVersion,
    movementConfigVersion: state.movementConfigVersion,
    lifecycleState: serializeMarketEpisodeLifecycleState(persistedState),
    currentEvidence: result.currentEvidence,
  };
}

// -------------------------------------------------------------
// Database setup for persistence tests
// -------------------------------------------------------------
const db = new PGlite();

before(async () => {
  await db.exec(`
    CREATE ROLE anon;
    CREATE ROLE authenticated;
    CREATE ROLE service_role BYPASSRLS;
    GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
  `);
  await db.exec(
    await readFile(
      new URL(
        "../operational-db/supabase/migrations/20260925090000_operational_store.sql",
        import.meta.url,
      ),
      "utf8",
    ),
  );
  await db.exec(
    await readFile(
      new URL(
        "../operational-db/supabase/migrations/20260925120000_binance_collector.sql",
        import.meta.url,
      ),
      "utf8",
    ),
  );
  await db.exec(
    await readFile(
      new URL(
        "../operational-db/supabase/migrations/20260925180000_market_movement_episodes.sql",
        import.meta.url,
      ),
      "utf8",
    ),
  );
});

after(() => db.close());

// =============================================================
// Section O Test Requirements
// =============================================================

test("1. one broad evaluation => no STARTED", () => {
  const { movement, classification } = makePair({
    primaryWindow: broadRiseWindow(),
    boundaryTime: BASE_TIME,
  });
  const result = processMarketEpisodeLifecycle({
    classification,
    movement,
    previousState: null,
  });

  assert.equal(result.transitions.length, 0);
  assert.equal(result.nextState.activeEpisode, null);
  assert.notEqual(result.nextState.pendingCandidate, null);
  assert.equal(result.nextState.pendingCandidate.direction, "BROAD_RISE");
  assert.equal(result.nextState.pendingCandidate.count, 1);
  assert.equal(result.nextState.pendingCandidate.startBoundaryTime, BASE_TIME);
});

test("explicit decelerating broad-drop evidence stays directionally coherent", () => {
  const drop = broadDropWindow({
    classificationPace: "DECELERATING",
    classificationAccelerationEvidence: ACCELERATION_EVIDENCE.DROP_DECELERATING,
    accelerations: Array(10).fill(1),
  });
  const pair = makePair({ primaryWindow: drop });
  const evidence = pair.classification.windows[1].evidence;
  assert.deepEqual(evidence.medianAcceleration, available(1));
  assert.equal(evidence.positiveAccelerationFraction, 1);
  assert.equal(evidence.negativeAccelerationFraction, 0);
});

test("duplicate and backward boundaries never advance start confirmation", () => {
  const candidate = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME });
  const first = processMarketEpisodeLifecycle({
    ...candidate,
    previousState: null,
  });
  const duplicate = processMarketEpisodeLifecycle({
    ...candidate,
    previousState: first.nextState,
  });

  assert.equal(duplicate.transitions.length, 0);
  assert.equal(duplicate.nextState.pendingCandidate?.count, 1);
  assert.equal(duplicate.nextState.evaluationBoundaryTime, BASE_TIME);
  assert.equal(duplicate.shouldPersistCurrentImmediately, false);

  const older = makePair({
    primaryWindow: broadRiseWindow(),
    boundaryTime: BASE_TIME - 5_000,
  });
  assert.throws(
    () => processMarketEpisodeLifecycle({ ...older, previousState: first.nextState }),
    /must not move backward/,
  );
});

test("pending start followed by a skipped boundary starts fresh at count one", () => {
  const first = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME });
  const pending = processMarketEpisodeLifecycle({ ...first, previousState: null });
  const afterGap = makePair({
    primaryWindow: broadRiseWindow(),
    boundaryTime: BASE_TIME + 10_000,
  });
  const result = processMarketEpisodeLifecycle({
    ...afterGap,
    previousState: pending.nextState,
  });

  assert.equal(result.transitions.length, 0);
  assert.equal(result.nextState.activeEpisode, null);
  assert.deepEqual(result.nextState.pendingCandidate, {
    direction: "BROAD_RISE",
    startBoundaryTime: BASE_TIME + 10_000,
    count: 1,
  });
});

test("2. second consecutive same broad direction => one STARTED with first boundary start time", () => {
  const eval1 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME });
  const step1 = processMarketEpisodeLifecycle({
    classification: eval1.classification,
    movement: eval1.movement,
    previousState: null,
  });

  const eval2 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME + 5_000 });
  const step2 = processMarketEpisodeLifecycle({
    classification: eval2.classification,
    movement: eval2.movement,
    previousState: step1.nextState,
  });

  assert.equal(step2.transitions.length, 1);
  const started = step2.transitions[0];
  assert.equal(started.transition, "STARTED");
  assert.equal(started.direction, "BROAD_RISE");
  assert.equal(started.transitionReason, "confirmed_broad_entry");
  // Episode start boundary should be FIRST qualifying evaluation (BASE_TIME), not second (BASE_TIME + 5000)
  assert.equal(started.episodeStartBoundaryTime, BASE_TIME);
  assert.equal(started.evaluationBoundaryTime, BASE_TIME + 5_000);
  assert.notEqual(step2.nextState.activeEpisode, null);
  assert.equal(step2.nextState.activeEpisode.direction, "BROAD_RISE");
  assert.equal(step2.nextState.activeEpisode.startBoundaryTime, BASE_TIME);
  assert.equal(step2.nextState.episodeAlgorithmVersion, "market-episode-v1");
  assert.equal(step2.nextState.lifecycleConfigVersion, "market-episode-config-v1");
  assert.equal(step2.nextState.activeEpisode.episodeAlgorithmVersion, "market-episode-v1");
  assert.equal(step2.nextState.activeEpisode.lifecycleConfigVersion, "market-episode-config-v1");
  assert.equal(started.episodeAlgorithmVersion, "market-episode-v1");
  assert.equal(started.lifecycleConfigVersion, "market-episode-config-v1");
  assert.equal(step2.shouldPersistCurrentImmediately, true);
});

test("3. repeated broad evaluations => STARTED is not repeated", () => {
  const eval1 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME });
  const step1 = processMarketEpisodeLifecycle({
    classification: eval1.classification,
    movement: eval1.movement,
    previousState: null,
  });

  const eval2 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME + 5_000 });
  const step2 = processMarketEpisodeLifecycle({
    classification: eval2.classification,
    movement: eval2.movement,
    previousState: step1.nextState,
  });
  assert.equal(step2.transitions.length, 1);

  const eval3 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME + 10_000 });
  const step3 = processMarketEpisodeLifecycle({
    classification: eval3.classification,
    movement: eval3.movement,
    previousState: step2.nextState,
  });
  assert.equal(step3.transitions.length, 0);
  assert.equal(step3.nextState.activeEpisode?.episodeId, step2.nextState.activeEpisode?.episodeId);

  const eval4 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME + 15_000 });
  const step4 = processMarketEpisodeLifecycle({
    classification: eval4.classification,
    movement: eval4.movement,
    previousState: step3.nextState,
  });
  assert.equal(step4.transitions.length, 0);
  assert.equal(step4.nextState.activeEpisode?.direction, "BROAD_RISE");
});

test("4. 70% entry can fall below 70% but stay >=55% without ending", () => {
  // Start active episode
  const eval1 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME });
  const step1 = processMarketEpisodeLifecycle({
    classification: eval1.classification,
    movement: eval1.movement,
    previousState: null,
  });
  const eval2 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME + 5_000 });
  const step2 = processMarketEpisodeLifecycle({
    classification: eval2.classification,
    movement: eval2.movement,
    previousState: step1.nextState,
  });
  assert.equal(step2.transitions[0].transition, "STARTED");

  // Breadth drops to 60% (below 70% entry threshold, but >= 55% continuation threshold)
  const eval3 = makePair({
    primaryWindow: windowFixture({
      breadth: { risingFraction: 0.6, materialRisingFraction: 0.45 },
      medianRawReturn: 0.05,
      medianNormalizedMovement: 0.4,
    }),
    boundaryTime: BASE_TIME + 10_000,
  });
  const step3 = processMarketEpisodeLifecycle({
    classification: eval3.classification,
    movement: eval3.movement,
    previousState: step2.nextState,
  });

  assert.equal(step3.transitions.length, 0);
  assert.notEqual(step3.nextState.activeEpisode, null);
  assert.equal(step3.nextState.pendingExitFailureCount, 0);
});

test("5. 3 consecutive failed continuation evaluations => ENDED", () => {
  // Start active episode
  const eval1 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME });
  const step1 = processMarketEpisodeLifecycle({
    classification: eval1.classification,
    movement: eval1.movement,
    previousState: null,
  });
  const eval2 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME + 5_000 });
  const step2 = processMarketEpisodeLifecycle({
    classification: eval2.classification,
    movement: eval2.movement,
    previousState: step1.nextState,
  });

  // Failure 1 (risingFraction = 0.40 < 0.55)
  const fail1 = makePair({
    primaryWindow: windowFixture({
      breadth: { risingFraction: 0.4, materialRisingFraction: 0.2 },
      medianRawReturn: 0.01,
      medianNormalizedMovement: 0.2,
    }),
    boundaryTime: BASE_TIME + 10_000,
  });
  const step3 = processMarketEpisodeLifecycle({
    classification: fail1.classification,
    movement: fail1.movement,
    previousState: step2.nextState,
  });
  assert.equal(step3.transitions.length, 0);
  assert.equal(step3.nextState.pendingExitFailureCount, 1);
  assert.notEqual(step3.nextState.activeEpisode, null);

  // Failure 2
  const fail2 = makePair({
    primaryWindow: windowFixture({
      breadth: { risingFraction: 0.4, materialRisingFraction: 0.2 },
      medianRawReturn: 0.01,
      medianNormalizedMovement: 0.2,
    }),
    boundaryTime: BASE_TIME + 15_000,
  });
  const step4 = processMarketEpisodeLifecycle({
    classification: fail2.classification,
    movement: fail2.movement,
    previousState: step3.nextState,
  });
  assert.equal(step4.transitions.length, 0);
  assert.equal(step4.nextState.pendingExitFailureCount, 2);
  assert.notEqual(step4.nextState.activeEpisode, null);

  // Failure 3 => ENDED
  const fail3 = makePair({
    primaryWindow: windowFixture({
      breadth: { risingFraction: 0.4, materialRisingFraction: 0.2 },
      medianRawReturn: 0.01,
      medianNormalizedMovement: 0.2,
    }),
    boundaryTime: BASE_TIME + 20_000,
  });
  const step5 = processMarketEpisodeLifecycle({
    classification: fail3.classification,
    movement: fail3.movement,
    previousState: step4.nextState,
  });
  assert.equal(step5.transitions.length, 1);
  assert.equal(step5.transitions[0].transition, "ENDED");
  assert.equal(step5.transitions[0].transitionReason, "continuation_failed");
  assert.equal(step5.nextState.activeEpisode, null);
  assert.equal(step5.nextState.pendingExitFailureCount, 0);
});

test("6. exit counter resets when continuation recovers", () => {
  // Start active episode
  const eval1 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME });
  const step1 = processMarketEpisodeLifecycle({
    classification: eval1.classification,
    movement: eval1.movement,
    previousState: null,
  });
  const eval2 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME + 5_000 });
  const step2 = processMarketEpisodeLifecycle({
    classification: eval2.classification,
    movement: eval2.movement,
    previousState: step1.nextState,
  });

  // Failure 1
  const fail1 = makePair({
    primaryWindow: neutralWindow(),
    boundaryTime: BASE_TIME + 10_000,
  });
  const step3 = processMarketEpisodeLifecycle({
    classification: fail1.classification,
    movement: fail1.movement,
    previousState: step2.nextState,
  });
  assert.equal(step3.nextState.pendingExitFailureCount, 1);

  // Failure 2
  const fail2 = makePair({
    primaryWindow: neutralWindow(),
    boundaryTime: BASE_TIME + 15_000,
  });
  const step4 = processMarketEpisodeLifecycle({
    classification: fail2.classification,
    movement: fail2.movement,
    previousState: step3.nextState,
  });
  assert.equal(step4.nextState.pendingExitFailureCount, 2);

  // Continuation recovers (risingFraction = 0.60, medianRawReturn = 0.05 > 0)
  const recovery = makePair({
    primaryWindow: windowFixture({
      breadth: { risingFraction: 0.6, materialRisingFraction: 0.4 },
      medianRawReturn: 0.05,
      medianNormalizedMovement: 0.4,
    }),
    boundaryTime: BASE_TIME + 20_000,
  });
  const step5 = processMarketEpisodeLifecycle({
    classification: recovery.classification,
    movement: recovery.movement,
    previousState: step4.nextState,
  });
  assert.equal(step5.nextState.pendingExitFailureCount, 0);
  assert.notEqual(step5.nextState.activeEpisode, null);

  // Subsequent single failure only increments to 1, does NOT trigger ENDED
  const failAgain = makePair({
    primaryWindow: neutralWindow(),
    boundaryTime: BASE_TIME + 25_000,
  });
  const step6 = processMarketEpisodeLifecycle({
    classification: failAgain.classification,
    movement: failAgain.movement,
    previousState: step5.nextState,
  });
  assert.equal(step6.transitions.length, 0);
  assert.equal(step6.nextState.pendingExitFailureCount, 1);
});

test("7, 8, 9. 2 consecutive full opposite broad states => one REVERSED, new episode start boundary, no extra STARTED, no cooldown", () => {
  // Start active BROAD_RISE episode at t=1000, confirmed at t=1005
  const eval1 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME });
  const step1 = processMarketEpisodeLifecycle({
    classification: eval1.classification,
    movement: eval1.movement,
    previousState: null,
  });
  const eval2 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME + 5_000 });
  const step2 = processMarketEpisodeLifecycle({
    classification: eval2.classification,
    movement: eval2.movement,
    previousState: step1.nextState,
  });
  const oldEpisodeId = step2.nextState.activeEpisode.episodeId;

  // Immediately (t=1010), opposite broad drop evaluation arrives (tick 1)
  const opp1 = makePair({ primaryWindow: broadDropWindow(), boundaryTime: BASE_TIME + 10_000 });
  const step3 = processMarketEpisodeLifecycle({
    classification: opp1.classification,
    movement: opp1.movement,
    previousState: step2.nextState,
  });
  assert.equal(step3.transitions.length, 0);
  assert.equal(step3.nextState.pendingReversal?.count, 1);
  assert.equal(step3.nextState.pendingReversal?.toDirection, "BROAD_DROP");
  assert.equal(step3.nextState.pendingReversal?.startBoundaryTime, BASE_TIME + 10_000);

  // Tick 2 (t=1015): opposite broad drop arrives again => REVERSAL CONFIRMED
  const opp2 = makePair({ primaryWindow: broadDropWindow(), boundaryTime: BASE_TIME + 15_000 });
  const step4 = processMarketEpisodeLifecycle({
    classification: opp2.classification,
    movement: opp2.movement,
    previousState: step3.nextState,
  });

  // Reversal checks:
  assert.equal(step4.transitions.length, 1); // EXACTLY ONE event (no extra STARTED)
  const rev = step4.transitions[0];
  assert.equal(rev.transition, "REVERSED");
  assert.equal(rev.episodeId, oldEpisodeId);
  assert.equal(rev.fromDirection, "BROAD_RISE");
  assert.equal(rev.toDirection, "BROAD_DROP");
  assert.equal(rev.transitionReason, "reversal_confirmed");
  assert.equal(rev.evaluationBoundaryTime, BASE_TIME + 15_000);
  assert.equal(rev.episodeStartBoundaryTime, BASE_TIME);
  assert.equal(rev.direction, "BROAD_DROP");
  assert.equal(rev.pace, "ACCELERATING");
  assert.equal(rev.directionalBreadth, 0.8);
  assert.equal(rev.materialBreadth, 0.6);
  assert.equal(rev.medianAcceleration, -1);
  assert.equal(rev.accelerationBreadth, 1);
  assert.deepEqual(
    rev.supportingContracts,
    Array.from({ length: 10 }, (_, i) => `S${i}USDT`),
  );
  assert.deepEqual(rev.conflictingContracts, []);

  // New active episode checks:
  assert.notEqual(step4.nextState.activeEpisode, null);
  assert.notEqual(step4.nextState.activeEpisode.episodeId, oldEpisodeId);
  assert.equal(step4.nextState.activeEpisode.direction, "BROAD_DROP");
  // New episode start boundary = FIRST opposite evaluation (BASE_TIME + 10_000)
  assert.equal(step4.nextState.activeEpisode.startBoundaryTime, BASE_TIME + 10_000);
});

test("duplicate pending reversal evaluation cannot confirm REVERSED", () => {
  const firstRise = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME });
  const step1 = processMarketEpisodeLifecycle({ ...firstRise, previousState: null });
  const secondRise = makePair({
    primaryWindow: broadRiseWindow(),
    boundaryTime: BASE_TIME + 5_000,
  });
  const step2 = processMarketEpisodeLifecycle({
    ...secondRise,
    previousState: step1.nextState,
  });
  const firstDrop = makePair({
    primaryWindow: broadDropWindow(),
    boundaryTime: BASE_TIME + 10_000,
  });
  const pending = processMarketEpisodeLifecycle({
    ...firstDrop,
    previousState: step2.nextState,
  });
  const duplicate = processMarketEpisodeLifecycle({
    ...firstDrop,
    previousState: pending.nextState,
  });

  assert.equal(duplicate.transitions.length, 0);
  assert.equal(duplicate.nextState.pendingReversal?.count, 1);
  assert.equal(duplicate.nextState.activeEpisode?.direction, "BROAD_RISE");
});

test("pending reversal followed by a skipped boundary restarts reversal confirmation", () => {
  const firstRise = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME });
  const step1 = processMarketEpisodeLifecycle({ ...firstRise, previousState: null });
  const secondRise = makePair({
    primaryWindow: broadRiseWindow(),
    boundaryTime: BASE_TIME + 5_000,
  });
  const active = processMarketEpisodeLifecycle({ ...secondRise, previousState: step1.nextState });
  const firstDrop = makePair({
    primaryWindow: broadDropWindow(),
    boundaryTime: BASE_TIME + 10_000,
  });
  const pending = processMarketEpisodeLifecycle({ ...firstDrop, previousState: active.nextState });
  const dropAfterGap = makePair({
    primaryWindow: broadDropWindow(),
    boundaryTime: BASE_TIME + 20_000,
  });
  const result = processMarketEpisodeLifecycle({
    ...dropAfterGap,
    previousState: pending.nextState,
  });

  assert.equal(result.transitions.length, 0);
  assert.equal(result.nextState.interrupted, true);
  assert.deepEqual(result.nextState.pendingReversal, {
    toDirection: "BROAD_DROP",
    startBoundaryTime: BASE_TIME + 20_000,
    count: 1,
  });
  assert.equal(result.nextState.activeEpisode?.direction, "BROAD_RISE");
});

test("an active episode becomes interrupted after a gap and requires fresh resume confirmation", () => {
  const firstRise = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME });
  const step1 = processMarketEpisodeLifecycle({ ...firstRise, previousState: null });
  const secondRise = makePair({
    primaryWindow: broadRiseWindow(),
    boundaryTime: BASE_TIME + 5_000,
  });
  const active = processMarketEpisodeLifecycle({ ...secondRise, previousState: step1.nextState });
  const riseAfterGap = makePair({
    primaryWindow: broadRiseWindow(),
    boundaryTime: BASE_TIME + 15_000,
  });
  const interrupted = processMarketEpisodeLifecycle({
    ...riseAfterGap,
    previousState: active.nextState,
  });

  assert.equal(interrupted.transitions.length, 0);
  assert.equal(interrupted.nextState.interrupted, true);
  assert.deepEqual(interrupted.nextState.pendingResume, {
    direction: "BROAD_RISE",
    count: 1,
  });
  assert.equal(interrupted.nextState.pendingExitFailureCount, 0);

  const nextRise = makePair({
    primaryWindow: broadRiseWindow(),
    boundaryTime: BASE_TIME + 20_000,
  });
  const resumed = processMarketEpisodeLifecycle({
    ...nextRise,
    previousState: interrupted.nextState,
  });
  assert.equal(resumed.transitions.length, 0);
  assert.equal(resumed.nextState.interrupted, false);
  assert.equal(resumed.nextState.pendingResume, null);
});

test("10. strengthened via pace crossing after 2 confirmations", () => {
  // Start BROAD_RISE with MIXED pace (5 symbols positive, 5 negative)
  const mixedRise = broadRiseWindow({
    classificationPace: "MIXED",
    classificationAccelerationEvidence: ACCELERATION_EVIDENCE.MIXED,
    accelerations: [-1, -1, -1, -1, -1, 1, 1, 1, 1, 1],
  });
  const eval1 = makePair({ primaryWindow: mixedRise, boundaryTime: BASE_TIME });
  const step1 = processMarketEpisodeLifecycle({
    classification: eval1.classification,
    movement: eval1.movement,
    previousState: null,
  });
  const eval2 = makePair({ primaryWindow: mixedRise, boundaryTime: BASE_TIME + 5_000 });
  const step2 = processMarketEpisodeLifecycle({
    classification: eval2.classification,
    movement: eval2.movement,
    previousState: step1.nextState,
  });
  assert.equal(step2.nextState.activeEpisode?.confirmedPace, "MIXED");

  // Tick 1 of ACCELERATING pace (10 positive accelerations)
  const accelRise = broadRiseWindow({ accelerations: Array(10).fill(1) });
  const eval3 = makePair({ primaryWindow: accelRise, boundaryTime: BASE_TIME + 10_000 });
  const step3 = processMarketEpisodeLifecycle({
    classification: eval3.classification,
    movement: eval3.movement,
    previousState: step2.nextState,
  });
  assert.equal(step3.transitions.length, 0);
  assert.equal(step3.nextState.pendingStrengthen?.count, 1);
  assert.equal(step3.nextState.pendingStrengthen?.reason, "pace_accelerated");

  // Tick 2 of ACCELERATING pace => STRENGTHENED
  const eval4 = makePair({ primaryWindow: accelRise, boundaryTime: BASE_TIME + 15_000 });
  const step4 = processMarketEpisodeLifecycle({
    classification: eval4.classification,
    movement: eval4.movement,
    previousState: step3.nextState,
  });
  assert.equal(step4.transitions.length, 1);
  assert.equal(step4.transitions[0].transition, "STRENGTHENED");
  assert.equal(step4.transitions[0].transitionReason, "pace_accelerated");
  assert.equal(step4.nextState.activeEpisode?.confirmedPace, "ACCELERATING");
});

test("11. strengthened via material breadth crossing after 2 confirmations", () => {
  // Start BROAD_RISE with material breadth = 0.60 (< 0.70)
  const normalRise = broadRiseWindow({
    breadth: { materialRisingFraction: 0.6 },
  });
  const eval1 = makePair({ primaryWindow: normalRise, boundaryTime: BASE_TIME });
  const step1 = processMarketEpisodeLifecycle({
    classification: eval1.classification,
    movement: eval1.movement,
    previousState: null,
  });
  const eval2 = makePair({ primaryWindow: normalRise, boundaryTime: BASE_TIME + 5_000 });
  const step2 = processMarketEpisodeLifecycle({
    classification: eval2.classification,
    movement: eval2.movement,
    previousState: step1.nextState,
  });
  assert.equal(step2.nextState.activeEpisode?.highMaterialBreadth, false);

  // Tick 1 with material breadth = 0.75 (>= 0.70)
  const highRise = broadRiseWindow({
    breadth: { materialRisingFraction: 0.75 },
  });
  const eval3 = makePair({ primaryWindow: highRise, boundaryTime: BASE_TIME + 10_000 });
  const step3 = processMarketEpisodeLifecycle({
    classification: eval3.classification,
    movement: eval3.movement,
    previousState: step2.nextState,
  });
  assert.equal(step3.transitions.length, 0);
  assert.equal(step3.nextState.pendingStrengthen?.count, 1);
  assert.equal(step3.nextState.pendingStrengthen?.reason, "material_breadth_expanded");

  // Tick 2 with material breadth = 0.75 => STRENGTHENED
  const eval4 = makePair({ primaryWindow: highRise, boundaryTime: BASE_TIME + 15_000 });
  const step4 = processMarketEpisodeLifecycle({
    classification: eval4.classification,
    movement: eval4.movement,
    previousState: step3.nextState,
  });
  assert.equal(step4.transitions.length, 1);
  assert.equal(step4.transitions[0].transition, "STRENGTHENED");
  assert.equal(step4.transitions[0].transitionReason, "material_breadth_expanded");
  assert.equal(step4.nextState.activeEpisode?.highMaterialBreadth, true);
});

test("12. weakened via pace crossing after 2 confirmations", () => {
  // Start BROAD_RISE with ACCELERATING pace
  const accelRise = broadRiseWindow({ accelerations: Array(10).fill(1) });
  const eval1 = makePair({ primaryWindow: accelRise, boundaryTime: BASE_TIME });
  const step1 = processMarketEpisodeLifecycle({
    classification: eval1.classification,
    movement: eval1.movement,
    previousState: null,
  });
  const eval2 = makePair({ primaryWindow: accelRise, boundaryTime: BASE_TIME + 5_000 });
  const step2 = processMarketEpisodeLifecycle({
    classification: eval2.classification,
    movement: eval2.movement,
    previousState: step1.nextState,
  });
  assert.equal(step2.nextState.activeEpisode?.confirmedPace, "ACCELERATING");

  // Tick 1 with DECELERATING pace
  const decelRise = broadRiseWindow({
    classificationPace: "DECELERATING",
    classificationAccelerationEvidence: ACCELERATION_EVIDENCE.RISE_DECELERATING,
    accelerations: Array(10).fill(-1),
  });
  const eval3 = makePair({ primaryWindow: decelRise, boundaryTime: BASE_TIME + 10_000 });
  const step3 = processMarketEpisodeLifecycle({
    classification: eval3.classification,
    movement: eval3.movement,
    previousState: step2.nextState,
  });
  assert.equal(step3.transitions.length, 0);
  assert.equal(step3.nextState.pendingWeaken?.count, 1);
  assert.equal(step3.nextState.pendingWeaken?.reason, "pace_decelerated");

  // Tick 2 with DECELERATING pace => WEAKENED
  const eval4 = makePair({ primaryWindow: decelRise, boundaryTime: BASE_TIME + 15_000 });
  const step4 = processMarketEpisodeLifecycle({
    classification: eval4.classification,
    movement: eval4.movement,
    previousState: step3.nextState,
  });
  assert.equal(step4.transitions.length, 1);
  assert.equal(step4.transitions[0].transition, "WEAKENED");
  assert.equal(step4.transitions[0].transitionReason, "pace_decelerated");
  assert.equal(step4.nextState.activeEpisode?.confirmedPace, "DECELERATING");
});

test("13. weakened via material breadth crossing after 2 confirmations while continuation still holds", () => {
  // Start BROAD_RISE with material breadth = 0.60 (> 0.50)
  const normalRise = broadRiseWindow({
    breadth: { materialRisingFraction: 0.6 },
  });
  const eval1 = makePair({ primaryWindow: normalRise, boundaryTime: BASE_TIME });
  const step1 = processMarketEpisodeLifecycle({
    classification: eval1.classification,
    movement: eval1.movement,
    previousState: null,
  });
  const eval2 = makePair({ primaryWindow: normalRise, boundaryTime: BASE_TIME + 5_000 });
  const step2 = processMarketEpisodeLifecycle({
    classification: eval2.classification,
    movement: eval2.movement,
    previousState: step1.nextState,
  });
  assert.equal(step2.nextState.activeEpisode?.lowMaterialBreadth, false);

  // Tick 1: material breadth crosses downward to 0.45 (<= 0.50), continuation holds (risingFraction = 0.60, return > 0)
  const weakRise = windowFixture({
    breadth: { risingFraction: 0.6, materialRisingFraction: 0.45 },
    medianRawReturn: 0.05,
    medianNormalizedMovement: 0.5,
    accelerations: Array(10).fill(1),
  });
  const eval3 = makePair({ primaryWindow: weakRise, boundaryTime: BASE_TIME + 10_000 });
  const step3 = processMarketEpisodeLifecycle({
    classification: eval3.classification,
    movement: eval3.movement,
    previousState: step2.nextState,
  });
  assert.equal(step3.transitions.length, 0);
  assert.equal(step3.nextState.pendingWeaken?.count, 1);
  assert.equal(step3.nextState.pendingWeaken?.reason, "material_breadth_reduced");

  // Tick 2: material breadth still 0.45 => WEAKENED
  const eval4 = makePair({ primaryWindow: weakRise, boundaryTime: BASE_TIME + 15_000 });
  const step4 = processMarketEpisodeLifecycle({
    classification: eval4.classification,
    movement: eval4.movement,
    previousState: step3.nextState,
  });
  assert.equal(step4.transitions.length, 1);
  assert.equal(step4.transitions[0].transition, "WEAKENED");
  assert.equal(step4.transitions[0].transitionReason, "material_breadth_reduced");
  assert.equal(step4.nextState.activeEpisode?.lowMaterialBreadth, true);
});

test("14. strength events do not repeat while condition remains true", () => {
  // Start with material breadth = 0.60
  const normalRise = broadRiseWindow({ breadth: { materialRisingFraction: 0.6 } });
  const eval1 = makePair({ primaryWindow: normalRise, boundaryTime: BASE_TIME });
  const step1 = processMarketEpisodeLifecycle({
    classification: eval1.classification,
    movement: eval1.movement,
    previousState: null,
  });
  const eval2 = makePair({ primaryWindow: normalRise, boundaryTime: BASE_TIME + 5_000 });
  const step2 = processMarketEpisodeLifecycle({
    classification: eval2.classification,
    movement: eval2.movement,
    previousState: step1.nextState,
  });

  // Cross upward through 0.70
  const highRise = broadRiseWindow({ breadth: { materialRisingFraction: 0.75 } });
  const eval3 = makePair({ primaryWindow: highRise, boundaryTime: BASE_TIME + 10_000 });
  const step3 = processMarketEpisodeLifecycle({
    classification: eval3.classification,
    movement: eval3.movement,
    previousState: step2.nextState,
  });
  const eval4 = makePair({ primaryWindow: highRise, boundaryTime: BASE_TIME + 15_000 });
  const step4 = processMarketEpisodeLifecycle({
    classification: eval4.classification,
    movement: eval4.movement,
    previousState: step3.nextState,
  });
  assert.equal(step4.transitions.length, 1);
  assert.equal(step4.transitions[0].transition, "STRENGTHENED");

  // Subsequent evaluations staying at 0.75 / 0.80 do NOT emit STRENGTHENED
  const eval5 = makePair({ primaryWindow: highRise, boundaryTime: BASE_TIME + 20_000 });
  const step5 = processMarketEpisodeLifecycle({
    classification: eval5.classification,
    movement: eval5.movement,
    previousState: step4.nextState,
  });
  assert.equal(step5.transitions.length, 0);

  const eval6 = makePair({
    primaryWindow: broadRiseWindow({ breadth: { materialRisingFraction: 0.8 } }),
    boundaryTime: BASE_TIME + 25_000,
  });
  const step6 = processMarketEpisodeLifecycle({
    classification: eval6.classification,
    movement: eval6.movement,
    previousState: step5.nextState,
  });
  assert.equal(step6.transitions.length, 0);
});

test("15. universe/version change ends old episode", () => {
  // Start active episode in universe "top-usdm" version "2026-09-25"
  const eval1 = makePair({
    primaryWindow: broadRiseWindow(),
    boundaryTime: BASE_TIME,
    universeVersion: "2026-09-25",
  });
  const step1 = processMarketEpisodeLifecycle({
    classification: eval1.classification,
    movement: eval1.movement,
    previousState: null,
  });
  const eval2 = makePair({
    primaryWindow: broadRiseWindow(),
    boundaryTime: BASE_TIME + 5_000,
    universeVersion: "2026-09-25",
  });
  const step2 = processMarketEpisodeLifecycle({
    classification: eval2.classification,
    movement: eval2.movement,
    previousState: step1.nextState,
  });
  assert.notEqual(step2.nextState.activeEpisode, null);

  // Next evaluation has new universe version "2026-09-26"
  const eval3 = makePair({
    primaryWindow: broadRiseWindow(),
    boundaryTime: BASE_TIME + 10_000,
    universeVersion: "2026-09-26",
  });
  const step3 = processMarketEpisodeLifecycle({
    classification: eval3.classification,
    movement: eval3.movement,
    previousState: step2.nextState,
  });

  assert.equal(step3.transitions.length, 1);
  assert.equal(step3.transitions[0].transition, "ENDED");
  assert.equal(step3.transitions[0].transitionReason, "universe_version_changed");
  assert.equal(step3.nextState.activeEpisode, null);
  // Fresh candidate started on new universe
  assert.notEqual(step3.nextState.pendingCandidate, null);
  assert.equal(step3.nextState.pendingCandidate.count, 1);
  assert.equal(step3.nextState.universeVersion, "2026-09-26");
});

function assertEndedMatchesEpisodeScope(ended, oldEpisode) {
  assert.equal(ended.episodeId, oldEpisode.episodeId);
  assert.equal(ended.universeId, oldEpisode.universeId);
  assert.equal(ended.universeVersion, oldEpisode.universeVersion);
  assert.equal(ended.primaryWindowMinutes, oldEpisode.primaryWindowMinutes);
  assert.equal(ended.episodeAlgorithmVersion, oldEpisode.episodeAlgorithmVersion);
  assert.equal(ended.lifecycleConfigVersion, oldEpisode.lifecycleConfigVersion);
  assert.equal(ended.classifierAlgorithmVersion, oldEpisode.classifierAlgorithmVersion);
  assert.equal(ended.classifierConfigVersion, oldEpisode.classifierConfigVersion);
  assert.equal(ended.movementAlgorithmVersion, oldEpisode.movementAlgorithmVersion);
  assert.equal(ended.movementConfigVersion, oldEpisode.movementConfigVersion);
}

test("context-change ENDED event scopes to the old episode on universe version change", () => {
  const eval1 = makePair({
    primaryWindow: broadRiseWindow(),
    boundaryTime: BASE_TIME,
    universeVersion: "2026-09-25",
  });
  const step1 = processMarketEpisodeLifecycle({ ...eval1, previousState: null });
  const eval2 = makePair({
    primaryWindow: broadRiseWindow(),
    boundaryTime: BASE_TIME + 5_000,
    universeVersion: "2026-09-25",
  });
  const step2 = processMarketEpisodeLifecycle({ ...eval2, previousState: step1.nextState });
  const oldEpisode = step2.nextState.activeEpisode;
  assert.notEqual(oldEpisode, null);
  assert.equal(oldEpisode.universeVersion, "2026-09-25");

  // Next evaluation arrives under a NEW universe version.
  const eval3 = makePair({
    primaryWindow: broadRiseWindow(),
    boundaryTime: BASE_TIME + 10_000,
    universeVersion: "2026-09-26",
  });
  const step3 = processMarketEpisodeLifecycle({ ...eval3, previousState: step2.nextState });

  assert.equal(step3.transitions.length, 1);
  const ended = step3.transitions[0];
  assert.equal(ended.transition, "ENDED");
  assert.equal(ended.transitionReason, "universe_version_changed");
  // The ENDED event must describe the OLD episode, not the new universe/context.
  assertEndedMatchesEpisodeScope(ended, oldEpisode);
  assert.equal(ended.universeVersion, "2026-09-25");
  // The reducer still adopts the new context for subsequent evaluations.
  assert.equal(step3.nextState.universeVersion, "2026-09-26");
});

test("context-change ENDED event keeps the old lifecycle config version", () => {
  const configV1 = lifecycleConfig("market-episode-config-v1");
  const configV2 = lifecycleConfig("market-episode-config-v2");
  const eval1 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME });
  const step1 = processMarketEpisodeLifecycle({
    ...eval1,
    previousState: null,
    config: configV1,
  });
  const eval2 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME + 5_000 });
  const step2 = processMarketEpisodeLifecycle({
    ...eval2,
    previousState: step1.nextState,
    config: configV1,
  });
  const oldEpisode = step2.nextState.activeEpisode;
  assert.notEqual(oldEpisode, null);
  assert.equal(oldEpisode.lifecycleConfigVersion, "market-episode-config-v1");

  const eval3 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME + 10_000 });
  const step3 = processMarketEpisodeLifecycle({
    ...eval3,
    previousState: step2.nextState,
    config: configV2,
  });

  assert.equal(step3.transitions.length, 1);
  const ended = step3.transitions[0];
  assert.equal(ended.transition, "ENDED");
  assert.equal(ended.transitionReason, "lifecycle_config_version_changed");
  assertEndedMatchesEpisodeScope(ended, oldEpisode);
  assert.equal(ended.lifecycleConfigVersion, "market-episode-config-v1");
  assert.equal(step3.nextState.lifecycleConfigVersion, "market-episode-config-v2");

  // Episode identity must remain internally consistent with the old scope.
  assert.equal(
    ended.episodeId,
    computeEpisodeId({
      universeId: oldEpisode.universeId,
      universeVersion: oldEpisode.universeVersion,
      primaryWindowMinutes: oldEpisode.primaryWindowMinutes,
      direction: oldEpisode.direction,
      startBoundaryTime: oldEpisode.startBoundaryTime,
      movementAlgorithmVersion: oldEpisode.movementAlgorithmVersion,
      movementConfigVersion: oldEpisode.movementConfigVersion,
      classifierAlgorithmVersion: oldEpisode.classifierAlgorithmVersion,
      classifierConfigVersion: oldEpisode.classifierConfigVersion,
      episodeAlgorithmVersion: oldEpisode.episodeAlgorithmVersion,
      lifecycleConfigVersion: oldEpisode.lifecycleConfigVersion,
    }),
  );
});

test("context-change ENDED event scopes classifier/movement/episode algorithm changes", () => {
  const eval1 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME });
  const step1 = processMarketEpisodeLifecycle({ ...eval1, previousState: null });
  const eval2 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME + 5_000 });
  const step2 = processMarketEpisodeLifecycle({ ...eval2, previousState: step1.nextState });
  const oldEpisode = step2.nextState.activeEpisode;
  assert.notEqual(oldEpisode, null);

  const cases = [
    {
      reason: "classifier_algorithm_version_changed",
      mutate: (pair) => {
        pair.classification.algorithmVersion = "market-state-v2";
      },
    },
    {
      reason: "classifier_config_version_changed",
      mutate: (pair) => {
        pair.classification.configVersion = "market-state-config-v2";
      },
    },
    {
      reason: "movement_algorithm_version_changed",
      mutate: (pair) => {
        pair.classification.movementAlgorithmVersion = "market-movement-v2";
        pair.movement.algorithmVersion = "market-movement-v2";
      },
    },
    {
      reason: "movement_config_version_changed",
      mutate: (pair) => {
        pair.classification.movementConfigVersion = "market-movement-config-v2";
        pair.movement.configVersion = "market-movement-config-v2";
      },
    },
  ];

  let boundary = BASE_TIME + 5_000;
  for (const { reason, mutate } of cases) {
    boundary += 5_000;
    const pair = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: boundary });
    mutate(pair);
    const step = processMarketEpisodeLifecycle({ ...pair, previousState: step2.nextState });
    assert.equal(step.transitions[0]?.transitionReason, reason);
    assertEndedMatchesEpisodeScope(step.transitions[0], oldEpisode);
  }

  // Episode algorithm change is detected from the persisted episode scope.
  const algorithmChangedState = {
    ...step2.nextState,
    episodeAlgorithmVersion: "market-episode-old",
    activeEpisode: { ...oldEpisode, episodeAlgorithmVersion: "market-episode-old" },
  };
  const pair = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: boundary + 5_000 });
  const ended = processMarketEpisodeLifecycle({ ...pair, previousState: algorithmChangedState });
  assert.equal(ended.transitions[0].transitionReason, "episode_algorithm_version_changed");
  assertEndedMatchesEpisodeScope(ended.transitions[0], algorithmChangedState.activeEpisode);
  assert.equal(ended.transitions[0].episodeAlgorithmVersion, "market-episode-old");
});

test("lifecycle version changes reset pending context and end active episodes", () => {
  const first = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME });
  const pendingV1 = processMarketEpisodeLifecycle({
    ...first,
    previousState: null,
    config: lifecycleConfig("lifecycle-v1"),
  });
  const second = makePair({
    primaryWindow: broadRiseWindow(),
    boundaryTime: BASE_TIME + 5_000,
  });
  const resetToV2 = processMarketEpisodeLifecycle({
    ...second,
    previousState: pendingV1.nextState,
    config: lifecycleConfig("lifecycle-v2"),
  });
  assert.equal(resetToV2.transitions.length, 0);
  assert.equal(resetToV2.nextState.pendingCandidate?.count, 1);
  assert.equal(resetToV2.nextState.pendingCandidate?.startBoundaryTime, BASE_TIME + 5_000);

  const third = makePair({
    primaryWindow: broadRiseWindow(),
    boundaryTime: BASE_TIME + 10_000,
  });
  const activeV2 = processMarketEpisodeLifecycle({
    ...third,
    previousState: resetToV2.nextState,
    config: lifecycleConfig("lifecycle-v2"),
  });
  assert.equal(activeV2.transitions[0].transition, "STARTED");

  const algorithmChangedState = {
    ...activeV2.nextState,
    episodeAlgorithmVersion: "market-episode-old",
    activeEpisode: {
      ...activeV2.nextState.activeEpisode,
      episodeAlgorithmVersion: "market-episode-old",
    },
  };
  const fourth = makePair({
    primaryWindow: broadRiseWindow(),
    boundaryTime: BASE_TIME + 15_000,
  });
  const configEnded = processMarketEpisodeLifecycle({
    ...fourth,
    previousState: activeV2.nextState,
    config: lifecycleConfig("lifecycle-v3"),
  });
  assert.equal(configEnded.transitions[0].transition, "ENDED");
  assert.equal(configEnded.transitions[0].transitionReason, "lifecycle_config_version_changed");

  const ended = processMarketEpisodeLifecycle({
    ...fourth,
    previousState: algorithmChangedState,
    config: lifecycleConfig("lifecycle-v2"),
  });
  assert.equal(ended.transitions[0].transition, "ENDED");
  assert.equal(ended.transitions[0].transitionReason, "episode_algorithm_version_changed");
  assert.equal(ended.nextState.pendingCandidate?.count, 1);

  const idInput = {
    universeId: "top-usdm",
    universeVersion: "2026-09-25",
    primaryWindowMinutes: 5,
    direction: "BROAD_RISE",
    startBoundaryTime: BASE_TIME,
    movementAlgorithmVersion: "market-movement-v1",
    movementConfigVersion: "market-movement-config-v1",
    classifierAlgorithmVersion: "market-state-v1",
    classifierConfigVersion: "market-state-config-v1",
    episodeAlgorithmVersion: "market-episode-v1",
  };
  assert.notEqual(
    computeEpisodeId({ ...idInput, lifecycleConfigVersion: "lifecycle-v1" }),
    computeEpisodeId({ ...idInput, lifecycleConfigVersion: "lifecycle-v2" }),
  );
});

test("16. retrying the same evaluation does not duplicate transition identity", () => {
  const eval1 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME });
  const step1 = processMarketEpisodeLifecycle({
    classification: eval1.classification,
    movement: eval1.movement,
    previousState: null,
  });

  const eval2 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME + 5_000 });
  const runA = processMarketEpisodeLifecycle({
    classification: eval2.classification,
    movement: eval2.movement,
    previousState: step1.nextState,
  });

  const runB = processMarketEpisodeLifecycle({
    classification: eval2.classification,
    movement: eval2.movement,
    previousState: step1.nextState,
  });

  assert.equal(runA.transitions.length, 1);
  assert.equal(runB.transitions.length, 1);
  assert.equal(runA.transitions[0].eventId, runB.transitions[0].eventId);
  assert.equal(runA.transitions[0].episodeId, runB.transitions[0].episodeId);
});

test("17. operational event insertion is idempotent in database", async () => {
  const eval1 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME });
  const step1 = processMarketEpisodeLifecycle({
    classification: eval1.classification,
    movement: eval1.movement,
    previousState: null,
  });
  const eval2 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME + 5_000 });
  const step2 = processMarketEpisodeLifecycle({
    classification: eval2.classification,
    movement: eval2.movement,
    previousState: step1.nextState,
  });
  const event = step2.transitions[0];

  // Insert event via RPC
  const insert1 = await db.query(
    `SELECT public.append_market_movement_event(
      $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
      $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $28, $29, $30,
      $31, $32, $33, $34, $35, $36, $37
    ) AS status`,
    [
      event.eventId,
      event.episodeId,
      event.episodeAlgorithmVersion,
      event.lifecycleConfigVersion,
      event.transition,
      event.transitionReason,
      event.fromDirection,
      event.toDirection,
      new Date(event.episodeStartBoundaryTime).toISOString(),
      new Date(event.evaluationBoundaryTime).toISOString(),
      event.universeId,
      event.universeVersion,
      event.primaryWindowMinutes,
      event.provider,
      event.exchange,
      event.priceType,
      event.direction,
      event.pace,
      event.directionalBreadth,
      event.materialBreadth,
      event.medianRawReturn,
      event.medianNormalizedMovement,
      event.medianAcceleration,
      event.accelerationBreadth,
      event.dispersion,
      JSON.stringify(event.rvolSummary),
      JSON.stringify(event.outliers),
      JSON.stringify(event.supportingContracts),
      JSON.stringify(event.conflictingContracts),
      JSON.stringify(event.configuredUniverse),
      JSON.stringify(event.includedSymbols),
      JSON.stringify(event.excludedSymbols),
      JSON.stringify(event.windowsContext),
      event.classifierAlgorithmVersion,
      event.classifierConfigVersion,
      event.movementAlgorithmVersion,
      event.movementConfigVersion,
    ],
  );
  assert.equal(insert1.rows[0].status, "appended");

  // Re-insert exact same event
  const insert2 = await db.query(
    `SELECT public.append_market_movement_event(
      $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
      $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $28, $29, $30,
      $31, $32, $33, $34, $35, $36, $37
    ) AS status`,
    [
      event.eventId,
      event.episodeId,
      event.episodeAlgorithmVersion,
      event.lifecycleConfigVersion,
      event.transition,
      event.transitionReason,
      event.fromDirection,
      event.toDirection,
      new Date(event.episodeStartBoundaryTime).toISOString(),
      new Date(event.evaluationBoundaryTime).toISOString(),
      event.universeId,
      event.universeVersion,
      event.primaryWindowMinutes,
      event.provider,
      event.exchange,
      event.priceType,
      event.direction,
      event.pace,
      event.directionalBreadth,
      event.materialBreadth,
      event.medianRawReturn,
      event.medianNormalizedMovement,
      event.medianAcceleration,
      event.accelerationBreadth,
      event.dispersion,
      JSON.stringify(event.rvolSummary),
      JSON.stringify(event.outliers),
      JSON.stringify(event.supportingContracts),
      JSON.stringify(event.conflictingContracts),
      JSON.stringify(event.configuredUniverse),
      JSON.stringify(event.includedSymbols),
      JSON.stringify(event.excludedSymbols),
      JSON.stringify(event.windowsContext),
      event.classifierAlgorithmVersion,
      event.classifierConfigVersion,
      event.movementAlgorithmVersion,
      event.movementConfigVersion,
    ],
  );
  assert.equal(insert2.rows[0].status, "already_exists");

  // Verify only 1 row exists in database
  const countRes = await db.query(
    "SELECT count(*) FROM public.market_movement_events WHERE event_id = $1",
    [event.eventId],
  );
  assert.equal(Number(countRes.rows[0].count), 1);
});

test("base event schema stores unavailable acceleration breadth as NULL", async () => {
  const result = await db.query(
    `INSERT INTO public.market_movement_events (
      event_id, episode_id, episode_algorithm_version, lifecycle_config_version,
      transition, transition_reason, episode_start_boundary_time, evaluation_boundary_time,
      universe_id, universe_version, primary_window_minutes, provider, exchange, price_type,
      direction, pace, directional_breadth, material_breadth, acceleration_breadth,
      classifier_algorithm_version, classifier_config_version,
      movement_algorithm_version, movement_config_version
    ) VALUES (
      'event-null-acceleration', 'episode-null-acceleration',
      'market-episode-v1', 'market-episode-config-v1', 'STARTED',
      'confirmed_broad_entry', $1, $2, 'top-usdm', 'fixture', 5,
      'binance-usdm', 'binance', 'trade', 'BROAD_RISE', 'NOT_APPLICABLE',
      0.8, 0.6, NULL, 'market-state-classifier-v1',
      'market-state-classifier-config-v1', 'market-movement-v1',
      'market-movement-config-v1'
    ) RETURNING acceleration_breadth`,
    [new Date(BASE_TIME).toISOString(), new Date(BASE_TIME + 5_000).toISOString()],
  );
  assert.equal(result.rows[0].acceleration_breadth, null);
});

test("18. WARMING/UNAVAILABLE interrupts rather than pretending continuity", () => {
  // Start active episode
  const eval1 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME });
  const step1 = processMarketEpisodeLifecycle({
    classification: eval1.classification,
    movement: eval1.movement,
    previousState: null,
  });
  const eval2 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME + 5_000 });
  const step2 = processMarketEpisodeLifecycle({
    classification: eval2.classification,
    movement: eval2.movement,
    previousState: step1.nextState,
  });
  assert.equal(step2.nextState.interrupted, false);

  // WARMING evaluation arrives
  const warmingWindow = windowFixture({
    classificationDirection: "WARMING",
    classificationAccelerationEvidence: ACCELERATION_EVIDENCE.UNAVAILABLE,
    marketWideEligible: false,
    excludedSymbols: Array.from({ length: 10 }, (_, i) => ({
      symbol: `S${i}USDT`,
      reasons: ["WARMING_INSUFFICIENT_LIVE_HISTORY"],
    })),
  });
  const eval3 = makePair({ primaryWindow: warmingWindow, boundaryTime: BASE_TIME + 10_000 });
  const step3 = processMarketEpisodeLifecycle({
    classification: eval3.classification,
    movement: eval3.movement,
    previousState: step2.nextState,
  });

  assert.equal(step3.transitions.length, 0); // NOT ended!
  assert.equal(step3.nextState.interrupted, true);
  assert.notEqual(step3.nextState.activeEpisode, null);
  assert.equal(step3.shouldPersistCurrentImmediately, true);

  // UNAVAILABLE arrives next
  const unavailWindow = windowFixture({
    classificationDirection: "UNAVAILABLE",
    classificationAccelerationEvidence: ACCELERATION_EVIDENCE.UNAVAILABLE,
    marketWideEligible: false,
    excludedSymbols: [{ symbol: "S0USDT", reasons: ["SOURCE_UNAVAILABLE"] }],
  });
  const eval4 = makePair({ primaryWindow: unavailWindow, boundaryTime: BASE_TIME + 15_000 });
  const step4 = processMarketEpisodeLifecycle({
    classification: eval4.classification,
    movement: eval4.movement,
    previousState: step3.nextState,
  });
  assert.equal(step4.transitions.length, 0);
  assert.equal(step4.nextState.interrupted, true);
  assert.notEqual(step4.nextState.activeEpisode, null);
});

test("19. restart from persisted active state requires fresh confirmation", () => {
  // Create an active episode state as if loaded from DB
  const eval1 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME });
  const step1 = processMarketEpisodeLifecycle({
    classification: eval1.classification,
    movement: eval1.movement,
    previousState: null,
  });
  const eval2 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME + 5_000 });
  const step2 = processMarketEpisodeLifecycle({
    classification: eval2.classification,
    movement: eval2.movement,
    previousState: step1.nextState,
  });

  // Simulate restart
  const restartedState = restoreLifecycleStateOnRestart(step2.nextState);
  assert.equal(restartedState.interrupted, true);
  assert.equal(restartedState.pendingCandidate, null);

  // Evaluation 1 after restart: BROAD_RISE
  const eval3 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME + 20_000 });
  const step3 = processMarketEpisodeLifecycle({
    classification: eval3.classification,
    movement: eval3.movement,
    previousState: restartedState,
  });
  assert.equal(step3.transitions.length, 0);
  // Still interrupted after 1 tick
  assert.equal(step3.nextState.interrupted, true);
  assert.equal(step3.nextState.pendingResume?.count, 1);

  // Evaluation 2 after restart: BROAD_RISE again => confirmed resumption!
  const eval4 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME + 25_000 });
  const step4 = processMarketEpisodeLifecycle({
    classification: eval4.classification,
    movement: eval4.movement,
    previousState: step3.nextState,
  });
  assert.equal(step4.transitions.length, 0);
  // Fresh confirmation complete, interrupted cleared!
  assert.equal(step4.nextState.interrupted, false);
  assert.equal(step4.nextState.pendingResume, null);
});

test("persistence acknowledgement and lifecycle serialization are explicit and lossless", () => {
  const eval1 = makePair({ primaryWindow: broadRiseWindow(), boundaryTime: BASE_TIME });
  const pending = processMarketEpisodeLifecycle({ ...eval1, previousState: null });
  assert.equal(pending.shouldPersistCurrentImmediately, true);
  assert.equal(pending.nextState.lastPersistedTime, undefined);

  const eval2 = makePair({
    primaryWindow: broadRiseWindow(),
    boundaryTime: BASE_TIME + 5_000,
  });
  const active = processMarketEpisodeLifecycle({
    ...eval2,
    previousState: pending.nextState,
  });
  const completeState = {
    ...active.nextState,
    pendingCandidate: { direction: "BROAD_RISE", startBoundaryTime: BASE_TIME, count: 1 },
    pendingExitFailureCount: 2,
    pendingReversal: {
      toDirection: "BROAD_DROP",
      startBoundaryTime: BASE_TIME + 10_000,
      count: 1,
    },
    pendingStrengthen: { reason: "pace_accelerated", count: 1 },
    pendingWeaken: { reason: "material_breadth_reduced", count: 1 },
    pendingResume: { direction: "BROAD_RISE", count: 1 },
  };
  const acknowledged = markMarketEpisodeStatePersisted(completeState);
  assert.equal(acknowledged.lastPersistedTime, completeState.evaluationBoundaryTime);
  assert.equal(completeState.lastPersistedTime, undefined);

  const serialized = serializeMarketEpisodeLifecycleState(acknowledged);
  assert.equal(serialized.serializationVersion, "market-episode-state-v1");
  assert.deepEqual(deserializeMarketEpisodeLifecycleState(serialized), acknowledged);
});

test("20. supporting/conflicting contract lists are exact", () => {
  const symbols = [
    makeSymbol(0, { direction: "RISING", included: true }),
    makeSymbol(1, { direction: "FALLING", included: true }),
    makeSymbol(2, { direction: "FLAT", included: true }),
    makeSymbol(3, { direction: "RISING", included: false }), // excluded
    makeSymbol(4, { direction: "RISING", included: true }),
  ];
  const win = windowFixture({ symbols, configuredCount: 5 });

  // For BROAD_RISE: RISING is supporting, FALLING is conflicting
  const riseContracts = extractSupportingAndConflictingSymbols(win, "BROAD_RISE");
  assert.deepEqual(riseContracts.supportingContracts, ["S0USDT", "S4USDT"]);
  assert.deepEqual(riseContracts.conflictingContracts, ["S1USDT"]);

  // For BROAD_DROP: FALLING is supporting, RISING is conflicting
  const dropContracts = extractSupportingAndConflictingSymbols(win, "BROAD_DROP");
  assert.deepEqual(dropContracts.supportingContracts, ["S1USDT"]);
  assert.deepEqual(dropContracts.conflictingContracts, ["S0USDT", "S4USDT"]);
});

test("21. market_state_current upsert is bounded to one row and updates on conflict", async () => {
  const boundaryTime1 = new Date(BASE_TIME).toISOString();
  const boundaryTime2 = new Date(BASE_TIME + 30_000).toISOString();

  // First upsert
  await db.query(
    `SELECT public.upsert_market_state_current(
      $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
      $17, $18, $19, $20
    )`,
    [
      "top-usdm",
      5,
      "2026-09-25",
      "binance-usdm",
      "binance",
      "trade",
      boundaryTime1,
      "BROAD_RISE",
      "ACCELERATING",
      "mep_test_1",
      "BROAD_RISE",
      false,
      "market-episode-v1",
      "market-episode-config-v1",
      "market-state-v1",
      "market-state-config-v1",
      "market-movement-v1",
      "market-movement-config-v1",
      JSON.stringify({ serializationVersion: "market-episode-state-v1" }),
      JSON.stringify({ risingFraction: 0.8 }),
    ],
  );

  let res = await db.query(
    "SELECT * FROM public.market_state_current WHERE universe_id = $1 AND primary_window_minutes = $2",
    ["top-usdm", 5],
  );
  assert.equal(res.rows.length, 1);
  assert.equal(res.rows[0].direction_state, "BROAD_RISE");
  assert.equal(res.rows[0].active_episode_id, "mep_test_1");

  // Second upsert for same universe and window updates the single bounded row
  await db.query(
    `SELECT public.upsert_market_state_current(
      $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
      $17, $18, $19, $20
    )`,
    [
      "top-usdm",
      5,
      "2026-09-25",
      "binance-usdm",
      "binance",
      "trade",
      boundaryTime2,
      "NEUTRAL",
      "NOT_APPLICABLE",
      null,
      null,
      false,
      "market-episode-v1",
      "market-episode-config-v1",
      "market-state-v1",
      "market-state-config-v1",
      "market-movement-v1",
      "market-movement-config-v1",
      JSON.stringify({ serializationVersion: "market-episode-state-v1" }),
      JSON.stringify({ risingFraction: 0.4 }),
    ],
  );

  res = await db.query(
    "SELECT * FROM public.market_state_current WHERE universe_id = $1 AND primary_window_minutes = $2",
    ["top-usdm", 5],
  );
  assert.equal(res.rows.length, 1);
  assert.equal(res.rows[0].direction_state, "NEUTRAL");
  assert.equal(res.rows[0].active_episode_id, null);
  assert.equal(res.rows[0].episode_algorithm_version, "market-episode-v1");
  assert.equal(res.rows[0].lifecycle_config_version, "market-episode-config-v1");
  assert.equal(res.rows[0].lifecycle_state.serializationVersion, "market-episode-state-v1");
});

function upsertCurrentRow({
  universeId = "monotonic-usdm",
  boundaryTime,
  directionState = "BROAD_RISE",
  pace = "ACCELERATING",
  activeEpisodeId = "mep_monotonic",
  activeDirection = "BROAD_RISE",
  evidence = { risingFraction: 0.8 },
}) {
  return db.query(
    `SELECT public.upsert_market_state_current(
      $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
      $17, $18, $19, $20
    )`,
    [
      universeId,
      5,
      "2026-09-25",
      "binance-usdm",
      "binance",
      "trade",
      new Date(boundaryTime).toISOString(),
      directionState,
      pace,
      activeEpisodeId,
      activeDirection,
      false,
      "market-episode-v1",
      "market-episode-config-v1",
      "market-state-v1",
      "market-state-config-v1",
      "market-movement-v1",
      "market-movement-config-v1",
      JSON.stringify({ serializationVersion: "market-episode-state-v1" }),
      JSON.stringify(evidence),
    ],
  );
}

test("market_state_current upsert is monotonic and rejects stale boundaries", async () => {
  const newerBoundary = BASE_TIME + 30_000;
  const olderBoundary = BASE_TIME;
  const readRow = () =>
    db.query(
      "SELECT * FROM public.market_state_current WHERE universe_id = $1 AND primary_window_minutes = $2",
      ["monotonic-usdm", 5],
    );

  // Newer snapshot is persisted first.
  await upsertCurrentRow({
    boundaryTime: newerBoundary,
    directionState: "BROAD_RISE",
    activeEpisodeId: "mep_newer",
    activeDirection: "BROAD_RISE",
    evidence: { risingFraction: 0.9 },
  });

  // A delayed/retried write for an OLDER boundary must not regress current state.
  await upsertCurrentRow({
    boundaryTime: olderBoundary,
    directionState: "NEUTRAL",
    pace: "NOT_APPLICABLE",
    activeEpisodeId: null,
    activeDirection: null,
    evidence: { risingFraction: 0.1 },
  });

  let res = await readRow();
  assert.equal(res.rows.length, 1);
  assert.equal(new Date(res.rows[0].evaluation_boundary_time).getTime(), newerBoundary);
  assert.equal(res.rows[0].direction_state, "BROAD_RISE");
  assert.equal(res.rows[0].active_episode_id, "mep_newer");
  assert.equal(res.rows[0].current_evidence.risingFraction, 0.9);

  // Equal-boundary retry is still allowed to write (idempotent retry path).
  await upsertCurrentRow({
    boundaryTime: newerBoundary,
    directionState: "BROAD_RISE",
    pace: "MIXED",
    activeEpisodeId: "mep_newer",
    activeDirection: "BROAD_RISE",
    evidence: { risingFraction: 0.9 },
  });
  res = await readRow();
  assert.equal(res.rows.length, 1);
  assert.equal(new Date(res.rows[0].evaluation_boundary_time).getTime(), newerBoundary);
  assert.equal(res.rows[0].pace, "MIXED");

  // A strictly newer boundary updates normally.
  await upsertCurrentRow({
    boundaryTime: newerBoundary + 5_000,
    directionState: "BROAD_DROP",
    activeEpisodeId: "mep_newest",
    activeDirection: "BROAD_DROP",
    evidence: { fallingFraction: 0.9 },
  });
  res = await readRow();
  assert.equal(res.rows.length, 1);
  assert.equal(new Date(res.rows[0].evaluation_boundary_time).getTime(), newerBoundary + 5_000);
  assert.equal(res.rows[0].direction_state, "BROAD_DROP");
  assert.equal(res.rows[0].active_episode_id, "mep_newest");
  assert.equal(res.rows[0].current_evidence.fallingFraction, 0.9);
});

test("atomic lifecycle persistence appends events and upserts current state in one transaction", async () => {
  const eval1 = makePair({
    primaryWindow: broadRiseWindow(),
    boundaryTime: BASE_TIME + 100_000,
    universeId: "atomic-usdm",
  });
  const step1 = processMarketEpisodeLifecycle({ ...eval1, previousState: null });
  const eval2 = makePair({
    primaryWindow: broadRiseWindow(),
    boundaryTime: BASE_TIME + 105_000,
    universeId: "atomic-usdm",
  });
  const step2 = processMarketEpisodeLifecycle({ ...eval2, previousState: step1.nextState });
  const current = persistedCurrent(step2);
  const event = step2.transitions[0];

  const first = await db.query(
    "SELECT public.persist_market_episode_lifecycle_step($1, $2) AS statuses",
    [JSON.stringify(current), JSON.stringify([event])],
  );
  assert.deepEqual(first.rows[0].statuses, [{ eventId: event.eventId, status: "appended" }]);

  const retry = await db.query(
    "SELECT public.persist_market_episode_lifecycle_step($1, $2) AS statuses",
    [JSON.stringify(current), JSON.stringify([event])],
  );
  assert.deepEqual(retry.rows[0].statuses, [{ eventId: event.eventId, status: "already_exists" }]);
  assert.equal(
    Number(
      (
        await db.query("SELECT count(*) FROM market_movement_events WHERE event_id = $1", [
          event.eventId,
        ])
      ).rows[0].count,
    ),
    1,
  );

  const currentRow = (
    await db.query("SELECT * FROM market_state_current WHERE universe_id = 'atomic-usdm'")
  ).rows[0];
  assert.equal(currentRow.active_episode_id, event.episodeId);
  assert.equal(currentRow.lifecycle_state.activeEpisode.episodeId, event.episodeId);
  assert.equal(currentRow.lifecycle_state.lastPersistedTime, current.evaluationBoundaryTime);

  const rollbackEvent = { ...event, eventId: "mevt_atomic_rollback" };
  await assert.rejects(
    db.query("SELECT public.persist_market_episode_lifecycle_step($1, $2)", [
      JSON.stringify({ ...current, primaryWindowMinutes: 1 }),
      JSON.stringify([rollbackEvent]),
    ]),
    /primary_window_minutes/,
  );
  assert.equal(
    Number(
      (
        await db.query("SELECT count(*) FROM market_movement_events WHERE event_id = $1", [
          rollbackEvent.eventId,
        ])
      ).rows[0].count,
    ),
    0,
  );
});

test("22. RLS blocks anon and authenticated roles from reading or writing market tables", async () => {
  await db.exec("SET ROLE anon;");
  await assert.rejects(db.query("SELECT * FROM public.market_state_current"), /permission denied/);
  await assert.rejects(
    db.query("SELECT * FROM public.market_movement_events"),
    /permission denied/,
  );

  await db.exec("SET ROLE authenticated;");
  await assert.rejects(db.query("SELECT * FROM public.market_state_current"), /permission denied/);
  await assert.rejects(
    db.query("SELECT * FROM public.market_movement_events"),
    /permission denied/,
  );

  await db.exec("RESET ROLE;");
});
