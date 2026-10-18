"""Forex symbol spec rows (#432): strict mapping to VenueSymbol and FxContract with exact decimals.

Every spec here is SYNTHETIC (a 5-digit pair and a 2-digit metal), not a broker's real values.
"""

from fractions import Fraction
import unittest

from market_analysis.fx.costs import FxContract
from market_analysis.fx.specs_rows import fx_contract_from_spec_row, fx_point
from market_analysis.instruments import VenueSymbol


def spec(**overrides):
    row = {"venue": "test-venue", "instrument_id": "fx:EURUSD", "native_symbol": "EURUSDm", "digits": 5,
           "point": 0.00001, "tick_size": 0.00001, "tick_value": 1, "contract_size": 100000, "volume_min": 0.01,
           "volume_max": 200, "volume_step": 0.01, "base_currency": "EUR", "profit_currency": "USD",
           "margin_currency": "EUR", "account_currency": "USD", "trade_mode": 4, "swap_mode": 1,
           "swap_long": -7.3, "swap_short": 2.1, "swap_rollover3days": 3, "spec_hash": "a" * 64,
           "observed_at_ms": 1_792_400_000_000}
    row.update(overrides)
    return row


GOLD = spec(instrument_id="fx:XAUUSD", native_symbol="XAUUSDm", digits=2, point=0.01, tick_size=0.01,
            contract_size=100, volume_min=0.01, volume_step=0.01, base_currency="XAU", margin_currency="XAU")


class BuilderTests(unittest.TestCase):
    def test_five_digit_pair(self):
        venue_symbol, contract = fx_contract_from_spec_row(spec())
        self.assertEqual(venue_symbol, VenueSymbol("test-venue", "EURUSDm", "fx:EURUSD", 5, Fraction(1, 100000),
                                                   Fraction(100000)))
        self.assertIsInstance(contract, FxContract)
        self.assertEqual((contract.contract_size, contract.tick_size, contract.min_lot, contract.lot_step),
                         (Fraction(100000), Fraction(1, 100000), Fraction(1, 100), Fraction(1, 100)))
        self.assertEqual((contract.base, contract.quote), ("EUR", "USD"))
        self.assertEqual(fx_point(spec()), Fraction(1, 100000))

    def test_two_digit_metal(self):
        venue_symbol, contract = fx_contract_from_spec_row(GOLD)
        self.assertEqual((venue_symbol.digits, venue_symbol.tick_size, venue_symbol.contract_size),
                         (2, Fraction(1, 100), Fraction(100)))
        self.assertEqual((contract.base, contract.quote), ("XAU", "USD"))
        self.assertEqual(fx_point(GOLD), Fraction(1, 100))

    def test_values_are_exact_decimals_not_float_arithmetic(self):
        # 0.1 as a float is not 1/10; the decimal text of the JSON number is.
        _, contract = fx_contract_from_spec_row(spec(volume_min=0.1, volume_step=0.1))
        self.assertEqual(contract.min_lot, Fraction(1, 10))
        self.assertNotEqual(Fraction(0.1), Fraction(1, 10))


class StrictTests(unittest.TestCase):
    def test_a_point_that_is_not_ten_to_minus_digits_raises(self):
        for row in (spec(point=0.0001), spec(digits=3), GOLD | {"point": 0.001}):
            with self.subTest(row=row), self.assertRaises(ValueError):
                fx_contract_from_spec_row(row)
            with self.assertRaises(ValueError):
                fx_point(row)

    def test_a_bad_key_set_raises(self):
        missing = spec()
        del missing["swap_long"]
        for row in (missing, spec(extra=1), spec(observation_id=7), "not a row", None):
            with self.subTest(row=row), self.assertRaises(ValueError):
                fx_contract_from_spec_row(row)

    def test_a_bool_or_text_where_a_number_is_required_raises(self):
        for row in (spec(tick_size=True), spec(digits=True), spec(contract_size="100000"), spec(digits=5.0),
                    spec(volume_min=None), spec(base_currency=""), spec(observed_at_ms=1.5)):
            with self.subTest(row=row), self.assertRaises(ValueError):
                fx_contract_from_spec_row(row)


if __name__ == "__main__":
    unittest.main()
