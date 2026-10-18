import assert from "node:assert/strict";
import { test } from "node:test";
import { createModuleLoader } from "./module-loader.mjs";

// #446: the per-user forex pass with injected deps (no network, no database, no Python). Every stage, event,
// spec and bar here is SYNTHETIC.
const moduleUrl = createModuleLoader(import.meta.url);
const user = await import(await moduleUrl("../src/lib/fx/fx-user.server.ts"));

const MIN = 60_000;
const HOUR = 3_600_000;
const START = Date.UTC(2026, 9, 13, 6, 0); // the account's start boundary
const B1 = START + HOUR;
const B2 = START + 2 * HOUR;
const B3 = START + 3 * HOUR;
const VENUE = "test-venue";

function spec(instrumentId, overrides = {}) {
  const symbol = instrumentId.split(":")[1];
  return { venue: VENUE, instrumentId, nativeSymbol: `${symbol}m`, digits: 5, point: 0.00001, tickSize: 0.00001,
    tickValue: 1, contractSize: 100000, volumeMin: 0.01, volumeMax: 200, volumeStep: 0.01,
    baseCurrency: symbol.slice(0, 3), profitCurrency: symbol.slice(3), marginCurrency: symbol.slice(0, 3),
    accountCurrency: "USD", tradeMode: 4, swapMode: 1, swapLong: -7.3, swapShort: 2.1, swapRollover3days: 3,
    specHash: "a".repeat(64), observedAt: 0, ...overrides };
}
function bar(instrumentId, openMs) {
  const symbol = instrumentId.split(":")[1];
  return { time_basis: "utc", venue: VENUE, instrument_id: instrumentId, symbol, native_symbol: `${symbol}m`,
    transport: "poll", open_time_ms: openMs, open: 1.085, high: 1.085, low: 1.085, close: 1.085, spread_points: 12,
    tick_volume: 3, received_at_ms: openMs + MIN };
}
const openEvent = (symbol, entry, id = "setup-1") => ({ type: "open", ms: entry, symbol, sequence: 0,
  stage_run_id: "s", quote_to_account: "1", setup: { setup_id: id, symbol, entry_ms: entry, status: "T" } });
const closeEvent = (symbol, ms, id = "setup-1") => ({ type: "close", ms, symbol, sequence: 1, stage_run_id: "s",
  setup_id: id, quote_to_account: "1", exit_fill: "108410000", outcome: "T" });

function stage(boundary, { fromMs = boundary - HOUR, events = [] } = {}) {
  return { id: `stage-${boundary}`, boundary_ms: boundary, fromMs, versions: { fx_stage: "fx-stage-v1" },
    params_hash: "p".repeat(64), payload: { from_ms: fromMs, processed_to_ms: { EURUSD: boundary },
      reasons: { EURUSD: {} } }, freshness: { refused: {}, evaluated: ["EURUSD"] }, events };
}

/** An in-memory forex account store that behaves like the hosted pair (cursor, state, previous run id). */
function world({ stages = [], users = ["u1"], specs, wallet, failCommit = [], accountState = {}, bars } = {}) {
  const calls = { wallet: [], commits: [], retryable: [], accounts: [], bars: [], errors: [], logs: [], commitAttempts: 0 };
  const accounts = new Map();
  const get = (id) => {
    if (!accounts.has(id)) accounts.set(id, { cursor: START, previousRunId: null, state: null, runs: 0, ...accountState[id] });
    return accounts.get(id);
  };
  const deps = {
    now: () => B3,
    store: {
      async getFxSymbolSpecs() { return specs ?? [spec("fx:EURUSD"), spec("fx:GBPUSD")]; },
      async getFxBars(venue, instrumentId, options) {
        calls.bars.push([venue, instrumentId, options]);
        if (bars) return bars(instrumentId, options);
        return [bar(instrumentId, options.fromMs)];
      },
    },
    hosted: {
      async listUsers() { return users; },
      async account(userId, boundary) {
        calls.accounts.push([userId, boundary]);
        const a = get(userId);
        return { userId, accountId: `${userId}:forex`, config: { initial_balance_e8: 100000000000, risk_fraction: "1/100",
          max_positions: 1000, max_exposure_multiple: "10", note: "dropped" }, startBoundary: START, cursor: a.cursor,
          previousRunId: a.previousRunId, state: a.state };
      },
      async stagesAfter(cursor, boundary) {
        return stages.filter((s) => s.boundary_ms > cursor && s.boundary_ms <= boundary)
          .sort((a, b) => a.boundary_ms - b.boundary_ms);
      },
      async commit(account, st, run, ledger) {
        calls.commitAttempts++;
        const code = failCommit.shift();
        if (code) throw Object.assign(new Error(`commit failed (${code})`), { code });
        const a = get(account.userId);
        assert.equal(account.previousRunId, a.previousRunId, "the commit carries the previous run id it read");
        calls.commits.push({ userId: account.userId, stage: st.boundary_ms, run, ledger, expected: account.previousRunId });
        a.cursor = st.boundary_ms; a.runs++; a.previousRunId = `run-${account.userId}-${a.runs}`; a.state = run.wallet_state;
        return a.previousRunId;
      },
      async retryable(account, boundary, status, reason) {
        calls.retryable.push({ userId: account.userId, boundary, status, reason });
      },
    },
    async callWallet(body) {
      calls.wallet.push(body);
      if (wallet) return wallet(body, calls.wallet.length);
      const n = Number(body.state?.n ?? 0) + 1;
      return { schema_version: 1, wallet_state: { version: "fx-wallet-v1", n, positions: {} },
        ledger: [{ seq: n, type: "open_fee" }], wallet_config: { version: "fx-wallet-v1" }, assumptions: ["a"] };
    },
    log: (line) => calls.logs.push(line),
    error: (line) => calls.errors.push(line),
  };
  return { deps, calls, accounts };
}

test("stages are consumed in order and the cursor follows each commit", async () => {
  const w = world({ stages: [stage(B2), stage(B1), stage(B3)] });
  const summary = await user.runFxUser(w.deps, "u1", B3);
  assert.deepEqual(summary, { userId: "u1", status: "ok", consumed: 3 });
  assert.deepEqual(w.calls.commits.map((c) => c.stage), [B1, B2, B3]);
  assert.equal(w.accounts.get("u1").cursor, B3);
  assert.equal(w.calls.retryable.length, 0);
  // Stages after the boundary are not consumed yet.
  const early = world({ stages: [stage(B1), stage(B2)] });
  assert.equal((await user.runFxUser(early.deps, "u1", B1)).consumed, 1);
  assert.deepEqual(early.calls.commits.map((c) => c.stage), [B1]);
});

test("a stage that decides after the account's cursor is a gap: nothing is committed", async () => {
  // B2 decides from B1 + 30 min, but the account is only consumed to START: decisions in between were never made.
  const w = world({ stages: [stage(B2, { fromMs: B1 + 30 * MIN })] });
  const summary = await user.runFxUser(w.deps, "u1", B2);
  assert.equal(summary.status, "refused");
  assert.match(summary.reason, /^gap: stage .* decides from/);
  assert.equal(w.calls.commits.length, 0);
  assert.equal(w.calls.wallet.length, 0);
  assert.deepEqual(w.calls.retryable.map((r) => [r.boundary, r.status]), [[B2, "skipped_stale"]]);
  assert.equal(w.accounts.get("u1").cursor, START);
  // A stage whose window reaches back past the cursor (catch-up after a refusal) is fine.
  const ok = world({ stages: [stage(B1, { fromMs: START - 3 * HOUR })] });
  assert.equal((await user.runFxUser(ok.deps, "u1", B1)).status, "ok");
  // The first stage of a gap blocks the later ones too.
  const blocked = world({ stages: [stage(B1, { fromMs: B1 }), stage(B2)] });
  assert.equal((await user.runFxUser(blocked.deps, "u1", B2)).consumed, 0);
  assert.equal(blocked.calls.commits.length, 0);
});

test("the previous run's state is passed on, the run payload has the crypto shape and the ledger goes with it", async () => {
  const w = world({ stages: [stage(B1), stage(B2)] });
  await user.runFxUser(w.deps, "u1", B2);
  assert.equal(w.calls.wallet[0].state, null, "a new account starts from null");
  assert.deepEqual(w.calls.wallet[1].state, { version: "fx-wallet-v1", n: 1, positions: {} });
  assert.deepEqual(w.calls.wallet[0].config, { initial_balance_e8: 100000000000, risk_fraction: "1/100",
    max_positions: 1000, max_exposure_multiple: "10" }, "only the config keys the endpoint accepts");
  const [first, second] = w.calls.commits;
  assert.equal(first.expected, null);
  assert.equal(second.expected, "run-u1-1");
  assert.deepEqual(second.ledger, [{ seq: 2, type: "open_fee" }]);
  assert.deepEqual(Object.keys(second.run).sort(), ["assumptions", "freshness", "from_ms", "params_hash",
    "processed_to_ms", "reasons", "status", "trigger", "versions", "wallet_config", "wallet_state"]);
  assert.equal(second.run.trigger, "hourly");
  assert.equal(second.run.status, "ok");
  assert.deepEqual(second.run.processed_to_ms, { EURUSD: B2 });
  assert.deepEqual(second.run.versions, { fx_stage: "fx-stage-v1" });
  assert.equal(second.run.params_hash, "p".repeat(64));
  assert.deepEqual(second.run.wallet_state, { version: "fx-wallet-v1", n: 2, positions: {} });
  assert.deepEqual(second.run.assumptions, ["a"]);
  assert.equal(second.run.freshness.evaluated[0], "EURUSD");
  assert.equal(typeof second.run.freshness.wallet_ms, "number");
});

test("the wallet call gets the stage's events, contracts and the bars the stop-out scan needs", async () => {
  const events = [openEvent("EURUSD", B1 - 30 * MIN), openEvent("EURUSD", START - 10 * MIN, "old"),
    closeEvent("EURUSD", B1 - 10 * MIN)];
  const w = world({ stages: [stage(B1, { events })],
    accountState: { u1: { state: { version: "fx-wallet-v1", positions: { carried: { symbol: "GBPUSD" } } } } } });
  await user.runFxUser(w.deps, "u1", B1);
  const body = w.calls.wallet[0];
  assert.deepEqual(body.events.map((e) => [e.type, e.setup?.setup_id ?? e.setup_id]),
    [["open", "setup-1"], ["close", "setup-1"]], "an open from before the account's start is dropped");
  assert.deepEqual(Object.keys(body.contracts), ["EURUSD", "GBPUSD"]);
  assert.deepEqual(body.contracts["EURUSD"], { contract_size: "100000", tick_size: "0.00001", min_lot: "0.01",
    lot_step: "0.01", base: "EUR", quote: "USD" });
  assert.deepEqual(body.symbols.map((s) => [s.symbol, s.quote_to_account, s.bars.length]),
    [["EURUSD", "1", 1], ["GBPUSD", "1", 1]]);
  assert.deepEqual(body.symbols[0].venue_symbol, { venue: VENUE, native_symbol: "EURUSDm", digits: 5, tick_size: "0.00001" });
  assert.equal(body.symbols[0].bars[0].open_time_ms, START + MIN);
  assert.equal("received_at_ms" in body.symbols[0].bars[0], false, "gateway row shape only");
  // The scan starts at the minute after the cursor (an entry later than that changes nothing); toMs is exclusive.
  assert.deepEqual(w.calls.bars.map(([, id, o]) => [id, o.fromMs, o.toMs, o.limit]), [
    ["fx:EURUSD", START + MIN, B1 + MIN, 20_000],
    ["fx:GBPUSD", START + MIN, B1 + MIN, 20_000]]);
  // A new position that entered before the cursor (catch-up) is scanned from its own entry.
  const late = world({ stages: [stage(B2, { fromMs: START, events: [openEvent("EURUSD", B1 - 20 * MIN)] })],
    accountState: { u1: { cursor: B1 } } });
  await user.runFxUser(late.deps, "u1", B2);
  assert.deepEqual(late.calls.bars.map(([, id, o]) => [id, o.fromMs, o.toMs]), [["fx:EURUSD", B1 - 20 * MIN, B2 + MIN]]);
  // Only symbols with open positions or open events are read: a stage with no events and no positions reads none.
  const quiet = world({ stages: [stage(B1)] });
  await user.runFxUser(quiet.deps, "u1", B1);
  assert.deepEqual(quiet.calls.bars, []);
  assert.deepEqual(quiet.calls.wallet[0].symbols, []);
});

test("a wallet refusal or a missing input writes a retryable audit row and leaves the cursor", async () => {
  const refused = world({ stages: [stage(B1), stage(B2)], wallet: async () => { throw new Error("Forex wallet step failed with HTTP 422: bad"); } });
  const summary = await user.runFxUser(refused.deps, "u1", B2);
  assert.equal(summary.status, "refused");
  assert.match(summary.reason, /wallet step refused: Forex wallet step failed with HTTP 422/);
  assert.equal(refused.calls.commits.length, 0);
  assert.equal(refused.accounts.get("u1").cursor, START);
  assert.deepEqual(refused.calls.retryable.map((r) => [r.userId, r.boundary, r.status]), [["u1", B1, "skipped_stale"]]);
  // An invalid wallet response is a refusal too.
  const invalid = world({ stages: [stage(B1)], wallet: async () => ({ schema_version: 1, wallet_state: null }) });
  assert.match((await user.runFxUser(invalid.deps, "u1", B1)).reason, /invalid response/);
  // A symbol with an event but no spec.
  const noSpec = world({ stages: [stage(B1, { events: [openEvent("EURUSD", B1 - 30 * MIN)] })], specs: [spec("fx:GBPUSD")] });
  assert.match((await user.runFxUser(noSpec.deps, "u1", B1)).reason, /no symbol spec for EURUSD/);
  assert.equal(noSpec.calls.wallet.length, 0);
  assert.equal(noSpec.calls.retryable.length, 1);
  // A bar window the store cannot return whole is refused, not trimmed.
  const full = world({ stages: [stage(B1, { events: [openEvent("EURUSD", B1 - 30 * MIN)] })],
    bars: () => Array.from({ length: 20_000 }, (_, i) => bar("fx:EURUSD", B1 - 30 * MIN + i * MIN)) });
  assert.match((await user.runFxUser(full.deps, "u1", B1)).reason, /more than 20000 bars/);
  assert.equal(full.calls.commits.length, 0);
});

test("one failing account does not stop the others, and the failure count is returned", async () => {
  const w = world({ stages: [stage(B1)], users: ["bad", "u2", "boom", "u3"],
    wallet: async (body, n) => {
      if (n === 1) throw new Error("refuse the first call");
      return { schema_version: 1, wallet_state: { version: "fx-wallet-v1", positions: {} }, ledger: [],
        wallet_config: {}, assumptions: [] };
    } });
  const hosted = w.deps.hosted;
  const original = hosted.account;
  hosted.account = async (userId, boundary) => {
    if (userId === "boom") throw new Error("database down");
    return original(userId, boundary);
  };
  const result = await user.runFxUsers(w.deps, B1);
  assert.deepEqual(result, { users: 4, failed: 2 });
  assert.deepEqual(w.calls.commits.map((c) => c.userId), ["u2", "u3"]);
  assert.deepEqual(w.calls.retryable.map((r) => r.userId), ["bad"]);
  assert.ok(w.calls.errors.some((line) => line.includes("boom") && line.includes("database down")));
  const none = world({ users: [] });
  assert.deepEqual(await user.runFxUsers(none.deps, B1), { users: 0, failed: 0 });
  assert.ok(none.calls.logs.some((line) => line.includes("no forex-entitled accounts")));
});

test("a replay is idempotent: an account that consumed the boundary does nothing", async () => {
  const w = world({ stages: [stage(B1)] });
  assert.equal((await user.runFxUser(w.deps, "u1", B1)).status, "ok");
  const again = await user.runFxUser(w.deps, "u1", B1);
  assert.deepEqual(again, { userId: "u1", status: "already_done", consumed: 0 });
  assert.equal(w.calls.wallet.length, 1);
  assert.equal(w.calls.commits.length, 1);
  // No stage committed for the boundary yet: the account waits.
  const waiting = world({ stages: [] });
  assert.equal((await user.runFxUser(waiting.deps, "u1", B1)).status, "no_stage");
  assert.equal(waiting.calls.retryable.length, 0);
});

test("a 40001 Wallet changed is retried once after re-reading the account, a second one fails", async () => {
  const w = world({ stages: [stage(B1)], failCommit: ["40001"] });
  assert.equal((await user.runFxUser(w.deps, "u1", B1)).status, "ok");
  assert.equal(w.calls.commitAttempts, 2);
  assert.equal(w.calls.wallet.length, 2, "the wallet step is recomputed from the fresh state");
  assert.equal(w.calls.commits.length, 1);
  const twice = world({ stages: [stage(B1)], failCommit: ["40001", "40001"] });
  await assert.rejects(user.runFxUser(twice.deps, "u1", B1), /40001/);
  assert.equal(twice.calls.commitAttempts, 2);
  assert.equal(twice.calls.retryable.length, 0, "a commit error is not a refusal");
  const other = world({ stages: [stage(B1)], failCommit: ["23505"] });
  await assert.rejects(user.runFxUser(other.deps, "u1", B1), /23505/);
  assert.equal(other.calls.commitAttempts, 1);
});

// ---------------------------------------------------------------- the hosted client

function fakeClient({ rpc = {}, tables = {} } = {}) {
  const log = { rpc: [], queries: [] };
  function query(table) {
    const q = { table, ops: [] };
    const record = (op) => (...args) => { q.ops.push([op, ...args]); return proxy; };
    const finish = () => {
      log.queries.push(q);
      const handler = tables[table];
      const result = typeof handler === "function" ? handler(q) : handler ?? { data: [], error: null };
      return Promise.resolve(result);
    };
    const proxy = {
      select: record("select"), eq: record("eq"), gt: record("gt"), lte: record("lte"), order: record("order"),
      limit: record("limit"), range: record("range"),
      single: () => { q.ops.push(["single"]); return finish(); },
      maybeSingle: () => { q.ops.push(["maybeSingle"]); return finish(); },
      insert: (row) => { q.ops.push(["insert", row]); return finish(); },
      then: (resolve, reject) => finish().then(resolve, reject),
    };
    return proxy;
  }
  return { log, client: {
    from: query,
    rpc: (name, args) => {
      log.rpc.push([name, args]);
      const handler = rpc[name];
      return Promise.resolve(typeof handler === "function" ? handler(args) : handler ?? { data: null, error: null });
    },
  } };
}
const opsOf = (query, name) => query.ops.filter(([op]) => op === name).map(([, ...args]) => args);

test("listUsers reads fx_entitled_users", async () => {
  const { client, log } = fakeClient({ rpc: { fx_entitled_users: { data: ["u1", "u2"], error: null } } });
  assert.deepEqual(await user.createHostedFxUser(client).listUsers(), ["u1", "u2"]);
  assert.deepEqual(log.rpc, [["fx_entitled_users", undefined]]);
  const broken = fakeClient({ rpc: { fx_entitled_users: { data: null, error: { message: "denied" } } } });
  await assert.rejects(user.createHostedFxUser(broken.client).listUsers(), /fx_entitled_users failed: denied/);
  const wrong = fakeClient({ rpc: { fx_entitled_users: { data: [1], error: null } } });
  await assert.rejects(user.createHostedFxUser(wrong.client).listUsers(), /no id list/);
});

test("account initializes through ensure_fx_account, then reads the forex account and its latest ok forex run", async () => {
  const { client, log } = fakeClient({ tables: {
    forward_wallet_accounts: { data: { account_id: "u1:forex", config: { max_positions: 1000 }, start_boundary_ms: "1000" }, error: null },
    paper_runs: { data: { id: "r9", boundary_ms: 5000, consumed_boundary_ms: 4000, wallet_state: { n: 1 } }, error: null },
  } });
  const account = await user.createHostedFxUser(client).account("u1", 7000);
  assert.deepEqual(log.rpc, [["ensure_fx_account", { p_user_id: "u1", p_boundary_ms: 7000 }]]);
  const [accounts, runs] = log.queries;
  assert.deepEqual(opsOf(accounts, "eq"), [["user_id", "u1"], ["market", "forex"]]);
  assert.deepEqual(opsOf(runs, "eq"), [["user_id", "u1"], ["paper_account", "u1:forex"], ["market", "forex"], ["status", "ok"]]);
  assert.deepEqual(account, { userId: "u1", accountId: "u1:forex", config: { max_positions: 1000 }, startBoundary: 1000,
    cursor: 4000, previousRunId: "r9", state: { n: 1 } });
  // No run yet: the cursor is the start boundary.
  const fresh = fakeClient({ tables: {
    forward_wallet_accounts: { data: { account_id: "u2:forex", config: {}, start_boundary_ms: 2000 }, error: null },
    paper_runs: { data: null, error: null } } });
  assert.deepEqual(await user.createHostedFxUser(fresh.client).account("u2", 2000),
    { userId: "u2", accountId: "u2:forex", config: {}, startBoundary: 2000, cursor: 2000, previousRunId: null, state: null });
  // A refused entitlement stops before any read.
  const denied = fakeClient({ rpc: { ensure_fx_account: { data: null, error: { message: "User is not entitled to forex", code: "42501" } } } });
  await assert.rejects(user.createHostedFxUser(denied.client).account("u3", 1), /not entitled/);
  assert.equal(denied.log.queries.length, 0);
});

test("stagesAfter reads the stages after the cursor in order and pages each stage's events", async () => {
  const eventRows = (from, count) => Array.from({ length: count }, (_, i) => ({ payload: { type: "swap", ms: from + i } }));
  const { client, log } = fakeClient({ tables: {
    fx_market_stages: { data: [{ id: "st1", boundary_ms: "2000", versions: { v: 1 }, params_hash: "h", payload: { from_ms: 1000 }, freshness: null }], error: null },
    fx_market_events: (q) => ({ data: opsOf(q, "range")[0][0] === 0 ? eventRows(0, 500) : eventRows(500, 3), error: null }),
  } });
  const [stage] = await user.createHostedFxUser(client).stagesAfter(1000, 2000);
  assert.deepEqual([stage.id, stage.boundary_ms, stage.fromMs, stage.freshness], ["st1", 2000, 1000, {}]);
  assert.equal(stage.events.length, 503);
  const [stages, first, second] = log.queries;
  assert.deepEqual(opsOf(stages, "gt"), [["boundary_ms", 1000]]);
  assert.deepEqual(opsOf(stages, "lte"), [["boundary_ms", 2000]]);
  assert.deepEqual(opsOf(stages, "order"), [["boundary_ms", { ascending: true }]]);
  assert.deepEqual(opsOf(first, "order").map(([column]) => column), ["ms", "symbol", "sequence"]);
  assert.deepEqual(opsOf(first, "eq"), [["stage_run_id", "st1"]]);
  assert.deepEqual([opsOf(first, "range")[0], opsOf(second, "range")[0]], [[0, 499], [500, 999]]);
  const noWindow = fakeClient({ tables: { fx_market_stages: { data: [{ id: "x", boundary_ms: 1, versions: {}, params_hash: null, payload: {}, freshness: {} }], error: null } } });
  await assert.rejects(user.createHostedFxUser(noWindow.client).stagesAfter(0, 1), /no from_ms/);
});

test("commit calls commit_fx_user_run with the expected run id and keeps the error code; retryable inserts a forex audit row", async () => {
  const { client, log } = fakeClient({ rpc: { commit_fx_user_run: { data: "run-1", error: null } } });
  const hosted = user.createHostedFxUser(client);
  const account = { userId: "u1", accountId: "u1:forex", previousRunId: "r0" };
  assert.equal(await hosted.commit(account, { id: "st1" }, { status: "ok" }, [{ seq: 1 }]), "run-1");
  assert.deepEqual(log.rpc[0], ["commit_fx_user_run", { p_user_id: "u1", p_stage_run_id: "st1", p_run: { status: "ok" },
    p_ledger: [{ seq: 1 }], p_expected_run_id: "r0" }]);
  const stale = fakeClient({ rpc: { commit_fx_user_run: { data: null, error: { message: "Wallet changed; reload and retry", code: "40001" } } } });
  await assert.rejects(user.createHostedFxUser(stale.client).commit(account, { id: "st1" }, {}, []), (error) => error.code === "40001");
  await hosted.retryable(account, 7000, "skipped_stale", "x".repeat(3000));
  const [insert] = opsOf(log.queries[0], "insert");
  assert.equal(log.queries[0].table, "paper_runs");
  assert.deepEqual({ ...insert[0], reason: insert[0].reason.length }, { user_id: "u1", paper_account: "u1:forex", market: "forex",
    run_key: "fx-hour:7000", boundary_ms: 7000, status: "skipped_stale", trigger: "hourly", reason: 2000 });
});

test("the wallet call posts JSON with the token and reports the service's detail on refusal", async () => {
  const seen = [];
  const send = async (url, init) => { seen.push([url, init]); return { ok: true, json: async () => ({ schema_version: 1 }) }; };
  const call = user.createFxWalletCall({ url: "http://127.0.0.1:1/v1/fx/forward/wallet_step", token: "t" }, send);
  assert.deepEqual(await call({ a: 1 }), { schema_version: 1 });
  assert.equal(seen[0][1].method, "POST");
  assert.equal(seen[0][1].redirect, "manual");
  assert.equal(seen[0][1].headers.Authorization, "Bearer t");
  assert.equal(seen[0][1].body, '{"a":1}');
  const refuse = user.createFxWalletCall({ url: "http://127.0.0.1:1/x", token: "t" },
    async () => ({ ok: false, status: 422, json: async () => ({ detail: "Invalid forex wallet request: bad" }) }));
  await assert.rejects(refuse({}), /HTTP 422: Invalid forex wallet request: bad/);
});
