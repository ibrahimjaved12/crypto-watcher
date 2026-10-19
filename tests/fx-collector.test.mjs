import assert from "node:assert/strict";
import { test } from "node:test";
import { createModuleLoader } from "./module-loader.mjs";

// #453: the forex collector with a fake gateway and a fake store (no network, no database, no terminal). Every
// bar, spec and clock value here is SYNTHETIC.
const moduleUrl = createModuleLoader(import.meta.url);
const fx = await import(await moduleUrl("../src/lib/fx/fx-collector.server.ts"));

const MIN = 60_000;
const HOUR = 3_600_000;
const VENUE = "test-venue";
const NOW = Date.UTC(2026, 9, 14, 12, 0, 30); // a Wednesday, the market is open
const TO = Math.floor(NOW / MIN) * MIN; // 12:00:00, the minute in progress is not closed
const SUBS = [
  { venue: VENUE, instrumentId: "fx:EURUSD", nativeSymbol: "EURUSD" },
  { venue: VENUE, instrumentId: "fx:XAUUSD", nativeSymbol: "XAUUSD" },
];
const key = (venue, id) => `${venue}|${id}`;

function row(symbol, openMs, overrides = {}) {
  return { time_basis: "utc", native_symbol: symbol, open_time_ms: openMs, open: 1.08, high: 1.0802, low: 1.0799,
    close: 1.0801, spread_points: 12, tick_volume: 7, ...overrides };
}
function rowsFor(symbol, from, to) {
  const out = [];
  for (let t = from; t < to && out.length < 5000; t += MIN) out.push(row(symbol, t));
  return out;
}
function goodSpec(overrides = {}) {
  return { digits: 5, point: 0.00001, trade_tick_size: 0.00001, trade_tick_value: 1, trade_contract_size: 100000,
    volume_min: 0.01, volume_max: 200, volume_step: 0.01, currency_base: "EUR", currency_profit: "USD",
    currency_margin: "EUR", trade_mode: 4, swap_mode: 1, swap_long: -7.3, swap_short: 2.1, swap_rollover3days: 3,
    ...overrides };
}

/** A fake gateway: `handlers` override a path's answer; every requested path is recorded. */
function fakeGateway(handlers = {}) {
  const calls = [];
  const send = async (path) => {
    calls.push(path);
    const url = new URL(path, "http://gateway");
    if (handlers[url.pathname]) return handlers[url.pathname](url, calls.filter((p) => p.startsWith(url.pathname)).length);
    if (url.pathname === "/health") return { status: 200, body: { ok: true, terminal_connected: true, offset_known: true, now_utc_ms: NOW } };
    if (url.pathname === "/v1/bars") {
      const [from, to] = [Number(url.searchParams.get("from_ms")), Number(url.searchParams.get("to_ms"))];
      return { status: 200, body: { bars: rowsFor(url.searchParams.get("symbol"), from, to) } };
    }
    if (url.pathname === "/v1/specs") {
      const names = url.searchParams.get("symbols").split(",");
      return { status: 200, body: { specs: Object.fromEntries(names.map((n) => [n, n === "EURUSD" || n === "XAUUSD" ? goodSpec() : null])) } };
    }
    return { status: 404, body: { error: "not_found" } };
  };
  return { send, calls, bars: () => calls.filter((p) => p.startsWith("/v1/bars")) };
}

function world({ subs = SUBS, coverage = {}, gateway = fakeGateway(), leaseOk = true, accountCurrency = "USD",
                 backfillDays, time = { now: NOW } } = {}) {
  const calls = { claim: [], renew: [], release: [], bars: [], health: [], specs: [], logs: [], errors: [] };
  const newest = new Map(Object.entries(coverage));
  const lease = { held: false, ok: leaseOk, renewOk: true };
  const store = {
    async getFxSubscriptions() { return subs; },
    async getFxBarCoverage() {
      return [...newest].map(([k, ms]) => ({ venue: k.split("|")[0], instrument_id: k.split("|")[1], bar_count: 1,
        oldest_open_time_ms: ms, newest_open_time_ms: ms }));
    },
    async recordFxBars(bars) {
      calls.bars.push(bars);
      for (const bar of bars) {
        const k = key(bar.venue, bar.instrumentId);
        newest.set(k, Math.max(newest.get(k) ?? 0, bar.openTime));
      }
      return bars.map((bar) => `${bar.venue}|${bar.instrumentId}|${bar.openTime}`);
    },
    async recordFxCollectorHealth(rows) { calls.health.push(rows); },
    async recordFxSymbolSpecs(rows) { calls.specs.push(rows); return rows.length; },
    async claimFxCollectorLease(id, seconds) { calls.claim.push([id, seconds]); lease.held = lease.ok; return lease.ok; },
    async renewFxCollectorLease(id, seconds) { calls.renew.push([id, seconds]); return lease.renewOk && lease.held; },
    async releaseFxCollectorLease(id) { calls.release.push(id); },
  };
  const deps = { store, gateway: gateway.send, now: () => time.now, instanceId: "00000000-0000-4000-8000-000000000001",
    accountCurrency, ...(backfillDays ? { backfillDays } : {}), log: (l) => calls.logs.push(l), error: (l) => calls.errors.push(l) };
  return { deps, calls, state: fx.newFxCollectorState(), gateway, lease, newest, time };
}
const flat = (batches) => batches.flat();
const healthOf = (w, n = -1) => Object.fromEntries(w.calls.health.at(n).map((r) => [r.instrumentId, r]));

test("the first run backfills in pages of 5000 minutes with transport history, in write batches of 1000", async () => {
  const w = world({ subs: [SUBS[0]], backfillDays: 5 });
  const result = await fx.runFxCollectorPass(w.deps, w.state);
  const from = TO - 5 * 86_400_000;
  assert.deepEqual(w.gateway.bars(), [
    `/v1/bars?symbol=EURUSD&from_ms=${from}&to_ms=${from + 5000 * MIN}`,
    `/v1/bars?symbol=EURUSD&from_ms=${from + 5000 * MIN}&to_ms=${TO}`,
  ]);
  const bars = flat(w.calls.bars);
  assert.equal(bars.length, 7200);
  assert.deepEqual(w.calls.bars.map((batch) => batch.length), [1000, 1000, 1000, 1000, 1000, 1000, 1000, 200]);
  assert.ok(bars.every((bar) => bar.transport === "history" && bar.receivedAt === NOW && bar.venue === VENUE));
  assert.deepEqual(bars[0], { venue: VENUE, instrumentId: "fx:EURUSD", symbol: "EURUSD", nativeSymbol: "EURUSD",
    openTime: from, open: 1.08, high: 1.0802, low: 1.0799, close: 1.0801, spreadPoints: 12, tickVolume: 7,
    transport: "history", receivedAt: NOW });
  assert.equal(bars.at(-1).openTime, TO - MIN, "up to the last closed minute");
  assert.equal(result.stored["EURUSD"], 7200);
  // Caught up in one pass: LIVE, not RECOVERING.
  assert.equal(healthOf(w)["fx:EURUSD"].status, "LIVE");
});

test("steady state polls from the newest stored minute + 1 and uses transport poll", async () => {
  const w = world({ subs: [SUBS[0]], coverage: { [key(VENUE, "fx:EURUSD")]: TO - 3 * MIN } });
  await fx.runFxCollectorPass(w.deps, w.state);
  assert.deepEqual(w.gateway.bars(), [`/v1/bars?symbol=EURUSD&from_ms=${TO - 2 * MIN}&to_ms=${TO}`]);
  assert.deepEqual(flat(w.calls.bars).map((b) => [b.openTime, b.transport]), [[TO - 2 * MIN, "poll"], [TO - MIN, "poll"]]);
  const health = healthOf(w)["fx:EURUSD"];
  assert.deepEqual([health.status, health.lastBarOpenTime, health.lagMs, health.errorMessage], ["LIVE", TO - MIN, 30_000, null]);
  // Up to date: nothing is requested.
  const again = world({ subs: [SUBS[0]], coverage: { [key(VENUE, "fx:EURUSD")]: TO - MIN } });
  await fx.runFxCollectorPass(again.deps, again.state);
  assert.deepEqual(again.gateway.bars(), []);
  assert.equal(healthOf(again)["fx:EURUSD"].status, "LIVE");
  // A span longer than 10 minutes is history.
  const long = world({ subs: [SUBS[0]], coverage: { [key(VENUE, "fx:EURUSD")]: TO - 30 * MIN } });
  await fx.runFxCollectorPass(long.deps, long.state);
  assert.ok(flat(long.calls.bars).every((b) => b.transport === "history"));
});

test("a closed market is MARKET_CLOSED, asks the gateway for nothing and reports no error", async () => {
  const saturday = Date.UTC(2026, 9, 17, 12, 0, 30);
  const fridayClose = Date.UTC(2026, 9, 16, 21, 0, 0);
  const w = world({ coverage: { [key(VENUE, "fx:EURUSD")]: fridayClose - MIN, [key(VENUE, "fx:XAUUSD")]: fridayClose - MIN },
    time: { now: saturday } });
  await fx.runFxCollectorPass(w.deps, w.state);
  assert.deepEqual(w.gateway.bars(), []);
  const health = healthOf(w);
  for (const id of ["fx:EURUSD", "fx:XAUUSD"]) {
    assert.deepEqual([health[id].status, health[id].errorMessage, health[id].lastBarOpenTime],
      ["MARKET_CLOSED", null, fridayClose - MIN]);
  }
  assert.deepEqual(w.calls.errors, []);
  // The week opens: the same collector asks for the minutes since the last stored one.
  w.time.now = Date.UTC(2026, 9, 18, 21, 5, 30); // Sunday, 5 minutes after the open
  await fx.runFxCollectorPass(w.deps, w.state);
  assert.equal(w.gateway.bars().length, 2);
});

test("gateway 503 offset_unknown and 409 offset_changed give UNAVAILABLE with the code; one symbol does not stop the other", async () => {
  const gateway = fakeGateway({ "/v1/bars": (url) => {
    const symbol = url.searchParams.get("symbol");
    if (symbol === "EURUSD") return { status: 503, body: { error: "offset_unknown" } };
    return { status: 200, body: { bars: rowsFor(symbol, Number(url.searchParams.get("from_ms")), Number(url.searchParams.get("to_ms"))) } };
  } });
  const w = world({ gateway, coverage: { [key(VENUE, "fx:EURUSD")]: TO - 3 * MIN, [key(VENUE, "fx:XAUUSD")]: TO - 3 * MIN } });
  await fx.runFxCollectorPass(w.deps, w.state);
  const health = healthOf(w);
  assert.deepEqual([health["fx:EURUSD"].status, health["fx:EURUSD"].errorMessage], ["UNAVAILABLE", "offset_unknown"]);
  assert.equal(health["fx:XAUUSD"].status, "LIVE");
  assert.deepEqual(flat(w.calls.bars).map((b) => b.symbol), ["XAUUSD", "XAUUSD"]);
  assert.ok(w.calls.errors.some((line) => line.startsWith("[fx-collector]") && line.includes("offset_unknown")));
  const changed = world({ gateway: fakeGateway({ "/v1/bars": () => ({ status: 409, body: { error: "offset_changed" } }) }),
    coverage: { [key(VENUE, "fx:EURUSD")]: TO - 3 * MIN, [key(VENUE, "fx:XAUUSD")]: TO - 3 * MIN } });
  await fx.runFxCollectorPass(changed.deps, changed.state);
  assert.deepEqual(changed.calls.health.at(-1).map((r) => [r.status, r.errorMessage]),
    [["UNAVAILABLE", "offset_changed"], ["UNAVAILABLE", "offset_changed"]]);
  assert.equal(changed.calls.bars.length, 0);
});

test("a symbol whose request throws is UNAVAILABLE and the others are still collected; an unreachable gateway is UNAVAILABLE for all", async () => {
  const gateway = fakeGateway({ "/v1/bars": (url) => {
    if (url.searchParams.get("symbol") === "EURUSD") throw new Error("socket hang up");
    return { status: 200, body: { bars: rowsFor("XAUUSD", Number(url.searchParams.get("from_ms")), Number(url.searchParams.get("to_ms"))) } };
  } });
  const w = world({ gateway, coverage: { [key(VENUE, "fx:EURUSD")]: TO - 2 * MIN, [key(VENUE, "fx:XAUUSD")]: TO - 2 * MIN } });
  const result = await fx.runFxCollectorPass(w.deps, w.state);
  assert.equal(healthOf(w)["fx:EURUSD"].status, "UNAVAILABLE");
  assert.match(healthOf(w)["fx:EURUSD"].errorMessage, /collect failed: socket hang up/);
  assert.equal(healthOf(w)["fx:XAUUSD"].status, "LIVE");
  assert.equal(result.stored["XAUUSD"], 1);
  const down = world({ gateway: fakeGateway({ "/health": () => { throw new Error("connect ECONNREFUSED"); } }) });
  await fx.runFxCollectorPass(down.deps, down.state);
  assert.ok(down.calls.health.at(-1).every((r) => r.status === "UNAVAILABLE" && /gateway_unreachable: connect ECONNREFUSED/.test(r.errorMessage)));
  assert.deepEqual(down.gateway.bars(), []);
});

test("terminal_connected false is LOGIN_FAILED for every instrument, with nothing requested", async () => {
  const gateway = fakeGateway({ "/health": () => ({ status: 200, body: { ok: true, terminal_connected: false, offset_known: false, now_utc_ms: NOW } }) });
  const w = world({ gateway });
  await fx.runFxCollectorPass(w.deps, w.state);
  assert.deepEqual(w.calls.health.at(-1).map((r) => [r.instrumentId, r.status, r.errorMessage]),
    [["fx:EURUSD", "LOGIN_FAILED", "terminal not connected"], ["fx:XAUUSD", "LOGIN_FAILED", "terminal not connected"]]);
  assert.deepEqual(w.gateway.calls, ["/health"]);
  assert.equal(w.calls.specs.length, 0);
});

test("rows that fail the local checks are skipped and counted, never stored", async () => {
  const gateway = fakeGateway({ "/v1/bars": (url) => ({ status: 200, body: { bars: [
    row("EURUSD", TO - 4 * MIN),
    row("EURUSD", TO - 3 * MIN, { spread_points: 1.5 }),
    row("EURUSD", TO - 2 * MIN + 1_000),
    row("EURUSD", TO - 2 * MIN, { tick_volume: -1 }),
    row("EURUSD", TO - 2 * MIN, { high: 1.0 }),
    row("EURUSD", TO - 2 * MIN, { time_basis: "server" }),
    row("EURUSD", TO + MIN),
    row("OTHER", TO - 2 * MIN),
    "not a row",
    row("EURUSD", TO - MIN),
  ] } }) });
  const w = world({ subs: [SUBS[0]], gateway, coverage: { [key(VENUE, "fx:EURUSD")]: TO - 5 * MIN } });
  const result = await fx.runFxCollectorPass(w.deps, w.state);
  assert.deepEqual(flat(w.calls.bars).map((b) => b.openTime), [TO - 4 * MIN, TO - MIN]);
  assert.deepEqual([result.stored["EURUSD"], result.skipped["EURUSD"]], [2, 8]);
  assert.ok(w.calls.errors.some((line) => line.includes("[fx-collector]") && line.includes("skipped 8 row(s)")));
});

test("health thresholds: LIVE up to 3 minutes, RECOVERING up to 10, STALE beyond, STALE with no bars", async () => {
  const quiet = fakeGateway({ "/v1/bars": () => ({ status: 200, body: { bars: [] } }) });
  const cases = [[2 * MIN, "LIVE"], [5 * MIN, "RECOVERING"], [12 * MIN, "STALE"]];
  for (const [age, expected] of cases) {
    const w = world({ subs: [SUBS[0]], gateway: quiet, coverage: { [key(VENUE, "fx:EURUSD")]: TO - age } });
    await fx.runFxCollectorPass(w.deps, w.state);
    assert.equal(healthOf(w)["fx:EURUSD"].status, expected, `newest bar ${age / MIN} min old`);
  }
  const none = world({ subs: [SUBS[0]], gateway: quiet, backfillDays: 1 });
  await fx.runFxCollectorPass(none.deps, none.state);
  assert.deepEqual([healthOf(none)["fx:EURUSD"].status, healthOf(none)["fx:EURUSD"].errorMessage, healthOf(none)["fx:EURUSD"].lastBarOpenTime],
    ["STALE", "no bars stored", null]);
  // A backfill with pages left is RECOVERING and resumes from the newest stored minute at the next pass.
  const big = world({ subs: [SUBS[0]], backfillDays: 30 });
  await fx.runFxCollectorPass(big.deps, big.state);
  assert.equal(big.gateway.bars().length, 4);
  assert.equal(healthOf(big)["fx:EURUSD"].status, "RECOVERING");
  const after = big.newest.get(key(VENUE, "fx:EURUSD"));
  await fx.runFxCollectorPass(big.deps, big.state);
  assert.ok(big.gateway.bars()[4].includes(`from_ms=${after + MIN}`));
});

test("the lease is claimed, renewed each pass and released on stop; without it nothing happens", async () => {
  const w = world({ subs: [SUBS[0]], coverage: { [key(VENUE, "fx:EURUSD")]: TO - MIN } });
  await fx.runFxCollectorPass(w.deps, w.state);
  assert.deepEqual(w.calls.claim, [[w.deps.instanceId, 90]]);
  assert.equal(w.calls.renew.length, 0);
  w.time.now += MIN;
  await fx.runFxCollectorPass(w.deps, w.state);
  assert.deepEqual(w.calls.renew, [[w.deps.instanceId, 90]]);
  assert.equal(w.calls.claim.length, 1);
  // The lease is lost: renew fails, a new claim succeeds, work continues.
  w.lease.renewOk = false;
  w.time.now += MIN;
  assert.equal((await fx.runFxCollectorPass(w.deps, w.state)).leased, true);
  assert.equal(w.calls.claim.length, 2);
  // Release on shutdown.
  const collector = fx.startFxCollector(w.deps, { startDelayMs: 10_000_000 });
  await collector.stop();
  assert.deepEqual(w.calls.release, [w.deps.instanceId]);
  // Another collector holds it: no gateway call, no store write, retried at the next pass.
  const other = world({ leaseOk: false });
  const result = await fx.runFxCollectorPass(other.deps, other.state);
  assert.equal(result.leased, false);
  assert.deepEqual(other.gateway.calls, []);
  assert.deepEqual([other.calls.bars.length, other.calls.health.length, other.calls.specs.length], [0, 0, 0]);
  await fx.runFxCollectorPass(other.deps, other.state);
  assert.equal(other.calls.claim.length, 2, "it keeps trying");
  assert.ok(other.calls.logs.some((line) => line.includes("[fx-collector] lease not held")));
});

test("specs are recorded at start and again after 6 hours, mapped to the spec store's fields", async () => {
  const w = world({ subs: [SUBS[0]], coverage: { [key(VENUE, "fx:EURUSD")]: TO - MIN } });
  await fx.runFxCollectorPass(w.deps, w.state);
  assert.deepEqual(w.gateway.calls.filter((p) => p.startsWith("/v1/specs")), ["/v1/specs?symbols=EURUSD"]);
  assert.equal(w.calls.specs.length, 1);
  assert.deepEqual(w.calls.specs[0], [{ venue: VENUE, instrumentId: "fx:EURUSD", nativeSymbol: "EURUSD", digits: 5,
    point: 0.00001, tickSize: 0.00001, tickValue: 1, contractSize: 100000, volumeMin: 0.01, volumeMax: 200,
    volumeStep: 0.01, baseCurrency: "EUR", profitCurrency: "USD", marginCurrency: "EUR", accountCurrency: "USD",
    tradeMode: 4, swapMode: 1, swapLong: -7.3, swapShort: 2.1, swapRollover3days: 3 }]);
  w.time.now += 5 * HOUR;
  await fx.runFxCollectorPass(w.deps, w.state);
  assert.equal(w.calls.specs.length, 1, "not yet");
  w.time.now += HOUR + MIN;
  await fx.runFxCollectorPass(w.deps, w.state);
  assert.equal(w.calls.specs.length, 2, "again after 6 hours");
});

test("a missing or incomplete spec is reported once per process and not recorded; a missing account currency records none", async () => {
  const gateway = fakeGateway({ "/v1/specs": () => ({ status: 200, body: { specs: {
    EURUSD: goodSpec(), XAUUSD: goodSpec({ volume_step: null }), } } }) });
  const w = world({ gateway });
  await fx.runFxCollectorPass(w.deps, w.state);
  assert.deepEqual(w.calls.specs[0].map((s) => s.instrumentId), ["fx:EURUSD"]);
  assert.equal(w.calls.errors.filter((l) => l.includes("XAUUSD: no usable symbol spec")).length, 1);
  w.state.nextSpecsAt = 0;
  await fx.runFxCollectorPass(w.deps, w.state);
  assert.equal(w.calls.errors.filter((l) => l.includes("XAUUSD: no usable symbol spec")).length, 1, "once per process");
  const unknown = world({ gateway: fakeGateway({ "/v1/specs": () => ({ status: 200, body: { specs: { EURUSD: goodSpec(), XAUUSD: null } } }) }) });
  await fx.runFxCollectorPass(unknown.deps, unknown.state);
  assert.ok(unknown.calls.errors.some((l) => l.includes("the terminal does not know the symbol")));
  const bad = world({ gateway: fakeGateway({ "/v1/specs": () => ({ status: 200, body: { specs: { EURUSD: goodSpec({ point: 0.0001 }), XAUUSD: goodSpec() } } }) }) });
  await fx.runFxCollectorPass(bad.deps, bad.state);
  assert.deepEqual(bad.calls.specs[0].map((s) => s.instrumentId), ["fx:XAUUSD"], "a point that is not 10**-digits is refused");
  const noCurrency = world({ accountCurrency: undefined });
  await fx.runFxCollectorPass(noCurrency.deps, noCurrency.state);
  assert.equal(noCurrency.calls.specs.length, 0);
  assert.equal(noCurrency.calls.errors.filter((l) => l.includes("FX_ACCOUNT_CURRENCY")).length, 1);
  // A failing spec request is retried at the next pass and does not stop the bars.
  const failing = world({ gateway: fakeGateway({ "/v1/specs": () => ({ status: 502, body: { error: "mt5_error" } }) }) });
  await fx.runFxCollectorPass(failing.deps, failing.state);
  assert.equal(failing.state.nextSpecsAt, 0);
  assert.ok(failing.gateway.bars().length > 0);
});

test("a pass never throws, whatever the store does", async () => {
  const w = world();
  w.deps.store.getFxSubscriptions = async () => { throw new Error("db down"); };
  const result = await fx.runFxCollectorPass(w.deps, w.state);
  assert.equal(result.leased, true, "the lease was held when the store failed");
  assert.ok(w.calls.errors.some((l) => l.includes("[fx-collector] pass failed: db down")));
  const noWrite = world({ subs: [SUBS[0]], coverage: { [key(VENUE, "fx:EURUSD")]: TO - 2 * MIN } });
  noWrite.deps.store.recordFxBars = async () => { throw new Error("disk full"); };
  noWrite.deps.store.recordFxCollectorHealth = async () => { throw new Error("health write"); };
  await fx.runFxCollectorPass(noWrite.deps, noWrite.state);
  assert.ok(noWrite.calls.errors.some((l) => l.includes("health write failed: health write")));
});

test("FX_SUBSCRIPTIONS parsing", () => {
  assert.deepEqual(fx.parseFxSubscriptions("fx:EURUSD=EURUSD, fx:XAUUSD=XAUUSDm"), [
    { venue: "exness-mt5-demo", instrumentId: "fx:EURUSD", nativeSymbol: "EURUSD" },
    { venue: "exness-mt5-demo", instrumentId: "fx:XAUUSD", nativeSymbol: "XAUUSDm" }]);
  assert.deepEqual(fx.parseFxSubscriptions("fx:GBPUSD", "other-venue"), [{ venue: "other-venue", instrumentId: "fx:GBPUSD", nativeSymbol: "GBPUSD" }]);
  for (const bad of [undefined, "", "EURUSD=EURUSD", "fx:eurusd=EURUSD", "fx:EURUSD=bad name", "fx:EURUSD,fx:EURUSD", "fx:E=E"])
    assert.throws(() => fx.parseFxSubscriptions(bad), Error, String(bad));
  assert.throws(() => fx.parseFxSubscriptions("fx:EURUSD", "Bad_Venue"), /FX_VENUE/);
});

test("the gateway client sends the bearer token, never follows redirects and checks its configuration", async () => {
  const token = "t".repeat(40);
  const seen = [];
  const send = async (url, init) => { seen.push([url, init]); return { status: 200, json: async () => ({ ok: true }) }; };
  const get = fx.createGatewayClient({ url: "http://10.0.0.5:8002", token }, send);
  assert.deepEqual(await get("/v1/bars?symbol=EURUSD&from_ms=1&to_ms=2"), { status: 200, body: { ok: true } });
  assert.equal(seen[0][0], "http://10.0.0.5:8002/v1/bars?symbol=EURUSD&from_ms=1&to_ms=2");
  assert.equal(seen[0][1].headers.Authorization, `Bearer ${token}`);
  assert.equal(seen[0][1].redirect, "manual");
  assert.equal(seen[0][1].method, "GET");
  // A non-JSON error body is a null body, not a throw.
  const text = fx.createGatewayClient({ url: "http://10.0.0.5:8002", token }, async () => ({ status: 502, json: async () => { throw new Error("no json"); } }));
  assert.deepEqual(await text("/x"), { status: 502, body: null });
  for (const url of ["ftp://x", "http://u:p@x:1", "http://x:1/path", "http://x:1/?a=1", "not a url"])
    assert.throws(() => fx.createGatewayClient({ url, token }, send), Error, url);
  assert.throws(() => fx.createGatewayClient({ url: "http://x:1", token: "short" }, send), /FX_GATEWAY_TOKEN/);
});
