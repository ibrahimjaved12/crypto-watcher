import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

const source = await readFile(new URL("../src/lib/forward/mvp-check.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const m = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
const DAY = 86_400_000;
const HOUR = 3_600_000;

test("history depth and gap thresholds", () => {
  assert.deepEqual([59, 60, 119, 120].map(m.classifyHistoryDays), ["FAIL", "WARN", "WARN", "PASS"]);
  assert.deepEqual([0, 1, 120, 121].map(m.classifyMissing120), ["PASS", "WARN", "WARN", "FAIL"]);
  assert.deepEqual([0, 1].map(m.classifyMissingRecent), ["PASS", "FAIL"]);
  assert.deepEqual([HOUR, 2 * HOUR, 5 * HOUR, 6 * HOUR].map(m.classifyRunAge), ["PASS", "WARN", "WARN", "FAIL"]);
  assert.equal(m.historyDays(null, 10 * DAY), 0);
  assert.equal(m.historyDays(0, 500 * DAY), 120, "saturates at the 120-day window");
});

test("missing minutes are counted inside the window only", () => {
  const boundary = 10 * HOUR;
  const opens = [];
  for (let t = 6 * HOUR; t < boundary; t += 60_000) if (t !== 8 * HOUR && t !== 8 * HOUR + 60_000) opens.push(t);
  opens.unshift(0); // an older candle outside the window is ignored
  assert.equal(m.missingMinutesIn(opens, 6 * HOUR, boundary), 2);
  assert.equal(m.missingMinutesIn(opens, 9 * HOUR, boundary), 0, "the gap is before the 3 h window");
});

test("ledger invariant: balance reconciles and seq is contiguous from 1", () => {
  const ok = { n: 3, max_seq: 3, sum_amount_e8: -250, last_balance_e8: 100e8 - 250 };
  assert.equal(m.ledgerInvariant(100e8, ok).ok, true);
  assert.equal(m.ledgerInvariant(100e8, null).ok, true, "an empty ledger is fine");
  assert.match(m.ledgerInvariant(100e8, { ...ok, max_seq: 4 }).detail, /not contiguous/);
  assert.match(m.ledgerInvariant(100e8, { ...ok, last_balance_e8: 100e8 }).detail, /last balance/);
});

test("no_sigma counts and placebo coverage", () => {
  const reasons = { BTCUSDT: { sigma: "no_sigma: 3 signal(s) before x" }, ETHUSDT: { sigma: "no_sigma: 2 signal(s) before x" }, XRPUSDT: {} };
  assert.equal(m.noSigmaSignals(reasons), 5);
  assert.equal(m.noSigmaSignals(null), 0);
  assert.deepEqual(m.strategiesWithoutPlacebo(["a:15", "placebo-v1:a:15", "b:60", "placebo-v1:c:15"]), ["b:60"]);
});

test("the latest run decides PASS, WARN or FAIL and shows the request size and a failed backfill", () => {
  const now = 100 * HOUR;
  assert.equal(m.checkLatestRun(null, now)[0].level, "FAIL");
  const fine = m.checkLatestRun({ status: "ok", reason: null, boundary_ms: now - HOUR,
    freshness: { request: { rows: 1000, python_ms: 1500 } } }, now);
  assert.deepEqual(fine.map((c) => c.level), ["PASS", "PASS", "PASS"]);
  const stale = m.checkLatestRun({ status: "skipped_stale", reason: "BTCUSDT: 1 gap(s); backfill_failed: BTCUSDT: HTTP 429", boundary_ms: now - 7 * HOUR }, now);
  assert.deepEqual(stale.map((c) => c.level), ["FAIL", "FAIL", "WARN"]);
  assert.equal(m.exitCodeFor(stale), 1);
  assert.equal(m.exitCodeFor(fine), 0);
  assert.equal(m.exitCodeFor([{ name: "x", level: "WARN", detail: "" }]), 0, "a WARN never fails the check");
});
