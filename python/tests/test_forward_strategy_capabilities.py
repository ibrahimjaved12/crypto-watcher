"""Strategy capability declarations (#374): every strategy declared, checks fail closed."""

import unittest

from market_analysis.benchmark import ta_strategies as ta
from market_analysis.forward.signals import FORWARD_STRATEGIES
from market_analysis.forward.strategy_capabilities import (
    TA_CAPABILITIES, capabilities_for, compatibility_for, compatible_strategy_ids,
)
from market_analysis.instruments import check_compatibility, crypto_instrument, forex_instrument

BTC = crypto_instrument("BTCUSDT")
EUR = forex_instrument("EURUSD")


class CapabilityTests(unittest.TestCase):
    def test_every_strategy_is_declared(self):
        self.assertEqual(set(ta.STRATEGIES) - set(TA_CAPABILITIES), set())
        for strategy_id in FORWARD_STRATEGIES:
            self.assertIsNotNone(capabilities_for(strategy_id), strategy_id)

    def test_forward_registry_runs_on_both_markets(self):
        self.assertEqual(len(FORWARD_STRATEGIES), 36)
        for instrument in (BTC, EUR):
            ok, refused = compatible_strategy_ids(instrument)
            self.assertEqual((len(ok), refused), (36, {}), instrument.instrument_id)

    def test_doji_is_crypto_only(self):
        for name in ("doji_continuation_v1", "doji_reversal_v1"):
            self.assertTrue(check_compatibility(TA_CAPABILITIES[name], BTC).ok)
            result = check_compatibility(TA_CAPABILITIES[name], EUR)
            self.assertEqual((result.ok, result.reasons), (False, ("market_not_supported:forex",)))

    def test_placebo_inherits_and_unknown_fails_closed(self):
        self.assertEqual(capabilities_for("placebo-v1:ema_cross_20_50:60"), capabilities_for("ema_cross_20_50:60"))
        self.assertIsNone(capabilities_for("nope:15"))
        result = compatibility_for("nope:15", BTC)
        self.assertEqual((result.ok, result.reasons), (False, ("no_capabilities_declared",)))
        ok, refused = compatible_strategy_ids(EUR, ["ema_cross_20_50:60", "nope:15"])
        self.assertEqual(ok, ["ema_cross_20_50:60"])
        self.assertEqual(refused, {"nope:15": ("no_capabilities_declared",)})


if __name__ == "__main__":
    unittest.main()
