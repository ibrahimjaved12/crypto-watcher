"""Forex paper wallet (#374): exact sizing, margin, commission, swap, stop-out and append-only ledger."""

from array import array
from copy import deepcopy
from fractions import Fraction
import unittest

from market_analysis.benchmark.canonical import exact_from_str
from market_analysis.fx.bars import FLAG_PRESENT, FxBarSeries
from market_analysis.fx.costs import FxContract, FxCostModel
from market_analysis.fx.setups import FxSetup
from market_analysis.fx.wallet import (E8, FxWalletConfig, initial_state, position_account_value, step,
                                       stop_out_events)

MINUTE = 60_000
T0 = 1_792_000_000_000 - 1_792_000_000_000 % MINUTE
ZERO = FxCostModel(version="zero-slip", market_slip_spread_fraction=0, market_slip_range_mult=0,
                   stop_slip_spread_fraction=0, stop_slip_range_mult=0)
EURUSD = FxContract(Fraction(100_000), Fraction(1, 100_000), Fraction(1, 100), Fraction(1, 100), "EUR", "USD")
USDJPY = FxContract(Fraction(100_000), Fraction(1, 1000), Fraction(1, 100), Fraction(1, 100), "USD", "JPY")
ONE = "1"


def config(balance=1000, model=ZERO, **kwargs):
    return FxWalletConfig(initial_balance_e8=balance * E8, model=model,
                          contracts=(("EURUSD", EURUSD), ("USDJPY", USDJPY)), **kwargs)


def setup(setup_id="a", side=1, fill=108_010_000, stop=107_810_000, target=108_410_000, symbol="EURUSD",
          entry_ms=T0, status="T"):
    return FxSetup(setup_id, "sig-" + setup_id, "ema_cross_20_50:60", "ta-v1", symbol, side, 60, entry_ms, entry_ms,
                   "2", "2", status, "fx-atr-v1", None, 240, None, fill, 10_000, 1000, 200, stop, target, str(fill),
                   str(abs(fill - stop)), "h").to_dict()


def open_event(s, ms=T0, conv=ONE):
    return {"type": "open", "ms": ms, "setup": s, "quote_to_account": conv}


def close_event(setup_id, exit_fill, ms=T0 + 60 * MINUTE, outcome="T", conv=ONE):
    return {"type": "close", "ms": ms, "setup_id": setup_id, "exit_fill": str(exit_fill), "outcome": outcome,
            "quote_to_account": conv}


def assert_balanced(test, cfg, state, ledgers):
    total = sum(line["amount_e8"] for ledger in ledgers for line in ledger)
    test.assertEqual(cfg.initial_balance_e8 + total, state["balance_e8"])
    seqs = [line["seq"] for ledger in ledgers for line in ledger]
    test.assertEqual(seqs, list(range(1, len(seqs) + 1)))


class OpenCloseTests(unittest.TestCase):
    def test_below_min_lot_is_rejected(self):
        cfg = config(balance=100)
        state, ledger = step(initial_state(cfg), [open_event(setup())], cfg)
        self.assertEqual([(l["type"], l["reason"]) for l in ledger], [("rejected", "below_min_lot")])
        self.assertEqual((state["balance_e8"], state["done"], state["positions"]), (100 * E8, ["a"], {}))

    def test_open_sizing_margin_and_stop_out_price(self):
        cfg = config()
        state, ledger = step(initial_state(cfg), [open_event(setup())], cfg)
        position = state["positions"]["a"]
        self.assertEqual(position["units"], "5000")  # 10 USD / 0.0020 = 5000 units = 0.05 lot
        self.assertEqual(position["leverage"], 100)  # floor(1.0801 * 0.5 / 0.004) = 135, capped at 100
        self.assertEqual(exact_from_str(position["notional_e8"]), 540_050_000_000)  # 5400.50 USD
        self.assertEqual(position["margin_e8"], 5_400_500_000)  # 54.005 USD
        self.assertEqual(position["stop_out_price"], "107469950")  # 1.0801 - 1.0801 * 0.5 / 100
        self.assertEqual([(l["type"], l["amount_e8"], l["units"], l["margin_e8"]) for l in ledger],
                         [("open_fee", 0, "5000", 5_400_500_000)])
        self.assertEqual(state["used_margin_e8"], 5_400_500_000)
        self.assertEqual(position_account_value(position, 108_110_000, 10_000, 1), Fraction(5))  # +0.0010 x 5000

    def test_close_at_target_and_stop(self):
        cfg = config()
        opened, first = step(initial_state(cfg), [open_event(setup())], cfg)
        won, ledger = step(opened, [close_event("a", 108_410_000)], cfg)
        self.assertEqual([(l["type"], l["amount_e8"]) for l in ledger], [("pnl", 2_000_000_000), ("close_fee", 0)])
        self.assertEqual((won["used_margin_e8"], won["positions"]), (0, {}))
        assert_balanced(self, cfg, won, [first, ledger])
        lost, ledger = step(opened, [close_event("a", 107_810_000, outcome="S")], cfg)
        self.assertEqual(ledger[0]["amount_e8"], -10 * E8)
        self.assertEqual(ledger[0]["outcome"], "S")
        assert_balanced(self, cfg, lost, [first, ledger])

    def test_commission_per_lot_and_side(self):
        model = FxCostModel(version="zero-slip-commission", market_slip_spread_fraction=0, market_slip_range_mult=0,
                            stop_slip_spread_fraction=0, stop_slip_range_mult=0, commission_per_lot_side=Fraction(7, 2))
        cfg = config(model=model)
        state, ledger = step(initial_state(cfg), [open_event(setup()), close_event("a", 108_410_000)], cfg)
        self.assertEqual([(l["type"], l["amount_e8"]) for l in ledger],
                         [("open_fee", -17_500_000), ("pnl", 2_000_000_000), ("close_fee", -17_500_000)])
        assert_balanced(self, cfg, state, [ledger])


class EntryMinuteExitTests(unittest.TestCase):
    def test_entry_minute_stop_and_target_in_one_batch(self):
        cfg = config()
        state, ledger = step(initial_state(cfg), [close_event("a", 107_810_000, ms=T0, outcome="S"),
                                                  open_event(setup())], cfg)
        self.assertEqual([l["type"] for l in ledger], ["open_fee", "pnl", "close_fee"])
        self.assertEqual((ledger[1]["amount_e8"], state["positions"], state["used_margin_e8"]), (-10 * E8, {}, 0))
        state, ledger = step(initial_state(cfg), [open_event(setup("b")), close_event("b", 108_410_000, ms=T0)], cfg)
        self.assertEqual((ledger[1]["amount_e8"], state["positions"]), (2_000_000_000, {}))
        # an unrelated position's exit still runs before a new open in the same minute
        opened, _ = step(initial_state(cfg), [open_event(setup("c", entry_ms=T0 - MINUTE), ms=T0 - MINUTE)], cfg)
        _, ledger = step(opened, [open_event(setup("d")), close_event("c", 108_410_000, ms=T0)], cfg)
        self.assertEqual([(l["type"], l["setup_id"]) for l in ledger],
                         [("pnl", "c"), ("close_fee", "c"), ("open_fee", "d")])


class SwapTests(unittest.TestCase):
    def swap(self, symbol="EURUSD", ms=T0 + MINUTE, long_rate="-1/50000", short_rate="1/200000", conv=ONE, mult=3):
        return {"type": "swap", "ms": ms, "symbol": symbol, "multiplier": mult, "long_per_unit": long_rate,
                "short_per_unit": short_rate, "quote_to_account": conv}

    def test_long_short_and_timing(self):
        cfg = config()
        state, ledger = step(initial_state(cfg), [open_event(setup()), self.swap()], cfg)
        self.assertEqual([(l["type"], l["amount_e8"], l["kind"], l["multiplier"]) for l in ledger[1:]],
                         [("funding", -30_000_000, "swap", 3)])  # 5000 x -0.00002 x 3 = -0.30 USD
        short = setup("s", side=-1, fill=108_000_000, stop=108_200_000, target=107_600_000)
        state, ledger = step(initial_state(cfg), [open_event(short), self.swap(mult=1)], cfg)
        self.assertEqual(ledger[-1]["amount_e8"], 2_500_000)  # 5000 x 0.000005 = +0.025 USD received
        state, ledger = step(initial_state(cfg), [open_event(setup()), self.swap(ms=T0)], cfg)
        self.assertEqual([l["type"] for l in ledger], ["open_fee"])  # entry_ms >= swap ms: no swap

    def test_swap_is_charged_once_and_on_an_exit_at_the_rollover(self):
        cfg = config()
        swap = self.swap()
        state, ledger = step(initial_state(cfg), [open_event(setup()), swap, dict(swap)], cfg)
        self.assertEqual([l["type"] for l in ledger], ["open_fee", "funding"])  # duplicate in one batch
        again, nothing = step(state, [swap], cfg)
        self.assertEqual((nothing, again["balance_e8"]), ([], state["balance_e8"]))  # replayed later
        opened, _ = step(initial_state(cfg), [open_event(setup())], cfg)
        closed, ledger = step(opened, [close_event("a", 108_410_000, ms=T0 + MINUTE), swap], cfg)
        self.assertEqual([l["type"] for l in ledger], ["funding", "pnl", "close_fee"])  # exit at rollover: charged
        _, ledger = step(initial_state(cfg), [open_event(setup(entry_ms=T0 + MINUTE), ms=T0 + MINUTE), swap], cfg)
        self.assertEqual([l["type"] for l in ledger], ["open_fee"])  # entry at rollover: not charged

    def test_jpy_quote_converts_to_the_account_currency(self):
        cfg = config()
        jpy = setup("j", fill=15_000_000_000, stop=14_970_000_000, target=15_060_000_000, symbol="USDJPY")
        conv = "1/150"
        state, ledger = step(initial_state(cfg), [open_event(jpy, conv=conv),
                                                  self.swap("USDJPY", long_rate="3/1000", conv=conv, mult=1)], cfg)
        # 10 USD / (0.30 JPY x 1/150 USD per JPY) = 5000 units; margin 5000 x 150 / 100 / 150 = 50 USD
        self.assertEqual(state["positions"]["j"]["units"], "5000")
        self.assertEqual(state["positions"]["j"]["margin_e8"], 50 * E8)
        self.assertEqual(ledger[-1]["amount_e8"], 10_000_000)  # 5000 x 0.003 JPY = 15 JPY = 0.10 USD


class StopOutTests(unittest.TestCase):
    def bars(self, lows=(), highs=(), minutes=10, start=T0, price=108_010_000):
        cols = {name: array("q", [price]) * minutes for name in ("open", "high", "low", "close")}
        cols["spread"], cols["tick_volume"] = array("q", [10_000]) * minutes, array("q", [1]) * minutes
        for i, value in lows:
            cols["low"][i] = value
        for i, value in highs:
            cols["high"][i] = value
        return FxBarSeries("fx:EURUSD", "exness-mt5-demo", "EURUSD", "fx-week-ny17-v1", start, minutes, 1000,
                           flags=array("H", [FLAG_PRESENT]) * minutes, **cols)

    def test_long_and_short_stop_out_cap_and_later_close(self):
        cfg = config()
        state, first = step(initial_state(cfg), [open_event(setup())], cfg)
        bars = self.bars(lows=[(2, 107_469_951), (3, 107_469_950), (5, 100_000_000)])
        events = stop_out_events(state["positions"], bars, {}, ONE)
        self.assertEqual(events, [{"type": "liquidation", "ms": T0 + 3 * MINUTE, "setup_id": "a",
                                   "quote_to_account": ONE}])
        after, ledger = step(state, events + [close_event("a", 108_410_000, ms=T0 + 3 * MINUTE)], cfg)
        self.assertEqual([(l["type"], l["amount_e8"]) for l in ledger], [("liquidation", -2_700_250_000)])
        self.assertLessEqual(-ledger[0]["amount_e8"], state["positions"]["a"]["margin_e8"])
        self.assertEqual((after["positions"], after["used_margin_e8"]), ({}, 0))
        assert_balanced(self, cfg, after, [first, ledger])

        short = setup("s", side=-1, fill=108_000_000, stop=108_200_000, target=107_600_000)
        state, _ = step(initial_state(cfg), [open_event(short)], cfg)
        self.assertEqual(state["positions"]["s"]["stop_out_price"], "108540000")
        bars = self.bars(highs=[(4, 108_530_000)], price=108_000_000)  # ask high = 108_540_000
        self.assertEqual([e["ms"] for e in stop_out_events(state["positions"], bars, {}, ONE)], [T0 + 4 * MINUTE])


class GuardTests(unittest.TestCase):
    def test_caps(self):
        cfg = config(max_positions=1)
        _, ledger = step(initial_state(cfg), [open_event(setup("a")), open_event(setup("b"))], cfg)
        self.assertEqual(ledger[-1]["reason"], "cap:max_positions")
        cfg = config(max_exposure_multiple=Fraction(5))  # 5400.50 > 5 x 1000
        _, ledger = step(initial_state(cfg), [open_event(setup())], cfg)
        self.assertEqual(ledger[-1]["reason"], "cap:total_exposure")
        cfg = config(risk_fraction=Fraction(1, 5), max_exposure_multiple=Fraction(1000))  # margin 1080.10 > 1000
        _, ledger = step(initial_state(cfg), [open_event(setup())], cfg)
        self.assertEqual(ledger[-1]["reason"], "insufficient_margin")
        cfg = config()
        _, ledger = step(initial_state(cfg), [open_event(setup(status="G")), open_event(setup("x", symbol="GBPUSD"))], cfg)
        self.assertEqual([l["reason"] for l in ledger], ["not_tradeable", "no_contract"])

    def test_idempotent_pure_and_contiguous(self):
        cfg = config()
        start = initial_state(cfg)
        snapshot = deepcopy(start)
        state, first = step(start, [open_event(setup())], cfg)
        self.assertEqual(start, snapshot)  # input state not mutated
        same, nothing = step(state, [open_event(setup())], cfg)
        self.assertEqual((nothing, same), ([], state))
        closed, second = step(state, [close_event("a", 108_410_000)], cfg)
        again, none = step(closed, [close_event("a", 108_410_000), open_event(setup())], cfg)
        self.assertEqual((none, again), ([], closed))
        assert_balanced(self, cfg, closed, [first, second])
        with self.assertRaises(ValueError):
            step(state, [{"type": "funding", "ms": T0}], cfg)
        self.assertEqual(cfg.to_record()["contracts"][0]["contract_size"], "100000")


if __name__ == "__main__":
    unittest.main()


class SwapPruneTests(unittest.TestCase):
    swap = SwapTests.swap

    def test_close_prunes_only_that_setups_keys_and_idempotence_holds(self):
        cfg = config()
        other = setup("b")
        first = self.swap()
        state, l1 = step(initial_state(cfg), [open_event(setup()), open_event(other), first], cfg)
        self.assertEqual(sorted(state["swaps"]), [f"a|{T0 + MINUTE}", f"b|{T0 + MINUTE}"])
        closed, l2 = step(state, [close_event("a", 108_410_000, ms=T0 + 2 * MINUTE)], cfg)
        self.assertFalse([key for key in closed["swaps"] if key.startswith("a|")])
        self.assertEqual(closed["swaps"], [f"b|{T0 + MINUTE}"])
        again, nothing = step(closed, [first], cfg)  # replayed rollover for the still-open "b"
        self.assertEqual((nothing, again["balance_e8"]), ([], closed["balance_e8"]))
        assert_balanced(self, cfg, again, [l1, l2, nothing])

    def test_exit_and_swap_at_the_same_ms_still_charge_swap_first(self):
        cfg = config()
        opened, l1 = step(initial_state(cfg), [open_event(setup())], cfg)
        closed, l2 = step(opened, [close_event("a", 108_410_000, ms=T0 + MINUTE), self.swap()], cfg)
        self.assertEqual([l["type"] for l in l2], ["funding", "pnl", "close_fee"])
        self.assertEqual(closed["swaps"], [])
        assert_balanced(self, cfg, closed, [l1, l2])
