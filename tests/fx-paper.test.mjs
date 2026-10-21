import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

import { createModuleLoader } from "./module-loader.mjs";

// #454: the forex Paper account's pure parts (reason, close and ledger mappings, lots, R, page state) and the
// market-keyed query keys and equity ranges both markets share.
const load = createModuleLoader(import.meta.url);
const fx = await import(load("../src/lib/paper/fx-paper.ts"));
const view = await import(load("../src/lib/paper/paper-view.ts"));

const E8 = 1e8;
const HOUR = 3_600_000;
const NOW = Date.parse("2026-10-22T12:00:00Z");

test("every forex rejection code, close reason and ledger type has a label and an icon", () => {
  assert.deepEqual([...fx.FX_REJECTION_CODES].sort(), Object.keys(fx.FX_REJECTIONS).sort());
  for (const code of fx.FX_REJECTION_CODES) {
    const entry = fx.fxRejection(code);
    assert.ok(entry.label.length > 0 && entry.label.length <= 24, `${code}: short label`);
    assert.ok(entry.help.length > 0, `${code}: help`);
    assert.notEqual(entry.icon, "unknown", `${code}: its own icon`);
    assert.equal(entry.code, code);
  }
  assert.equal(new Set(fx.FX_REJECTION_CODES.map((code) => fx.FX_REJECTIONS[code].icon)).size, fx.FX_REJECTION_CODES.length,
    "each reason has a distinct icon");
  const unknown = fx.fxRejection("cap:new_rule");
  assert.deepEqual([unknown.label, unknown.icon], ["new rule", "unknown"]);
  assert.equal(fx.fxRejection(null).label, "Unknown reason");

  for (const type of fx.FX_LEDGER_TYPES) {
    const entry = fx.fxLedgerType(type);
    assert.ok(entry.label && entry.icon && entry.icon !== "unknown", type);
  }
  assert.equal(fx.fxLedgerType("funding").label, "Swap");
  assert.equal(fx.fxLedgerType("liquidation").label, "Stop-out");
  assert.equal(fx.fxLedgerType("mystery_kind").icon, "unknown");

  for (const [key, reason] of Object.entries(fx.FX_CLOSE_REASONS)) assert.ok(reason.label && reason.icon && reason.help, key);
  const close = (type, outcome) => fx.fxCloseReason({ type, payload: outcome === undefined ? {} : { outcome } });
  assert.deepEqual(
    [close("pnl", "T"), close("pnl", "S"), close("pnl", "E"), close("liquidation"), close("pnl", "ambiguous"), close("pnl", "X")],
    ["target", "stop", "time", "stop_out", "ambiguous", "unresolved"],
  );
  assert.deepEqual([fx.reasonTone("target"), fx.reasonTone("stop_out"), fx.reasonTone("time")], ["gain", "loss", "flat"]);
});

test("the forex page maps every icon key to an icon", async () => {
  const source = await readFile(new URL("../src/components/paper/fx-paper-account.tsx", import.meta.url), "utf8");
  const block = source.match(/FX_ICONS: Record<FxIconKey, LucideIcon> = \{([\s\S]*?)\n\};/);
  assert.ok(block, "FX_ICONS map present");
  const mapped = new Set([...block[1].matchAll(/^\s*"?([a-z-]+)"?:/gm)].map((m) => m[1]));
  const used = new Set([
    ...Object.values(fx.FX_REJECTIONS).map((entry) => entry.icon),
    ...Object.values(fx.FX_CLOSE_REASONS).map((entry) => entry.icon),
    ...Object.values(fx.FX_LEDGER).map((entry) => entry.icon),
    "unknown",
  ]);
  for (const key of used) assert.ok(mapped.has(key), `icon for ${key}`);
});

test("lots are units over the contract size, shown with two or three decimals", () => {
  assert.equal(fx.lotsOf("10000", "100000"), 0.1);
  assert.equal(fx.lotsOf("150", "100"), 1.5, "gold: 100 oz per lot");
  assert.equal(fx.lotsOf("1500/1", "100000"), 0.015);
  assert.deepEqual([fx.lotsOf(null, "100000"), fx.lotsOf("1000", "0"), fx.lotsOf("1000", undefined)], [null, null, null]);
  assert.deepEqual([fx.formatLots(0.1), fx.formatLots(1), fx.formatLots(0.015), fx.formatLots(null)], ["0.10", "1.00", "0.015", "—"]);
  assert.deepEqual([fx.formatFxPrice(1.08123), fx.formatFxPrice(151.234), fx.formatFxPrice(2345.6), fx.formatFxPrice(1.08, 3)],
    ["1.08123", "151.234", "2,345.60", "1.080"]);
  assert.deepEqual(fx.signedR(1.854), { text: "+1.85 R", tone: "gain", arrow: "▲" });
  assert.deepEqual(fx.signedR(-1), { text: "−1.00 R", tone: "loss", arrow: "▼" });
  assert.equal(fx.signedR(Number.NaN), null);
});

test("the page state is honest: no access, no account, no run, stale after two hours", () => {
  const base = { access: "ready", entitled: true, loaded: true, error: false, accountId: "u:forex", boundaryMs: NOW - HOUR, nowMs: NOW };
  assert.equal(fx.fxPageState({ ...base, access: "loading" }), "loading");
  assert.equal(fx.fxPageState({ ...base, entitled: false }), "no_access");
  assert.equal(fx.fxPageState({ ...base, loaded: false }), "loading");
  assert.equal(fx.fxPageState({ ...base, loaded: false, error: true }), "error");
  assert.equal(fx.fxPageState({ ...base, accountId: null, boundaryMs: null }), "no_account");
  assert.equal(fx.fxPageState({ ...base, boundaryMs: null }), "waiting");
  assert.equal(fx.fxPageState(base), "ready");
  assert.equal(fx.fxPageState({ ...base, boundaryMs: NOW - 2 * HOUR }), "ready", "exactly two hours is not yet stale");
  assert.equal(fx.fxPageState({ ...base, boundaryMs: NOW - 2 * HOUR - 1 }), "stale");
  assert.equal(fx.FX_STALE_AFTER_MS, 2 * HOUR);
  assert.equal(fx.fxPageState({ ...base, entitled: false, loaded: false, error: true }), "no_access", "access is decided first");
});

test("query keys carry the market, so the two accounts never share a cache entry", () => {
  assert.deepEqual(view.paperKeys.account("crypto"), ["paper", "crypto", "account"]);
  assert.notDeepEqual(view.paperKeys.account("crypto"), view.paperKeys.account("forex"));
  assert.deepEqual(view.paperKeys.list("forex", "ledger", "u:forex", 5), ["paper", "forex", "ledger", "u:forex", 5]);
  assert.notDeepEqual(view.paperKeys.list("crypto", "trades", "a", 1), view.paperKeys.list("forex", "trades", "a", 1));
  assert.equal(view.paperKeys.account("forex")[1], "forex");
  assert.deepEqual([view.accountMarketOf("8c1e:forex"), view.accountMarketOf("8c1e"), view.accountMarketOf("forex")],
    ["forex", "crypto", "crypto"]);
});

test("equity ranges carry the last balance into the window and the balance line reaches now", () => {
  const points = [
    { ms: NOW - 10 * 86_400_000, value: 1000 },
    { ms: NOW - 3 * 86_400_000, value: 1010 },
    { ms: NOW - 2 * HOUR, value: 1005 },
  ];
  assert.equal(view.rangePoints(points, "all", NOW).length, 3);
  const day = view.rangePoints(points, "1d", NOW);
  assert.deepEqual(day, [{ ms: NOW - 86_400_000, value: 1010 }, { ms: NOW - 2 * HOUR, value: 1005 }]);
  assert.deepEqual(view.rangeChange(day), { value: -5, fraction: -5 / 1010 });
  assert.equal(view.rangeChange(day.slice(0, 1)), null);
  assert.deepEqual(view.rangePoints([], "1w", NOW), []);
  const series = view.balanceSeries([{ ms: String(NOW - HOUR), balance_e8: String(1000 * E8) }], NOW);
  assert.deepEqual(series, [{ ms: NOW - HOUR, value: 1000 }, { ms: NOW, value: 1000 }]);
});

const position = (over = {}) => ({
  symbol: "EURUSD", side: 1, entry_ms: NOW - 2 * HOUR, stop: 107_900_000, target: 108_300_000, units: "10000",
  entry_fill: "108000000", leverage: 100, stop_out_price: "107000000", margin_e8: 108 * E8, notional_e8: "1080000000000",
  contract_size: "100000", quote_to_account_entry: "1", ...over,
});

test("open positions are priced at bid (long) or ask (short) and the headline never invents a mark", () => {
  const state = { version: "fx-wallet-v1", balance_e8: 1010 * E8, used_margin_e8: 216 * E8,
    positions: { a: position(), b: position({ side: -1, entry_ms: NOW - HOUR }) } };
  const quotes = [{ symbol: "EURUSD", bid: 1.081, ask: 1.0812, digits: 5, published_at: new Date(NOW - 60_000).toISOString() }];
  const views = fx.fxPositionViews(state, quotes, NOW, { a: { strategy_id: "macd_12_26_9", horizon_min: 60 } });
  assert.deepEqual(views.map((v) => v.id), ["b", "a"], "newest first");
  const [short, long] = views;
  assert.equal(long.lotsText, "0.10");
  assert.equal(long.unrealized.text, "+10.00", "10,000 × (1.08100 − 1.08000)");
  assert.equal(short.unrealized.text, "−12.00", "a short closes at the ask");
  assert.equal(long.openRiskUsd.toFixed(2), "10.00", "10,000 × 0.0010 to the stop");
  assert.equal(long.strategyId, "macd_12_26_9");
  assert.equal(long.entry, "1.08000");

  const account = fx.fxAccountView(state, views, { initialBalanceE8: 1000 * E8, dayStartBalanceE8: 1004 * E8 });
  assert.equal(account.equityUsd.toFixed(2), "1008.00", "1,010 + 10 − 12");
  assert.equal(account.total.text, "+10.00");
  assert.equal(account.totalFraction, 0.01);
  assert.equal(account.today.text, "+6.00");
  assert.equal(account.freeMarginUsd, 1010 - 216);
  assert.equal(account.openPositions, 2);

  const unpriced = fx.fxPositionViews(state, [], NOW);
  assert.equal(unpriced[0].unrealized, null);
  const noQuote = fx.fxAccountView(state, unpriced, { initialBalanceE8: 1000 * E8, dayStartBalanceE8: null });
  assert.deepEqual([noQuote.unmarked, noQuote.equityUsd, noQuote.unrealized, noQuote.today.text], [2, 1010, null, "+10.00"],
    "no price: equity is the balance, unrealized is unknown, today falls back to the starting balance");
  const empty = fx.fxAccountView(null, [], { initialBalanceE8: null, dayStartBalanceE8: null });
  assert.deepEqual([empty.balanceUsd, empty.total, empty.today], [null, null, null], "no state: nothing, not zeros");
  assert.equal(fx.utcDayStart(NOW), Date.parse("2026-10-22T00:00:00Z"));
});

const line = (seq, type, amount, balance, extra = {}) => ({
  seq, ms: NOW - 10 * HOUR + seq * HOUR, type, setup_id: extra.setup_id ?? "s1", symbol: "EURUSD",
  amount_e8: amount, balance_e8: balance, payload: extra.payload ?? {},
});

test("closed trades carry lots, entry and exit, the close reason and R against the 1 % budget", () => {
  const lines = [
    line(1, "open_fee", -70_000_000, 99_930_000_000, { payload: { units: "10000", leverage: 100 } }),
    line(2, "funding", -30_000_000, 99_900_000_000, { payload: { kind: "swap", multiplier: 3 } }),
    line(3, "pnl", 2_000_000_000, 101_900_000_000, { payload: { outcome: "T" } }),
    line(4, "close_fee", -70_000_000, 101_830_000_000),
    line(5, "open_fee", -70_000_000, 101_760_000_000, { setup_id: "s2", payload: { units: "10000" } }),
    line(6, "liquidation", -4_000_000_000, 97_760_000_000, { setup_id: "s2" }),
    line(7, "rejected", 0, 97_760_000_000, { setup_id: "s3", payload: { reason: "below_min_lot" } }),
  ];
  const facts = {
    s1: { strategy_id: "ema_cross_20_50", side: 1, horizon_min: 60, entry_fill: "108000000", stop: 107900000, target: 108200000, exit_fill: "108200000" },
    s2: { strategy_id: "rsi_14_reversion", side: -1, horizon_min: 15, entry_fill: "108000000", stop: 108100000, target: null, exit_fill: null },
  };
  const config = { riskFraction: 0.01, contractSizes: { EURUSD: "100000" } };
  const [stopOut, target] = fx.fxClosedTradeViews(lines, facts, config, { EURUSD: 5 });
  assert.equal(target.id, "s1");
  assert.deepEqual([target.lotsText, target.entry, target.exit, target.reason, target.side], ["0.10", "1.08000", "1.08200", "target", 1]);
  assert.equal(target.net.text, "+18.30", "20 − 0.7 − 0.3 − 0.7");
  assert.equal(target.costs.text, "−1.40");
  assert.equal(target.swap.text, "−0.3000", "below one unit: four decimals, like every paper amount");
  assert.equal(target.netR.text, "+1.83 R", "18.30 ÷ (1 % of 1,000.00 before the opening commission)");
  assert.deepEqual([stopOut.reason, stopOut.exit, stopOut.netR.tone], ["stop_out", "—", "loss"], "a stop-out has no stored exit fill");
  assert.equal(fx.fxClosedTradeViews(lines, facts, { riskFraction: null, contractSizes: {} })[1].netR, null, "no risk fraction, no R");
  assert.equal(fx.fxClosedTradeViews(lines, {}, config)[1].lotsText, "0.10", "lots come from the ledger, not the facts");

  const rejected = fx.fxRejectedViews(lines, { s3: { strategy_id: "macd_12_26_9", side: 1 } });
  assert.deepEqual(rejected.map((r) => [r.code, r.label, r.icon, r.side]), [["below_min_lot", "Below minimum lot", "lot", 1]]);

  const ledger = fx.fxLedgerViews(lines);
  assert.deepEqual(ledger.map((r) => r.label), ["Commission · open", "Swap", "Trade result", "Commission · close",
    "Commission · open", "Stop-out", "Rejected"]);
  assert.equal(ledger[1].detail, "× 3 rollover");
  assert.equal(ledger[2].detail, "Target");
  assert.equal(ledger[6].amount, null, "a rejection moves no money: no amount, not a zero");
  assert.equal(ledger[6].detail, "Below minimum lot");
});

test("the stored forex config gives the risk fraction, contract sizes and the run facts", () => {
  const config = { version: "fx-wallet-v1", initial_balance_e8: 100_000_000_000, risk_fraction: "1/100", max_positions: 1000,
    max_exposure_multiple: "10", contracts: [{ symbol: "XAUUSD", contract_size: "100" }, { symbol: "EURUSD", contract_size: "100000" }] };
  const facts = fx.fxConfigFacts(config);
  assert.deepEqual([facts.riskFraction, facts.initialBalanceE8, facts.contractSizes.XAUUSD], [0.01, 1000 * E8, "100"]);
  assert.deepEqual(fx.fxRunFacts(config), [
    ["Wallet version", "fx-wallet-v1"], ["Starting balance", "1,000.00 USD"], ["Risk per trade", "1.00%"],
    ["Exposure cap", "10 × balance"], ["Max positions", "1000"], ["Contracts", "EURUSD, XAUUSD"],
  ]);
  assert.deepEqual(fx.fxRunFacts(null), []);
  assert.deepEqual(fx.fxConfigFacts(null).contractSizes, {});
});
