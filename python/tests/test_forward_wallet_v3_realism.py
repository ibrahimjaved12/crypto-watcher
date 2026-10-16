"""wallet-v3 (#36 Part 2): contract tiers, liquidation per bracket, portfolio limits and the order lifecycle.

Synthetic inputs only; the owner-supplied numbers are asserted from the versioned data file. Runs in well under a
second. Part 1 (golden equivalence, fills, reconciliation) is in test_forward_wallet_v3.py.
"""
from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path
import re
import unittest

from market_analysis.benchmark import costs
from market_analysis.benchmark.canonical import exact_from_str
from market_analysis.forward import contract_inputs as ci
from market_analysis.forward import wallet as v2
from market_analysis.forward import wallet_v3 as v3
from market_analysis.forward.wallet_reconcile import reconcile

from test_forward_wallet import MINUTE, P0, START, setup

E8 = 10 ** 8
ROOT = Path(__file__).resolve().parents[2]
INPUTS = ci.load()
STAGE = "00000000-0000-0000-0000-000000000001"


def config(**options) -> v3.WalletV3Config:
    return v3.WalletV3Config(**options)


def opens(*setups):
    return [{"type": "open", "ms": s["entry_ms"], "setup": s} for s in setups]


def limit_order(order_id="o1", ms=START, limit=99 * E8, expires=None, **setup_args):
    return {"type": "order", "ms": ms, "order_id": order_id, "order_type": "limit", "limit_price": limit,
            "expires_ms": expires, "setup": setup(**setup_args)}


def market_order(order_id="m1", ms=START, **setup_args):
    return {"type": "order", "ms": ms, "order_id": order_id, "order_type": "market", "setup": setup(**setup_args)}


def tape(ms, low, high, close, **extra):
    return {"type": "tape", "ms": ms, "symbol": "BTCUSDT", "low": low, "high": high, "close": close, **extra}


def kinds(lines):
    return [line["type"] for line in lines]


def altered_inputs(**tiers):
    """A parsed copy of the data file whose named symbols get one explicit tier list (test-only numbers)."""
    document = copy.deepcopy(json.loads(ci.DATA_FILE.read_text(encoding="utf-8")))
    for symbol, replacement in tiers.items():
        document["symbols"][symbol]["tiers"] = replacement
    return document


class ContractInputTests(unittest.TestCase):
    def test_owner_supplied_values_are_exactly_the_ticket_numbers(self):
        self.assertEqual(INPUTS.version, "contract-inputs-v1")
        self.assertEqual(tuple(INPUTS.symbols), ci.FROZEN_SYMBOLS)
        for symbol in ("BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT"):
            self.assertEqual(INPUTS.symbol(symbol).liquidation_fee_rate, Fraction(125, 10_000), symbol)
        for symbol in ("DOGEUSDT", "SOLUSDT"):
            self.assertEqual(INPUTS.symbol(symbol).liquidation_fee_rate, Fraction(15, 1000), symbol)
        for symbol in ci.FROZEN_SYMBOLS:
            self.assertEqual(INPUTS.symbol(symbol).liquidation_fee_source, "owner:#36")
        first = INPUTS.symbol("BTCUSDT").tiers[0]
        self.assertEqual((first.floor, first.cap, first.mmr, first.cum, first.max_leverage, first.source),
                         (0, 300_000, Fraction(4, 1000), 0, 150, "owner:#36"))
        self.assertEqual(first.label, "owner-tier")

    def test_everything_else_is_a_labelled_placeholder_and_filters_are_unsupplied(self):
        owned = {("BTCUSDT", 0)}
        for symbol in ci.FROZEN_SYMBOLS:
            for index, tier in enumerate(INPUTS.symbol(symbol).tiers):
                expected = "owner-tier" if (symbol, index) in owned else "placeholder-tier"
                self.assertEqual(tier.label, expected, (symbol, index))
                if expected == "placeholder-tier":
                    self.assertEqual((tier.mmr, tier.max_leverage), (Fraction(1, 100), 20))
            inputs = INPUTS.symbol(symbol)
            self.assertEqual((inputs.tick_size, inputs.step_size, inputs.min_notional), (None, None, None))
            self.assertEqual((inputs.taker_rate, inputs.maker_rate),
                             (costs.COST_MODEL_V1.taker_rate, costs.COST_MODEL_V1.maker_rate))

    def test_tier_lookup_at_bracket_edges(self):
        btc, eth = INPUTS.symbol("BTCUSDT"), INPUTS.symbol("ETHUSDT")
        self.assertEqual(btc.tier_for(0).max_leverage, 150)                       # zero belongs to the first tier
        self.assertEqual(btc.tier_for(300_000 * E8).max_leverage, 150)            # an exact cap belongs to its own tier
        self.assertEqual(btc.tier_for(300_000 * E8 + 1).label, "placeholder-tier")  # the next 1e-8 USDT is above it
        self.assertEqual(btc.tier_for(10_000_000 * E8).label, "placeholder-tier")
        self.assertIsNone(btc.tier_for(10_000_000 * E8 + 1))                      # above the table: unavailable
        self.assertEqual(eth.tier_for(10_000_000 * E8).max_leverage, 20)
        self.assertIsNone(eth.tier_for(10_000_000 * E8 + 1))
        low, high = btc.tiers
        self.assertEqual(low.cap, high.floor)
        self.assertEqual(low.cap * low.mmr - low.cum, high.floor * high.mmr - high.cum)  # maintenance is continuous

    def test_the_file_hash_is_content_not_formatting_and_bad_tables_are_refused(self):
        document = json.loads(ci.DATA_FILE.read_text(encoding="utf-8"))
        self.assertEqual(ci.parse(document).sha256, INPUTS.sha256)
        self.assertEqual(len(INPUTS.sha256), 64)
        changed = copy.deepcopy(document)
        changed["symbols"]["SOLUSDT"]["liquidation_fee_rate"]["value"] = "0.02"
        self.assertNotEqual(ci.parse(changed).sha256, INPUTS.sha256)
        broken = copy.deepcopy(document)
        broken["symbols"]["BTCUSDT"]["tiers"][1]["cum"] = "1799"
        with self.assertRaisesRegex(ValueError, "continuous"):
            ci.parse(broken)
        gap = copy.deepcopy(document)
        gap["symbols"]["BTCUSDT"]["tiers"][1]["floor"] = "300001"
        with self.assertRaisesRegex(ValueError, "contiguous"):
            ci.parse(gap)
        with self.assertRaises(ValueError):
            INPUTS.symbol("LTCUSDT")


class LiquidationFormulaTests(unittest.TestCase):
    def test_the_ticket_example_distances_are_reproduced(self):
        entry, mmr = Fraction(73_000 * E8), Fraction(4, 1000)
        for leverage, percent in ((7, "13.9"), (10, "9.6"), (20, "4.6")):
            lp = v3.liquidation_price_for(1, Fraction(1), entry, entry / leverage, mmr)
            distance = (entry - lp) / entry
            self.assertEqual(distance, (Fraction(1, leverage) - mmr) / (1 - mmr))   # LP = EP (1 - 1/L) / (1 - MMR)
            self.assertEqual(f"{float(distance) * 100:.1f}", percent)
            self.assertEqual(lp, costs.liquidation_price(1, entry, leverage, mmr))  # the benchmark's formula
            short = v3.liquidation_price_for(-1, Fraction(1), entry, entry / leverage, mmr)
            self.assertEqual((short - entry) / entry, (Fraction(1, leverage) - mmr) / (1 + mmr))

    def test_with_a_maintenance_amount_the_liquidation_price_is_where_equity_meets_maintenance(self):
        for side in (1, -1):
            qty, entry, margin = Fraction(7, 3), Fraction(50_000 * E8), Fraction(30_000 * E8)
            mmr, cum = Fraction(1, 100), Fraction(1_800 * E8)
            lp = v3.liquidation_price_for(side, qty, entry, margin, mmr, cum)
            self.assertEqual(margin + side * qty * (lp - entry), qty * lp * mmr - cum)

    def test_the_wallets_own_liquidation_price_also_reserves_the_closing_fee(self):
        # Why the wallet's distances are a little smaller than the ticket's "before fees" numbers: its maintenance
        # rate is MMR + taker fee (0.40 % + 0.05 %), so the same 10x long liquidates 0.0454 points closer.
        entry, mmr, fee = Fraction(73_000 * E8), Fraction(4, 1000), costs.COST_MODEL_V1.taker_rate
        pure = v3.liquidation_price_for(1, Fraction(1), entry, entry / 10, mmr)
        reserved = v3.liquidation_price_for(1, Fraction(1), entry, entry / 10, mmr + fee)
        gap = ((entry - pure) - (entry - reserved)) / entry * 100
        self.assertGreater(gap, Fraction(4, 100))
        self.assertLess(gap, Fraction(5, 100))

    def test_leverage_selector_is_the_largest_that_keeps_the_buffer_and_matches_v2_for_the_flat_tier(self):
        cfg = config(leverage_cap=150)
        bracket = v3.bracket_for(config(contract_inputs=INPUTS, leverage_cap=150), "BTCUSDT", 500 * E8)
        fill, stop_distance = Fraction(73_000 * E8), Fraction(1_460 * E8)   # a 2 % stop; buffer 2
        qty = Fraction(1, 100)
        leverage, lp = v3.tiered_terms(1, fill, qty, stop_distance, bracket, costs.COST_MODEL_V1.taker_rate, cfg)
        self.assertGreaterEqual(fill - lp, cfg.liquidation_buffer * stop_distance)
        worse = v3.liquidation_price_for(1, qty, fill, qty * fill / (leverage + 1), bracket.mmr + costs.COST_MODEL_V1.taker_rate)
        self.assertLess(fill - worse, cfg.liquidation_buffer * stop_distance)
        self.assertEqual(bracket.label, "owner-tier")
        for side in (1, -1):
            for distance in (1, 3, 8):
                trade = setup(side=side, stop_distance=distance * E8)
                flat = v2.position_terms(trade, v2.WalletConfig())
                price = flat[0]
                flat_bracket = v3.bracket_for(config(), "BTCUSDT", 0)
                for quantity in (Fraction(1), Fraction(1, 3), Fraction(7, 5)):
                    self.assertEqual(
                        v3.tiered_terms(side, price, quantity, abs(price - trade["stop"]), flat_bracket,
                                        costs.COST_MODEL_V1.taker_rate, config()), (flat[1], flat[2]))


class TieredWalletTests(unittest.TestCase):
    def open(self, cfg, *setups):
        return v3.step(v3.initial_state(cfg), opens(*setups), cfg)

    def test_an_owner_tier_line_says_so_and_a_placeholder_line_says_so(self):
        cfg = config(contract_inputs=INPUTS)
        state, ledger = self.open(cfg, setup("btc"), setup("eth", symbol="ETHUSDT"))
        by_setup = {line["setup_id"]: line for line in ledger}
        self.assertEqual((by_setup["btc"]["assumptions"], by_setup["eth"]["assumptions"]),
                         (["owner-tier"], ["placeholder-tier"]))
        self.assertTrue(all(line["contract_inputs_hash"] == INPUTS.sha256 for line in ledger))
        self.assertEqual(cfg.to_record()["contract_inputs_hash"], INPUTS.sha256)
        self.assertEqual((state["positions"]["btc"]["liquidation_rule"], state["positions"]["btc"]["tier_label"]),
                         ("wallet-v3:tiered", "owner-tier"))
        # the 0.40 % tier allows more leverage than the flat 1 % at the same stop ...
        flat = v2.position_terms(setup("btc"), v2.WalletConfig())
        self.assertGreater(state["positions"]["btc"]["leverage"], flat[1])
        # ... and the 1 % placeholder tier is wallet-v2's flat tier: same leverage, liquidation price and fee
        trade = setup("eth", symbol="ETHUSDT")
        eth_state, eth_lines = self.open(cfg, trade)
        eth = eth_state["positions"]["eth"]
        expected = v2.position_terms(trade, v2.WalletConfig())
        self.assertEqual((eth["leverage"], exact_from_str(eth["liquidation_price"])), (expected[1], expected[2]))
        self.assertEqual(eth_lines[0]["amount_e8"], v2.step(v2.initial_state(), opens(trade))[1][0]["amount_e8"])

    def test_the_bracket_of_the_notional_is_used_and_above_the_table_is_refused(self):
        big = config(contract_inputs=INPUTS, initial_balance_e8=10 ** 6 * E8)       # 10,000 USDT risk, ~500k notional
        state, ledger = self.open(big, setup("btc"))
        self.assertEqual((ledger[0]["assumptions"], state["positions"]["btc"]["tier_label"]),
                         (["placeholder-tier"], "placeholder-tier"))
        self.assertGreater(exact_from_str(state["positions"]["btc"]["notional_e8"]), 300_000 * E8)
        huge = config(contract_inputs=INPUTS, initial_balance_e8=10 ** 9 * E8, max_exposure_multiple=Fraction(10))
        _, ledger = self.open(huge, setup("btc"))
        self.assertEqual((ledger[0]["type"], ledger[0]["reason"]), ("rejected", "notional_above_tier_table"))
        _, ledger = self.open(config(contract_inputs=INPUTS), setup("x", symbol="LTCUSDT"))
        self.assertEqual(ledger[0]["reason"], "no_contract_inputs")

    def test_the_liquidation_charge_uses_the_symbols_own_fee(self):
        three_percent = [{"floor": "0", "cap": "10000000", "mmr": "0.03", "cum": "0", "max_leverage": 20,
                          "source": "placeholder"}]
        inputs = ci.parse(altered_inputs(BTCUSDT=three_percent, SOLUSDT=three_percent))
        cfg = config(contract_inputs=inputs)
        losses = {}
        for symbol, fee in (("BTCUSDT", Fraction(1, 80)), ("SOLUSDT", Fraction(3, 200))):
            state, _ = self.open(cfg, setup("p", symbol=symbol))
            position = state["positions"]["p"]
            self.assertEqual(v3.liquidation_model(cfg, symbol).liquidation_fee_rate, fee)
            _, lines = v3.step(state, [{"type": "liquidation", "ms": START + MINUTE, "setup_id": "p"}], cfg)
            qty, entry = exact_from_str(position["qty"]), exact_from_str(position["entry_fill"])
            lp = exact_from_str(position["liquidation_price"])
            expected = round(qty * ((entry - lp) + fee * lp))
            self.assertLess(expected, position["margin_e8"])                      # not capped: the fee is visible
            self.assertEqual(lines[0]["amount_e8"], -expected)
            losses[symbol] = expected
        self.assertGreater(losses["SOLUSDT"], losses["BTCUSDT"])
        for symbol in ("BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT"):
            self.assertEqual(v3.liquidation_model(config(contract_inputs=INPUTS), symbol).liquidation_fee_rate,
                             Fraction(1, 80))
        self.assertEqual(v3.liquidation_model(config(), "SOLUSDT").liquidation_fee_rate, costs.COST_MODEL_V1.liquidation_fee_rate)

    def test_filters_are_enforced_only_when_supplied(self):
        document = altered_inputs()
        document["symbols"]["BTCUSDT"]["filters"] = {"tick_size": "0.1", "step_size": "0.25", "min_notional": "500",
                                                    "source": "test"}
        cfg = config(contract_inputs=ci.parse(document))
        state, ledger = self.open(cfg, setup("a"))        # qty 0.4975 -> floored to the 0.25 step, notional < 500
        self.assertEqual(ledger[0]["reason"], "below_min_notional")
        document["symbols"]["BTCUSDT"]["filters"]["min_notional"] = "10"
        state, ledger = self.open(config(contract_inputs=ci.parse(document)), setup("a"))
        qty = exact_from_str(state["positions"]["a"]["qty"])
        self.assertEqual(qty % Fraction(1, 4), 0)
        self.assertEqual(qty, Fraction(1, 4))
        document["symbols"]["BTCUSDT"]["filters"]["step_size"] = "1000"
        _, ledger = self.open(config(contract_inputs=ci.parse(document)), setup("a"))
        self.assertEqual(ledger[0]["reason"], "below_step_size")


class PortfolioLimitTests(unittest.TestCase):
    SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT")

    def run_setups(self, cfg, specs):
        trades = [setup(f"s{i}", side, symbol=symbol) for i, (symbol, side) in enumerate(specs)]
        state, ledger = v3.step(v3.initial_state(cfg), opens(*trades), cfg)
        return state, [(line["setup_id"], line.get("reason")) for line in ledger if line["type"] in ("rejected", "open_fee")]

    def test_total_open_risk_cap_blocks_the_setup_that_would_exceed_it(self):
        self.assertEqual(v3.DEFAULT_MAX_OPEN_RISK, Fraction(5, 100))
        cfg = config(max_open_risk_fraction=Fraction(45, 1000))      # four 1 % trades fit under 4.5 %, a fifth does not
        state, results = self.run_setups(cfg, [(symbol, 1 if i % 2 else -1) for i, symbol in enumerate(self.SYMBOLS)])
        self.assertEqual([reason for _, reason in results], [None] * 4 + ["cap:total_open_risk", "cap:total_open_risk"])
        self.assertEqual(len(state["positions"]), 4)
        off, _ = self.run_setups(config(), [(symbol, 1) for symbol in self.SYMBOLS])
        self.assertEqual(len(off["positions"]), 6)                    # without the option: v2's caps only

    def test_same_direction_positions_across_the_six_coins_are_one_cluster(self):
        cfg = config(cluster_risk_fraction=Fraction(25, 1000))        # two 1 % longs fit under 2.5 %
        state, results = self.run_setups(cfg, [("BTCUSDT", 1), ("ETHUSDT", 1), ("SOLUSDT", 1), ("DOGEUSDT", -1),
                                               ("XRPUSDT", 1)])
        self.assertEqual([reason for _, reason in results],
                         [None, None, "cap:cluster_risk", None, "cap:cluster_risk"])
        self.assertEqual(sorted(state["positions"]), ["s0", "s1", "s3"])  # the short is a different cluster
        self.assertEqual(v3.PLACEHOLDER_CLUSTER_RISK, Fraction(3, 100))

    def test_existing_caps_keep_their_names_and_order_when_limits_are_on(self):
        cfg = config(max_positions=1, max_open_risk_fraction=Fraction(5, 100), cluster_risk_fraction=Fraction(1, 10))
        _, results = self.run_setups(cfg, [("BTCUSDT", 1), ("ETHUSDT", 1)])
        self.assertEqual(results[1][1], "cap:max_positions")
        tight = config(max_exposure_multiple=Fraction(1, 10), max_open_risk_fraction=Fraction(1, 1000))
        self.assertEqual(self.run_setups(tight, [("BTCUSDT", 1)])[1][0][1], "cap:total_exposure")

    def test_working_limit_orders_count_against_the_limits(self):
        cfg = config(max_open_risk_fraction=Fraction(15, 1000))      # one 1 % risk fits, two do not
        orders = [limit_order("o1", setup_id="a"), limit_order("o2", setup_id="b", symbol="ETHUSDT")]
        orders[1]["setup"]["symbol"] = "ETHUSDT"
        state, lines = v3.step(v3.initial_state(cfg), orders, cfg)
        self.assertEqual(kinds(lines), ["order_submitted", "order_accepted", "order_submitted", "order_rejected"])
        self.assertEqual(lines[3]["reason"], "cap:total_open_risk")


class OrderTests(unittest.TestCase):
    def step(self, events, cfg=None, state=None):
        cfg = cfg or config(max_exposure_multiple=Fraction(50))
        return v3.step(state or v3.initial_state(cfg), events, cfg)

    def test_a_market_order_has_a_lifecycle_and_the_same_money_as_an_open(self):
        state, lines = self.step([market_order()])
        self.assertEqual(kinds(lines), ["order_submitted", "order_accepted", "order_filled", "open_fee"])
        self.assertEqual([line["amount_e8"] for line in lines[:3]], [0, 0, 0])
        self.assertEqual(lines[2]["liquidity"], "taker")
        self.assertEqual(lines[3]["amount_e8"], v2.step(v2.initial_state(), opens(setup()))[1][0]["amount_e8"])
        self.assertEqual({line["order_id"] for line in lines}, {"m1"})
        again, replay = self.step([market_order()], state=state)
        self.assertEqual((replay, again["positions"]), ([], state["positions"]))      # the order id is spent
        _, lines = self.step([market_order("m2", status="V")])
        self.assertEqual((kinds(lines)[-1], lines[-1]["reason"]), ("order_rejected", "not_tradeable"))

    def test_limit_orders_must_be_passive_valid_and_unique_per_setup(self):
        for limit, side, reason in ((100 * E8, 1, "limit_not_passive"), (101 * E8, 1, "limit_not_passive"),
                                    (100 * E8, -1, "limit_not_passive"), (99 * E8, -1, "limit_not_passive")):
            _, lines = self.step([limit_order(limit=limit, side=side)])
            self.assertEqual((kinds(lines), lines[-1]["reason"]), (["order_submitted", "order_rejected"], reason))
        _, lines = self.step([limit_order(limit=0)])
        self.assertEqual(lines[-1]["reason"], "invalid_limit_price")
        _, lines = self.step([limit_order(expires=START)])
        self.assertEqual(lines[-1]["reason"], "invalid_expiry")
        state, lines = self.step([limit_order("o1"), limit_order("o2")])
        self.assertEqual(lines[-1]["reason"], "setup_already_used")
        self.assertEqual([o["status"] for _, o in sorted(state["orders"].items())], ["working", "rejected"])
        document = altered_inputs()
        document["symbols"]["BTCUSDT"]["filters"]["tick_size"] = "0.1"
        _, lines = self.step([limit_order(limit=99 * E8 + 5)], config(contract_inputs=ci.parse(document)))
        self.assertEqual(lines[-1]["reason"], "price_off_tick")

    def test_a_working_limit_order_reserves_margin_so_concurrent_setups_cannot_spend_it_twice(self):
        cfg = config(leverage_cap=1, max_exposure_multiple=Fraction(1000))
        state, lines = self.step([limit_order("o1", setup_id="a"), *opens(setup("b", stop_distance=10 ** 8))], cfg)
        self.assertEqual(kinds(lines), ["order_submitted", "order_accepted", "rejected"])
        self.assertEqual(lines[2]["reason"], "insufficient_margin")
        summary = v3.account_summary(state, cfg)
        self.assertGreater(state["reserved_e8"], 0)
        self.assertEqual(summary["available_e8"], state["balance_e8"] - state["used_margin_e8"] - state["reserved_e8"])
        self.assertEqual([o["order_id"] for o in summary["working_orders"]], ["o1"])
        self.assertTrue(reconcile(cfg.initial_balance_e8, lines, state)["ok"])

    def test_a_limit_fills_only_when_the_price_crosses_it_never_on_touch_and_never_from_before_the_order(self):
        state, _ = self.step([limit_order()])
        order = state["orders"]["o1"]
        self.assertEqual((order["status"], exact_from_str(order["qty"])), ("working", 1))
        # data from before the decision (the minute that ended at submission) is unusable, even deep through
        state, lines = self.step([tape(START, 90 * E8, 100 * E8, 91 * E8)], state=state)
        self.assertEqual(lines, [])
        # a touch is not a cross
        state, lines = self.step([tape(START + MINUTE, 99 * E8, 100 * E8, 99 * E8)], state=state)
        self.assertEqual((lines, state["positions"]), ([], {}))
        # crossed intrabar but the minute closed back above the limit: ambiguous, marked once, not filled
        state, lines = self.step([tape(START + 2 * MINUTE, 98 * E8, 100 * E8, 99 * E8 + 1)], state=state)
        self.assertEqual((kinds(lines), lines[0]["reason"]), (["order_ambiguous"], "crossed_without_fill_evidence"))
        state, lines = self.step([tape(START + 3 * MINUTE, 98 * E8, 100 * E8, 99 * E8)], state=state)
        self.assertEqual((lines, state["positions"]), ([], {}))
        # the minute closes through the limit: filled at the limit, exactly, with the maker fee
        reserved = state["reserved_e8"]
        state, lines = self.step([tape(START + 4 * MINUTE, 97 * E8, 100 * E8, 98 * E8)], state=state)
        self.assertEqual(kinds(lines), ["order_filled", "open_fee"])
        position = state["positions"]["s1"]
        self.assertEqual(exact_from_str(position["entry_fill"]), 99 * E8)        # no slippage on a resting order
        self.assertEqual(lines[1]["amount_e8"], -round(99 * E8 * costs.COST_MODEL_V1.maker_rate))
        self.assertEqual((lines[1]["liquidity"], lines[0]["liquidity"], position["entry_ms"]), ("maker", "maker", START + 3 * MINUTE))
        self.assertEqual(lines[0]["ambiguity"], "stop_touched_in_fill_minute")   # the same minute reached the 98 stop
        self.assertEqual((state["reserved_e8"], state["used_margin_e8"] > 0, reserved > 0), (0, True, True))
        self.assertEqual(state["orders"]["o1"]["status"], "filled")

    def test_a_sell_limit_needs_the_high_to_go_above_it(self):
        state, _ = self.step([limit_order(limit=101 * E8, side=-1)])
        state, lines = self.step([tape(START + MINUTE, 99 * E8, 101 * E8, 100 * E8)], state=state)   # touch only
        self.assertEqual(lines, [])
        state, lines = self.step([tape(START + 2 * MINUTE, 99 * E8, 102 * E8, 101 * E8 + 1)], state=state)
        self.assertEqual(kinds(lines), ["order_filled", "open_fee"])
        self.assertEqual(state["positions"]["s1"]["side"], -1)

    def test_partial_fills_follow_the_quantity_that_traded_through_and_replay_is_a_no_op(self):
        state, _ = self.step([limit_order()])
        first = tape(START + MINUTE, 98 * E8, 100 * E8, 99 * E8 + 1, through_qty="1/4")
        state, lines = self.step([first], state=state)
        self.assertEqual(kinds(lines), ["order_partially_filled", "open_fee"])
        self.assertEqual((lines[0]["qty"], lines[0]["remaining"], lines[0]["fill_id"]), ("1/4", "3/4", "o1:1"))
        self.assertEqual(exact_from_str(state["positions"]["s1"]["qty"]), Fraction(1, 4))
        self.assertGreater(state["reserved_e8"], 0)
        self.assertEqual(state["orders"]["o1"]["status"], "working")
        again, replay = self.step([first], state=state)
        self.assertEqual((replay, again["positions"]), ([], state["positions"]))
        state, lines = self.step([tape(START + 2 * MINUTE, 98 * E8, 100 * E8, 98 * E8, through_qty="10")], state=state)
        self.assertEqual(kinds(lines), ["order_filled", "open_fee"])
        position = state["positions"]["s1"]
        self.assertEqual((exact_from_str(position["qty"]), exact_from_str(position["entry_fill"])), (1, 99 * E8))
        self.assertEqual((state["reserved_e8"], position["margin_e8"], state["used_margin_e8"]),
                         (0, position["margin_e8"], position["margin_e8"]))
        self.assertEqual(lines[1]["position_event"], "increased")
        # bars cannot say how much traded: through_qty 0 with a crossing price is ambiguous, not a fill
        waiting, _ = self.step([limit_order("w", setup_id="w")])
        waiting, lines = self.step([tape(START + MINUTE, 98 * E8, 100 * E8, 98 * E8, through_qty="0")], state=waiting)
        self.assertEqual(kinds(lines), ["order_ambiguous"])

    def test_cancel_and_expiry_release_the_reservation_and_stop_all_fills(self):
        state, _ = self.step([limit_order(expires=START + 2 * MINUTE)])
        cancelled, lines = self.step([{"type": "cancel", "ms": START + MINUTE, "order_id": "o1"}], state=state)
        self.assertEqual(kinds(lines), ["order_cancelled"])
        self.assertEqual((cancelled["reserved_e8"], cancelled["orders"]["o1"]["status"]), (0, "cancelled"))
        later, lines = self.step([{"type": "cancel", "ms": START + 2 * MINUTE, "order_id": "o1"},
                                  tape(START + 2 * MINUTE, 90 * E8, 100 * E8, 90 * E8)], state=cancelled)
        self.assertEqual((lines, later["positions"]), ([], {}))
        # a tape stamped at the expiry may still fill; one after it finds the order expired
        at_expiry, lines = self.step([tape(START + 2 * MINUTE, 97 * E8, 100 * E8, 97 * E8)], state=state)
        self.assertEqual(kinds(lines), ["order_filled", "open_fee"])
        expired, lines = self.step([tape(START + 3 * MINUTE, 97 * E8, 100 * E8, 97 * E8)], state=state)
        self.assertEqual(kinds(lines), ["order_expired"])
        self.assertEqual((lines[0]["ms"], expired["reserved_e8"], expired["positions"]), (START + 2 * MINUTE, 0, {}))

    def test_replaying_a_whole_order_history_changes_nothing_and_reconciles(self):
        cfg = config(max_exposure_multiple=Fraction(50), ledger_links=True, run_id="r1")
        events = [limit_order(), tape(START + MINUTE, 97 * E8, 100 * E8, 98 * E8, through_qty="1/2"),
                  tape(START + 2 * MINUTE, 97 * E8, 100 * E8, 98 * E8),
                  {"type": "cancel", "ms": START + 3 * MINUTE, "order_id": "o1"},
                  {"type": "close", "ms": START + 4 * MINUTE, "setup_id": "s1", "outcome": "T",
                   "exit_ref_price": 103 * E8}]
        state, ledger = v3.step(v3.initial_state(cfg), events, cfg)
        self.assertEqual(kinds(ledger), ["order_submitted", "order_accepted", "order_partially_filled", "open_fee",
                                         "order_filled", "open_fee", "pnl", "close_fee"])
        again, replay = v3.step(state, events, cfg)
        self.assertEqual(replay, [])
        self.assertEqual({key: again[key] for key in ("balance_e8", "used_margin_e8", "reserved_e8", "positions")},
                         {key: state[key] for key in ("balance_e8", "used_margin_e8", "reserved_e8", "positions")})
        self.assertTrue(reconcile(cfg.initial_balance_e8, ledger, state)["ok"])
        self.assertTrue(all(line["position_id"] == "s1" and line["run_id"] == "r1" for line in ledger))
        self.assertEqual(ledger[0]["source_event"], {"type": "order", "ms": START})

    def test_orders_compose_through_the_portfolio_step_with_a_stage_envelope(self):
        cfg = config(max_exposure_multiple=Fraction(50))
        events = [{**limit_order(), "symbol": "BTCUSDT", "sequence": 0, "stage_run_id": STAGE},
                  {**tape(START + MINUTE, 97 * E8, 100 * E8, 98 * E8), "sequence": 1, "stage_run_id": STAGE}]
        state, ledger = v3.wallet_step(v3.initial_state(cfg), events, cfg)
        self.assertEqual(kinds(ledger), ["order_submitted", "order_accepted", "order_filled", "open_fee"])
        path = {"setup_id": "s1", "symbol": "BTCUSDT", "side": 1, "points": [[START + 2 * MINUTE, 1]]}
        _, with_path = v3.wallet_step(v3.initial_state(cfg), events, cfg, adverse_paths=[path])
        self.assertEqual(kinds(with_path)[-1], "liquidation")      # a limit-filled position can be liquidated


class LifecycleRegressionTests(unittest.TestCase):
    def compose(self, state, events, cfg, paths=()):
        enveloped = [{**event, "symbol": event.get("symbol", "BTCUSDT"),
                      "sequence": i, "stage_run_id": STAGE} for i, event in enumerate(events)]
        return v3.wallet_step(state, enveloped, cfg, adverse_paths=paths)

    def test_limits_at_or_beyond_stop_reject_without_spending_or_aborting_batch(self):
        for side in (1, -1):
            for aware in (False, True):
                for inputs in (None, INPUTS):
                    for distance in (2, 3):
                        with self.subTest(side=side, aware=aware, inputs=bool(inputs), distance=distance):
                            cfg = config(cost_aware_sizing=aware, contract_inputs=inputs)
                            bad = limit_order(limit=(100 - side * distance) * E8, side=side)
                            good = limit_order("good", limit=(100 - side) * E8, side=side, setup_id="good")
                            state, lines = v3.step(v3.initial_state(cfg), [bad], cfg)
                            self.assertEqual(lines[-1]["reason"], "limit_at_or_beyond_stop")
                            self.assertEqual((state["reserved_e8"], state["used_margin_e8"]), (0, 0))
                            self.assertEqual(state["balance_e8"], cfg.initial_balance_e8)
                            state, lines = v3.step(v3.initial_state(cfg), [bad, good], cfg)
                            self.assertEqual(state["orders"]["good"]["status"], "working")
                            self.assertGreater(exact_from_str(state["orders"]["good"]["qty"]), 0)
                            self.assertGreater(state["reserved_e8"], 0)

    def test_nonpositive_sizing_is_rejected(self):
        cfg = config(risk_fraction=Fraction(0))
        state, lines = v3.step(v3.initial_state(cfg), [limit_order()], cfg)
        self.assertEqual(lines[-1]["reason"], "invalid_quantity")
        self.assertEqual(state["reserved_e8"], 0)
        cfg = config(slip_floor_bps=Fraction(0))
        trade = setup(stop_distance=0)
        state, lines = v3.step(v3.initial_state(cfg), opens(trade), cfg)
        self.assertEqual(lines[-1]["reason"], "invalid_loss_per_unit")
        self.assertEqual(state["positions"], {})

    def test_scale_in_uses_new_terms_only_after_the_fill(self):
        for side in (1, -1):
            cfg = config(max_exposure_multiple=Fraction(50))
            state, opening = v3.step(v3.initial_state(cfg), opens(setup(side=side)), cfg)
            position = state["positions"]["s1"]
            increase = {"type": "fill", "ms": START + 2 * MINUTE, "setup_id": "s1", "fill_id": "scale",
                        "direction": "increase", "qty": position["qty"], "price": (100 + side * 10) * E8}
            changed, _ = v3.step(state, [increase], cfg)
            old_lp = exact_from_str(position["liquidation_price"])
            new_lp = exact_from_str(changed["positions"]["s1"]["liquidation_price"])
            price = int((old_lp + new_lp) / 2)
            path = {"setup_id": "s1", "symbol": "BTCUSDT", "side": side,
                    "points": [[START + MINUTE, price], [START + 3 * MINUTE, price]]}
            final, lines = self.compose(state, [increase], cfg, [path])
            self.assertEqual(kinds(lines), ["open_fee", "liquidation"])
            self.assertEqual(lines[-1]["ms"], START + 3 * MINUTE)
            self.assertEqual((final["positions"], final["used_margin_e8"]), ({}, 0))
            self.assertTrue(reconcile(cfg.initial_balance_e8, opening + lines, final)["ok"])
            intermediate, first = self.compose(state, [increase], cfg,
                [{**path, "points": path["points"][:1]}])
            split, second = self.compose(intermediate, [], cfg,
                [{**path, "points": path["points"][1:]}])
            self.assertEqual((split, first + second), (final, lines))
            # An earlier adverse point must never be tested against a future scale-in's terms.
            path["points"] = path["points"][:1]
            final, lines = self.compose(state, [increase], cfg, [path])
            self.assertIn("s1", final["positions"])
            self.assertNotIn("liquidation", kinds(lines))

    def test_fill_minute_liquidation_runs_after_limit_fill_and_cancels_remainder(self):
        for side in (1, -1):
            for through in ("1/4", "10"):
                cfg = config(max_exposure_multiple=Fraction(50))
                events = [limit_order(limit=(100 - side) * E8, side=side),
                          tape(START + MINUTE, 90 * E8, 110 * E8, 100 * E8, through_qty=through)]
                path = {"setup_id": "s1", "symbol": "BTCUSDT", "side": side,
                        "points": [[START, 1 if side == 1 else 200 * E8]]}
                final, lines = self.compose(v3.initial_state(cfg), events, cfg, [path])
                self.assertEqual(kinds(lines).count("liquidation"), 1)
                self.assertLess(kinds(lines).index("open_fee"), kinds(lines).index("liquidation"))
                self.assertEqual(next(line["ms"] for line in lines if line["type"] == "liquidation"), START + MINUTE)
                self.assertEqual((final["positions"], final["reserved_e8"], final["used_margin_e8"]), ({}, 0, 0))
                self.assertEqual(final["orders"]["o1"]["status"], "cancelled" if through == "1/4" else "filled")
                self.assertTrue(reconcile(cfg.initial_balance_e8, lines, final)["ok"])

    def test_every_position_exit_cancels_remainder_and_later_tapes_cannot_reopen(self):
        for kind in ("close", "liquidation", "fill"):
            cfg = config()
            state, first = v3.step(v3.initial_state(cfg), [limit_order(),
                tape(START + MINUTE, 98 * E8, 100 * E8, 99 * E8, through_qty="1/4")], cfg)
            event = {"type": kind, "ms": START + 2 * MINUTE, "setup_id": "s1"}
            if kind == "fill":
                event.update(direction="reduce", qty="1/4", price=98 * E8, fill_id="exit")
            state, lines = v3.step(state, [event], cfg)
            self.assertEqual(lines[-1]["type"], "order_cancelled")
            self.assertEqual(lines[-1]["reason"], "setup_completed")
            self.assertEqual((state["orders"]["o1"]["status"], state["reserved_e8"]), ("cancelled", 0))
            final, later = v3.step(state, [tape(START + 3 * MINUTE, 90 * E8, 100 * E8, 90 * E8)], cfg)
            self.assertEqual((later, final["positions"], final["done"]), ([], {}, ["s1"]))
            self.assertTrue(reconcile(cfg.initial_balance_e8, first + lines, final)["ok"])
            # Defensive handling of an older persisted state with a stale working remainder.
            state["orders"]["o1"]["status"] = "working"
            final, later = v3.step(state, [tape(START + 3 * MINUTE, 90 * E8, 100 * E8, 90 * E8)], cfg)
            self.assertEqual((final["positions"], final["orders"]["o1"]["status"]), ({}, "cancelled"))


class CostAwareSizingTests(unittest.TestCase):
    def test_the_loss_at_the_stop_equals_the_risk_budget_only_when_costs_are_inside_it(self):
        for side in (1, -1):
            for aware in (False, True):
                cfg = config(cost_aware_sizing=aware)
                trade = setup("s", side)
                state, opened = v3.step(v3.initial_state(cfg), opens(trade), cfg)
                _, closed = v3.step(state, [{"type": "close", "ms": START + MINUTE, "setup_id": "s", "outcome": "S",
                                             "exit_ref_price": trade["stop"]}], cfg)
                loss = -sum(line["amount_e8"] for line in [*opened, *closed])
                risk = cfg.initial_balance_e8 * cfg.risk_fraction
                if aware:
                    self.assertLessEqual(abs(loss - risk), 2, (side, loss))      # three rounded ledger lines
                else:
                    self.assertGreater(loss, risk + 10 ** 6, (side, loss))       # fees and exit slippage on top of 1 R
        self.assertTrue(config(cost_aware_sizing=True).to_record()["cost_aware_sizing"])
        self.assertFalse(config().to_record()["cost_aware_sizing"])


class SafetyBoundaryTests(unittest.TestCase):
    FORBIDDEN = re.compile(r"X-MBX-APIKEY|/fapi/v\d/(order|batchOrders|leverage|marginType|positionSide)|newOrder|hmac\.new|signature=")

    def test_no_trading_credential_or_order_endpoint_in_the_wallet_path(self):
        paths = [*(ROOT / "python/market_analysis/forward").glob("*.py"), ROOT / "python/market_analysis/api.py",
                 *(ROOT / "src/lib/paper").glob("*.ts"), ROOT / "src/components/paper-account.tsx",
                 ROOT / "src/lib/paper.functions.ts"]
        self.assertGreater(len(paths), 10)
        for path in paths:
            self.assertIsNone(self.FORBIDDEN.search(path.read_text(encoding="utf-8")), path.name)


class LedgerKindTests(unittest.TestCase):
    def test_python_sql_and_typescript_list_the_same_ledger_kinds(self):
        sql = (ROOT / "supabase/migrations/20261016180000_paper_ledger_order_kinds.sql").read_text(encoding="utf-8")
        check = sql[sql.index("CHECK (type IN ("):]
        in_sql = set(re.findall(r"'([a-z_]+)'", check))
        source = (ROOT / "src/lib/forward/forward-contract.ts").read_text(encoding="utf-8")
        start = source.index("export const LEDGER_KINDS = [")
        block = source[start:source.index("] as const;", start)]
        self.assertEqual(in_sql, set(re.findall(r'"([a-z_]+)"', block)))
        self.assertEqual(in_sql, set(v3.LEDGER_KINDS))
        self.assertEqual(set(v3.RESERVED_KINDS), {"stop_moved", "target_moved"})   # #221: reserved, never emitted


class ApiTests(unittest.TestCase):
    def test_contract_inputs_limits_and_orders_through_the_endpoint(self):
        try:
            import asyncio
            import httpx
            from market_analysis.api import create_app
        except ImportError:
            self.skipTest("API dependencies are not installed")
        token = "test-service-token-" + "x" * 32
        cfg = {"wallet_version": "wallet-v3", "contract_inputs": True, "portfolio_limits": True,
               "max_exposure_multiple": "50", "cost_aware_sizing": True}

        def envelope(sequence, **payload):
            return {"symbol": "BTCUSDT", "sequence": sequence, "stage_run_id": STAGE, **payload}

        async def main():
            app = create_app(token)
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    headers = {"Authorization": f"Bearer {token}"}
                    first = await client.post("/v1/forward/wallet_step", headers=headers, json={
                        "schema_version": 1, "config": cfg, "events": [
                            envelope(0, type="order", ms=START, order_id="o1", order_type="limit", limit_price=99 * E8,
                                     setup=setup())]})
                    second = await client.post("/v1/forward/wallet_step", headers=headers, json={
                        "schema_version": 1, "config": cfg, "state": first.json()["wallet_state"], "events": [
                            envelope(1, type="tape", ms=START + MINUTE, low=97 * E8, high=100 * E8, close=98 * E8),
                            envelope(2, type="cancel", ms=START + 2 * MINUTE, order_id="o1")]})
                    bad = await client.post("/v1/forward/wallet_step", headers=headers, json={
                        "schema_version": 1, "config": {"contract_inputs": True}, "events": []})
                    worse = await client.post("/v1/forward/wallet_step", headers=headers, json={
                        "schema_version": 1, "config": cfg, "events": [
                            envelope(0, type="tape", ms=START, low=100 * E8, high=99 * E8, close=99 * E8)]})
                    pinned = {**cfg, "contract_inputs_version": "contract-inputs-v1",
                              "contract_inputs_hash": INPUTS.sha256}
                    same = await client.post("/v1/forward/wallet_step", headers=headers,
                                             json={"schema_version": 1, "config": pinned, "events": []})
                    changed = await client.post("/v1/forward/wallet_step", headers=headers, json={
                        "schema_version": 1, "config": {**pinned, "contract_inputs_hash": "0" * 64}, "events": []})
                    return first, second, bad, worse, same, changed

        first, second, bad, worse, same, changed = asyncio.run(main())
        self.assertEqual(first.status_code, 200, first.text)
        record = first.json()["wallet_config"]
        self.assertEqual((record["contract_inputs_hash"], record["max_open_risk_fraction"], record["cluster_risk_fraction"]),
                         (INPUTS.sha256, "1/20", "3/100"))
        self.assertEqual([line["type"] for line in first.json()["ledger"]], ["order_submitted", "order_accepted"])
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual([line["type"] for line in second.json()["ledger"]], ["order_filled", "open_fee"])
        self.assertTrue(second.json()["reconciliation"]["ok"])
        self.assertEqual(second.json()["account"]["positions"][0]["tier_label"], "owner-tier")
        self.assertEqual(bad.status_code, 422)     # v3-only option under the default wallet-v2 configuration
        self.assertEqual(worse.status_code, 422)   # a tape with low above high
        self.assertEqual(same.status_code, 200, same.text)     # a stored configuration pins the data file ...
        self.assertEqual(changed.status_code, 409)             # ... and a changed file is a new run, not a swap


if __name__ == "__main__":
    unittest.main()
