"""Effective number of trials (Bailey and Lopez de Prado) for the #182 benchmark harness."""
from __future__ import annotations

import unittest

import numpy as np

from market_analysis.benchmark.neff import effective_trials

UNIT = 1_000_000
# T = 4: f1 = f2 = [1, 1, -1, -1] and f3 = [1, -1, 1, -1] are centred; rho_12 = 1, rho_13 = rho_23 = 0,
# so the six ordered off-diagonal pairs sum to 2 and rho_bar = 2 / 6 = 1/3.
EXACT = np.array([[1, 1, 1], [1, 1, -1], [-1, -1, 1], [-1, -1, -1]], dtype=np.int64) * UNIT


class EffectiveTrialsTests(unittest.TestCase):
    def test_identical_columns_are_one_trial(self):
        column = np.random.default_rng(1).integers(-10_000, 10_000, size=(500, 1)).astype(np.int64)
        self.assertEqual(effective_trials(np.repeat(column, 6, axis=1)), 1.0)  # rho_bar = 1

    def test_exact_orthogonal_centred_example(self):
        self.assertEqual(effective_trials(EXACT), 2.333333333)  # 1/3 + (2/3) * 3

    def test_independent_columns_close_to_k(self):
        f = np.round(np.random.default_rng(2).standard_normal((2000, 20)) * UNIT).astype(np.int64)
        n_eff = effective_trials(f)
        self.assertLessEqual(n_eff, 20.0)
        self.assertGreaterEqual(n_eff, 20 * 0.9)

    def test_constant_column_is_its_own_trial(self):
        column = np.arange(100, dtype=np.int64).reshape(-1, 1)
        f = np.hstack([column, column, np.full((100, 1), 42, dtype=np.int64)])
        # By hand: rho_12 = rho_21 = 1 and the constant column contributes 0, over K * (K - 1) = 6
        # ordered pairs: rho_bar = 2/6 = 1/3, N_eff = 1/3 + (2/3) * 3 = 2.333333333.
        self.assertEqual(effective_trials(f), 2.333333333)
        self.assertEqual(effective_trials(np.zeros((10, 4), dtype=np.int64)), 4.0)  # all constant: rho_bar = 0

    def test_single_trial_and_ledger_scaling(self):
        self.assertEqual(effective_trials(np.arange(10, dtype=np.int64).reshape(-1, 1)), 1.0)
        self.assertEqual(effective_trials(np.arange(10, dtype=np.int64).reshape(-1, 1), n_trials=5), 5.0)
        self.assertEqual(effective_trials(EXACT, n_trials=3), 2.333333333)
        self.assertEqual(effective_trials(EXACT, n_trials=9), 6.333333333)  # 1/3 + (2/3) * 9
        column = np.random.default_rng(3).integers(-100, 100, size=(50, 1)).astype(np.int64)
        self.assertEqual(effective_trials(np.repeat(column, 4, axis=1), n_trials=50), 1.0)
        for n_trials in (2, 0, 3.0, True):
            with self.subTest(n_trials=n_trials), self.assertRaises(ValueError):
                effective_trials(EXACT, n_trials=n_trials)

    def test_overflow_guard(self):
        with self.assertRaises(OverflowError):
            effective_trials(np.full((2, 2), 2 ** 31, dtype=np.int64))  # 2 * (2**31)**2 = 2**63
        self.assertEqual(effective_trials(np.array([[2 ** 30], [-(2 ** 30)]], dtype=np.int64)), 1.0)

    def test_many_trials_without_a_k_by_k_object(self):
        f = np.round(np.random.default_rng(4).standard_normal((60, 3000)) * UNIT).astype(np.int64)
        n_eff = effective_trials(f)
        self.assertTrue(1.0 <= n_eff <= 3000.0)

    def test_input_validation(self):
        for f in (np.zeros((1, 3), dtype=np.int64), np.zeros((5, 2)), np.zeros(5, dtype=np.int64)):
            with self.assertRaises(ValueError):
                effective_trials(f)


if __name__ == "__main__":
    unittest.main()
