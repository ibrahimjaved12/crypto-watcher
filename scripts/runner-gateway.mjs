import { resolve } from "node:path";
import { loadEnv, runnerImport } from "vite";

// Load the owner-machine gateway through the same runner as the collector.
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

  // Shared monitoring code logs upstream errors for the app. This dedicated process
  // must never emit those details. Keep only the gateway's fixed category/route pairs
  // and its port-only startup announcement; do not stringify other arguments.
  const writeLog = console.log.bind(console);
  const writeError = console.error.bind(console);
  const categories = new Set(["unauthorized", "bad_request", "rate_limited", "busy", "internal"]);
  const routes = new Set(["/health", "/v1/analysis", "/v1/monitor/check", "unknown"]);
  const safeLog = (...args) => {
    if (args.length === 2 && categories.has(args[0]) && routes.has(args[1])) {
      writeError(args[0], args[1]);
    } else if (args.length === 1 && typeof args[0] === "string" &&
      /^Runner gateway listening on port [0-9]{4,5}$/.test(args[0])) {
      writeLog(args[0]);
    } else {
      writeError("internal", "unknown");
    }
  };
  for (const method of ["log", "info", "warn", "error", "debug", "trace"]) console[method] = safeLog;

  await runnerImport("/src/worker/runner-gateway.ts", {
    root,
    mode,
    configFile: false,
    envDir: false,
    resolve: { alias: { "@": resolve(root, "src") } },
  });
}

try {
  await main();
} catch {
  console.error("internal startup");
  process.exit(1);
}
