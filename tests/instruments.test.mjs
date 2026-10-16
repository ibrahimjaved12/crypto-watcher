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

const symbolsUrl = await moduleUrl("../src/lib/market/symbols.ts");
const inst = await import(await moduleUrl("../src/lib/market/instruments.ts", { "./symbols": symbolsUrl }));

const anyVolume = new Set(["volume_real", "volume_tick"]);

test("crypto ids are unchanged and stay restricted to the supported symbols", () => {
  const btc = inst.cryptoInstrument("btcusdt");
  assert.equal(btc.instrumentId, "binance-usdm:BTCUSDT");
  assert.equal(btc.base, "BTC");
  assert.equal(btc.quote, "USDT");
  assert.equal(btc.market, "crypto");
  assert.equal(btc.kind, "perpetual");
  assert.throws(() => inst.cryptoInstrument("NOTACOINUSDT"), /Unsupported futures contract/);
  assert.throws(() => inst.cryptoInstrument("BTCUSD"), /Unsupported futures contract/);
});

test("kind/market mismatch and bad ids are rejected", () => {
  const base = { market: "forex", kind: "spot_cfd", assetClass: "fx", base: "EUR", quote: "USD", display: "EURUSD" };
  assert.throws(() => inst.makeInstrument({ ...base, instrumentId: "binance-usdm:BTCUSDT", market: "crypto" }), /not valid for market/);
  assert.throws(() => inst.makeInstrument({ ...base, instrumentId: "fx:EURUSD", kind: "perpetual" }), /not valid for market/);
  for (const bad of ["EURUSD", "FX:EURUSD", "fx:eurusd", "fx:", "fx:EUR USD"])
    assert.throws(() => inst.makeInstrument({ ...base, instrumentId: bad }), /prefix:SYMBOL/, bad);
  assert.throws(() => inst.makeInstrument({ ...base, instrumentId: "fut:EURUSD" }), /must start with 'fx:'/);
});

test("forex defaults, optional extras and user-added symbols", () => {
  assert.deepEqual(inst.FOREX_DEFAULTS.map((i) => i.instrumentId), ["fx:XAUUSD", "fx:EURUSD", "fx:USDJPY", "fx:USOIL"]);
  assert.deepEqual(inst.FOREX_OPTIONAL.map((i) => i.instrumentId), ["fx:DXY", "fx:UKOIL"]);
  assert.equal(inst.FOREX_DEFAULTS[3].display, "WTI crude");
  const eur = inst.forexInstrument("EURUSD");
  assert.equal(eur.base, "EUR");
  assert.equal(eur.quote, "USD");
  assert.equal(inst.forexInstrument("eurusd", { kind: "future" }).instrumentId, "fut:EURUSD");
  assert.throws(() => inst.forexInstrument("USOIL"), /base and quote are required/);
});

test("venue symbols reject non-positive sizes", () => {
  const ok = inst.makeVenueSymbol({ venue: "exness-mt5-demo", nativeSymbol: "EURUSDm", instrumentId: "fx:EURUSD", digits: 5, tickSize: "0.00001" });
  assert.equal(ok.contractSize, null);
  for (const extra of [{ tickSize: "0" }, { tickSize: "0.000" }, { tickSize: "-0.1" }, { contractSize: "0" }, { digits: -1 }])
    assert.throws(() => inst.makeVenueSymbol({ venue: "exness-mt5-demo", nativeSymbol: "EURUSDm", instrumentId: "fx:EURUSD", ...extra }));
});

test("compatibility reasons match the Python strings and order", () => {
  const btc = inst.cryptoInstrument("BTCUSDT");
  const eur = inst.forexInstrument("EURUSD");
  const eurFuture = inst.forexInstrument("EURUSD", { kind: "future" });
  const requires = new Set(["ohlc", "taker_flow"]);
  const cryptoOnly = { markets: new Set(["crypto"]), requires };
  assert.deepEqual(inst.checkCompatibility(cryptoOnly, btc), { ok: true, reasons: [] });
  assert.deepEqual(inst.checkCompatibility(cryptoOnly, eur).reasons, ["market_not_supported:forex", "missing_field:taker_flow"]);
  const both = { markets: new Set(["crypto", "forex"]), requires };
  assert.deepEqual(inst.checkCompatibility(both, eur), { ok: false, reasons: ["missing_field:taker_flow"] });

  const markets = new Set(["crypto", "forex"]);
  assert.equal(inst.checkCompatibility({ markets, requires: new Set(["ohlc"]), requiresAny: [anyVolume] }, eur).ok, true);
  const realVolume = { markets, requires: new Set(["volume_real"]) };
  assert.deepEqual(inst.checkCompatibility(realVolume, eur).reasons, ["missing_field:volume_real"]);
  assert.equal(inst.checkCompatibility(realVolume, eurFuture).ok, true);

  assert.deepEqual(inst.checkCompatibility(null, btc), { ok: false, reasons: ["no_capabilities_declared"] });
  const many = {
    markets: new Set(["crypto"]),
    requires: new Set(["taker_flow", "funding", "ohlc"]),
    requiresAny: [new Set(["open_interest", "mark"])],
  };
  assert.deepEqual(inst.checkCompatibility(many, eur).reasons, [
    "market_not_supported:forex",
    "missing_field:funding",
    "missing_field:taker_flow",
    "missing_any_of:mark|open_interest",
  ]);
});

test("parseMarket falls back for unknown values", () => {
  assert.equal(inst.parseMarket("forex"), "forex");
  assert.equal(inst.parseMarket("crypto", "forex"), "crypto");
  assert.equal(inst.parseMarket("stocks"), "crypto");
  assert.equal(inst.parseMarket(undefined, "forex"), "forex");
  assert.equal(inst.parseMarket(42), "crypto");
  assert.equal(inst.isMarket("forex"), true);
  assert.equal(inst.isMarket("Forex"), false);
});
