import { resolve } from "node:path";
import { loadEnv, runnerImport } from "vite";

// The Binance collector runs as its own persistent process, independently of the
// TanStack application server. This launcher loads the local environment, then
// imports the TypeScript worker entrypoint through Vite's SSR module runner.
const root = resolve(import.meta.dirname, "..");

function modeFromArgs() {
  const index = process.argv.indexOf("--mode");
  if (index !== -1) {
    const value = process.argv[index + 1];
    if (!value) throw new Error("--mode requires a value, for example --mode production");
    return value;
  }
  return process.env.NODE_ENV === "production" ? "production" : "development";
}

async function main() {
  const mode = modeFromArgs();

  // runnerImport ignores vite.config.ts and its env dir, so load the environment
  // here and make it available to process.env and import.meta.env.
  const env = { ...loadEnv(mode, root, ""), ...process.env };
  for (const [name, value] of Object.entries(env)) {
    if (value !== undefined && process.env[name] === undefined) process.env[name] = value;
  }

  const { module } = await runnerImport("/src/lib/market/collector-worker.server.ts", {
    root,
    mode,
    configFile: false,
    envDir: false,
  });

  const runtime = module.startCollectorWorker();
  if (!runtime) {
    console.log("[collector-worker] BINANCE_COLLECTOR_ENABLED is not true; no collector to run.");
    return;
  }
  console.log("[collector-worker] collector started; awaiting termination signal.");
}

try {
  await main();
} catch (error) {
  console.error(`[collector-worker] ${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
}
