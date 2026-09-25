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

test("the built worker reads its collector configuration at runtime", async () => {
  const root = fileURLToPath(new URL("..", import.meta.url));
  const artifact = buildWorkerArtifact(root);

  // The tracked production build bakes BINANCE_COLLECTOR_ENABLED=false. A runtime
  // override must win; otherwise enablement was bound at build time.
  const enabled = runWorker(artifact, { BINANCE_COLLECTOR_ENABLED: "true" });
  assert.equal(enabled.status, 1, enabled.stderr);
  assert.match(enabled.stderr, /requires OPERATIONAL_DB_ENABLED=true/);

  // Operational database configuration is also read at runtime, not baked.
  const operational = runWorker(artifact, {
    BINANCE_COLLECTOR_ENABLED: "true",
    OPERATIONAL_DB_ENABLED: "true",
  });
  assert.equal(operational.status, 1, operational.stderr);
  assert.match(
    operational.stderr,
    /OPERATIONAL_SUPABASE_URL and OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY are required/,
  );

  const invalid = runWorker(artifact, { BINANCE_COLLECTOR_ENABLED: "yes" });
  assert.equal(invalid.status, 1, invalid.stderr);
  assert.match(invalid.stderr, /BINANCE_COLLECTOR_ENABLED must be true or false/);

  const output = readFileSync(artifact, "utf8");
  // Vite replaces every import.meta.env reference with the build-time public
  // values; server configuration must still be read from process.env.
  assert.doesNotMatch(output, /import\.meta/);
  assert.match(output, /process\.env/);
  for (const name of [
    "BINANCE_COLLECTOR_ENABLED",
    "OPERATIONAL_DB_ENABLED",
    "OPERATIONAL_SUPABASE_URL",
    "OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY",
    "MOVEMENT_FINALIZATION_GRACE_MS",
  ]) {
    assert.match(output, new RegExp(`"${name}"`), `${name} must stay runtime-configurable`);
  }
});

test("the enabled built worker validates the runtime server environment", async () => {
  const root = fileURLToPath(new URL("..", import.meta.url));
  const artifact = buildWorkerArtifact(root);

  // A local fake operational Supabase answers the lease RPC so the enabled path
  // reaches its first supabaseAdmin use. No real service is contacted, and the
  // WebSocket connect is never reached because validation fails first.
  const server = createServer((request, response) => {
    const isLease = String(request.url).includes("claim_collector_lease");
    request.resume();
    response.writeHead(200, { "content-type": "application/json" });
    response.end(isLease ? "true" : "[]");
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();

  const child = spawn(process.execPath, [artifact], {
    env: {
      PATH: process.env.PATH,
      HOME: process.env.HOME,
      BINANCE_COLLECTOR_ENABLED: "true",
      OPERATIONAL_DB_ENABLED: "true",
      OPERATIONAL_SUPABASE_URL: `http://127.0.0.1:${port}`,
      OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY: "test-service-role",
      // No APP_PROFILE/SUPABASE_URL: the runtime environment is incomplete.
    },
    stdio: ["ignore", "ignore", "pipe"],
  });
  let stderr = "";
  child.stderr.on("data", (chunk) => (stderr += chunk));
  try {
    await new Promise((resolve) => setTimeout(resolve, 1_500));
    // validateServerEnvironment() cross-checks the runtime process.env against the
    // build-time public VITE_* values, so an incomplete host environment is surfaced.
    assert.match(stderr, /\[Environment\] APP_PROFILE must be local or production/);
    assert.match(stderr, /startup unavailable/);
  } finally {
    child.kill("SIGKILL");
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
});

test("local development starts the collector as a separate process", async () => {
  const devLocal = await readFile(new URL("../scripts/dev-local.mjs", import.meta.url), "utf8");
  assert.match(devLocal, /collector:worker/);
  assert.match(devLocal, /start\(\s*"collector"/);
});
