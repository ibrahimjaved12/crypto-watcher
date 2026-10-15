import { resolve } from "node:path";
import { loadEnv, runnerImport } from "vite";

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
  await runnerImport("/src/worker/movement-state-publisher.ts", {
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
  console.error("Movement state publisher could not start. Check the host configuration.");
  process.exit(1);
}
