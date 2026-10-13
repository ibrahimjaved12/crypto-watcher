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

test("formatAgo uses compact elapsed time at each display boundary", () => {
  const now = Date.parse("2026-10-13T12:00:00.000Z");
  for (const [age, expected] of [
    [0, "just now"], [44_999, "just now"], [45_000, "1m ago"],
    [12 * 60_000, "12m ago"], [3_599_999, "59m ago"],
    [3_600_000, "1h ago"], [6 * 3_600_000, "6h ago"],
    [6 * 3_600_000 + 9 * 60_000, "6h ago"],
    [6 * 3_600_000 + 10 * 60_000, "6h 10m ago"],
    [6 * 3_600_000 + 20 * 60_000, "6h 20m ago"],
    [86_399_999, "23h 59m ago"], [86_400_000, "1d ago"],
    [3 * 86_400_000, "3d ago"], [-30_000, "just now"],
  ]) assert.equal(labels.formatAgo(now - age, now), expected);
  assert.equal(labels.formatAgo(new Date(now - 12 * 60_000), now), "12m ago");
  assert.equal(labels.formatAgo(new Date(now - 12 * 60_000).toISOString(), now), "12m ago");
  for (const input of [null, undefined, NaN, Infinity, "not a date", new Date(NaN)]) {
    assert.equal(labels.formatAgo(input, now), "—");
  }
});

test("formatClock shows the clock time on the same local day and the full date otherwise", () => {
  const now = new Date(2026, 9, 13, 16, 30, 0).getTime();
  const today = new Date(2026, 9, 13, 16, 25, 15);
  const yesterday = new Date(2026, 9, 12, 23, 59, 59);
  assert.equal(labels.formatClock(today, now), today.toLocaleTimeString());
  assert.equal(labels.formatClock(today.getTime(), now), today.toLocaleTimeString());
  assert.equal(labels.formatClock(today.toISOString(), now), today.toLocaleTimeString());
  assert.equal(labels.formatClock(yesterday, now), yesterday.toLocaleString());
  assert.equal(labels.formatClock(yesterday.toISOString(), now), yesterday.toLocaleString());
  for (const input of [null, undefined, NaN, Infinity, "not a date", new Date(NaN)]) {
    assert.equal(labels.formatClock(input, now), "—");
  }
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
