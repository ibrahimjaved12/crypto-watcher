import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

async function load(path) {
  const source = await readFile(new URL(path, import.meta.url), "utf8");
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  });
  return import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
}
const report = await load("../src/lib/forward/forward-report.ts");
const R = 1e6;

// n trades alternating between two values (in R): exact sums for the report row.
function row({ n, a, b, pn = 0, pa = 0, pb = 0, id = "rsi_14_reversion:60", rr = "2" }) {
  const sums = (count, x, y) => {
    const hi = Math.floor(count / 2);
    const lo = count - hi;
    return [lo * x * R + hi * y * R, lo * (x * R) ** 2 + hi * (y * R) ** 2];
  };
  const [sum, sq] = sums(n, a, b);
  const [psum, psq] = sums(pn, pa, pb);
  return {
    strategy_id: id, version: "ta-v1", horizon_min: 60, rr, n, n_t: 1, n_s: 1, n_e: 0, n_l: 0, n_x: 0, n_ambiguous: 0,
    sum_net_ur: sum, sum_sq_net_ur: sq, sum_cost_ur: 0, sum_fund_ur: 0, first_exit_ms: 1, last_exit_ms: 2,
    placebo_n: pn, placebo_sum_net_ur: psum, placebo_sum_sq_net_ur: psq,
  };
}

test("mean and standard error come from n, the sum and the sum of squares", () => {
  const m = report.moments(2, 4 * R, 10 * R * R); // values 1 R and 3 R
  assert.equal(m.mean, 2);
  assert.ok(Math.abs(m.se - 1) < 1e-12);
  assert.equal(report.moments(1, R, R * R).se, null);
  assert.deepEqual(report.moments(0, 0, 0), { n: 0, mean: null, se: null });
  assert.equal(report.moments(3, 3 * R, 3 * R * R * (1 - 1e-16)).se, 0, "rounding never gives a NaN");
});

test("the Welch difference against the matched control and the 30-trade rule", () => {
  // 30 trades 0 R / 2 R (mean 1 R) against 30 control trades -1 R / +1 R (mean 0 R).
  const [full] = report.buildReportRows([row({ n: 30, a: 0, b: 2, pn: 30, pa: -1, pb: 1 })]);
  const se = Math.sqrt(1 / 29 / 1) ; // sample variance 30/29 per trade, se of each mean = sqrt(1/29)
  assert.ok(Math.abs(full.mean.mean - 1) < 1e-12 && Math.abs(full.placebo.mean) < 1e-12);
  assert.ok(Math.abs(full.diffSe - Math.sqrt(2) * se) < 1e-9);
  assert.ok(Math.abs(full.z - 1 / (Math.sqrt(2) * se)) < 1e-9);
  assert.equal(full.verdict.kind, "better");
  assert.match(full.verdict.text, /unadjusted for K/);
  const [few] = report.buildReportRows([row({ n: 29, a: 0, b: 2, pn: 30, pa: -1, pb: 1 })]);
  assert.equal(few.verdict.text, "too few trades", "29 trades are never judged");
});

test("verdict kinds: no baseline, no spread, no difference, worse", () => {
  assert.equal(report.buildReportRows([row({ n: 30, a: 0, b: 2 })])[0].verdict.kind, "no_baseline");
  const flat = report.buildReportRows([row({ n: 30, a: 1, b: 1, pn: 30, pa: 0, pb: 0 })])[0];
  assert.equal(flat.verdict.kind, "no_spread");
  assert.equal(flat.z, null);
  assert.equal(report.buildReportRows([row({ n: 30, a: -1, b: 1, pn: 30, pa: -1, pb: 1 })])[0].verdict.kind, "no_difference");
  assert.equal(report.buildReportRows([row({ n: 30, a: -2, b: 0, pn: 30, pa: -1, pb: 1 })])[0].verdict.kind, "worse");
});

test("the Bonferroni threshold scales with the number of strategy rows shown", () => {
  assert.ok(Math.abs(report.normalQuantile(0.975) - 1.959964) < 1e-6);
  assert.ok(Math.abs(report.bonferroniCritical(1) - 1.959964) < 1e-6);
  assert.ok(Math.abs(report.bonferroniCritical(10) - 2.807034) < 1e-6);
  const rows = Array.from({ length: 10 }, (_, i) =>
    row({ n: 30, a: 0, b: 2, pn: 30, pa: -1, pb: 1, id: `macd_12_26_9:${60 + i}` }));
  const built = report.buildReportRows(rows);
  assert.match(built[0].verdict.text, /threshold for 10 rows: z > 2\.81/);
  assert.equal(built[0].clearsBonferroni, true, "z = 3.8 > 2.81");
});

test("non-trades join their strategy row and unmatched ones still show", () => {
  const nonTrades = [
    { strategy_id: "rsi_14_reversion:60", horizon_min: 60, rr: "2", n_v: 2, n_c: 1, n_p: 0, n_g: 3, n_n: 0, pending_or_open: 4 },
    { strategy_id: "macd_12_26_9:15", horizon_min: 15, rr: "2", n_v: 5, n_c: 0, n_p: 0, n_g: 0, n_n: 0, pending_or_open: 0 },
  ];
  const rows = report.buildReportRows([row({ n: 30, a: 0, b: 2 })], nonTrades);
  assert.equal(rows.length, 2);
  assert.deepEqual(rows[0].nonTrades, { vetoed: 2, cost: 1, position: 0, gap: 3, notEntered: 0, pending: 4 });
  assert.equal(rows[1].n, 0);
  assert.equal(rows[1].nonTrades.vetoed, 5);
});

test("max drawdown is peak to trough", () => {
  const dd = report.maxDrawdown([100, 120, 90, 130, 100].map((balance, ms) => ({ ms, balance })));
  assert.deepEqual([dd.usdt, dd.fraction], [30, 0.25]);
  assert.equal(report.maxDrawdown([{ ms: 0, balance: 1 }]), null);
});

test("CSV cells are quoted and formula prefixes neutralised, numbers keep their sign", () => {
  assert.equal(report.csvCell('say "hi", ok'), '"say ""hi"", ok"');
  for (const risky of ["=1+1", "+1", "-1", "@SUM(A1)"]) assert.equal(report.csvCell(risky), `'${risky}`);
  assert.equal(report.csvCell(-5), "-5");
  assert.equal(report.csvCell(null), "");
  assert.equal(report.toCsv(["a", "b"], [["x", "=1"], [1, null]]), "a,b\nx,'=1\n1,\n");
});
