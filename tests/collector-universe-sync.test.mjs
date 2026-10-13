import assert from "node:assert/strict";
import { test } from "node:test";
import { createModuleLoader } from "./module-loader.mjs";

// In-memory sync and timer fakes only: no hosted/local DB, network or real waits.
const moduleUrl = createModuleLoader(import.meta.url);
const loop = await import(await moduleUrl("../src/lib/market/collector-universe-sync.ts"));

function clock(t) {
  const pending = new Set();
  const scheduled = [];
  t.mock.method(globalThis, "setTimeout", (callback, delay) => {
    const timer = { callback, delay, unref: t.mock.fn(), hasRef: () => true };
    pending.add(timer);
    scheduled.push(timer);
    return timer;
  });
  const clear = t.mock.method(globalThis, "clearTimeout", (timer) => pending.delete(timer));
  return {
    pending,
    scheduled,
    clear,
    async tick() {
      assert.equal(pending.size, 1);
      const timer = [...pending][0];
      pending.delete(timer);
      await timer.callback();
    },
  };
}

test("sync starts immediately, reports assignments/unchanged/skipped and repeats at the configured interval", async (t) => {
  const timers = clock(t);
  const lines = [];
  const results = [
    { status: "assigned", symbols: ["ADAUSDT", "BTCUSDT"] },
    { status: "unchanged", symbols: ["ADAUSDT", "BTCUSDT"] },
    { status: "skipped", reason: "Collector mode disabled" },
  ];
  const sync = t.mock.fn(async () => results.shift());
  let now = Date.parse("2026-10-13T00:00:00Z");
  const running = loop.startCollectorUniverseSync({
    sync,
    now: () => now,
    log: (line) => lines.push(line),
    env: { COLLECTOR_UNIVERSE_SYNC_INTERVAL_MS: "45000" },
  });
  try {
    assert.equal(timers.scheduled[0].delay, 0);
    await timers.tick();
    assert.match(
      lines[0],
      /heartbeat status=assigned symbols=2 newest_assignment_at=2026-10-13T00:00:00.000Z$/,
    );
    now += 45_000;
    await timers.tick();
    assert.match(
      lines[1],
      /status=unchanged symbols=2 newest_assignment_at=2026-10-13T00:00:00.000Z$/,
    );
    await timers.tick();
    assert.match(lines[2], /status=skipped symbols=2 .*reason=Collector mode disabled$/);
    assert.equal(sync.mock.callCount(), 3);
    assert.deepEqual(
      timers.scheduled.map((timer) => timer.delay),
      [0, 45_000, 45_000, 45_000],
    );
  } finally {
    running.stop();
  }
});

test("hosted and operational failures are caught and the next tick still assigns", async (t) => {
  const timers = clock(t);
  const lines = [];
  let calls = 0;
  const running = loop.startCollectorUniverseSync({
    env: {},
    log: (line) => lines.push(line),
    sync: async () => {
      calls += 1;
      if (calls === 1) throw new Error("Hosted read failed\noffline");
      if (calls === 2) throw new Error("Operational DB starting");
      return { status: "assigned", symbols: ["BTCUSDT"] };
    },
  });
  try {
    await timers.tick();
    assert.match(
      lines[0],
      /status=skipped symbols=unknown newest_assignment_at=never error=Hosted read failed offline retry=next_interval$/,
    );
    await timers.tick();
    assert.match(lines[1], /Operational DB starting retry=next_interval$/);
    await timers.tick();
    assert.equal(calls, 3);
    assert.match(lines[2], /status=assigned symbols=1/);
    assert.equal([...timers.pending][0].delay, 60_000);
  } finally {
    running.stop();
  }
});

test("a failed pass retains the last known symbol count and assignment time", async (t) => {
  const timers = clock(t);
  const lines = [];
  let calls = 0;
  const running = loop.startCollectorUniverseSync({
    env: {},
    now: () => 0,
    log: (line) => lines.push(line),
    sync: async () => {
      if (calls++ === 0) return { status: "assigned", symbols: ["BTCUSDT"] };
      throw new Error("read failed");
    },
  });
  try {
    await timers.tick();
    await timers.tick();
    assert.match(
      lines[1],
      /status=skipped symbols=1 newest_assignment_at=1970-01-01T00:00:00.000Z error=read failed/,
    );
  } finally {
    running.stop();
  }
});

test("every pending timer stays referenced and stop clears it idempotently", async (t) => {
  const timers = clock(t);
  const sync = t.mock.fn(async () => ({ status: "unchanged", symbols: [] }));
  const running = loop.startCollectorUniverseSync({ sync, env: {}, log: () => {} });
  await timers.tick();
  for (const timer of timers.scheduled) {
    assert.equal(timer.unref.mock.callCount(), 0);
    assert.equal(timer.hasRef(), true);
  }
  const pending = [...timers.pending][0];
  running.stop();
  running.stop();
  assert.equal(timers.clear.mock.callCount(), 1);
  assert.equal(timers.clear.mock.calls[0].arguments[0], pending);
  assert.equal(timers.pending.size, 0);
  await pending.callback(); // Even an already-dispatched callback cannot run after stop.
  assert.equal(sync.mock.callCount(), 1);
  assert.equal(timers.pending.size, 0);
});

test("a pending sync cannot overlap another tick or reschedule after stop", async (t) => {
  const timers = clock(t);
  let resolve;
  const running = loop.startCollectorUniverseSync({
    env: {},
    log: () => {},
    sync: () =>
      new Promise((done) => {
        resolve = done;
      }),
  });
  const tick = timers.tick();
  assert.equal(timers.pending.size, 0, "no next timer until the current sync finishes");
  running.stop();
  resolve({ status: "unchanged", symbols: [] });
  await tick;
  assert.equal(timers.pending.size, 0);
});

test("interval parsing defaults invalid values, clamps the minimum and respects Node's timer range", () => {
  const parse = (raw) =>
    loop.collectorUniverseSyncInterval({ COLLECTOR_UNIVERSE_SYNC_INTERVAL_MS: raw });
  for (const raw of [
    undefined,
    "",
    " ",
    "nope",
    "0",
    "-1",
    "1.5",
    "Infinity",
    "NaN",
    "1e5",
    "2147483648",
    "9007199254740992",
  ])
    assert.equal(parse(raw), 60_000, String(raw));
  assert.equal(parse("1"), 10_000);
  assert.equal(parse("9999"), 10_000);
  assert.equal(parse("10000"), 10_000);
  assert.equal(parse(" 120000 "), 120_000);
  assert.equal(parse("2147483647"), 2_147_483_647);
});

test("start eligibility requires collector mode, hosted/operational config and an enabled child", () => {
  const env = {
    BINANCE_COLLECTOR_ENABLED: "true",
    OPERATIONAL_DB_ENABLED: "true",
    SUPABASE_URL: "configured",
    SUPABASE_SERVICE_ROLE_KEY: "configured",
    OPERATIONAL_SUPABASE_URL: "configured",
    OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY: "configured",
  };
  assert.equal(loop.collectorUniverseSyncConfigured(env), true);
  assert.equal(
    loop.collectorUniverseSyncConfigured({ ...env, COLLECTOR_UNIVERSE_SYNC_ENABLED: "false" }),
    false,
  );
  for (const name of Object.keys(env)) {
    assert.equal(loop.collectorUniverseSyncConfigured({ ...env, [name]: undefined }), false, name);
    assert.equal(loop.collectorUniverseSyncConfigured({ ...env, [name]: " " }), false, name);
  }
  assert.equal(
    loop.collectorUniverseSyncConfigured({ ...env, BINANCE_COLLECTOR_ENABLED: "TRUE" }),
    false,
  );
  assert.equal(
    loop.collectorUniverseSyncConfigured({ ...env, OPERATIONAL_DB_ENABLED: "false" }),
    false,
  );
});
