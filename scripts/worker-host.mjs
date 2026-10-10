import { spawn, spawnSync } from "node:child_process";
import { existsSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { loadEnv, runnerImport } from "vite";

// Worker-host supervisor (#270). Runs the forward-test workers on the owner's machine in production
// mode against the hosted main Supabase and the local operational Supabase: two FastAPI services,
// the collector, the hourly forward job and the daily trend job. Pure logic (preflight, backoff,
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
const KILL_AFTER_MS = 10_000; // SIGTERM first, SIGKILL after this long

const importLib = async (path) =>
  (await runnerImport(path, { root, mode: MODE, configFile: false, envDir: false })).module;

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
    const response = await fetch(new URL("/health", origin), { signal: AbortSignal.timeout(2_000) });
    return response.ok;
  } catch {
    return false;
  }
}

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

// ── Status file ────────────────────────────────────────────────────────────────────────────────

function writeStatus() {
  if (!lib || records.size === 0) return;
  const status = lib.buildStatus({
    supervisorPid: process.pid,
    startedAtMs,
    nowMs: Date.now(),
    shuttingDown,
    children: [...records.values()].map((r) => ({
      name: r.name,
      state: r.state,
      pid: r.state === "running" || r.state === "stopping" ? (r.proc?.pid ?? null) : null,
      restartCount: r.restartCount,
      lastRestartAt: r.lastRestartAt,
      lastExit: r.lastExit,
      nextRestartAt: r.nextRestartAt,
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
    const { state, decision } = lib.nextRestart(record.restartState, {
      nowMs: Date.now(),
      startedAtMs: record.startedAt,
    });
    record.restartState = state;
    if (decision.kind === "crash-loop") {
      record.state = "crash-loop";
      crashLoopSeen = true;
      logError(
        `${record.name} stopped with ${reason}: ${decision.crashesInWindow} crashes within 10 minutes. Not restarting it; the other children keep running and the host will exit non-zero.`,
      );
    } else {
      record.state = "backoff";
      record.nextRestartAt = new Date(Date.now() + decision.delayMs).toISOString();
      logError(`${record.name} stopped with ${reason}; restarting in ${Math.round(decision.delayMs / 1000)}s.`);
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
  log(`${reason}; stopping children (SIGTERM, SIGKILL after ${KILL_AFTER_MS / 1000}s).`);
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
  for (const record of records.values()) if (record.state === "stopping") signalChild(record, "SIGTERM");
  const killTimer = setTimeout(() => {
    for (const record of records.values()) if (record.state === "stopping") signalChild(record, "SIGKILL");
  }, KILL_AFTER_MS);
  await Promise.all(exits);
  clearTimeout(killTimer);
  const code = crashLoopSeen ? 1 : requestedCode;
  if (code === 0) {
    rmSync(statusPath, { force: true }); // deliberate stop: the host is simply no longer in use
  } else {
    writeStatus(); // keep the evidence (crash-loop state); mvp:check turns stale into FAIL
  }
  log(code === 0 ? "stopped." : `stopped with exit code ${code}${crashLoopSeen ? " (a child hit the crash-loop breaker)" : ""}.`);
  process.exit(code);
}

// ── Startup ────────────────────────────────────────────────────────────────────────────────────

function runtimeProblems(python) {
  const problems = [];
  if (!existsSync(python)) {
    problems.push(
      "Python environment missing. Run: python3 -m venv python/.venv && python/.venv/bin/python -m pip install -r python/requirements.txt",
    );
  } else if (spawnSync(python, ["-c", "import fastapi, uvicorn, numpy"], { stdio: "ignore" }).status !== 0) {
    problems.push("Python API dependencies missing. Run: python/.venv/bin/python -m pip install -r python/requirements.txt");
  }
  if (existsSync(statusPath)) {
    const previous = lib.parseStatus(readFileSync(statusPath, "utf8"));
    if (previous && previous.supervisorPid !== process.pid && lib.isStatusFresh(previous, Date.now())) {
      try {
        process.kill(previous.supervisorPid, 0);
        problems.push(`Another worker host is running (pid ${previous.supervisorPid}). Stop it first.`);
      } catch (error) {
        if (error.code === "EPERM") problems.push(`Another worker host may be running (pid ${previous.supervisorPid}).`);
      }
    }
  }
  return problems;
}

async function main() {
  const envFileExists = existsSync(envFile);
  const env = { ...loadEnv(MODE, root, ""), ...process.env };
  lib = await importLib("/src/lib/worker-host.ts");

  const problems = lib.preflight({ env, envFileExists });
  if (!problems.length) {
    try {
      // The collector's own contract (retention days, Python URL/token, grace period).
      (await importLib("/src/lib/market/collector-worker-env.server.ts")).validateCollectorWorkerEnvironment(env);
    } catch (error) {
      problems.push(error instanceof Error ? error.message : "collector worker environment is invalid");
    }
  }
  let endpoints;
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

  setInterval(writeStatus, lib.STATUS_WRITE_INTERVAL_MS);
  log(`Preflight passed (${MODE}, hosted main Supabase). Starting ${specs.length} children.`);
  for (const name of ["fastapi-analysis", "fastapi-forward", "collector"]) launch(records.get(name));
  const pythonNames = ["fastapi-analysis", "fastapi-forward"];
  const urls = [endpoints.analysis.origin, endpoints.forward.origin];
  void startAfterPython(records.get("forward-hourly"), urls, pythonNames);
  void startAfterPython(records.get("forward-trend"), urls, pythonNames);
}

for (const signal of process.platform === "win32" ? ["SIGINT", "SIGTERM", "SIGBREAK"] : ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => void shutdown(`received ${signal}`));
}
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
