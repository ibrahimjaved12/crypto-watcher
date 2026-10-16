import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

// No network, processes or timers: the pure module with the real validators it reuses.
async function load(path, imports = {}) {
  const source = await readFile(new URL(path, import.meta.url), "utf8");
  let { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  });
  for (const [specifier, target] of Object.entries(imports))
    outputText = outputText.replaceAll(JSON.stringify(specifier), JSON.stringify(target));
  return `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`;
}
const w = await import(
  await load("../src/lib/worker-host.ts", {
    "./runner/gateway-core": await load("../src/lib/runner/gateway-core.ts"),
    "./environment": await load("../src/lib/environment.ts"),
    "./operational/config.server": await load("../src/lib/operational/config.server.ts"),
    "./python-service.server": await load("../src/lib/python-service.server.ts"),
  })
);

const jwt = (role) =>
  `${Buffer.from('{"alg":"HS256"}').toString("base64url")}.${Buffer.from(JSON.stringify({ role })).toString("base64url")}.sig`;
const SERVICE_KEY = jwt("service_role");
const TOKEN = "T".repeat(40);
const good = () => ({
  APP_PROFILE: "production",
  ALLOW_HOSTED_SUPABASE: "true",
  SUPABASE_URL: "https://abc.supabase.co",
  SUPABASE_SERVICE_ROLE_KEY: SERVICE_KEY,
  VITE_APP_PROFILE: "production",
  VITE_SUPABASE_URL: "https://abc.supabase.co",
  VITE_SUPABASE_PUBLISHABLE_KEY: jwt("anon"),
  OPERATIONAL_DB_ENABLED: "true",
  OPERATIONAL_SUPABASE_URL: "http://127.0.0.1:55321",
  OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY: jwt("service_role") + "x",
  BINANCE_COLLECTOR_ENABLED: "true",
  PYTHON_ANALYSIS_ENABLED: "true",
  PYTHON_ANALYSIS_URL: "http://127.0.0.1:8000",
  PYTHON_FORWARD_URL: "http://127.0.0.1:8001",
  PYTHON_ANALYSIS_TOKEN: TOKEN,
});
// The host's own file defines every name by default; `fileText` overrides it (null = file missing).
const fileFor = (env) => Object.keys(env).map((name) => `${name}=x`).join("\n");
const run = (patch = {}, fileText = undefined) => {
  const env = { ...good(), ...patch };
  for (const [k, v] of Object.entries(patch)) if (v === undefined) delete env[k];
  return w.preflight({ env, envFileText: fileText === undefined ? fileFor(env) : fileText });
};
const refuses = (patch, pattern, fileText = undefined) => {
  const problems = run(patch, fileText);
  assert.ok(problems.some((p) => pattern.test(p)), `expected ${pattern}, got ${JSON.stringify(problems)}`);
};

test("preflight passes a complete hosted production configuration", () => {
  assert.deepEqual(run(), []);
  assert.deepEqual(run({ OPERATIONAL_SUPABASE_URL: "https://ops.supabase.co" }), []);
});

test("preflight refuses every REPLACE_ placeholder and names only the variable", () => {
  const placeholder = "REPLACE_private_placeholder_value";
  for (const name of [...w.REQUIRED_ENV_FILE_NAMES, "FORWARD_USER_ID", "VITE_SUPABASE_PUBLISHABLE_KEY"]) {
    const problems = run({ [name]: placeholder });
    assert.ok(problems.includes(`${name} must be configured; placeholder values are not allowed.`));
    for (const problem of problems) assert.ok(!problem.includes(placeholder));
  }
  const placeholders = {
    SUPABASE_URL: "REPLACE_HOSTED_SUPABASE_URL",
    SUPABASE_SERVICE_ROLE_KEY: "REPLACE_HOSTED_SUPABASE_SERVICE_ROLE_KEY",
  };
  const problems = run(placeholders);
  for (const [name, value] of Object.entries(placeholders)) {
    assert.ok(problems.includes(`${name} must be configured; placeholder values are not allowed.`));
    for (const problem of problems) assert.ok(!problem.includes(value));
  }
});

test("preflight refuses each unsafe condition with a clear message", () => {
  refuses({}, /\.env\.production\.local is missing/, null);
  refuses({ SUPABASE_URL: "http://127.0.0.1:54321" }, /SUPABASE_URL must be the hosted main/);
  refuses({ SUPABASE_URL: "http://abc.supabase.co" }, /SUPABASE_URL must be the hosted main/);
  refuses({ ALLOW_HOSTED_SUPABASE: "TRUE" }, /ALLOW_HOSTED_SUPABASE must be exactly true/);
  refuses({ ALLOW_HOSTED_SUPABASE: undefined }, /ALLOW_HOSTED_SUPABASE must be exactly true/);
  refuses({ APP_PROFILE: "local" }, /APP_PROFILE must be production/);
  refuses({ OPERATIONAL_SUPABASE_URL: "http://ops.example.com" }, /HTTPS/);
  refuses({ OPERATIONAL_DB_ENABLED: "false" }, /OPERATIONAL_DB_ENABLED must be true/);
  refuses({ OPERATIONAL_SUPABASE_URL: "https://abc.supabase.co" }, /separate from the main/);
  refuses({ PYTHON_FORWARD_URL: "http://127.0.0.1:8000" }, /must differ from PYTHON_ANALYSIS_URL/);
  refuses({ PYTHON_FORWARD_URL: "http://localhost:8000/" }, /different port|must differ/);
  refuses({ PYTHON_FORWARD_URL: undefined }, /PYTHON_FORWARD_URL is not set/);
  refuses({ PYTHON_ANALYSIS_TOKEN: undefined }, /PYTHON_ANALYSIS_TOKEN is missing/);
  refuses({ PYTHON_ANALYSIS_TOKEN: "short" }, /PYTHON_ANALYSIS_TOKEN is missing/);
  refuses({ PYTHON_ANALYSIS_ENABLED: "false" }, /PYTHON_ANALYSIS_ENABLED must be true/);
  refuses({ PYTHON_ANALYSIS_URL: "https://py.example.com" }, /loopback HTTP origin/);
  refuses({ BINANCE_COLLECTOR_ENABLED: "false" }, /BINANCE_COLLECTOR_ENABLED must be true/);
  refuses({ SUPABASE_SERVICE_ROLE_KEY: undefined }, /SUPABASE_SERVICE_ROLE_KEY is required/);
});

test("names must be defined in .env.production.local itself, not inherited from .env.local", () => {
  assert.deepEqual(w.missingEnvFileNames(fileFor(good())), []);
  // A value present only in the merged environment (as if filled from .env.local) is not enough.
  const { SUPABASE_SERVICE_ROLE_KEY: _omitted, ...withoutKey } = good();
  refuses({}, /must define these names itself.*SUPABASE_SERVICE_ROLE_KEY/, fileFor(withoutKey));
  const missing = w.missingEnvFileNames("APP_PROFILE=production\n");
  assert.ok(missing.includes("PYTHON_ANALYSIS_TOKEN") && !missing.includes("APP_PROFILE"));
  // Comments, values mentioning a name, and prefixes do not count; export and spacing do.
  const text = [
    "# SUPABASE_SERVICE_ROLE_KEY=commented",
    "NOTE=SUPABASE_SERVICE_ROLE_KEY=inside a value",
    "X_SUPABASE_SERVICE_ROLE_KEY=prefixed",
    "export  APP_PROFILE = production",
    "OPERATIONAL_DB_ENABLED=",
  ].join("\r\n");
  const names = w.envFileNames(text);
  assert.ok(!names.has("SUPABASE_SERVICE_ROLE_KEY"));
  assert.ok(names.has("APP_PROFILE") && names.has("OPERATIONAL_DB_ENABLED"), "empty values still count as defined");
});

test("preflight refuses a service-role key copied into a VITE_ variable", () => {
  refuses({ VITE_SUPABASE_SERVICE_ROLE_KEY: "anything" }, /VITE_SUPABASE_SERVICE_ROLE_KEY exposes a server secret/);
  refuses({ VITE_SUPABASE_PUBLISHABLE_KEY: SERVICE_KEY }, /VITE_SUPABASE_PUBLISHABLE_KEY exposes a server secret/);
  refuses({ VITE_OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY: "x" }, /must remain server-only|exposes a server secret/);
});

test("preflight messages never contain secret values", () => {
  const secrets = [SERVICE_KEY, TOKEN, jwt("service_role") + "x"];
  const problems = run({
    VITE_SUPABASE_PUBLISHABLE_KEY: SERVICE_KEY,
    PYTHON_FORWARD_URL: "http://127.0.0.1:8000",
    APP_PROFILE: "local",
    OPERATIONAL_DB_ENABLED: "false",
    ALLOW_HOSTED_SUPABASE: "no",
  });
  assert.ok(problems.length >= 3);
  for (const message of problems) for (const secret of secrets) assert.ok(!message.includes(secret));
});

test("both FastAPI services are single-worker and keep the dev-local command", () => {
  const { analysis, forward } = w.pythonEndpoints(good());
  assert.deepEqual([analysis.port, forward.port], ["8000", "8001"]);
  const args = w.uvicornArgs(analysis);
  assert.equal(args[args.indexOf("--workers") + 1], "1");
  assert.deepEqual(args.slice(0, 3), ["-m", "uvicorn", "market_analysis.api:app"]);
  assert.equal(args[args.indexOf("--limit-concurrency") + 1], "32");
  assert.equal(w.pythonEndpoints({ ...good(), PYTHON_ANALYSIS_URL: "http://[::1]:9000" }).analysis.host, "::1");
  const env = w.pythonChildEnv({ ...good(), WEB_CONCURRENCY: "4", PATH: "/bin" });
  assert.equal(env.WEB_CONCURRENCY, undefined);
  assert.equal(env.PATH, "/bin");
  assert.equal(env.PYTHON_ANALYSIS_TOKEN, TOKEN);
  for (const name of ["SUPABASE_SERVICE_ROLE_KEY", "OPERATIONAL_SUPABASE_URL", "VITE_SUPABASE_URL"])
    assert.equal(env[name], undefined, `${name} must not reach FastAPI`);
});

test("backoff doubles from 2 s to a 5 minute cap", () => {
  let state = w.initialRestartState();
  let t = 0;
  const delays = [];
  // Crashes spaced 11 minutes apart never trip the breaker; each run is short, so no stable reset.
  for (let i = 0; i < 10; i += 1) {
    t += 11 * 60_000;
    const out = w.nextRestart(state, { nowMs: t, startedAtMs: t - 1_000 });
    assert.equal(out.decision.kind, "restart");
    delays.push(out.decision.delayMs);
    state = out.state;
  }
  assert.deepEqual(delays, [2, 4, 8, 16, 32, 64, 128, 256, 300, 300].map((s) => s * 1000));
});

test("ten stable minutes reset the backoff", () => {
  let state = { backoffMs: 128_000, crashes: [] };
  const justShort = w.nextRestart(state, { nowMs: 1_000_000, startedAtMs: 1_000_000 - 599_999 });
  assert.equal(justShort.decision.delayMs, 128_000);
  const stable = w.nextRestart(state, { nowMs: 1_000_000, startedAtMs: 1_000_000 - 600_000 });
  assert.equal(stable.decision.delayMs, 2_000);
  assert.equal(stable.state.backoffMs, 4_000);
});

test("five crashes within ten minutes open the breaker; slower crashes do not", () => {
  let state = w.initialRestartState();
  const kinds = [];
  for (const nowMs of [0, 10_000, 30_000, 60_000, 100_000]) {
    const out = w.nextRestart(state, { nowMs, startedAtMs: nowMs - 500 });
    kinds.push(out.decision.kind);
    state = out.state;
  }
  assert.deepEqual(kinds, ["restart", "restart", "restart", "restart", "crash-loop"]);

  // Four crashes in the window, then a fifth exactly 10 minutes after the first: the first has aged out.
  state = w.initialRestartState();
  let last;
  for (const nowMs of [0, 100_000, 200_000, 300_000, 600_000]) {
    last = w.nextRestart(state, { nowMs, startedAtMs: nowMs - 500 });
    state = last.state;
  }
  assert.equal(last.decision.kind, "restart");
  assert.equal(state.crashes.length, 4);

  // A crash after a stable run does not count old crashes: they are older than the window.
  const after = w.nextRestart({ backoffMs: 8_000, crashes: [0, 1_000, 2_000, 3_000] }, { nowMs: 700_000, startedAtMs: 100_000 });
  assert.equal(after.decision.kind, "restart");
  assert.deepEqual(after.state.crashes, [700_000]);
});

test("the collector never enters the permanent stop; it retries at the cap", () => {
  let state = w.initialRestartState();
  const delays = [];
  for (const nowMs of [0, 10_000, 30_000, 60_000, 100_000, 200_000, 400_000]) {
    const out = w.nextRestart(state, { nowMs, startedAtMs: nowMs - 500 }, w.RESTART_POLICY, { neverGiveUp: true });
    assert.equal(out.decision.kind, "restart");
    delays.push(out.decision.delayMs / 1000);
    state = out.state;
  }
  assert.deepEqual(delays, [2, 4, 8, 16, 300, 300, 300]);
  // A stable run still resets it.
  const reset = w.nextRestart(state, { nowMs: 5_000_000, startedAtMs: 5_000_000 - 600_000 }, w.RESTART_POLICY, { neverGiveUp: true });
  assert.equal(reset.decision.delayMs, 2_000);
  // Without the option the same sequence still stops (other children).
  let other = w.initialRestartState();
  let kind;
  for (const nowMs of [0, 10_000, 30_000, 60_000, 100_000]) {
    const out = w.nextRestart(other, { nowMs, startedAtMs: nowMs - 500 });
    other = out.state;
    kind = out.decision.kind;
  }
  assert.equal(kind, "crash-loop");
});

test("exit rules: both forward jobs looping, collector looping, or everything down for a minute", () => {
  const set = (states) => Object.entries(states).map(([name, state]) => ({ name, state }));
  const up = { "fastapi-analysis": "running", "fastapi-forward": "running", collector: "running", "forward-hourly": "running", "forward-trend": "running" };
  assert.deepEqual(w.exitDecision(set(up), null, 1000), { reason: null, allDownSinceMs: null });
  assert.equal(w.exitDecision(set({ ...up, "forward-hourly": "crash-loop" }), null, 0).reason, null, "one forward job alone does not stop the host");
  assert.match(w.exitDecision(set({ ...up, "forward-hourly": "crash-loop", "forward-trend": "crash-loop" }), null, 0).reason, /both forward jobs/);
  assert.match(w.exitDecision(set({ ...up, collector: "crash-loop" }), null, 0).reason, /collector/);
  assert.equal(w.exitDecision(set({ ...up, collector: "backoff" }), null, 0).reason, null, "a collector in backoff keeps the host alive");
  const down = Object.fromEntries(Object.keys(up).map((n) => [n, "backoff"]));
  const first = w.exitDecision(set(down), null, 10_000);
  assert.deepEqual(first, { reason: null, allDownSinceMs: 10_000 });
  assert.equal(w.exitDecision(set(down), first.allDownSinceMs, 69_999).reason, null);
  assert.match(w.exitDecision(set(down), first.allDownSinceMs, 70_000).reason, /every child has been down/);
  assert.equal(w.exitDecision(set({ ...down, collector: "running" }), 10_000, 80_000).allDownSinceMs, null, "any child up resets the timer");
});

test("a previous run's pids are found unless the file predates the boot", () => {
  const status = w.buildStatus({
    supervisorPid: 100, startedAtMs: 0, nowMs: Date.parse("2026-10-10T10:00:00Z"), shuttingDown: false,
    children: [
      { name: "collector", state: "running", pid: 201, restartCount: 0, lastRestartAt: null, lastExit: null, nextRestartAt: null },
      { name: "forward-trend", state: "backoff", pid: null, restartCount: 1, lastRestartAt: null, lastExit: "exit code 1", nextRestartAt: null },
    ],
  });
  const boot = Date.parse("2026-10-10T08:00:00Z");
  assert.deepEqual(w.recordedPids(status, boot), [{ label: "worker host", pid: 100 }, { label: "collector", pid: 201 }]);
  assert.deepEqual(w.recordedPids(status, Date.parse("2026-10-10T11:00:00Z")), [], "pids from before a reboot are not ours");
  assert.deepEqual(w.recordedPids(null, boot), []);
});

test("the status file is written through a temporary file and renamed", () => {
  const files = new Map();
  const ops = [];
  const fs = {
    writeFile: (path, data) => (ops.push(`write ${path}`), files.set(path, data)),
    rename: (from, to) => (ops.push(`rename ${from} -> ${to}`), files.set(to, files.get(from)), files.delete(from)),
    remove: (path) => (ops.push(`remove ${path}`), files.delete(path)),
  };
  const status = w.buildStatus({
    supervisorPid: 42,
    startedAtMs: Date.parse("2026-10-10T00:00:00Z"),
    nowMs: Date.parse("2026-10-10T00:01:00Z"),
    shuttingDown: false,
    children: [{ name: "collector", state: "running", pid: 7, restartCount: 2, lastRestartAt: "2026-10-10T00:00:30.000Z", lastExit: "exit code 1", nextRestartAt: null }],
  });
  w.writeStatusAtomic(fs, "/r/.worker-host.status.json", status, 42);
  assert.deepEqual(ops, ["write /r/.worker-host.status.json.42.tmp", "rename /r/.worker-host.status.json.42.tmp -> /r/.worker-host.status.json"]);
  assert.deepEqual([...files.keys()], ["/r/.worker-host.status.json"]);
  const parsed = JSON.parse(files.get("/r/.worker-host.status.json"));
  assert.equal(parsed.updatedAt, "2026-10-10T00:01:00.000Z");
  assert.equal(parsed.children[0].restartCount, 2);
  assert.equal(parsed.children[0].lastRestartAt, "2026-10-10T00:00:30.000Z");

  // A failed rename leaves the previous file intact and removes the temporary one.
  const failing = { ...fs, rename: () => { throw new Error("EPERM"); } };
  assert.throws(() => w.writeStatusAtomic(failing, "/r/.worker-host.status.json", status, 42), /EPERM/);
  assert.equal(ops.at(-1), "remove /r/.worker-host.status.json.42.tmp");
  assert.ok(files.has("/r/.worker-host.status.json"));
});

test("mvp:check worker host line: missing WARN, fresh+running PASS, stale or crash loop FAIL", () => {
  const now = Date.parse("2026-10-10T12:00:00Z");
  const child = (name, state, restartCount = 0) => ({ name, state, pid: 1, restartCount, lastRestartAt: null, lastExit: null, nextRestartAt: null });
  const file = (ageMs, children, extra = {}) =>
    JSON.stringify(w.buildStatus({ supervisorPid: 1, startedAtMs: now - 3_600_000, nowMs: now - ageMs, shuttingDown: false, children, ...extra }));
  const running = ["a", "b", "c", "d", "e"].map((n) => child(n, "running"));

  assert.equal(w.classifyWorkerHost(null, now).level, "WARN");
  assert.equal(w.classifyWorkerHost(file(0, running), now).level, "PASS");
  assert.equal(w.classifyWorkerHost(file(120_000, running), now).level, "PASS", "exactly 2 minutes is still fresh");
  const stale = w.classifyWorkerHost(file(120_001, running), now);
  assert.equal(stale.level, "FAIL");
  assert.match(stale.detail, /stale/);
  const loop = w.classifyWorkerHost(file(1_000, [...running.slice(1), child("a", "crash-loop")]), now);
  assert.equal(loop.level, "FAIL");
  assert.match(loop.detail, /crash loop.*a/);
  const backoff = w.classifyWorkerHost(file(1_000, [...running.slice(1), child("a", "backoff")]), now);
  assert.equal(backoff.level, "WARN");
  assert.match(backoff.detail, /a backoff/);
  assert.equal(w.classifyWorkerHost(file(1_000, [child("a", "waiting")]), now).level, "WARN");
  // Down for an hour (the collector retrying at the cap) fails; 59 minutes is still a WARN.
  const downFor = (ms) => [...running.slice(1), { ...child("a", "backoff"), downSince: new Date(now - ms).toISOString() }];
  assert.equal(w.classifyWorkerHost(file(1_000, downFor(59 * 60_000)), now).level, "WARN");
  const longDown = w.classifyWorkerHost(file(1_000, downFor(3_600_000)), now);
  assert.equal(longDown.level, "FAIL");
  assert.match(longDown.detail, /more than an hour: a/);
  assert.equal(w.classifyWorkerHost("{not json", now).level, "FAIL");
  assert.equal(w.classifyWorkerHost(file(1_000, []), now).level, "FAIL");
  assert.equal(w.classifyWorkerHost(file(-200_000, running), now).level, "FAIL", "a future timestamp is not fresh");
});

test("the status parser rejects other shapes and the exit text names signal or code", () => {
  assert.equal(w.parseStatus("null"), null);
  assert.equal(w.parseStatus(JSON.stringify({ schema: 2 })), null);
  assert.equal(w.describeExit(1, null), "exit code 1");
  assert.equal(w.describeExit(null, "SIGKILL"), "signal SIGKILL");
});


test("gateway is opt-in and requires its own file names when enabled", () => {
  assert.deepEqual(run({ RUNNER_GATEWAY_SECRET: "REPLACE_WITH_RANDOM_32_PLUS_CHARS" }), []);
  const gateway = {
    RUNNER_GATEWAY_ENABLED: "true", RUNNER_GATEWAY_PORT: "8787",
    RUNNER_GATEWAY_SECRET: "x".repeat(40), SUPABASE_PUBLISHABLE_KEY: jwt("anon"),
  };
  assert.deepEqual(run(gateway), []);
  for (const name of Object.keys(gateway)) {
    const env = { ...good(), ...gateway };
    delete env[name];
    refuses(gateway, new RegExp(name), fileFor(env));
  }
  refuses({ ...gateway, RUNNER_GATEWAY_PORT: "1000" }, /RUNNER_GATEWAY_PORT/);
  refuses({ ...gateway, RUNNER_GATEWAY_SECRET: "REPLACE_WITH_RANDOM_32_PLUS_CHARS" }, /RUNNER_GATEWAY_SECRET/);
});

test("runner jobs require only the existing main Supabase credentials when enabled", () => {
  for (const flag of [undefined, "false", "TRUE", "1", "true"])
    assert.deepEqual(run({ RUNNER_JOBS_ENABLED: flag, RUNNER_GATEWAY_ENABLED: "false" }), []);
  for (const name of ["SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"])
    refuses({ RUNNER_JOBS_ENABLED: "true", [name]: undefined }, new RegExp(`${name} is required for runner jobs`));
  const { SUPABASE_SERVICE_ROLE_KEY: _omitted, ...withoutKey } = good();
  refuses({ RUNNER_JOBS_ENABLED: "true" }, /must define these names itself.*SUPABASE_SERVICE_ROLE_KEY/, fileFor(withoutKey));
  assert.equal(w.ESSENTIAL_CHILD, "collector");
  assert.deepEqual(w.FORWARD_CHILDREN, ["forward-hourly", "forward-trend"]);
});

// ── Monitor scheduler (#30) ──────────────────────────────────────────────────────────────────────

test("monitor scheduler: preflight validates its flag and interval; the child needs hosted credentials and the operational DB", () => {
  assert.deepEqual(run({ MONITOR_SCHEDULER_ENABLED: "false", MONITOR_SCHEDULER_INTERVAL_SECONDS: "30" }), []);
  assert.deepEqual(run({ MONITOR_SCHEDULER_INTERVAL_SECONDS: "3600" }), []);
  refuses({ MONITOR_SCHEDULER_ENABLED: "yes" }, /^MONITOR_SCHEDULER_ENABLED must be true or false\.$/);
  for (const bad of ["29", "3601", "60.5", "abc", "-60"])
    refuses({ MONITOR_SCHEDULER_INTERVAL_SECONDS: bad }, /^MONITOR_SCHEDULER_INTERVAL_SECONDS must be a whole number from 30 to 3600\.$/);
  assert.equal(w.monitorSchedulerConfigured(good()), true);
  assert.equal(w.monitorSchedulerConfigured({ ...good(), MONITOR_SCHEDULER_ENABLED: "false" }), false);
  assert.equal(w.monitorSchedulerConfigured({ ...good(), OPERATIONAL_DB_ENABLED: "false" }), false);
  assert.equal(w.monitorSchedulerConfigured({ ...good(), SUPABASE_SERVICE_ROLE_KEY: "" }), false);
  assert.equal(w.monitorSchedulerIntervalMs({}), 60_000);
  assert.equal(w.monitorSchedulerIntervalMs({ MONITOR_SCHEDULER_INTERVAL_SECONDS: "45" }), 45_000);
  assert.throws(() => w.monitorSchedulerIntervalMs({ MONITOR_SCHEDULER_INTERVAL_SECONDS: "10" }), /MONITOR_SCHEDULER_INTERVAL_SECONDS/);
});

test("monitor scheduler: passes sit at the boundary plus 8 s, never overlap and never burst after an overrun", () => {
  const minute = Date.parse("2026-10-16T12:00:00Z");
  // First pass: the next minute boundary plus the settle.
  assert.equal(w.nextMonitorPassAt(minute + 1_000, null, 60_000), minute + 8_000);
  assert.equal(w.nextMonitorPassAt(minute + 9_000, null, 60_000), minute + 68_000);
  // A pass that started a little late is followed by the next grid instant, not one a minute later still.
  assert.equal(w.nextMonitorPassAt(minute + 8_400, minute + 8_000, 60_000), minute + 68_000);
  // A pass that overran its slot waits for the following instant (no catch-up burst).
  assert.equal(w.nextMonitorPassAt(minute + 75_000, minute + 8_000, 60_000), minute + 128_000);
  // Sub-minute intervals sit on their own grid; longer ones round up to whole minutes.
  assert.equal(w.nextMonitorPassAt(minute + 9_000, minute + 8_000, 30_000), minute + 38_000);
  assert.equal(w.nextMonitorPassAt(minute + 9_000, minute + 8_000, 90_000), minute + 128_000);
  assert.equal(w.MONITOR_SCHEDULER_SETTLE_MS, 8_000);
});
