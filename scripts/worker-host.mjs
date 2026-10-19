import { spawn, spawnSync } from "node:child_process";
import { existsSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { uptime } from "node:os";
import { resolve } from "node:path";
import { loadEnv, runnerImport } from "vite";
import { createRunnerHealthReporter } from "./runner-health.mjs";

// Worker-host supervisor (#270). Runs the forward-test workers on the owner's machine in production
// mode against the hosted main Supabase and the local operational Supabase: two FastAPI services,
// the collector, the hourly forward job, the daily trend job and the non-essential siblings (quote and
// movement-state publishers, alert charts, the monitor scheduler). Pure logic (preflight, backoff,
// crash-loop accounting, status file) lives in src/lib/worker-host.ts; this file only spawns and
// wires. See docs/worker-host.md.
const root = resolve(import.meta.dirname, "..");
const MODE = "production";
const envFile = resolve(root, ".env.production.local");
const statusPath = resolve(root, ".worker-host.status.json");
const startedAtMs = Date.now();
const log = (line) => console.log(`${new Date().toISOString()} [worker-host] ${line}`);
const logError = (line) => console.error(`${new Date().toISOString()} [worker-host] ${line}`);

/** @type {Map<string, any>} */
const records = new Map();
let lib;
let shuttingDown = false;
let crashLoopSeen = false;
let forceSignalSeen = false;
let statusWarned = false;
let allDownSinceMs = null;
let reportHealth;
let healthEndpoints;
let healthTimer;
let healthPublishing;
let healthWarned = false;
const KILL_AFTER_MS = 10_000; // SIGTERM first, SIGKILL after this long
const RUNNER_JOBS_DRAIN_MS = 60_000;
// Children that finish their current unit of work on SIGTERM while Python and the collector stay up.
const DRAIN_FIRST = ["runner-jobs", "monitor-scheduler"];

const importLib = async (path) => {
  const loaded = (await runnerImport(path, { root, mode: MODE, configFile: false, envDir: false })).module;
  if (loaded === null || typeof loaded !== "object") {
    throw new Error(`${path} did not load as a module object; the Vite runner returned ${loaded === null ? "null" : typeof loaded}.`);
  }
  return loaded;
};

// ── Process helpers ────────────────────────────────────────────────────────────────────────────

function signalChild(record, signal) {
  const pid = record.proc?.pid;
  if (!pid || record.proc.exitCode !== null || record.proc.signalCode !== null) return;
  try {
    if (process.platform === "win32") {
      // Windows has no POSIX signals: kill() is a forced TerminateProcess of the direct child.
      // For the forced step also end the child's tree (uvicorn/npm helpers) with taskkill.
      if (signal === "SIGKILL") spawnSync("taskkill", ["/pid", String(pid), "/T", "/F"], { stdio: "ignore" });
      else record.proc.kill();
    } else {
      process.kill(-pid, signal); // each child leads its own process group
    }
  } catch (error) {
    if (error.code !== "ESRCH") logError(`could not signal ${record.name}: ${error.message}`);
  }
}

const isAlive = (proc) => !!proc && proc.pid !== undefined && proc.exitCode === null && proc.signalCode === null;

function pidAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error.code === "EPERM";
  }
}

function pipeLines(stream, name, sink) {
  let carry = "";
  const emit = (line) => sink.write(`${new Date().toISOString()} [${name}] ${line}\n`);
  stream.setEncoding("utf8");
  stream.on("data", (chunk) => {
    const parts = (carry + chunk).split(/\r?\n/);
    carry = parts.pop() ?? "";
    for (const line of parts) emit(line);
  });
  stream.on("end", () => {
    if (carry) emit(carry);
    carry = "";
  });
}

async function healthy(origin) {
  try {
    const response = await fetch(new URL("/health", origin), { redirect: "error", signal: AbortSignal.timeout(2_000) });
    await response.body?.cancel();
    return response.ok;
  } catch {
    return false;
  }
}

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

/** Probe services repeatedly, not just at startup; publish independently of the public tunnel. */
function publishHealth() {
  if (!reportHealth || healthPublishing) return;
  healthPublishing = (async () => {
    const [analysis, forward] = await Promise.all([
      healthy(healthEndpoints.analysis.origin), healthy(healthEndpoints.forward.origin),
    ]);
    const services = Object.fromEntries([...records.values()].map((record) => [
      record.name, record.state === "running" && isAlive(record.proc),
    ]));
    services["fastapi-analysis"] = services["fastapi-analysis"] && analysis;
    services["fastapi-forward"] = services["fastapi-forward"] && forward;
    const status = !shuttingDown && Object.values(services).every(Boolean) ? "online" : "degraded";
    await reportHealth("worker-host", status, services);
    healthWarned = false;
  })().catch(() => {
    if (!healthWarned) logError("could not publish runner health; retrying next interval.");
    healthWarned = true;
  }).finally(() => { healthPublishing = undefined; });
}

// ── Status file ────────────────────────────────────────────────────────────────────────────────

function writeStatus() {
  if (!lib || records.size === 0) return;
  const now = Date.now();
  for (const r of records.values()) {
    // Down until the relaunched child has run for the stable period.
    if (r.downSince && r.state === "running" && now - r.startedAt >= lib.RESTART_POLICY.stableMs) r.downSince = null;
  }
  const status = lib.buildStatus({
    supervisorPid: process.pid,
    startedAtMs,
    nowMs: now,
    shuttingDown,
    children: [...records.values()].map((r) => ({
      name: r.name,
      state: r.state,
      // Every live child pid is recorded (also while stopping), so a later start can find orphans.
      pid: isAlive(r.proc) ? r.proc.pid : null,
      restartCount: r.restartCount,
      lastRestartAt: r.lastRestartAt,
      lastExit: r.lastExit,
      nextRestartAt: r.nextRestartAt,
      downSince: r.downSince,
    })),
  });
  try {
    lib.writeStatusAtomic(
      {
        writeFile: (path, data) => writeFileSync(path, data, { mode: 0o600 }),
        rename: renameSync,
        remove: (path) => rmSync(path, { force: true }),
      },
      statusPath,
      status,
      process.pid,
    );
    statusWarned = false;
  } catch (error) {
    if (!statusWarned) logError(`could not write the status file: ${error.message}`);
    statusWarned = true;
  }
}

// ── Children ───────────────────────────────────────────────────────────────────────────────────

function launch(record) {
  record.startedAt = Date.now();
  record.nextRestartAt = null;
  record.downSince ??= null;
  let handled = false;
  const proc = spawn(record.command, record.args, {
    cwd: record.cwd,
    env: record.env,
    stdio: ["ignore", "pipe", "pipe"],
    detached: process.platform !== "win32",
    windowsHide: true,
  });
  record.proc = proc;
  record.state = "running";
  pipeLines(proc.stdout, record.name, process.stdout);
  pipeLines(proc.stderr, record.name, process.stderr);
  log(`${record.name} started (pid ${proc.pid ?? "?"}).`);

  const onGone = (reason) => {
    if (handled) return;
    handled = true;
    record.lastExit = reason;
    if (shuttingDown) {
      record.state = "stopped";
      writeStatus();
      return;
    }
    record.downSince ??= new Date().toISOString();
    const { state, decision } = lib.nextRestart(
      record.restartState,
      { nowMs: Date.now(), startedAtMs: record.startedAt },
      lib.RESTART_POLICY,
      { neverGiveUp: record.name === lib.ESSENTIAL_CHILD },
    );
    record.restartState = state;
    if (decision.kind === "crash-loop") {
      record.state = "crash-loop";
      crashLoopSeen = true;
      logError(
        `${record.name} stopped with ${reason}: ${decision.crashesInWindow} crashes within 10 minutes. Not restarting it; the other children keep running.`,
      );
    } else {
      record.state = "backoff";
      record.nextRestartAt = new Date(Date.now() + decision.delayMs).toISOString();
      logError(`${record.name} stopped with ${reason}; restarting in ${Math.round(decision.delayMs / 1000)}s.`);
      if (record.name === lib.ESSENTIAL_CHILD && decision.delayMs === lib.RESTART_POLICY.capMs)
        logError(`${record.name} is never stopped for good: it keeps retrying every ${decision.delayMs / 1000}s.`);
      record.timer = setTimeout(() => {
        record.timer = null;
        if (shuttingDown) return;
        record.restartCount += 1;
        record.lastRestartAt = new Date().toISOString();
        launch(record);
        writeStatus();
      }, decision.delayMs);
    }
    writeStatus();
    evaluateExit();
  };
  proc.once("exit", (code, signal) => onGone(lib.describeExit(code, signal)));
  proc.once("error", (error) => onGone(`spawn error: ${error.message}`));
  writeStatus();
}

/** Forward jobs start only once both FastAPI services answer; give up if one is in a crash loop. */
async function startAfterPython(record, endpoints, pythonNames) {
  record.state = "waiting";
  writeStatus();
  let announced = false;
  while (!shuttingDown) {
    if (pythonNames.some((name) => records.get(name).state === "crash-loop")) {
      logError(`${record.name} not started: a FastAPI service is in a crash loop.`);
      return;
    }
    if ((await Promise.all(endpoints.map(healthy))).every(Boolean)) {
      if (!shuttingDown) launch(record);
      return;
    }
    if (!announced) log(`${record.name} waiting for both Python health endpoints…`);
    announced = true;
    await sleep(500);
  }
}

/** Exit non-zero when the forward test cannot go on (rules in `exitDecision`). */
function evaluateExit() {
  if (shuttingDown || !lib || records.size === 0) return;
  const result = lib.exitDecision(
    [...records.values()].map((r) => ({ name: r.name, state: r.state })),
    allDownSinceMs,
    Date.now(),
  );
  allDownSinceMs = result.allDownSinceMs;
  if (result.reason) {
    logError(`${result.reason}; exiting so the process manager can restart the host.`);
    void shutdown(result.reason, 1);
  }
}

// ── Shutdown ───────────────────────────────────────────────────────────────────────────────────

async function shutdown(reason, requestedCode = 0) {
  if (shuttingDown) {
    // A second signal means "now".
    if (!forceSignalSeen) {
      forceSignalSeen = true;
      logError("second signal: sending SIGKILL to the children now.");
      for (const record of records.values()) signalChild(record, "SIGKILL");
    }
    return;
  }
  shuttingDown = true;
  log(`${reason}; stopping children (runner jobs and the monitor scheduler drain for up to 60s; other children then get ${KILL_AFTER_MS / 1000}s).`);
  for (const record of records.values()) {
    if (record.timer) clearTimeout(record.timer);
    record.timer = null;
    record.nextRestartAt = null;
    const live = record.proc && record.proc.exitCode === null && record.proc.signalCode === null;
    record.state = live ? "stopping" : record.state === "crash-loop" ? "crash-loop" : "stopped";
  }
  writeStatus();
  const exits = [...records.values()]
    .filter((r) => r.state === "stopping")
    .map((r) => new Promise((done) => r.proc.once("exit", done)));
  // Keep Python and the collector alive while the on-demand job and the current monitor pass (which may call
  // Python and holds a monitor lease) finish, then stop the rest.
  const draining = DRAIN_FIRST.map((name) => records.get(name)).filter((r) => r?.state === "stopping");
  if (draining.length) {
    let drainTimer;
    const drained = Promise.all(draining.map((r) => new Promise((done) => {
      if (!isAlive(r.proc)) done();
      else r.proc.once("exit", done);
    })));
    const drainDeadline = new Promise((done) => {
      drainTimer = setTimeout(done, RUNNER_JOBS_DRAIN_MS);
    });
    for (const r of draining) signalChild(r, "SIGTERM");
    await Promise.race([drained, drainDeadline]);
    clearTimeout(drainTimer);
    for (const r of draining) if (isAlive(r.proc)) signalChild(r, "SIGKILL");
  }
  for (const record of records.values())
    if (record.state === "stopping" && !DRAIN_FIRST.includes(record.name)) signalChild(record, "SIGTERM");
  const killTimer = setTimeout(() => {
    for (const record of records.values()) if (record.state === "stopping") signalChild(record, "SIGKILL");
  }, KILL_AFTER_MS);
  // Hard deadline: a child that survives SIGKILL (possible with taskkill) must not hold the host forever.
  let deadlineTimer;
  const deadline = new Promise((done) => {
    deadlineTimer = setTimeout(() => done("deadline"), lib?.SHUTDOWN_HARD_DEADLINE_MS ?? 15_000);
  });
  const outcome = await Promise.race([Promise.all(exits).then(() => "exited"), deadline]);
  clearTimeout(killTimer);
  clearTimeout(deadlineTimer);
  const survivors = [...records.values()].filter((r) => isAlive(r.proc));
  if (outcome === "deadline" && survivors.length) {
    logError(
      `shutdown deadline reached; still alive: ${survivors.map((r) => `${r.name} (pid ${r.proc.pid})`).join(", ")}. Their pids stay in the status file and block the next start until they are gone.`,
    );
  }
  const code = crashLoopSeen || survivors.length ? 1 : requestedCode;
  clearInterval(healthTimer);
  await healthPublishing;
  if (reportHealth) {
    try { await reportHealth("worker-host", "offline"); }
    catch { logError("could not publish stopped runner health; the last heartbeat will expire."); }
  }
  if (code === 0) {
    rmSync(statusPath, { force: true }); // deliberate stop: the host is simply no longer in use
  } else {
    writeStatus(); // keep the evidence (crash-loop state); mvp:check turns stale into FAIL
  }
  log(code === 0 ? "stopped." : `stopped with exit code ${code}${crashLoopSeen ? " (a child hit the crash-loop breaker)" : ""}.`);
  process.exit(code);
}

// ── Startup ────────────────────────────────────────────────────────────────────────────────────

/**
 * Processes a previous run recorded in the status file (the host and its children) that are still
 * alive. A host killed without cleanup, or a closed terminal, leaves its children running; a second
 * collector next to one would double-write.
 */
function orphanProblems() {
  if (!existsSync(statusPath)) return [];
  let raw;
  try {
    raw = readFileSync(statusPath, "utf8");
  } catch (error) {
    return [`Cannot read ${statusPath}: ${error.message}. It may list running workers.`];
  }
  const previous = lib.parseStatus(raw);
  if (!previous)
    return [".worker-host.status.json exists but is unreadable, so running workers cannot be ruled out. Check for stray processes, then delete it."];
  const bootMs = Date.now() - uptime() * 1000;
  return lib
    .recordedPids(previous, bootMs)
    .filter(({ pid }) => pid !== process.pid && pidAlive(pid))
    .map(
      ({ label, pid }) =>
        `${label} from a previous run is still alive (pid ${pid}). Stop it first (Linux/macOS: kill -TERM -- -${pid}; Windows: taskkill /pid ${pid} /T /F), or delete .worker-host.status.json if the pid now belongs to something unrelated.`,
    );
}

function runtimeProblems(python) {
  const problems = [];
  if (!existsSync(python)) {
    problems.push(
      "Python environment missing. Run: python3 -m venv python/.venv && python/.venv/bin/python -m pip install -r python/requirements.txt",
    );
  } else if (spawnSync(python, ["-c", "import fastapi, uvicorn, numpy"], { stdio: "ignore" }).status !== 0) {
    problems.push("Python API dependencies missing. Run: python/.venv/bin/python -m pip install -r python/requirements.txt");
  }
  problems.push(...orphanProblems());
  return problems;
}

async function main() {
  const envFileText = existsSync(envFile) ? readFileSync(envFile, "utf8") : null;
  const env = { ...loadEnv(MODE, root, ""), ...process.env, MACHINE_ONLY_ENABLED: "true" };
  lib = await importLib("/src/lib/worker-host.ts");

  const problems = lib.preflight({ env, envFileText });
  if (!problems.length) {
    try {
      // The collector's own contract (retention days, Python URL/token, grace period).
      (await importLib("/src/lib/market/collector-worker-env.server.ts")).validateCollectorWorkerEnvironment(env);
    } catch (error) {
      problems.push(error instanceof Error ? error.message : "collector worker environment is invalid");
    }
  }
  let endpoints;
  // Orphans are checked even when the configuration is wrong: they are a fact, not a setting.
  if (problems.length) problems.push(...orphanProblems());
  const python = resolve(root, process.platform === "win32" ? "python/.venv/Scripts/python.exe" : "python/.venv/bin/python");
  if (!problems.length) {
    endpoints = lib.pythonEndpoints(env);
    problems.push(...runtimeProblems(python));
    for (const [label, endpoint] of [["analysis", endpoints.analysis], ["forward", endpoints.forward]]) {
      if (await healthy(endpoint.origin))
        problems.push(
          `A service already answers on the ${label} FastAPI port (${endpoint.origin}). Stop it first; the host must own a single instance of each service.`,
        );
    }
  }
  if (problems.length) {
    logError("Refusing to start:");
    for (const problem of problems) logError(`  - ${problem}`);
    logError("See docs/worker-host.md.");
    process.exit(1);
  }

  const jobEnv = { ...env };
  delete jobEnv["WEB_CONCURRENCY"];
  const pythonEnv = lib.pythonChildEnv(env);
  const nodeJob = (name, script, extra = []) => ({
    name,
    command: process.execPath,
    args: [script, ...extra, "--mode", MODE], // the same commands as the npm scripts of the same names
    cwd: root,
    env: jobEnv,
  });
  const uvicorn = (name, endpoint) => ({
    name,
    command: python,
    args: lib.uvicornArgs(endpoint),
    cwd: resolve(root, "python"),
    env: pythonEnv,
  });
  const specs = [
    uvicorn("fastapi-analysis", endpoints.analysis),
    uvicorn("fastapi-forward", endpoints.forward),
    nodeJob("collector", "scripts/collector-worker.mjs"),
    nodeJob("forward-hourly", "scripts/forward-run.mjs"),
    nodeJob("forward-trend", "scripts/forward-run.mjs", ["--trend"]),
  ];
  // The hosted quote publisher (#323) is a sibling of the collector, not part of it, so the collector
  // artifact keeps no main-database credential. It needs the hosted service credentials to publish.
  if (env.MARKET_QUOTE_PUBLISHER_ENABLED !== "false" && env.SUPABASE_URL && env.SUPABASE_SERVICE_ROLE_KEY)
    specs.push(nodeJob("quote-publisher", "scripts/market-quote-publisher.mjs"));
  // Economic calendar fetcher (#401): non-essential sibling that keeps the news-blackout calendar fresh in the
  // operational database. Default off until forex runs.
  if (env.FX_CALENDAR_ENABLED === "true" && env.OPERATIONAL_DB_ENABLED === "true")
    specs.push(nodeJob("fx-calendar", "scripts/fx-calendar-fetcher.mjs"));
  // Forex quote publisher (#433): non-essential sibling that publishes forex quotes to the hosted
  // fx_market_quotes. Default off until the forex collector runs.
  if (env.FX_QUOTE_PUBLISHER_ENABLED === "true" && env.OPERATIONAL_DB_ENABLED === "true" && env.SUPABASE_URL
      && env.SUPABASE_SERVICE_ROLE_KEY)
    specs.push(nodeJob("fx-quote-publisher", "scripts/fx-quote-publisher.mjs"));
  // Forex collector (#453): a forex child that polls the MT5 gateway (#452) over HTTP and writes the local
  // operational database. It needs no Python service and no hosted credential. Default off; it also needs a running
  // terminal and gateway, and FX_SUBSCRIPTIONS seeded once with `npm run forex:collector -- --subscribe`.
  if (env.FX_COLLECTOR_ENABLED === "true" && env.OPERATIONAL_DB_ENABLED === "true" && env.FX_GATEWAY_URL?.trim()
      && env.FX_GATEWAY_TOKEN?.trim())
    specs.push(nodeJob("forex-collector", "scripts/forex-collector.mjs"));
  // Hourly forex market stage (#397): a forex child; it calls the Python forward service, so it waits for both
  // health endpoints like forward-hourly. Default off until the MT5 collector writes bars.
  if (env.FX_HOURLY_ENABLED === "true" && env.OPERATIONAL_DB_ENABLED === "true")
    specs.push(nodeJob("forex-hourly", "scripts/forex-hourly.mjs"));
  // Non-essential sibling: uses the host's existing restart/backoff/crash-loop policy.
  const universeSync = await importLib("/src/lib/market/collector-universe-sync.ts");
  if (universeSync.collectorUniverseSyncConfigured(env))
    specs.push(nodeJob("collector-universe-sync", "scripts/collector-universe-sync.mjs"));
  if (env.RUNNER_GATEWAY_ENABLED === "true")
    specs.push(nodeJob("runner-gateway", "scripts/runner-gateway.mjs"));
  if (env.RUNNER_JOBS_ENABLED === "true")
    specs.push(nodeJob("runner-jobs", "scripts/runner-jobs.mjs"));
  // Alert setup chart renderer (#299): non-essential, needs only the operational database, the
  // Python venv with matplotlib (python/requirements-machine.txt) and a PNG directory.
  if (env.ALERT_CHARTS_ENABLED !== "false" && env.OPERATIONAL_DB_ENABLED === "true" && env.ALERT_CHART_DIR?.trim())
    specs.push(nodeJob("alert-charts", "scripts/alert-charts.mjs"));
  // Movement-state publisher (#30): non-essential sibling that copies the engine's persisted state to the hosted
  // database through the site hook (shared secret, no service key) so the deployed site can show the market strip.
  if (env.MOVEMENT_STATE_PUBLISH_ENABLED !== "false" && env.OPERATIONAL_DB_ENABLED === "true"
      && (env.MOVEMENT_STATE_HOSTED_URL?.trim() || env.ALERT_CHART_HOSTED_URL?.trim()))
    specs.push(nodeJob("movement-state-publisher", "scripts/movement-state-publisher.mjs"));
  // Monitor scheduler (#30): non-essential; runs the scheduled monitoring pass (movement alerts and completed-candle
  // TA) about once a minute. The Worker cron stays off, so without this child checks only run on a manual click.
  if (lib.monitorSchedulerConfigured(env))
    specs.push(nodeJob("monitor-scheduler", "scripts/monitor-scheduler.mjs"));
  for (const spec of specs) {
    records.set(spec.name, {
      ...spec,
      proc: null,
      state: "waiting",
      startedAt: 0,
      restartCount: 0,
      lastRestartAt: null,
      lastExit: null,
      nextRestartAt: null,
      timer: null,
      restartState: lib.initialRestartState(),
    });
  }

  reportHealth = createRunnerHealthReporter(env);
  healthEndpoints = endpoints;
  // This timer stays active through the jobs drain, then a final offline report retains last seen.
  healthTimer = setInterval(publishHealth, lib.STATUS_WRITE_INTERVAL_MS);
  publishHealth();

  setInterval(writeStatus, lib.STATUS_WRITE_INTERVAL_MS);
  setInterval(evaluateExit, 5_000);
  log(`Preflight passed (${MODE}, hosted main Supabase). Starting ${specs.length} children.`);
  for (const name of ["fastapi-analysis", "fastapi-forward", "collector"]) launch(records.get(name));
  if (records.has("quote-publisher")) launch(records.get("quote-publisher"));
  if (records.has("fx-quote-publisher")) launch(records.get("fx-quote-publisher"));
  if (records.has("forex-collector")) launch(records.get("forex-collector"));
  if (records.has("collector-universe-sync")) launch(records.get("collector-universe-sync"));
  if (records.has("runner-gateway")) launch(records.get("runner-gateway"));
  if (records.has("alert-charts")) launch(records.get("alert-charts"));
  if (records.has("movement-state-publisher")) launch(records.get("movement-state-publisher"));
  const pythonNames = ["fastapi-analysis", "fastapi-forward"];
  const urls = [endpoints.analysis.origin, endpoints.forward.origin];
  void startAfterPython(records.get("forward-hourly"), urls, pythonNames);
  void startAfterPython(records.get("forward-trend"), urls, pythonNames);
  if (records.has("runner-jobs")) void startAfterPython(records.get("runner-jobs"), urls, pythonNames);
  // TA calls the analysis service, so the scheduler also waits for both health endpoints.
  if (records.has("monitor-scheduler")) void startAfterPython(records.get("monitor-scheduler"), urls, pythonNames);
  // The calendar fetcher parses through the analysis service, so it waits for it too.
  if (records.has("fx-calendar")) void startAfterPython(records.get("fx-calendar"), urls, pythonNames);
  if (records.has("forex-hourly")) void startAfterPython(records.get("forex-hourly"), urls, pythonNames);
}

// SIGHUP is what a closed terminal sends; without it the children would outlive the host.
for (const signal of process.platform === "win32" ? ["SIGINT", "SIGTERM", "SIGBREAK", "SIGHUP"] : ["SIGINT", "SIGTERM", "SIGHUP"]) {
  process.on(signal, () => void shutdown(`received ${signal}`));
}
// A closed terminal also breaks the log pipes; that must not crash the host before it can clean up.
for (const stream of [process.stdout, process.stderr]) stream.on("error", () => {});
process.on("uncaughtException", (error) => {
  logError(`unexpected error: ${error.message}`);
  void shutdown("unexpected error", 1);
});

try {
  await main();
} catch (error) {
  logError(error instanceof Error ? error.message : String(error));
  await shutdown("startup failed", 1);
}
