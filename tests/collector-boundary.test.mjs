import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

const stub = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;

function transpile(source, replacements = {}) {
  let { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  });
  for (const [specifier, replacement] of Object.entries(replacements)) {
    outputText = outputText.replaceAll(JSON.stringify(specifier), JSON.stringify(replacement));
  }
  return stub(outputText);
}

async function read(path) {
  return readFile(new URL(path, import.meta.url), "utf8");
}

// The collector runtime is transpiled with only its operational/market dependencies
// stubbed. Application modules (the Lovable admin client, the monitor engine, and
// the TA engine) are deliberately not stubbed, so any restored import would fail to
// resolve and fail this test.
const collectorReplacements = {
  "../operational/repository.server": stub(
    `export function getOperationalStore() { throw new Error("the test injects a store"); }`,
  ),
  "./providers.server": stub(
    `export async function loadBinanceFuturesKlines() {
       return { candles: [], retrievedAt: new Date().toISOString() };
     }`,
  ),
  "./collector": stub(`
    export const BINANCE_USDM_WS_ENDPOINT = "wss://example.invalid/stream";
    export function normalizeRestCandles() { return []; }
    export class BinanceFuturesCollector {
      async reconcile(symbols) { globalThis.__collectorBoundary.reconciled.push([...symbols]); }
      streamNames() { return []; }
      subscribedSymbols() { return []; }
      async markConnectionStatus() {}
      noteReconnect() {}
      async recoverAfterReconnect() {}
      accept() { return true; }
      movementSnapshot() { return null; }
      advanceMovementBuckets() {}
      symbolSourceStatus() { return "UNAVAILABLE"; }
      movementLateRejections() { return 0; }
    }
  `),
  "./movement-engine.server": stub(`
    export class MovementEngineRuntime {
      constructor() {}
      async start() {}
      async stop() {}
    }
  `),
  "./movement-finalization": stub(`export function movementFinalizationConfig() { return {}; }`),
  "./collector-worker-env.server": stub(`export function validateCollectorWorkerEnvironment() {}`),
};

function boundaryStore(universe) {
  return {
    enabled: true,
    async claimCollectorLease() {
      globalThis.__collectorBoundary.leaseClaims += 1;
      return true;
    },
    async renewCollectorLease() {
      return true;
    },
    async releaseCollectorLease() {
      globalThis.__collectorBoundary.leaseReleases += 1;
    },
    async readCollectorSubscriptions() {
      globalThis.__collectorBoundary.universeReads += 1;
      return [...universe];
    },
  };
}

class FakeWebSocket {
  static OPEN = 1;
  readyState = 0;
  addEventListener() {}
  send() {}
  close() {}
}

test("the collector worker reads one shared subscription universe from the operational store", async () => {
  globalThis.__collectorBoundary = {
    reconciled: [],
    leaseClaims: 0,
    leaseReleases: 0,
    universeReads: 0,
  };
  const previousWebSocket = globalThis.WebSocket;
  globalThis.WebSocket = FakeWebSocket;
  try {
    const module = await import(
      transpile(await read("../src/lib/market/collector.server.ts"), collectorReplacements)
    );
    const runtime = new module.CollectorRuntime(boundaryStore(["ETHUSDT", "BTCUSDT"]));
    await runtime.tryBecomeActive();
    // One assigned set is reconciled as-is: the collector consumes the application's
    // shared operational representation rather than querying Lovable user tables.
    assert.deepEqual(globalThis.__collectorBoundary.reconciled, [["ETHUSDT", "BTCUSDT"]]);
    assert.equal(globalThis.__collectorBoundary.leaseClaims, 1);
    assert.equal(globalThis.__collectorBoundary.universeReads, 1);
    await runtime.stop();
    assert.equal(globalThis.__collectorBoundary.leaseReleases, 1);
  } finally {
    globalThis.WebSocket = previousWebSocket;
  }
});

test("the collector runtime has no direct Lovable application dependency", async () => {
  const source = await read("../src/lib/market/collector.server.ts");
  for (const forbidden of [
    "supabaseAdmin",
    "integrations/supabase/client.server",
    "watchlist_items",
    "monitor_settings",
    "ta_signals",
    "runTA",
    "../ta/engine.server",
    "../monitor/engine.server",
    "../monitor/run-context",
  ]) {
    assert.equal(
      source.includes(forbidden),
      false,
      `${forbidden} must not appear in the collector runtime`,
    );
  }
  assert.match(source, /readCollectorSubscriptions\(\)/);
});

test("the application assigns the shared collector universe and keeps TA ownership", async () => {
  const hook = await read("../src/routes/api/public/hooks/monitor-prices.ts");
  assert.match(hook, /select\("user_id, symbol"\)/);
  assert.match(hook, /assignCollectorSubscriptions/);

  const engine = await read("../src/lib/monitor/engine.server.ts");
  // TA is no longer gated by collector ownership: TanStack keeps determining due
  // completed-candle work and stays the privileged Lovable `ta_signals` writer.
  assert.doesNotMatch(engine, /completed_candle_ta_enabled\s*&&\s*!sharedCollectorOwnsMarketData/);
  assert.match(engine, /if \(settings\.completed_candle_ta_enabled\) \{/);
  assert.match(engine, /runTA\(/);
  assert.match(engine, /sharedCollectorOwnsMarketData \? null : operationalStore/);
});

test("the collector worker validates operational-only runtime configuration", async () => {
  const env = await read("../src/lib/market/collector-worker-env.server.ts");
  assert.doesNotMatch(env, /validateServerSupabase/);
  assert.doesNotMatch(env, /SUPABASE_URL|SUPABASE_SERVICE_ROLE_KEY|APP_PROFILE/);
  assert.match(env, /operationalDbConfig/);
  assert.match(env, /movementFinalizationConfig/);
});

test("the operational store maps the collector subscription RPCs", async () => {
  const calls = [];
  const client = {
    rpc(name, args) {
      calls.push([name, args]);
      if (name === "get_collector_subscriptions") {
        return Promise.resolve({ data: ["btcusdt", "ethusdt"], error: null });
      }
      return Promise.resolve({ data: null, error: null });
    },
  };
  const module = await import(
    transpile(await read("../src/lib/operational/repository.server.ts"), {
      "@supabase/supabase-js": stub(
        `export function createClient() { throw new Error("unused"); }`,
      ),
      "./config.server": stub(
        `export function operationalDbConfig() { throw new Error("unused"); }`,
      ),
    })
  );
  const store = module.createOperationalStore(client, {
    candleRetentionDays: 7,
    monitorRunRetentionDays: 30,
    outboxMaxAttempts: 10,
  });
  await store.assignCollectorSubscriptions(["btcusdt", "ETHUSDT"]);
  assert.deepEqual(calls[0], [
    "assign_collector_subscriptions",
    { p_symbols: ["BTCUSDT", "ETHUSDT"] },
  ]);
  assert.deepEqual(await store.readCollectorSubscriptions(), ["BTCUSDT", "ETHUSDT"]);
  assert.equal(calls[1][0], "get_collector_subscriptions");
});
