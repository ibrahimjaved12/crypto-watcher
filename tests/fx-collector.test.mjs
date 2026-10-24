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

test("a missing or incomplete spec is reported once per process and not recorded; the account currency is the configured one", async () => {
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
  // The account currency is FX_ACCOUNT_CURRENCY's, whatever the instrument.
  const eur = world({ accountCurrency: "EUR" });
  await fx.runFxCollectorPass(eur.deps, eur.state);
  assert.ok(eur.calls.specs[0].every((spec) => spec.accountCurrency === "EUR"));
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

test("the gateway's MT5 spec fields map to the spec store's names in one function; a missing field is incomplete, never filled", () => {
  const sub = SUBS[0];
  const built = fx.specFromGateway(sub, goodSpec(), "GBP");
  assert.deepEqual(built.spec, { venue: VENUE, instrumentId: "fx:EURUSD", nativeSymbol: "EURUSD", digits: 5,
    point: 0.00001, tickSize: 0.00001, tickValue: 1, contractSize: 100000, volumeMin: 0.01, volumeMax: 200,
    volumeStep: 0.01, baseCurrency: "EUR", profitCurrency: "USD", marginCurrency: "EUR", accountCurrency: "GBP",
    tradeMode: 4, swapMode: 1, swapLong: -7.3, swapShort: 2.1, swapRollover3days: 3 });
  // Distinct values prove each source field lands in its target.
  const distinct = fx.specFromGateway(sub, goodSpec({ trade_tick_size: 0.5, trade_tick_value: 2, trade_contract_size: 7,
    currency_base: "AAA", currency_profit: "BBB", currency_margin: "CCC" }), "USD").spec;
  assert.deepEqual([distinct.tickSize, distinct.tickValue, distinct.contractSize, distinct.baseCurrency,
    distinct.profitCurrency, distinct.marginCurrency], [0.5, 2, 7, "AAA", "BBB", "CCC"]);
  // Every field is required: absent or null or the wrong type is a problem naming it.
  for (const name of Object.keys(goodSpec())) {
    for (const bad of [null, undefined, "x"]) {
      const raw = goodSpec(); if (bad === undefined) delete raw[name]; else raw[name] = bad;
      if (bad === "x" && ["currency_base", "currency_profit", "currency_margin"].includes(name)) continue;
      const result = fx.specFromGateway(sub, raw, "USD");
      assert.match(result.problem ?? "", new RegExp(name), `${name} = ${String(bad)}`);
      assert.equal(result.spec, undefined);
    }
  }
  assert.match(fx.specFromGateway(sub, null, "USD").problem, /does not know/);
});

test("FX_BACKFILL_DAYS defaults to 0: the first run asks for the last closed minute only", async () => {
  assert.equal(fx.FX_DEFAULT_BACKFILL_DAYS, 0);
  const w = world({ subs: [SUBS[0]] });
  await fx.runFxCollectorPass(w.deps, w.state);
  assert.deepEqual(w.gateway.bars(), [`/v1/bars?symbol=EURUSD&from_ms=${TO - MIN}&to_ms=${TO}`]);
  assert.equal(healthOf(w)["fx:EURUSD"].status, "LIVE");
});

test("503 offset_unknown is a wait: UNAVAILABLE with the code, nothing stored, the same range retried at the next pass", async () => {
  let known = false;
  const gateway = fakeGateway({ "/v1/bars": (url) => known
    ? { status: 200, body: { bars: rowsFor("EURUSD", Number(url.searchParams.get("from_ms")), Number(url.searchParams.get("to_ms"))) } }
    : { status: 503, body: { error: "offset_unknown" } } });
  const w = world({ subs: SUBS, gateway, backfillDays: 5 });
  await fx.runFxCollectorPass(w.deps, w.state);
  assert.equal(w.calls.bars.length, 0);
  assert.deepEqual(w.calls.health.at(-1).map((r) => [r.status, r.errorMessage]), [["UNAVAILABLE", "offset_unknown"], ["UNAVAILABLE", "offset_unknown"]]);
  assert.equal(gateway.bars().length, 2, "each symbol was tried once, none abandoned");
  known = true;
  w.time.now += MIN;
  await fx.runFxCollectorPass(w.deps, w.state);
  assert.equal(healthOf(w)["fx:EURUSD"].status, "LIVE");
  assert.equal(gateway.bars()[2].includes(`from_ms=${TO + MIN - 5 * 86_400_000}`), true, "the retry asks for the same backfill start (shifted by the clock)");
});

// ---------------------------------------------------------------- gap scan (#457)

/** [from, to) in whole minutes. */
const minutes = (from, to) => { const out = []; for (let t = from; t < to; t += MIN) out.push(t); return out; };
const without = (times, gone) => times.filter((t) => !gone.includes(t));
const barsQuery = (from, to, symbol = "EURUSD") => `/v1/bars?symbol=${symbol}&from_ms=${from}&to_ms=${to}`;
const barsReply = (url) => ({ status: 200, body: { bars: rowsFor(url.searchParams.get("symbol"),
  Number(url.searchParams.get("from_ms")), Number(url.searchParams.get("to_ms"))) } });

/** world() plus a store that keeps every stored open time, the gap checks and the gap-scan reads. */
function gapWorld({ subs = [SUBS[0]], stored = {}, gapScan = { scanHours: 24, recheckHours: 24 }, ...rest } = {}) {
  const w = world({ subs, ...rest });
  const series = new Map(Object.entries(stored).map(([id, times]) => [key(VENUE, id), new Set(times)]));
  const checks = [];
  Object.assign(w.calls, { reads: [], checkReads: [] });
  const recordBars = w.deps.store.recordFxBars;
  Object.assign(w.deps.store, {
    async recordFxBars(bars) {
      for (const bar of bars) {
        const k = key(bar.venue, bar.instrumentId);
        if (!series.has(k)) series.set(k, new Set());
        series.get(k).add(bar.openTime);
      }
      return recordBars(bars);
    },
    async getFxBarCoverage() {
      return [...series].filter(([, set]) => set.size).map(([k, set]) => ({ venue: k.split("|")[0],
        instrument_id: k.split("|")[1], bar_count: set.size, oldest_open_time_ms: Math.min(...set),
        newest_open_time_ms: Math.max(...set) }));
    },
    async getFxBars(venue, instrumentId, options) {
      w.calls.reads.push([instrumentId, options]);
      const times = [...(series.get(key(venue, instrumentId)) ?? [])].sort((a, b) => a - b)
        .filter((t) => t >= options.fromMs && t < options.toMs).slice(-options.limit);
      return times.map((t) => ({ ...row(instrumentId.split(":")[1], t), venue, instrument_id: instrumentId, open_time_ms: t }));
    },
    async recordFxGapChecks(rows) { for (const r of rows) checks.push({ ...r, checkedAt: w.time.now }); },
    async getFxGapChecks(venue, instrumentId, sinceMs) {
      w.calls.checkReads.push([instrumentId, sinceMs]);
      return checks.filter((c) => c.venue === venue && c.instrumentId === instrumentId && c.checkedAt >= sinceMs).reverse()
        .slice(0, 500).map((c) => ({ from_ms: c.fromMs, to_ms: c.toMs, checked_at_ms: c.checkedAt,
          requested_minutes: c.requestedMinutes, returned_minutes: c.returnedMinutes }));
    },
  });
  if (gapScan) w.deps.gapScan = gapScan;
  return { ...w, series, checks };
}
const checkOf = (c) => [c.instrumentId, c.fromMs, c.toMs, c.requestedMinutes, c.returnedMinutes];

test("gap scan: a hole behind the newest stored minute is found, refilled with transport history and recorded", async () => {
  const hole = minutes(TO - 2 * HOUR, TO - 2 * HOUR + 5 * MIN);
  const w = gapWorld({ stored: { "fx:EURUSD": without(minutes(TO - 3 * HOUR, TO), hole) } });
  await fx.runFxCollectorPass(w.deps, w.state);
  assert.deepEqual(w.gateway.bars(), [barsQuery(hole[0], hole[0] + 5 * MIN)], "only the hole; nothing after the newest minute");
  assert.deepEqual(flat(w.calls.bars).map((b) => [b.openTime, b.transport, b.receivedAt]), hole.map((t) => [t, "history", NOW]));
  assert.deepEqual(w.checks.map(checkOf), [["fx:EURUSD", hole[0], hole[0] + 5 * MIN, 5, 5]]);
  assert.ok(w.calls.logs.includes("[fx-collector] EURUSD: gap scan: 5 missing, 5 requested, 5 filled"));
  assert.equal(w.state.nextGapScanAt.get("fx:EURUSD"), NOW + HOUR);
  assert.equal(w.state.lastGapMissing.get("fx:EURUSD"), 0);
  assert.deepEqual([healthOf(w)["fx:EURUSD"].status, healthOf(w)["fx:EURUSD"].errorMessage], ["LIVE", null]);
  // Not again before the hour is up.
  w.time.now += 30 * MIN;
  await fx.runFxCollectorPass(w.deps, w.state);
  assert.equal(w.calls.reads.length, 1);
  assert.equal(w.checks.length, 1);
});

test("gap scan: a minute the gateway returns nothing for is asked once, then skipped until the recheck time", async () => {
  const hole = TO - 2 * HOUR;
  const gateway = fakeGateway({ "/v1/bars": (url) => Number(url.searchParams.get("from_ms")) === hole
    ? { status: 200, body: { bars: [] } } : barsReply(url) });
  const w = gapWorld({ gateway, gapScan: { scanHours: 24, recheckHours: 2 },
    stored: { "fx:EURUSD": without(minutes(TO - 3 * HOUR, TO), [hole]) } });
  const asked = () => gateway.bars().filter((p) => p.includes(`from_ms=${hole}&`)).length;
  await fx.runFxCollectorPass(w.deps, w.state);
  assert.equal(asked(), 1);
  assert.deepEqual(w.checks.map(checkOf), [["fx:EURUSD", hole, hole + MIN, 1, 0]], "a confirmed-empty check");
  assert.ok(w.calls.logs.includes("[fx-collector] EURUSD: gap scan: 1 missing, 1 requested, 0 filled"));
  assert.equal(w.state.lastGapMissing.get("fx:EURUSD"), 1);
  // The next scan (an hour later) still sees it missing but inside the check: not asked.
  w.time.now = NOW + HOUR + MIN;
  await fx.runFxCollectorPass(w.deps, w.state);
  assert.equal(w.calls.reads.length, 2, "the scan ran");
  assert.equal(asked(), 1);
  assert.deepEqual(w.calls.checkReads.at(-1), ["fx:EURUSD", w.time.now - 2 * HOUR]);
  // After the recheck window it is asked once more.
  w.time.now = NOW + 2 * HOUR + 2 * MIN;
  await fx.runFxCollectorPass(w.deps, w.state);
  assert.equal(asked(), 2);
  assert.equal(w.checks.length, 2);
});

test("gap scan: closed-market minutes are never requested; runs break at the weekend", async () => {
  const fridayClose = Date.UTC(2026, 9, 16, 21, 0, 0); // 17:00 New York (EDT)
  const sundayOpen = Date.UTC(2026, 9, 18, 21, 0, 0);
  const now = Date.UTC(2026, 9, 19, 0, 0, 30);
  const to = Math.floor(now / MIN) * MIN;
  const stored = [...minutes(fridayClose - 3 * HOUR, fridayClose - MIN), ...minutes(sundayOpen + MIN, to)];
  const w = gapWorld({ stored: { "fx:EURUSD": stored }, gapScan: { scanHours: 72, recheckHours: 24 }, time: { now } });
  await fx.runFxCollectorPass(w.deps, w.state);
  assert.deepEqual(w.gateway.bars(), [barsQuery(fridayClose - MIN, fridayClose), barsQuery(sundayOpen, sundayOpen + MIN)]);
  assert.ok(w.calls.logs.includes("[fx-collector] EURUSD: gap scan: 2 missing, 2 requested, 2 filled"));
  assert.deepEqual(fx.fxOpenMinutes(fridayClose - 2 * MIN, sundayOpen + 2 * MIN),
    [fridayClose - 2 * MIN, fridayClose - MIN, sundayOpen, sundayOpen + MIN]);
});

test("gap scan: a run longer than 5000 minutes is asked in two pages, each recorded", async () => {
  const open = Date.UTC(2026, 9, 11, 21, 0, 0); // Sunday 17:00 New York (EDT)
  const now = Date.UTC(2026, 9, 16, 20, 0, 30); // Friday
  const to = Math.floor(now / MIN) * MIN;
  const resume = Date.UTC(2026, 9, 16, 19, 0, 0);
  const w = gapWorld({ stored: { "fx:EURUSD": [open, ...minutes(resume, to)] }, gapScan: { scanHours: 168, recheckHours: 24 },
    time: { now } });
  await fx.runFxCollectorPass(w.deps, w.state);
  const split = open + MIN + 5000 * MIN;
  assert.deepEqual(w.gateway.bars(), [barsQuery(open + MIN, split), barsQuery(split, resume)]);
  assert.deepEqual(w.checks.map(checkOf), [["fx:EURUSD", open + MIN, split, 5000, 5000], ["fx:EURUSD", split, resume, 2079, 2079]]);
  assert.deepEqual(w.calls.bars.map((batch) => batch.length), [1000, 1000, 1000, 1000, 1000, 1000, 1000, 79]);
  assert.ok(w.calls.logs.includes("[fx-collector] EURUSD: gap scan: 7079 missing, 7079 requested, 7079 filled"));
  assert.deepEqual(w.calls.reads, [["fx:EURUSD", { fromMs: open, toMs: to, limit: 20000 }]], "not before the oldest stored bar");
});

test("gap scan: 503 offset_unknown, 409 offset_changed and an unreachable gateway are waits: nothing recorded, next scan in 10 minutes", async () => {
  const hole = TO - HOUR;
  const answers = {
    offset_unknown: () => ({ status: 503, body: { error: "offset_unknown" } }),
    offset_changed: () => ({ status: 409, body: { error: "offset_changed" } }),
    "connect ECONNREFUSED": () => { throw new Error("connect ECONNREFUSED"); },
  };
  for (const [code, answer] of Object.entries(answers)) {
    const gateway = fakeGateway({ "/v1/bars": (url) => Number(url.searchParams.get("from_ms")) === hole ? answer() : barsReply(url) });
    const w = gapWorld({ gateway, stored: { "fx:EURUSD": without(minutes(TO - 2 * HOUR, TO), [hole]) } });
    await fx.runFxCollectorPass(w.deps, w.state);
    assert.deepEqual([w.checks.length, w.calls.bars.length], [0, 0], code);
    assert.equal(w.state.nextGapScanAt.get("fx:EURUSD"), NOW + 10 * MIN, code);
    assert.equal(w.state.lastGapMissing.has("fx:EURUSD"), false, code);
    assert.ok(w.calls.errors.some((l) => l.startsWith("[fx-collector] EURUSD: gap scan: 1 missing, 0 requested, 0 filled; stopped")
      && l.includes(code)), code);
    assert.equal(healthOf(w)["fx:EURUSD"].status, "LIVE", `${code}: the scan never changes the status`);
    // Retried after 10 minutes, not before.
    const asked = () => gateway.bars().filter((p) => p.includes(`from_ms=${hole}&`)).length;
    w.time.now = NOW + 5 * MIN;
    await fx.runFxCollectorPass(w.deps, w.state);
    assert.equal(asked(), 1, code);
    w.time.now = NOW + 10 * MIN;
    await fx.runFxCollectorPass(w.deps, w.state);
    assert.equal(asked(), 2, code);
  }
});

test("gap scan: one failing symbol does not stop the others", async () => {
  const hole = TO - HOUR;
  const gateway = fakeGateway({ "/v1/bars": (url) => {
    if (url.searchParams.get("symbol") === "EURUSD") throw new Error("socket hang up");
    return barsReply(url);
  } });
  const series = without(minutes(TO - 2 * HOUR, TO), [hole]);
  const w = gapWorld({ subs: SUBS, gateway, stored: { "fx:EURUSD": series, "fx:XAUUSD": series } });
  await fx.runFxCollectorPass(w.deps, w.state);
  assert.deepEqual(w.checks.map(checkOf), [["fx:XAUUSD", hole, hole + MIN, 1, 1]]);
  assert.equal(w.state.nextGapScanAt.get("fx:EURUSD"), NOW + 10 * MIN);
  assert.equal(w.state.nextGapScanAt.get("fx:XAUUSD"), NOW + HOUR);
  assert.deepEqual(w.calls.health.at(-1).map((r) => [r.instrumentId, r.status]), [["fx:EURUSD", "LIVE"], ["fx:XAUUSD", "LIVE"]]);
});

test("gap scan: the scan stays inside its window and caps its requests", async () => {
  const outside = TO - 25 * HOUR;
  const inside = TO - 23 * HOUR;
  const w = gapWorld({ stored: { "fx:EURUSD": without(minutes(TO - 30 * HOUR, TO), [outside, inside]) } });
  await fx.runFxCollectorPass(w.deps, w.state);
  assert.deepEqual(w.gateway.bars(), [barsQuery(inside, inside + MIN)]);
  assert.deepEqual(w.calls.reads, [["fx:EURUSD", { fromMs: TO - MIN - 24 * HOUR, toMs: TO, limit: 20000 }]]);
  // More runs than requests per scan: the first 30 now, the rest 10 minutes later (done runs are skipped by their checks).
  const holes = Array.from({ length: 31 }, (_, i) => TO - 2 * HOUR + 2 * i * MIN);
  const many = gapWorld({ stored: { "fx:EURUSD": without(minutes(TO - 3 * HOUR, TO), holes) } });
  await fx.runFxCollectorPass(many.deps, many.state);
  assert.equal(many.gateway.bars().length, fx.FX_GAP_MAX_REQUESTS_PER_SCAN);
  assert.equal(many.state.nextGapScanAt.get("fx:EURUSD"), NOW + 10 * MIN);
  many.time.now = NOW + 10 * MIN;
  await fx.runFxCollectorPass(many.deps, many.state);
  assert.deepEqual(many.checks.map((c) => c.fromMs), holes);
});

test("no gap scan without gapScan, with FX_GAP_SCAN_HOURS 0, while the market is closed or while backfilling", async () => {
  const series = without(minutes(TO - 2 * HOUR, TO), [TO - HOUR]);
  for (const gapScan of [null, { scanHours: 0, recheckHours: 24 }]) {
    const w = gapWorld({ gapScan, stored: { "fx:EURUSD": series } });
    await fx.runFxCollectorPass(w.deps, w.state);
    assert.deepEqual([w.calls.reads.length, w.calls.checkReads.length, w.gateway.bars().length], [0, 0, 0]);
    assert.equal(healthOf(w)["fx:EURUSD"].errorMessage, null);
  }
  const fridayClose = Date.UTC(2026, 9, 16, 21, 0, 0);
  const closed = gapWorld({ stored: { "fx:EURUSD": without(minutes(fridayClose - HOUR, fridayClose), [fridayClose - 30 * MIN]) },
    time: { now: Date.UTC(2026, 9, 17, 12, 0, 30) } });
  await fx.runFxCollectorPass(closed.deps, closed.state);
  assert.deepEqual([closed.calls.reads.length, closed.gateway.bars().length], [0, 0]);
  const old = TO - 30 * 86_400_000;
  const backfilling = gapWorld({ stored: { "fx:EURUSD": [old - 2 * MIN, old] } });
  await fx.runFxCollectorPass(backfilling.deps, backfilling.state);
  assert.equal(healthOf(backfilling)["fx:EURUSD"].status, "RECOVERING");
  assert.equal(backfilling.calls.reads.length, 0);
  assert.ok(backfilling.gateway.bars().every((p) => !p.includes(`from_ms=${old - MIN}&`)));
});

test("the missing count is in the health message only when the status is not LIVE", async () => {
  // In a pass: the gateway has nothing new, so the newest bar ages to RECOVERING and the quiet minute is reported.
  const hole = TO - HOUR;
  const quiet = fakeGateway({ "/v1/bars": () => ({ status: 200, body: { bars: [] } }) });
  const w = gapWorld({ gateway: quiet, stored: { "fx:EURUSD": without(minutes(TO - 2 * HOUR, TO - 4 * MIN), [hole]) } });
  await fx.runFxCollectorPass(w.deps, w.state);
  assert.deepEqual([healthOf(w)["fx:EURUSD"].status, healthOf(w)["fx:EURUSD"].errorMessage],
    ["RECOVERING", "1 open minute(s) missing in the last 24 h"]);
  // Directly: appended to an existing message, alone for a null one, never on LIVE, nothing for 0.
  const sub = SUBS[0];
  const ok = { problem: null, backfilling: false };
  const message = (...args) => fx.fxHealth(sub, ...args).errorMessage;
  assert.equal(message(NOW, TO - MIN, ok, null, 7), null);
  assert.equal(fx.fxHealth(sub, NOW, TO - MIN, ok, null, 7).status, "LIVE");
  assert.equal(message(NOW, TO - 5 * MIN, ok, null, 7), "7 open minute(s) missing in the last 24 h");
  assert.equal(message(NOW, null, ok, null, 7), "no bars stored; 7 open minute(s) missing in the last 24 h");
  assert.equal(message(NOW, TO - MIN, { problem: { status: "UNAVAILABLE", message: "offset_unknown" }, backfilling: false }, null, 2),
    "offset_unknown; 2 open minute(s) missing in the last 24 h");
  assert.equal(message(NOW, TO - 5 * MIN, ok, null, 0), null);
});

test("fxHealth without the missing count is unchanged", () => {
  const sub = SUBS[0];
  const ok = { problem: null, backfilling: false };
  const base = { venue: VENUE, instrumentId: "fx:EURUSD" };
  const saturday = Date.UTC(2026, 9, 17, 12, 0, 30);
  const cases = [
    [[NOW, TO - MIN, ok, null], { status: "LIVE", lastBarOpenTime: TO - MIN, lagMs: 30_000, errorMessage: null }],
    [[NOW, TO - 5 * MIN, ok, null], { status: "RECOVERING", lastBarOpenTime: TO - 5 * MIN, lagMs: 4 * MIN + 30_000, errorMessage: null }],
    [[NOW, TO - 12 * MIN, ok, null], { status: "STALE", lastBarOpenTime: TO - 12 * MIN, lagMs: 11 * MIN + 30_000, errorMessage: null }],
    [[NOW, null, ok, null], { status: "STALE", lastBarOpenTime: null, lagMs: null, errorMessage: "no bars stored" }],
    [[NOW, TO - MIN, { problem: null, backfilling: true }, null], { status: "RECOVERING", lastBarOpenTime: TO - MIN, lagMs: 30_000, errorMessage: null }],
    [[NOW, TO - MIN, null, { status: "LOGIN_FAILED", message: "terminal not connected" }],
      { status: "LOGIN_FAILED", lastBarOpenTime: TO - MIN, lagMs: 30_000, errorMessage: "terminal not connected" }],
    [[saturday, TO - MIN, ok, null], { status: "MARKET_CLOSED", lastBarOpenTime: TO - MIN, lagMs: saturday - TO, errorMessage: null }],
  ];
  for (const [args, expected] of cases) {
    assert.deepEqual(fx.fxHealth(sub, ...args), { ...base, ...expected });
    assert.deepEqual(fx.fxHealth(sub, ...args, undefined), { ...base, ...expected });
  }
});
