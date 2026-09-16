import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";
import { PGlite } from "@electric-sql/pglite";

async function moduleUrl(path, imports = {}) {
  const source = await readFile(new URL(path, import.meta.url), "utf8");
  let { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  });
  for (const [specifier, target] of Object.entries(imports))
    outputText = outputText.replaceAll(`"${specifier}"`, JSON.stringify(target));
  return `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`;
}
const { closedCandles, analyze, patterns, outcomeDue } = await import(
  await moduleUrl("../src/lib/ta/core.ts", {
    technicalindicators: import.meta.resolve("technicalindicators"),
  })
);
const duration = 15 * 60_000;
const now = 1800000000000;
const end = Math.floor(now / duration) * duration;
const candle = (patch = {}) => ({
  time: end - duration,
  open: 100,
  high: 102,
  low: 98,
  close: 100,
  volume: 100,
  complete: true,
  ...patch,
});
const series = () =>
  Array.from({ length: 249 }, (_, i) => candle({ time: end - (249 - i) * duration }));

test("completed candles reject bad prices, gaps, duplicates, insufficient history and stale data", () => {
  const bars = series();
  assert.equal(
    closedCandles([...bars, candle({ time: end, high: 99999, complete: false })], 15, now).length,
    249,
  );
  assert.equal(closedCandles([...bars].reverse(), 15, now).length, 249);
  assert.throws(() => closedCandles(bars.slice(60), 15, now), /200/);
  assert.throws(() => closedCandles([...bars, bars.at(-1)], 15, now), /duplicate/);
  assert.throws(
    () =>
      closedCandles(
        bars.filter((_, i) => i !== 100),
        15,
        now,
      ),
    /gap/,
  );
  assert.throws(() => closedCandles(bars, 15, now + 2 * duration), /Stale/);
  for (const patch of [
    { volume: -1 },
    { high: 99 },
    { low: 101 },
    { open: NaN },
    { close: Infinity },
  ]) {
    assert.throws(() => closedCandles([...bars.slice(0, -1), candle(patch)], 15, now), /Invalid/);
  }
});

test("indicator values on constant and rising prices and zero volume", () => {
  const values = analyze(series());
  assert.equal(values.ema20, 100);
  assert.equal(values.ema50, 100);
  assert.equal(values.atr14, 4);
  assert.equal(values.volume_change_pct, 0);
  const rising = series().map((c, i) => ({
    ...c,
    open: i + 100,
    close: i + 101,
    high: i + 102,
    low: i + 99,
  }));
  assert.equal(analyze(rising).rsi14, 100);
  assert.ok(Math.abs(analyze(rising).ema20 - (349 - 9.5)) < 1e-8);
  assert.equal(analyze(series().map((c) => ({ ...c, volume: 0 }))).volume_change_pct, null);
  const spike = series();
  spike.at(-1).volume = 250;
  assert.equal(analyze(spike).volume_change_pct, 150);
  assert.ok(analyze(spike).patterns.includes("volume_spike"));
});

test("numeric candle rules, trend context and zero-range bars", () => {
  assert.deepEqual(patterns(candle({ high: 100, low: 100 }), candle(), 0), []);
  assert.ok(patterns(candle(), candle(), 0).includes("doji"));
  const hammer = candle({ open: 100, close: 101, low: 97, high: 101.3 });
  assert.ok(patterns(hammer, candle(), -1).includes("hammer"));
  assert.ok(!patterns(hammer, candle(), 1).includes("hammer"));
  assert.ok(
    patterns(candle({ open: 100, close: 101, low: 99.7, high: 104 }), candle(), 1).includes(
      "shooting_star",
    ),
  );
  assert.ok(
    patterns(candle({ open: 98, close: 102 }), candle({ open: 101, close: 99 }), 0).includes(
      "bullish_engulfing",
    ),
  );
  assert.ok(
    patterns(candle({ open: 102, close: 98 }), candle({ open: 99, close: 101 }), 0).includes(
      "bearish_engulfing",
    ),
  );
});

test("outcome is four complete intervals beyond detection, never a pre-detection target", () => {
  assert.equal(outcomeDue(new Date(end).toISOString(), 15), end + 4 * duration);
  assert.equal(outcomeDue(new Date(end + 1).toISOString(), 15), end + 5 * duration);
});

test("TA persistence deduplicates snapshots, retains first source and enforces user read isolation", async () => {
  const db = new PGlite();
  try {
    await db.exec(`CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
      CREATE SCHEMA auth; CREATE TABLE auth.users(id uuid PRIMARY KEY);
      CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql AS 'SELECT current_setting(''request.jwt.claim.sub'')::uuid';
      GRANT USAGE ON SCHEMA public,auth TO authenticated,service_role;
      INSERT INTO auth.users VALUES ('00000000-0000-0000-0000-000000000001'),('00000000-0000-0000-0000-000000000002');`);
    await db.exec(
      await readFile(
        new URL("../supabase/migrations/20260917090000_technical_analysis.sql", import.meta.url),
        "utf8",
      ),
    );
    const insert = `INSERT INTO ta_signals(user_id,symbol,timeframe,candle_at,source,version,price,indicators,patterns)
      VALUES ('00000000-0000-0000-0000-000000000001','BTCUSDT',15,now(),'Binance','ta-v1',100,'{}','{doji}')`;
    await db.exec("BEGIN");
    await db.exec(insert);
    await db.exec(
      insert.replace("'Binance'", "'OKX'") +
        " ON CONFLICT(user_id,symbol,timeframe,candle_at,version) DO NOTHING",
    );
    const { rows } = await db.query("SELECT * FROM ta_signals");
    assert.equal(rows.length, 1);
    assert.equal(rows[0].source, "Binance");
    await db.exec(
      "COMMIT; SET ROLE authenticated; SET request.jwt.claim.sub = '00000000-0000-0000-0000-000000000002'",
    );
    assert.equal((await db.query("SELECT * FROM ta_signals")).rows.length, 0);
    await db.exec("SET request.jwt.claim.sub = '00000000-0000-0000-0000-000000000001'");
    assert.equal((await db.query("SELECT * FROM ta_signals")).rows.length, 1);
    await assert.rejects(db.exec(insert), /permission denied/);
    await assert.rejects(db.exec("UPDATE ta_signals SET price=1"), /permission denied/);
  } finally {
    await db.close();
  }
});

test("provider mapping preserves OHLCV and rejects bad provider before fallback", async () => {
  const { loadTACandles } = await import(await moduleUrl("../src/lib/market/providers.server.ts"));
  const original = globalThis.fetch;
  try {
    const calls = [];
    globalThis.fetch = async (url) => {
      calls.push(url);
      if (url.includes("binance"))
        return Response.json([[end - duration, "100", "102", "98", "101", "35", end - 1]]);
      return Response.json({
        code: "0",
        data: [[String(end - duration), "100", "102", "98", "101", "35", "0", "0", "1"]],
      });
    };
    const result = await loadTACandles("BTCUSDT", 15, () => {});
    assert.deepEqual(
      result.candles[0],
      candle({ close: 101, volume: 35, complete: end < Date.now() }),
    );
    assert.equal(calls.length, 1);
    let validations = 0;
    const fallback = await loadTACandles("BTCUSDT", 15, () => {
      if (++validations === 1) throw new Error("bad feed");
    });
    assert.equal(fallback.source, "OKX");
    assert.equal(fallback.candles[0].volume, 35);
    assert.equal(fallback.candles[0].complete, true);
  } finally {
    globalThis.fetch = original;
  }
});

test("TA runner isolates frame failures and settles on the recorded exchange only", async () => {
  const core = await moduleUrl("../src/lib/ta/core.ts", {
    technicalindicators: import.meta.resolve("technicalindicators"),
  });
  const provider = `data:text/javascript,export const loadTACandles = (...args) => globalThis.__taLoad(...args);`;
  const { runTA } = await import(
    await moduleUrl("../src/lib/ta/engine.server.ts", {
      "./core": core,
      "../market/providers.server": provider,
    })
  );
  const calls = [],
    writes = [],
    updates = [];
  globalThis.__taLoad = async (symbol, frame, validate, source) => {
    calls.push({ frame, source });
    if (frame === 60) throw new Error("feed down");
    const step = frame * 60000;
    const boundary = Math.floor(Date.now() / step) * step;
    const candles = Array.from({ length: 249 }, (_, i) =>
      candle({
        time: boundary - (249 - i) * step,
        close: source === "Binance" ? 101 : 100,
      }),
    );
    validate(candles);
    return { source: source || "OKX", candles };
  };
  const db = {
    from(table) {
      assert.equal(table, "ta_signals");
      const filters = {};
      let mode, patch;
      const q = {
        upsert(row, options) {
          writes.push({ row, options });
          return Promise.resolve({ error: null });
        },
        select() {
          mode = "read";
          return q;
        },
        update(value) {
          mode = "update";
          patch = value;
          return q;
        },
        eq(key, value) {
          filters[key] = value;
          return q;
        },
        order() {
          return q;
        },
        limit() {
          return q;
        },
        then(resolve) {
          if (mode === "update") {
            updates.push({ filters, patch });
            return Promise.resolve(resolve({ error: null }));
          }
          const step = filters.timeframe * 60000;
          const boundary = Math.floor(Date.now() / step) * step;
          return Promise.resolve(
            resolve({
              error: null,
              data: [
                {
                  id: `due-${filters.timeframe}`,
                  source: "Binance",
                  detected_at: new Date(boundary - 10 * step).toISOString(),
                  price: 100,
                },
                {
                  id: `expired-${filters.timeframe}`,
                  source: "Binance",
                  detected_at: new Date(boundary - 500 * step).toISOString(),
                  price: 100,
                },
                {
                  id: `new-${filters.timeframe}`,
                  source: "OKX",
                  detected_at: new Date().toISOString(),
                  price: 100,
                },
              ],
            }),
          );
        },
      };
      return q;
    },
  };
  try {
    const errors = await runTA(db, "owner", "BTCUSDT");
    assert.equal(errors.length, 1);
    assert.match(errors[0], /60m.*feed down/);
    assert.equal(writes.length, 2);
    assert.ok(writes.every((w) => w.options.ignoreDuplicates && w.row.user_id === "owner"));
    assert.equal(calls.filter((c) => c.source === "Binance").length, 2);
    assert.equal(updates.length, 4);
    assert.ok(
      updates.every((u) => u.filters.user_id === "owner" && u.filters.outcome_status === "pending"),
    );
    assert.equal(updates.filter((u) => u.patch.outcome_status === "unavailable").length, 2);
    for (const u of updates.filter((u) => u.patch.outcome_status === "measured")) {
      assert.equal(u.patch.outcome_price, 101);
      assert.ok(Math.abs(u.patch.return_pct - 1) < 1e-9);
    }
  } finally {
    delete globalThis.__taLoad;
  }
});
