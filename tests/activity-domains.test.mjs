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

const { DEFAULT_SETTINGS, runMonitorForUser } = await import(
  await moduleUrl("../src/lib/monitor/engine.server.ts", {
    "@/lib/market/providers.server": stub(`
      export async function loadCandles(symbol) {
        globalThis.__domains.market.push(symbol);
        return {ok:true,result:{source:'Binance',minute:[],quarter:[]}};
      }
    `),
    "./observation": stub(`
      export const completedObservation=()=>({price:100,observedAt:'2026-09-21T00:00:00.000Z'});
    `),
    "../ta/engine.server": stub(`
      export async function runTA(db,userId,symbol) {
        globalThis.__domains.ta.push(symbol);
        return [];
      }
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

async function run(patch) {
  globalThis.__domains = { reads: 0, market: [], checkpoints: 0, rpc: 0, ta: [] };
  const result = await runMonitorForUser(database(), "user", settings(patch));
  return { result, calls: globalThis.__domains };
}

test("movement alerts and completed-candle TA run independently", async () => {
  let outcome = await run({ completed_candle_ta_enabled: false });
  assert.equal(outcome.result.status, "success");
  assert.deepEqual(outcome.calls.market, ["BTCUSDT", "ETHUSDT"]);
  assert.equal(outcome.calls.checkpoints, 2);
  assert.equal(outcome.calls.rpc, 2);
  assert.deepEqual(outcome.calls.ta, []);

  outcome = await run({ movement_alerts_enabled: false });
  assert.equal(outcome.result.status, "success");
  assert.deepEqual(outcome.calls.market, ["BTCUSDT", "ETHUSDT"]);
  assert.equal(outcome.calls.checkpoints, 2);
  assert.equal(outcome.calls.rpc, 0);
  assert.deepEqual(outcome.calls.ta, ["BTCUSDT", "ETHUSDT"]);
});

test("the master and collection controls stop work before provider or database I/O", async () => {
  for (const patch of [{ monitoring_enabled: false }, { market_data_collection_enabled: false }]) {
    const { result, calls } = await run(patch);
    assert.equal(result.status, "skipped");
    assert.deepEqual(calls, { reads: 0, market: [], checkpoints: 0, rpc: 0, ta: [] });
  }
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
  assert.deepEqual(calls.market, ["BTCUSDT", "ETHUSDT"]);
  assert.equal(calls.checkpoints, 2);
  assert.equal(calls.rpc, 2);
  assert.deepEqual(calls.ta, ["BTCUSDT", "ETHUSDT"]);
});
