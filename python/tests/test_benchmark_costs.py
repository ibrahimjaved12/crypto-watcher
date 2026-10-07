"""Exact cost, fill, funding and liquidation math for the #182 benchmark harness."""
from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
import unittest

from market_analysis import futures_execution
from market_analysis.benchmark.costs import (
    ASSUMPTIONS_V1, COST_MODEL_V1, CostModel, entry_fill_price, fee, funding_cash, liquidation_loss,
    liquidation_price, market_slippage, max_admissible_leverage, stop_fill_price, stop_slippage, with_multiplier,
)
from market_analysis.futures_execution_contracts import (
    Bracket, BracketTable, Evidence, FeePolicy, FeeRole, PositionSide, Provenance, Scope,
)

MODEL = COST_MODEL_V1
LONG, SHORT = 1, -1
D = Decimal
EVIDENCE = Evidence(Provenance.FIXED_SIMULATION_ASSUMPTION, "fixture", "v1")
SCOPE = Scope("binance-usdm:BTCUSDT")


def as_fraction(value) -> Fraction:
    """futures_execution results are finite Decimals or ExactScalars; both give an integer ratio."""
    return Fraction(*value.as_integer_ratio())


class SlippageAndFillTests(unittest.TestCase):
    def test_market_and_stop_slippage(self):
        # floor = 10000 * 1bps = 1; range = (10010 - 9990) / 10 = 2
        self.assertEqual(market_slippage(10_000, 10_010, 9_990, MODEL), Fraction(2))
        self.assertEqual(market_slippage(10_000, 10_000, 10_000, MODEL), Fraction(1))  # floor wins
        # floor = 10000 * 2bps = 2; range = 20 / 4 = 5
        self.assertEqual(stop_slippage(10_000, 10_010, 9_990, MODEL), Fraction(5))
        self.assertEqual(stop_slippage(10_003, 10_003, 10_003, MODEL), Fraction(10_003 * 2, 10_000))
        with self.assertRaises(ValueError):
            market_slippage(10_000, 9_990, 10_010, MODEL)  # high below low
        with self.assertRaises(ValueError):
            market_slippage(-(2 ** 63), 10_010, 9_990, MODEL)  # MISSING is never a price

    def test_entry_and_stop_fills_including_gap_through(self):
        self.assertEqual(entry_fill_price(LONG, 10_000, 10_010, 9_990, MODEL), Fraction(10_002))
        self.assertEqual(entry_fill_price(SHORT, 10_000, 10_010, 9_990, MODEL), Fraction(9_998))
        # Long stop at 9995: no gap -> 9995 - 5; gap open 9980 -> 9980 - 5.
        self.assertEqual(stop_fill_price(LONG, 9_995, 10_000, 10_010, 9_990, MODEL), Fraction(9_990))
        self.assertEqual(stop_fill_price(LONG, 9_995, 9_980, 9_990, 9_970, MODEL), Fraction(9_980 - 5))
        # Short stop at 10005: no gap -> 10005 + 5; gap open 10020 -> 10020 + 5.
        self.assertEqual(stop_fill_price(SHORT, 10_005, 10_000, 10_010, 9_990, MODEL), Fraction(10_010))
        self.assertEqual(stop_fill_price(SHORT, 10_005, 10_020, 10_030, 10_010, MODEL), Fraction(10_025))
        self.assertEqual(stop_fill_price(LONG, Fraction(19_991, 2), 10_000, 10_010, 9_990, MODEL),
                         Fraction(19_991, 2) - 5)
        for side in (0, 2, True):
            with self.subTest(side=side), self.assertRaises(ValueError):
                entry_fill_price(side, 10_000, 10_010, 9_990, MODEL)


class FeeAndFundingTests(unittest.TestCase):
    def test_fee_and_funding_signs(self):
        self.assertEqual(fee(10_002, MODEL.taker_rate, MODEL), Fraction(5_001, 1_000))
        self.assertEqual(fee(Fraction(10_001, 2), MODEL.maker_rate, MODEL), Fraction(10_001, 10_000))
        rate = Fraction(1, 10_000)
        self.assertEqual(funding_cash(LONG, 10_000, rate, MODEL), Fraction(-1))  # longs pay a positive rate
        self.assertEqual(funding_cash(SHORT, 10_000, rate, MODEL), Fraction(1))
        self.assertEqual(funding_cash(LONG, 10_000, -rate, MODEL), Fraction(1))  # and receive a negative one
        self.assertEqual(funding_cash(SHORT, 10_000, Fraction(98, 10 ** 8), MODEL), Fraction(98, 10_000))

    def test_cost_grid(self):
        for multiplier in (0, 1, 2, 3):
            model = with_multiplier(MODEL, multiplier)
            with self.subTest(multiplier=multiplier):
                self.assertEqual(model.cost_multiplier, Fraction(multiplier))
                self.assertEqual(fee(10_000, model.taker_rate, model), 5 * multiplier)
                self.assertEqual(market_slippage(10_000, 10_010, 9_990, model), 2 * multiplier)
                self.assertEqual(stop_slippage(10_000, 10_010, 9_990, model), 5 * multiplier)
                self.assertEqual(funding_cash(LONG, 10_000, Fraction(1, 10_000), model), -1)  # funding unaffected
        self.assertEqual(with_multiplier(MODEL, Fraction(1, 2)).cost_multiplier, Fraction(1, 2))
        self.assertEqual(MODEL.cost_multiplier, 1)  # the original is unchanged

    def test_record_identity_and_assumptions(self):
        record = MODEL.to_record()
        self.assertEqual(record["version"], "cost-v1")
        self.assertEqual((record["taker_rate"], record["maker_rate"], record["market_slip_range_mult"]),
                         ("1/2000", "1/5000", "1/10"))
        self.assertEqual((record["market_slip_floor_bps"], record["leverage_cap"], record["liq_buffer_multiple"]),
                         ("1", 20, 3))
        self.assertEqual(MODEL.identity(), MODEL.identity())
        self.assertEqual(len(MODEL.identity()), 64)
        self.assertEqual(MODEL.identity(), CostModel(**{name: getattr(MODEL, name) for name in record}).identity())
        self.assertNotEqual(with_multiplier(MODEL, 2).identity(), MODEL.identity())
        text = " ".join(ASSUMPTIONS_V1)
        self.assertIn("VIP0", text)
        for parameter in ("liquidation_fee_rate", "mmr", "leverage_cap", "slippage"):
            self.assertIn(parameter, text)
        self.assertIn("UNVERIFIED", text)
        for changes in ({"taker_rate": 0.0005}, {"mmr": Fraction(1)}, {"leverage_cap": 0}, {"version": ""},
                        {"cost_multiplier": Fraction(-1)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                CostModel(**{**{name: getattr(MODEL, name) for name in record}, **changes})


class LiquidationTests(unittest.TestCase):
    def test_hand_computed_liquidation_prices(self):
        # Long 10x at 10000, mmr 1%: WB 1000; LP = (1000 - 10000) / (1/100 - 1) = 100000/11
        self.assertEqual(liquidation_price(LONG, 10_000, 10, Fraction(1, 100)), Fraction(100_000, 11))
        # Short 10x: LP = (1000 + 10000) / (1/100 + 1) = 1100000/101
        self.assertEqual(liquidation_price(SHORT, 10_000, 10, Fraction(1, 100)), Fraction(1_100_000, 101))
        self.assertEqual(liquidation_price(LONG, 10_000, 1, 0), 0)  # 1x, no maintenance: liquidated at zero
        for leverage in (0, 1.5, True):
            with self.subTest(leverage=leverage), self.assertRaises(ValueError):
                liquidation_price(LONG, 10_000, leverage, Fraction(1, 100))

    def test_max_admissible_leverage_exact_examples(self):
        # need = 3 * 300 = 900. Long: 1/L >= 1/100 + 900 * 99/100 / 10000 = 0.0991 -> L = 10.
        self.assertEqual(max_admissible_leverage(LONG, 10_000, 300, MODEL), 10)
        # Short: 1/L >= 1/100 + 900 * 101/100 / 10000 = 0.1009 -> L = 9.
        self.assertEqual(max_admissible_leverage(SHORT, 10_000, 300, MODEL), 9)
        self.assertEqual(max_admissible_leverage(LONG, 10_000, 100, MODEL), 20)  # capped
        self.assertIsNone(max_admissible_leverage(LONG, 10_000, 4_000, MODEL))  # even 1x fails
        self.assertEqual(max_admissible_leverage(LONG, 10_000, 0, MODEL), 20)

    def test_max_admissible_leverage_properties(self):
        for side in (LONG, SHORT):
            for entry in (10_000, 9_731, Fraction(123_457, 3)):
                for distance in (1, 37, 150, Fraction(901, 3), 500, 1_200, 3_000):
                    need = MODEL.liq_buffer_multiple * Fraction(distance)

                    def gap(leverage):  # signed entry-to-liquidation distance on the losing side
                        return side * (Fraction(entry) - liquidation_price(side, entry, leverage, MODEL.mmr))

                    leverage = max_admissible_leverage(side, entry, distance, MODEL)
                    with self.subTest(side=side, entry=entry, distance=distance, leverage=leverage):
                        if leverage is None:
                            self.assertLess(gap(1), need)
                            continue
                        self.assertTrue(1 <= leverage <= MODEL.leverage_cap)
                        self.assertGreaterEqual(gap(leverage), need)
                        if leverage < MODEL.leverage_cap:
                            self.assertLess(gap(leverage + 1), need)

    def test_liquidation_loss(self):
        lp = liquidation_price(LONG, 10_000, 10, MODEL.mmr)
        self.assertEqual(liquidation_loss(LONG, 10_000, lp, MODEL), (10_000 - lp) + lp / 80)
        short_lp = liquidation_price(SHORT, 10_000, 10, MODEL.mmr)
        self.assertEqual(liquidation_loss(SHORT, 10_000, short_lp, MODEL), (short_lp - 10_000) + short_lp / 80)
        self.assertEqual(liquidation_loss(LONG, 10_000, lp, with_multiplier(MODEL, 0)), 10_000 - lp)

    def test_liquidation_loss_is_capped_at_the_isolated_wallet_balance(self):
        lp = liquidation_price(LONG, 10_000, 10, MODEL.mmr)
        uncapped = liquidation_loss(LONG, 10_000, lp, MODEL)
        wallet = Fraction(10_000, 10)
        self.assertGreater(uncapped, wallet)  # 1.25% fee > 1% maintenance rate: the raw loss exceeds the margin
        self.assertEqual(liquidation_loss(LONG, 10_000, lp, MODEL, leverage=10), wallet)
        self.assertEqual(liquidation_loss(LONG, 10_000, lp, MODEL, leverage=1), uncapped)
        with self.assertRaises(ValueError):
            liquidation_loss(LONG, 10_000, lp, MODEL, leverage=0)


class CrossCheckTests(unittest.TestCase):
    """Exact equality with the existing #37 futures_execution math."""

    def test_fee_matches_futures_execution(self):
        policy = FeePolicy(D("0.0002"), D("0.0005"), EVIDENCE)
        for fill, role, rate in ((D("10002"), FeeRole.TAKER, MODEL.taker_rate),
                                 (D("5000.5"), FeeRole.MAKER, MODEL.maker_rate),
                                 (D("0.00012345"), FeeRole.TAKER, MODEL.taker_rate)):
            with self.subTest(fill=fill, role=role):
                expected = futures_execution.fee(D(1), fill, role, policy).value
                self.assertEqual(fee(Fraction(fill), rate, MODEL), as_fraction(expected))

    def test_funding_matches_futures_execution(self):
        for side, position in ((LONG, PositionSide.LONG), (SHORT, PositionSide.SHORT)):
            for rate, mark in ((D("0.0001"), D("10000")), (D("-0.00025"), D("0.3512")), (D("0.00000098"), D("97000.1"))):
                with self.subTest(side=side, rate=rate, mark=mark):
                    expected = futures_execution.funding(position, D(1), rate, mark).value
                    self.assertEqual(funding_cash(side, Fraction(mark), Fraction(rate), MODEL), as_fraction(expected))

    def test_liquidation_price_matches_futures_execution(self):
        table = BracketTable(SCOPE, (Bracket("1", D(0), D(10 ** 12), 20, D("0.01"), D(0)),), EVIDENCE)
        for side, position in ((LONG, PositionSide.LONG), (SHORT, PositionSide.SHORT)):
            for entry, leverage in ((D("10000"), 10), (D("97000.5"), 4), (D("0.3512"), 8), (D("2500"), 1)):
                with self.subTest(side=side, entry=entry, leverage=leverage):
                    wallet = entry / leverage  # finite decimal for these inputs
                    result = futures_execution.liquidation_threshold(position, D(1), entry, wallet, table)
                    ours = liquidation_price(side, Fraction(entry), leverage, Fraction(1, 100))
                    if ours <= 0:  # 1x long: no positive threshold exists in the existing code
                        self.assertEqual(result.status, "NO_POSITIVE_THRESHOLD")
                        continue
                    self.assertEqual(result.status, "VALID")
                    self.assertEqual(ours, as_fraction(result.value))


if __name__ == "__main__":
    unittest.main()
