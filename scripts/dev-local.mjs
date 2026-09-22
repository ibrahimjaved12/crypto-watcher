import { spawn, spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { loadEnvFile } from "node:process";

const root = resolve(import.meta.dirname, "..");
const children = new Map();
let shuttingDown = false;

function run(command, args, failure, options = {}) {
  const result = spawnSync(command, args, {
    cwd: root,
    stdio: options.quiet ? ["ignore", "pipe", "inherit"] : "inherit",
  });
  if (result.error) throw new Error(`${failure}: ${result.error.message}`);
  if (result.status !== 0) throw new Error(failure);
}

function start(name, command, args, options = {}) {
  const child = spawn(command, args, {
    cwd: options.cwd ?? root,
    env: options.env ?? process.env,
    stdio: "inherit",
    detached: process.platform !== "win32",
  });
  children.set(name, child);
  child.once("exit", (code, signal) => {
    children.delete(name);
    if (!shuttingDown) {
      const reason = signal ? `signal ${signal}` : `exit code ${code ?? 1}`;
      console.error(`[local-dev] ${name} stopped with ${reason}.`);
      void shutdown(code || 1);
    }
  });
  child.once("error", (error) => {
    children.delete(name);
    if (!shuttingDown) {
      console.error(`[local-dev] Could not start ${name}: ${error.message}`);
      void shutdown(1);
    }
  });
  return child;
}

function stop(child, signal) {
  if (!child.pid) return;
  try {
    if (process.platform === "win32") child.kill(signal);
    else process.kill(-child.pid, signal);
  } catch (error) {
    if (error.code !== "ESRCH") throw error;
  }
}

async function shutdown(code = 0) {
  if (shuttingDown) return;
  shuttingDown = true;
  for (const child of children.values()) stop(child, "SIGTERM");

  const waitForExit = [...children.values()].map(
    (child) => new Promise((done) => child.once("exit", done)),
  );
  const forceTimer = setTimeout(() => {
    for (const child of children.values()) stop(child, "SIGKILL");
  }, 3_000);
  forceTimer.unref();
  await Promise.allSettled(waitForExit);
  clearTimeout(forceTimer);
  process.exit(code);
}

function requireLocalUrl(value) {
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error("PYTHON_ANALYSIS_URL must be a valid local HTTP URL.");
  }
  if (
    url.protocol !== "http:" ||
    !["localhost", "127.0.0.1", "[::1]"].includes(url.hostname) ||
    url.username ||
    url.password ||
    url.pathname !== "/" ||
    url.search ||
    url.hash
  ) {
    throw new Error("PYTHON_ANALYSIS_URL must be a loopback HTTP origin.");
  }
  return url;
}

async function healthy(url) {
  try {
    const response = await fetch(new URL("/health", url), { signal: AbortSignal.timeout(1_000) });
    return response.ok;
  } catch {
    return false;
  }
}

async function waitForHealth(url, child) {
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error("FastAPI exited before becoming healthy.");
    if (await healthy(url)) return;
    await new Promise((resolveWait) => setTimeout(resolveWait, 250));
  }
  throw new Error(`FastAPI did not become healthy at ${new URL("/health", url)}.`);
}

async function main() {
  console.log("[local-dev] Starting local Supabase if needed…");
  run("supabase", ["start"], "Could not start local Supabase. Ensure Docker is running.", {
    quiet: true,
  });
  console.log("[local-dev] Supabase is ready.");

  const envPath = resolve(root, ".env.local");
  if (!existsSync(envPath)) {
    console.log("[local-dev] Creating .env.local from local Supabase…");
    run(process.execPath, ["scripts/local-env.mjs"], "Could not create .env.local.");
  }
  loadEnvFile(envPath);

  if (process.env.PYTHON_ANALYSIS_ENABLED === "true") {
    const url = requireLocalUrl(process.env.PYTHON_ANALYSIS_URL ?? "");
    const token = process.env.PYTHON_ANALYSIS_TOKEN ?? "";
    if (!/^[A-Za-z0-9_-]{32,256}$/.test(token)) {
      throw new Error("PYTHON_ANALYSIS_TOKEN must contain 32–256 URL-safe characters.");
    }

    if (await healthy(url)) {
      console.log(`[local-dev] Reusing FastAPI at ${url.origin}.`);
    } else {
      const python = resolve(
        root,
        process.platform === "win32"
          ? "python/.venv/Scripts/python.exe"
          : "python/.venv/bin/python",
      );
      if (!existsSync(python)) {
        throw new Error(
          "Python environment missing. Run: python3 -m venv python/.venv && python/.venv/bin/python -m pip install -r python/requirements.txt",
        );
      }
      const imports = spawnSync(python, ["-c", "import fastapi, uvicorn"], { stdio: "ignore" });
      if (imports.status !== 0) {
        throw new Error(
          "Python API dependencies missing. Run: python/.venv/bin/python -m pip install -r python/requirements.txt",
        );
      }

      const port = url.port || "80";
      const host = url.hostname === "[::1]" ? "::1" : url.hostname;
      console.log(`[local-dev] Starting FastAPI at ${url.origin}…`);
      const api = start(
        "FastAPI",
        python,
        [
          "-m",
          "uvicorn",
          "market_analysis.api:app",
          "--host",
          host,
          "--port",
          port,
          "--no-access-log",
          "--limit-concurrency",
          "32",
        ],
        { cwd: resolve(root, "python") },
      );
      await waitForHealth(url, api);
      console.log("[local-dev] FastAPI is healthy.");
    }
  } else {
    console.log("[local-dev] FastAPI skipped because PYTHON_ANALYSIS_ENABLED is not true.");
  }

  console.log("[local-dev] Starting the application…");
  const npm = process.platform === "win32" ? "npm.cmd" : "npm";
  start("application", npm, ["run", "dev"]);
}

process.once("SIGINT", () => void shutdown(0));
process.once("SIGTERM", () => void shutdown(0));

try {
  await main();
} catch (error) {
  console.error(`[local-dev] ${error instanceof Error ? error.message : String(error)}`);
  await shutdown(1);
}
