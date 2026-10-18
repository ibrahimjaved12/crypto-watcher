import assert from "node:assert/strict";
import { test } from "node:test";
import { createModuleLoader } from "./module-loader.mjs";

// #401 part 2b: the calendar fetcher's pass and loop with fake deps and a fake clock. No network, no
// database, no Python. Every event here is SYNTHETIC.
const moduleUrl = createModuleLoader(import.meta.url);
const fetcher = await import(await moduleUrl("../src/lib/fx/calendar-fetcher.server.ts"));
const { runPass, runCalendarLoop, newSourceState, THIS_WEEK, NEXT_WEEK, CalendarParseRejected } = fetcher;

const MIN = 60_000;
const T0 = Date.UTC(2026, 9, 19, 9, 0);
const COVERED_TO = Date.UTC(2026, 9, 25, 4, 0);
const FEED = JSON.stringify([{ title: "CPI y/y", country: "GBP", date: "2026-10-21T02:00:00-04:00", impact: "High",
  forecast: "3.1%", previous: "3.0%" }]);
const PARSED = {
  rows: [{ source: "forexfactory", source_event_key: "GBP|CPI y/y|2026-10-18|0", event_ts: "2026-10-21T06:00:00Z",
    currency: "GBP", impact: "high", title: "CPI y/y", detail: { forecast: "3.1%", previous: "3.0%" } }],
  coveredToMs: COVERED_TO, skippedHolidays: 2, skippedAllLow: 1,
};
const ok = (body = FEED) => ({ status: 200, retryAfterSeconds: null, body });
// Every fetch-log row and log line from every test, checked last for URLs.
const allFetchRows = [];
const allLogs = [];

function fakeDeps({ respond = () => ok(), parse = async () => PARSED, failStore = () => false, stopAfter = Infinity } = {}) {
  const state = { now: T0, fetches: [], writes: [], logs: [], sleeps: [], controller: new AbortController() };
  state.deps = {
    async fetchFeed(url) {
      state.fetches.push({ url, at: state.now });
      const response = respond(url, state.fetches.length);
      if (response instanceof Error) throw response;
      return response;
    },
    parse,
    store: {
      async recordFxCalendar(rows) {
        if (failStore("rows")) throw new Error("operational database down");
        state.writes.push(["rows", rows]);
        return rows.length;
      },
      async recordFxCalendarFetch(row) {
        if (failStore("fetch", row)) throw new Error("operational database down");
        state.writes.push(["fetch", row]);
        allFetchRows.push(row);
      },
    },
    now: () => state.now,
    async sleep(ms) {
      state.sleeps.push(ms);
      state.now += ms;
      if (state.fetches.length >= stopAfter) state.controller.abort();
    },
    log(line) {
      state.logs.push(line);
      allLogs.push(line);
    },
    random: () => 0,
  };
  return state;
}
const fetchRows = (state) => state.writes.filter(([kind]) => kind === "fetch").map(([, row]) => row);

test("a 200 writes the rows, then an ok fetch row with the parser's covered_to", async () => {
  const state = fakeDeps();
  let parsedPayload;
  state.deps.parse = async (payload) => { parsedPayload = payload; return PARSED; };
  const result = await runPass(state.deps, THIS_WEEK);
  assert.deepEqual(result, { outcome: "ok", nextDelayMs: fetcher.CYCLE_MS });
  assert.deepEqual(parsedPayload, JSON.parse(FEED));
  assert.deepEqual(state.writes, [
    ["rows", [{ source: "forexfactory", sourceEventKey: "GBP|CPI y/y|2026-10-18|0",
      eventTs: Date.UTC(2026, 9, 21, 6, 0), currency: "GBP", impact: "high", title: "CPI y/y",
      detail: { forecast: "3.1%", previous: "3.0%" } }]],
    ["fetch", { source: "forexfactory", status: "ok", eventCount: 1, coveredTo: COVERED_TO }],
  ]);
  assert.deepEqual(state.logs, ["[fx-calendar] this-week: 1 events (2 holidays, 1 all-low skipped)"]);
  assert.equal(state.fetches[0].url, fetcher.THIS_WEEK_URL);
});

test("a 429 honours Retry-After (capped at 6 h) and writes rate_limited", async () => {
  const state = fakeDeps({ respond: () => ({ status: 429, retryAfterSeconds: 120, body: "" }) });
  assert.deepEqual(await runPass(state.deps, THIS_WEEK), { outcome: "rate_limited", nextDelayMs: 120_000 });
  assert.deepEqual(fetchRows(state), [{ source: "forexfactory", status: "error", eventCount: 0, error: "rate_limited" }]);
  const huge = fakeDeps({ respond: () => ({ status: 429, retryAfterSeconds: 86_400, body: "" }) });
  assert.equal((await runPass(huge.deps, THIS_WEEK)).nextDelayMs, 6 * 60 * MIN);
});

test("the loop waits the Retry-After before fetching that source again", async () => {
  const state = fakeDeps({
    respond: (url, n) => (n === 1 ? { status: 429, retryAfterSeconds: 120, body: "" } : ok()),
    stopAfter: 4,
  });
  await runCalendarLoop(state.deps, {}, state.controller.signal);
  assert.deepEqual(state.fetches.map(({ url, at }) => [url === fetcher.THIS_WEEK_URL ? "this" : "next", (at - T0) / 1_000]), [
    ["this", 5], // first pass 5 s after start-up: rate limited for 120 s
    ["next", 35], // next week still runs 30 s after the first slot
    ["this", 125], // exactly Retry-After later
    ["next", 155], // and next week again 30 s after the ok this-week write, so it stays the latest ok
  ]);
});

test("a 429 without Retry-After backs off 30 min, then 60 min", async () => {
  const state = fakeDeps({ respond: () => ({ status: 429, retryAfterSeconds: null, body: "" }) });
  const sourceState = newSourceState();
  assert.equal((await runPass(state.deps, THIS_WEEK, sourceState)).nextDelayMs, 30 * MIN);
  assert.equal((await runPass(state.deps, THIS_WEEK, sourceState)).nextDelayMs, 60 * MIN);
});

test("a 404 on next week writes nothing and logs once; on this week it is an error", async () => {
  const state = fakeDeps({ respond: () => ({ status: 404, retryAfterSeconds: null, body: "" }) });
  const sourceState = newSourceState();
  for (let i = 0; i < 3; i++) {
    assert.deepEqual(await runPass(state.deps, NEXT_WEEK, sourceState), { outcome: "not_published", nextDelayMs: fetcher.CYCLE_MS });
  }
  assert.deepEqual(state.writes, []);
  assert.deepEqual(state.logs, ["[fx-calendar] next-week: not published yet"]);
  assert.equal((await runPass(state.deps, THIS_WEEK)).outcome, "error");
  assert.equal(fetchRows(state)[0].error, "http_404");
});

test("bad JSON, a non-array and an oversized body are errors with fixed reasons", async () => {
  for (const [body, reason] of [
    ["{not json", "bad_json"],
    ['{"events": []}', "bad_json"],
    [`[${" ".repeat(fetcher.MAX_BODY_BYTES)}]`, "too_large"],
  ]) {
    const state = fakeDeps({ respond: () => ok(body) });
    let parsed = false;
    state.deps.parse = async () => { parsed = true; return PARSED; };
    assert.deepEqual(await runPass(state.deps, THIS_WEEK), { outcome: "error", nextDelayMs: 5 * MIN });
    assert.equal(parsed, false, reason);
    assert.deepEqual(fetchRows(state), [{ source: "forexfactory", status: "error", eventCount: 0, error: reason }]);
    assert.equal(state.writes.some(([kind]) => kind === "rows"), false);
  }
});

test("a parser rejection is parse_rejected; an unreachable parser is parser_unavailable", async () => {
  const rejected = fakeDeps({ parse: async () => { throw new CalendarParseRejected("rejected"); } });
  await runPass(rejected.deps, THIS_WEEK);
  assert.equal(fetchRows(rejected)[0].error, "parse_rejected");
  const down = fakeDeps({ parse: async () => { throw new Error("connect ECONNREFUSED"); } });
  await runPass(down.deps, THIS_WEEK);
  assert.equal(fetchRows(down)[0].error, "parser_unavailable");
  const network = fakeDeps({ respond: () => new TypeError("fetch failed") });
  await runPass(network.deps, THIS_WEEK);
  assert.equal(fetchRows(network)[0].error, "network");
  const status = fakeDeps({ respond: () => ({ status: 503, retryAfterSeconds: null, body: "" }) });
  await runPass(status.deps, THIS_WEEK);
  assert.equal(fetchRows(status)[0].error, "http_503");
});

test("errors back off 5 min doubling to 1 h and reset after a success", async () => {
  let failing = true;
  const state = fakeDeps({ respond: () => (failing ? { status: 500, retryAfterSeconds: null, body: "" } : ok()) });
  const sourceState = newSourceState();
  const delays = [];
  for (let i = 0; i < 6; i++) delays.push((await runPass(state.deps, THIS_WEEK, sourceState)).nextDelayMs / MIN);
  assert.deepEqual(delays, [5, 10, 20, 40, 60, 60]);
  failing = false;
  assert.equal((await runPass(state.deps, THIS_WEEK, sourceState)).outcome, "ok");
  failing = true;
  assert.equal((await runPass(state.deps, THIS_WEEK, sourceState)).nextDelayMs, 5 * MIN);
});

test("a store error is an error pass and never ends the loop", async () => {
  let storeDown = true;
  const state = fakeDeps({ failStore: () => storeDown, stopAfter: 5 });
  // The store recovers after the first two passes.
  const fetchFeed = state.deps.fetchFeed;
  state.deps.fetchFeed = async (url, signal) => {
    if (state.fetches.length >= 2) storeDown = false;
    return fetchFeed(url, signal);
  };
  await runCalendarLoop(state.deps, {}, state.controller.signal);
  assert.equal(state.fetches.length, 5);
  assert.ok(fetchRows(state).some((row) => row.status === "ok"));
  assert.ok(state.logs.some((line) => line.includes("store_unavailable")));
  // Direct: a throwing store makes runPass report an error instead of throwing.
  const direct = fakeDeps({ failStore: () => true });
  assert.equal((await runPass(direct.deps, THIS_WEEK)).outcome, "error");
});

test("this week is always fetched before next week, every cycle", async () => {
  const state = fakeDeps({ stopAfter: 6 });
  await runCalendarLoop(state.deps, {}, state.controller.signal);
  const order = state.fetches.map(({ url }) => (url === fetcher.THIS_WEEK_URL ? "this" : "next"));
  assert.deepEqual(order, ["this", "next", "this", "next", "this", "next"]);
  const okRows = fetchRows(state).filter((row) => row.status === "ok");
  assert.equal(okRows.length, 6);
  // Each cycle: this week, 30 s, next week; cycles are 2 h apart (jitter 0 here).
  const at = state.fetches.map(({ at: time }) => time - T0);
  assert.deepEqual(at.slice(0, 2), [5_000, 35_000]);
  assert.equal(at[2] - at[0], fetcher.CYCLE_MS);
  assert.equal(at[3] - at[2], 30_000);
});

test("a failing this-week retry is followed by next week again, so next week stays the latest ok", async () => {
  const state = fakeDeps({
    respond: (url, n) => (n === 1 ? { status: 500, retryAfterSeconds: null, body: "" } : ok()),
    stopAfter: 4,
  });
  await runCalendarLoop(state.deps, {}, state.controller.signal);
  const order = state.fetches.map(({ url, at }) => [url === fetcher.THIS_WEEK_URL ? "this" : "next", (at - T0) / 1_000]);
  assert.deepEqual(order, [["this", 5], ["next", 35], ["this", 305], ["next", 335]]);
  const okSources = state.fetches.slice(1).map(({ url }) => url);
  assert.equal(okSources.at(-1), fetcher.NEXT_WEEK_URL);
});

test("no error row or log line ever contains a feed URL", () => {
  assert.ok(allFetchRows.some((row) => row.status === "error"));
  for (const row of allFetchRows) {
    const text = JSON.stringify(row);
    assert.doesNotMatch(text, /https?:|faireconomy|ff_calendar/, text);
  }
  for (const line of allLogs) assert.doesNotMatch(line, /https?:|faireconomy|ff_calendar/, line);
});
