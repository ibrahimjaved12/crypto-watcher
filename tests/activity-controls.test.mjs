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

test("flags default to existing behavior and accept explicit false", () => {
  assert.equal(activityEnabled(undefined), true);
  assert.equal(activityEnabled("true"), true);
  assert.equal(activityEnabled(" FALSE "), false);
  assert.equal(automaticQueryOptions(undefined).refetchInterval, 60_000);
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
    ...automaticQueryOptions("false"),
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
  globalThis.__activityFetch = () => {
    fetches++;
    return { source: "test", candles: [{ time: 0, close: 100 }] };
  };
  const { runTA } = await import(
    await moduleUrl("../src/lib/ta/engine.server.ts", {
      "../activity-controls": controlsUrl,
      "../market/providers.server": stub(
        "export const loadTACandles = async () => globalThis.__activityFetch();",
      ),
      "./core": stub(
        "export const TA_FRAMES=[15,60,240], TA_VERSION='test'; export const closedCandles=x=>x; export const analyze=()=>({patterns:[]}); export const outcomeDue=()=>0;",
      ),
    })
  );
  const db = {
    from() {
      return {
        async upsert() {
          inserts++;
          return {};
        },
        select() {
          reads++;
          return this;
        },
        eq() {
          return this;
        },
        order() {
          return this;
        },
        async limit() {
          return { data: [] };
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
      fetches = inserts = reads = 0;
      process.env.TA_GENERATION_ENABLED = String(generation);
      process.env.TA_OUTCOME_EVALUATION_ENABLED = String(outcomes);
      assert.deepEqual(await runTA(db, "user", "BTCUSDT"), []);
      assert.equal(fetches, generation || outcomes ? 3 : 0);
      assert.equal(inserts, generation ? 3 : 0);
      assert.equal(reads, outcomes ? 3 : 0);
    }
  } finally {
    names.forEach((name, i) =>
      saved[i] === undefined ? delete process.env[name] : (process.env[name] = saved[i]),
    );
    delete globalThis.__activityFetch;
  }
});

test("disabled scheduled endpoint authenticates and skips before loading database client", async () => {
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
      "@/integrations/supabase/client.server": stub("throw Error('must not load database');"),
    })
  );
  const oldFlag = process.env.SCHEDULED_MONITOR_ENABLED;
  const oldToken = process.env.MONITOR_CRON_TOKEN;
  process.env.SCHEDULED_MONITOR_ENABLED = "false";
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
