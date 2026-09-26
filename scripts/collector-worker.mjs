import { resolve } from "node:path";
import { loadEnv, runnerImport } from "vite";

// Local-development launcher for the persistent collector worker. It loads the
// TypeScript entrypoint through Vite's SSR module runner, so Vite (a
// devDependency) is only needed here. Production runs the prebuilt
// `dist/collector-worker/collector-worker.mjs` artifact with plain `node`.
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

  await runnerImport("/src/worker/collector-worker.ts", {
    root,
    mode,
    configFile: false,
    envDir: false,
  });
}

try {
  await main();
} catch (error) {
  console.error(`[collector-worker] ${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
}
