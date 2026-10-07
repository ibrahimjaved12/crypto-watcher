"""Deflated Sharpe of the best trial for the #182 benchmark harness."""
from __future__ import annotations

import math
import unittest

import numpy as np

from market_analysis.benchmark.best_trial import best_trial_dsr
from market_analysis.benchmark.dsr import sharpe_stats

UNIT = 1_000_000


def noise(seed, T=300, K=20):
    return np.round(np.random.default_rng(seed).standard_normal((T, K)) * UNIT).astype(np.int64)


class BestTrialDsrTests(unittest.TestCase):
    def test_pure_noise_and_record(self):
        result = best_trial_dsr(noise(1))
        self.assertTrue(0 < result.dsr_raw < 1)
        self.assertTrue(0 < result.dsr_effective < 1)
        self.assertEqual((result.n_trials, result.n_obs), (20, 300))
        self.assertTrue(1.0 <= result.n_effective <= 20.0)
        self.assertLessEqual(result.sr0_effective, result.sr0_raw)
        record = result.to_record()
        self.assertEqual(record["dsr_raw"], repr(result.dsr_raw))
        self.assertEqual(record["best_index"], result.best_index)
        self.assertEqual(record, best_trial_dsr(noise(1)).to_record())
        # The best index is the column with the highest exact Sharpe ratio.
        f = noise(1)
        sharpes = [sharpe_stats(f[:, k].tolist())[2] for k in range(20)]
        self.assertEqual(result.best_index, max(range(20), key=lambda k: (sharpes[k], -k)))
        self.assertTrue(math.isclose(result.sr_hat, sharpes[result.best_index], rel_tol=1e-12))

    def test_planted_column_is_deflated_close_to_one(self):
        f = noise(1)
        f[:, 4] += UNIT // 2  # about 0.5 per-day Sharpe over 300 days
        result = best_trial_dsr(f)
        self.assertEqual(result.best_index, 4)
        self.assertGreater(result.dsr_raw, 0.99)
        self.assertGreaterEqual(result.dsr_effective, result.dsr_raw)

    def test_ledger_count(self):
        f = noise(2)
        larger = best_trial_dsr(f, n_trials=1000)
        self.assertEqual(larger.n_trials, 1000)
        self.assertGreater(larger.sr0_raw, best_trial_dsr(f).sr0_raw)
        self.assertLessEqual(larger.dsr_raw, best_trial_dsr(f).dsr_raw)
        with self.assertRaises(ValueError):
            best_trial_dsr(f, n_trials=19)

    def test_constant_columns_excluded(self):
        f = noise(3, K=5)
        f[:, 0] = 10 * UNIT  # constant positive: no Sharpe ratio, never the best
        f[:, 3] = 0
        result = best_trial_dsr(f)
        self.assertNotIn(result.best_index, (0, 3))
        base = best_trial_dsr(f[:, [1, 2, 4]])
        self.assertEqual(result.var_sharpe, base.var_sharpe)  # constants left out of the variance
        self.assertEqual(result.sr_hat, base.sr_hat)
        with self.assertRaises(ValueError):
            best_trial_dsr(np.hstack([noise(4, K=1), np.zeros((300, 3), dtype=np.int64)]))


if __name__ == "__main__":
    unittest.main()
