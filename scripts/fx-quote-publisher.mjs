import { resolve } from "node:path";
import { loadEnv, runnerImport } from "vite";

// Launcher for the forex quote publisher (#433). It runs as a non-essential child of the worker host
// and loads src/worker/fx-quote-publisher.ts through Vite's SSR module runner like the other worker
// launchers. It reads the local operational database and writes the hosted fx_market_quotes.
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
  await runnerImport("/src/worker/fx-quote-publisher.ts", {
    root,
    mode,
    configFile: false,
    envDir: false,
  });
}

try {
  await main();
} catch (error) {
  console.error(`[fx-quote] ${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
}
