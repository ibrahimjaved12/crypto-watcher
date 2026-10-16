import assert from "node:assert/strict";
import { test } from "node:test";

import { createModuleLoader } from "./module-loader.mjs";

// #36 Part 3: the pure view models and formatting helpers behind the Paper account page.
const load = createModuleLoader(import.meta.url);
const m = await import(load("../src/lib/paper/paper-account.ts"));

const HOUR = 3_600_000;
const NOW = Date.parse("2026-10-16T12:00:00Z");
const E8 = 1e8;

test("exact text and money formatting never show a sign without an arrow and keep sensible decimals", () => {
  assert.deepEqual([m.parseExact("5"), m.parseExact("1/4"), m.parseExact("-7/2"), m.parseExact(2.5)], [5, 0.25, -3.5, 2.5]);
  assert.deepEqual([m.parseExact("x"), m.parseExact("1/0"), m.parseExact(null), m.parseExact(Number.NaN)], [null, null, null, null]);
  assert.deepEqual([m.formatUsdt(1234.5), m.formatUsdt(0.0123), m.formatUsdt(0), m.formatUsdt(null)], ["1,234.50", "0.0123", "0.00", "—"]);
  assert.equal(m.formatE8(123_456_789), "1.23");
  assert.deepEqual(m.signedUsdt(1.2), { text: "+1.20", tone: "gain", arrow: "▲" });
  assert.deepEqual(m.signedUsdt(-0.05), { text: "−0.0500", tone: "loss", arrow: "▼" });
  assert.deepEqual(m.signedUsdt(0), { text: "0.00", tone: "flat", arrow: "" });
  assert.equal(m.signedUsdt(0.00001).tone, "flat", "below the shown precision it is not a gain");
  assert.equal(m.signedUsdt(null), null);
  assert.equal(m.signedE8(-250_000_000).text, "−2.50");
  for (const value of [3.2, -3.2, 0.5, -0.5]) assert.ok(m.signedUsdt(value).arrow, "a gain or loss always carries an arrow");
});

test("prices, quantities, percentages, durations and times read well at every size", () => {
  assert.deepEqual([m.formatPrice(73_000.123), m.formatPrice(12.3456), m.formatPrice(5.5), m.formatPrice(0.123456), m.formatPrice(null)],
    ["73,000.12", "12.346", "5.5000", "0.123456", "—"]);
  assert.deepEqual([m.formatQuantity("1/3"), m.formatQuantity("5"), m.formatQuantity("0.5"), m.formatQuantity("x")],
    ["0.333333", "5", "0.5", "—"]);
  assert.deepEqual([m.formatPercent(0.1234), m.formatPercent(0.05, 1), m.formatPercent(null)], ["12.34%", "5.0%", "—"]);
  assert.deepEqual([45_000, 12 * 60_000, 2 * HOUR + 5 * 60_000, 76 * HOUR, -1, null].map(m.formatDuration),
    ["45 s", "12 min", "2 h 05 min", "3 d 4 h", "—", "—"]);
  assert.match(m.dateTimeText(Date.UTC(2026, 9, 16, 15, 4)), /^Oct 16, 2026.*3:04.*PM UTC$/);
  assert.equal(m.dateTimeText(null), "—");
  assert.equal(m.coinOf("BTCUSDT"), "BTC");
});

const position = (over = {}) => ({
  symbol: "BTCUSDT", side: 1, leverage_cap: 20, entry_ms: NOW - 5 * HOUR, stop: 7_154_000_000_000, target: 7_700_000_000_000, qty: "1/100",
  entry_fill: "7300000000000", leverage: 20, liquidation_price: "6570000000000", margin_e8: 3_650_000_000,
  notional_e8: "73000000000", ...over,
});

test("open positions: weighted entry, mark from the wallet or the published price, unrealized P&L and liquidation distance", () => {
  const state = {
    balance_e8: 100 * E8, used_margin_e8: 10 * E8,
    marks: { BTCUSDT: { ms: NOW - 10 * 60_000, price: 74_000 * E8 } },
    positions: {
      btc: position(),
      eth: position({ symbol: "ETHUSDT", side: -1, qty: "1/2", entry_fill: "250000000000", liquidation_price: "280000000000",
        entry_ms: NOW - 2 * HOUR, stop: 260_000_000_000, target: 230_000_000_000 }),
      sol: position({ symbol: "SOLUSDT", entry_fill: "10000000000", liquidation_price: "9000000000", entry_ms: NOW - HOUR, qty: "3" }),
    },
  };
  const quotes = [{ symbol: "ETHUSDT", price: 2550, published_at: new Date(NOW - 5 * 60_000).toISOString() }];
  const meta = { btc: { strategy_id: "macd_12_26_9:60", horizon_min: 60 } };
  const [sol, eth, btc] = m.openPositionViews(state, quotes, NOW, meta);
  assert.deepEqual([sol.coin, eth.coin, btc.coin], ["SOL", "ETH", "BTC"], "newest position first");
  assert.deepEqual([btc.strategyId, btc.horizonMin, btc.atCap, sol.strategyId, sol.horizonMin], ["macd_12_26_9:60", 60, true, null, null]);
  assert.deepEqual([btc.sideLabel, btc.quantity, btc.entry, btc.mark, btc.markSource, btc.markStale], ["Long", "0.01", "73,000.00", "74,000.00", "wallet-mark", false]);
  assert.deepEqual(btc.unrealized, { text: "+10.00", tone: "gain", arrow: "▲" });
  assert.equal(btc.liquidationDistanceText, "11.2%");
  assert.deepEqual([btc.margin, btc.leverage, btc.liquidation, btc.stop, btc.target], ["36.50", "20×", "65,700.00", "71,540.00", "77,000.00"]);
  assert.deepEqual([eth.sideLabel, eth.markSource, eth.markStale, eth.unrealized.text, eth.unrealized.arrow], ["Short", "published-price", false, "−25.00", "▼"]);
  assert.equal(eth.liquidationDistanceText, "9.8%");
  assert.deepEqual([sol.markSource, sol.mark, sol.unrealized, sol.liquidationDistanceText], ["none", "—", null, "10.0%"]);
  const stale = m.openPositionViews(state, [{ ...quotes[0], published_at: new Date(NOW - 11 * 60_000).toISOString() }], NOW);
  assert.equal(stale[1].markStale, true, "a published price older than 10 minutes is flagged");
  const old = m.openPositionViews({ ...state, marks: { BTCUSDT: { ms: NOW - 4 * HOUR, price: 74_000 * E8 } } }, quotes, NOW);
  assert.equal(old[2].markStale, true, "a wallet mark older than 3 hours is flagged");
});

test("no positions: an empty list, a flat account and an explicit zero for unrealized P&L", () => {
  assert.deepEqual(m.openPositionViews(null, [], NOW), []);
  assert.deepEqual(m.openPositionViews({ balance_e8: 1, used_margin_e8: 0, positions: {} }, [], NOW), []);
  const run = { boundary_ms: NOW - 30 * 60_000, wallet_config: null, wallet_state: { balance_e8: 100 * E8, used_margin_e8: 0, positions: {} } };
  const view = m.accountView(run, null, [], NOW);
  const card = (key) => view.cards.find((item) => item.key === key);
  assert.equal(view.status, "ready");
  assert.deepEqual([card("balance").value, card("equity").value, card("available").value, card("unrealized").value], ["100.00", "100.00", "100.00", "0.00"]);
  assert.deepEqual([card("realized").value, card("fees").value, card("funding").value], ["—", "—", "—"], "totals not read: unknown, not zero");
});

test("account cards: balance, equity, available and used margin, P&L, fees and funding, each with a hint", () => {
  const state = { balance_e8: 100 * E8, used_margin_e8: 10 * E8, marks: { BTCUSDT: { ms: NOW - 60_000, price: 74_000 * E8 } },
    positions: { btc: position() } };
  const run = { boundary_ms: NOW - 30 * 60_000, wallet_config: null, wallet_state: state };
  const summary = { pnl_e8: 5 * E8, open_fees_e8: -0.03 * E8, close_fees_e8: -0.02 * E8, funding_e8: -0.1 * E8 };
  const view = m.accountView(run, summary, m.openPositionViews(state, [], NOW), NOW);
  assert.deepEqual(view.cards.map((card) => card.key), ["balance", "equity", "available", "margin", "unrealized", "realized", "fees", "funding"]);
  const card = (key) => view.cards.find((item) => item.key === key);
  assert.deepEqual([card("balance").value, card("equity").value, card("available").value, card("margin").value],
    ["100.00", "110.00", "90.00", "10.00"]);
  assert.equal(card("margin").sub, "10.0% of balance");
  assert.deepEqual([card("unrealized").value, card("unrealized").tone, card("realized").value, card("realized").tone], ["+10.00", "gain", "+5.00", "gain"]);
  assert.deepEqual([card("fees").value, card("fees").tone, card("funding").value, card("funding").tone], ["−0.0500", "loss", "−0.1000", "loss"]);
  assert.ok(view.cards.every((item) => item.hint.length > 20 && item.label));
  assert.ok(Math.abs(view.equityUsdt - 110) < 1e-9);
  assert.deepEqual([view.balanceUsdt, view.unmarked, view.note], [100, 0, null]);
  const fallback = m.accountView({ ...run, wallet_state: { ...state, totals: { realized_e8: 2 * E8, fees_e8: -1 * E8, funding_e8: 0 } } }, null, [], NOW);
  assert.deepEqual(["realized", "fees", "funding"].map((key) => fallback.cards.find((item) => item.key === key).value), ["+2.00", "−1.00", "0.00"]);
});

test("stale data: the wallet's age, the unmarked-position note and the state of the page", () => {
  assert.deepEqual([null, NOW - HOUR, NOW - 3 * HOUR, NOW - 7 * HOUR].map((boundary) => m.walletFreshness(boundary, NOW)),
    ["none", "fresh", "delayed", "stale"]);
  const state = { balance_e8: 100 * E8, used_margin_e8: 10 * E8, positions: { sol: position({ symbol: "SOLUSDT" }) } };
  const views = m.openPositionViews(state, [], NOW);
  const old = m.accountView({ boundary_ms: NOW - 7 * HOUR, wallet_config: null, wallet_state: state }, null, views, NOW);
  assert.deepEqual([old.status, old.freshness, old.asOfMs], ["stale", "stale", NOW - 7 * HOUR]);
  assert.deepEqual([old.unmarked, old.equityUsdt, old.cards.find((card) => card.key === "unrealized").value], [1, 100, "—"]);
  assert.match(old.note, /valued at their entry/);
  assert.match(old.cards.find((card) => card.key === "equity").sub, /1 position without a price/);
  assert.equal(m.accountView(null, null, [], NOW).status, "empty");
  assert.equal(m.accountView({ boundary_ms: 1, wallet_config: null, wallet_state: null }, null, [], NOW).status, "empty");
  assert.deepEqual([
    m.listState({ isPending: true, error: null, count: 0 }), m.listState({ isPending: true, error: new Error("x"), count: 0 }),
    m.listState({ isPending: false, error: null, count: 0 }), m.listState({ isPending: false, error: null, count: 3 }),
  ], ["loading", "error", "empty", "ready"]);
});

const line = (seq, setup, type, ms, amount, payload = {}, symbol = "BTCUSDT") =>
  ({ seq, setup_id: setup, type, ms, amount_e8: amount, balance_e8: 100 * E8, symbol, payload });

test("closed trades: gross, fees, funding, net, holding time and the outcome of each", () => {
  const lines = [
    line(1, "t1", "open_fee", 1_000, -0.1 * E8), line(2, "t1", "pnl", 1_000 + HOUR, 1 * E8, { outcome: "T" }), line(3, "t1", "close_fee", 1_000 + HOUR, -0.1 * E8),
    line(4, "t2", "open_fee", 2_000, -0.1 * E8), line(5, "t2", "funding", 3_000, -0.05 * E8), line(6, "t2", "liquidation", 2_000 + 2 * HOUR, -2 * E8),
    line(7, "t3", "open_fee", 3_000, -0.1 * E8), line(8, "t3", "pnl", 3_000 + 30 * 60_000, -0.5 * E8, { outcome: "S" }), line(9, "t3", "close_fee", 3_000 + 30 * 60_000, -0.1 * E8),
    line(10, "t4", "open_fee", 4_000, -0.1 * E8), line(11, "t4", "pnl", 4_000 + HOUR, 0, { outcome: "ambiguous" }),
    line(12, "t5", "open_fee", 5_000, -0.1 * E8), line(13, "t5", "pnl", 5_000 + HOUR, 0.1 * E8, { outcome: "E" }),
    line(14, "t6", "open_fee", 6_000, -0.1 * E8), line(15, "t6", "pnl", 6_000 + HOUR, 0, { outcome: "X" }),
    line(16, "t7", "open_fee", 7_000, -0.1 * E8), line(17, "t7", "pnl", 7_000 + 60_000, 0.2 * E8, { outcome: "T", position_event: "reduced" }),
  ];
  const trades = m.closedTradeViews(lines, new Map([["t1", "macd_12_26_9:60"]]));
  assert.deepEqual(trades.map((t) => t.id), ["t2", "t6", "t5", "t4", "t1", "t3"], "newest close first");
  assert.ok(!trades.some((trade) => trade.id === "t7"), "a partial reduction does not close a trade");
  const by = Object.fromEntries(trades.map((t) => [t.id, t]));
  assert.deepEqual([by.t1.outcome, by.t1.outcomeLabel, by.t1.strategyId, by.t1.holdingText], ["target", "Target hit", "macd_12_26_9:60", "1 h 00 min"]);
  assert.deepEqual([by.t1.gross.text, by.t1.fees.text, by.t1.funding.text, by.t1.net.text], ["+1.00", "−0.2000", "0.00", "+0.8000"]);
  assert.equal(by.t1.netE8, 0.8 * E8);
  assert.deepEqual([by.t3.outcome, by.t3.net.text, by.t3.net.arrow, by.t3.holdingText], ["stopped", "−0.7000", "▼", "30 min"]);
  assert.deepEqual([by.t5.outcome, by.t4.outcome, by.t6.outcome, by.t6.outcomeLabel], ["time_limit", "ambiguous", "unresolved", "Closed early"]);
  assert.ok(by.t3.outcomeHelp.length > 0 && by.t4.outcomeHelp.length > 0);
});

test("a liquidated trade: the liquidation line is its gross result, with the funding it paid", () => {
  const trades = m.closedTradeViews([
    line(1, "x", "open_fee", 1_000, -0.1 * E8), line(2, "x", "funding", 3_000, -0.05 * E8), line(3, "x", "liquidation", 2_000 + 2 * HOUR, -2 * E8),
  ]);
  assert.equal(trades.length, 1);
  const [t] = trades;
  assert.deepEqual([t.outcome, t.outcomeLabel, t.gross.text, t.fees.text, t.funding.text, t.net.text, t.holdingText],
    ["liquidated", "Liquidated", "−2.00", "−0.1000", "−0.0500", "−2.15", "2 h 00 min"]);
  assert.deepEqual([t.gross.arrow, t.net.arrow], ["▼", "▼"]);
  assert.equal(m.outcomeShown("liquidated").code, "L");
});

test("rejected-only account: no trades, readable reasons, orders and fills told apart, results with nothing to average", () => {
  const lines = [
    line(1, "a", "rejected", 1_000, 0, { reason: "cap:max_positions" }, "ETHUSDT"),
    line(2, "b", "rejected", 2_000, 0, { reason: "over_close", fill_id: "f1" }),
    line(3, "o", "order_rejected", 3_000, 0, { reason: "limit_not_passive", order_id: "o1" }),
    line(4, "c", "rejected", 4_000, 0, { reason: "some_new_reason" }),
  ];
  assert.deepEqual(m.closedTradeViews(lines), []);
  const views = m.rejectedViews(lines, new Map([["a", "rsi_14_reversion:15"]]));
  assert.deepEqual(views.map((view) => view.seq), [4, 3, 2, 1], "newest first");
  assert.deepEqual(views.map((view) => view.kind), ["setup", "order", "fill", "setup"]);
  assert.deepEqual(views.map((view) => view.reasonLabel), ["Some new reason", "Limit price is not beyond the decision price",
    "Exit larger than the open quantity", "Position limit reached"]);
  assert.deepEqual([views[3].coin, views[3].strategyId, views[0].strategyId], ["ETH", "rsi_14_reversion:15", null]);
  const summary = { trades: 0, target: 0, stopped: 0, expired: 0, liquidated: 0, ambiguous: 0, unresolved: 0, wins: 0, losses: 0, sum_net_e8: 0,
    gross_wins_e8: 0, gross_losses_e8: 0, rejected: 3, order_rejected: 1, open_fees_e8: 0, close_fees_e8: 0, funding_e8: 0,
    liquidation_e8: 0, pnl_e8: 0, lines: 4, first_ms: 1000, last_ms: 4000 };
  const results = m.resultsView(summary, null, null);
  assert.deepEqual([results.completed, results.rejected, results.expectancy, results.profitFactor, results.maxDrawdown], [0, 4, null, "—", "—"]);
  assert.equal(m.resultsView(null, null, null), null);
});

test("results: counts, expectancy, profit factor, drawdown, cost totals and margin utilization", () => {
  const summary = { trades: 4, target: 1, stopped: 1, expired: 0, liquidated: 1, ambiguous: 1, unresolved: 0, wins: 1, losses: 2,
    sum_net_e8: -205 * E8, gross_wins_e8: 80 * E8, gross_losses_e8: -285 * E8, rejected: 2, order_rejected: 1,
    open_fees_e8: -0.5 * E8, close_fees_e8: -0.3 * E8, funding_e8: -0.05 * E8, liquidation_e8: -2 * E8, pnl_e8: 75 * E8,
    lines: 17, first_ms: 0, last_ms: 1 };
  const state = { balance_e8: 100 * E8, used_margin_e8: 10 * E8 };
  const equity = m.equityView([{ ms: 0, balance_e8: 100 * E8 }, { ms: HOUR, balance_e8: 120 * E8 }, { ms: 2 * HOUR, balance_e8: 90 * E8 }], null, 3 * HOUR);
  const r = m.resultsView(summary, state, equity);
  assert.deepEqual([r.completed, r.rejected, r.stopped, r.liquidated, r.ambiguous, r.target, r.timeLimit], [4, 3, 1, 1, 1, 1, 0]);
  assert.deepEqual([r.expectancy.text, r.expectancy.tone, r.profitFactor, r.maxDrawdown], ["−51.25", "loss", "0.28", "−30.00 USDT (−25.00%)"]);
  assert.deepEqual([r.fees.text, r.funding.text, r.liquidation.text, r.marginUtilization], ["−0.8000", "−0.0500", "−2.00", "10.0%"]);
  assert.equal(r.slippage, "not recorded (wallet-v2)");
  assert.equal(m.resultsView(summary, { ...state, totals: { slippage_e8: 0.12 * E8 } }, equity).slippage, "0.1200 USDT");
  assert.equal(m.resultsView({ ...summary, gross_losses_e8: 0 }, state, equity).profitFactor, "no losing trade");
});

test("equity and drawdown: balance history, equity now as the last point, drawdown from the running peak", () => {
  const series = [{ ms: HOUR, balance_e8: 120 * E8 }, { ms: 0, balance_e8: "10000000000" }, { ms: 2 * HOUR, balance_e8: 90 * E8 }];
  const view = m.equityView(series, 95, 5 * HOUR);
  assert.equal(view.status, "ready");
  assert.deepEqual(view.points.map((p) => [p.ms, p.value]), [[0, 100], [HOUR, 120], [2 * HOUR, 90], [5 * HOUR, 95]]);
  assert.deepEqual(view.drawdown.map((p) => Math.round(p.value * 1000) / 1000 + 0), [0, 0, -0.25, -0.208]);
  assert.deepEqual(view.maxDrawdown, { usdt: 30, fraction: 0.25 });
  assert.deepEqual([view.last, view.change.text, view.currentEquity], [95, "−5.00", 95]);
  assert.ok(view.drawdown.every((p) => p.value <= 0));
  const same = m.equityView(series, 90, 5 * HOUR);
  assert.equal(same.points.length, 3, "no extra point when equity equals the last balance");
  assert.equal(m.equityView([], null, NOW).status, "empty");
  assert.equal(m.equityView([{ ms: 0, balance_e8: E8 }], null, NOW).status, "empty");
  assert.equal(m.equityView([{ ms: 0, balance_e8: 100 * E8 }, { ms: HOUR, balance_e8: 110 * E8 }], null, NOW).maxDrawdown, null);
});

test("ledger rows: money lines carry a signed amount, everything else shows none, with the ids that link them", () => {
  const rows = m.ledgerViews([
    line(1, "s1", "open_fee", 1_000, -0.1 * E8, { qty: "1/4", liquidity: "maker", fill_id: "o1:1", order_id: "o1", assumptions: ["owner-tier"] }),
    line(2, "s1", "order_filled", 1_000, 0, { order_id: "o1", ambiguity: "stop_touched_in_fill_minute" }),
    line(3, "s2", "rejected", 2_000, 0, { reason: "insufficient_margin" }),
    line(4, "s1", "pnl", 3_000, 1 * E8, { outcome: "S" }),
  ]);
  assert.deepEqual(rows.map((row) => row.typeLabel), ["Opening fee", "Order filled", "Rejected", "Trade result"]);
  assert.deepEqual(rows.map((row) => row.amount?.text ?? null), ["−0.1000", null, null, "+1.00"]);
  assert.equal(rows[2].detail, "Not enough available margin");
  assert.equal(rows[3].detail, "Stopped out");
  assert.deepEqual(Object.fromEntries(rows[0].fields), { Setup: "s1", Order: "o1", Fill: "o1:1", Quantity: "0.25", Liquidity: "maker", Assumption: "owner-tier" });
  assert.equal(Object.fromEntries(rows[1].fields).Note, "stop_touched_in_fill_minute");
  assert.equal(m.ledgerKindLabel("stop_moved"), "Stop moved");
  assert.equal(m.ledgerKindLabel("something_new"), "something new");
});

test("run configuration facts and the hints (terms) every figure needs", () => {
  const facts = Object.fromEntries(m.runFacts({
    wallet_version: "wallet-v3", initial_balance_e8: 100 * E8, risk_fraction: "1/100", leverage_cap: 20, max_positions: 6,
    sizing_basis: "balance", max_open_risk_fraction: "1/20", cluster_risk_fraction: "3/100",
    contract_inputs_version: "contract-inputs-v1", contract_inputs_hash: "a1b2c3d4e5f6".padEnd(64, "0"),
  }));
  assert.deepEqual(
    [facts["Wallet version"], facts["Starting balance"], facts["Risk per trade"], facts["Leverage cap"], facts["Max total open risk"],
      facts["Same-direction cluster risk"], facts["Contract inputs"]],
    ["wallet-v3", "100.00 USDT", "1.00%", "20×", "5.0%", "3.0%", "contract-inputs-v1 a1b2c3d4"],
  );
  assert.deepEqual(m.runFacts(null), []);
  assert.equal(Object.fromEntries(m.runFacts({ version: "wallet-v2", leverage_cap: 20 }))["Wallet version"], "wallet-v2");
  for (const [name, text] of Object.entries(m.PAPER_TERMS)) assert.ok(text.length > 30, `${name} has a real explanation`);
  assert.match(m.PAPER_TERMS.filter, /never recreates the wallet/);
  assert.match(m.PAPER_TERMS.simulated, /fake money/);
});
