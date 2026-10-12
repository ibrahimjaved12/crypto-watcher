import { startCollectorWorker } from "../lib/market/collector-worker.server";

// Runnable entrypoint for the persistent collector worker. `npm run collector:worker`
// loads this through Vite for local development, and the built artifact
// (`npm run collector:worker:build`) runs it with plain `node` in production.
try {
  const runtime = startCollectorWorker();
  console.log(
    runtime
      ? "[collector-worker] collector started; awaiting termination signal."
      : "[collector-worker] BINANCE_COLLECTOR_ENABLED is not true; no collector to run.",
  );
} catch (error) {
  console.error(`[collector-worker] ${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
}
