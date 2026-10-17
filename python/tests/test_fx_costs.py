"""Forex CFD cost model (#374): bid/ask fills, triggers, commission, swap, sizing, stop-out."""

from dataclasses import fields, replace
from fractions import Fraction as F
import unittest

from market_analysis.benchmark.costs import COST_MODEL_V1
from market_analysis.fx import costs as fx
from market_analysis.instruments import VenueSymbol

LONG, SHORT = 1, -1
ZERO_SLIP = fx.FxCostModel(version="zero-slip", market_slip_spread_fraction=0, market_slip_range_mult=0)
EURUSD = fx.FxContract(F(100_000), F("0.00001"), F("0.01"), F("0.01"), "EUR", "USD")
SPREAD = F("0.00010")


class FillTests(unittest.TestCase):
    def test_entry_reference_and_fill(self):
        self.assertEqual(fx.ask(F("1.08"), SPREAD), F("1.08010"))
        p0 = fx.entry_reference(LONG, F("1.08000"), SPREAD)
        self.assertEqual(p0, F("1.08010"))
        high, low = F("1.08040"), F("1.07980")  # range 0.00060
        self.assertEqual(fx.entry_fill_price(LONG, p0, SPREAD, high, low, ZERO_SLIP), F("1.08010"))
        slip = fx.market_slippage(SPREAD, high, low, fx.FX_COST_MODEL_V1)
        self.assertEqual(slip, F("0.000030"))  # max(0.00010/10, 0.00060/20)
        self.assertEqual(fx.entry_fill_price(LONG, p0, SPREAD, high, low, fx.FX_COST_MODEL_V1), F("1.08013"))
        short_p0 = fx.entry_reference(SHORT, F("1.08000"), SPREAD)
        self.assertEqual(short_p0, F("1.08000"))
        self.assertEqual(fx.entry_fill_price(SHORT, short_p0, SPREAD, high, low, fx.FX_COST_MODEL_V1),
                         F("1.08000") - slip)
        self.assertEqual(fx.market_slippage(SPREAD, high, low, fx.with_multiplier(fx.FX_COST_MODEL_V1, 2)), 2 * slip)

    def test_market_exit_takes_the_right_side_of_the_book(self):
        high, low = F("1.08040"), F("1.07980")
        slip = fx.market_slippage(SPREAD, high, low, fx.FX_COST_MODEL_V1)
        self.assertEqual(fx.exit_price_at_market(LONG, F("1.08"), SPREAD), F("1.08"))
        self.assertEqual(fx.exit_price_at_market(SHORT, F("1.08"), SPREAD), F("1.08010"))
        self.assertEqual(fx.market_exit_fill(LONG, F("1.08"), SPREAD, high, low, fx.FX_COST_MODEL_V1), F("1.08") - slip)
        self.assertEqual(fx.market_exit_fill(SHORT, F("1.08"), SPREAD, high, low, fx.FX_COST_MODEL_V1),
                         F("1.08010") + slip)

    def test_stop_fills_from_the_worse_of_stop_and_open(self):
        high, low = F("1.07850"), F("1.07750")
        slip = fx.stop_slippage(SPREAD, high, low, fx.FX_COST_MODEL_V1)
        self.assertEqual(slip, F("0.00025"))  # max(0.00010/2, 0.00100/4)
        self.assertEqual(fx.stop_fill_price(LONG, F("1.07810"), F("1.07800"), SPREAD, high, low, fx.FX_COST_MODEL_V1),
                         F("1.07800") - slip)  # gap through the stop
        self.assertEqual(fx.stop_fill_price(LONG, F("1.07810"), F("1.07900"), SPREAD, high, low, fx.FX_COST_MODEL_V1),
                         F("1.07810") - slip)
        self.assertEqual(fx.stop_fill_price(SHORT, F("1.08200"), F("1.08210"), SPREAD, high, low, fx.FX_COST_MODEL_V1),
                         F("1.08220") + slip)  # ask open above the stop
        self.assertEqual(fx.stop_fill_price(SHORT, F("1.08200"), F("1.08100"), SPREAD, high, low, fx.FX_COST_MODEL_V1),
                         F("1.08200") + slip)
        self.assertEqual(fx.target_fill_price(F("1.09")), F("1.09"))

    def test_trigger_table_with_equality(self):
        high, low = F("1.0820"), F("1.0800")
        self.assertTrue(fx.stop_hit(LONG, F("1.0800"), SPREAD, high, low))
        self.assertFalse(fx.stop_hit(LONG, F("1.07999"), SPREAD, high, low))
        self.assertTrue(fx.target_hit(LONG, F("1.0820"), SPREAD, high, low))
        self.assertFalse(fx.target_hit(LONG, F("1.08201"), SPREAD, high, low))
        self.assertTrue(fx.stop_hit(SHORT, F("1.0821"), SPREAD, high, low))  # high + spread == stop
        self.assertFalse(fx.stop_hit(SHORT, F("1.08211"), SPREAD, high, low))
        self.assertTrue(fx.target_hit(SHORT, F("1.0801"), SPREAD, high, low))  # low + spread == target
        self.assertFalse(fx.target_hit(SHORT, F("1.08009"), SPREAD, high, low))

    def test_bad_inputs(self):
        for bad_side in (True, 0, 2, 1.0):
            with self.assertRaises(ValueError):
                fx.entry_reference(bad_side, F("1.08"), SPREAD)
        with self.assertRaises(ValueError):
            fx.ask(1.08, SPREAD)
        with self.assertRaises(ValueError):
            fx.ask(F("1.08"), F("-0.0001"))
        with self.assertRaises(ValueError):
            fx.market_slippage(SPREAD, F("1.07"), F("1.08"), fx.FX_COST_MODEL_V1)
        self.assertEqual(fx.ask(F("1.08"), 0), F("1.08"))  # a zero spread (Raw account) is allowed


class CashTests(unittest.TestCase):
    def test_commission_and_swap(self):
        model = replace(fx.FX_COST_MODEL_V1, commission_per_lot_side=F("3.50"))
        self.assertEqual(fx.commission_cash(2000, EURUSD, model), F("0.07"))
        self.assertEqual(fx.commission_cash(2000, EURUSD, fx.FX_COST_MODEL_V1), 0)
        rates = fx.SwapRates(F("-0.00002"), F("0.000005"))
        self.assertEqual(fx.swap_cash_quote(LONG, 1000, 3, rates, fx.FX_COST_MODEL_V1), F("-0.06"))
        self.assertEqual(fx.swap_cash_quote(SHORT, 1000, 1, rates, fx.FX_COST_MODEL_V1), F("0.005"))
        with self.assertRaises(ValueError):
            fx.swap_cash_quote(LONG, 1000, 0, rates, fx.FX_COST_MODEL_V1)

    def test_quote_to_account_rate(self):
        self.assertEqual(fx.quote_to_account_rate("USD", "USD", {}), 1)
        self.assertEqual(fx.quote_to_account_rate("JPY", "USD", {"USDJPY": 150}), F(1, 150))
        self.assertEqual(fx.quote_to_account_rate("GBP", "USD", {"GBPUSD": F("1.25")}), F("1.25"))
        with self.assertRaises(ValueError):
            fx.quote_to_account_rate("CHF", "USD", {"GBPUSD": F("1.25")})


class SizingTests(unittest.TestCase):
    def test_units_for_risk(self):
        self.assertIsNone(fx.units_for_risk(1, F("0.0020"), 1, EURUSD))  # 500 units < 0.01 lot
        self.assertEqual(fx.units_for_risk(10, F("0.0020"), 1, EURUSD), 5000)
        self.assertEqual(fx.units_for_risk(F("10.80"), F("0.0020"), 1, EURUSD), 5000)  # 5400 floored

    def test_margin_leverage_and_stop_out(self):
        self.assertEqual(fx.required_margin(5000, F("1.08"), 100, 1), F("54"))
        self.assertEqual(fx.max_admissible_leverage(F("1.08"), F("0.0020"), fx.FX_COST_MODEL_V1), 100)  # 135 capped
        self.assertEqual(fx.max_admissible_leverage(2650, 30, fx.FX_COST_MODEL_V1), 22)
        self.assertIsNone(fx.max_admissible_leverage(F("1.08"), F("1"), fx.FX_COST_MODEL_V1))
        self.assertEqual(fx.stop_out_price(LONG, F("1.08"), 100, fx.FX_COST_MODEL_V1), F("1.0746"))
        self.assertEqual(fx.stop_out_price(SHORT, F("1.08"), 100, fx.FX_COST_MODEL_V1), F("1.0854"))

    def test_contract_from_broker_only(self):
        with self.assertRaisesRegex(ValueError, "must come from the broker"):
            fx.FxContract.from_venue_symbol(VenueSymbol("exness-mt5-demo", "EURUSDm", "fx:EURUSD"),
                                            min_lot=F("0.01"), lot_step=F("0.01"), base="EUR", quote="USD")
        symbol = VenueSymbol("exness-mt5-demo", "EURUSDm", "fx:EURUSD", digits=5,
                             tick_size=F(1, 100_000), contract_size=F(100_000))
        contract = fx.FxContract.from_venue_symbol(symbol, min_lot=F("0.01"), lot_step=F("0.01"),
                                                   base="EUR", quote="USD")
        self.assertEqual(contract, EURUSD)
        with self.assertRaises(ValueError):
            fx.FxContract(F(100_000), F("0.00001"), F("0.015"), F("0.01"), "EUR", "USD")


class ModelTests(unittest.TestCase):
    def test_validation_record_and_identity(self):
        for bad in ({"version": ""}, {"stop_out_level": F(1)}, {"cost_multiplier": F(-1)},
                    {"leverage_cap": 0}, {"stop_out_buffer_multiple": True}, {"account_currency": "usd"}):
            with self.assertRaises(ValueError):
                replace(fx.FX_COST_MODEL_V1, **bad)
        record = fx.FX_COST_MODEL_V1.to_record()
        self.assertEqual(record["market_slip_spread_fraction"], "1/10")
        self.assertEqual(record["commission_per_lot_side"], "0")
        self.assertEqual(record["leverage_cap"], 100)
        identity = fx.FX_COST_MODEL_V1.identity()
        self.assertEqual(identity, fx.FxCostModel(version="fx-cost-v1").identity())
        changes = {"version": "x", "account_currency": "EUR", "leverage_cap": 50, "stop_out_buffer_multiple": 3}
        for field in fields(fx.FxCostModel):
            value = (changes[field.name] if field.name in changes
                     else getattr(fx.FX_COST_MODEL_V1, field.name) + F(1, 7))
            changed = replace(fx.FX_COST_MODEL_V1, **{field.name: value})
            self.assertNotEqual(changed.identity(), identity, field.name)
        self.assertEqual(replace(fx.FX_COST_MODEL_V1, cost_multiplier=2).cost_multiplier, F(2))
        self.assertEqual(fx.with_multiplier(fx.FX_COST_MODEL_V1, F(3, 2)).cost_multiplier, F(3, 2))
        for bad in (0.1, True, "2"):
            with self.assertRaises(ValueError):
                fx.with_multiplier(fx.FX_COST_MODEL_V1, bad)
        self.assertTrue(fx.ASSUMPTIONS_FX_V1)
        self.assertEqual(COST_MODEL_V1.version, "cost-v1")  # the Binance perpetual model is untouched


if __name__ == "__main__":
    unittest.main()
