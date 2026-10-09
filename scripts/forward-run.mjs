import { resolve } from "node:path";
import { loadEnv, runnerImport } from "vite";

// Local launcher for the forward-test job (#239). Same style as collector-worker.mjs: it loads
// the TypeScript entrypoint through Vite's SSR module runner (local MVP only; production
// scheduling is listed in docs/production-todo.md). Pass --once for a single evaluation.
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
  await runnerImport("/src/worker/forward-run.ts", { root, mode, configFile: false, envDir: false });
}

try {
  await main();
} catch (error) {
  console.error(`[forward-run] ${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
}
