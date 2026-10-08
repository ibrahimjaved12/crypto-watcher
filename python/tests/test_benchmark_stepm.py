"""Romano-Wolf StepM for the #182 benchmark harness."""
from __future__ import annotations

import unittest

import numpy as np

from fractions import Fraction

from market_analysis.benchmark.stepm import critical_rank, stepm, stepm_p_values

UNIT = 1_000_000


def noise(seed, T, K):
    return np.round(np.random.default_rng(seed).standard_normal((T, K)) * UNIT).astype(np.int64)


class StepMTests(unittest.TestCase):
    def test_planted_trial_rejected_and_noise_kept(self):
        clean = 0
        for simulation in range(20):
            f = noise(900 + simulation, 400, 30)
            f[:, 11] += UNIT // 4  # 0.25 sigma per day
            result = stepm(f, B=200, seed=simulation, stream_prefix="stepm")
            clean += result.rejected == (11,)
            self.assertIn(11, result.rejected)
        # Nominal FWER is 5%. These 20 fixed noise draws give 15 clean simulations at B=200 and 16 at
        # B=1000 (the false rejections are specific draws, not bootstrap noise), and an independent
        # 60-simulation run gave 6 false-rejection simulations at B=200. 13 is a deterministic margin
        # that still fails if the step-down stops controlling the noise trials at all.
        self.assertGreaterEqual(clean, 13)

    def test_adjusted_p_values_agree_with_rejections(self):
        # B * alpha = 10 is an integer, the boundary case: rejection holds exactly when p <= alpha.
        for simulation in range(6):
            f = noise(70 + simulation, 300, 10)
            if simulation % 2 == 0:
                f[:, :3] += np.array([UNIT // 3, UNIT // 6, UNIT // 12])
            arguments = dict(B=200, seed=simulation, stream_prefix="adjusted")
            result, p = stepm(f, **arguments), stepm_p_values(f, **arguments)
            with self.subTest(simulation=simulation):
                self.assertEqual({k for k in range(10) if p[k] <= Fraction(1, 20)}, set(result.rejected))
                if simulation % 2 == 0:
                    self.assertIn(0, result.rejected)

    def test_step_structure(self):
        for simulation in range(5):
            f = noise(40 + simulation, 300, 12)
            f[:, :3] += np.array([UNIT // 3, UNIT // 5, UNIT // 8])
            result = stepm(f, B=200, seed=simulation, stream_prefix="steps")
            first, last = result.critical_values[0], result.critical_values[-1]
            t = result.t_stats
            with self.subTest(simulation=simulation):
                # Step 1 rejects exactly {t_k > c_1}; later steps only add trials (with lower critical values).
                self.assertTrue({k for k in range(12) if t[k] > first} <= set(result.rejected))
                self.assertTrue(set(result.rejected) <= {k for k in range(12) if t[k] > last})
                self.assertEqual(list(result.critical_values), sorted(result.critical_values, reverse=True))
                self.assertEqual(result.steps, len(result.critical_values))
                self.assertEqual(result.rejected, tuple(sorted(result.rejected)))

    def test_all_noise_rejects_nothing_mostly(self):
        empty = sum(stepm(noise(1300 + simulation, 300, 20), B=200, seed=simulation,
                          stream_prefix="null").rejected == () for simulation in range(20))
        self.assertGreaterEqual(empty, 15)

    def test_degenerate_and_record(self):
        f = noise(5, 200, 4)
        f[:, 1] = 7
        f[:, 0] += UNIT
        result = stepm(f, B=100, seed=2, stream_prefix="deg")
        self.assertNotIn(1, result.rejected)
        self.assertIn(0, result.rejected)
        self.assertEqual(result.to_record(), stepm(f.copy(), B=100, seed=2, stream_prefix="deg").to_record())
        self.assertEqual(result.to_record()["critical_values"], [repr(value) for value in result.critical_values])
        nothing = stepm(np.full((50, 3), 2, dtype=np.int64), B=20, seed=0, stream_prefix="deg")
        self.assertEqual((nothing.rejected, nothing.critical_values, nothing.steps), ((), (), 0))

    def test_critical_rank(self):
        self.assertEqual(critical_rank(0.05, 10_000), 9_500)
        self.assertEqual(critical_rank(0.05, 200), 190)
        self.assertEqual(critical_rank(0.1, 199), 180)  # ceil(179.1)
        self.assertEqual(critical_rank(0.999, 10), 1)  # ceil(0.01)
        for alpha, B in ((0.0, 100), (1.0, 100), (-0.1, 10)):
            with self.subTest(alpha=alpha, B=B), self.assertRaises(ValueError):
                critical_rank(alpha, B)


if __name__ == "__main__":
    unittest.main()
