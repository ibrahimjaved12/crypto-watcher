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
const stub = (s) => `data:text/javascript;base64,${Buffer.from(s).toString("base64")}`;
const contractUrl = await moduleUrl("../src/lib/market/completed-candle-contract.ts");
const { completedCandleIdentity, validateCompletedCandleSeries, completedMarketCandles,
  missingCompletedCandleOpenTimes } = await import(contractUrl);
const { validatePythonCompletedCandleSeries } = await import(await moduleUrl(
  "../src/lib/market/completed-candle-python-client.server.ts", {
    zod: import.meta.resolve("zod"), "./completed-candle-contract": contractUrl,
    "../python-service.server": await moduleUrl("../src/lib/python-service.server.ts"),
  }));
const { createOperationalStore } = await import(await moduleUrl(
  "../src/lib/operational/repository.server.ts", {
    "../market/completed-candle-contract": contractUrl,
    "@supabase/supabase-js": stub("export const createClient=()=>({});"),
    "./config.server": stub("export const operationalDbConfig=()=>({enabled:false});"),
    "../market/symbols": stub('export const MARKET_SOURCE="binance-usdm"; export const MARKET_PRICE_TYPE="trade";'),
  }));
const candle = (time = 60_000) => ({ openTime: time, closeTime: time + 59_999,
  open: 100, high: 102, low: 99, close: 101, baseVolume: 2, quoteVolume: 202.25 });
const series = () => ({ contractVersion: "completed-candle-v1", identity: completedCandleIdentity("BTCUSDT", 1),
  observations: [{ candle: candle(), provenance: { sourceKind: "rest", endpoint: "/klines", retrievedAt: 120_100 } }] });
const row = (frame = 1, patch = {}) => ({ provider: "binance-usdm", instrument_id: "binance-usdm:BTCUSDT",
  symbol: "BTCUSDT", native_symbol: "BTCUSDT", market_type: "futures", contract_type: "perpetual",
  price_type: "trade", timeframe_minutes: frame, endpoint: "/fapi/v1/klines", transport: "rest",
  open_time_ms: 0, close_time_ms: frame * 60_000 - 1, source_event_at_ms: null, received_at_ms: frame * 60_000 + 100,
  open: 100, high: 102, low: 99, close: 101, volume: 2, quote_volume: 202.25, ...patch });
const store = (rows) => createOperationalStore({ async rpc() { return { data: rows, error: null }; } },
  { candleRetentionDays: 7, monitorRunRetentionDays: 30, outboxMaxAttempts: 10 });

test("market facts deduplicate across provenance; gaps remain explicit", () => {
  const value = series();
  value.observations.push({ candle: candle(), provenance: { sourceKind: "websocket", endpoint: "ws",
    sourceEventTime: 119_997, receivedAt: 120_123 } },
    { candle: candle(180_000), provenance: { sourceKind: "archive", datasetId: "d", datasetVersion: "v1",
      datasetContentSha256: "a".repeat(64) } });
  validateCompletedCandleSeries(value);
  assert.equal(completedMarketCandles(value).length, 2);
  assert.deepEqual(missingCompletedCandleOpenTimes(value), [120_000]);
  assert.equal(value.observations[1].provenance.sourceEventTime, 119_997);
  assert.equal("sourceEventTime" in value.observations[0].provenance, false);
  const bad = structuredClone(value);
  bad.observations[1].candle.quoteVolume = 999;
  assert.throws(() => validateCompletedCandleSeries(bad), /Conflicting/);
  for (const patch of [{ closeTime: 120_000 }, { openTime: 60_001 }, { quoteVolume: -1 }, { high: 99 }]) {
    const bad = series(); Object.assign(bad.observations[0].candle, patch);
    assert.throws(() => validateCompletedCandleSeries(bad));
  }
});

test("generic operational read preserves exact native identity and provenance at every frame", async () => {
  for (const frame of [1, 15, 60, 240]) {
    const rest = await store([row(frame)]).readCollectorCompletedCandles("btcusdt", frame);
    assert.equal(rest.identity.timeframeMinutes, frame);
    assert.equal(rest.observations[0].candle.quoteVolume, 202.25);
    assert.deepEqual(rest.observations[0].provenance,
      { sourceKind: "rest", endpoint: "/fapi/v1/klines", retrievedAt: frame * 60_000 + 100 });
    const ws = await store([row(frame, { transport: "websocket", endpoint: "wss://fstream.binance.com/market/stream", source_event_at_ms: 12345 })])
      .readCollectorCompletedCandles("BTCUSDT", frame);
    assert.equal(ws.observations[0].provenance.sourceEventTime, 12345);
    assert.equal((await store([]).readCollectorCompletedCandles("BTCUSDT", frame)).observations.length, 0);
  }
  const gap = await store([row(), row(1, { open_time_ms: 120_000, close_time_ms: 179_999 })])
    .readCollectorCompletedCandles("BTCUSDT", 1);
  assert.deepEqual(missingCompletedCandleOpenTimes(gap), [60_000]);
  assert.equal(gap.observations.length, 2);
});

test("DB adapter fails closed on inconsistent identities, intervals and transport provenance", async () => {
  for (const patch of [{ endpoint: "wss://fstream.binance.com/market/stream" },
    { transport: "websocket", endpoint: "/fapi/v1/klines", source_event_at_ms: 12345 },
    { source_event_at_ms: 1 }, { transport: "websocket", endpoint: "wss://fstream.binance.com/market/stream", source_event_at_ms: null },
    { transport: "websocket", endpoint: "wss://fstream.binance.com/market/stream", source_event_at_ms: true }, { received_at_ms: null },
    { symbol: "ETHUSDT" }, { native_symbol: "ETHUSDT" }, { instrument_id: "BTCUSDT" },
    { market_type: "spot" }, { contract_type: "dated" }, { provider: "other" }, { price_type: "mark" },
    { timeframe_minutes: 15 }, { close_time_ms: 60_000 }, { open_time_ms: 1 },
    { quote_volume: null }, { quote_volume: -1 }, { volume: -1 }, { open: 0 }, { endpoint: "" }]) {
    await assert.rejects(store([row(1, patch)]).readCollectorCompletedCandles("BTCUSDT", 1));
  }
  const bad = series(); bad.observations[0].provenance.sourceEventTime = 1;
  assert.throws(() => validateCompletedCandleSeries(bad), /REST/);
});

const env = { PYTHON_ANALYSIS_ENABLED: "true", PYTHON_ANALYSIS_URL: "https://python.example/",
  PYTHON_ANALYSIS_TOKEN: "x".repeat(32) };
const response = { schema_version: 1, contract_version: "completed-candle-v1", provider: "binance-usdm",
  exchange: "binance", instrument_id: "binance-usdm:BTCUSDT", price_type: "trade", timeframe_minutes: 1,
  observation_count: 1, candle_count: 1, first_open_time_ms: 60_000, last_open_time_ms: 60_000,
  missing_open_times_ms: [] };
test("Python client sends exactly the versioned contract with decimal text and no REST source event", async () => {
  const result = await validatePythonCompletedCandleSeries(series(), env, async (url, init) => {
    assert.equal(url, "https://python.example/v1/completed-candles/validate");
    assert.equal(init.redirect, "error"); assert.equal(init.cache, "no-store");
    assert.equal(init.headers.Authorization, `Bearer ${env.PYTHON_ANALYSIS_TOKEN}`);
    assert.equal(init.headers["Cache-Control"], "no-store");
    assert.deepEqual(JSON.parse(init.body), {
      contract_version: "completed-candle-v1",
      identity: { provider: "binance-usdm", exchange: "binance", market_type: "futures", contract_type: "perpetual",
        instrument_id: "binance-usdm:BTCUSDT", symbol: "BTCUSDT", native_symbol: "BTCUSDT",
        price_type: "trade", series_basis: "native-kline", timeframe_minutes: 1 },
      observations: [{ candle: { open_time_ms: 60_000, close_time_ms: 119_999, open: "100", high: "102",
        low: "99", close: "101", base_volume: "2", quote_volume: "202.25" },
        provenance: { source_kind: "rest", endpoint: "/klines", retrieved_at_ms: 120_100 } }],
    });
    return Response.json(response);
  });
  assert.deepEqual(result, response);
});
test("Python client rejects mismatched summaries and unsupported frames", async () => {
  for (const patch of [{ instrument_id: "binance-usdm:ETHUSDT" }, { observation_count: 2 },
    { candle_count: 2 }, { first_open_time_ms: null }, { last_open_time_ms: 120_000 },
    { missing_open_times_ms: [120_000] }, { unexpected: 1 }]) {
    await assert.rejects(validatePythonCompletedCandleSeries(series(), env,
      async () => Response.json({ ...response, ...patch })));
  }
  const bad = series(); bad.identity.timeframeMinutes = 15;
  await assert.rejects(validatePythonCompletedCandleSeries(bad, env, async () => assert.fail("must not send")));
});
