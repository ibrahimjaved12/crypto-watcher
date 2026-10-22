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
const { loadForwardDashboard, loadForwardLogPage } = await import(await moduleUrl("../src/lib/forward/forward-dashboard.ts", {
  "./forward-filters": filters, "./forward-report": report,
}));

test("the Strategy Lab dashboard reads signals, open setups and the latest run only (the wallet moved to the Paper account page)", async () => {
  const tables = {
    forward_market_signals: [{ signal_id: "s1", signal_ms: 5 }, { signal_id: "s2", signal_ms: 9 }],
    forward_market_open_setups: [{ setup_id: "a", entry_ms: 1 }, { setup_id: "b", entry_ms: 2 }],
    paper_runs: [
      { status: "skipped_stale", boundary_ms: 200, created_at: "b", wallet_state: { balance_e8: 1 } },
      { status: "ok", boundary_ms: 100, created_at: "a" },
    ],
  };
  const read = [];
  const compare = (x, y) => (x < y ? -1 : x > y ? 1 : 0);
  const client = {
    from(table) {
      read.push(table);
      const orders = [];
      let max = Infinity;
      const query = {
        select() { return query; },
        eq() { return query; },
        order(key, { ascending }) { orders.push([key, ascending]); return query; },
        limit(n) { max = n; return query; },
        then(resolve) {
          const rows = [...(tables[table] ?? [])].sort((x, y) => {
            for (const [key, ascending] of orders) {
              const result = compare(x[key] ?? 0, y[key] ?? 0);
              if (result) return ascending ? result : -result;
            }
            return 0;
          });
          return Promise.resolve({ data: rows.slice(0, max), error: null }).then(resolve);
        },
      };
      return query;
    },
    rpc() { throw new Error("the dashboard no longer calls the equity series"); },
  };
  const data = await loadForwardDashboard(client);
  assert.deepEqual(Object.keys(data).sort(), ["latestRun", "openSetups", "signals"]);
  assert.equal(data.latestRun.status, "skipped_stale");
  assert.deepEqual(data.signals.map((s) => s.signal_id), ["s2", "s1"]);
  assert.equal(data.openSetups.length, 2);
  assert.deepEqual([...read].sort(), ["forward_market_open_setups", "forward_market_signals", "paper_runs"]);
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

// #454: a user has a crypto and a forex paper account, both in paper_ledger; the ledger log never mixes them.
test("the ledger log is scoped to one market's paper account", async () => {
  const rows = [
    { seq: 1, paper_account: "u", ms: 1 },
    { seq: 2, paper_account: "u:forex", ms: 2 },
    { seq: 3, paper_account: "u", ms: 3 },
  ];
  const calls = [];
  const client = {
    from(table) {
      const keep = [];
      const query = {
        select() { return query; },
        like(column, pattern) { calls.push(["like", column, pattern]); keep.push((r) => r[column].endsWith(pattern.slice(1))); return query; },
        not(column, op, pattern) { calls.push(["not", column, op, pattern]); keep.push((r) => !r[column].endsWith(pattern.slice(1))); return query; },
        lt(column, value) { keep.push((r) => r[column] < value); return query; },
        order() { return query; },
        limit() { return query; },
        then(resolve) {
          assert.equal(table, "paper_ledger");
          const out = rows.filter((r) => keep.every((k) => k(r))).sort((a, b) => b.seq - a.seq);
          return Promise.resolve({ data: out, error: null }).then(resolve);
        },
      };
      return query;
    },
  };
  const none = { families: [], horizons: [], sides: [], symbols: [], rr: [] };
  const crypto = await loadForwardLogPage(client, "ledger", none, null);
  assert.deepEqual(crypto.rows.map((r) => r.seq), [3, 1], "the default (crypto) leaves the forex account out");
  const forex = await loadForwardLogPage(client, "ledger", none, null, 50, "forex");
  assert.deepEqual(forex.rows.map((r) => r.seq), [2]);
  assert.deepEqual(calls, [["not", "paper_account", "like", "%:forex"], ["like", "paper_account", "%:forex"]]);
  await assert.rejects(loadForwardLogPage(client, "signals", none, null, 50, "forex"), /Only the ledger/);
});
