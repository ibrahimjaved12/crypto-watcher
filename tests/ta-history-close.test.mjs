import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

test("TA history exposes candle close and expands across all nine columns", async () => {
  const source = await readFile(new URL("../src/components/market/technical-analysis.tsx", import.meta.url), "utf8");
  const headers = source.split("const HEADERS:")[1]?.split("type HistoryRowData")[0];
  assert.ok(headers, "history headers exist");
  assert.match(headers, /"Candle close"/);
  assert.match(source, /colSpan=\{9\}/);
  assert.match(source, /queryKey: \["watchlist"\], queryFn: fetchWatchlist[\s\S]*const defaultSymbol = watchlist\.data\?\.\[0\]\?\.symbol \?\? "BTCUSDT";[\s\S]*useState\(defaultSymbol\)[\s\S]*if \(!symbolTouched\) setSymbol\(defaultSymbol\);[\s\S]*setSymbolTouched\(true\);\s*setSymbol\(e\.target\.value\);/);
});
