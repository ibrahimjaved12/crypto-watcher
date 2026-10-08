"""Exact baselines, matched-cell fallback and counter-RNG placebo behavior."""
from fractions import Fraction
import unittest

from market_analysis.benchmark.baselines import (
    PoolRow, always_side, matched_placebo, random_walk_comparison, random_walk_hit_rate,
    tercile_boundaries, volatility_tercile,
)
from market_analysis.benchmark.evaluate import Trade
from market_analysis.benchmark.label_store import Geometry, GeometryColumns
from market_analysis.benchmark.scan import UR

G = Geometry("BTCUSDT", 15, 1, Fraction(1), 0)
MONDAY = 1_735_516_800_000  # 2024-12-30 00:00 UTC
WEEK = 7 * 86_400_000


def pool_row(index, net, *, volatility=Fraction(1, 100), hour=0, geometry=G):
    ms = MONDAY + index * WEEK + hour * 3_600_000
    trade = Trade(geometry, ms, "T" if net > 0 else "S", 5, net, 100, 0, 10 * UR, 0, net, 100, 0)
    return PoolRow(trade, volatility)


class BaselineTests(unittest.TestCase):
    def test_random_walk_target_first_denominator(self):
        self.assertEqual(random_walk_hit_rate(Fraction(3, 2)), Fraction(2, 5))
        self.assertEqual(random_walk_hit_rate(1), Fraction(1, 2))
        rows = [pool_row(0, UR).trade, pool_row(1, -UR).trade]
        comparison = random_walk_comparison(rows, 2)
        self.assertEqual(comparison["observed_target_share"], "0.5")
        self.assertEqual(comparison["expected_target_share"], "0.333333333333")
        self.assertIsNone(random_walk_comparison([], 1)["observed_target_share"])

    def test_always_side_preserves_non_trade_counts(self):
        long, short = GeometryColumns(G), GeometryColumns(G._replace(side=-1))
        for column in (long, short):
            column.non_trades["signal_ms"].append(MONDAY)
            column.non_trades["reason"].append(ord("V"))
            column.purged_signal_ms.append(MONDAY + 1)
        selection = always_side({G: long, short.geometry: short}, 1)
        self.assertEqual(selection.signals, 1)
        self.assertEqual(selection.non_trades, {"V": 1})
        self.assertEqual(selection.purged, 0)

    def test_placebo_noise_near_half_and_deterministic(self):
        pool = [pool_row(i, UR if i % 2 else -UR) for i in range(64)]
        first = matched_placebo(pool, pool, B=400, seed=9, stream_prefix="noise")
        self.assertEqual(first, matched_placebo(reversed(pool), reversed(pool), B=400, seed=9, stream_prefix="noise"))
        self.assertTrue(Fraction(7, 20) < Fraction(first["p_placebo"]) < Fraction(7, 10))
        self.assertEqual(first["observed_mean_net_r"], "0")
        self.assertEqual(first["fallback_count"], 0)
        self.assertEqual(len(first["means_net_r"]), 400)
        self.assertEqual(set(first["cost_grid"]), {"0", "1", "2", "3"})

    def test_terciles_and_fallback_preserve_hour_and_geometry(self):
        pool = [pool_row(0, -UR, volatility=Fraction(1, 100)),
                pool_row(1, 2 * UR, volatility=Fraction(2, 100), hour=1),
                pool_row(2, 3 * UR, volatility=Fraction(3, 100), hour=1)]
        bounds = tercile_boundaries(pool)
        self.assertEqual([volatility_tercile(row, bounds) for row in pool], [1, 2, 3])
        strategy = [pool_row(3, 4 * UR, volatility=Fraction(3, 100))]
        result = matched_placebo(strategy, pool, B=20, seed=0, stream_prefix="fallback")
        self.assertEqual(result["fallback_count"], 1)
        self.assertEqual(result["quantiles"], {"5": "-1", "50": "-1", "95": "-1"})
        self.assertEqual(result["p_placebo"], "0.047619047619")
        with self.assertRaises(ValueError):
            matched_placebo([pool_row(0, UR, hour=2)], pool, seed=0, stream_prefix="missing")
        with self.assertRaises(ValueError):
            matched_placebo([pool_row(0, UR, geometry=G._replace(side=-1))], pool,
                            seed=0, stream_prefix="wrong-side")


if __name__ == "__main__":
    unittest.main()
