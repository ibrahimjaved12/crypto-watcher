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
    { symbol: "BTCUSDT", fundingTime: 2, fundingRate: "-1e-5" },
    { symbol: "BTCUSDT", fundingTime: 3, fundingRate: "0.0001", markPrice: "" }];
  assert.deepEqual(contract.parseBinanceFunding("BTCUSDT", rows),
    [{ calc_time_ms: 1, rate: "0.00010000", mark: "1" }, { calc_time_ms: 2, rate: "-1e-5" },
      { calc_time_ms: 3, rate: "0.0001" }], "the exchange mark rides along only when it is a positive decimal");
  assert.throws(() => contract.parseBinanceFunding("ETHUSDT", rows), /Invalid Binance funding/);
  assert.throws(() => contract.parseBinanceFunding("BTCUSDT", [rows[1], rows[0]]), /Invalid Binance funding/);
  assert.throws(() => contract.parseBinanceFunding("BTCUSDT", { code: -1 }), /Invalid Binance funding/);
});

// P16: one-time 1m backfill (public REST klines, paginated, weight aware) when history starts too late.
const backfill = await import(await moduleUrl("../src/lib/forward/forward-backfill.server.ts", { zod: import.meta.resolve("zod") }));

function kline(openTime, volume = "1.5") {
  return [openTime, "100.0", "101.0", "99.0", "100.5", volume, openTime + 59_999, "150.75", 3, "0.7", "70.35", "0"];
}

test("1m klines are validated and only completed candles become REST collector candles", () => {
  const rows = [kline(BOUNDARY - 120_000), kline(BOUNDARY - 60_000), kline(BOUNDARY)];
  const candles = backfill.parseMinuteKlines("btcusdt", rows, BOUNDARY + 30_000, BOUNDARY + 30_000);
  assert.equal(candles.length, 2, "the still-open minute is dropped");
  assert.deepEqual(
    [candles[0].symbol, candles[0].transport, candles[0].endpoint, candles[0].sourceEventTime, candles[0].timeframeMinutes],
    ["BTCUSDT", "rest", "/fapi/v1/klines", null, 1],
  );
  assert.throws(() => backfill.parseMinuteKlines("BTCUSDT", [kline(BOUNDARY + 1)], NOW, NOW), /Invalid/);
  assert.throws(() => backfill.parseMinuteKlines("BTCUSDT", [kline(BOUNDARY), kline(BOUNDARY)], NOW + HOUR, NOW), /order/);
  assert.throws(() => backfill.parseMinuteKlines("BTCUSDT", [{ open: 1 }], NOW, NOW), /Invalid/);
});

test("the backfill pages oldest first, records through the collector write and pauses on high weight", async () => {
  const pages = [];
  const recorded = [];
  const sleeps = [];
  const start = BOUNDARY - 4000 * 60_000;
  const deps = {
    now: () => NOW,
    async fetchPage(symbol, from, to) {
      pages.push([from, to]);
      const rows = [];
      for (let open = from; open <= to && rows.length < backfill.KLINE_PAGE_LIMIT; open += 60_000) rows.push(kline(open));
      return { rows, usedWeight: pages.length === 1 ? 1600 : 40 };
    },
    async record(candles) { recorded.push(candles.length); },
    async sleep(ms) { sleeps.push(ms); },
  };
  const offered = await backfill.backfillMinuteHistory(deps, "BTCUSDT", start, BOUNDARY);
  assert.equal(offered, 4000);
  assert.deepEqual(pages.map(([from]) => from), [start, start + 1500 * 60_000, start + 3000 * 60_000]);
  assert.ok(recorded.every((size) => size <= 1000), "each RPC batch stays within the 1000-row limit");
  assert.equal(sleeps.length, 1, "one pause after the page that reported weight above the threshold");
});

test("a forward run backfills short history once and stores candle versions, never sent to Python", async () => {
  const { calls, deps } = fakeDeps();
  const reads = [];
  const hashed = (rows) => rows.map((row) => ({ ...row, candle_hash: `${(row.open_time_ms / 60_000) % 1e6}`.padStart(32, "0") }));
  deps.readMinutes = async (symbol, since) => { reads.push(since); return hashed(minutes(symbol, 120)); };
  const backfills = [];
  deps.backfillMinutes = async (symbol, startMs, endMs) => { backfills.push([symbol, startMs, endMs]); return 5; };
  const persisted = [];
  deps.repository.persistEvaluation = async (userId, runId, value, previous, versions) => {
    persisted.push(versions);
    return { signals: 1 };
  };
  const summary = await run.runForward(deps, { userId: USER, trigger: "hourly", strategyIds: ["ema_cross_20_50:60"], symbols: ["BTCUSDT"] });
  assert.equal(summary.status, "ok");
  assert.equal(run.FORWARD_HISTORY_DAYS, 120);
  assert.deepEqual(backfills, [["BTCUSDT", BOUNDARY - run.BACKFILL_DAYS * 86_400_000, BOUNDARY - 120 * 60_000]]);
  assert.equal(reads.length, 2, "history is read again after the backfill");
  assert.ok(calls.python[0].symbols[0].rows.every((row) => !("candle_hash" in row)), "hashes never go to Python");
  const version = persisted[0].get(hex("a"));
  assert.match(version, /^[0-9a-f]{64}$/, "the 60m decision candle has all 60 one-minute hashes");
  const again = await run.decisionCandleVersions(response(), new Map([["BTCUSDT", hashed(minutes("BTCUSDT", 120))]]));
  assert.equal(again.get(hex("a")), version, "deterministic");
  const gap = await run.decisionCandleVersions(response(), new Map([["BTCUSDT", hashed(minutes("BTCUSDT", 120, 90))]]));
  assert.equal(gap.get(hex("a")), null, "an incomplete decision candle has no version");
});

test("only recent gaps are stale; older gaps reach Python as missing bars", async () => {
  const { calls, deps } = fakeDeps();
  const old = BOUNDARY - 3 * 86_400_000;
  deps.readMinutes = async (symbol) => [
    ...minutes(symbol, 120).map((row) => ({ ...row, open_time_ms: row.open_time_ms - 3 * 86_400_000 + 7_200_000 })),
    ...minutes(symbol, 49 * 60),
  ];
  const summary = await run.runForward(deps, { userId: USER, trigger: "on_demand", strategyIds: ["x"], symbols: ["BTCUSDT"] });
  assert.equal(summary.status, "ok", "a gap older than STALE_GAP_WINDOW_MS does not block the engine");
  assert.equal(calls.python.length, 1);
  assert.equal(calls.python[0].symbols[0].rows[0].open_time_ms, old);
  const recent = run.staleness(["BTCUSDT"], new Map([["BTCUSDT", minutes("BTCUSDT", 120, 30)]]),
    [{ symbol: "BTCUSDT", timeframe_minutes: 1, status: "LIVE" }], BOUNDARY);
  assert.match(recent.join(), /1 gap/);
});

test("backfill ranges include interior gaps, so an interrupted backfill is resumed", async () => {
  const M = 60_000;
  const since = BOUNDARY - 1000 * M;
  // An earlier backfill stored [since, since+100) and then hit HTTP 429; live data starts at -120.
  const stored = [];
  for (let open = since; open < since + 100 * M; open += M) stored.push({ open_time_ms: open });
  stored.push(...minutes("BTCUSDT", 120));
  assert.deepEqual(run.backfillRanges(stored, since, since - 50 * M, BOUNDARY),
    [{ startMs: since + 100 * M, endMs: BOUNDARY - 120 * M }]);
  assert.deepEqual(run.backfillRanges([], since, since - 50 * M, BOUNDARY), [{ startMs: since - 50 * M, endMs: BOUNDARY }]);
  assert.equal(run.missingMinutes(stored, since, BOUNDARY), 1000 - 220);

  const { deps } = fakeDeps();
  let reads = 0;
  deps.readMinutes = async () => (reads++ === 0 ? stored : minutes("BTCUSDT", 120));
  const attempts = [];
  deps.backfillMinutes = async (symbol, startMs, endMs) => {
    attempts.push([startMs, endMs]);
    throw new Error("Binance 1m klines rate limited (HTTP 429)");
  };
  const summary = await run.runForward(deps, { userId: USER, trigger: "hourly", strategyIds: ["x"], symbols: ["BTCUSDT"] });
  assert.equal(attempts.length, 1, "a failure stops this symbol's repair for this run");
  assert.equal(reads, 2, "pages recorded before the failure are re-read");
  assert.match(summary.reason, /backfill_failed: BTCUSDT: .*429/);
});

test("on-demand runs never backfill", async () => {
  const { deps } = fakeDeps();
  let called = false;
  deps.backfillMinutes = async () => { called = true; return 1; };
  await run.runForward(deps, { userId: USER, trigger: "on_demand", strategyIds: ["x"], symbols: ["BTCUSDT"] });
  assert.equal(called, false);
});

test("the repository stores candle_version as a column, never inside the setup payload", async () => {
  const writes = [];
  const client = { from(table) {
    return { async upsert(rows) { writes.push({ table, rows }); return { error: null }; } };
  } };
  const repository = createForwardRepository(client);
  await repository.persistEvaluation(USER, "run-9", response(), new Map(), new Map([[hex("a"), "f".repeat(64)]]));
  const signal = writes.find((item) => item.table === "forward_signals").rows[0];
  const setup = writes.find((item) => item.table === "forward_setups").rows[0];
  assert.equal(signal.candle_version, "f".repeat(64));
  assert.equal(setup.candle_version, "f".repeat(64));
  assert.ok(!("candle_version" in setup.payload));
});

test("stale then fresh in the same hour succeeds, and only success consumes the key", async () => {
  const { deps, calls } = fakeDeps({ status: "STALE" });
  const input = { userId: USER, trigger: "on_demand", strategyIds: ["x"], symbols: ["BTCUSDT"] };
  assert.equal((await run.runForward(deps, input)).status, "skipped_stale");
  deps.listHealth = async () => [{ symbol: "BTCUSDT", timeframe_minutes: 1, status: "LIVE" }];
  assert.equal((await run.runForward(deps, input)).status, "ok");
  assert.equal((await run.runForward(deps, input)).status, "already_done");
  assert.equal(calls.python.length, 1);
});

test("wholly no-sigma runs retry, while partial evaluations remain idempotent", async () => {
  const { deps, calls } = fakeDeps();
  const input = { userId: USER, trigger: "on_demand", strategyIds: ["x"], symbols: ["BTCUSDT", "ETHUSDT"] };
  const reasons = { BTCUSDT: { sigma: "no_sigma: waiting" }, ETHUSDT: { sigma: "no_sigma: waiting" } };
  deps.callPython = async () => response({ reasons, setups: [], resolutions: [], ledger: [] });
  assert.equal((await run.runForward(deps, input)).status, "no_sigma");
  assert.deepEqual(calls.runs[0].processed_to_ms, {});
  deps.callPython = async () => response({ reasons: { ETHUSDT: { sigma: "no_sigma: waiting" } } });
  assert.equal((await run.runForward(deps, input)).status, "ok");
  assert.equal((await run.runForward(deps, input)).status, "already_done");
});
