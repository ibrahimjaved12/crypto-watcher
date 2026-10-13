// TEMPORARY degraded-mode fallback (#323): remove after the machine has run 24/7 for 30 days
import assert from "node:assert/strict";
import { test } from "node:test";
import { createModuleLoader } from "./module-loader.mjs";
const moduleUrl = createModuleLoader(import.meta.url);
const { withDegradedFallback, DEGRADED_SOURCES, degradedSourceLabel, degradedContractLabel } =
  await import(await moduleUrl("../src/lib/market/degraded-fallback.ts"));
const { getQuotes } = await import(await moduleUrl("../src/lib/market/quotes.server.ts"));
const { loadCandles } = await import(await moduleUrl("../src/lib/market/providers.server.ts"));
const stamp = "2026-10-13T00:00:00.000Z";
const quote = (symbol, source = "binance-usdm", stale = false, ok = true) => ({
  symbol, source: ok ? source : null, stale, ok, price: ok ? 100 : null,
  changes: {}, chart: [], lastCandleAt: Date.parse(stamp), fetchedAt: stamp,
  error: ok ? null : "unavailable", fallbackReasons: [],
});
const snapshot = (quotes, publishedAt = stamp) => ({ quotes, publishedAt, fetchedAt: stamp });

test("fresh, mixed stale/fresh and fresh with missing symbols never call an exchange", async () => {
  for (const quotes of [
    [quote("BTCUSDT")],
    [quote("BTCUSDT"), quote("ETHUSDT", "binance-usdm", true)],
    [quote("BTCUSDT"), quote("ETHUSDT", null, true, false)],
  ]) {
    const original = snapshot(quotes);
    const result = await withDegradedFallback(original, quotes.map(q => q.symbol), async () => {
      assert.fail("fresh snapshot must not load exchange quotes");
    });
    assert.equal(result, original);
    assert.equal(result.degraded, undefined);
  }
});

test("stale and empty snapshots select Kraken for the entire page and label it", async () => {
  for (const original of [snapshot([quote("BTCUSDT", "binance-usdm", true)]), snapshot([], null)]) {
    const calls = [];
    const result = await withDegradedFallback(original, ["BTCUSDT", "ETHUSDT"], async (symbols, options) => {
      calls.push([symbols, options.sources]);
      return [quote("BTCUSDT", options.sources[0]), quote("ETHUSDT", null, true, false)];
    });
    assert.deepEqual(calls, [[["BTCUSDT", "ETHUSDT"], ["kraken-futures"]]]);
    assert.deepEqual(result.degraded, {
      source: "kraken-futures", reason: original.publishedAt ? "stale" : "empty",
      lastPublishedAt: original.publishedAt,
    });
    assert.equal(result.quotes[1].ok, false, "do not fill a failed Kraken pair from another venue");
    assert.equal(degradedSourceLabel(result.degraded.source), "Kraken futures");
    assert.match(degradedContractLabel("kraken-futures", "BTCUSDT"), /PF_XBTUSD.*USD perpetual.*trade price.*not Binance/);
    assert.match(degradedContractLabel("okx-usdt-swap", "ETHUSDT"), /ETH-USDT-SWAP.*trade price.*not Binance/);
    assert.equal(degradedContractLabel("binance-usdm", "BTCUSDT"), null);
  }
});

test("Kraken then OKX, never Binance; retain #319 failure diagnostics", async () => {
  const sources = [];
  const failure = { source: "kraken-futures", status: 403, message: "unavailable" };
  const result = await withDegradedFallback(snapshot([], null), ["BTCUSDT"], async (symbols, options) => {
    sources.push(...options.sources);
    if (options.sources[0] === "kraken-futures")
      return [{ ...quote("BTCUSDT", null, true, false), fallbackReasons: [failure] }];
    return [quote("BTCUSDT", "okx-usdt-swap")];
  });
  assert.deepEqual(sources, ["kraken-futures", "okx-usdt-swap"]);
  assert.equal(result.degraded.source, "okx-usdt-swap");
  assert.equal(degradedSourceLabel(result.degraded.source), "OKX USDT perpetual swaps");
  assert.deepEqual(result.quotes[0].fallbackReasons, [failure]);
});

test("kill switch off and total failure preserve the published snapshot", async () => {
  for (const original of [snapshot([], null), snapshot([quote("BTCUSDT", "binance-usdm", true)])]) {
    assert.equal(await withDegradedFallback(original, ["BTCUSDT"], async () => {
      assert.fail("disabled fallback must not load quotes");
    }, "false"), original);
    assert.equal(await withDegradedFallback(original, ["BTCUSDT"], async () => [quote("BTCUSDT", null, true, false)]), original);
  }
});

test("source list passes through quote builder and provider uses its explicit order", async () => {
  const sources = [];
  const candle = { time: Date.now(), close: 100 };
  const quotes = await getQuotes(["BTCUSDT", "BTCUSDT"], {
    sources: DEGRADED_SOURCES,
    load: (symbol, validate, observer, requested) => loadCandles(symbol, validate, observer, requested,
      async (source) => {
        sources.push(source);
        if (source === "kraken-futures") throw new Error("Kraken down");
        return { source, minute: [candle, candle], quarter: [candle, candle], attempts: [] };
      }),
  });
  assert.deepEqual(sources, ["kraken-futures", "okx-usdt-swap"]);
  assert.equal(quotes.length, 1);
  assert.equal(quotes[0].source, "okx-usdt-swap");
  assert.equal(quotes[0].fallbackReasons[0].source, "kraken-futures");
});

test("exchange request budget is shared across both fallback venues", async () => {
  let observer;
  await withDegradedFallback(snapshot([], null), ["BTCUSDT"], async (_, options) => {
    if (!observer) {
      observer = options.observer;
      for (let i = 0; i < 40; i += 1) observer.request("kraken-futures", "candles");
    } else {
      assert.equal(options.observer, observer);
      assert.throws(() => observer.request("okx-usdt-swap", "metadata"), /budget exhausted/);
    }
    return [quote("BTCUSDT", null, true, false)];
  });
});
