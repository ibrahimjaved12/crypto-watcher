"""Stationary bootstrap with counter-based randomness for the #182 benchmark harness."""
from __future__ import annotations

import unittest

import numpy as np

from market_analysis.benchmark.bootstrap import (
    bootstrap_indices, bootstrap_mean_matrix, default_mean_block, restart_mask, restart_threshold,
    stationary_indices,
)
from market_analysis.benchmark.rng import u64_words


def run_starts(index, T):
    """Positions where the index does not continue the previous one."""
    return [t for t in range(len(index)) if t == 0 or index[t] != (index[t - 1] + 1) % T]


class BootstrapIndexTests(unittest.TestCase):
    def test_default_mean_block(self):
        self.assertEqual(default_mean_block(550), 9)    # ceil(cbrt 550) = 9
        self.assertEqual(default_mean_block(20), 2)     # max(3, 5) = 5, capped by 20 // 10 = 2
        self.assertEqual(default_mean_block(5000), 18)  # 17**3 = 4913 < 5000 <= 18**3
        self.assertEqual(default_mean_block(1), 1)
        self.assertEqual(default_mean_block(125), 5)

    def test_indices_in_range_and_row_alone_equals_batch(self):
        T = 97
        batch = bootstrap_indices(T, 6, 3, "spa", range(10))
        self.assertEqual((batch.shape, batch.dtype), ((10, T), np.int64))
        self.assertTrue(((batch >= 0) & (batch < T)).all())
        for replicate in (0, 4, 9):
            self.assertTrue(np.array_equal(stationary_indices(T, 6, 3, "spa", replicate), batch[replicate]))
        self.assertTrue(np.array_equal(bootstrap_indices(T, 6, 3, "spa", [9, 4]), batch[[9, 4]]))
        self.assertEqual(bootstrap_indices(T, 6, 3, "spa", []).shape, (0, T))

    def test_threshold_logic_and_mean_block_one(self):
        self.assertEqual(restart_threshold(1), 2 ** 64)
        self.assertEqual(restart_threshold(2), 2 ** 63)
        self.assertEqual(restart_threshold(3), (2 ** 64) // 3)
        edge = np.array([0, 2 ** 63 - 1, 2 ** 63, 2 ** 64 - 1], dtype=np.uint64)
        self.assertEqual(restart_mask(edge, 2).tolist(), [True, True, False, False])
        self.assertEqual(restart_mask(edge, 1).tolist(), [True, True, True, True])  # 2**64 - 1 restarts too
        # mean_block == 1: every position restarts, so index[t] is exactly the start draw of t.
        T = 50
        starts = (u64_words(11, "q1/rep/2", 0, T) % np.uint64(T)).astype(np.int64)
        self.assertTrue(np.array_equal(stationary_indices(T, 1, 11, "q1", 2), starts))

    def test_large_mean_block_gives_long_runs(self):
        T, mean_block = 500, 20
        runs = 0
        replicates = 200
        for row in bootstrap_indices(T, mean_block, 5, "runs", range(replicates)):
            runs += len(run_starts(row.tolist(), T))
        observed = replicates * T / runs
        self.assertLess(abs(observed - mean_block), 0.25 * mean_block)

    def test_mean_matrix_sums(self):
        rng = np.random.default_rng(1)
        d = rng.integers(-1000, 1000, size=(30, 4)).astype(np.int64)
        indices = bootstrap_indices(30, 3, 2, "sum", range(7))
        sums = bootstrap_mean_matrix(d, indices)
        self.assertEqual((sums.shape, sums.dtype), ((7, 4), np.int64))
        for b in range(7):
            for k in range(4):
                self.assertEqual(int(sums[b, k]), sum(int(d[t, k]) for t in indices[b]))
        with self.assertRaises(OverflowError):
            bootstrap_mean_matrix(np.full((30, 1), 2 ** 62, dtype=np.int64), indices)


if __name__ == "__main__":
    unittest.main()
