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
      resetMovementTransportState() { globalThis.__collectorBoundary.movementResets += 1; }
      async reconcile(symbols) { globalThis.__collectorBoundary.reconciled.push([...symbols]); }
      streamNames() { return []; }
      subscribedSymbols() { return []; }
      async markConnectionStatus() {}
      async markCandleConnectionStatus() {}
      markMovementConnectionStatus() {}
      noteReconnect() {}
      async recoverAfterReconnect() {}
      accept() { return true; }
      movementSnapshot() { return null; }
      advanceMovementBuckets() {}
      movementSourceStatus() { return "UNAVAILABLE"; }
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
  "./movement-python-client.server": stub(`
    export async function advancePythonMovementBoundary() {
      return { snapshots: [], lateAfterFinalizationCount: 0 };
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

class ValidatingFakeWebSocket {
  static OPEN = 1;
  static CLOSED = 3;
  static instances = [];

  readyState = ValidatingFakeWebSocket.OPEN;
  listeners = new Map();
  closeCalls = [];
  sent = [];

  constructor(url) {
    this.url = url;
    ValidatingFakeWebSocket.instances.push(this);
  }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) ?? [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  emit(type, event) {
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }

  send(payload) {
    this.sent.push(payload);
  }

  close(code, reason = "") {
    if (
      code !== undefined &&
      (typeof code !== "number" ||
        !Number.isInteger(code) ||
        (code !== 1000 && (code < 3000 || code > 4999)))
    ) {
      throw new DOMException("invalid client close code", "InvalidAccessError");
    }
    if (Buffer.byteLength(reason) > 123) {
      throw new DOMException("close reason is too long", "SyntaxError");
    }
    this.closeCalls.push({ code, reason });
    this.readyState = ValidatingFakeWebSocket.CLOSED;
  }
}

async function loadBehavioralCollectorRuntime() {
  const collector = transpile(await read("../src/lib/market/collector.ts"), {
    "./symbols": stub(
      `export const isSupportedSymbol = (value) => ["BTCUSDT", "ETHUSDT"].includes(value);`,
    ),
    "./movement-contract": stub(`export const MOVEMENT_OBSERVATION_BATCH_MAX = 20000;`),
  });
  return import(
    transpile(await read("../src/lib/market/collector.server.ts"), {
      ...collectorReplacements,
      "./collector": collector,
    })
  );
}

async function openBehavioralRuntime(module, symbols) {
  const store = {
    async recordCollectorCandles() {
      return [];
    },
    async recordCollectorHealth() {},
  };
  const runtime = new module.CollectorRuntime(store);
  await runtime.collector.reconcile(symbols);
  await runtime.collector.markConnectionStatus("LIVE", null);
  runtime.active = true;
  runtime.connect();
  return {
    runtime,
    socket: ValidatingFakeWebSocket.instances.at(-1),
  };
}

test("the collector worker reads one shared subscription universe from the operational store", async () => {
  globalThis.__collectorBoundary = {
    reconciled: [],
    leaseClaims: 0,
    leaseReleases: 0,
    universeReads: 0,
    movementResets: 0,
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

test("movement transport resets on collector lease acquisition and loss", async () => {
  globalThis.__collectorBoundary = {
    reconciled: [],
    leaseClaims: 0,
    leaseReleases: 0,
    universeReads: 0,
    movementResets: 0,
  };
  const previousWebSocket = globalThis.WebSocket;
  globalThis.WebSocket = FakeWebSocket;
  try {
    const module = await import(
      transpile(await read("../src/lib/market/collector.server.ts"), collectorReplacements)
    );
    const store = boundaryStore(["BTCUSDT"]);
    store.renewCollectorLease = async () => false;
    const runtime = new module.CollectorRuntime(store);
    runtime.startTimers = () => {};
    runtime.connect = () => {};
    await runtime.tryBecomeActive();
    assert.equal(globalThis.__collectorBoundary.movementResets, 1);

    runtime.stopped = true;
    await runtime.renewLease();
    assert.equal(globalThis.__collectorBoundary.movementResets, 2);

    runtime.stopped = false;
    await runtime.tryBecomeActive();
    assert.equal(globalThis.__collectorBoundary.movementResets, 3);
  } finally {
    globalThis.WebSocket = previousWebSocket;
  }
});

test("malformed WebSocket JSON fences movement and closes with a valid client code", async () => {
  const previousWebSocket = globalThis.WebSocket;
  ValidatingFakeWebSocket.instances.length = 0;
  globalThis.WebSocket = ValidatingFakeWebSocket;
  try {
    const module = await loadBehavioralCollectorRuntime();
    const { runtime, socket } = await openBehavioralRuntime(module, ["BTCUSDT", "ETHUSDT"]);

    assert.doesNotThrow(() => socket.emit("message", { data: "{" }));
    assert.equal(runtime.collector.movementSourceStatus("BTCUSDT"), "UNAVAILABLE");
    assert.equal(runtime.collector.movementSourceStatus("ETHUSDT"), "UNAVAILABLE");
    assert.equal(socket.closeCalls.length, 1);
    assert.ok(
      socket.closeCalls[0].code === 1000 ||
        (socket.closeCalls[0].code >= 3000 && socket.closeCalls[0].code <= 4999),
    );
    assert.equal(socket.closeCalls[0].reason, "malformed Binance movement frame");
  } finally {
    globalThis.WebSocket = previousWebSocket;
  }
});

test("an aggTrade stream rejects a valid trade for another symbol without cross-queuing", async () => {
  const previousWebSocket = globalThis.WebSocket;
  ValidatingFakeWebSocket.instances.length = 0;
  globalThis.WebSocket = ValidatingFakeWebSocket;
  try {
    const module = await loadBehavioralCollectorRuntime();
    const { runtime, socket } = await openBehavioralRuntime(module, ["BTCUSDT", "ETHUSDT"]);
    const mismatchedTrade = {
      stream: "btcusdt@aggTrade",
      data: {
        e: "aggTrade",
        E: 1_800_000_000_001,
        s: "ETHUSDT",
        st: 1,
        a: 42,
        p: "123.4500",
        q: "0.5000",
        T: 1_800_000_000_000,
      },
    };

    assert.doesNotThrow(() =>
      socket.emit("message", { data: JSON.stringify(mismatchedTrade) }),
    );
    assert.equal(runtime.collector.movementSourceStatus("BTCUSDT"), "UNAVAILABLE");
    assert.equal(runtime.collector.movementSourceStatus("ETHUSDT"), "LIVE");
    assert.equal(runtime.collector.latestPrice("ETHUSDT"), null);
    assert.deepEqual(runtime.collector.movementObservations.get("ETHUSDT"), []);
    assert.equal(socket.closeCalls.length, 1);
    assert.ok(socket.closeCalls[0].code >= 3000 && socket.closeCalls[0].code <= 4999);
  } finally {
    globalThis.WebSocket = previousWebSocket;
  }
});

test("an aggTrade stream rejects an otherwise valid kline event", async () => {
  const previousWebSocket = globalThis.WebSocket;
  ValidatingFakeWebSocket.instances.length = 0;
  globalThis.WebSocket = ValidatingFakeWebSocket;
  try {
    const module = await loadBehavioralCollectorRuntime();
    const { runtime, socket } = await openBehavioralRuntime(module, ["BTCUSDT"]);
    const klineOnTradeStream = {
      stream: "btcusdt@aggTrade",
      data: {
        e: "kline",
        E: 60_000,
        s: "BTCUSDT",
        st: 1,
        k: {
          t: 0,
          T: 59_999,
          s: "BTCUSDT",
          i: "1m",
          o: "100",
          h: "102",
          l: "99",
          c: "101",
          v: "12",
          x: false,
        },
      },
    };

    assert.doesNotThrow(() =>
      socket.emit("message", { data: JSON.stringify(klineOnTradeStream) }),
    );
    assert.equal(runtime.collector.movementSourceStatus("BTCUSDT"), "UNAVAILABLE");
    assert.equal(runtime.collector.developingCandle("BTCUSDT", 1), null);
    assert.deepEqual(runtime.collector.movementObservations.get("BTCUSDT"), []);
    assert.equal(socket.closeCalls.length, 1);
    assert.ok(socket.closeCalls[0].code >= 3000 && socket.closeCalls[0].code <= 4999);
  } finally {
    globalThis.WebSocket = previousWebSocket;
  }
});

test("subscription acknowledgement makes movement LIVE while candle recovery is pending", async () => {
  const previousWebSocket = globalThis.WebSocket;
  ValidatingFakeWebSocket.instances.length = 0;
  globalThis.WebSocket = ValidatingFakeWebSocket;
  try {
    const module = await loadBehavioralCollectorRuntime();
    const { runtime, socket } = await openBehavioralRuntime(module, ["BTCUSDT"]);
    await runtime.collector.markConnectionStatus("RECOVERING", "fixture reconnect");

    let finishRecovery;
    let recoverySettled = false;
    const recovery = new Promise((resolve) => {
      finishRecovery = () => {
        recoverySettled = true;
        resolve();
      };
    });
    runtime.collector.recoverAfterReconnect = () => recovery;

    let observeLive;
    const movementLive = new Promise((resolve) => { observeLive = resolve; });
    const markMovementConnectionStatus =
      runtime.collector.markMovementConnectionStatus.bind(runtime.collector);
    runtime.collector.markMovementConnectionStatus = (status) => {
      markMovementConnectionStatus(status);
      if (status === "LIVE") observeLive();
    };

    socket.emit("open", {});
    assert.equal(runtime.collector.movementSourceStatus("BTCUSDT"), "RECOVERING");
    assert.equal(socket.sent.length, 1);
    const subscription = JSON.parse(socket.sent[0]);
    socket.emit("message", { data: JSON.stringify({ result: null, id: subscription.id }) });
    await movementLive;

    assert.equal(recoverySettled, false);
    assert.equal(runtime.collector.movementSourceStatus("BTCUSDT"), "LIVE");
    assert.equal(runtime.collector.symbolSourceStatus("BTCUSDT"), "RECOVERING");
    assert.equal(socket.closeCalls.length, 0);

    finishRecovery();
    await recovery;
    runtime.active = false;
    await runtime.stop();
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

test("the application owns the collector universe and completed-candle TA", async () => {
  // The scheduled monitor hook must not reconcile the collector universe: #22
  // requires a disabled request to return before loading the privileged client, so
  // the universe is reconciled by a dedicated hook instead.
  const hook = await read("../src/routes/api/public/hooks/monitor-prices.ts");
  assert.doesNotMatch(hook, /assignCollectorSubscriptions/);
  const skipped = hook.indexOf("Scheduled monitoring disabled");
  const privileged = hook.indexOf("integrations/supabase/client.server");
  assert.ok(skipped !== -1 && privileged !== -1 && skipped < privileged);

  // The dedicated hook is authenticated but independent of scheduled monitoring,
  // and it is the initial/ongoing reconciliation mechanism the deployment schedules.
  const syncRoute = await read("../src/routes/api/public/hooks/sync-collector-subscriptions.ts");
  assert.match(syncRoute, /syncCollectorUniverse/);
  assert.doesNotMatch(syncRoute, /process\.env\["SCHEDULED_MONITOR_ENABLED"\]/);
  assert.match(syncRoute, /initial and ongoing reconciliation mechanism/);

  // The server's first-request pass is only a safety net: it never claims to be the
  // primary lifecycle mechanism and never starts or hosts the collector itself.
  const server = await read("../src/server.ts");
  assert.match(server, /reconcileCollectorUniverseOnFirstRequest\(\)/);
  assert.match(server, /bootstrapCollectorUniverse\(\)/);
  assert.match(server, /only a safety net/);
  assert.doesNotMatch(server, /reconcileCollectorUniverseAtStartup/);

  const engine = await read("../src/lib/monitor/engine.server.ts");
  // TA is no longer gated by collector ownership: TanStack keeps determining due
  // completed-candle work and stays the privileged Lovable `ta_signals` writer.
  assert.doesNotMatch(engine, /completed_candle_ta_enabled\s*&&\s*!sharedCollectorOwnsMarketData/);
  assert.match(engine, /if \(settings\.completed_candle_ta_enabled\) \{/);
  assert.match(
    engine,
    /runTA\(supabaseAdmin, userId, symbol, context, undefined, operationalStore\)/,
  );
});

test("collector mode reads canonical completed candles instead of a second live series", async () => {
  const engine = await read("../src/lib/ta/engine.server.ts");
  assert.match(engine, /readCollectorTACandles\(symbol, timeframe\)/);
  assert.match(engine, /BINANCE_COLLECTOR_ENABLED/);
  // The REST provider stays the non-collector path; while collector mode is active
  // the engine must not silently fall back to it.
  assert.match(engine, /await context\.ta\(symbol, timeframe, validate, requestedSource\)/);
  // The versioned Python request and the persisted conclusion use the provenance the
  // collector recorded, never a reconstructed endpoint or candle-open boundary.
  assert.match(engine, /source_event_time_ms: candle\.sourceEventTime/);
  assert.match(engine, /endpoint: candle\.endpoint/);
  // Actual exchange event time is optional provenance: absent REST candles persist no
  // source event rather than a boundary fabricated from the candle open.
  assert.match(engine, /source_event_at:\n\s+candle\.sourceEventTime === null\n/);
  assert.doesNotMatch(engine, /source_event_time_ms: candle\.time \+ duration/);
  assert.doesNotMatch(engine, /endpoint: generationMarket!\.endpoint/);
  assert.doesNotMatch(engine, /retrievedAt: new Date\(\)\.toISOString\(\)/);
});

test("the operational TA read adapter transports provenance and fabricates nothing", async () => {
  const repository = await read("../src/lib/operational/repository.server.ts");
  // The adapter carries the collector's recorded endpoint/transport/source-event and
  // receive times instead of reconstructing them.
  assert.match(repository, /source_event_at_ms/);
  assert.match(repository, /received_at_ms/);
  assert.match(repository, /transport/);
  assert.doesNotMatch(repository, /COLLECTOR_TA_ENDPOINT/);
  assert.doesNotMatch(repository, /retrievedAt: new Date\(\)\.toISOString\(\)/);
  // The collector records the exchange event time for WebSocket candles and records no
  // event at all for REST bootstrap/recovery. Receive times are preserved exactly,
  // never clamped or rewritten from the event time.
  const collector = await read("../src/lib/market/collector.ts");
  assert.match(collector, /sourceEventTime: null,/);
  assert.match(collector, /sourceEventTime,/);
  assert.doesNotMatch(collector, /completedSourceEventTime/);
  assert.doesNotMatch(collector, /Math\.max\(input\.retrievedAt, sourceEventTime\)/);
  assert.doesNotMatch(collector, /receivedAt: Math\.max/);
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
      "../market/symbols": stub(`
        export const MARKET_SOURCE = "binance-usdm";
        export const MARKET_PRICE_TYPE = "trade";
        export function futuresInstrument(symbol) {
          const native = symbol.toUpperCase();
          return {
            id: "binance-usdm:" + native, exchange: "binance", nativeSymbol: native,
            marketType: "futures", contractType: "perpetual",
            baseAsset: native.replace(/USDT$/, ""), quoteAsset: "USDT", marginAsset: "USDT",
            settlementAsset: "USDT", linear: true, contractMultiplier: 1,
          };
        }
      `),
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
  // The application reads canonical collector candles back through the same adapter.
  await store.readCollectorTACandles("btcusdt", 15);
  assert.equal(calls[2][0], "get_collector_ta_candles");
  assert.deepEqual(calls[2][1], { p_symbol: "BTCUSDT", p_timeframe_minutes: 15, p_limit: 260 });
});
