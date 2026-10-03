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
const zod = import.meta.resolve("zod");
const contractUrl = await moduleUrl("../src/lib/forward/forward-contract.ts", { zod });
const trendContractUrl = await moduleUrl("../src/lib/forward/forward-trend-contract.ts", { zod });
const runUrl = await moduleUrl("../src/lib/forward/forward-run.server.ts", {
  "./forward-contract": contractUrl,
});
const pagesUrl = await moduleUrl("../src/lib/forward/forward-trend-pages.ts");
const trend = await import(
  await moduleUrl("../src/lib/forward/forward-trend-run.server.ts", {
    "./forward-run.server": runUrl,
    "./forward-trend-contract": trendContractUrl,
  })
);
const contract = await import(trendContractUrl);
const { createTrendRepository } = await import(
  await moduleUrl("../src/lib/forward/forward-trend-repository.server.ts", {
    "./forward-trend-pages": pagesUrl,
  })
);
const { summarizeTrack, loadTrendDashboard } = await import(
  await moduleUrl("../src/lib/forward/forward-trend-dashboard.ts", {
    "./forward-trend-pages": pagesUrl,
  })
);
const { listForwardUsers } = await import(
  await moduleUrl("../src/lib/forward/forward-users.server.ts")
);

const DAY = contract.DAY_MS;
const NOW = Date.UTC(2026, 9, 9, 0, 7);
const LAST = NOW - (NOW % DAY) - DAY;
const USER = "11111111-1111-4111-8111-111111111111";
const hex = (c) => c.repeat(64);
const info = {
  name: "ens_ls_25",
  kind: "variant",
  control: "bh_vt_std90_25",
  config_hash: hex("b"),
};
function kline(day) {
  return [day, "100", "103", "99", "101.5", "10", day + DAY - 1, "1000"];
}
function sample(n = 0) {
  return {
    n_days: n,
    sum_ppm: 120 * n,
    sum_sq_ppm: 14400 * n,
    turnover_sum_ppm: 0,
    gross_sum_ppm: 500000 * n,
    equity_ppm: 1000000 + 120 * n,
    mean_daily: n ? 0.00012 : null,
    sd_daily: n > 1 ? 0.0002 : null,
    mu_min_daily: n > 1 ? 0.00025 : null,
    days_needed: n > 1 ? 22 : null,
    mean_turnover: n ? 0 : null,
    mean_gross: n ? 0.5 : null,
  };
}
function response(patch = {}) {
  return {
    schema_version: 1,
    versions: { trend_track: "trend-track-v2", strategy: "trend-v1" },
    params_hash: hex("a"),
    symbols: ["BTCUSDT"],
    track_start_ms: trend.TREND_TRACK_START_MS,
    history_start_ms: trend.TREND_HISTORY_START_MS,
    tracks: [info],
    through_day_ms: LAST,
    ledger: [
      {
        track: info.name,
        day_ms: LAST,
        daily_ppm: 120,
        equity_ppm: 1000120,
        sample_kind: "retrospective",
        sample_equity_ppm: 1000120,
        turnover_ppm: 0,
        gross_ppm: 500000,
        symbols_active: 1,
        weights: { BTCUSDT: 0.5 },
      },
    ],
    weights: [
      {
        track: info.name,
        day_ms: LAST + DAY,
        decided_from_close_ms: LAST,
        decided_at_ms: NOW,
        weights: { BTCUSDT: 0.5 },
        defined: { BTCUSDT: true },
      },
    ],
    states: {
      [info.name]: {
        version: "trend-track-v2",
        track: info.name,
        last_day_ms: LAST,
        equity_ppm: 1000120,
        n_days: 1,
        sum_ppm: 120,
        sum_sq_ppm: 14400,
        peak_equity_ppm: 1000120,
        max_drawdown_ppm: 0,
        weights: { BTCUSDT: 0.5 },
        mu_min_daily: null,
        samples: { prospective: sample(), retrospective: sample(1) },
      },
    },
    funding_unavailable: [],
    reasons: {},
    assumptions: ["hypothetical-no-margin"],
    ...patch,
  };
}
function fakeDeps({ fundingFails = false, reply = () => response() } = {}) {
  const calls = {
    python: [],
    fetches: [],
    recorded: [],
    funding: [],
    commits: [],
    diagnostics: [],
  };
  const db = new Map();
  let snapshot = null;
  const okRuns = new Set();
  const decisions = new Map();
  const repository = {
    async findOkRun(userId, runKey) {
      return okRuns.has(`${userId}|${runKey}`);
    },
    async latestSnapshot() {
      return snapshot;
    },
    async readDecisions(userId, from) {
      return [...decisions.values()].filter((row) => row.day_ms >= from);
    },
    async commitRun(userId, expectedStateId, value, row) {
      assert.equal(expectedStateId, snapshot?.id ?? null);
      calls.commits.push({ userId, expectedStateId, value, row });
      snapshot = {
        id: `state-${calls.commits.length}`,
        params_hash: value.params_hash,
        symbols: value.symbols,
        history_start_ms: value.history_start_ms,
        track_start_ms: value.track_start_ms,
        states: value.states,
      };
      for (const weight of value.weights)
        decisions.set(`${weight.track}|${weight.day_ms}`, {
          ...weight,
          recorded_at_ms: NOW,
          sample_kind: "prospective",
        });
      if (row.status === "ok") okRuns.add(`${userId}|${row.run_key}`);
      return {
        bars_inserted: row.counts.bars_inserted,
        ledger: value.ledger.length,
        weights: value.weights.length,
      };
    },
    async recordDiagnostic(userId, row) {
      calls.diagnostics.push({ userId, row });
    },
  };
  return {
    calls,
    deps: {
      now: () => NOW,
      readDailyBars: async (symbol, since) =>
        [...db.values()]
          .filter((bar) => bar.symbol === symbol && bar.day_ms >= since)
          .sort((a, b) => a.day_ms - b.day_ms),
      recordDailyBars: async (bars) => {
        calls.recorded.push(bars);
        let changed = 0;
        for (const bar of bars) {
          const key = `${bar.symbol}|${bar.day_ms}`;
          if (!db.has(key)) {
            db.set(key, bar);
            changed++;
          }
        }
        return changed;
      },
      fetchDailyKlines: async (symbol, startMs, nowMs) => {
        calls.fetches.push({ symbol, startMs });
        const end = Math.floor(nowMs / DAY) * DAY;
        const rows = [];
        for (let day = startMs; day <= end && rows.length < 1500; day += DAY) rows.push(kline(day));
        return contract.parseBinanceDailyKlines(symbol, rows, nowMs);
      },
      fetchFunding: async (symbol, startMs) => {
        calls.funding.push({ symbol, startMs });
        if (fundingFails) throw new Error("HTTP 429");
        const rows = [];
        let next = Math.ceil(startMs / trend.FUNDING_INTERVAL_MS) * trend.FUNDING_INTERVAL_MS;
        for (; next <= LAST + DAY && rows.length < 1000; next += trend.FUNDING_INTERVAL_MS)
          rows.push({ calc_time_ms: next, rate: "0" });
        return rows;
      },
      fetchFundingIntervals: async () => ({}),
      callPython: async (body) => {
        calls.python.push(body);
        return reply(body);
      },
      repository,
    },
  };
}

test("daily klines keep only completed UTC days and reject malformed rows", () => {
  assert.deepEqual(
    contract
      .parseBinanceDailyKlines("BTCUSDT", [kline(LAST), kline(LAST + DAY)], NOW)
      .map((row) => row.day_ms),
    [LAST],
  );
  assert.throws(
    () => contract.parseBinanceDailyKlines("BTCUSDT", [kline(LAST), kline(LAST)], NOW),
    /Invalid/,
  );
  assert.throws(
    () => contract.parseBinanceDailyKlines("BTCUSDT", [kline(LAST + 1)], NOW),
    /Invalid/,
  );
});

test("initialization pages from January 2020; resume appends and passes saved identity and decisions", async () => {
  const { deps, calls } = fakeDeps();
  const input = { userId: USER, trigger: "daily", symbols: ["BTCUSDT"] };
  assert.equal((await trend.runForwardTrend(deps, input)).status, "ok");
  assert.equal(trend.TREND_HISTORY_START_MS, Date.UTC(2020, 0, 1));
  assert.equal(calls.fetches[0].startMs, trend.TREND_HISTORY_START_MS);
  assert.equal(calls.fetches.length, 2);
  const sent = calls.python[0].symbols[0].bars;
  assert.equal(sent.length, (LAST - trend.TREND_HISTORY_START_MS) / DAY + 1);
  assert.equal(sent.at(-1).day_ms, LAST);
  assert.equal(calls.python[0].history_start_ms, Date.UTC(2020, 0, 1));
  assert.equal(calls.funding[0].startMs, trend.TREND_TRACK_START_MS + 1);
  assert.equal(calls.commits[0].expectedStateId, null);
  assert.equal((await trend.runForwardTrend(deps, input)).status, "already_done");
  const next = await trend.runForwardTrend({ ...deps, now: () => NOW + DAY }, input);
  assert.equal(next.status, "partial");
  assert.equal(calls.fetches.at(-1).startMs, LAST + DAY);
  assert.equal(calls.python.at(-1).saved_params_hash, hex("a"));
  assert.equal(calls.python.at(-1).decisions.length, 1);
  assert.equal(calls.commits.at(-1).expectedStateId, "state-1");
});

test("initial symbol fetch failure is diagnostics only; recovery starts with the full universe", async () => {
  const { deps, calls } = fakeDeps({
    reply: (body) => response({ symbols: body.expected_symbols }),
  });
  const fetch = deps.fetchDailyKlines;
  deps.fetchDailyKlines = (symbol, ...args) =>
    symbol === "ETHUSDT" ? Promise.reject(new Error("failed")) : fetch(symbol, ...args);
  const input = { userId: USER, trigger: "daily", symbols: ["BTCUSDT", "ETHUSDT"] };
  assert.equal((await trend.runForwardTrend(deps, input)).status, "partial");
  assert.equal(calls.python.length, 0);
  assert.equal(calls.commits.length, 0);
  assert.equal(calls.diagnostics.length, 1);
  assert.deepEqual(calls.diagnostics[0].row.states, {});
  deps.fetchDailyKlines = fetch;
  await trend.runForwardTrend(deps, input);
  assert.deepEqual(
    calls.python[0].symbols.map((item) => item.symbol),
    input.symbols,
  );
  assert.equal(calls.python[0].states, null);
  assert.equal(calls.commits[0].expectedStateId, null);
  assert.equal(
    calls.fetches.filter(
      (call) => call.startMs === trend.TREND_HISTORY_START_MS && call.symbol === "BTCUSDT",
    ).length,
    2,
  );
});

test("failed and successful-empty funding remain unavailable", async () => {
  for (const fails of [false, true]) {
    const { deps, calls } = fakeDeps({
      fundingFails: fails,
      reply: () =>
        response({
          ledger: [],
          through_day_ms: null,
          funding_unavailable: ["BTCUSDT"],
          reasons: { [info.name]: "funding_unavailable:BTCUSDT" },
        }),
    });
    if (!fails) deps.fetchFunding = async () => [];
    const result = await trend.runForwardTrend(deps, {
      userId: USER,
      trigger: "daily",
      symbols: ["BTCUSDT"],
    });
    assert.equal(result.status, "partial");
    assert.equal(calls.python[0].symbols[0].funding_available, false);
    assert.equal(calls.python[0].symbols[0].funding_to_ms, null);
    assert.match(result.reason, /funding_unavailable/);
  }
});

test("funding coverage requires all settlements through day-end, regardless of page size", async () => {
  const from = trend.TREND_TRACK_START_MS;
  assert.deepEqual(
    await trend.fetchTrendFunding(
      {
        fetchFunding: async () => {
          throw new Error("No fetch needed");
        },
      },
      "BTCUSDT",
      from,
      from,
    ),
    { events: [], toMs: from },
  );
  const step = trend.FUNDING_INTERVAL_MS;
  const events = Array.from({ length: 1230 }, (_, i) => ({
    calc_time_ms: from + (i + 1) * step,
    rate: "0",
  }));
  let requests = 0;
  const deps = {
    async fetchFunding(symbol, cursor) {
      requests++;
      return events.filter((event) => event.calc_time_ms >= cursor).slice(0, 1000);
    },
  };
  const complete = await trend.fetchTrendFunding(deps, "BTCUSDT", from, from + 410 * DAY);
  assert.equal(requests, 2);
  assert.equal(complete.toMs, from + 410 * DAY);
  for (const rows of [
    [],
    events.slice(0, 2),
    [events[0], events[2]],
    [events[0], events[0], events[1]],
  ]) {
    assert.equal(
      (
        await trend.fetchTrendFunding(
          { fetchFunding: async () => rows },
          "BTCUSDT",
          from,
          from + DAY,
        )
      ).toMs,
      null,
    );
  }
  const partial = await trend.fetchTrendFunding(
    { fetchFunding: async () => events.slice(0, 4) },
    "BTCUSDT",
    from,
    from + 2 * DAY,
  );
  assert.equal(partial.toMs, from + DAY);
  const hourly = Array.from({ length: 24 }, (_, i) => ({
    calc_time_ms: from + (i + 1) * 3_600_000,
    rate: "0",
  }));
  assert.equal(
    (
      await trend.fetchTrendFunding(
        { fetchFunding: async () => hourly },
        "BTCUSDT",
        from,
        from + DAY,
        3_600_000,
      )
    ).toMs,
    from + DAY,
  );
  assert.equal(
    (
      await trend.fetchTrendFunding(
        { fetchFunding: async () => hourly.filter((_, i) => i !== 12) },
        "BTCUSDT",
        from,
        from + DAY,
        3_600_000,
      )
    ).toMs,
    null,
  );
});

test("adjusted funding intervals are validated; missing schedule metadata cannot finalize days", async () => {
  assert.deepEqual(
    contract.parseBinanceFundingIntervals([{ symbol: "BTCUSDT", fundingIntervalHours: 4 }]),
    { BTCUSDT: 4 * 3_600_000 },
  );
  assert.throws(
    () => contract.parseBinanceFundingIntervals([{ symbol: "BTCUSDT", fundingIntervalHours: 0 }]),
    /Invalid/,
  );
  const { deps, calls } = fakeDeps();
  deps.fetchFundingIntervals = async () => {
    throw new Error("metadata failed");
  };
  const result = await trend.runForwardTrend(deps, {
    userId: USER,
    trigger: "daily",
    symbols: ["BTCUSDT"],
  });
  assert.equal(result.status, "partial");
  assert.equal(calls.python[0].symbols[0].funding_available, false);
  assert.match(result.reason, /settlement schedule unavailable/);
});

test("a lagging resumed symbol holds back every track", async () => {
  const { deps, calls } = fakeDeps({
    reply: (body) => response({ symbols: body.expected_symbols }),
  });
  const input = { userId: USER, trigger: "daily", symbols: ["BTCUSDT", "ETHUSDT"] };
  await trend.runForwardTrend(deps, input);
  const fetch = deps.fetchDailyKlines;
  deps.fetchDailyKlines = (symbol, ...args) =>
    symbol === "ETHUSDT" ? Promise.reject(new Error("HTTP 418")) : fetch(symbol, ...args);
  const result = await trend.runForwardTrend({ ...deps, now: () => NOW + DAY }, input);
  assert.equal(result.status, "partial");
  assert.equal(calls.python.at(-1).through_day_ms, LAST);
  assert.match(result.reason, /ETHUSDT/);
});

test("response contract refuses malformed or duplicate decisions", () => {
  assert.equal(contract.validateTrendResponse(response()).ledger.length, 1);
  assert.throws(
    () => contract.validateTrendResponse(response({ schema_version: 2 })),
    /schema_version/,
  );
  const late = response();
  late.weights[0].decided_from_close_ms = LAST + DAY;
  assert.throws(() => contract.validateTrendResponse(late), /weights/);
  const twice = response();
  twice.ledger.push(twice.ledger[0]);
  assert.throws(() => contract.validateTrendResponse(twice), /duplicate ledger/);
});

test("repository calls one account-scoped transaction with an expected prior state", async () => {
  const calls = [];
  const repository = createTrendRepository({
    async rpc(name, args) {
      calls.push({ name, args });
      return { data: { ledger: 1, weights: 1 }, error: null };
    },
  });
  const value = contract.validateTrendResponse(response());
  assert.deepEqual(await repository.commitRun(USER, "prior-id", value, { run_key: "new" }), {
    ledger: 1,
    weights: 1,
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].name, "commit_forward_trend_run");
  assert.equal(calls[0].args.p_user_id, USER);
  assert.equal(calls[0].args.p_expected_state_id, "prior-id");
  assert.equal(calls[0].args.p_response.params_hash, hex("a"));
});

test("dashboard uses Python statistics and separates reconstructed days", () => {
  const state = response().states[info.name];
  state.samples.prospective = sample(5);
  state.samples.retrospective = sample(8);
  const rows = [
    { track: info.name, day_ms: LAST, sample_kind: "retrospective", sample_equity_ppm: 1000200 },
    {
      track: info.name,
      day_ms: LAST + DAY,
      sample_kind: "prospective",
      sample_equity_ppm: 1000100,
    },
  ];
  const summary = summarizeTrack(info, rows, state);
  assert.equal(summary.days, 5);
  assert.equal(summary.retrospectiveDays, 8);
  assert.equal(summary.muMinDaily, 0.00025);
  assert.equal(summary.daysNeeded, 22);
  assert.equal(summary.equity.length, 1);
  assert.match(summary.edgeLine, /Minimum detectable edge/);
  assert.doesNotMatch(summary.edgeLine, /significant|verdict/);
  assert.equal(summarizeTrack(info, rows, state, "retrospective").days, 8);
});

function pagedClient(tables, cap = 317) {
  const queries = [];
  return {
    queries,
    from(table) {
      let own = [...(tables[table] ?? [])];
      const filters = [];
      const orders = [];
      const query = {
        select() {
          return query;
        },
        eq(key, value) {
          filters.push((row) => row[key] === value);
          return query;
        },
        lte(key, value) {
          filters.push((row) => row[key] <= value);
          return query;
        },
        gte(key, value) {
          filters.push((row) => row[key] >= value);
          return query;
        },
        order(key, { ascending = true } = {}) {
          orders.push([key, ascending]);
          return query;
        },
        range(start, end) {
          query.start = start;
          query.end = end;
          return query;
        },
        limit(n) {
          query.end = n - 1;
          return query;
        },
        maybeSingle() {
          query.single = true;
          return query;
        },
        then(resolve, reject) {
          own = own.filter((row) => filters.every((test) => test(row)));
          own.sort((a, b) => {
            for (const [key, asc] of orders) {
              if (a[key] !== b[key]) return (a[key] > b[key] ? 1 : -1) * (asc ? 1 : -1);
            }
            return 0;
          });
          const rows = own.slice(
            query.start ?? 0,
            Math.min((query.end ?? own.length - 1) + 1, (query.start ?? 0) + cap),
          );
          queries.push({ table, start: query.start ?? 0, orders });
          return Promise.resolve({
            data: query.single ? (rows[0] ?? null) : rows,
            error: null,
          }).then(resolve, reject);
        },
      };
      return query;
    },
  };
}

test("dashboard paginates more than 1000 rows in stable order and freezes the committed snapshot", async () => {
  const state = response().states[info.name];
  state.samples.prospective = sample(1505);
  const rows = Array.from({ length: 1505 }, (_, i) => ({
    track: info.name,
    day_ms: LAST - (1504 - i) * DAY,
    sample_kind: "prospective",
    sample_equity_ppm: 1000000 + i,
    created_at: "2026-10-09T00:07:00Z",
    run_key: "first",
  }));
  const client = pagedClient({
    forward_trend_state: [
      {
        revision: 1,
        committed: true,
        states: { [info.name]: state },
        tracks: [info],
        through_day_ms: LAST,
        created_at: "2026-10-09T00:08:00Z",
      },
    ],
    forward_trend_ledger: [
      ...rows,
      { ...rows[0], created_at: "2026-10-09T00:09:00Z", run_key: "later" },
    ],
    forward_trend_weights: [],
  });
  const data = await loadTrendDashboard(client);
  assert.equal(data.tracks[0].equity.length, 1505);
  assert.equal(data.tracks[0].equity.at(-1).day_ms, LAST);
  assert.equal(data.reconstructedTracks[0].equity.length, 0);
  assert.equal(client.queries.filter((q) => q.table === "forward_trend_ledger").length, 6);
  assert.deepEqual(client.queries.find((q) => q.table === "forward_trend_ledger").orders, [
    ["day_ms", true],
    ["track", true],
  ]);
});

test("resume selects committed revisions, never newer startup diagnostics", async () => {
  const own = pagedClient({
    forward_trend_state: [
      { id: "good", user_id: USER, committed: true, revision: 1 },
      { id: "diagnostic", user_id: USER, committed: false, revision: 2 },
    ],
  });
  assert.equal((await createTrendRepository(own).latestSnapshot(USER)).id, "good");
});

test("default user selection pages all accounts; UUID override remains optional", async () => {
  const pages = [];
  const client = {
    auth: {
      admin: {
        async listUsers({ page, perPage }) {
          pages.push({ page, perPage });
          return { data: { users: page <= 2 ? [{ id: `user-${page}` }] : [] }, error: null };
        },
      },
    },
  };
  assert.deepEqual(await listForwardUsers(client), ["user-1", "user-2"]);
  assert.equal(pages.length, 3);
  assert.deepEqual(await listForwardUsers(client, USER), [USER]);
  assert.equal(pages.length, 3);
  await assert.rejects(() => listForwardUsers(client, "invalid"), /UUID/);
});

test("daily schedule is 00:05 UTC", () => {
  assert.equal(trend.msUntilNextDailyRun(Date.UTC(2026, 9, 9, 0, 0)), 5 * 60000);
  assert.equal(trend.msUntilNextDailyRun(Date.UTC(2026, 9, 9, 0, 5)), DAY);
});
