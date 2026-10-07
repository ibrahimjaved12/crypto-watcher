"""Deflated Sharpe Ratio for the #182 benchmark harness."""
from __future__ import annotations

import math
import unittest

from market_analysis.benchmark.dsr import deflated_sharpe_ratio, expected_max_sharpe, sharpe_stats


class DsrTests(unittest.TestCase):
    def test_sharpe_stats_hand_computed(self):
        mean, std, sr, skew, kurtosis, n = sharpe_stats([1, 2, 3, 4])
        self.assertEqual((mean, n), (2.5, 4))
        self.assertAlmostEqual(std, math.sqrt(5 / 3), places=15)
        self.assertAlmostEqual(sr, 2.5 / math.sqrt(5 / 3), places=15)
        self.assertAlmostEqual(skew, 0.0, places=15)
        self.assertAlmostEqual(kurtosis, 2.5625 / 1.5625, places=15)  # m4 / m2**2 = 1.64
        _, _, _, skewed, _, _ = sharpe_stats([0, 0, 0, 10])
        self.assertGreater(skewed, 0)
        for series in ([5, 5, 5], [1]):
            with self.subTest(series=series), self.assertRaises(ValueError):
                sharpe_stats(series)

    def test_no_selection_gives_half(self):
        self.assertEqual(expected_max_sharpe(1, 0.04), 0.0)
        self.assertEqual(expected_max_sharpe(0.5, 0.04), 0.0)
        self.assertEqual(deflated_sharpe_ratio(0.0, 0.0, 10_000, 0.0, 3.0), 0.5)

    def test_expected_max_sharpe_monotone(self):
        values = [expected_max_sharpe(n, 0.01) for n in (1.5, 2, 10, 100, 1000)]
        self.assertEqual(values, sorted(values))
        self.assertGreater(values[0], 0)
        self.assertLess(expected_max_sharpe(50, 0.01), expected_max_sharpe(50, 0.04))
        self.assertAlmostEqual(expected_max_sharpe(50, 0.04), 2 * expected_max_sharpe(50, 0.01), places=12)
        for args in ((0, 0.01), (-1, 0.01), (float("nan"), 0.01), (10, -0.01)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                expected_max_sharpe(*args)

    def test_dsr_behaviour(self):
        values = [deflated_sharpe_ratio(0.1, 0.05, n, -0.2, 5.0) for n in (30, 100, 1000)]
        self.assertEqual(values, sorted(values))
        self.assertTrue(all(0.5 < value < 1 for value in values))
        self.assertLess(deflated_sharpe_ratio(0.05, 0.1, 500, 0.0, 3.0), 0.5)
        with self.assertRaisesRegex(ValueError, "excess kurtosis"):
            deflated_sharpe_ratio(0.1, 0.0, 100, 0.0, 0.0)  # excess kurtosis passed by mistake
        with self.assertRaises(ValueError):
            deflated_sharpe_ratio(0.1, 0.0, 2, 0.0, 3.0)
        with self.assertRaises(ValueError):
            deflated_sharpe_ratio(2.0, 0.0, 100, 1.0, 1.0)  # 1 - 2 + 0 <= 0


class SmallTrialCountTests(unittest.TestCase):
    def test_expected_max_sharpe_never_negative_and_continuous_near_one(self):
        for n in (1.0001, 1.05, 1.1, 1.2, 1.3):
            with self.subTest(n=n):
                self.assertGreaterEqual(expected_max_sharpe(n, 0.01), 0.0)
        self.assertEqual(expected_max_sharpe(1.1, 0.01), 0.0)
        self.assertLessEqual(expected_max_sharpe(1.3, 0.01), expected_max_sharpe(1.5, 0.01))


if __name__ == "__main__":
    unittest.main()
