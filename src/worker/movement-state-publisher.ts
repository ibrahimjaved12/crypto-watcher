import { getOperationalStore } from "../lib/operational/repository.server";
import { MARKET_UNIVERSE_ID, MOVEMENT_PUBLISH_INTERVAL_MS } from "../lib/market/market-movement-state";
import {
  hostSender,
  movementPublisherConfig,
  newPublisherState,
  newSlowPublisherState,
  publishMovementOnce,
  publishSlowOnce,
  slowMovementEnabled,
} from "../lib/market/movement-publisher.server";
import { computeSlowMovement, evaluatePythonSlowMovement } from "../lib/market/slow-movement.server";

// Worker-host child (#30): publishes the movement engine's persisted state to the hosted copy through the site
// hook so the deployed site can show the market strip. Best effort: failures are logged, never fatal.
// Set MOVEMENT_STATE_PUBLISH_ENABLED=false to turn it off.
const store = getOperationalStore();
const config = movementPublisherConfig();
if (!store.enabled || !config) {
  console.log("Movement state publisher: needs the operational database and MOVEMENT_STATE_HOSTED_URL (or ALERT_CHART_HOSTED_URL) with RUNNER_GATEWAY_SECRET; not publishing.");
  process.exit(0);
}
const deps = { read: () => store.getMarketStateCurrent(MARKET_UNIVERSE_ID), send: hostSender(config) };
const state = newPublisherState();
let lastLogged = "";

async function tick() {
  const outcome = await publishMovementOnce(deps, state);
  const line = outcome.kind === "failed" ? `failed: ${outcome.error}` : outcome.kind;
  // Log changes only, so a healthy minute-by-minute heartbeat stays quiet.
  if (outcome.kind !== "too_soon" && (outcome.kind === "failed" || line !== lastLogged)) {
    console.log(`[movement-state-publisher] ${line}`);
  }
  lastLogged = line;
}

// "Longer moves" (slow-movement-v1, #30 / #373): once per 15-minute boundary, 20 s after it, up to 3 attempts
// 30 s apart. Its own loop and state, so a slow failure never touches the V1 publish above. SLOW_MOVEMENT_ENABLED=false
// turns it off. The candle RPC keeps candles with open + 1 min < its bound, so B + 1 ms reads every candle that
// closed at or before B; buildSlowGrid applies the exact openTime + 60 s <= B rule.
const slowDeps = {
  compute: (boundaryMs: number) => computeSlowMovement({
    readUniverse: async () => (await store.getMarketStateCurrent(MARKET_UNIVERSE_ID))?.currentEvidence.universe.includedSymbols ?? [],
    readCandles: async (symbol, sinceMs, boundary) =>
      (await store.readMovementCandleHistory([symbol], sinceMs, boundary + 1)).get(symbol) ?? [],
    evaluate: (request) => evaluatePythonSlowMovement(request),
    now: Date.now,
  }, boundaryMs),
  send: hostSender(config),
};
const slowState = newSlowPublisherState();
let slowRunning = false;
async function slowTick() {
  if (slowRunning) return;
  slowRunning = true;
  try {
    const outcome = await publishSlowOnce(slowDeps, slowState);
    if (outcome.kind === "failed") {
      console.log(`[movement-state-publisher] longer moves for ${new Date(outcome.boundary).toISOString()} failed after 3 attempts: ${outcome.error}`);
    }
  } finally {
    slowRunning = false;
  }
}

void tick();
const timer = setInterval(() => void tick(), MOVEMENT_PUBLISH_INTERVAL_MS);
const slowTimer = slowMovementEnabled() ? setInterval(() => void slowTick(), 5_000) : null;
for (const signal of ["SIGTERM", "SIGINT", "SIGHUP"] as const) {
  process.once(signal, () => {
    clearInterval(timer);
    if (slowTimer) clearInterval(slowTimer);
    process.exit(0);
  });
}
console.log(`[movement-state-publisher] publishing to ${config.origin} every ${MOVEMENT_PUBLISH_INTERVAL_MS / 1000} s`);
