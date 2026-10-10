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

const labelsUrl = await moduleUrl("../src/lib/labels.ts");
const labels = await import(labelsUrl);
const status = await import(await moduleUrl("../src/lib/forward/forward-status.ts", { "../labels": labelsUrl }));

const MACHINE = [
  "skipped_stale", "funding_unavailable", "funding_unavailable: BTCUSDT,ETHUSDT", "collector missing",
  "no candles", "ok", "already_done", "provider_timeout", "provider_unavailable", "invalid_or_stale_data",
  "invalid_data", "stale_data", "insufficient_history", "insufficient", "unavailable", "stale", "success",
  "failed", "partial", "skipped", "ta_signals_futures_provenance_check",
  "TA catch-up gap exceeds the available 260-candle history", "last completed minute missing",
  "3 gap(s) in candle history", "collector STALE", "not every track is finalised",
];

test("every known machine string maps to a non-empty plain short text", () => {
  for (const raw of MACHINE) {
    const reason = labels.humanizeReason(raw);
    assert.ok(reason.short.length > 0, raw);
    assert.doesNotMatch(reason.short, /_/, raw);
  }
  assert.match(labels.humanizeReason("skipped_stale").short, /too old to trust/);
  assert.match(labels.humanizeReason("insufficient_history").short, /Not enough past candles/);
});

test("unknown and empty values fall back without throwing", () => {
  assert.equal(labels.humanizeReason("some_new_reason").short, "Some new reason");
  assert.equal(labels.humanizeReason(null).short, "No details");
  assert.equal(labels.humanizeReason(undefined).short, "No details");
  assert.equal(labels.outcomeLabel("Q").short, "Q");
  assert.equal(labels.strategyLabel("brand_new_strategy").name, "Brand new strategy");
  assert.equal(labels.relativeTime("not a date"), "—");
});

test("composite reasons are grouped per message with the affected coins", () => {
  const summary = labels.summarizeReasons("SOLUSDT: collector missing; SOLUSDT: no candles; BTCUSDT: no candles");
  assert.equal(summary.items.length, 2);
  assert.match(summary.items[1], /\(SOL, BTC\)/);
  assert.equal(summary.level, "wait");
});

test("strategyLabel covers every strategy and track id emitted by the code", async () => {
  const deps = await readFile(new URL("../src/lib/forward/forward-deps.server.ts", import.meta.url), "utf8");
  const ta = [...deps.matchAll(/^\s+"([a-z0-9_]+)",$/gm)].map((m) => m[1]);
  assert.equal(ta.length, 6);
  const trend = await readFile(new URL("../python/market_analysis/benchmark/trend.py", import.meta.url), "utf8");
  const variants = [...trend.matchAll(/TrendVariant\("([a-z0-9_]+)"/g)].map((m) => m[1]);
  assert.ok(variants.length >= 9);
  const ids = [
    ...ta.flatMap((name) => [15, 60, 240].flatMap((m) => [`${name}:${m}`, `placebo-v1:${name}:${m}`])),
    ...variants, "bh_vt_std90_25", "bh_vt_std90_15", "bh_vt_ewma60_25", "ew_long",
  ];
  for (const id of ids) {
    const label = labels.strategyLabel(id);
    assert.ok(label.blurb.length > 0, `no blurb for ${id}`);
    assert.doesNotMatch(label.name, /_/, id);
  }
});

test("outcome codes are named", () => {
  for (const code of ["T", "S", "E", "L", "X", "ambiguous"]) assert.ok(labels.outcomeLabel(code).help, code);
});

test("forward status keeps raw machine strings only in technical details", () => {
  const now = 1_760_000_000_000;
  const stale = status.explainSignalEngine(
    { status: "skipped_stale", reason: "SOLUSDT: collector missing; SOLUSDT: no candles", boundary_ms: now },
    now,
  );
  assert.equal(stale.level, "wait");
  assert.doesNotMatch(stale.detail, /skipped_stale|collector missing/);
  assert.match(stale.raw, /skipped_stale/);
  const lagging = status.explainSignalEngine({ status: "skipped_stale", reason: "BTCUSDT: 2 gap(s) in candle history", boundary_ms: now }, now);
  assert.match(lagging.detail, /too old to trust/);
  const funding = status.explainTrendTrack({ status: "ok", reason: "funding_unavailable: BTCUSDT" }, 0, true);
  assert.equal(funding.level, "problem");
  assert.doesNotMatch(funding.detail, /funding_unavailable/);
  assert.equal(status.explainTrendTrack(null, 0, false).level, "wait");
});
