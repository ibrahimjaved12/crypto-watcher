import { spawn } from "node:child_process";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { createInterface } from "node:readline";
import { parseEnv } from "node:util";
import { setTimeout as sleep } from "node:timers/promises";

const root = resolve(import.meta.dirname, "..");
const updateSecret = process.argv.includes("--update-worker-secret");
const log = (message) => console.log(`[up] ${message}`);
const children = new Set();
let shuttingDown = false;
let restartTimer;
let tunnel;
let backoff = 2_000;
let publishedUrl;
let secretUpdates = Promise.resolve();

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
  if (await command("cloudflared", ["--version"]) !== 0) {
    failed = true;
    log("cloudflared is missing or cannot run. Install the cloudflared package for your OS and put it on PATH.");
  }
  let env;
  try {
    env = parseEnv(await readFile(resolve(root, ".env.production.local"), "utf8"));
  } catch {
    failed = true;
    log(".env.production.local is missing or unreadable. Complete the host setup in docs/worker-host.md.");
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

async function announceWhenHealthy(url, instance) {
  const deadline = Date.now() + 60_000;
  while (!shuttingDown && tunnel === instance && !instance.ended && Date.now() < deadline) {
    const attemptStarted = Date.now();
    try {
      const response = await fetch(`${url}/health`, {
        redirect: "manual",
        signal: AbortSignal.any([instance.abort.signal, AbortSignal.timeout(Math.max(1, Math.min(2_000, deadline - Date.now())))]),
      });
      await response.body?.cancel();
      if (response.status === 200) {
        if (shuttingDown || tunnel !== instance || instance.ended) return;
        if (url !== publishedUrl) {
          publishedUrl = url;
          log(`Runner gateway is healthy: ${url}`);
          if (!updateSecret) log("Set RUNNER_GATEWAY_URL in Cloudflare to this URL.");
          else {
            // Serialize writes so an older tunnel can never overwrite a newer URL.
            secretUpdates = secretUpdates.then(async () => {
              if (shuttingDown || tunnel !== instance || instance.ended) return;
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
            await secretUpdates;
          }
        }
        return;
      }
    } catch {
      // DNS, tunnel startup and gateway startup can all lag behind URL allocation.
    }
    await sleep(Math.min(Math.max(0, 2_000 - (Date.now() - attemptStarted)), Math.max(0, deadline - Date.now())), undefined,
      { signal: instance.abort.signal }).catch(() => {});
  }
  if (!shuttingDown && tunnel === instance && !instance.ended) {
    if (publishedUrl) {
      log("Tunnel /health did not return 200 within 60 seconds; restarting the tunnel.");
      signal(instance.child, "SIGKILL");
    } else {
      log("Tunnel /health did not return 200 within 60 seconds. Check worker-host output and connectivity; stopping.");
      await shutdown(1);
    }
  }
}

function startTunnel() {
  if (shuttingDown) return;
  const child = start("cloudflared", ["tunnel", "--url", "http://127.0.0.1:8787"]);
  child.stdin.end();
  const instance = { child, started: Date.now(), ended: false, abort: new AbortController(), url: null };
  tunnel = instance;
  // Do not echo tunnel output: print the public URL once it has passed health checks.
  for (const stream of [child.stdout, child.stderr]) {
    createInterface({ input: stream }).on("line", (line) => {
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
    startTunnel();
  }
} catch (error) {
  log(error instanceof Error ? error.message : "Startup failed.");
  await shutdown(1);
}
