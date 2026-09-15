import unittest
from decimal import Decimal

from market_analysis.core import MINUTE
from market_analysis.cumulative import observe

START = 1704067200000


class CumulativeTests(unittest.TestCase):
    def advance(self, state, price, minute, **kwargs):
        now = START + minute * MINUTE
        return observe(state, price, now, kwargs.pop("source", "Binance"), now, **kwargs)

    def test_slow_rise_beyond_fifteen_minutes(self):
        state = None
        for minute, price, status in ((0, "100", "initialized"), (10, "101", "below_threshold"),
                                      (20, "101.99", "below_threshold"), (30, "102", "alerted")):
            state, result = self.advance(state, price, minute)
            self.assertEqual(result["status"], status)
        self.assertEqual(result["direction"], "up")
        self.assertEqual(result["baseline_price"], "100")
        self.assertEqual(state.price, 102)

    def test_slow_fall(self):
        state, _ = self.advance(None, "100", 0)
        state, result = self.advance(state, "98.01", 20)
        self.assertEqual(result["status"], "below_threshold")
        state, result = self.advance(state, "98", 40)
        self.assertEqual(result["status"], "alerted")
        self.assertEqual(result["direction"], "down")
        self.assertEqual(Decimal(result["change_pct"]), -2)

    def test_reversal_has_separate_cooldown(self):
        state, _ = self.advance(None, "100", 0)
        state, _ = self.advance(state, "102", 5)
        state, result = self.advance(state, "99.96", 6)
        self.assertEqual(result["status"], "alerted")
        self.assertEqual(result["direction"], "down")

    def test_cooldown_keeps_baseline_then_alerts_at_boundary(self):
        state, _ = self.advance(None, "100", 0)
        state, _ = self.advance(state, "102", 5)
        state, result = self.advance(state, "104.04", 10)
        self.assertEqual(result["status"], "cooldown")
        self.assertEqual(state.price, 102)
        state, result = self.advance(state, "104.04", 20)
        self.assertEqual(result["status"], "alerted")
        self.assertEqual(state.price, Decimal("104.04"))

    def test_no_repeat_for_unchanged_elevated_price(self):
        state, _ = self.advance(None, "100", 0)
        state, _ = self.advance(state, "102", 5)
        state, result = self.advance(state, "102", 30)
        self.assertEqual(result["status"], "below_threshold")

    def test_fluctuations_are_net_movement_not_sum_of_absolute_moves(self):
        state, _ = self.advance(None, "100", 0)
        for minute, price in ((5, "101"), (10, "99"), (20, "101.99")):
            state, result = self.advance(state, price, minute)
            self.assertEqual(result["status"], "below_threshold")
            self.assertEqual(state.price, 100)

    def test_provider_or_threshold_change_reinitializes_without_alert(self):
        state, _ = self.advance(None, "100", 0)
        state, result = self.advance(state, "120", 5, source="OKX")
        self.assertEqual(result["status"], "reinitialized")
        state, result = self.advance(state, "150", 10, source="OKX", threshold="3")
        self.assertEqual(result["status"], "reinitialized")

    def test_replayed_or_older_observation_ignored(self):
        state, _ = self.advance(None, "100", 5)
        for minute in (5, 4):
            next_state, result = self.advance(state, "120", minute)
            self.assertEqual(next_state, state)
            self.assertEqual(result["status"], "already_processed")

    def test_invalid_and_stale_observations(self):
        for price in ("0", "-1", "NaN", "Infinity"):
            with self.assertRaises(ValueError):
                self.advance(None, price, 0)
        for observed in (START - 11 * MINUTE, START + MINUTE, START + 1):
            with self.assertRaises(ValueError):
                observe(None, "100", observed, "Binance", START)


if __name__ == "__main__":
    unittest.main()
