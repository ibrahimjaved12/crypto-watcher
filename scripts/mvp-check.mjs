import { resolve } from "node:path";
import { loadEnv, runnerImport } from "vite";

// Launcher for `npm run mvp:check` (#239 P21): a READ-ONLY health table for the local MVP. Same style as
// forward-run.mjs: it loads src/worker/mvp-check.ts through Vite's SSR module runner. Pass --json for
// machine output; the exit code is 1 when any check FAILs.
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
  await runnerImport("/src/worker/mvp-check.ts", { root, mode, configFile: false, envDir: false });
}

try {
  await main();
} catch (error) {
  console.error(`[mvp-check] ${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
}
