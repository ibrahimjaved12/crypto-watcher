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
const { completedCandles, outcomeDue, TA_VERSION } = await import(
  await moduleUrl("../src/lib/ta/schedule.ts")
);
function completedCollectorSeries(symbol, timeframe, candles) {
  return {
    contractVersion: "completed-candle-v1",
    identity: { provider: "binance-usdm", exchange: "binance", marketType: "futures",
      contractType: "perpetual", instrumentId: `binance-usdm:${symbol}`, symbol,
      nativeSymbol: symbol, priceType: "trade", seriesBasis: "native-kline", timeframeMinutes: timeframe },
    observations: candles.map((c) => ({
      candle: { openTime: c.time, closeTime: c.closeTime, open: c.open, high: c.high,
        low: c.low, close: c.close, baseVolume: c.volume, quoteVolume: 123.25 },
      provenance: c.transport === "websocket"
        ? { sourceKind: "websocket", endpoint: c.endpoint, sourceEventTime: c.sourceEventTime, receivedAt: c.receivedAt }
        : { sourceKind: "rest", endpoint: c.endpoint, retrievedAt: c.receivedAt },
    })),
  };
}
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
const pythonResults = async (requests) =>
  requests.map((request) => {
    const selected = request.candles.find(
      (value) => value.open_ms === request.target_candle_open_time_ms,
    );
    const candleCount =
      request.candles.findIndex((value) => value.open_ms === request.target_candle_open_time_ms) +
      1;
    const factors = Object.fromEntries(
      ["trend", "momentum", "patterns", "volume"].map((name) => [
        name,
        { classification: "neutral", contribution: 0, reason: `${name}_neutral` },
      ]),
    );
    return {
      schema_version: 2,
      status: "ok",
      reason: null,
      classification: "neutral",
      direction: "neutral",
      score: 0,
      atr_pct: 1,
      factor_breakdown: factors,
      reasons: Object.values(factors).map((value) => value.reason),
      indicators: { candle: selected, candle_count: candleCount, patterns: [] },
      patterns: [],
      timeframe_minutes: request.timeframe_minutes,
      candle_open_time_ms: request.target_candle_open_time_ms,
      candle_close_time_ms: request.target_candle_open_time_ms + request.timeframe_minutes * 60_000,
      source_event_time_ms: request.source_event_time_ms,
      evaluation_time_ms: request.evaluation_time_ms,
      detection_time_ms: request.detection_time_ms,
      ta_version: "ta-v2",
      strategy_version: "interpretation-v1",
      provenance: {
        ...request.instrument,
        source: request.source,
        price_type: request.price_type,
        candle_count: candleCount,
        warmup_candle_count: 0,
        missing_open_times_ms: [],
      },
    };
  });
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

test("TA scheduling accepts only aligned completed history and uses the Python version", () => {
  const bars = series();
  assert.equal(
    completedCandles([...bars, candle({ time: end, high: 99999, complete: false })], 15, now)
      .length,
    249,
  );
  assert.equal(completedCandles([...bars].reverse(), 15, now).length, 249);
  assert.throws(() => completedCandles(bars.slice(60), 15, now), /200/);
  assert.throws(() => completedCandles([...bars, bars.at(-1)], 15, now), /duplicate/);
  assert.throws(
    () =>
      completedCandles(
        bars.filter((_, i) => i !== 100),
        15,
        now,
      ),
    /gap/,
  );
  assert.equal(TA_VERSION, "ta-v2");
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
      "20260924090000_monitor_efficiency.sql",
      "20260924110000_analysis_conclusions.sql",
      "20260924120000_python_scheduled_ta.sql",
      "20260926100000_ta_source_event_provenance.sql",
    ]) {
      await db.exec(
        await readFile(new URL(`../supabase/migrations/${name}`, import.meta.url), "utf8"),
      );
    }
    await db.exec(`INSERT INTO watchlist_items(user_id,symbol,instrument_id) VALUES
      ('00000000-0000-0000-0000-000000000001','BTCUSDT','binance-usdm:BTCUSDT')`);
    const insert = `INSERT INTO ta_signals(
      user_id,symbol,instrument_id,source_instrument_id,source_native_symbol,
      timeframe,candle_at,source_event_at,evaluated_at,detected_at,source,version,
      strategy_version,classification,score,atr_pct,factor_breakdown,reasons,
      price,indicators,patterns
    ) VALUES (
      '00000000-0000-0000-0000-000000000001','BTCUSDT','binance-usdm:BTCUSDT',
      'binance-usdm:BTCUSDT','BTCUSDT',15,'2025-12-31T23:00:00Z',
      '2025-12-31T23:15:00Z','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',
      'binance-usdm','ta-v2','interpretation-v1','neutral',0,1,'{}','{trend_neutral}',
      100,'{}','{doji}'
    )`;
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

    await db.exec("RESET ROLE; SET ROLE service_role");
    await assert.rejects(
      db.exec("UPDATE ta_signals SET score=10"),
      /conclusion and provenance are immutable/,
    );
    await assert.rejects(
      db.exec("DELETE FROM ta_signals"),
      /conclusion and provenance are immutable/,
    );
    const due = await db.query(
      `SELECT * FROM get_ta_due_work(
        '00000000-0000-0000-0000-000000000001','BTCUSDT','ta-v2',
        '2026-01-01T02:00:00Z',true,true)`,
    );
    assert.deepEqual(due.rows.map((row) => row.work_kind).sort(), ["latest", "outcome"]);
    const signalId = due.rows.find((row) => row.work_kind === "outcome").id;
    const batch = JSON.stringify([
      {
        id: signalId,
        outcome_status: "measured",
        outcome_at: "2026-01-01T01:00:00Z",
        outcome_price: 102,
        return_pct: 2,
      },
    ]);
    assert.equal(
      (
        await db.query("SELECT apply_ta_outcomes($1,$2::jsonb) AS changed", [
          "00000000-0000-0000-0000-000000000001",
          batch,
        ])
      ).rows[0].changed,
      1,
    );
    assert.equal(
      (
        await db.query("SELECT apply_ta_outcomes($1,$2::jsonb) AS changed", [
          "00000000-0000-0000-0000-000000000001",
          batch,
        ])
      ).rows[0].changed,
      0,
    );
    await db.exec(
      "RESET ROLE; SET ROLE authenticated; SET request.jwt.claim.sub = '00000000-0000-0000-0000-000000000001'",
    );
    await assert.rejects(
      db.query("SELECT apply_ta_outcomes($1,$2::jsonb)", [
        "00000000-0000-0000-0000-000000000001",
        batch,
      ]),
      /permission denied/,
    );

    // A REST-derived conclusion has no exchange event, so `source_event_at` is
    // nullable; a present exchange event must still precede detection. The insert is
    // rolled back so it cannot affect the assertions above.
    const signalRow = (candleAt, sourceEventAt, detectedAt) => `INSERT INTO ta_signals(
      user_id,symbol,instrument_id,source_instrument_id,source_native_symbol,
      timeframe,candle_at,source_event_at,evaluated_at,detected_at,source,version,
      strategy_version,classification,score,atr_pct,factor_breakdown,reasons,
      price,indicators,patterns
    ) VALUES (
      '00000000-0000-0000-0000-000000000001','BTCUSDT','binance-usdm:BTCUSDT',
      'binance-usdm:BTCUSDT','BTCUSDT',15,'${candleAt}',
      ${sourceEventAt === null ? "NULL" : `'${sourceEventAt}'`},
      '2026-01-01T00:00:00Z','${detectedAt}',
      'binance-usdm','ta-v2','interpretation-v1','neutral',0,1,'{}','{trend_neutral}',
      100,'{}','{doji}'
    )`;
    await db.exec("RESET ROLE; SET ROLE service_role; BEGIN");
    await db.exec(signalRow("2025-12-31T22:45:00Z", null, "2026-01-01T00:00:00Z"));
    const rest = await db.query(
      "SELECT source_event_at FROM ta_signals WHERE candle_at = '2025-12-31T22:45:00Z'",
    );
    assert.equal(rest.rows[0].source_event_at, null);
    await assert.rejects(
      db.exec(signalRow("2025-12-31T22:30:00Z", "2026-01-01T00:00:00Z", "2025-12-31T23:00:00Z")),
      /ta_signals_python_timestamps_check/,
    );
    await db.exec("ROLLBACK");
  } finally {
    await db.close();
  }
});

test("futures provider preserves OHLCV and contract identity", async () => {
  const providerUrl = await moduleUrl("../src/lib/market/providers.server.ts", {
    "./symbols": await moduleUrl("../src/lib/market/symbols.ts"),
  });
  const { clearExchangeInfoCache, loadFuturesSnapshot, loadObservationCandles, loadTACandles,
    loadBinanceFuturesCompatibility } =
    await import(providerUrl);
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
      return Response.json([
        [end - duration, "100", "102", "98", "101", "35", end - 1, "3535"],
      ]);
    };
    const result = await loadTACandles("BTCUSDT", 15, () => {});
    assert.equal(await loadBinanceFuturesCompatibility("BTCUSDT"), true);
    assert.deepEqual(
      result.candles[0],
      candle({ close: 101, volume: 35, quoteVolume: 3535, complete: end < Date.now() }),
    );
    assert.equal(result.source, "binance-usdm");
    assert.equal(result.instrument.id, "binance-usdm:BTCUSDT");
    assert.equal(result.endpoint, "/fapi/v1/klines");
    assert.equal(result.priceType, "trade");
    assert.equal(result.instrument.priceTick, "0.10");
    assert.equal(result.instrument.quantityStep, "0.001");
    assert.equal(result.instrument.minNotional, "100");
    assert.equal(calls.length, 2);
    clearExchangeInfoCache();
    globalThis.fetch = async () => Response.json({ symbols: [futuresMetadata("BREAK")] });
    assert.equal(await loadBinanceFuturesCompatibility("BTCUSDT"), false);
    clearExchangeInfoCache();
    globalThis.fetch = async () => { throw new Error("metadata unavailable"); };
    assert.equal(await loadBinanceFuturesCompatibility("BTCUSDT"), null);
    clearExchangeInfoCache();
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
      return Response.json([]);
    };
    const snapshot = await loadFuturesSnapshot("BTCUSDT");
    assert.deepEqual(
      [snapshot.lastPrice, snapshot.markPrice, snapshot.indexPrice, snapshot.fundingRate],
      [101, 100.5, 100.4, 0.0001],
    );

    clearExchangeInfoCache();
    calls.length = 0;
    const observed = { requests: 0, rows: 0 };
    const minuteEnd = Math.floor(Date.now() / 60_000) * 60_000;
    globalThis.fetch = async (url) => {
      calls.push(url);
      if (url.endsWith("/fapi/v1/exchangeInfo")) {
        return Response.json({ symbols: [futuresMetadata()] });
      }
      return Response.json([
        [minuteEnd - 120_000, "100", "102", "98", "101", "35", minuteEnd - 60_001, "3535"],
        [minuteEnd - 60_000, "101", "103", "99", "102", "40", minuteEnd - 1, "4080"],
      ]);
    };
    const observation = await loadObservationCandles("BTCUSDT", undefined, {
      request() {
        observed.requests++;
      },
      candleRows(_source, _timeframe, rows) {
        observed.rows += rows;
      },
    });
    assert.equal(observation.ok, true);
    assert.equal(calls.filter((url) => url.includes("/klines?")).length, 1);
    assert.ok(calls.find((url) => url.includes("interval=1m")));
    assert.ok(!calls.find((url) => url.includes("interval=15m")));
    assert.deepEqual(observed, { requests: 2, rows: 2 });

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

test("run context shares only identity-equivalent market inputs", async () => {
  globalThis.__contextCalls = { observation: 0, ta: [] };
  const provider = `data:text/javascript,${encodeURIComponent(`
    export async function loadObservationCandles(symbol,validate,observer) {
      globalThis.__contextCalls.observation++;
      observer.request('binance-usdm','candles',1);
      observer.candleRows('binance-usdm',1,2);
      return {ok:true,result:{source:'binance-usdm',minute:[],quarter:[]}};
    }
    export async function loadTACandles(symbol,timeframe,validate,source,observer) {
      globalThis.__contextCalls.ta.push({symbol,timeframe,source});
      observer.request('binance-usdm','candles',timeframe);
      observer.candleRows('binance-usdm',timeframe,250);
      return {source:source??'binance-usdm',instrument:{id:'binance-usdm:'+symbol},endpoint:'/fapi/v1/klines',priceType:'trade',candles:[]};
    }
  `)}`;
  const { createMonitorRunContext } = await import(
    await moduleUrl("../src/lib/monitor/run-context.ts", {
      "../market/providers.server": provider,
    })
  );
  try {
    const context = createMonitorRunContext();
    await context.observation("BTCUSDT");
    await context.observation("BTCUSDT");
    await context.ta("BTCUSDT", 15, () => {});
    await context.ta("BTCUSDT", 15, () => {});
    await context.ta("BTCUSDT", 15, () => {}, "binance-usdm");
    await context.ta("BTCUSDT", 15, () => {}, "okx-usdt-swap");
    assert.equal(globalThis.__contextCalls.observation, 1);
    assert.deepEqual(globalThis.__contextCalls.ta, [
      { symbol: "BTCUSDT", timeframe: 15, source: undefined },
      { symbol: "BTCUSDT", timeframe: 15, source: "okx-usdt-swap" },
    ]);
    assert.equal(context.metrics.marketCacheHits, 3);
    assert.equal(context.metrics.exchangeRequests, 3);
    assert.equal(context.metrics.candleRows, 502);
  } finally {
    delete globalThis.__contextCalls;
  }
});

test("TA due gating skips completed work and bounds catch-up batches", async () => {
  const now = 1_800_000_000_000;
  const oldNow = Date.now;
  Date.now = () => now;
  const contextModule = `data:text/javascript,${encodeURIComponent(`
    export const createMonitorRunContext=()=>{throw Error('explicit context required')};
  `)}`;
  const { latestCompletedCandleAt, runTA, TA_CATCH_UP_LIMIT } = await import(
    await moduleUrl("../src/lib/ta/engine.server.ts", {
      "../activity-controls": await moduleUrl("../src/lib/activity-controls.ts"),
      "./python-client.server": "data:text/javascript,export const calculateTechnicalBatch=()=>{};",
      "./python-contract": await moduleUrl("../src/lib/ta/python-contract.ts", {
        zod: import.meta.resolve("zod"),
      }),
      "./schedule": await moduleUrl("../src/lib/ta/schedule.ts"),
      "../monitor/run-context": contextModule,
      "../market/symbols": "data:text/javascript,export const MARKET_SOURCE='binance-usdm';",
    })
  );
  const zero = () => ({
    exchangeRequests: 0,
    candleRows: 0,
    marketCacheHits: 0,
    taCalculations: 0,
    taSignalsSaved: 0,
    taOutcomesUpdated: 0,
    databaseReads: 0,
    databaseWriteAttempts: 0,
    databaseNoOps: 0,
  });
  const oldOutcomes = process.env.TA_OUTCOME_EVALUATION_ENABLED;
  process.env.TA_OUTCOME_EVALUATION_ENABLED = "false";
  try {
    let providerCalls = 0;
    const upserts = [];
    let latestOffset = 0;
    const db = {
      async rpc(name) {
        assert.equal(name, "get_ta_due_work");
        return {
          error: null,
          data: [15, 60, 240].map((timeframe) => ({
            work_kind: "latest",
            timeframe,
            candle_at: new Date(
              latestCompletedCandleAt(timeframe, now) - latestOffset * timeframe * 60_000,
            ).toISOString(),
          })),
        };
      },
      from() {
        return {
          upsert(rows) {
            upserts.push(rows);
            return { select: async () => ({ data: rows.map((_, id) => ({ id })), error: null }) };
          },
        };
      },
    };
    const context = {
      metrics: zero(),
      async ta(symbol, timeframe) {
        providerCalls++;
        const end = latestCompletedCandleAt(timeframe, now);
        const duration = timeframe * 60_000;
        return {
          source: "binance-usdm",
          instrument: { id: `binance-usdm:${symbol}` },
          endpoint: "/fapi/v1/klines",
          priceType: "trade",
          candles: Array.from({ length: 220 }, (_, index) => ({
            time: end - (219 - index) * duration,
            open: 100,
            high: 101,
            low: 99,
            close: 100,
            volume: 1,
            complete: true,
          })),
        };
      },
    };

    assert.deepEqual(await runTA(db, "owner", "BTCUSDT", context, pythonResults), []);
    assert.equal(providerCalls, 0);
    assert.equal(upserts.length, 0);
    assert.equal(context.metrics.databaseNoOps, 3);

    latestOffset = 10;
    assert.deepEqual(await runTA(db, "owner", "BTCUSDT", context, pythonResults), []);
    assert.equal(providerCalls, 3);
    assert.equal(upserts.length, 3);
    assert.ok(upserts.every((rows) => rows.length === TA_CATCH_UP_LIMIT));
    assert.equal(context.metrics.taCalculations, TA_CATCH_UP_LIMIT * 3);
    assert.equal(context.metrics.taSignalsSaved, TA_CATCH_UP_LIMIT * 3);
  } finally {
    Date.now = oldNow;
    if (oldOutcomes === undefined) delete process.env.TA_OUTCOME_EVALUATION_ENABLED;
    else process.env.TA_OUTCOME_EVALUATION_ENABLED = oldOutcomes;
  }
});

test("TA runner isolates frame failures and settles on the recorded futures source", async () => {
  const { runTA } = await import(
    await moduleUrl("../src/lib/ta/engine.server.ts", {
      "../activity-controls": await moduleUrl("../src/lib/activity-controls.ts"),
      "./python-client.server": "data:text/javascript,export const calculateTechnicalBatch=()=>{};",
      "./python-contract": await moduleUrl("../src/lib/ta/python-contract.ts", {
        zod: import.meta.resolve("zod"),
      }),
      "./schedule": await moduleUrl("../src/lib/ta/schedule.ts"),
      "../monitor/run-context": `data:text/javascript,${encodeURIComponent(`
        const zero=()=>({exchangeRequests:0,candleRows:0,marketCacheHits:0,taCalculations:0,taSignalsSaved:0,taOutcomesUpdated:0,databaseReads:0,databaseWriteAttempts:0,databaseNoOps:0});
        export const createMonitorRunContext=()=>({metrics:zero(),ta:(...args)=>globalThis.__taLoad(...args)});
      `)}`,
      "../market/symbols": "data:text/javascript,export const MARKET_SOURCE='binance-usdm';",
    })
  );
  const calls = [],
    writes = [],
    outcomeBatches = [];
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
    async rpc(name, args) {
      if (name === "get_ta_due_work") {
        const rows = [];
        for (const frame of [15, 60, 240]) {
          const step = frame * 60000;
          const boundary = Math.floor(Date.now() / step) * step;
          rows.push(
            {
              work_kind: "outcome",
              id: `due-${frame}`,
              timeframe: frame,
              candle_at: new Date(boundary - 10 * step).toISOString(),
              source: "binance-usdm",
              detected_at: new Date(boundary - 10 * step).toISOString(),
              price: 100,
            },
            {
              work_kind: "outcome",
              id: `expired-${frame}`,
              timeframe: frame,
              candle_at: new Date(boundary - 500 * step).toISOString(),
              source: "binance-usdm",
              detected_at: new Date(boundary - 500 * step).toISOString(),
              price: 100,
            },
          );
        }
        return { data: rows, error: null };
      }
      assert.equal(name, "apply_ta_outcomes");
      outcomeBatches.push(args);
      return { data: args.p_outcomes.length, error: null };
    },
    from(table) {
      assert.equal(table, "ta_signals");
      return {
        upsert(rows, options) {
          writes.push({ rows, options });
          return { select: async () => ({ data: rows.map((_, id) => ({ id })), error: null }) };
        },
      };
    },
  };
  try {
    const errors = await runTA(db, "owner", "BTCUSDT", undefined, pythonResults);
    assert.equal(errors.length, 1);
    assert.match(errors[0], /60m.*feed down/);
    assert.equal(writes.length, 2);
    assert.ok(
      writes.every(
        (w) => w.options.ignoreDuplicates && w.rows.length === 1 && w.rows[0].user_id === "owner",
      ),
    );
    assert.equal(calls.filter((c) => c.source === "binance-usdm").length, 0);
    assert.equal(outcomeBatches.length, 2);
    assert.ok(outcomeBatches.every((batch) => batch.p_user_id === "owner"));
    const updates = outcomeBatches.flatMap((batch) => batch.p_outcomes);
    assert.equal(updates.length, 4);
    assert.equal(updates.filter((u) => u.outcome_status === "unavailable").length, 2);
    for (const update of updates.filter((u) => u.outcome_status === "measured")) {
      assert.equal(update.outcome_price, 101);
      assert.ok(Math.abs(update.return_pct - 1) < 1e-9);
    }
  } finally {
    delete globalThis.__taLoad;
  }
});

test("collector mode reads canonical completed candles and never a second live series", async () => {
  const fixedNow = 1_800_000_000_000;
  const oldNow = Date.now;
  Date.now = () => fixedNow;
  const contextModule = `data:text/javascript,export const createMonitorRunContext=()=>{throw Error('explicit context required')};`;
  const { runTA } = await import(
    await moduleUrl("../src/lib/ta/engine.server.ts", {
      "../activity-controls": await moduleUrl("../src/lib/activity-controls.ts"),
      "./python-client.server": "data:text/javascript,export const calculateTechnicalBatch=()=>{};",
      "./python-contract": await moduleUrl("../src/lib/ta/python-contract.ts", {
        zod: import.meta.resolve("zod"),
      }),
      "./schedule": await moduleUrl("../src/lib/ta/schedule.ts"),
      "../monitor/run-context": contextModule,
      "../market/symbols": "data:text/javascript,export const MARKET_SOURCE='binance-usdm';",
    })
  );
  const zero = () => ({
    exchangeRequests: 0,
    candleRows: 0,
    marketCacheHits: 0,
    taCalculations: 0,
    taSignalsSaved: 0,
    taOutcomesUpdated: 0,
    databaseReads: 0,
    databaseWriteAttempts: 0,
    databaseNoOps: 0,
  });
  const latest = (timeframe) => {
    const step = timeframe * 60_000;
    return Math.floor(fixedNow / step) * step - step;
  };
  // The newest four candles are WebSocket live; the rest are REST
  // bootstrap/recovery. The adapter must keep each candle's own provenance.
  const collectorSeries = (timeframe, end = latest(timeframe)) => {
    const step = timeframe * 60_000;
    return Array.from({ length: 220 }, (_, index) => {
      const time = end - (219 - index) * step;
      const websocket = index >= 216;
      return {
        time,
        open: 100,
        high: 101,
        low: 99,
        close: 100,
        volume: 1,
        complete: true,
        closeTime: time + step - 1,
        // WebSocket candles carry the exchange's actual event time, which deliberately
        // differs from the deterministic completion boundary (`time + step`). REST
        // bootstrap/recovery has no exchange event and must stay honestly absent.
        sourceEventTime: websocket ? time + step + 7 : null,
        receivedAt: time + step + 120,
        endpoint: websocket ? "wss://fstream.binance.com/market/stream" : "/fapi/v1/klines",
        transport: websocket ? "websocket" : "rest",
      };
    });
  };
  const previous = process.env.BINANCE_COLLECTOR_ENABLED;
  process.env.BINANCE_COLLECTOR_ENABLED = "true";
  try {
    let restCalls = 0;
    const collectorReads = [];
    const upserts = [];
    const outcomeBatches = [];
    const capturedRequests = [];
    const store = {
      enabled: true,
      async readCollectorCompletedCandles(symbol, timeframe) {
        collectorReads.push([symbol, timeframe]);
        return completedCollectorSeries(symbol, timeframe, collectorSeries(timeframe));
      },
    };
    const context = {
      metrics: zero(),
      async ta() {
        restCalls++;
        throw new Error("REST TA provider must not run in collector mode");
      },
    };
    const db = {
      async rpc(name, args) {
        if (name === "get_ta_due_work") {
          const rows = [15, 60, 240].map((timeframe) => ({
            work_kind: "latest",
            timeframe,
            // Eight frames behind forces the full bounded catch-up batch.
            candle_at: new Date(latest(timeframe) - 8 * timeframe * 60_000).toISOString(),
          }));
          // An outcome recorded under a different provider has no canonical collector
          // history and must be settled as visibly unavailable.
          rows.push({
            work_kind: "outcome",
            id: "foreign",
            timeframe: 15,
            candle_at: new Date(latest(15) - 10 * duration).toISOString(),
            source: "okx-usdt-swap",
            detected_at: new Date(latest(15) - 10 * duration).toISOString(),
            price: 100,
          });
          return { data: rows, error: null };
        }
        assert.equal(name, "apply_ta_outcomes");
        outcomeBatches.push(args.p_outcomes);
        return { data: args.p_outcomes.length, error: null };
      },
      from(table) {
        assert.equal(table, "ta_signals");
        return {
          upsert(rows) {
            upserts.push(rows);
            return { select: async () => ({ data: rows.map((_, id) => ({ id })), error: null }) };
          },
        };
      },
    };
    const calculate = async (requests) => {
      capturedRequests.push(...requests);
      return pythonResults(requests);
    };

    assert.deepEqual(await runTA(db, "owner", "BTCUSDT", context, calculate, store), []);
    assert.equal(restCalls, 0);
    assert.deepEqual(collectorReads, [
      ["BTCUSDT", 15],
      ["BTCUSDT", 60],
      ["BTCUSDT", 240],
    ]);
    assert.equal(upserts.length, 3);
    assert.ok(upserts.every((rows) => rows.length === 8));

    // Every persisted conclusion keeps the exact endpoint of the candle that produced
    // it: WebSocket live candles stay WebSocket-derived, REST bootstrap/recovery
    // candles keep REST provenance.
    const wsEndpoint = "wss://fstream.binance.com/market/stream";
    const restEndpoint = "/fapi/v1/klines";
    for (const timeframe of [15, 60, 240]) {
      const rows = upserts.find((batch) => batch[0].timeframe === timeframe);
      const expected = collectorSeries(timeframe).slice(-8);
      assert.deepEqual(
        rows.map((row) => row.endpoint),
        expected.map((candle) => candle.endpoint),
      );
      // The four newest candidates are live WebSocket candles, so both provenance
      // kinds are exercised through the adapter.
      assert.equal(rows.filter((row) => row.endpoint === wsEndpoint).length, 4);
      assert.equal(rows.filter((row) => row.endpoint === restEndpoint).length, 4);
      for (const row of rows) {
        const openTime = Date.parse(row.candle_at);
        const expectedCandle = expected.find((candle) => candle.time === openTime);
        // The persisted source event is exactly the collector's recorded exchange event
        // time; a REST candle honestly persists no source event at all.
        if (expectedCandle.sourceEventTime === null) {
          assert.equal(row.source_event_at, null);
        } else {
          assert.equal(Date.parse(row.source_event_at), expectedCandle.sourceEventTime);
        }
      }
    }

    // The versioned Python request carries the recorded exchange event time exactly
    // (null for REST) and the exact target candle open, never a reconstructed boundary.
    assert.equal(capturedRequests.length, 24);
    for (const request of capturedRequests) {
      const step = request.timeframe_minutes * 60_000;
      const expectedCandle = collectorSeries(request.timeframe_minutes).find(
        (candle) => candle.time === request.target_candle_open_time_ms,
      );
      assert.equal(request.source_event_time_ms, expectedCandle.sourceEventTime);
      assert.equal(
        request.candles.find((candle) => candle.open_ms === request.target_candle_open_time_ms)
          .open_ms,
        request.target_candle_open_time_ms,
      );
      // Finality is the deterministic completion boundary, never the exchange event.
      assert.ok(request.target_candle_open_time_ms + step <= request.evaluation_time_ms);
    }
    // A valid WebSocket exchange event time that differs from the completion boundary
    // must survive into the request without failing TA.
    const wsRequests = capturedRequests.filter(
      (request) =>
        collectorSeries(request.timeframe_minutes).find(
          (candle) => candle.time === request.target_candle_open_time_ms,
        ).transport === "websocket",
    );
    assert.ok(wsRequests.length > 0);
    assert.ok(
      wsRequests.every(
        (request) =>
          request.source_event_time_ms !==
          request.target_candle_open_time_ms + request.timeframe_minutes * 60_000,
      ),
    );
    // REST-sourced requests carry no invented exchange event time.
    const restRequests = capturedRequests.filter(
      (request) =>
        collectorSeries(request.timeframe_minutes).find(
          (candle) => candle.time === request.target_candle_open_time_ms,
        ).transport === "rest",
    );
    assert.ok(restRequests.length > 0);
    assert.ok(restRequests.every((request) => request.source_event_time_ms === null));

    assert.deepEqual(outcomeBatches, [
      [
        {
          id: "foreign",
          outcome_status: "unavailable",
          outcome_at: null,
          outcome_price: null,
          return_pct: null,
        },
      ],
    ]);

    // Missing canonical history fails visibly instead of fetching a live series.
    const emptyStore = {
      enabled: true,
      async readCollectorCompletedCandles(symbol, timeframe) {
        return completedCollectorSeries(symbol, timeframe, []);
      },
    };
    const missing = await runTA(db, "owner", "BTCUSDT", context, pythonResults, emptyStore);
    assert.equal(missing.length, 3);
    assert.ok(missing.every((error) => /At least 200 completed candles required/.test(error)));
    assert.equal(restCalls, 0);

    // Stale canonical history also fails visibly.
    const staleStore = {
      enabled: true,
      async readCollectorCompletedCandles(symbol, timeframe) {
        return completedCollectorSeries(symbol, timeframe,
          collectorSeries(timeframe, latest(timeframe) - 50 * timeframe * 60_000));
      },
    };
    const stale = await runTA(db, "owner", "BTCUSDT", context, pythonResults, staleStore);
    assert.equal(stale.length, 3);
    assert.ok(stale.every((error) => /Stale TA candles/.test(error)));
    assert.equal(restCalls, 0);
  } finally {
    Date.now = oldNow;
    if (previous === undefined) delete process.env.BINANCE_COLLECTOR_ENABLED;
    else process.env.BINANCE_COLLECTOR_ENABLED = previous;
  }
});
