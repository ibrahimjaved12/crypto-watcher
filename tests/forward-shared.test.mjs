import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

async function moduleUrl(path, imports = {}) {
  let { outputText } = ts.transpileModule(await readFile(new URL(path, import.meta.url), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  });
  for (const [specifier, target] of Object.entries(imports))
    outputText = outputText.replaceAll(JSON.stringify(specifier), JSON.stringify(target));
  return `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`;
}
const contractUrl = await moduleUrl("../src/lib/forward/forward-contract.ts", { zod: import.meta.resolve("zod") });
const runUrl = await moduleUrl("../src/lib/forward/forward-run.server.ts", { "./forward-contract": contractUrl });
const stageUrl = await moduleUrl("../src/lib/forward/forward-stages.server.ts", {
  "./forward-contract": contractUrl, "./forward-run.server": runUrl });
const { runMarketStage, runUserStage } = await import(stageUrl);
const { runHourlyForAccounts } = await import(await moduleUrl("../src/lib/forward/forward-job.server.ts", {
  "./forward-run.server": runUrl, "./forward-stages.server": stageUrl }));
const { sigmaCanonical } = await import(contractUrl);
const HOUR = 3_600_000, MINUTE = 60_000, BOUNDARY = 1_760_000_000_000 - 1_760_000_000_000 % HOUR;
const SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT"];
const A = "11111111-1111-4111-8111-111111111111", B = "22222222-2222-4222-8222-222222222222";
function sigma(symbol, asOf) {
  const body = { symbol, sigma_version: "forward-sigma-v1", as_of_ms: asOf,
    payload: { source: "store_rebuild", symbol, as_of_ms: String(asOf) } };
  return { ...body, checksum: createHash("sha256").update(sigmaCanonical(body)).digest("hex") };
}
function fixture({ stale = false, noSigma = false, failingUser = null } = {}) {
  const calls = { market: [], wallet: [], commits: [], sigmaLoads: 0, retryable: [], errors: [] };
  const stages = [];
  const accounts = new Map([A, B].map((userId, i) => [userId, { userId, accountId: userId,
    cursor: BOUNDARY - HOUR, startBoundary: BOUNDARY - HOUR, previousRunId: null, state: null,
    config: { leverage_cap: 5 + i * 15 } }]));
  let states = Object.fromEntries(SYMBOLS.map(s => [s, sigma(s, BOUNDARY - HOUR - MINUTE)]));
  const repository = {
    stage: async boundary => stages.find(s => s.boundary_ms === boundary) ?? null,
    latestStage: async () => stages.at(-1) ?? null,
    openSetups: async () => [],
    commitMarket: async (stage, previous, versions, advanced) => {
      stages.push(stage); states = Object.fromEntries(advanced.map(s => [s.symbol, s]));
    },
    account: async user => structuredClone(accounts.get(user)),
    stagesAfter: async (cursor, boundary) => stages.filter(s => s.boundary_ms > cursor && s.boundary_ms <= boundary),
    commitUser: async (account, stage, response) => {
      if (account.userId === failingUser) throw new Error("user commit failed");
      calls.commits.push({ user: account.userId, stage: stage.id, response });
      accounts.set(account.userId, { ...account, cursor: stage.boundary_ms,
        previousRunId: stage.id, state: response.wallet_state });
    },
    retryable: async (...args) => calls.retryable.push(args),
  };
  const deps = {
    now: () => BOUNDARY + 2 * MINUTE,
    firstMinute: async () => BOUNDARY - 2 * HOUR,
    readMinutes: async () => Array.from({ length: 120 }, (_, i) => ({ open_time_ms: BOUNDARY - (120 - i) * MINUTE,
      open: 100, high: 101, low: 99, close: 100, transport: "rest" })),
    listHealth: async symbols => symbols.map(symbol => ({ symbol, timeframe_minutes: 1, status: stale ? "STALE" : "LIVE" })),
    fetchFunding: async () => [],
    repository: { shared: repository, loadSigmaStates: async () => { calls.sigmaLoads++; return states; } },
    callMarket: async body => {
      calls.market.push(body);
      assert.equal("positions" in body, false);
      assert.equal("wallet_config" in body, false);
      assert.equal(body.sigma_only, undefined, "a second user must never rebuild the shared checkpoint");
      return { symbol: body.symbol, signals: [], setups: [], resolutions: [],
        events: [{ stage_run_id: body.stage_run_id, symbol: body.symbol, sequence: 0,
          type: "funding", ms: BOUNDARY - MINUTE, rate: "0.001", mark: 100 }], adverse_paths: [],
        reasons: noSigma ? { sigma: "no_sigma: fixture" } : {}, processed_to_ms: BOUNDARY - MINUTE,
        funding_available: true, sigma_state: sigma(body.symbol, BOUNDARY - MINUTE),
        versions: { forward: "forward-v1" }, params_hash: "a".repeat(64), assumptions: [] };
    },
    callWallet: async body => {
      calls.wallet.push(structuredClone(body));
      const amount = -body.config.leverage_cap;
      return { wallet_config: body.config, assumptions: [], wallet_state: { version: "wallet-v2",
        balance_e8: 1000 + amount, seq: 1, used_margin_e8: 0, positions: {}, done: [] },
        ledger: [{ seq: 1, ms: BOUNDARY - MINUTE, type: "funding", amount_e8: amount,
          balance_e8: 1000 + amount, assumptions: [] }] };
    },
  };
  return { deps, calls, accounts, stages, repository };
}

test("one shared market call per symbol; second wallet consumes identical events without sigma reads", async () => {
  const f = fixture();
  const input = { trigger: "on_demand", strategyIds: ["test"] };
  await runMarketStage(f.deps, input);
  await runUserStage(f.deps, await f.repository.account(A), BOUNDARY, "on_demand");
  await runMarketStage(f.deps, input);
  await runUserStage(f.deps, await f.repository.account(B), BOUNDARY, "on_demand");
  assert.equal(f.calls.market.length, SYMBOLS.length);
  assert.equal(f.calls.sigmaLoads, 1);
  assert.deepEqual(f.calls.wallet[0].events, f.calls.wallet[1].events);
  assert.notEqual(f.calls.commits[0].response.ledger[0].amount_e8, f.calls.commits[1].response.ledger[0].amount_e8);
  for (const user of [A, B])
    assert.equal((await runUserStage(f.deps, await f.repository.account(user), BOUNDARY, "hourly")).status, "already_done");
  assert.equal(f.calls.wallet.length, 2);
});

test("hourly job isolates a failing user after the one shared stage commits", async () => {
  const f = fixture({ failingUser: A });
  const result = await runHourlyForAccounts({ listUsers: async () => [A, B], deps: async () => f.deps,
    strategyIds: ["test"], log: () => {}, error: line => f.calls.errors.push(line) });
  assert.deepEqual(result, { users: 2, failed: 1 });
  assert.equal(f.stages.length, 1);
  assert.equal(f.calls.market.length, SYMBOLS.length);
  assert.deepEqual(f.calls.commits.map(c => c.user), [B]);
  assert.equal(f.accounts.get(A).cursor, BOUNDARY - HOUR);
  assert.equal(f.accounts.get(B).cursor, BOUNDARY);
});

test("retryable stages never persist market state or advance either user's cursor", async () => {
  for (const options of [{ stale: true }, { noSigma: true }]) {
    const f = fixture(options);
    await runHourlyForAccounts({ listUsers: async () => [A, B], deps: async () => f.deps,
      strategyIds: ["test"], log: () => {}, error: () => {} });
    assert.equal(f.stages.length, 0);
    assert.equal(f.calls.wallet.length, 0);
    assert.equal(f.calls.retryable.length, 2);
    assert.equal(f.accounts.get(A).cursor, BOUNDARY - HOUR);
  }
});

test("late account consumes no history and a lagging account replays stages in order", async () => {
  const f = fixture();
  await runMarketStage(f.deps, { trigger: "hourly", strategyIds: ["test"] });
  const late = { ...await f.repository.account(A), cursor: BOUNDARY, startBoundary: BOUNDARY };
  assert.equal((await runUserStage(f.deps, late, BOUNDARY, "hourly")).status, "already_done");
  f.stages.push({ ...f.stages[0], id: "next-stage", boundary_ms: BOUNDARY + HOUR });
  await runUserStage(f.deps, await f.repository.account(B), BOUNDARY + HOUR, "hourly");
  assert.deepEqual(f.calls.commits.map(c => c.stage), f.stages.map(s => s.id));
  assert.deepEqual(f.calls.wallet[1].state, f.calls.commits[0].response.wallet_state);
});
