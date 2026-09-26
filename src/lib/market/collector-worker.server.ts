import { startBinanceCollector, type CollectorRuntime } from "./collector.server";

export type CollectorWorkerOptions = {
  /** Overrides the shared collector startup path. Used by tests. */
  start?: () => CollectorRuntime | null;
  /** Registers the graceful-shutdown callback. Defaults to SIGTERM/SIGINT. */
  registerSignals?: (shutdown: (code: number) => void) => void;
  /** Terminates the process after shutdown. Defaults to `process.exit`. */
  exit?: (code: number) => void;
};

function registerProcessSignals(shutdown: (code: number) => void): void {
  process.once("SIGTERM", () => shutdown(0));
  process.once("SIGINT", () => shutdown(0));
}

/**
 * Runs the shared Binance collector as an independently runnable persistent worker.
 *
 * It reuses the existing collector lifecycle (`startBinanceCollector`), returns the
 * running runtime handle, or null when `BINANCE_COLLECTOR_ENABLED` is not true. On a
 * termination signal it stops the runtime — which releases the existing collector
 * lease — before exiting.
 */
export function startCollectorWorker(
  options: CollectorWorkerOptions = {},
): CollectorRuntime | null {
  const runtime = (options.start ?? startBinanceCollector)();
  if (!runtime) return null;

  const exit = options.exit ?? ((code: number) => process.exit(code));
  let shuttingDown = false;
  const shutdown = (code: number): void => {
    if (shuttingDown) return;
    shuttingDown = true;
    void (async () => {
      let finalCode = code;
      try {
        await runtime.stop();
      } catch (error) {
        console.error(
          `[binance-collector] shutdown failed: ${error instanceof Error ? error.message : String(error)}`,
        );
        finalCode = 1;
      }
      exit(finalCode);
    })();
  };

  (options.registerSignals ?? registerProcessSignals)(shutdown);
  return runtime;
}
