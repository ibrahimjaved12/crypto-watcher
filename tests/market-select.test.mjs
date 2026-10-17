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

test("resolveMarket keeps an openable stored market, else the first open one, else crypto", () => {
  for (const stored of [null, undefined, "", "stocks", 42, {}, ["forex"], "FOREX "])
    assert.equal(sel.resolveMarket(stored, ["crypto"]), "crypto", String(stored));
  assert.equal(sel.resolveMarket("forex", ["crypto"]), "crypto");
  assert.equal(sel.resolveMarket("crypto", ["forex"]), "forex");
  assert.equal(sel.resolveMarket(null, ["forex"]), "forex");
  assert.equal(sel.resolveMarket("forex", []), "crypto");
  assert.equal(sel.resolveMarket("forex", ["crypto", "forex"]), "forex");
});

test("canOpen: only markets the label lists, crypto included", () => {
  assert.equal(sel.canOpen("crypto", []), false);
  assert.equal(sel.canOpen("crypto", ["forex"]), false);
  assert.equal(sel.canOpen("crypto", ["crypto"]), true);
  assert.equal(sel.canOpen("forex", ["crypto"]), false);
  assert.equal(sel.canOpen("forex", ["forex"]), true);
});

test("normalizeEntitled keeps known markets once, in MARKETS order; an unknown shape means no access", () => {
  assert.deepEqual(sel.normalizeEntitled(null), []);
  assert.deepEqual(sel.normalizeEntitled(undefined), []);
  assert.deepEqual(sel.normalizeEntitled("forex"), []);
  assert.deepEqual(sel.normalizeEntitled({ forex: true }), []);
  assert.deepEqual(sel.normalizeEntitled([]), []);
  assert.deepEqual(sel.normalizeEntitled(["forex"]), ["forex"]);
  assert.deepEqual(sel.normalizeEntitled(["stocks", 7, null, "forex"]), ["forex"]);
  assert.deepEqual(sel.normalizeEntitled(["forex", "crypto", "forex", "crypto"]), ["crypto", "forex"]);
});

test("accessState maps the query status and the list", () => {
  assert.equal(sel.accessState("loading", []), "loading");
  assert.equal(sel.accessState("error", ["crypto"]), "error");
  assert.equal(sel.accessState("ready", []), "none");
  assert.equal(sel.accessState("ready", ["forex"]), "granted");
});

test("MARKET_META describes both markets", () => {
  assert.deepEqual(Object.keys(sel.MARKET_META).sort(), ["crypto", "forex"]);
  assert.equal(sel.MARKET_META.crypto.label, "Crypto");
  assert.equal(sel.MARKET_META.forex.label, "Forex");
  assert.equal(sel.MARKET_STORAGE_KEY, "cw.market");
});
