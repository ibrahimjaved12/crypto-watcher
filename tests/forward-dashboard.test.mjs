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
const filters = await moduleUrl("../src/lib/forward/forward-filters.ts", { zod: import.meta.resolve("zod") });
const report = await moduleUrl("../src/lib/forward/forward-report.ts");
const { loadForwardDashboard } = await import(await moduleUrl("../src/lib/forward/forward-dashboard.ts", {
  "./forward-filters": filters, "./forward-report": report,
}));

test("skipped latest run preserves each funded setup and its strategy metadata", async () => {
  const tables = {
    paper_runs: [
      { status: "skipped_stale", paper_account: "default", boundary_ms: 200, created_at: "b", wallet_state: {} },
      { status: "ok", paper_account: "default", boundary_ms: 100, created_at: "a",
        wallet_state: { positions: { a: { symbol: "SOLUSDT", side: 1 }, b: { symbol: "SOLUSDT", side: -1 } } },
        wallet_config: { leverage_cap: 20 } },
    ],
    forward_setups: [
      { setup_id: "a", strategy_id: "bollinger_20_2_reversion:15", horizon_min: 15 },
      { setup_id: "b", strategy_id: "macd_12_26_9:60", horizon_min: 60 },
      { setup_id: "c", strategy_id: "rsi_14_reversion:15", horizon_min: 15 },
    ],
    paper_ledger: [{ seq: 1, ms: 100, type: "rejected", paper_account: "default", symbol: "SOLUSDT",
      setup_id: "c", payload: { reason: "cap:max_positions" } }],
  };
  const client = {
    from(table) {
      let selected = [...(tables[table] ?? [])];
      const query = {
        select() { return query; },
        eq(key, value) { selected = selected.filter((r) => r[key] === value); return query; },
        in(key, values) { selected = selected.filter((r) => values.includes(r[key])); return query; },
        order() { return query; },
        limit(n) { selected = selected.slice(0, n); return query; },
        then(resolve) { return Promise.resolve({ data: selected, error: null }).then(resolve); },
      };
      return query;
    },
    rpc() { return Promise.resolve({ data: [], error: null }); },
  };
  const data = await loadForwardDashboard(client);
  assert.equal(data.latestRun.status, "skipped_stale");
  assert.equal(data.walletAsOf, 100);
  assert.equal(data.openPositions.length, 2, "same coin must not collapse separate positions");
  assert.deepEqual(data.openPositions.map((p) => p.strategyId), ["bollinger_20_2_reversion:15", "macd_12_26_9:60"]);
  assert.equal(data.openPositions[0].leverageCap, 20);
  assert.equal(data.rejectedPositions[0].payload.reason, "cap:max_positions");
  assert.equal(data.rejectedPositions[0].strategyId, "rsi_14_reversion:15");
});

const { pythonServiceConfig } = await import(await moduleUrl("../src/lib/python-service.server.ts"));
test("forward origin isolates forward calls without moving stateful movement calls", () => {
  const env = { PYTHON_ANALYSIS_ENABLED: "true", PYTHON_ANALYSIS_TOKEN: "x".repeat(32),
    PYTHON_ANALYSIS_URL: "http://127.0.0.1:8000", PYTHON_FORWARD_URL: "http://127.0.0.1:8001" };
  for (const path of ["/v1/forward/evaluate", "/v1/forward/trend"]) {
    assert.equal(pythonServiceConfig(path, env).url, `http://127.0.0.1:8001${path}`);
  }
  assert.equal(pythonServiceConfig("/v1/movement/boundary", env).url, "http://127.0.0.1:8000/v1/movement/boundary");
  assert.equal(pythonServiceConfig("/v1/forward/evaluate", { ...env, PYTHON_FORWARD_URL: undefined }).url,
    "http://127.0.0.1:8000/v1/forward/evaluate");
  assert.throws(() => pythonServiceConfig("/v1/forward/evaluate", { ...env, PYTHON_FORWARD_URL: "http://public.example" }));
});
