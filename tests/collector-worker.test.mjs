import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import ts from "../node_modules/typescript/lib/typescript.js";

const stub = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;

// The worker entrypoint is transpiled with its collector dependency replaced, so the
// lifecycle can be exercised without a database, network, or real collector.
async function loadWorker(collectorStubSource) {
  const source = await readFile(
    new URL("../src/lib/market/collector-worker.server.ts", import.meta.url),
    "utf8",
  );
  let { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  });
  outputText = outputText.replaceAll(
    JSON.stringify("./collector.server"),
    JSON.stringify(stub(collectorStubSource)),
  );
  return import(stub(outputText));
}

let builtArtifact;
function buildWorkerArtifact(root) {
  if (builtArtifact) return builtArtifact; // one vite build per file: every test reads the same artifact
  const build = spawnSync(
    process.execPath,
    ["node_modules/vite/bin/vite.js", "build", "--config", "vite.collector-worker.config.ts"],
    { cwd: root, encoding: "utf8" },
  );
  assert.equal(build.status, 0, build.stderr);
  builtArtifact = join(root, "dist/collector-worker/collector-worker.mjs");
  return builtArtifact;
}

// A minimal environment proves the artifact reads runtime configuration instead of
// inheriting values that happen to be set in the test process.
function runWorker(artifact, env) {
  return spawnSync(process.execPath, [artifact], {
    env: { PATH: process.env.PATH, HOME: process.env.HOME, ...env },
    encoding: "utf8",
  });
}

function captureShutdown() {
  let shutdown;
  let resolveExit;
  const exited = new Promise((resolve) => {
    resolveExit = resolve;
  });
  let exitCode;
  return {
    options: {
      registerSignals: (fn) => {
        shutdown = fn;
      },
      exit: (code) => {
        exitCode = code;
        resolveExit();
      },
    },
    get shutdown() {
      return shutdown;
    },
    exited,
    get exitCode() {
      return exitCode;
    },
  };
}

test("the worker starts through startBinanceCollector and releases on shutdown", async () => {
  const module = await loadWorker(`
    globalThis.__collectorWorkerCalls = [];
    export function startBinanceCollector() {
      globalThis.__collectorWorkerCalls.push("start");
      return { stop: async () => { globalThis.__collectorWorkerCalls.push("stop"); } };
    }
  `);
  const capture = captureShutdown();
  const runtime = module.startCollectorWorker(capture.options);
  assert.ok(runtime, "worker returns the running runtime handle");
  assert.deepEqual(globalThis.__collectorWorkerCalls, ["start"]);
  assert.equal(typeof capture.shutdown, "function");
  capture.shutdown(0);
  await capture.exited;
  assert.deepEqual(globalThis.__collectorWorkerCalls, ["start", "stop"]);
  assert.equal(capture.exitCode, 0);
});

test("the worker is a no-op when the collector is disabled", async () => {
  const module = await loadWorker(`export function startBinanceCollector() { return null; }`);
  let registered = false;
  const runtime = module.startCollectorWorker({
    registerSignals: () => {
      registered = true;
    },
    exit: () => {
      throw new Error("a disabled worker must not exit");
    },
  });
  assert.equal(runtime, null);
  assert.equal(registered, false);
});

test("a shutdown failure still exits, with a failure code", async () => {
  const module = await loadWorker(`
    export function startBinanceCollector() {
      return { stop: async () => { throw new Error("lease release failed"); } };
    }
  `);
  const capture = captureShutdown();
  const originalError = console.error;
  console.error = () => {};
  try {
    module.startCollectorWorker(capture.options);
    capture.shutdown(0);
    await capture.exited;
  } finally {
    console.error = originalError;
  }
  assert.equal(capture.exitCode, 1);
});

test("the worker wires SIGTERM and SIGINT by default", async () => {
  const module = await loadWorker(`
    export function startBinanceCollector() { return { stop: async () => {} }; }
  `);
  const beforeTerm = process.listeners("SIGTERM").length;
  const beforeInt = process.listeners("SIGINT").length;
  const runtime = module.startCollectorWorker({ exit: () => {} });
  assert.ok(runtime);
  const termListeners = process.listeners("SIGTERM").slice(beforeTerm);
  const intListeners = process.listeners("SIGINT").slice(beforeInt);
  try {
    assert.equal(termListeners.length, 1);
    assert.equal(intListeners.length, 1);
  } finally {
    for (const listener of termListeners) process.removeListener("SIGTERM", listener);
    for (const listener of intListeners) process.removeListener("SIGINT", listener);
  }
});

test("the production worker artifact builds and runs with plain node", async () => {
  const root = fileURLToPath(new URL("..", import.meta.url));
  const artifact = buildWorkerArtifact(root);
  const output = readFileSync(artifact, "utf8");
  const externalImports = [...output.matchAll(/^import .* from "([^"]+)"/gm)].map(
    (match) => match[1],
  );
  // Self-contained: no bare package specifiers, so no devDependency (Vite) is
  // needed at runtime.
  assert.deepEqual(externalImports, ["node:crypto"]);
  const run = runWorker(artifact, { BINANCE_COLLECTOR_ENABLED: "false" });
  assert.equal(run.status, 0, run.stderr);
  assert.match(run.stdout, /no collector to run/);
});

// A complete worker configuration. The collector owns operational working state
// and delegates movement buckets to the configured Python service.
function workerEnv(overrides = {}) {
  return {
    OPERATIONAL_DB_ENABLED: "true",
    OPERATIONAL_SUPABASE_URL: "http://127.0.0.1:55321",
    OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY: "test-operational-service-role",
    PYTHON_ANALYSIS_ENABLED: "true",
    PYTHON_ANALYSIS_URL: "http://127.0.0.1:8000",
    PYTHON_ANALYSIS_TOKEN: "test-python-analysis-service-token-123456",
    BINANCE_COLLECTOR_ENABLED: "true",
    ...overrides,
  };
}

function omitWorkerEnv(key) {
  const env = workerEnv();
  delete env[key];
  return env;
}

test("the built worker reads its collector configuration at runtime", async () => {
  const root = fileURLToPath(new URL("..", import.meta.url));
  const artifact = buildWorkerArtifact(root);

  // The tracked production build bakes BINANCE_COLLECTOR_ENABLED=false. A runtime
  // override must win; otherwise enablement was bound at build time. The worker
  // needs no main-database credential, so the first missing requirement is the
  // operational database, never a Lovable Supabase value.
  const enabled = runWorker(artifact, { BINANCE_COLLECTOR_ENABLED: "true" });
  assert.equal(enabled.status, 1, enabled.stderr);
  assert.match(enabled.stderr, /requires OPERATIONAL_DB_ENABLED=true/);

  const invalid = runWorker(artifact, { BINANCE_COLLECTOR_ENABLED: "yes" });
  assert.equal(invalid.status, 1, invalid.stderr);
  assert.match(invalid.stderr, /BINANCE_COLLECTOR_ENABLED must be true or false/);

  const output = readFileSync(artifact, "utf8");
  // Server configuration must be read from process.env, and the artifact must not
  // carry a build-time browser environment object to compare against.
  assert.match(output, /process\.env/);
  assert.doesNotMatch(output, /"VITE_SUPABASE_URL":/);
  assert.doesNotMatch(output, /"VITE_APP_PROFILE":/);
  for (const name of [
    "BINANCE_COLLECTOR_ENABLED",
    "OPERATIONAL_DB_ENABLED",
    "OPERATIONAL_SUPABASE_URL",
    "OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY",
    "MOVEMENT_FINALIZATION_GRACE_MS",
  ]) {
    assert.match(output, new RegExp(`"${name}"`), `${name} must stay runtime-configurable`);
  }
  // The worker owns no Lovable state, so its artifact must carry no main-database
  // credential requirement and no application-table name.
  for (const forbidden of [
    '"SUPABASE_SERVICE_ROLE_KEY"',
    '"SUPABASE_PUBLISHABLE_KEY"',
    "watchlist_items",
    "monitor_settings",
    "ta_signals",
    "supabaseAdmin",
    "validateServerEnvironment",
    "validateServerSupabase",
  ]) {
    assert.equal(
      output.includes(forbidden),
      false,
      `${forbidden} must not appear in the collector artifact`,
    );
  }
});

test("invalid or missing worker configuration fails visibly", async () => {
  const root = fileURLToPath(new URL("..", import.meta.url));
  const artifact = buildWorkerArtifact(root);

  const cases = [
    [{ BINANCE_COLLECTOR_ENABLED: "true" }, /requires OPERATIONAL_DB_ENABLED=true/],
    [
      workerEnv({ OPERATIONAL_DB_ENABLED: "maybe" }),
      /OPERATIONAL_DB_ENABLED must be true or false/,
    ],
    [
      omitWorkerEnv("OPERATIONAL_SUPABASE_URL"),
      /OPERATIONAL_SUPABASE_URL and OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY are required/,
    ],
    [
      omitWorkerEnv("OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY"),
      /OPERATIONAL_SUPABASE_URL and OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY are required/,
    ],
    [
      omitWorkerEnv("PYTHON_ANALYSIS_TOKEN"),
      /requires PYTHON_ANALYSIS_ENABLED=true and a valid Python service URL\/token/,
    ],
    [
      workerEnv({ MOVEMENT_FINALIZATION_GRACE_MS: "999999" }),
      /MOVEMENT_FINALIZATION_GRACE_MS must be an integer/,
    ],
  ];
  for (const [env, expected] of cases) {
    const run = runWorker(artifact, env);
    assert.equal(run.status, 1, `expected failure for ${JSON.stringify(env)}`);
    assert.match(run.stderr, expected);
  }
});

async function startFakeServer(handler) {
  const server = createServer((request, response) => {
    request.resume();
    handler(response, request);
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return { server, port: server.address().port };
}

function stopFakeServer(server) {
  server.closeAllConnections();
  return new Promise((resolve) => server.close(resolve));
}

test("the built collector artifact runs with operational credentials only", async () => {
  const root = fileURLToPath(new URL("..", import.meta.url));
  const artifact = buildWorkerArtifact(root);

  // The fake operational Supabase declines the lease, so the worker validates its
  // runtime configuration, stays in standby, and never opens a market stream. No
  // real service is contacted.
  const operational = await startFakeServer((response) => {
    response.writeHead(200, { "content-type": "application/json" });
    response.end("false");
  });

  const child = spawn(process.execPath, [artifact], {
    env: {
      PATH: process.env.PATH,
      HOME: process.env.HOME,
      ...workerEnv({ OPERATIONAL_SUPABASE_URL: `http://127.0.0.1:${operational.port}` }),
      // Deliberately mismatched browser values must be ignored by the worker.
      VITE_APP_PROFILE: "production",
      VITE_SUPABASE_URL: "https://elsewhere.example",
      VITE_SUPABASE_PUBLISHABLE_KEY: "sb_publishable_other",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (chunk) => (stdout += chunk));
  child.stderr.on("data", (chunk) => (stderr += chunk));
  try {
    await new Promise((resolve) => setTimeout(resolve, 1_000));
    assert.match(stdout, /collector started/);
    // No APP_PROFILE, SUPABASE_URL, or SUPABASE_SERVICE_ROLE_KEY was provided, yet
    // the worker validated and started: its boundary is operational-only, and the
    // mismatched VITE_* build must never surface as an environment error.
    assert.doesNotMatch(stderr, /\[Environment\]/);
    assert.doesNotMatch(stderr, /SUPABASE_SERVICE_ROLE_KEY/);
    assert.equal(child.exitCode, null, "the worker must keep running");
  } finally {
    child.kill("SIGKILL");
    await stopFakeServer(operational.server);
  }
});

test("the built worker reads its subscription universe from the operational database", async () => {
  const root = fileURLToPath(new URL("..", import.meta.url));
  const artifact = buildWorkerArtifact(root);

  // The fake operational Supabase grants the lease but fails the subscription
  // universe read. The worker surfaces it and stays in standby: it never opens a
  // market stream and never contacts a main Lovable database.
  const operational = await startFakeServer((response, request) => {
    const isLease = String(request.url).includes("claim_collector_lease");
    response.writeHead(isLease ? 200 : 400, { "content-type": "application/json" });
    response.end(
      isLease ? "true" : JSON.stringify({ message: "universe unavailable", code: "400" }),
    );
  });

  const child = spawn(process.execPath, [artifact], {
    env: {
      PATH: process.env.PATH,
      HOME: process.env.HOME,
      ...workerEnv({ OPERATIONAL_SUPABASE_URL: `http://127.0.0.1:${operational.port}` }),
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stderr = "";
  child.stderr.on("data", (chunk) => (stderr += chunk));
  try {
    await new Promise((resolve) => setTimeout(resolve, 1_500));
    assert.match(
      stderr,
      /startup unavailable: Operational database collector subscription read failed/,
    );
    assert.equal(child.exitCode, null, "the worker must keep running");
  } finally {
    child.kill("SIGKILL");
    await stopFakeServer(operational.server);
  }
});

