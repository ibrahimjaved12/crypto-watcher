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
const { runMarketStage, runUserStage, runSharedForward, checkpointDueSigma } = await import(stageUrl);
const trafficUrl = await moduleUrl("../src/lib/forward/forward-hosted-traffic.server.ts");
const { createHostedTraffic } = await import(trafficUrl);
const { runHourlyForAccounts } = await import(await moduleUrl("../src/lib/forward/forward-job.server.ts", {
  "./forward-run.server": runUrl, "./forward-stages.server": stageUrl,
  "./forward-hosted-traffic.server": trafficUrl }));
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
  const calls = { market: [], wallet: [], commits: [], sigmaLoads: 0, retryable: [], errors: [],
    saves: [], checkpoints: [], conflicts: [], stageReads: 0, replayStages: [], logs: [] };
  const clock = { boundary: BOUNDARY };
  const stages = [];
  const accounts = new Map([A, B].map((userId, i) => [userId, { userId, accountId: userId,
    cursor: BOUNDARY - HOUR, startBoundary: BOUNDARY - HOUR, previousRunId: null, state: null,
    config: { leverage_cap: 5 + i * 15 } }]));
  const states = Object.fromEntries(SYMBOLS.map(s => [s, sigma(s, BOUNDARY - HOUR - MINUTE)]));
  const entries = new Map(SYMBOLS.map(symbol => [symbol, { state: states[symbol], hosted: {
    checksum: states[symbol].checksum, as_of_ms: states[symbol].as_of_ms, checkpointed_at: BOUNDARY } }]));
  const sigmaCache = {
    load: async (symbol, restore) => {
      const entry = entries.get(symbol);
      if (entry && entry.hosted?.checkpointed_at !== null) return { ...entry, source: "local" };
      const restored = await restore();
      return restored ? { state: restored.state, hosted: { checksum: restored.state.checksum,
        as_of_ms: restored.state.as_of_ms, checkpointed_at: restored.checkpointed_at }, source: "restored" }
        : { state: null, hosted: null, source: "rebuilt" };
    },
    save: async entry => {
      calls.saves.push(structuredClone(entry)); entries.set(entry.state.symbol, entry); return true;
    },
    checkpointed: async (state, at) => {
      entries.get(state.symbol).hosted = { checksum: state.checksum, as_of_ms: state.as_of_ms, checkpointed_at: at };
    },
    conflict: async symbol => {
      calls.conflicts.push(symbol); entries.get(symbol).hosted.checkpointed_at = null;
    },
  };
  const repository = {
    traffic: createHostedTraffic(),
    stageId: async boundary => stages.find(s => s.boundary_ms === boundary)?.id ?? null,
    stage: async boundary => { calls.stageReads++; return stages.find(s => s.boundary_ms === boundary) ?? null; },
    latestStage: async () => {
      const stage = stages.at(-1);
      return stage ? { id: stage.id, boundary_ms: stage.boundary_ms, processed_to_ms: stage.response.processed_to_ms } : null;
    },
    openSetups: async () => [],
    commitMarket: async stage => {
      stages.push(stage); return stage.id;
    },
    account: async user => structuredClone(accounts.get(user)),
    stagesAfter: async (cursor, boundary, current) => {
      calls.replayStages.push(current);
      return stages.filter(s => s.boundary_ms > cursor && s.boundary_ms <= boundary);
    },
    commitUser: async (account, stage, response) => {
      if (account.userId === failingUser) throw new Error("user commit failed");
      calls.commits.push({ user: account.userId, stage: stage.id, response });
      accounts.set(account.userId, { ...account, cursor: stage.boundary_ms,
        previousRunId: stage.id, state: response.wallet_state });
    },
    retryable: async (...args) => calls.retryable.push(args),
  };
  const deps = {
    now: () => clock.boundary + 2 * MINUTE,
    sigmaCache,
    log: line => calls.logs.push(line),
    firstMinute: async () => BOUNDARY - 2 * HOUR,
    readMinutes: async (_symbol, _since, before) => Array.from({ length: 120 }, (_, i) => ({ open_time_ms: before - (120 - i) * MINUTE,
      open: 100, high: 101, low: 99, close: 100, transport: "rest" })),
    listHealth: async symbols => symbols.map(symbol => ({ symbol, timeframe_minutes: 1, status: stale ? "STALE" : "LIVE" })),
    fetchFunding: async () => [],
    repository: { shared: repository,
      loadSigmaStates: async () => { throw new Error("hourly bulk sigma read is forbidden"); },
      restoreSigmaState: async symbol => {
        calls.sigmaLoads++; return states[symbol] ? { state: states[symbol], checkpointed_at: BOUNDARY } : null;
      },
      checkpointSigmaState: async (state, expected) => { calls.checkpoints.push({ state, expected }); },
    },
    callMarket: async body => {
      calls.market.push(body);
      assert.equal("positions" in body, false);
      assert.equal("wallet_config" in body, false);
      if (body.sigma_only) return { sigma_state: sigma(body.symbol, body.to_ms) };
      return { symbol: body.symbol, signals: [], setups: [], resolutions: [],
        events: [{ stage_run_id: body.stage_run_id, symbol: body.symbol, sequence: 0,
          type: "funding", ms: body.to_ms, rate: "0.001", mark: 100 }], adverse_paths: [],
        reasons: noSigma ? { sigma: "no_sigma: fixture" } : {}, processed_to_ms: body.to_ms,
        funding_available: true, sigma_state: sigma(body.symbol, body.to_ms),
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
  return { deps, calls, accounts, stages, repository, entries, states, clock };
}

test("one shared market call per symbol; second wallet consumes identical events without sigma reads", async () => {
  const f = fixture();
  const input = { trigger: "on_demand", strategyIds: ["test"] };
  await runMarketStage(f.deps, input);
  await runUserStage(f.deps, await f.repository.account(A), BOUNDARY, "on_demand");
  await runMarketStage(f.deps, input);
  await runUserStage(f.deps, await f.repository.account(B), BOUNDARY, "on_demand");
  assert.equal(f.calls.market.length, SYMBOLS.length);
  assert.equal(f.calls.sigmaLoads, 0);
  assert.equal(f.calls.saves.length, SYMBOLS.length);
  assert.equal(f.calls.checkpoints.length, 0);
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
  assert.equal(f.calls.stageReads, 0, "the winning calculation is reused without read-back");
  assert.ok(f.calls.replayStages.every(stages => stages.length === 1 && stages[0] === f.stages[0]));
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
    assert.equal(f.calls.saves.length, 0);
    assert.equal(f.calls.checkpoints.length, 0);
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

test("only the process that wins the boundary saves local sigma", async () => {
  const f = fixture();
  f.repository.commitMarket = async stage => {
    f.stages.push({ ...stage, id: "other-winner" }); return "other-winner";
  };
  const stage = await runMarketStage(f.deps, { trigger: "hourly", strategyIds: ["test"] });
  assert.equal(stage.id, "other-winner");
  assert.equal(f.calls.stageReads, 1);
  assert.equal(f.calls.saves.length, 0);
  assert.equal(f.calls.checkpoints.length, 0);
  await runMarketStage(f.deps, { trigger: "hourly", strategyIds: ["test"] });
  assert.equal(f.calls.saves.length, 0, "an existing stage must not touch local sigma");
});

test("a failed market commit leaves all local files unchanged", async () => {
  const f = fixture();
  f.repository.commitMarket = async () => { throw new Error("timeout"); };
  await assert.rejects(runMarketStage(f.deps, { trigger: "hourly", strategyIds: ["test"] }), /timeout/);
  assert.equal(f.calls.saves.length, 0);
  assert.equal(f.calls.checkpoints.length, 0);
});

test("missing local state restores one symbol; a missing hosted row rebuilds", async () => {
  const f = fixture();
  f.entries.delete("BTCUSDT"); f.entries.delete("ETHUSDT"); delete f.states.ETHUSDT;
  const pending = new Map();
  await runMarketStage(f.deps, { trigger: "hourly", strategyIds: ["test"] }, BOUNDARY, BOUNDARY - HOUR, pending);
  assert.equal(f.calls.checkpoints.length, 0);
  await checkpointDueSigma(f.deps, pending);
  assert.equal(f.calls.sigmaLoads, 2);
  assert.equal(f.calls.market.filter(body => body.sigma_only).length, 1);
  assert.ok(f.calls.logs.some(line => line.includes("BTCUSDT source=restored")));
  assert.ok(f.calls.logs.some(line => line.includes("ETHUSDT source=rebuilt")));
  assert.equal(f.calls.checkpoints.length, 1);
  assert.equal(f.calls.checkpoints[0].state.symbol, "ETHUSDT");
  assert.equal(f.calls.checkpoints[0].expected, null);
});

test("daily checkpoint failures do not fail accounts and retry the next hour", async () => {
  const f = fixture();
  for (const entry of f.entries.values()) entry.hosted.checkpointed_at = BOUNDARY - 24 * HOUR;
  f.deps.repository.checkpointSigmaState = async (state, expected) => {
    assert.equal(f.calls.commits.length, 2, "checkpoint happens after both wallets");
    f.calls.checkpoints.push({ state, expected }); throw new Error("checkpoint unavailable");
  };
  const io = { listUsers: async () => [A, B], deps: async () => f.deps, strategyIds: ["test"],
    log: line => f.calls.logs.push(line), error: line => f.calls.errors.push(line) };
  assert.deepEqual(await runHourlyForAccounts(io), { users: 2, failed: 0 });
  assert.equal(f.calls.checkpoints.length, SYMBOLS.length);
  assert.equal(f.calls.commits.length, 2);
  f.clock.boundary += HOUR;
  f.deps.repository.checkpointSigmaState = async (state, expected) => f.calls.checkpoints.push({ state, expected });
  assert.deepEqual(await runHourlyForAccounts(io), { users: 2, failed: 0 });
  assert.equal(f.calls.checkpoints.length, 2 * SYMBOLS.length);
  assert.equal(f.calls.sigmaLoads, 0);
  f.clock.boundary += HOUR;
  await runHourlyForAccounts(io);
  assert.equal(f.calls.checkpoints.length, 2 * SYMBOLS.length, "less than 24h needs no checkpoint");
});

test("a checksum conflict restores hosted state next hour without retrying an overwrite", async () => {
  const f = fixture();
  f.entries.get("BTCUSDT").hosted.checkpointed_at = BOUNDARY - 24 * HOUR;
  f.deps.repository.checkpointSigmaState = async () => { throw Object.assign(new Error("checksum conflict"), { code: "40001" }); };
  const pending = new Map();
  await runMarketStage(f.deps, { trigger: "hourly", strategyIds: ["test"] }, BOUNDARY, BOUNDARY - HOUR, pending);
  assert.equal(f.calls.checkpoints.length, 0);
  await checkpointDueSigma(f.deps, pending);
  assert.deepEqual(f.calls.conflicts, ["BTCUSDT"]);
  assert.equal(f.calls.sigmaLoads, 0);
  f.clock.boundary += HOUR;
  await runMarketStage(f.deps, { trigger: "hourly", strategyIds: ["test"] });
  assert.equal(f.calls.sigmaLoads, 1);
  assert.ok(f.calls.logs.some(line => line.includes("BTCUSDT source=restored")));
});

test("the first range starts at the earliest registered account after failed hours", async () => {
  const f = fixture();
  f.accounts.get(A).startBoundary = BOUNDARY - 5 * HOUR;
  f.accounts.get(B).startBoundary = BOUNDARY - 3 * HOUR;
  await runHourlyForAccounts({ listUsers: async () => [A, B], deps: async () => f.deps,
    strategyIds: ["test"], log: () => {}, error: () => {} });
  assert.ok(f.calls.market.every(body => body.from_ms === BOUNDARY - 5 * HOUR));
});

test("hosted summary is logged once, including the account listing, even on market failure", async () => {
  const f = fixture(), lines = [];
  f.repository.commitMarket = async () => { throw new Error("timeout"); };
  await runHourlyForAccounts({ listUsers: async onRead => { onRead({ users: [{ id: A }] }); return [A]; },
    deps: async () => f.deps, strategyIds: ["test"], log: line => lines.push(line), error: () => {} });
  assert.equal(lines.filter(line => line.startsWith("[forward-hosted]")).length, 1);
  assert.match(lines.at(-1), /reads=1 read_bytes=\d+ writes=0 write_bytes=0/);
});

function hourly(f) {
  return runHourlyForAccounts({ listUsers: async () => [A, B], deps: async () => f.deps,
    strategyIds: ["test"], log: line => f.calls.logs.push(line), error: line => f.calls.errors.push(line) });
}
function gapFixture(days, committed = true) {
  const f = fixture(), start = BOUNDARY - days * 24 * HOUR;
  for (const account of f.accounts.values()) { account.cursor = start; account.startBoundary = start; }
  for (const [symbol, entry] of f.entries) {
    entry.state = sigma(symbol, start - MINUTE);
    entry.hosted = { checksum: entry.state.checksum, as_of_ms: start - MINUTE, checkpointed_at: start };
  }
  if (committed) f.stages.push({ id: "previous", boundary_ms: start,
    response: { processed_to_ms: Object.fromEntries(SYMBOLS.map(symbol => [symbol, start - MINUTE])) } });
  return f;
}

test("three-day gap commits three ordered daily chunks and both wallets consume all three", async () => {
  const f = gapFixture(3);
  let openReads = 0;
  f.repository.openSetups = async () => { assert.equal(f.stages.length, ++openReads); return []; };
  const expectedChecksums = new Map([...f.entries].map(([symbol, entry]) => [symbol, entry.hosted.checksum]));
  f.deps.repository.checkpointSigmaState = async (state, expected) => {
    assert.equal(f.calls.commits.length, 6);
    assert.equal(state.as_of_ms, BOUNDARY - MINUTE);
    assert.equal(expected, expectedChecksums.get(state.symbol));
    f.calls.checkpoints.push({ state, expected });
  };
  assert.deepEqual(await hourly(f), { users: 2, failed: 0 });
  const stages = f.stages.slice(1);
  assert.deepEqual(stages.map(stage => stage.boundary_ms), [BOUNDARY - 48 * HOUR, BOUNDARY - 24 * HOUR, BOUNDARY]);
  assert.ok(f.calls.market.filter(body => !body.sigma_only).every(body => body.to_ms - body.from_ms <= 24 * HOUR));
  assert.equal(openReads, 3);
  for (const user of [A, B]) {
    assert.deepEqual(f.calls.commits.filter(commit => commit.user === user).map(commit => commit.stage), stages.map(stage => stage.id));
    assert.equal(f.accounts.get(user).cursor, BOUNDARY);
  }
  assert.ok(f.calls.replayStages.every(values => values.length === 3 && values.every((value, i) => value === stages[i])));
  assert.equal(f.calls.checkpoints.length, SYMBOLS.length);
});

test("catch-up stops at seven chunks and the next hourly run continues", async () => {
  const f = gapFixture(10);
  await hourly(f);
  assert.equal(f.stages.length, 8);
  assert.equal(f.accounts.get(A).cursor, BOUNDARY - 3 * 24 * HOUR);
  assert.ok(f.calls.logs.includes("[forward-catchup] chunks=7 remaining_hours=72"));
  await hourly(f);
  assert.equal(f.stages.length, 11);
  assert.equal(f.accounts.get(A).cursor, BOUNDARY);
});

test("first run clamps to retained candles, records freshness and uses the same chunks on demand", async () => {
  const f = gapFixture(150, false);
  const result = await runSharedForward(f.deps, { userId: A, trigger: "on_demand", strategyIds: ["test"] });
  assert.equal(result.status, "ok");
  assert.equal(f.stages.length, 7);
  assert.equal(f.calls.market.find(body => !body.sigma_only).from_ms, BOUNDARY - 120 * 24 * HOUR);
  assert.equal(f.stages[0].freshness.range_clamped_hours, 30 * 24);
  assert.ok(f.calls.logs.includes(`[forward-range] clamped from=${new Date(BOUNDARY - 150 * 24 * HOUR).toISOString()} to=${new Date(BOUNDARY - 120 * 24 * HOUR).toISOString()}`));
  assert.equal(f.accounts.get(A).cursor, f.stages.at(-1).boundary_ms);
  assert.equal(f.accounts.get(B).cursor, BOUNDARY - 150 * 24 * HOUR);
});

test("a non-stage stops catch-up after replaying already committed chunks", async () => {
  const f = gapFixture(3);
  const market = f.deps.callMarket;
  f.deps.callMarket = async body => {
    const response = await market(body);
    if (body.to_ms === BOUNDARY - 24 * HOUR - MINUTE) response.reasons = { sigma: "no_sigma: fixture" };
    return response;
  };
  await hourly(f);
  assert.equal(f.stages.length, 2);
  assert.equal(f.calls.commits.length, 2);
  assert.equal(f.calls.retryable.length, 2);
  assert.equal(f.accounts.get(A).cursor, BOUNDARY - 48 * HOUR);
});

const { createSharedRepository } = await import(await moduleUrl("../src/lib/forward/forward-shared-repository.server.ts", {
  "./forward-contract": contractUrl, "./forward-run.server": runUrl, "./forward-hosted-traffic.server": trafficUrl }));
function outcomesFixture(extra = false, failPage = false) {
  const setups = Array.from({ length: 250 }, (_, i) => ({ setup_id: String(i).padStart(4, "0"), payload: { setup_id: String(i).padStart(4, "0") } }));
  const outcomes = setups.flatMap((setup, i) => i % 5 === 0 ? [] : [
    { setup_id: setup.setup_id, status: "open", created_at: 2, payload: { status: "open", marker: `new-${i}` } },
    { setup_id: setup.setup_id, status: i % 3 === 0 ? "T" : "open", created_at: 1, payload: { status: i % 3 === 0 ? "T" : "open", marker: `old-${i}` } },
  ]);
  if (extra) {
    // One setup crosses the page boundary; its final outcome is on the second page.
    outcomes.push(...Array.from({ length: 501 }, (_, i) => ({ setup_id: "0000", status: i === 500 ? "S" : "open",
      created_at: 1000 - i, payload: { status: i === 500 ? "S" : "open", marker: i } })));
  }
  const queries = [], client = { from(table) {
    const query = { table, orders: [], offset: 0, count: 500, ids: null, after: null };
    queries.push(query);
    const builder = {
      select() { return builder; },
      in(key, ids) { assert.equal(key, "setup_id"); assert.ok(ids.length <= 100); query.ids = ids; return builder; },
      gt(_key, value) { query.after = value; return builder; },
      order(key, options) { query.orders.push([key, options.ascending]); return builder; },
      limit(count) { query.count = count; return builder; },
      range(from, to) { query.offset = from; query.count = to - from + 1; return builder; },
      then(ok, fail) {
        let rows = table === "forward_market_open_setups" ? setups : outcomes;
        if (query.ids) rows = rows.filter(row => query.ids.includes(row.setup_id));
        if (query.after !== null) rows = rows.filter(row => row.setup_id > query.after);
        rows = rows.toSorted((a, b) => {
          for (const [key, ascending] of query.orders) {
            if (a[key] !== b[key]) return (a[key] < b[key] ? -1 : 1) * (ascending ? 1 : -1);
          }
          return 0;
        }).slice(query.offset, query.offset + query.count);
        return Promise.resolve({ data: rows, error: failPage && query.offset === 500 ? { message: "page unavailable" } : null }).then(ok, fail);
      },
    };
    return builder;
  } };
  const expected = setups.flatMap(setup => {
    const group = outcomes.filter(row => row.setup_id === setup.setup_id).toSorted((a, b) => b.created_at - a.created_at);
    return group.some(row => ["T", "S", "E", "L", "X", "ambiguous"].includes(row.status)) ? []
      : [{ setup: setup.payload, resolution: group[0]?.payload ?? null }];
  });
  return { client, queries, expected };
}

test("250 open setups use three bulk outcome reads with the old final-status and newest-payload semantics", async () => {
  const f = outcomesFixture(), traffic = createHostedTraffic();
  const result = await createSharedRepository(f.client, traffic).openSetups();
  assert.deepEqual(result, f.expected);
  const reads = f.queries.filter(query => query.table === "forward_market_outcomes");
  assert.equal(reads.length, 3);
  assert.ok(reads.every(query => JSON.stringify(query.orders) === JSON.stringify([["setup_id", true], ["created_at", false]])));
  assert.equal(traffic.snapshot().reads, f.queries.length);
});

test("outcome pagination retains older final outcomes across a page boundary and measures each page", async () => {
  const f = outcomesFixture(true), traffic = createHostedTraffic();
  assert.deepEqual(await createSharedRepository(f.client, traffic).openSetups(), f.expected);
  assert.ok(f.queries.some(query => query.offset === 500));
  assert.equal(traffic.snapshot().reads, f.queries.length);
  const broken = outcomesFixture(true, true), failedTraffic = createHostedTraffic();
  await assert.rejects(createSharedRepository(broken.client, failedTraffic).openSetups(), /market outcomes read.*page unavailable/);
  assert.equal(failedTraffic.snapshot().reads, broken.queries.length);
});

test("an old committed cursor is clamped before the next chunk evaluates", async () => {
  const f = gapFixture(150);
  await hourly(f);
  assert.equal(f.calls.market.find(body => !body.sigma_only).from_ms, BOUNDARY - 120 * 24 * HOUR);
  assert.equal(f.stages[1].freshness.range_clamped_hours, 30 * 24 + MINUTE / HOUR);
  assert.equal(f.stages[1].boundary_ms, BOUNDARY - 119 * 24 * HOUR);
});
