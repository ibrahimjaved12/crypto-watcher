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
const instrumentsUrl = await moduleUrl("../src/lib/market/instruments.ts", { "./symbols": symbolsUrl });
const sel = await import(await moduleUrl("../src/lib/market/market-select.ts", { "./instruments": instrumentsUrl }));

test("resolveMarket falls back to crypto unless the stored market is known and entitled", () => {
  for (const stored of [null, undefined, "", "stocks", 42, {}, ["forex"], "FOREX "])
    assert.equal(sel.resolveMarket(stored, ["crypto"]), "crypto", String(stored));
  assert.equal(sel.resolveMarket("forex", ["crypto"]), "crypto");
  assert.equal(sel.resolveMarket("forex", []), "crypto");
  assert.equal(sel.resolveMarket("forex", ["crypto", "forex"]), "forex");
  assert.equal(sel.resolveMarket("forex", ["forex"]), "forex");
  assert.equal(sel.resolveMarket("crypto", ["forex"]), "crypto");
});

test("canOpen: crypto always, forex only when listed", () => {
  assert.equal(sel.canOpen("crypto", []), true);
  assert.equal(sel.canOpen("crypto", ["forex"]), true);
  assert.equal(sel.canOpen("forex", ["crypto"]), false);
  assert.equal(sel.canOpen("forex", ["forex"]), true);
});

test("normalizeEntitled keeps known markets once, in MARKETS order, and never adds crypto by itself", () => {
  assert.deepEqual(sel.normalizeEntitled(null), ["crypto"]);
  assert.deepEqual(sel.normalizeEntitled(undefined), ["crypto"]);
  assert.deepEqual(sel.normalizeEntitled("forex"), ["crypto"]);
  assert.deepEqual(sel.normalizeEntitled({ forex: true }), ["crypto"]);
  assert.deepEqual(sel.normalizeEntitled([]), []);
  assert.deepEqual(sel.normalizeEntitled(["forex"]), ["forex"]);
  assert.deepEqual(sel.normalizeEntitled(["stocks", 7, null, "forex"]), ["forex"]);
  assert.deepEqual(sel.normalizeEntitled(["forex", "crypto", "forex", "crypto"]), ["crypto", "forex"]);
  assert.equal(sel.canOpen("crypto", sel.normalizeEntitled([])), true);
});

test("MARKET_META describes both markets", () => {
  assert.deepEqual(Object.keys(sel.MARKET_META).sort(), ["crypto", "forex"]);
  assert.equal(sel.MARKET_META.crypto.label, "Crypto");
  assert.equal(sel.MARKET_META.forex.label, "Forex");
  assert.equal(sel.MARKET_META.crypto.icon, "bitcoin");
  assert.equal(sel.MARKET_META.forex.icon, "globe");
  assert.equal(sel.MARKET_STORAGE_KEY, "cw.market");
});
