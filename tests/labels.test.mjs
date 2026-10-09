import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

const source = await readFile(new URL("../src/lib/labels.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { humanizeReason, strategyLabel, outcomeLabel } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`
);

test("machine reasons have readable labels, including embedded run errors", () => {
  for (const reason of [
    "skipped_stale",
    "funding_unavailable",
    "collector missing",
    "no candles",
    "provider_timeout",
    "provider_unavailable",
    "invalid_or_stale_data",
    "invalid_data",
    "stale_data",
    "insufficient_history",
    "ok",
    "insufficient",
    "unavailable",
    "stale",
    "success",
    "failed",
    "partial",
    "skipped",
  ]) {
    assert.ok(humanizeReason(reason).short.length > 0, reason);
    assert.ok(!humanizeReason(reason).short.includes("_"), reason);
    assert.ok(humanizeReason(reason).level, reason);
  }
  assert.match(
    humanizeReason("SOLUSDT: collector missing; SOLUSDT: no candles").short,
    /collector/,
  );
  assert.match(
    humanizeReason("TA catch-up gap exceeds the available 260-candle history").help,
    /recover/,
  );
  assert.match(
    humanizeReason("ta_signals_futures_provenance_check").short,
    /internal consistency check/,
  );
});

test("unknown and empty reasons are safe, deterministic fallbacks", () => {
  assert.equal(humanizeReason("some_new_reason").short, "Some new reason");
  for (const value of [null, undefined, "", "constructor", "__proto__"])
    assert.ok(humanizeReason(value).short);
  assert.equal(strategyLabel("new_strategy").name, "New strategy");
  for (const code of ["T", "S", "E", "L", "X", "ambiguous"])
    assert.ok(outcomeLabel(code).length > code.length);
});

test("labels cover the emitted indicator and trend strategy registries", async () => {
  const ta = await readFile(
    new URL("../python/market_analysis/benchmark/ta_strategies.py", import.meta.url),
    "utf8",
  );
  const trend = await readFile(
    new URL("../python/market_analysis/benchmark/trend.py", import.meta.url),
    "utf8",
  );
  const taRegistry = ta.split("STRATEGIES = {")[1].split("\n}")[0];
  const ids = [...taRegistry.matchAll(/^\s+"([^"]+)":/gm)].flatMap(([, name]) =>
    [15, 60, 240].flatMap((minutes) => [`${name}:${minutes}`, `placebo-v1:${name}:${minutes}`]),
  );
  const variants = [...trend.matchAll(/TrendVariant\("([^"]+)", "([^"]+)", "0\.(\d+)"/g)];
  ids.push(
    ...variants.map(([, name]) => name),
    "ew_long",
    ...variants.map(
      ([, , family, target]) => `bh_vt_${family === "ens" ? "std90" : "ewma60"}_${target}`,
    ),
  );
  assert.equal(variants.length, 9);
  assert.equal(ids.filter((id) => id.startsWith("placebo-v1:")).length, 18);
  for (const id of new Set(ids)) {
    const label = strategyLabel(id);
    assert.ok(label.name && label.blurb, id);
    assert.ok(!label.blurb.includes("unrecognised"), id);
    assert.ok(!label.name.includes("_"), id);
  }
});
