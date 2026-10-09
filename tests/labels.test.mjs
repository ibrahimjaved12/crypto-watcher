import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

async function moduleUrl(path) {
  const source = await readFile(new URL(path, import.meta.url), "utf8");
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  });
  return `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`;
}

const labels = await import(await moduleUrl("../src/lib/labels.ts"));

const MACHINE = [
  "skipped_stale", "funding_unavailable", "collector missing", "no candles", "ok", "success", "failed",
  "partial", "skipped", "already_done", "provider_timeout", "provider_unavailable", "invalid_or_stale_data",
  "invalid_data", "stale_data", "insufficient_history", "insufficient", "unavailable", "stale",
  "SOLUSDT: collector missing; SOLUSDT: no candles", "funding_unavailable: BTCUSDT,ETHUSDT",
  "TA catch-up gap exceeds the available 260-candle history",
  'new row violates check constraint "ta_signals_futures_provenance_check"',
];

test("every known machine string maps to a non-empty plain sentence", () => {
  for (const raw of MACHINE) {
    const reason = labels.humanizeReason(raw);
    assert.ok(reason.short.length > 0, raw);
    assert.doesNotMatch(reason.short, /_/, raw);  // no snake_case leaks into the default view
  }
  assert.match(labels.humanizeReason("skipped_stale").short, /too old to trust/);
  assert.match(labels.humanizeReason("funding_unavailable: BTCUSDT").short, /Funding rates/);
  assert.match(labels.humanizeReason("TA catch-up gap exceeds the available 260-candle history").short, /catch up/);
});

test("unknown values fall back without throwing", () => {
  assert.equal(labels.humanizeReason("some_new_reason").short, "Some new reason");
  for (const value of [null, undefined, "", "   ", "x".repeat(500)]) {
    assert.doesNotThrow(() => labels.humanizeReason(value));
  }
  assert.equal(labels.strategyLabel("brand_new_strategy").name, "Brand new strategy");
  assert.equal(labels.outcomeLabel("Q").short, "Q");
});

test("strategyLabel covers every strategy id the code emits", async () => {
  const deps = await readFile(new URL("../src/lib/forward/forward-deps.server.ts", import.meta.url), "utf8");
  const ta = JSON.parse(/const TA_STRATEGIES = (\[[^\]]*\])/.exec(deps)[1].replace(/\s+/g, "").replace(/,\]$/, "]"));
  const python = await readFile(new URL("../python/market_analysis/forward/trend_track.py", import.meta.url), "utf8")
    .catch(() => "");
  const trend = ["ens_ls_25", "ens_lo_25", "ens_ls_15", "ens_ls_25_sub3", "tsmom_7", "tsmom_14", "tsmom_28",
    "ens_ls_25_mfilter", "ens_ls_25_rvfilter", "bh_vt_std90_25", "bh_vt_std90_15", "bh_vt_ewma60_25", "ew_long"];
  if (python) assert.match(python, /ew_long/);
  for (const name of ta) {
    for (const minutes of ["15", "60", "240"]) {
      for (const id of [`${name}:${minutes}`, `placebo-v1:${name}:${minutes}`]) {
        const info = labels.strategyLabel(id);
        assert.doesNotMatch(info.name, /_/, id);
        assert.ok(info.blurb.length > 0, id);
      }
    }
  }
  for (const id of trend) {
    const info = labels.strategyLabel(id);
    assert.doesNotMatch(info.name, /_/, id);
    assert.ok(info.blurb.length > 0, id);
  }
  assert.match(labels.strategyLabel("placebo-v1:rsi_14_reversion:15").name, /Random baseline/);
});

test("outcome codes and glossary terms", () => {
  for (const code of ["T", "S", "E", "L", "X", "ambiguous"]) assert.ok(labels.outcomeLabel(code).short.length > 3);
  assert.match(labels.outcomeLabel("ambiguous").short, /same candle/);
  for (const key of ["R", "ATR", "RSI", "EMA", "funding", "placebo", "scoreBand", "turnover", "volTarget",
    "live", "backfilled", "mde"]) {
    assert.ok(labels.GLOSSARY[key].plain.length > 20, key);
  }
  assert.match(labels.GLOSSARY.score.plain, /not a win probability/);
});
