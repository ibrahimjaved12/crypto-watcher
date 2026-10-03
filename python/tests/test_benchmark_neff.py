"""Effective number of trials for the #182 benchmark harness."""
from __future__ import annotations

import unittest

import numpy as np

from market_analysis.benchmark.neff import effective_trials


class EffectiveTrialsTests(unittest.TestCase):
    def test_identical_columns_are_one_trial(self):
        column = np.random.default_rng(1).integers(-10_000, 10_000, size=(500, 1)).astype(np.int64)
        self.assertEqual(effective_trials(np.repeat(column, 6, axis=1)), 1.0)

    def test_independent_columns_close_to_k(self):
        f = np.round(np.random.default_rng(2).standard_normal((2000, 10)) * 1_000_000).astype(np.int64)
        n_eff = effective_trials(f)
        self.assertLessEqual(n_eff, 10.0)
        self.assertGreater(n_eff, 10 * 0.85)

    def test_constant_column_is_its_own_trial(self):
        column = np.arange(100, dtype=np.int64).reshape(-1, 1)
        f = np.hstack([column, column, np.full((100, 1), 42, dtype=np.int64)])
        self.assertEqual(effective_trials(f), 1.8)  # 3**2 / (4 + 1)
        self.assertEqual(effective_trials(np.zeros((10, 4), dtype=np.int64)), 4.0)

    def test_overflow_guard(self):
        with self.assertRaises(OverflowError):
            effective_trials(np.full((2, 1), 2 ** 31, dtype=np.int64))  # 2 * (2**31)**2 = 2**63
        self.assertEqual(effective_trials(np.array([[2 ** 30], [-(2 ** 30)]], dtype=np.int64)), 1.0)

    def test_input_validation(self):
        for f in (np.zeros((1, 3), dtype=np.int64), np.zeros((5, 2)), np.zeros(5, dtype=np.int64)):
            with self.assertRaises(ValueError):
                effective_trials(f)


if __name__ == "__main__":
    unittest.main()
