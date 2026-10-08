"""Acceptance: 1,000 correlated pure-noise trials (#182 multiple-testing calibration).

Data generation is fixed so results match the measured reference values:
one-factor noise with pairwise correlation 0.5 (seeds 7000+s) and 0.8 (seeds
8000+s), T = 550 days, K = 1000 trials, in 1e-6 R units. Measured: SPA and StepM
false rejections 0 of 20, best-trial raw DSR > 0.95 in 0 of 20, effective-N DSR
false passes 0.5% to 4%; with a planted 0.3-sigma trial both SPA and StepM find
it in 10 of 10.

These three acceptance tests take about 1.5 minutes, so they are marked @slow and
run in slow-tests.yml. The default suite runs a small smoke test instead (T=300,
K=100, B=100): it checks that the procedures run and find a strong planted trial,
with no null-rate thresholds.
"""
from __future__ import annotations

import unittest

import numpy as np

from market_analysis.benchmark.best_trial import best_trial_dsr
from market_analysis.benchmark.spa import spa_test
from market_analysis.benchmark.stepm import stepm

try:  # discovered with tests/ on sys.path (unittest discover, run_shard.py)
    from slow import slow
except ImportError:  # run as ``python -m unittest tests.test_benchmark_calibration``
    from tests.slow import slow

T, K, UNIT = 550, 1000, 1_000_000


def matrix(seed, correlation):
    """Exactly the reference generation (written out per correlation, not via sqrt(1 - c))."""
    rng = np.random.default_rng(seed)
    common = rng.standard_normal((T, 1))
    idio = rng.standard_normal((T, K))
    if correlation == 0.5:
        return np.round((np.sqrt(0.5) * common + np.sqrt(0.5) * idio) * UNIT).astype(np.int64)
    return np.round((np.sqrt(0.8) * common + np.sqrt(0.2) * idio) * UNIT).astype(np.int64)


def small_matrix(seed, rows, columns, correlation=0.5):
    """One-factor correlated noise with unit per-trial sigma, in 1e-6 R units."""
    rng = np.random.default_rng(seed)
    common = rng.standard_normal((rows, 1))
    idio = rng.standard_normal((rows, columns))
    return np.round((np.sqrt(correlation) * common + np.sqrt(1 - correlation) * idio) * UNIT).astype(np.int64)


class CalibrationSmokeTests(unittest.TestCase):
    def test_procedures_run_and_find_a_strong_planted_trial(self):
        rows, columns = 300, 100
        f = small_matrix(4242, rows, columns)
        spa = spa_test(f, B=100, seed=1, stream_prefix="smoke")
        for p in (spa.p_lower, spa.p_consistent, spa.p_upper, spa.p_reality_check):
            self.assertTrue(0.0 <= p <= 1.0)
        self.assertTrue(spa.p_lower <= spa.p_consistent <= spa.p_upper)  # Hansen's recentering order
        self.assertEqual((spa.T, spa.K, spa.B, len(spa.t_stats)), (rows, columns, 100, columns))
        step = stepm(f, B=100, seed=1, stream_prefix="smoke")
        self.assertTrue(set(step.rejected) <= set(range(columns)))
        self.assertEqual(step.steps, len(step.critical_values))
        self.assertGreaterEqual(step.steps, 1)
        self.assertEqual(len(step.t_stats), columns)
        best = best_trial_dsr(f)
        self.assertTrue(0 <= best.best_index < columns)
        self.assertEqual(best.n_trials, columns)
        self.assertTrue(1.0 <= best.n_effective <= columns)
        self.assertTrue(0.0 <= best.dsr_raw <= 1.0 and 0.0 <= best.dsr_effective <= 1.0)
        planted = f.copy()
        planted[:, 37] += int(0.8 * UNIT)  # 0.8 sigma per day: t about 14 at T = 300
        self.assertLess(spa_test(planted, B=100, seed=2, stream_prefix="smoke-planted").p_consistent, 0.05)
        self.assertIn(37, stepm(planted, B=100, seed=2, stream_prefix="smoke-planted").rejected)


class NoiseCalibrationTests(unittest.TestCase):
    @slow
    def test_correlation_half(self):
        spa_rejections = stepm_rejections = raw_passes = effective_passes = 0
        for s in range(20):
            f = matrix(7000 + s, 0.5)
            spa_rejections += spa_test(f, B=500, seed=s, stream_prefix="cal").p_consistent < 0.05
            stepm_rejections += bool(stepm(f, B=500, seed=s, stream_prefix="cal").rejected)
            best = best_trial_dsr(f)
            raw_passes += best.dsr_raw > 0.95
            effective_passes += best.dsr_effective > 0.95
        self.assertLessEqual(spa_rejections, 4)
        self.assertLessEqual(stepm_rejections, 5)
        self.assertLessEqual(raw_passes, 3)
        self.assertLessEqual(effective_passes, 3)

    @slow
    def test_correlation_point_eight(self):
        raw_passes = effective_passes = 0
        for s in range(20):
            best = best_trial_dsr(matrix(8000 + s, 0.8))
            raw_passes += best.dsr_raw > 0.95
            effective_passes += best.dsr_effective > 0.95
        self.assertLessEqual(raw_passes, 5)
        self.assertLessEqual(effective_passes, 5)

    @slow
    def test_planted_trial_power(self):
        spa_hits = stepm_hits = 0
        for s in range(10):
            f = matrix(9000 + s, 0.5)
            f[:, 123] += int(0.3 * UNIT)
            spa_hits += spa_test(f, B=500, seed=s, stream_prefix="pl").p_consistent < 0.05
            stepm_hits += 123 in stepm(f, B=500, seed=s, stream_prefix="pl").rejected
        self.assertGreaterEqual(spa_hits, 9)
        self.assertGreaterEqual(stepm_hits, 9)


if __name__ == "__main__":
    unittest.main()
