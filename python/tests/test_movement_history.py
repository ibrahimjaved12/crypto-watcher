"""Boundary-deterministic historical #71 fixtures."""

from decimal import Decimal
import math
import unittest

from market_analysis.movement_history import (CompletedMovementCandle,
                                              build_historical_window_inputs)
from market_analysis.movement_metrics import MarketMovementConfig

BASE = 1_800_000_000_000
MINUTE = 60_000
CONFIG = MarketMovementConfig(historical_lookback_ms=30 * MINUTE,
                              minimum_historical_coverage_ms=MINUTE,
                              rvol_comparison_windows=3)


def candles():
    return tuple(CompletedMovementCandle(BASE - (50 - index) * MINUTE,
                                         Decimal(100 + index), Decimal(2),
                                         Decimal(index + 1)) for index in range(50))


class HistoricalBuilderTests(unittest.TestCase):
    def test_aligned_returns_warmup_and_exact_quote_notionals(self):
        result = build_historical_window_inputs(candles(), BASE, CONFIG)
        self.assertEqual(len(result[1].returns), 30)
        self.assertEqual(len(result[5].returns), 6)
        self.assertEqual(len(result[15].returns), 2)
        self.assertAlmostEqual(result[15].returns[0], math.log(119 / 104))
        self.assertAlmostEqual(result[1].returns[0], math.log(119 / 118))
        self.assertEqual(result[15].previous_notional_volumes[0], Decimal(195))
        self.assertEqual(result[15].usable_coverage_ms, 49 * MINUTE)
        self.assertNotIn(math.log(149 / 134), result[15].returns)

    def test_gap_truncates_and_duplicate_does_not_add_a_sample(self):
        raw = candles()
        original = build_historical_window_inputs(raw, BASE, CONFIG)
        self.assertEqual(original, build_historical_window_inputs(raw + (raw[12],), BASE, CONFIG))
        gapped = build_historical_window_inputs(tuple(c for c in raw
                                                      if c.open_time_ms != BASE - 20 * MINUTE),
                                                BASE, CONFIG)
        self.assertNotIn(15, gapped)
        self.assertLess(len(gapped[1].returns), len(original[1].returns))

    def test_exact_boundary_not_cache_timing_selects_history(self):
        raw = candles()
        first = build_historical_window_inputs(raw, BASE, CONFIG)
        self.assertEqual(first, build_historical_window_inputs(tuple(reversed(raw)), BASE, CONFIG))
        next_boundary = build_historical_window_inputs(raw, BASE + 5_000, CONFIG)
        self.assertEqual(len(next_boundary[15].returns), len(first[15].returns))
        self.assertAlmostEqual(next_boundary[15].returns[0], math.log(134 / 119))
        self.assertAlmostEqual(next_boundary[15].returns[-1], math.log(149 / 134))
        self.assertEqual(first, build_historical_window_inputs(raw, BASE, CONFIG))


if __name__ == "__main__":
    unittest.main()
