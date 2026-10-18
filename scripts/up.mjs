import { spawn } from "node:child_process";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { createInterface } from "node:readline";
import { parseEnv } from "node:util";
import { setTimeout as sleep } from "node:timers/promises";
import { parseRunnerPublicUrl } from "./lib/runner-url.mjs";

const root = resolve(import.meta.dirname, "..");
const updateSecret = process.argv.includes("--update-worker-secret");
const log = (message) => console.log(`[up] ${message}`);
const children = new Set();
let shuttingDown = false;
let restartTimer;
let tunnel;
let backoff = 2_000;
let publishedUrl;
let startupHealthFailures = 0;
let secretUpdates = Promise.resolve();
// Fixed-URL mode (#308): a Tailscale Funnel address from RUNNER_PUBLIC_URL; null = quick-tunnel mode.
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
    // Supabase status and Wrangler output can contain credentials. Never echo them.
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
  tunnel?.abort.abort();
  fixedMonitor.abort();
  clearTimeout(restartTimer);
  log("Stopping tunnel and worker host; allowing up to 75 seconds for drain. Docker stack stays running.");
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
  if (process.argv.slice(2).some((arg) => arg !== "--update-worker-secret")) {
    throw new Error("Usage: npm run up -- [--update-worker-secret]");
  }
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
  try {
    publicUrl = parseRunnerPublicUrl(process.env.RUNNER_PUBLIC_URL ?? env?.RUNNER_PUBLIC_URL);
  } catch (error) {
    failed = true;
    log(error.message);
  }
  if (publicUrl === null) {
    if (await command("cloudflared", ["--version"]) !== 0) {
      failed = true;
      log("cloudflared is missing or cannot run. Install the cloudflared package for your OS and put it on PATH.");
    }
  } else if (await command("tailscale", ["version"]) !== 0) {
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

// Serialize writes so an older tunnel can never overwrite a newer URL. `current` is checked when
// the queued write starts; the URL goes to Wrangler through stdin, never on a command line.
function updateWorkerSecret(url, current) {
  secretUpdates = secretUpdates.then(async () => {
    if (shuttingDown || !current()) return;
    const child = start("npx", ["wrangler", "secret", "put", "RUNNER_GATEWAY_URL"]);
    child.stdin.end(`${url}\n`);
    const timer = setTimeout(() => signal(child, "SIGKILL"), 120_000);
    const result = await child.done;
    clearTimeout(timer);
    if (!alive(child)) children.delete(child);
    if (!shuttingDown) log(result === 0
      ? "RUNNER_GATEWAY_URL updated successfully."
      : "RUNNER_GATEWAY_URL update failed. Check Wrangler login and Worker access, then retry.");
  });
  return secretUpdates;
}

// One /health attempt with the quick-tunnel settings: no redirects, at most 5 s.
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

// Fixed-URL mode: nothing to restart (Funnel is managed by hand), so a failing URL never stops the
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
      if (updateSecret) await updateWorkerSecret(url, () => true);
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

async function announceWhenHealthy(url, instance) {
  const deadline = Date.now() + 90_000;
  let lastFailure = "No health response received";
  // Give the new hostname time to propagate before risking a cached DNS miss.
  await sleep(5_000, undefined, { signal: instance.abort.signal }).catch(() => {});
  while (!shuttingDown && tunnel === instance && !instance.ended && Date.now() < deadline) {
    const attemptStarted = Date.now();
    try {
      const response = await fetch(`${url}/health`, {
        redirect: "manual",
        signal: AbortSignal.any([instance.abort.signal, AbortSignal.timeout(Math.max(1, Math.min(5_000, deadline - Date.now())))]),
      });
      await response.body?.cancel();
      if (response.status === 200) {
        if (shuttingDown || tunnel !== instance || instance.ended) return;
        if (url !== publishedUrl) {
          publishedUrl = url;
          log(`Runner gateway is healthy: ${url}`);
          if (!updateSecret) log("Set RUNNER_GATEWAY_URL in Cloudflare to this URL.");
          else await updateWorkerSecret(url, () => tunnel === instance && !instance.ended);
        }
        return;
      }
      lastFailure = `HTTP ${response.status}`;
    } catch (error) {
      // DNS, tunnel startup and gateway startup can all lag behind URL allocation.
      lastFailure = error.name === "TimeoutError" ? "ETIMEDOUT"
        : error.name === "AbortError" ? "abort"
          : String(error.cause?.code ?? error.code ?? error.message ?? error);
    }
    await sleep(Math.min(Math.max(0, 3_000 - (Date.now() - attemptStarted)), Math.max(0, deadline - Date.now())), undefined,
      { signal: instance.abort.signal }).catch(() => {});
  }
  if (!shuttingDown && tunnel === instance && !instance.ended) {
    if (!publishedUrl) startupHealthFailures += 1;
    const failure = `Tunnel /health did not return 200 within 90 seconds (last failure: ${lastFailure})`;
    for (const line of instance.output) log(`[cloudflared] ${line}`);
    if (publishedUrl || startupHealthFailures < 3) {
      const retry = publishedUrl ? "" : ` Startup health attempt ${startupHealthFailures}/3 failed.`;
      log(`${failure}; restarting the tunnel.${retry}`);
      signal(instance.child, "SIGKILL");
    } else {
      log(`${failure}. All 3 startup health attempts failed. Check worker-host output and connectivity; stopping.`);
      await shutdown(1);
    }
  }
}

function startTunnel() {
  if (shuttingDown) return;
  const child = start("cloudflared", ["tunnel", "--url", "http://127.0.0.1:8787"]);
  child.stdin.end();
  const instance = { child, started: Date.now(), ended: false, abort: new AbortController(), url: null, output: [] };
  tunnel = instance;
  // Keep a per-tunnel ring buffer in memory; only dump it on health failure.
  for (const stream of [child.stdout, child.stderr]) {
    createInterface({ input: stream }).on("line", (line) => {
      instance.output.push(line);
      if (instance.output.length > 30) instance.output.shift();
      const match = line.match(/https:\/\/([a-z0-9]+(?:-[a-z0-9]+)*)\.trycloudflare\.com\b/i);
      if (!match || match[1].toLowerCase() === "api" || instance.url || instance.ended || shuttingDown) return;
      instance.url = match[0];
      clearTimeout(urlDeadline);
      void announceWhenHealthy(instance.url, instance).catch(() => shutdown(1));
    });
  }
  const urlDeadline = setTimeout(() => {
    if (!shuttingDown && !instance.ended && !instance.url) {
      log("cloudflared did not provide a quick-tunnel URL within 60 seconds; restarting the tunnel.");
      signal(child, "SIGKILL");
    }
  }, 60_000);
  void child.done.then(() => {
    if (!alive(child)) children.delete(child);
    instance.ended = true;
    instance.abort.abort();
    clearTimeout(urlDeadline);
    if (shuttingDown) return;
    if (Date.now() - instance.started >= 5 * 60_000) backoff = 2_000;
    const delay = backoff;
    backoff = Math.min(backoff * 2, 60_000);
    log(`cloudflared exited; restarting in ${delay / 1_000}s. Worker host stays running.`);
    restartTimer = setTimeout(startTunnel, delay);
  });
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
    if (publicUrl === null) startTunnel();
    else void monitorPublicUrl(publicUrl);
  }
} catch (error) {
  log(error instanceof Error ? error.message : "Startup failed.");
  await shutdown(1);
}
