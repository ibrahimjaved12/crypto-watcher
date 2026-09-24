import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

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
const controlsUrl = await moduleUrl("../src/lib/activity-controls.ts");
const { activityEnabled, automaticQueryOptions } = await import(controlsUrl);

test("automatic activity defaults off while TA processing defaults on", () => {
  assert.equal(activityEnabled(undefined), true);
  assert.equal(activityEnabled(undefined, false), false);
  assert.equal(activityEnabled("true", false), true);
  assert.equal(activityEnabled(" FALSE "), false);
  assert.equal(activityEnabled("invalid", false), false);
  assert.deepEqual(automaticQueryOptions(undefined), {
    enabled: false,
    refetchInterval: false,
    retry: false,
  });
  assert.equal(automaticQueryOptions("true").refetchInterval, 60_000);
});

test("paused queries ignore timers, mount, focus, reconnect, invalidation and new keys; manual refresh works", async () => {
  // Query core decides whether timers run at import time. Simulate a browser.
  globalThis.window = {};
  const { QueryClient, QueryObserver, focusManager, onlineManager } =
    await import("@tanstack/react-query");
  const client = new QueryClient();
  client.mount();
  let calls = 0;
  const options = (key) => ({
    queryKey: [key],
    queryFn: async () => ++calls,
    ...automaticQueryOptions(undefined),
    // Even a short timer cannot bypass enabled:false.
    refetchInterval: 5,
  });
  const observer = new QueryObserver(client, options("ta"));
  const unsubscribe = observer.subscribe(() => {});
  try {
    focusManager.setFocused(false);
    focusManager.setFocused(true);
    onlineManager.setOnline(false);
    onlineManager.setOnline(true);
    await client.invalidateQueries();
    observer.setOptions(options("ta-new-filter"));
    await new Promise((resolve) => setTimeout(resolve, 25));
    assert.equal(calls, 0);
    assert.equal((await observer.refetch()).data, 1);
    await client.invalidateQueries();
    await new Promise((resolve) => setTimeout(resolve, 25));
    assert.equal(calls, 1);
  } finally {
    unsubscribe();
    client.unmount();
    client.clear();
    focusManager.setFocused(undefined);
    delete globalThis.window;
  }
});

test("TA controls independently gate inserts and outcome reads; both off avoid all I/O", async () => {
  let fetches = 0;
  let inserts = 0;
  let reads = 0;
  let outcomeWrites = 0;
  globalThis.__activityFetch = () => {
    fetches++;
    return {
      source: "binance-usdm",
      instrument: { id: "binance-usdm:BTCUSDT" },
      endpoint: "/fapi/v1/klines",
      priceType: "trade",
      candles: Array.from({ length: 200 }, (_, time) => ({ time, close: 100 })),
    };
  };
  globalThis.__activityCalculate = async (requests) =>
    requests.map((request) => ({
      status: "ok",
      reason: null,
      timeframe_minutes: request.timeframe_minutes,
      candle_open_time_ms: request.target_candle_open_time_ms,
      candle_close_time_ms: request.target_candle_open_time_ms + request.timeframe_minutes * 60_000,
      source_event_time_ms: request.source_event_time_ms,
      evaluation_time_ms: request.evaluation_time_ms,
      detection_time_ms: request.detection_time_ms,
      ta_version: "ta-v2",
      strategy_version: "interpretation-v1",
      classification: "neutral",
      score: 0,
      atr_pct: 1,
      factor_breakdown: {},
      reasons: [],
      patterns: [],
      indicators: {
        candle: { open_ms: request.target_candle_open_time_ms, close: 100, complete: true },
        candle_count: 200,
      },
      provenance: {
        instrument_id: request.instrument.instrument_id,
        exchange: request.instrument.exchange,
        native_symbol: request.instrument.native_symbol,
        market_type: request.instrument.market_type,
        contract_type: request.instrument.contract_type,
        source: request.source,
        price_type: request.price_type,
        candle_count: 200,
        warmup_candle_count: 0,
        missing_open_times_ms: [],
      },
    }));
  const { runTA } = await import(
    await moduleUrl("../src/lib/ta/engine.server.ts", {
      "../activity-controls": controlsUrl,
      "../monitor/run-context": stub(`
        const metrics=()=>({exchangeRequests:0,candleRows:0,marketCacheHits:0,taCalculations:0,taSignalsSaved:0,taOutcomesUpdated:0,databaseReads:0,databaseWriteAttempts:0,databaseNoOps:0});
        export const createMonitorRunContext=()=>({metrics:metrics(),ta:async()=>globalThis.__activityFetch()});
      `),
      "./python-client.server": stub(
        "export const calculateTechnicalBatch=(...args)=>globalThis.__activityCalculate(...args);",
      ),
      "./python-contract": stub(
        "export const requestCandles=x=>x.map(c=>({...c,open_ms:c.time}));",
      ),
      "./schedule": stub(
        "export const TA_FRAMES=[15,60,240],TA_VERSION='ta-v2',TA_INTERPRETATION_VERSION='interpretation-v1',TA_MINIMUM_HISTORY=200; export const completedCandles=x=>x; export const outcomeDue=()=>0;",
      ),
    })
  );
  const db = {
    async rpc(name, args) {
      if (name === "get_ta_due_work") {
        reads++;
        const rows = [];
        if (args.p_include_outcomes) {
          for (const timeframe of [15, 60, 240])
            rows.push({
              work_kind: "outcome",
              id: `due-${timeframe}`,
              timeframe,
              candle_at: new Date(0).toISOString(),
              source: "binance-usdm",
              detected_at: new Date(0).toISOString(),
              price: 100,
            });
        }
        return { data: rows, error: null };
      }
      outcomeWrites++;
      return { data: 0, error: null };
    },
    from() {
      return {
        upsert() {
          inserts++;
          return { select: async () => ({ data: [{ id: "saved" }], error: null }) };
        },
      };
    },
  };
  const names = ["TA_GENERATION_ENABLED", "TA_OUTCOME_EVALUATION_ENABLED"];
  const saved = names.map((name) => process.env[name]);
  try {
    for (const [generation, outcomes] of [
      [false, false],
      [true, false],
      [false, true],
      [true, true],
    ]) {
      fetches = inserts = reads = outcomeWrites = 0;
      process.env.TA_GENERATION_ENABLED = String(generation);
      process.env.TA_OUTCOME_EVALUATION_ENABLED = String(outcomes);
      assert.deepEqual(await runTA(db, "user", "BTCUSDT"), []);
      assert.equal(fetches, generation || outcomes ? 3 : 0);
      assert.equal(inserts, generation ? 3 : 0);
      assert.equal(reads, generation || outcomes ? 1 : 0);
      assert.equal(outcomeWrites, outcomes ? 3 : 0);
    }
  } finally {
    names.forEach((name, i) =>
      saved[i] === undefined ? delete process.env[name] : (process.env[name] = saved[i]),
    );
    delete globalThis.__activityFetch;
    delete globalThis.__activityCalculate;
  }
});

test("scheduled endpoint defaults disabled, authenticates and skips before database access", async () => {
  const { Route } = await import(
    await moduleUrl("../src/routes/api/public/hooks/monitor-prices.ts", {
      "@tanstack/react-router": stub("export const createFileRoute=()=>x=>x;"),
      "@/lib/activity-controls": controlsUrl,
      "@/integrations/supabase/cron-auth": stub(
        "export const authenticateCronRequest=async()=>new Response('Unauthorized',{status:401});",
      ),
      "@/lib/monitor/engine.server": stub(
        "export const DEFAULT_SETTINGS={}; export const recordRun=()=>{throw Error('must not write')}; export const runMonitorForUser=()=>{throw Error('must not run')};",
      ),
      "@/lib/monitor/run-context": stub(
        "export const createMonitorRunContext=()=>{throw Error('must not create context')};",
      ),
      "@/integrations/supabase/client.server": stub("throw Error('must not load database');"),
    })
  );
  const oldFlag = process.env.SCHEDULED_MONITOR_ENABLED;
  const oldToken = process.env.MONITOR_CRON_TOKEN;
  delete process.env.SCHEDULED_MONITOR_ENABLED;
  process.env.MONITOR_CRON_TOKEN = "test-only";
  try {
    for (const method of ["GET", "POST"]) {
      const handle = Route.server.handlers[method];
      assert.equal((await handle({ request: new Request("https://example.test") })).status, 401);
      const response = await handle({
        request: new Request("https://example.test", {
          headers: { authorization: "Bearer test-only" },
        }),
      });
      assert.equal(response.status, 200);
      assert.equal((await response.json()).status, "skipped");
    }
  } finally {
    if (oldFlag === undefined) delete process.env.SCHEDULED_MONITOR_ENABLED;
    else process.env.SCHEDULED_MONITOR_ENABLED = oldFlag;
    if (oldToken === undefined) delete process.env.MONITOR_CRON_TOKEN;
    else process.env.MONITOR_CRON_TOKEN = oldToken;
  }
});
