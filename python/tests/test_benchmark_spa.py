"""Hansen SPA and White Reality Check for the #182 benchmark harness."""
from __future__ import annotations

import math
import unittest

import numpy as np

from market_analysis.benchmark.bootstrap import bootstrap_indices
from market_analysis.benchmark.spa import spa_test
from slow import slow

UNIT = 1_000_000  # 1e-6 R


def noise(seed, T, K, scale=UNIT):
    return np.round(np.random.default_rng(seed).standard_normal((T, K)) * scale).astype(np.int64)


def oracle(f, B, mean_block, seed, prefix):
    """O(B*K*T) pure-Python SPA/RC with the same indices and the same float operation order."""
    T, K = len(f), len(f[0])
    indices = bootstrap_indices(T, mean_block, seed, prefix, range(B)).tolist()
    sums = [sum(f[t][k] for t in range(T)) for k in range(K)]
    star = [[sum(f[t][k] for t in row) for k in range(K)] for row in indices]
    dbar = [float(s) / T for s in sums]
    dstar = [[float(s) / T for s in row] for row in star]
    omega = []
    for k in range(K):
        s1 = sum(row[k] for row in star)
        s2 = sum(row[k] * row[k] for row in star)
        omega.append(math.sqrt((B * s2 - s1 * s1) / (B * B * T)))
    root_t = math.sqrt(T)
    valid = [k for k in range(K) if omega[k] > 0]
    t = [root_t * dbar[k] / omega[k] if omega[k] > 0 else 0.0 for k in range(K)]
    statistic = max(0.0, max(t[k] for k in valid))
    rc_statistic = max(0.0, max(root_t * dbar[k] for k in valid))
    threshold = -math.sqrt(2.0 * math.log(math.log(T)))
    centres = {
        "lower": [max(dbar[k], 0.0) for k in range(K)],
        "consistent": [dbar[k] if t[k] >= threshold else 0.0 for k in range(K)],
        "upper": list(dbar),
    }
    result = {}
    for name, g in centres.items():
        stars = [max(0.0, max(root_t * (row[k] - g[k]) / omega[k] for k in valid)) for row in dstar]
        result[name] = sum(1 for value in stars if value > statistic) / B if statistic > 0 else 1.0
    rc_stars = [max(0.0, max(root_t * (row[k] - dbar[k]) for k in valid)) for row in dstar]
    result["rc"] = sum(1 for value in rc_stars if value > rc_statistic) / B if rc_statistic > 0 else 1.0
    return result, t, omega


class SpaOracleTests(unittest.TestCase):
    def test_matches_pure_python_oracle_exactly(self):
        for seed in (1, 2, 3):
            f = noise(seed, 24, 3, scale=1000)
            f[:, 1] += 150 * seed  # some edge so the p-values are not all trivial
            expected, t, omega = oracle(f.tolist(), 40, 4, seed, "oracle")
            result = spa_test(f, B=40, mean_block=4, seed=seed, stream_prefix="oracle")
            with self.subTest(seed=seed):
                self.assertEqual(result.p_lower, expected["lower"])
                self.assertEqual(result.p_consistent, expected["consistent"])
                self.assertEqual(result.p_upper, expected["upper"])
                self.assertEqual(result.p_reality_check, expected["rc"])
                self.assertEqual(list(result.t_stats), t)
                self.assertEqual(list(result.omega), omega)


class SpaBehaviourTests(unittest.TestCase):
    @slow  # statistical acceptance (60/20 simulations); weekly slow-tests.yml runs it
    def test_null_calibration(self):
        rejections = 0
        for simulation in range(60):
            result = spa_test(noise(100 + simulation, 300, 20), B=200, seed=simulation, stream_prefix="null")
            rejections += result.p_consistent < 0.05
        self.assertLessEqual(rejections / 60, 0.2)

    def test_planted_edge_and_ordering(self):
        rejected = 0
        for simulation in range(20):
            f = noise(500 + simulation, 400, 30)
            f[:, 7] += UNIT // 4  # 0.25 sigma per day
            result = spa_test(f, B=200, seed=simulation, stream_prefix="planted")
            rejected += result.p_consistent < 0.05
            self.assertLessEqual(result.p_lower, result.p_consistent)
            self.assertLessEqual(result.p_consistent, result.p_upper)
        self.assertGreaterEqual(rejected, 18)

    def test_deterministic_and_copy_independent(self):
        f = noise(9, 120, 5)
        first = spa_test(f, B=100, seed=4, stream_prefix="det")
        self.assertEqual(first.to_record(), spa_test(f, B=100, seed=4, stream_prefix="det").to_record())
        self.assertEqual(first.to_record(), spa_test(f.copy(order="F"), B=100, seed=4, stream_prefix="det").to_record())
        self.assertNotEqual(first.to_record(), spa_test(f, B=100, seed=5, stream_prefix="det").to_record())
        record = first.to_record()
        self.assertEqual(record["p_consistent"], repr(first.p_consistent))
        self.assertEqual(record["t_stats"], [repr(value) for value in first.t_stats])
        benchmark = spa_test(f, f0=f[:, 0].copy(), B=100, seed=4, stream_prefix="det")
        self.assertEqual(benchmark.dbar[0], 0.0)

    def test_degenerate_zero_variance_column(self):
        f = noise(3, 100, 4)
        f[:, 2] = 0
        result = spa_test(f, B=100, seed=1, stream_prefix="degenerate")
        self.assertEqual((result.t_stats[2], result.omega[2]), (0.0, 0.0))
        constant = np.full((100, 2), 5, dtype=np.int64)  # positive but constant: degenerate, never rejects
        only = spa_test(constant, B=50, seed=1, stream_prefix="degenerate")
        self.assertEqual((only.statistic, only.p_consistent, only.p_reality_check), (0.0, 1.0, 1.0))
        losing = spa_test(-np.abs(noise(4, 100, 3)) - 1, B=50, seed=1, stream_prefix="degenerate")
        self.assertEqual((losing.statistic, losing.p_lower, losing.p_upper), (0.0, 1.0, 1.0))

    def test_input_validation(self):
        for f in (np.zeros((10, 2), dtype=np.int64), np.zeros((30, 2)), np.zeros(30, dtype=np.int64)):
            with self.assertRaises(ValueError):
                spa_test(f, B=10, seed=0, stream_prefix="v")
        with self.assertRaises(OverflowError):
            spa_test(np.full((20, 1), 2 ** 62, dtype=np.int64), B=10, seed=0, stream_prefix="v")


if __name__ == "__main__":
    unittest.main()
