import { resolve } from "node:path";
import { loadEnv, runnerImport } from "vite";

// Launcher for the hosted market quote publisher (#323). It runs as its own child of the worker host,
// beside the collector, and loads src/worker/market-quote-publisher.ts through Vite's SSR module runner
// like the collector launcher does. It is separate from the collector because it writes to the main
// hosted project with the service-role key, which the collector artifact must never contain.
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
  const env = { ...loadEnv(mode, root, ""), ...process.env };
  for (const [name, value] of Object.entries(env)) {
    if (value !== undefined && process.env[name] === undefined) process.env[name] = value;
  }
  await runnerImport("/src/worker/market-quote-publisher.ts", {
    root,
    mode,
    configFile: false,
    envDir: false,
  });
}

try {
  await main();
} catch (error) {
  console.error(`[market-quote-publisher] ${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
}
