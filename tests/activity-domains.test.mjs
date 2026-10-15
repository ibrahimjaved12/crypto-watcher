import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";
import { createModuleLoader } from "./module-loader.mjs";

const loadModule = createModuleLoader(import.meta.url);
const { describeAlertCheck } = await import(await loadModule("../src/lib/monitor/check-message.ts"));

async function moduleUrl(path, imports = {}) {
  const source = await readFile(new URL(path, import.meta.url), "utf8");
  let { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  });
  for (const [specifier, target] of Object.entries(imports))
    outputText = outputText.replaceAll(JSON.stringify(specifier), JSON.stringify(target));
  return `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`;
}

const stub = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;

const { DEFAULT_SETTINGS, recordRun, runLeasedMonitorForUser, runMonitorForUser } = await import(
  await moduleUrl("../src/lib/monitor/engine.server.ts", {
    "../alert-charts/capture.server": stub(`
      export function startMovementCapture(deps) {
        const hook = globalThis.__captureHook;
        return hook ? hook(deps) : { symbol: deps.symbol, promise: Promise.resolve({ status: "skipped" }) };
      }
      export async function settleMovementCaptures(pending) {
        globalThis.__settled = pending.map((item) => item.symbol);
        return { settled: pending.length, abandoned: [] };
      }
    `),
    "@/lib/market/providers.server": stub(`
      export async function loadCandles(symbol) {
        globalThis.__domains.market.push(symbol);
        return {ok:true,result:{source:'binance-usdm',minute:[],quarter:[],attempts:[]}};
      }
    `),
    "./run-context": stub(`
      const zero=()=>({exchangeRequests:0,candleRows:0,marketCacheHits:0,taCalculations:0,taSignalsSaved:0,taOutcomesUpdated:0,databaseReads:0,databaseWriteAttempts:0,databaseNoOps:0});
      export const createMonitorRunContext=()=>({
        metrics:zero(),
        async observation(symbol) {
          globalThis.__domains.market.push(symbol);
          return {ok:true,result:{source:'binance-usdm',minute:[],quarter:[],attempts:[]}};
        }
      });
      export const metricsSnapshot=(value)=>({...value});
      export const metricsSince=(current,previous)=>Object.fromEntries(Object.keys(current).map(key=>[key,current[key]-previous[key]]));
    `),
    "./observation": stub(`
      export const completedObservation=()=>({price:100,observedAt:'2026-09-21T00:00:00.000Z'});
    `),
    "../ta/engine.server": stub(`
      export async function runTA(db,userId,symbol) {
        globalThis.__domains.ta.push(symbol);
        return globalThis.__domains.taResults?.[symbol] ?? { checks: 3, errors: [] };
      }
    `),
    "../operational/repository.server": stub(`
      const disabled={enabled:false};
      export const getOperationalStore=()=>disabled;
      export const operationalNativeSymbol=(_source,symbol)=>symbol;
    `),
  })
);

function settings(patch = {}) {
  return { user_id: "user", ...DEFAULT_SETTINGS, ...patch };
}

function database() {
  return {
    from(name) {
      assert.equal(name, "watchlist_items");
      globalThis.__domains.reads++;
      return {
        select() {
          return this;
        },
        async eq() {
          return { data: [{ symbol: "BTCUSDT" }, { symbol: "ETHUSDT" }], error: null };
        },
      };
    },
    async rpc(name) {
      if (name === "record_market_data_checkpoint") {
        globalThis.__domains.checkpoints++;
        return { data: { status: "recorded" }, error: null };
      }
      assert.equal(name, "process_cumulative_observation");
      globalThis.__domains.rpc++;
      return { data: { status: "below_threshold" }, error: null };
    },
  };
}

async function run(patch, operationalStore, taResults) {
  globalThis.__domains = { reads: 0, market: [], checkpoints: 0, operational: 0, rpc: 0, ta: [] };
  if (taResults) globalThis.__domains.taResults = taResults;
  const result = await runMonitorForUser(
    database(),
    "user",
    settings(patch),
    undefined,
    operationalStore,
  );
  return { result, calls: globalThis.__domains };
}

test("movement alerts and completed-candle TA run independently", async () => {
  let outcome = await run({ completed_candle_ta_enabled: false });
  assert.equal(outcome.result.status, "success");
  assert.equal(outcome.result.taChecks, 0);
  assert.equal(outcome.result.taFailures, 0);
  assert.deepEqual(outcome.calls.market, ["BTCUSDT", "ETHUSDT"]);
  assert.equal(outcome.calls.checkpoints, 2);
  assert.equal(outcome.calls.rpc, 2);
  assert.deepEqual(outcome.calls.ta, []);

  outcome = await run({ movement_alerts_enabled: false });
  assert.equal(outcome.result.status, "success");
  assert.equal(outcome.result.taChecks, 6);
  assert.equal(outcome.result.taFailures, 0);
  assert.deepEqual(outcome.calls.market, ["BTCUSDT", "ETHUSDT"]);
  assert.equal(outcome.calls.checkpoints, 2);
  assert.equal(outcome.calls.rpc, 0);
  assert.deepEqual(outcome.calls.ta, ["BTCUSDT", "ETHUSDT"]);
});

test("skipped TA work adds no checks or all-checks-ran reassurance", async () => {
  const { result } = await run({}, undefined, {
    BTCUSDT: { checks: 0, errors: [] },
    ETHUSDT: { checks: 0, errors: [] },
  });
  assert.equal(result.taChecks, 0);
  assert.equal(result.taFailures, 0);
  assert.doesNotMatch(describeAlertCheck(result).title, /technical-analysis checks ran/);
});

test("the monitor sums attempted TA frames and their failures", async () => {
  const { result } = await run({}, undefined, {
    BTCUSDT: { checks: 1, errors: ["TA BTCUSDT 15m: provider timeout"] },
    ETHUSDT: { checks: 2, errors: [] },
  });
  assert.equal(result.status, "partial");
  assert.equal(result.taChecks, 3);
  assert.equal(result.taFailures, 1);
  assert.match(describeAlertCheck(result).title, /^1 of 3 technical-analysis checks failed\./);
});

test("the master and collection controls stop work before provider or database I/O", async () => {
  for (const patch of [{ monitoring_enabled: false }, { market_data_collection_enabled: false }]) {
    const { result, calls } = await run(patch);
    assert.equal(result.status, "skipped");
    assert.equal(result.taChecks, 0);
    assert.equal(result.taFailures, 0);
    assert.deepEqual(calls, {
      reads: 0,
      market: [],
      checkpoints: 0,
      operational: 0,
      rpc: 0,
      ta: [],
    });
  }
});

test("the shared monitor lease skips work already owned by another run", async () => {
  const context = {
    metrics: {
      exchangeRequests: 0,
      candleRows: 0,
      marketCacheHits: 0,
      taCalculations: 0,
      taSignalsSaved: 0,
      taOutcomesUpdated: 0,
      databaseReads: 0,
      databaseWriteAttempts: 0,
      databaseNoOps: 0,
    },
    observation() {
      throw new Error("monitor work must not start");
    },
  };
  const result = await runLeasedMonitorForUser(
    {
      from() {
        throw new Error("watchlist must not be read");
      },
      async rpc(name) {
        assert.equal(name, "claim_monitor_run_lease");
        return { data: false, error: null };
      },
    },
    "user",
    settings(),
    context,
    { enabled: false },
  );
  assert.equal(result.status, "skipped");
  assert.equal(result.taChecks, 0);
  assert.equal(result.taFailures, 0);
  assert.match(result.error, /already in progress/);
  assert.equal(context.metrics.databaseNoOps, 1);
});

test("the feature flag selects one checkpoint writer without dual writes", async () => {
  const store = {
    enabled: true,
    async recordCandles() {
      globalThis.__domains.operational++;
    },
    async recordCheckpoint() {
      globalThis.__domains.operational++;
      return "recorded";
    },
  };
  const { result, calls } = await run({ completed_candle_ta_enabled: false }, store);
  assert.equal(result.status, "success");
  assert.equal(calls.checkpoints, 0);
  assert.equal(calls.operational, 4);
  assert.equal(calls.rpc, 2);
});

test("monitor-run ownership switches as one domain without a main-database dual-write", async () => {
  let operationalWrites = 0;
  const store = {
    enabled: true,
    async recordMonitorRun() {
      operationalWrites++;
    },
  };
  const db = {
    from() {
      throw new Error("Main-database monitor_runs must not be written");
    },
  };
  await recordRun(
    db,
    {
      userId: "user",
      status: "success",
      symbolsChecked: 1,
      alertsCreated: 0,
      taChecks: 0,
      taFailures: 0,
      dataSource: "binance-usdm",
      error: null,
      durationMs: 1,
      metrics: {
        exchangeRequests: 0,
        candleRows: 0,
        marketCacheHits: 0,
        taCalculations: 0,
        taSignalsSaved: 0,
        taOutcomesUpdated: 0,
        databaseReads: 0,
        databaseWriteAttempts: 0,
        databaseNoOps: 0,
      },
    },
    store,
  );
  assert.equal(operationalWrites, 1);
});

test("collection checkpoints continue while every downstream consumer is paused", async () => {
  const { result, calls } = await run({
    movement_alerts_enabled: false,
    completed_candle_ta_enabled: false,
    developing_setup_evaluation_enabled: true,
    paper_trading_enabled: true,
  });
  assert.equal(result.status, "success");
  assert.deepEqual(calls.market, ["BTCUSDT", "ETHUSDT"]);
  assert.equal(calls.checkpoints, 2);
  assert.equal(calls.rpc, 0);
  assert.deepEqual(calls.ta, []);
});

test("enabling both current consumers runs both paths", async () => {
  const { result, calls } = await run({});
  assert.equal(result.status, "success");
  assert.equal(result.symbolsChecked, 2);
  assert.equal(result.taChecks, 6);
  assert.equal(result.taFailures, 0);
  assert.deepEqual(calls.market, ["BTCUSDT", "ETHUSDT"]);
  assert.equal(calls.checkpoints, 2);
  assert.equal(calls.rpc, 2);
  assert.deepEqual(calls.ta, ["BTCUSDT", "ETHUSDT"]);
});

test("completed-candle TA stays application-owned while the collector owns market data", async () => {
  const previous = process.env.BINANCE_COLLECTOR_ENABLED;
  process.env.BINANCE_COLLECTOR_ENABLED = "true";
  try {
    const store = {
      enabled: true,
      async recordCandles() {
        globalThis.__domains.operational++;
      },
      async recordCheckpoint() {
        globalThis.__domains.operational++;
        return "recorded";
      },
    };
    const { result, calls } = await run({}, store);
    assert.equal(result.status, "success");
    // The collector owns canonical candles and checkpoints, so the app writes none.
    assert.equal(calls.checkpoints, 0);
    assert.equal(calls.operational, 0);
    // Completed-candle TA remains application-orchestrated and application-written;
    // removing the collector's TA path must not remove the application's.
    assert.deepEqual(calls.ta, ["BTCUSDT", "ETHUSDT"]);
  } finally {
    if (previous === undefined) delete process.env.BINANCE_COLLECTOR_ENABLED;
    else process.env.BINANCE_COLLECTOR_ENABLED = previous;
  }
});

test("a slow chart capture never delays the next symbols' alerts and is settled after the loop (#299)", async () => {
  const order = [];
  globalThis.__settled = null;
  // A capture that never finishes: if the loop awaited it, the second symbol would never be processed.
  globalThis.__captureHook = (deps) => {
    order.push(`capture:${deps.symbol}`);
    return { symbol: deps.symbol, promise: new Promise(() => {}) };
  };
  globalThis.__domains = { reads: 0, market: [], checkpoints: 0, operational: 0, rpc: 0, ta: [] };
  const db = database();
  const rpc = db.rpc;
  db.rpc = async (name, args) => {
    if (name !== "process_cumulative_observation") return rpc(name, args);
    order.push(`rpc:${args.p_symbol}`);
    return { data: { status: "alerted", alert_id: `alert-${args.p_symbol}` }, error: null };
  };
  try {
    const finished = await Promise.race([
      runMonitorForUser(db, "user", settings({ completed_candle_ta_enabled: false })),
      new Promise((resolve) => setTimeout(() => resolve("timeout"), 2_000)),
    ]);
    assert.notEqual(finished, "timeout");
    assert.equal(finished.alertsCreated, 2);
    assert.deepEqual(order, ["rpc:BTCUSDT", "capture:BTCUSDT", "rpc:ETHUSDT", "capture:ETHUSDT"]);
    assert.deepEqual(globalThis.__settled, ["BTCUSDT", "ETHUSDT"]); // awaited together after the loop
  } finally {
    delete globalThis.__captureHook;
  }
});
