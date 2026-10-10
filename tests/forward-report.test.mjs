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
    exit_days: n, cluster_sum_sq_ur: n ? sq - sum * sum / n : 0,
    placebo_exit_days: pn, placebo_cluster_sum_sq_ur: pn ? psq - psum * psum / pn : 0,
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

// Paired-by-exit-day helpers. `fromDays` builds a consistent report row AND the paired row from per-day
// trade lists (R units, index = UTC exit day), with an independent JS implementation of
// r_d = (s_d - n_d*S/n)/n - (c_d - m_d*C/m)/m  and  paired_ss = sum_d r_d^2 (micro-R squared).
const ID = "rsi_14_reversion:60";
function fromDays(realDays, ctlDays) {
  const sum = (list) => list.reduce((a, b) => a + b, 0);
  const flat = (days) => Object.values(days).flat();
  const [rt, ct] = [flat(realDays), flat(ctlDays)];
  const n = rt.length, m = ct.length, S = sum(rt) * R, C = sum(ct) * R;
  const dayStat = (days, key) => [(days[key] ?? []).length, sum(days[key] ?? []) * R];
  const clusters = (days, tot, cnt) => Object.keys(days).reduce((a, key) => {
    const [nd, sd] = dayStat(days, key);
    return a + (sd - nd * tot / cnt) ** 2;
  }, 0);
  const keys = [...new Set([...Object.keys(realDays), ...Object.keys(ctlDays)])];
  let ss = 0, both = 0;
  for (const key of keys) {
    const [nd, sd] = dayStat(realDays, key), [md, cd] = dayStat(ctlDays, key);
    const r = (sd - nd * S / n) / n - (cd - md * C / m) / m;
    ss += r * r;
    if (nd && md) both += 1;
  }
  const squares = (list) => list.reduce((a, x) => a + (x * R) ** 2, 0);
  return {
    row: {
      strategy_id: ID, version: "ta-v1", horizon_min: 60, rr: "2", n, n_t: 1, n_s: 1, n_e: 0, n_l: 0, n_x: 0, n_ambiguous: 0,
      sum_net_ur: S, sum_sq_net_ur: squares(rt), sum_cost_ur: 0, sum_fund_ur: 0, first_exit_ms: 1, last_exit_ms: 2,
      exit_days: Object.keys(realDays).length, cluster_sum_sq_ur: clusters(realDays, S, n),
      placebo_exit_days: Object.keys(ctlDays).length, placebo_cluster_sum_sq_ur: clusters(ctlDays, C, m),
      placebo_n: m, placebo_sum_net_ur: C, placebo_sum_sq_net_ur: squares(ct),
    },
    paired: { strategy_id: ID, version: "ta-v1", horizon_min: 60, rr: "2", paired_days: both, union_days: keys.length, paired_ss: ss },
  };
}
const spread = (count, from, value) => Object.fromEntries(Array.from({ length: count }, (_, i) => [from + i, value(i)]));

test("hand-computed 4-day example with uneven counts and a day where the control has no trade", () => {
  const { row: r4, paired } = fromDays(
    { 1: [1, 3], 2: [-2], 3: [2, 2, 0] },   // n = 6, S = 6 R, mean 1
    { 1: [1], 2: [-1, -1], 4: [2] },        // m = 4, C = 1 R, mean 0.25; no control trade on day 3
  );
  assert.equal(paired.paired_days, 2, "only days 1 and 2 have both series");
  assert.equal(paired.union_days, 4);
  // Day residuals r_d: 7/48, 1/8, 1/6 (control absent, contributes 0) and -7/16 (strategy absent); sum r_d^2 = 295/1152 R^2.
  assert.ok(Math.abs(paired.paired_ss / (R * R) - 295 / 1152) < 1e-12);
  const [built] = report.buildReportRows([r4], [], [paired]);
  assert.ok(Math.abs(built.diffR - 0.75) < 1e-12);
  assert.equal(built.pairedDays, 2);
  assert.ok(Math.abs(built.diffSe - Math.sqrt(295 / 1152)) < 1e-12);
  assert.ok(Math.abs(built.z - 0.75 / Math.sqrt(295 / 1152)) < 1e-12);
  assert.equal(built.verdict.kind, "too_few");
});

test("the paired difference against the matched control and the 30-trade rule", () => {
  // 30 days, one trade each: strategy alternates 0 R / 2 R, control alternates +1 R / -1 R in the opposite phase.
  const days = (a, b) => spread(30, 0, (i) => [i % 2 ? b : a]);
  const { row: full, paired } = fromDays(days(0, 2), days(1, -1));
  const [built] = report.buildReportRows([full], [], [paired]);
  // r_d = (x_d - 1)/30 - y_d/30 = -2/30 on every day, so SE(D) = sqrt(30 * (2/30)^2).
  assert.ok(Math.abs(built.diffSe - Math.sqrt(30 * (2 / 30) ** 2)) < 1e-9);
  assert.ok(Math.abs(built.z - 1 / built.diffSe) < 1e-9);
  assert.equal(built.verdict.kind, "better");
  assert.match(built.verdict.text, /paired by exit day; Bonferroni/);
  const { row: few, paired: fewPaired } = fromDays(spread(29, 0, (i) => [i % 2 ? 2 : 0]), days(1, -1));
  assert.equal(report.buildReportRows([few], [], [fewPaired])[0].verdict.text, "too few trades", "29 trades are never judged");
});

test("perfectly correlated strategy and control: the paired SE collapses while the unpaired SE does not", () => {
  const x = (i) => ((i * 7) % 11) - 5;                       // varied daily results
  const { row: corr, paired } = fromDays(spread(40, 0, (i) => [x(i)]), spread(40, 0, (i) => [x(i) - 0.5]));
  const [built] = report.buildReportRows([corr], [], [paired]);
  const unpaired = Math.sqrt(built.mean.se ** 2 + built.placebo.se ** 2);
  assert.ok(unpaired > 0.4, `the independent estimate stays large (${unpaired})`);
  assert.ok(Math.abs(built.diffSe) < 1e-9, "r_d is 0 on every day: the control moves with the strategy");
  assert.equal(built.z, null, "no variation in the difference: no z, no verdict of edge");
  assert.equal(built.verdict.kind, "no_spread");
  assert.ok(Math.abs(built.diffR - 0.5) < 1e-12);
});

test("fewer than 10 paired days gives no verdict even with 30 trades and 10 days in each series", () => {
  const strategy = spread(40, 0, (i) => [i % 2 ? 1 : -1]);
  const [nine] = (() => { const o = fromDays(strategy, spread(30, 31, (i) => [i % 2 ? 1 : -1])); return [report.buildReportRows([o.row], [], [o.paired])[0], o]; })();
  assert.equal(nine.pairedDays, 9, "control days 31..60 overlap strategy days 0..39 on 9 days");
  assert.equal(nine.verdict.kind, "too_few");
  const ten = fromDays(strategy, spread(30, 30, (i) => [i % 2 ? 1 : -1]));
  const built = report.buildReportRows([ten.row], [], [ten.paired])[0];
  assert.equal(built.pairedDays, 10);
  assert.notEqual(built.verdict.kind, "too_few");
});

test("verdict kinds: no baseline, no spread, no difference, worse", () => {
  const verdict = (real, ctl) => { const o = fromDays(real, ctl); return report.buildReportRows([o.row], [], [o.paired])[0]; };
  const alt = (a, b) => spread(30, 0, (i) => [i % 2 ? b : a]);
  assert.equal(report.buildReportRows([fromDays(alt(0, 2), {}).row])[0].verdict.kind, "no_baseline");
  assert.equal(verdict(alt(1, 1), alt(0, 0)).verdict.kind, "no_spread");
  assert.equal(verdict(alt(-1, 1), alt(1, -1)).verdict.kind, "no_difference", "mean difference 0");
  assert.equal(verdict(alt(-2, 0), alt(1, -1)).verdict.kind, "worse");
});

test("the Bonferroni threshold scales with the number of strategy rows shown", () => {
  assert.ok(Math.abs(report.normalQuantile(0.975) - 1.959964) < 1e-6);
  assert.ok(Math.abs(report.bonferroniCritical(1) - 1.959964) < 1e-6);
  assert.ok(Math.abs(report.bonferroniCritical(10) - 2.807034) < 1e-6);
  // Strategy 1.5 / 0.5 R and control -0.5 / +0.5 R in phase: r_d = +-1/30, SE(D) = sqrt(30)/30, z = 5.48.
  const parts = Array.from({ length: 10 }, (_, i) => {
    const o = fromDays(spread(30, 0, (d) => [d % 2 ? 0.5 : 1.5]), spread(30, 0, (d) => [d % 2 ? 0.5 : -0.5]));
    return { row: { ...o.row, strategy_id: `macd_12_26_9:${60 + i}` }, paired: { ...o.paired, strategy_id: `macd_12_26_9:${60 + i}` } };
  });
  const built = report.buildReportRows(parts.map((p) => p.row), [], parts.map((p) => p.paired));
  assert.ok(Math.abs(built[0].z - 1 / (Math.sqrt(30) / 30)) < 1e-9);
  assert.match(built[0].verdict.text, /threshold for 10 rows: z ≥ 2\.81/);
  assert.equal(built[0].clearsBonferroni, true, "z = 5.48 clears 2.81");
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

test("day-clustered SE: three uneven exit days, hand-computed residuals", () => {
  // Real days: [1,3], [-2], [2,2,0]. n=6, sum=6, mean=1 R.
  // Daily residuals s_d - n_d*mean: 2, -3, 1; squares sum to 14.
  // Control days: [-1,1], [-2], [0,0,2]. mean=0, residuals 0,-2,2; squares sum to 8.
  const input = { ...row({ n: 6, a: 0, b: 2, pn: 6 }), sum_net_ur: 6*R, sum_sq_net_ur: 22*R*R,
    exit_days: 3, cluster_sum_sq_ur: 14*R*R, placebo_exit_days: 3,
    placebo_sum_net_ur: 0, placebo_sum_sq_net_ur: 10*R*R, placebo_cluster_sum_sq_ur: 8*R*R };
  const [r] = report.buildReportRows([input]);
  assert.equal(r.mean.mean, 1);
  assert.ok(Math.abs(r.mean.se - Math.sqrt(14)/6) < 1e-12);
  assert.ok(Math.abs(r.placebo.se - Math.sqrt(8)/6) < 1e-12);
  assert.equal(r.z, null, "no paired sums loaded: no z (the independent Welch SE is not used)");
  assert.ok(Math.abs(r.naiveSe - Math.sqrt(16/30)) < 1e-12);
  assert.equal(r.verdict.kind, "too_few");
  const csv = report.reportCsv([r]);
  assert.match(csv.split("\n")[0], /Trades,Days,Mean net R,Day-clustered SE,naive SE/);
  assert.match(csv.split("\n")[0], /Control trades,Control days/);
  assert.match(csv.split("\n")[0], /Difference R,Paired days,Paired SE of difference,Paired z,Verdict/);
});

test("ten UTC exit days are required for both series, independently of trade count", () => {
  const build = (realDays, ctlDays) => {
    const per = (days) => Math.ceil(300 / days);
    const make = (days, base) => spread(days, 0, (i) => Array.from({ length: per(days) }, (_, j) => ((i + j) % 2 ? base + 1 : base - 1)));
    const o = fromDays(make(realDays, 1), make(ctlDays, 0));
    return report.buildReportRows([o.row], [], [o.paired])[0];
  };
  assert.equal(build(9, 10).verdict.kind, "too_few");
  assert.equal(build(10, 9).verdict.kind, "too_few");
  assert.notEqual(build(10, 10).verdict.kind, "too_few");
  const o = fromDays(spread(30, 0, (d) => [d % 2 ? 0.5 : 1.5]), spread(30, 0, (d) => [d % 2 ? 0.5 : -0.5]));
  assert.equal(report.buildReportRows([o.row], [], [{ ...o.paired, paired_ss: null }])[0].z, null);
  assert.equal(report.verdictFor(30, 2.5, true, 10, 10, 10, 30).kind, "no_difference");
});
