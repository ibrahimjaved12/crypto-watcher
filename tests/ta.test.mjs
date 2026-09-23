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
const { closedCandles, analyze, patterns, outcomeDue, TA_VERSION } = await import(
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
const futuresMetadata = (status = "TRADING") => ({
  symbol: "BTCUSDT",
  pair: "BTCUSDT",
  contractType: "PERPETUAL",
  status,
  baseAsset: "BTC",
  quoteAsset: "USDT",
  marginAsset: "USDT",
  onboardDate: 1569398400000,
  deliveryDate: 4133404800000,
  filters: [
    { filterType: "PRICE_FILTER", tickSize: "0.10" },
    { filterType: "LOT_SIZE", stepSize: "0.001", minQty: "0.001" },
    { filterType: "MIN_NOTIONAL", notional: "100" },
  ],
});

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

test("v2 EMA200, MACD, bands, ADX and range agree with independently known series", () => {
  assert.equal(TA_VERSION, "ta-v2");
  const rising = series().map((c, i) => ({
    ...c,
    open: i + 100,
    close: i + 101,
    high: i + 102,
    low: i + 99,
  }));
  const values = analyze(rising);
  const near = (actual, expected) =>
    assert.ok(Math.abs(actual - expected) < 1e-8, `${actual} != ${expected}`);
  near(values.ema200, 349 - 99.5);
  near(values.macd.line, 7);
  near(values.macd.signal, 7);
  near(values.macd.histogram, 0);
  near(values.bollinger.middle, 339.5);
  near(values.bollinger.upper, 339.5 + 2 * Math.sqrt(33.25));
  near(values.bollinger.lower, 339.5 - 2 * Math.sqrt(33.25));
  near(values.adx14, 100);
  assert.deepEqual(values.range20, { low: 327, high: 349 });
  assert.equal(values.volume_ratio, 1);
  assert.equal(values.volume_average20, 100);
  const altered = rising.map((c) => ({ ...c }));
  altered.at(-1).high = 10000;
  altered.at(-1).volume = 400;
  assert.deepEqual(analyze(altered).range20, values.range20);
  assert.equal(analyze(altered).volume_average20, 100);
  assert.equal(analyze(altered).volume_ratio, 4);
});

test("v2 flat prices have finite persisted values and forming candles cannot affect indicators", () => {
  const flat = series().map((c) => ({ ...c, high: 100, low: 100, volume: 0 }));
  const values = analyze(flat);
  assert.equal(values.bollinger.bandwidth_pct, 0);
  assert.equal(values.bollinger.percent_b, null);
  assert.equal(values.volume_ratio, null);
  assert.deepEqual(JSON.parse(JSON.stringify(values)), values);
  const withForming = [
    ...flat,
    candle({ time: end, high: 10000, close: 9999, volume: 99999, complete: false }),
  ];
  assert.deepEqual(analyze(closedCandles(withForming, 15, now)), values);
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

test("TA persistence deduplicates futures snapshots and enforces user read isolation", async () => {
  const db = new PGlite();
  try {
    await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
      CREATE SCHEMA auth; CREATE TABLE auth.users(id uuid PRIMARY KEY);
      CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql AS 'SELECT current_setting(''request.jwt.claim.sub'')::uuid';
      GRANT USAGE ON SCHEMA public,auth TO authenticated,service_role;
      INSERT INTO auth.users VALUES ('00000000-0000-0000-0000-000000000001'),('00000000-0000-0000-0000-000000000002');`);
    for (const name of [
      "20260913170352_3d6925ba-68c4-4528-aea1-da9439eb127e.sql",
      "20260915090000_cumulative_monitor.sql",
      "20260917090000_technical_analysis.sql",
      "20260921090000_activity_domains.sql",
      "20260923090000_binance_usdm_futures.sql",
    ]) {
      await db.exec(
        await readFile(new URL(`../supabase/migrations/${name}`, import.meta.url), "utf8"),
      );
    }
    await db.exec(`INSERT INTO watchlist_items(user_id,symbol,instrument_id) VALUES
      ('00000000-0000-0000-0000-000000000001','BTCUSDT','binance-usdm:BTCUSDT')`);
    const insert = `INSERT INTO ta_signals(user_id,symbol,instrument_id,timeframe,candle_at,source,version,price,indicators,patterns)
      VALUES ('00000000-0000-0000-0000-000000000001','BTCUSDT','binance-usdm:BTCUSDT',15,now(),'binance-usdm','ta-v2',100,'{}','{doji}')`;
    await db.exec("BEGIN");
    await db.exec(insert);
    await db.exec(insert + " ON CONFLICT(user_id,symbol,timeframe,candle_at,version) DO NOTHING");
    const { rows } = await db.query("SELECT * FROM ta_signals");
    assert.equal(rows.length, 1);
    assert.equal(rows[0].source, "binance-usdm");
    assert.equal(rows[0].instrument_id, "binance-usdm:BTCUSDT");
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

test("futures provider preserves OHLCV and contract identity", async () => {
  const providerUrl = await moduleUrl("../src/lib/market/providers.server.ts", {
    "./symbols": await moduleUrl("../src/lib/market/symbols.ts"),
  });
  const { clearExchangeInfoCache, loadFuturesSnapshot, loadTACandles } = await import(providerUrl);
  const original = globalThis.fetch;
  try {
    const calls = [];
    globalThis.fetch = async (url) => {
      calls.push(url);
      if (url.endsWith("/fapi/v1/exchangeInfo"))
        return Response.json({ symbols: [futuresMetadata()] });
      if (url.includes("/fapi/v2/ticker/price"))
        return Response.json({ symbol: "BTCUSDT", price: "101", time: end });
      if (url.includes("/fapi/v1/premiumIndex"))
        return Response.json({ symbol: "BTCUSDT", markPrice: "100.5", indexPrice: "100.4" });
      if (url.includes("/fapi/v1/fundingRate"))
        return Response.json([{ symbol: "BTCUSDT", fundingRate: "0.0001", fundingTime: end }]);
      return Response.json([[end - duration, "100", "102", "98", "101", "35", end - 1]]);
    };
    const result = await loadTACandles("BTCUSDT", 15, () => {});
    assert.deepEqual(
      result.candles[0],
      candle({ close: 101, volume: 35, complete: end < Date.now() }),
    );
    assert.equal(result.source, "binance-usdm");
    assert.equal(result.instrument.id, "binance-usdm:BTCUSDT");
    assert.equal(result.endpoint, "/fapi/v1/klines");
    assert.equal(result.priceType, "trade");
    assert.equal(result.instrument.priceTick, "0.10");
    assert.equal(result.instrument.quantityStep, "0.001");
    assert.equal(result.instrument.minNotional, "100");
    assert.equal(calls.length, 2);
    const snapshot = await loadFuturesSnapshot("BTCUSDT");
    assert.deepEqual(
      [snapshot.lastPrice, snapshot.markPrice, snapshot.indexPrice, snapshot.fundingRate],
      [101, 100.5, 100.4, 0.0001],
    );
    await assert.rejects(
      loadTACandles("BTCUSDT", 15, () => {}, "OKX"),
      /Unknown data source/,
    );
    clearExchangeInfoCache();
    globalThis.fetch = async (url) => {
      if (url.endsWith("/fapi/v1/exchangeInfo"))
        return Response.json({ symbols: [futuresMetadata("BREAK")] });
      assert.fail("inactive contract must fail before requesting candles");
    };
    await assert.rejects(
      loadTACandles("BTCUSDT", 15, () => {}, "binance-usdm"),
      /inactive or incompatible/,
    );
  } finally {
    globalThis.fetch = original;
  }
});

test("provider HTTP failures cancel unused bodies and preserve errors if cleanup fails", async () => {
  const { clearExchangeInfoCache, loadTACandles } = await import(
    await moduleUrl("../src/lib/market/providers.server.ts", {
      "./symbols": await moduleUrl("../src/lib/market/symbols.ts"),
    })
  );
  const original = globalThis.fetch;
  try {
    for (const cleanupFails of [false, true]) {
      clearExchangeInfoCache();
      let canceled = 0;
      globalThis.fetch = async () =>
        new Response(
          new ReadableStream({
            cancel() {
              canceled++;
              if (cleanupFails) throw new Error("cleanup failed");
            },
          }),
          { status: 503 },
        );
      await assert.rejects(
        loadTACandles("BTCUSDT", 15, () => {}, "binance-usdm"),
        {
          message: "binance-usdm: HTTP 503",
        },
      );
      assert.equal(canceled, 1);
    }
    globalThis.fetch = async () => new Response(null, { status: 503 });
    await assert.rejects(
      loadTACandles("BTCUSDT", 15, () => {}, "binance-usdm"),
      {
        message: "binance-usdm: HTTP 503",
      },
    );
  } finally {
    globalThis.fetch = original;
  }
});

test("TA runner isolates frame failures and settles on the recorded futures source", async () => {
  const core = await moduleUrl("../src/lib/ta/core.ts", {
    technicalindicators: import.meta.resolve("technicalindicators"),
  });
  const provider = `data:text/javascript,export const loadTACandles = (...args) => globalThis.__taLoad(...args);`;
  const { runTA } = await import(
    await moduleUrl("../src/lib/ta/engine.server.ts", {
      "../activity-controls": await moduleUrl("../src/lib/activity-controls.ts"),
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
        close: 101,
      }),
    );
    validate(candles);
    return {
      source: "binance-usdm",
      instrument: { id: "binance-usdm:BTCUSDT" },
      endpoint: "/fapi/v1/klines",
      priceType: "trade",
      candles,
    };
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
                  source: "binance-usdm",
                  detected_at: new Date(boundary - 10 * step).toISOString(),
                  price: 100,
                },
                {
                  id: `expired-${filters.timeframe}`,
                  source: "binance-usdm",
                  detected_at: new Date(boundary - 500 * step).toISOString(),
                  price: 100,
                },
                {
                  id: `new-${filters.timeframe}`,
                  source: "binance-usdm",
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
    assert.equal(calls.filter((c) => c.source === "binance-usdm").length, 0);
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
