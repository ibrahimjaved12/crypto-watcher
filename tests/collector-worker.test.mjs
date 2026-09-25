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

function buildWorkerArtifact(root) {
  const build = spawnSync(
    process.execPath,
    ["node_modules/vite/bin/vite.js", "build", "--config", "vite.collector-worker.config.ts"],
    { cwd: root, encoding: "utf8" },
  );
  assert.equal(build.status, 0, build.stderr);
  return join(root, "dist/collector-worker/collector-worker.mjs");
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

test("the TanStack application server no longer starts the collector", async () => {
  const server = await readFile(new URL("../src/server.ts", import.meta.url), "utf8");
  assert.doesNotMatch(server, /collector\.server/);
  assert.doesNotMatch(server, /startBinanceCollector|startCollectorWorker/);
});

test("the worker entrypoint reuses the shared collector startup path", async () => {
  const worker = await readFile(
    new URL("../src/lib/market/collector-worker.server.ts", import.meta.url),
    "utf8",
  );
  assert.match(worker, /from "\.\/collector\.server"/);
  assert.match(worker, /startBinanceCollector/);
  const entry = await readFile(
    new URL("../src/worker/collector-worker.ts", import.meta.url),
    "utf8",
  );
  assert.match(entry, /from "\.\.\/lib\/market\/collector-worker\.server"/);
  assert.match(entry, /startCollectorWorker\(\)/);
});

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

test("package.json and the launcher expose a separate collector worker", async () => {
  const pkg = JSON.parse(await readFile(new URL("../package.json", import.meta.url), "utf8"));
  assert.equal(pkg.scripts["collector:worker"], "node scripts/collector-worker.mjs");
  assert.equal(
    pkg.scripts["collector:worker:build"],
    "vite build --config vite.collector-worker.config.ts",
  );
  assert.equal(
    pkg.scripts["collector:worker:start"],
    "node dist/collector-worker/collector-worker.mjs",
  );
  const launcher = await readFile(
    new URL("../scripts/collector-worker.mjs", import.meta.url),
    "utf8",
  );
  assert.match(launcher, /src\/worker\/collector-worker\.ts/);
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

// A complete server-only worker configuration. No browser VITE_* value appears.
function workerEnv(overrides = {}) {
  return {
    APP_PROFILE: "local",
    SUPABASE_URL: "http://127.0.0.1:54321",
    SUPABASE_SERVICE_ROLE_KEY: "test-service-role",
    OPERATIONAL_DB_ENABLED: "true",
    OPERATIONAL_SUPABASE_URL: "http://127.0.0.1:55321",
    OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY: "test-operational-service-role",
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
  // override must win; otherwise enablement was bound at build time. Validation is
  // server-only, so the first missing requirement is APP_PROFILE, not a VITE_* value.
  const enabled = runWorker(artifact, { BINANCE_COLLECTOR_ENABLED: "true" });
  assert.equal(enabled.status, 1, enabled.stderr);
  assert.match(enabled.stderr, /APP_PROFILE must be local or production/);

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
    "SUPABASE_SERVICE_ROLE_KEY",
  ]) {
    assert.match(output, new RegExp(`"${name}"`), `${name} must stay runtime-configurable`);
  }
});

test("invalid or missing worker configuration fails visibly", async () => {
  const root = fileURLToPath(new URL("..", import.meta.url));
  const artifact = buildWorkerArtifact(root);

  const cases = [
    [{ BINANCE_COLLECTOR_ENABLED: "true" }, /APP_PROFILE must be local or production/],
    [omitWorkerEnv("SUPABASE_SERVICE_ROLE_KEY"), /SUPABASE_SERVICE_ROLE_KEY is required/],
    [omitWorkerEnv("OPERATIONAL_DB_ENABLED"), /requires OPERATIONAL_DB_ENABLED=true/],
    [
      omitWorkerEnv("OPERATIONAL_SUPABASE_URL"),
      /OPERATIONAL_SUPABASE_URL and OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY are required/,
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
    handler(response);
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return { server, port: server.address().port };
}

function stopFakeServer(server) {
  server.closeAllConnections();
  return new Promise((resolve) => server.close(resolve));
}

test("the built collector artifact is configurable without VITE_* values", async () => {
  const root = fileURLToPath(new URL("..", import.meta.url));
  const artifact = buildWorkerArtifact(root);

  // The fake operational Supabase grants the lease, so the worker proceeds to its
  // first supabaseAdmin use and would run the app's browser cross-check if it still
  // depended on it. The fake main Supabase answers 401, so the watchlist read fails
  // fast with an HTTP error — no real service is contacted.
  const operational = await startFakeServer((response) => {
    response.writeHead(200, { "content-type": "application/json" });
    response.end("true");
  });
  const main = await startFakeServer((response) => {
    response.writeHead(401, { "content-type": "application/json" });
    response.end(JSON.stringify({ message: "invalid api key", code: "401" }));
  });

  const child = spawn(process.execPath, [artifact], {
    env: {
      PATH: process.env.PATH,
      HOME: process.env.HOME,
      ...workerEnv({
        SUPABASE_URL: `http://127.0.0.1:${main.port}`,
        OPERATIONAL_SUPABASE_URL: `http://127.0.0.1:${operational.port}`,
      }),
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
    await new Promise((resolve) => setTimeout(resolve, 1_500));
    assert.match(stdout, /collector started/);
    // Reaching the watchlist read proves the server-only configuration passed and
    // supabaseAdmin was created. The mismatched VITE_* build must not surface as the
    // application's browser/server consistency error.
    assert.match(stderr, /Collector watchlist read failed/);
    assert.doesNotMatch(stderr, /\[Environment\]/);
    assert.equal(child.exitCode, null, "the worker must keep running");
  } finally {
    child.kill("SIGKILL");
    await Promise.all([stopFakeServer(operational.server), stopFakeServer(main.server)]);
  }
});

test("local development starts the collector as a separate process", async () => {
  const devLocal = await readFile(new URL("../scripts/dev-local.mjs", import.meta.url), "utf8");
  assert.match(devLocal, /collector:worker/);
  assert.match(devLocal, /start\(\s*"collector"/);
});
