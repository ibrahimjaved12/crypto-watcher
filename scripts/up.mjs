import { spawn } from "node:child_process";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { createInterface } from "node:readline";
import { parseEnv } from "node:util";
import { setTimeout as sleep } from "node:timers/promises";
import { parseRunnerPublicUrl } from "./lib/runner-url.mjs";

const root = resolve(import.meta.dirname, "..");
const log = (message) => console.log(`[up] ${message}`);
const children = new Set();
let shuttingDown = false;
// The fixed Tailscale Funnel address from RUNNER_PUBLIC_URL (#308, #431); required by preflight.
let publicUrl = null;
const fixedMonitor = new AbortController();

function start(command, args, prefix) {
  if (shuttingDown) throw new Error("Shutdown is in progress.");
  const child = spawn(command, args, {
    cwd: root,
    detached: true,
    stdio: ["pipe", "pipe", "pipe"],
  });
  children.add(child);
  child.done = new Promise((done) => {
    child.once("error", () => done(1));
    child.once("exit", (code) => done(code ?? 1));
  });
  // Keep records until shutdown: npm can exit before its host process drains.
  if (prefix) {
    for (const stream of [child.stdout, child.stderr]) {
      createInterface({ input: stream }).on("line", (line) => log(`[${prefix}] ${line}`));
    }
  } else {
    // Supabase status output can contain credentials. Never echo it.
    child.stdout.resume();
    child.stderr.resume();
  }
  child.stdin.on("error", () => {});
  return child;
}

function signal(child, value) {
  if (!child?.pid) return;
  try {
    process.kill(-child.pid, value);
  } catch (error) {
    if (error.code !== "ESRCH") log("Could not signal a child process group.");
  }
}

function alive(child) {
  if (!child.pid) return false;
  try {
    process.kill(-child.pid, 0);
    return true;
  } catch (error) {
    return error.code !== "ESRCH";
  }
}

async function shutdown(code = 0) {
  if (shuttingDown) return;
  shuttingDown = true;
  fixedMonitor.abort();
  log("Stopping the worker host; allowing up to 75 seconds for drain. Docker stack stays running.");
  for (const child of children) signal(child, "SIGTERM");
  const deadline = Date.now() + 75_000;
  while ([...children].some(alive) && Date.now() < deadline) await sleep(100);
  for (const child of children) if (alive(child)) signal(child, "SIGKILL");
  process.exit(code);
}

async function command(commandName, args, timeout = 30_000) {
  const child = start(commandName, args);
  child.stdin.end();
  const timer = setTimeout(() => signal(child, "SIGKILL"), timeout);
  try {
    return await child.done;
  } finally {
    clearTimeout(timer);
    if (!alive(child)) children.delete(child);
  }
}

async function ensureOperational() {
  log("Checking operational Supabase through dev:local:all --operational-only…");
  const child = start("npm", ["run", "dev:local:all", "--", "--operational-only"], "operational");
  child.stdin.end();
  if (await child.done !== 0 && !shuttingDown)
    throw new Error("Operational Supabase startup failed; worker host and tunnel were not started.");
  if (!alive(child)) children.delete(child);
}

async function preflight() {
  if (process.argv.length > 2) throw new Error("Usage: npm run up");
  if (process.platform === "win32") throw new Error("Run this launcher on Linux with Docker Engine (or in WSL with Docker Engine).");
  let failed = false;
  if (await command("docker", ["info"]) !== 0) {
    failed = true;
    log("Docker Engine is unavailable. Check these manually (nothing is changed automatically):");
    log("sudo systemctl start docker");
    log("sudo systemctl enable docker");
    log("docker context ls / docker context use default");
    log("For socket permissions, add your user to the docker group and log in again.");
  }
  let env;
  try {
    env = parseEnv(await readFile(resolve(root, ".env.production.local"), "utf8"));
  } catch {
    failed = true;
    log(".env.production.local is missing or unreadable. Complete the host setup in docs/worker-host.md.");
  }
  // The shell value takes precedence over the host file; the value is never logged here.
  let urlInvalid = false;
  try {
    publicUrl = parseRunnerPublicUrl(process.env.RUNNER_PUBLIC_URL ?? env?.RUNNER_PUBLIC_URL);
  } catch (error) {
    failed = urlInvalid = true;
    log(error.message);
  }
  if (publicUrl === null && !urlInvalid) {
    failed = true;
    log("RUNNER_PUBLIC_URL is missing. Set it to your Tailscale Funnel address (see docs/worker-host.md).");
  }
  if (await command("tailscale", ["version"]) !== 0) {
    // The launcher never manages Tailscale: Funnel is started by hand once (docs/worker-host.md).
    failed = true;
    log("tailscale is missing or cannot run. Install Tailscale and sign in.");
  }
  for (const flag of ["RUNNER_GATEWAY_ENABLED", "RUNNER_JOBS_ENABLED"]) {
    if (env?.[flag] !== "true") {
      failed = true;
      log(`.env.production.local: required enabled line is missing or invalid: ${flag}. Set this flag to true.`);
    }
    if (process.env[flag] !== undefined && process.env[flag] !== "true") {
      failed = true;
      log(`Shell override disables ${flag}; correct it before starting.`);
    }
  }
  if ([env?.RUNNER_GATEWAY_PORT, process.env.RUNNER_GATEWAY_PORT].some(
    (port) => port !== undefined && port !== "8787",
  )) {
    failed = true;
    log("RUNNER_GATEWAY_PORT must use port 8787 for this launcher; correct the host file or shell override.");
  }
  if (failed) throw new Error("Preflight failed; no services were started.");
}

// One /health attempt: no redirects, at most 5 s.
async function healthy(url, abortSignal) {
  try {
    const response = await fetch(`${url}/health`, {
      redirect: "manual",
      signal: AbortSignal.any([abortSignal, AbortSignal.timeout(5_000)]),
    });
    await response.body?.cancel();
    return response.status === 200;
  } catch {
    return false;
  }
}

// Nothing to restart (Funnel is managed by hand), so a failing URL never stops the
// launcher. Startup polls every 3 s; after the first success, or after 90 s without one, every 30 s.
// A line is logged only when the state changes.
async function monitorPublicUrl(url) {
  const pause = (ms) => sleep(ms, undefined, { signal: fixedMonitor.signal }).catch(() => {});
  const notAnswering = "Public runner URL is not answering. Check: tailscale status, tailscale funnel status. The worker host keeps running.";
  await pause(5_000);
  const deadline = Date.now() + 90_000;
  let announced = false;
  let up = false;
  let warned = false;
  while (!shuttingDown) {
    const attemptStarted = Date.now();
    const ok = await healthy(url, fixedMonitor.signal);
    if (shuttingDown) return;
    if (ok && !announced) {
      announced = true;
      log(`Runner gateway is healthy: ${url}`);
    } else if (ok && !up) {
      log("Public runner URL is answering again.");
    } else if (!ok && up) {
      log(notAnswering);
    } else if (!ok && !announced && !warned && Date.now() >= deadline) {
      warned = true;
      log(notAnswering);
    }
    up = ok;
    const interval = announced || warned ? 30_000 : 3_000;
    await pause(Math.max(0, interval - (Date.now() - attemptStarted)));
  }
}

for (const signalName of ["SIGINT", "SIGTERM", "SIGHUP"]) process.on(signalName, () => void shutdown());
for (const stream of [process.stdout, process.stderr]) stream.on("error", () => void shutdown(1));

try {
  await preflight();
  if (!shuttingDown) {
    await ensureOperational();
  }
  if (!shuttingDown) {
    const host = start("npm", ["run", "worker:host"], "worker:host");
    host.stdin.end();
    void host.done.then((code) => {
      if (!shuttingDown) {
        log("worker:host exited; shutting down.");
        void shutdown(code);
      }
    });
    void monitorPublicUrl(publicUrl);
  }
} catch (error) {
  log(error instanceof Error ? error.message : "Startup failed.");
  await shutdown(1);
}
