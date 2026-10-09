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

const contractUrl = await moduleUrl("../src/lib/forward/forward-contract.ts", { zod: import.meta.resolve("zod") });
const runUrl = await moduleUrl("../src/lib/forward/forward-run.server.ts", { "./forward-contract": contractUrl });
const contract = await import(contractUrl);
const run = await import(runUrl);
const { createForwardRepository } = await import(await moduleUrl("../src/lib/forward/forward-repository.server.ts", {
  "./forward-contract": contractUrl, "./forward-run.server": runUrl }));
const { summarizeOutcomes } = await import(await moduleUrl("../src/lib/forward/forward-dashboard.ts"));

const HOUR = 3_600_000;
const NOW = 1_760_000_000_000 - (1_760_000_000_000 % HOUR) + 5 * 60_000;
const BOUNDARY = NOW - (NOW % HOUR);
const hex = (c) => c.repeat(64);
const USER = "11111111-1111-4111-8111-111111111111";

function response(patch = {}) {
  const signal = { signal_id: hex("a"), strategy_id: "ema_cross_20_50:60", version: "ta-v1", symbol: "BTCUSDT",
    signal_ms: BOUNDARY - HOUR, side: 1, horizon_min: 60 };
  const setup = { setup_id: hex("b"), signal_id: hex("a"), strategy_id: "ema_cross_20_50:60", version: "ta-v1",
    symbol: "BTCUSDT", side: 1, horizon_min: 60, signal_ms: BOUNDARY - HOUR, entry_ms: BOUNDARY - HOUR, k: "2",
    rr: "3/2", status: "T", half_life_days: 3, window_minutes: 240, var: "123456789012345678", factor_weight: "12000000",
    sigma: "300000000000000000", p0: 6_500_000_000_000, tick: 10_000_000, d_ticks: 100, stop: 6_499_000_000_000,
    target: 6_501_500_000_000, label_leverage: 20, params_hash: hex("c"), versions: { forward: "forward-v1" } };
  return {
    schema_version: 1, versions: { forward: "forward-v1" }, params_hash: hex("c"), wallet_config: {},
    assumptions: ["flat-tier"], processed_to_ms: { BTCUSDT: BOUNDARY - 60_000 }, signals: [signal], setups: [setup],
    resolutions: [{ setup_id: hex("b"), status: "open", resolved_through_ms: BOUNDARY - 60_000, exit_offset: null,
      exit_ms: null, exit_ref_price: null, net_ur: null, cost_ur: null, fund_ur: null, optimistic: null,
      label_leverage: 20, wallet_ur: 1 }],
    reasons: { BTCUSDT: {} },
    ledger: [{ seq: 1, ms: BOUNDARY - HOUR, type: "open_fee", amount_e8: -100, balance_e8: 9_999_999_900,
      assumptions: ["flat-tier"], setup_id: hex("b"), symbol: "BTCUSDT" }],
    wallet_state: { version: "wallet-v1", balance_e8: 9_999_999_900, used_margin_e8: 5, seq: 1, positions: {}, done: [] },
    ...patch,
  };
}

test("the Python response contract is validated before persistence", () => {
  assert.equal(contract.validateForwardResponse(response()).signals.length, 1);
  assert.throws(() => contract.validateForwardResponse(response({ schema_version: 2 })), /schema_version/);
  const bad = response();
  bad.setups[0].status = "Z";
  assert.throws(() => contract.validateForwardResponse(bad), /setups/);
  assert.throws(() => contract.validateForwardResponse(response({ signals: [] })), /setup without its signal/);
  const unsafe = response();
  unsafe.setups[0].sigma = 300000000000000000;  // a JSON number above 2**53 is refused (text only)
  assert.throws(() => contract.validateForwardResponse(unsafe), /sigma/);
});

function minutes(symbol, count = 120, gapAt = -1) {
  const rows = [];
  for (let i = count; i >= 1; i--) {
    if (i === gapAt) continue;
    const open_time_ms = BOUNDARY - i * 60_000;
    rows.push({ open_time_ms, open: 1, high: 1, low: 1, close: 1, volume: 0, transport: "rest", source_event_at_ms: null });
  }
  return rows;
}

function fakeDeps({ status = "LIVE", gapAt = -1, fundingFails = false } = {}) {
  const calls = { python: [], runs: [], persisted: [], funding: [] };
  const runs = new Map();
  const repository = {
    async findRun(userId, runKey) { return runs.get(`${userId}|${runKey}`) ?? null; },
    async latestOkRun() { return null; },
    async openSetups() { return []; },
    async insertRun(userId, row) {
      calls.runs.push({ userId, ...row });
      runs.set(`${userId}|${row.run_key}`, { id: "run-1", status: row.status });
      return "run-1";
    },
    async persistEvaluation(userId, runId, value) { calls.persisted.push({ userId, runId, value }); return { signals: 1 }; },
  };
  return { calls, deps: {
    now: () => NOW,
    readMinutes: async (symbol) => minutes(symbol, 120, gapAt),
    listHealth: async (symbols) => symbols.map((symbol) => ({ symbol, timeframe_minutes: 1, status })),
    callPython: async (body) => { calls.python.push(body); return response(); },
    fetchFunding: async (symbol, startMs) => {
      calls.funding.push({ symbol, startMs });
      if (fundingFails) throw new Error("HTTP 429");
      return [{ calc_time_ms: BOUNDARY - 8 * HOUR, rate: "0.0001" }];
    },
    repository,
  } };
}

test("stale collector health or a gap records skipped_stale and generates nothing", async () => {
  for (const options of [{ status: "RECOVERING" }, { gapAt: 30 }]) {
    const { calls, deps } = fakeDeps(options);
    const summary = await run.runForward(deps, { userId: USER, trigger: "hourly", strategyIds: ["x"], symbols: ["BTCUSDT"] });
    assert.equal(summary.status, "skipped_stale");
    assert.equal(calls.python.length, 0);
    assert.equal(calls.runs[0].status, "skipped_stale");
    assert.equal(calls.persisted.length, 0);
  }
});

test("a fresh hour calls Python once, persists for the account and is idempotent", async () => {
  const { calls, deps } = fakeDeps();
  const input = { userId: USER, trigger: "hourly", strategyIds: ["ema_cross_20_50:60"], symbols: ["BTCUSDT"] };
  const first = await run.runForward(deps, input);
  assert.equal(first.status, "ok");
  assert.equal(first.runKey, `hour:${BOUNDARY}`);
  assert.equal(calls.python.length, 1);
  assert.equal(calls.python[0].to_ms, BOUNDARY - 60_000);
  assert.equal(calls.python[0].symbols[0].rows.length, 120);
  assert.equal(calls.persisted[0].userId, USER);
  const again = await run.runForward(deps, input);
  assert.equal(again.status, "already_done");
  assert.equal(calls.python.length, 1);
});

test("the repository scopes every row to the account and ignores duplicates", async () => {
  const writes = [];
  const client = { from(table) {
    return { upsert(rows, options) { writes.push({ table, rows, options }); return Promise.resolve({ error: null }); } };
  } };
  const repository = createForwardRepository(client);
  const value = contract.validateForwardResponse(response());
  const counts = await repository.persistEvaluation(USER, "run-9", value, new Map());
  assert.deepEqual(counts, { signals: 1, setups: 1, outcomes: 1, ledger: 1 });
  for (const write of writes) {
    assert.equal(write.options.ignoreDuplicates, true);
    for (const row of write.rows) {
      assert.equal(row.user_id, USER);
      assert.equal(row.run_id, "run-9");
    }
  }
  assert.deepEqual(writes.map((w) => w.options.onConflict),
    ["user_id,signal_id", "user_id,setup_id", "user_id,setup_id,status", "user_id,paper_account,seq"]);
  const unchanged = await repository.persistEvaluation(USER, "run-9", value, new Map([[hex("b"), "open"]]));
  assert.equal(unchanged.outcomes, 0);  // an unchanged status appends no outcome row
});

test("outcome summary: sample size first and the placebo control beside each strategy", () => {
  const rows = [
    { setup_id: "1", strategy_id: "rsi_14_reversion:15", version: "ta-v1", rr: "2", status: "T", net_ur: 2_000_000, exit_ms: 10 },
    { setup_id: "2", strategy_id: "rsi_14_reversion:15", version: "ta-v1", rr: "2", status: "ambiguous", net_ur: -1_000_000, exit_ms: 10 },
    { setup_id: "3", strategy_id: "placebo-v1:rsi_14_reversion:15", version: "placebo-v1", rr: "2", status: "S", net_ur: -1_000_000, exit_ms: 10 },
    { setup_id: "4", strategy_id: "macd_12_26_9:60", version: "ta-v1", rr: "2", status: "S", net_ur: -1_000_000, exit_ms: 10 },
  ];
  const summary = summarizeOutcomes(rows);
  assert.equal(summary.length, 2);
  assert.equal(summary[0].strategyId, "rsi_14_reversion:15");
  assert.deepEqual([summary[0].n, summary[0].wins, summary[0].ambiguous, summary[0].meanNetR], [2, 1, 1, 0.5]);
  assert.deepEqual(summary[0].placebo, { n: 1, meanNetR: -1 });
  assert.equal(summary[1].placebo, null);
});

test("funding is fetched once per symbol per run and passed to Python", async () => {
  const { calls, deps } = fakeDeps();
  const summary = await run.runForward(deps, { userId: USER, trigger: "hourly", strategyIds: ["x"], symbols: ["BTCUSDT", "ETHUSDT"] });
  assert.equal(summary.status, "ok");
  assert.deepEqual(calls.funding.map((c) => c.symbol), ["BTCUSDT", "ETHUSDT"]);
  assert.equal(calls.funding[0].startMs, BOUNDARY - 2 * HOUR);  // from_ms (boundary - 1 h) minus 1 h
  const body = calls.python[0];
  assert.deepEqual(body.symbols[0].funding, [{ calc_time_ms: BOUNDARY - 8 * HOUR, rate: "0.0001" }]);
  assert.equal(body.symbols[0].funding_available, true);
  const cache = new Map();
  let fetched = 0;
  const counting = { fetchFunding: async () => { fetched++; return []; } };
  await run.fetchRunFunding(counting, ["BTCUSDT"], 0, cache);
  await run.fetchRunFunding(counting, ["BTCUSDT"], 0, cache);
  assert.equal(fetched, 1);  // the per-run cache answers the second request
});

test("a failed funding fetch never fills zeros and records funding_unavailable", async () => {
  const { calls, deps } = fakeDeps({ fundingFails: true });
  const summary = await run.runForward(deps, { userId: USER, trigger: "hourly", strategyIds: ["x"], symbols: ["BTCUSDT"] });
  assert.equal(summary.status, "ok");
  assert.match(summary.reason, /funding_unavailable: BTCUSDT/);
  const body = calls.python[0];
  assert.equal(body.symbols[0].funding_available, false);
  assert.deepEqual(body.symbols[0].funding, []);
  assert.match(calls.runs[0].reason, /funding_unavailable/);
});

test("Binance funding responses are validated", () => {
  const rows = [{ symbol: "BTCUSDT", fundingTime: 1, fundingRate: "0.00010000", markPrice: "1" },
    { symbol: "BTCUSDT", fundingTime: 2, fundingRate: "-1e-5" }];
  assert.deepEqual(contract.parseBinanceFunding("BTCUSDT", rows),
    [{ calc_time_ms: 1, rate: "0.00010000" }, { calc_time_ms: 2, rate: "-1e-5" }]);
  assert.throws(() => contract.parseBinanceFunding("ETHUSDT", rows), /Invalid Binance funding/);
  assert.throws(() => contract.parseBinanceFunding("BTCUSDT", [rows[1], rows[0]]), /Invalid Binance funding/);
  assert.throws(() => contract.parseBinanceFunding("BTCUSDT", { code: -1 }), /Invalid Binance funding/);
});
