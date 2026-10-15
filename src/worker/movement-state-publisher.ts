import { getOperationalStore } from "../lib/operational/repository.server";
import { MARKET_UNIVERSE_ID, MOVEMENT_PUBLISH_INTERVAL_MS } from "../lib/market/market-movement-state";
import {
  hostSender,
  movementPublisherConfig,
  newPublisherState,
  publishMovementOnce,
} from "../lib/market/movement-publisher.server";

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

void tick();
const timer = setInterval(() => void tick(), MOVEMENT_PUBLISH_INTERVAL_MS);
for (const signal of ["SIGTERM", "SIGINT", "SIGHUP"] as const) {
  process.once(signal, () => {
    clearInterval(timer);
    process.exit(0);
  });
}
console.log(`[movement-state-publisher] publishing to ${config.origin} every ${MOVEMENT_PUBLISH_INTERVAL_MS / 1000} s`);
