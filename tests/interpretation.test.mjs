import assert from "node:assert/strict";
import { test } from "node:test";
import { readFile } from "node:fs/promises";
import ts from "../node_modules/typescript/lib/typescript.js";
const source = await readFile(new URL("../src/lib/ta/interpretation.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { interpretTA } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`
);
const base = { ema20: 100, ema50: 90, rsi14: 60, atr14: 2, volume_change_pct: 20 };
test("aligned directional evidence yields bounded, transparent scores", () => {
  const up = interpretTA(110, { ...base, rsi14: 80 }, [
    "hammer",
    "bullish_engulfing",
    "volume_spike",
  ]);
  assert.equal(up.score, 100);
  assert.equal(up.support, "Aligned + volume supported");
  assert.equal(up.atrPct, (2 / 110) * 100);
  const down = interpretTA(80, { ...base, ema50: 110, rsi14: 20 }, ["shooting_star"]);
  assert.equal(down.score, -100);
  assert.equal(down.momentum, "Oversold");
  assert.equal(
    Object.values(down.contributions).reduce((a, b) => a + b),
    down.score,
  );
});
test("RSI boundaries and mixed EMA ordering", () => {
  for (const [rsi, label] of [
    [0, "Oversold"],
    [30, "Weak"],
    [45, "Neutral"],
    [55, "Neutral"],
    [55.1, "Strong"],
    [70, "Strong"],
    [100, "Overbought"],
  ]) {
    assert.equal(interpretTA(100, { ...base, rsi14: rsi }, []).momentum, label);
  }
  assert.equal(interpretTA(95, base, []).trend, "Neutral");
  assert.equal(interpretTA(100, base, []).trend, "Neutral");
});
test("countertrend, low volume, and conflicting patterns cannot gain volume support", () => {
  const low = interpretTA(110, { ...base, volume_change_pct: -80 }, ["hammer"]);
  assert.equal(low.contributions.volume, 0);
  assert.match(low.support, /below-average/);
  const against = interpretTA(80, { ...base, ema50: 110 }, ["hammer"]);
  assert.equal(against.contributions.volume, 0);
  assert.match(against.support, /Against/);
  const mixed = interpretTA(110, base, ["hammer", "shooting_star"]);
  assert.equal(mixed.contributions.patterns, 0);
  assert.equal(mixed.contributions.volume, 0);
  assert.equal(mixed.support, "Conflicting directions");
  assert.equal(interpretTA(110, base, ["doji", "volume_spike"]).contributions.patterns, 0);
});
test("missing and invalid data are unavailable instead of neutral or zero-filled", () => {
  for (const raw of [
    null,
    [],
    {},
    { ...base, rsi14: NaN },
    { ...base, rsi14: 101 },
    { ...base, volume_change_pct: null },
    { ...base, ema20: -1 },
  ]) {
    assert.equal(interpretTA(110, raw, []).score, null);
  }
  assert.equal(interpretTA(0, base, []).atrPct, null);
  assert.equal(interpretTA(110, { ...base, atr14: Infinity }, []).atrPct, null);
  assert.equal(
    interpretTA(110, { ...base, volume_change_pct: 0 }, ["hammer"]).contributions.volume,
    20,
  );
});
