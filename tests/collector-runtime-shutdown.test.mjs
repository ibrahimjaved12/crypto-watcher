import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

const stub = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;

const movementStub = `
  export class MovementEngineRuntime {
    constructor() {}
    async start() {}
    requestNormalizationHistoryRefresh() {}
    async stop() {
      globalThis.__collectorStopOrder.push("movement:start");
      await new Promise((resolve) => setTimeout(resolve, 10));
      globalThis.__collectorStopOrder.push("movement:end");
    }
  }
`;

const replacements = {
  "../../integrations/supabase/client.server": `export const supabaseAdmin = {};`,
  "../operational/repository.server": `export function getOperationalStore() { return undefined; }`,
  "../monitor/run-context": `export function createMonitorRunContext() { return {}; }`,
  "../monitor/engine.server": `export const DEFAULT_SETTINGS = {};`,
  "../ta/engine.server": `export async function runTA() { return []; }`,
  "./providers.server": `
    export async function loadBinanceFuturesKlines() {
      return { candles: [], retrievedAt: new Date().toISOString() };
    }
    export async function loadBinanceFuturesListingTime() { return 0; }
    export async function loadBinanceFuturesCompatibility() { return true; }
  `,
  "./collector": `
    export const BINANCE_USDM_WS_ENDPOINT = "wss://example.invalid/stream";
    export const BinanceFuturesCollector = class { resetMovementTransportState() {} };
    export function normalizeRestCandles() { return []; }
  `,
  "./movement-engine.server": movementStub,
  "./movement-python-client.server": `
    export async function advancePythonMovementBoundary() {}
    export async function registerPythonMovementHistory() {}
    export async function calculatePythonMarketMovement() {}
  `,
  "./movement-finalization": `export function movementFinalizationConfig() { return {}; }`,
  "./movement-metrics-contract": `
    export const DEFAULT_MARKET_MOVEMENT_CONFIG = { historicalLookbackMs: 604800000 };
  `,
  "./collector-worker-env.server": `export function validateCollectorWorkerEnvironment() {}`,
};

// The collector runtime is transpiled with every dependency stubbed so the
// shutdown barrier can be observed without a database, socket, or timers.
async function loadCollectorRuntime() {
  const source = await readFile(
    new URL("../src/lib/market/collector.server.ts", import.meta.url),
    "utf8",
  );
  let { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  });
  for (const [specifier, stubSource] of Object.entries(replacements)) {
    outputText = outputText.replaceAll(JSON.stringify(specifier), JSON.stringify(stub(stubSource)));
  }
  return import(stub(outputText));
}

test("an explicit collector stop awaits movement-engine teardown", async () => {
  globalThis.__collectorStopOrder = [];
  const module = await loadCollectorRuntime();
  const runtime = new module.CollectorRuntime({});
  const stopping = runtime.stop();
  assert.deepEqual(globalThis.__collectorStopOrder, ["movement:start"]);
  await stopping;
  assert.deepEqual(globalThis.__collectorStopOrder, ["movement:start", "movement:end"]);
});

test("movement teardown completes before the collector lease is released", async () => {
  globalThis.__collectorStopOrder = [];
  const module = await loadCollectorRuntime();
  const store = {
    async releaseCollectorLease() {
      globalThis.__collectorStopOrder.push("lease:release");
    },
  };
  const runtime = new module.CollectorRuntime(store);
  // `active` is TypeScript-private only; this simulates an acquired lease.
  runtime.active = true;
  await runtime.stop();
  assert.deepEqual(globalThis.__collectorStopOrder, [
    "movement:start",
    "movement:end",
    "lease:release",
  ]);
});
