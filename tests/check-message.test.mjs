import assert from "node:assert/strict";
import { test } from "node:test";
import { createModuleLoader } from "./module-loader.mjs";

const moduleUrl = createModuleLoader(import.meta.url);
const { describeAlertCheck } = await import(await moduleUrl("../src/lib/monitor/check-message.ts"));

const base = {
  status: "success",
  alertsCreated: 0,
  symbolsChecked: 6,
  taChecks: 18,
  taFailures: 0,
  error: null,
};

test("failed checks report no completed analysis and summarize the reasons", () => {
  assert.deepEqual(describeAlertCheck({
    ...base,
    status: "failed",
    taFailures: 18,
    error: "BTCUSDT: provider_timeout | ETHUSDT: provider_timeout | SOLUSDT: insufficient_history",
  }), {
    kind: "error",
    title: "Alert check failed. No analysis completed.",
    description: "The exchange took too long to answer (BTC, ETH); Not enough past candles yet for this timeframe (SOL)",
  });
});

test("partial checks show TA failures without treating zero alerts as an all-clear", () => {
  const message = describeAlertCheck({
    ...base,
    status: "partial",
    taFailures: 18,
    error: "provider_timeout",
  });
  assert.deepEqual(message, {
    kind: "warning",
    title: "18 of 18 technical-analysis checks failed. 0 new alert(s); 0 new alerts is not an all-clear.",
    description: "The exchange took too long to answer",
  });
  assert.doesNotMatch(message.title, /pairs checked/);
});

test("partial checks without TA failures report the alert count and summarized reason", () => {
  const message = describeAlertCheck({
    ...base,
    status: "partial",
    alertsCreated: 2,
    error: "provider_unavailable",
  });
  assert.deepEqual(message, {
    kind: "warning",
    title: "Alert check partly completed. 2 new alert(s).",
    description: "The exchange could not be reached",
  });
  assert.doesNotMatch(message.title, /pairs checked/);
});

test("successful checks confirm every TA check ran", () => {
  assert.deepEqual(describeAlertCheck({ ...base, alertsCreated: 2 }), {
    kind: "success",
    title: "Alert check completed: 6 pairs checked, 2 new alert(s). All 18 technical-analysis checks ran.",
  });
});

test("successful checks with TA disabled omit the TA confirmation", () => {
  assert.deepEqual(describeAlertCheck({ ...base, taChecks: 0 }), {
    kind: "success",
    title: "Alert check completed: 6 pairs checked, 0 new alert(s).",
  });
});

test("skipped checks warn and summarize the reason", () => {
  assert.deepEqual(describeAlertCheck({
    ...base,
    status: "skipped",
    symbolsChecked: 0,
    taChecks: 0,
    error: "A monitor run is already in progress for this account.",
  }), {
    kind: "warning",
    title: "Alert check skipped.",
    description: "A monitor run is already in progress for this account.",
  });
});

test("unsuccessful checks without reasons omit the description", () => {
  for (const status of ["failed", "partial", "skipped"]) {
    const message = describeAlertCheck({ ...base, status });
    assert.equal(message.description, undefined);
  }
});
