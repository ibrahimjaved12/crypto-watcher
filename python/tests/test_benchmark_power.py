"""Normal-approximation examples and closed-interval independence counts."""
from fractions import Fraction
from types import SimpleNamespace
import unittest

from market_analysis.benchmark.power import (
    min_detectable_edge_per_day, min_detectable_edge_per_trade, n_independent_greedy, power_gate, required_days,
)
from market_analysis.benchmark.scan import UR


class PowerTests(unittest.TestCase):
    def test_known_observation_requirement_and_inverse(self):
        self.assertEqual(required_days(Fraction(1, 10), 1), 785)
        self.assertGreater(min_detectable_edge_per_day(784, 1), 1 / 10)
        self.assertLessEqual(min_detectable_edge_per_day(785, 1), 1 / 10)
        day = min_detectable_edge_per_day(785, 1)
        self.assertAlmostEqual(min_detectable_edge_per_trade(785, 1, 2), day / 2)
        self.assertEqual(required_days(Fraction(1, 10), 0), 0)

    def test_gate_uses_full_calendar_length_and_sample_sigma(self):
        daily = [-UR, UR] * 500
        result = power_gate(daily, 1)
        self.assertTrue(result["passes"])
        self.assertEqual(result["T_days"], 1000)
        self.assertLess(float(result["mde_per_trade"]), 1 / 10)
        self.assertEqual(result, power_gate(daily, 1))
        self.assertFalse(power_gate([-UR, UR] * 50, 1)["passes"])
        with_zeros = power_gate([-UR, UR] + [0] * 98, Fraction(1, 50))
        self.assertEqual(with_zeros["T_days"], 100)
        self.assertEqual(with_zeros["trades_per_day"], "0.02")

    def test_empty_singleton_zero_rate_and_zero_variance(self):
        for daily, rate in (([], 1), ([UR], 1), ([UR, UR], 0)):
            result = power_gate(daily, rate)
            self.assertFalse(result["passes"])
            self.assertIsNone(result["mde_per_trade"])
        self.assertTrue(power_gate([UR, UR], 1)["passes"])

    def test_greedy_per_symbol_closed_intervals(self):
        def row(symbol, first, end):
            return SimpleNamespace(symbol=symbol, signal_ms=first, exit_ms=end)
        trades = [row("BTCUSDT", 0, 10), row("BTCUSDT", 1, 2), row("BTCUSDT", 3, 4),
                  row("BTCUSDT", 4, 5), row("ETHUSDT", 0, 10)]
        self.assertEqual(n_independent_greedy(trades), 3)
        self.assertEqual(n_independent_greedy(reversed(trades)), 3)
        self.assertEqual(n_independent_greedy([]), 0)

    def test_invalid_inputs(self):
        for call in (lambda: required_days(0, 1), lambda: required_days(1, -1),
                     lambda: required_days(1, 1, alpha=0),
                     lambda: min_detectable_edge_per_day(0, 1),
                     lambda: min_detectable_edge_per_trade(10, 1, 0),
                     lambda: power_gate([0, 1], -1)):
            with self.assertRaises(ValueError):
                call()


if __name__ == "__main__":
    unittest.main()
