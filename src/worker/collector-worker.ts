import { startCollectorWorker } from "../lib/market/collector-worker.server";
import { startMarketQuotePublisher } from "../lib/market/quote-publisher.server";
import { getOperationalStore } from "../lib/operational/repository.server";

// Runnable entrypoint for the persistent collector worker. `npm run collector:worker` and the
// worker host (`npm run up`) load this through Vite for local development, and the built artifact
// (`npm run collector:worker:build`) runs it with plain `node` in production.
try {
  const runtime = startCollectorWorker();
  console.log(
    runtime
      ? "[collector-worker] collector started; awaiting termination signal."
      : "[collector-worker] BINANCE_COLLECTOR_ENABLED is not true; no collector to run.",
  );
  if (runtime) {
    // The quote publisher (#323) shares this process, so the same supervisor restarts both. It is
    // started in its own try/catch and handles its own errors: it can never stop the collector.
    try {
      const publisher = startMarketQuotePublisher({ store: getOperationalStore() });
      for (const signal of ["SIGTERM", "SIGINT"] as const) {
        process.once(signal, () => publisher?.stop());
      }
    } catch (error) {
      console.error(
        `[market-quote-publisher] not started: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  }
} catch (error) {
  console.error(`[collector-worker] ${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
}
