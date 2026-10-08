"""Exact selection accounting, cost scaling, calendar attribution and metrics."""
from array import array
from dataclasses import replace
from fractions import Fraction
import gzip
from math import sqrt
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from market_analysis.benchmark.evaluate import (
    DAY_MS, Trade, bootstrap_ci, daily_series, decimal_text, drawdown, metrics, net_at, select,
)
from market_analysis.benchmark.label_store import Geometry, load_geometry_columns
from market_analysis.benchmark.labels import LabelParams
from market_analysis.benchmark.scan import UR
from market_analysis.benchmark.segments import segment_bounds_ms

G = Geometry("BTCUSDT", 15, 1, Fraction(1), 0)
START = segment_bounds_ms("development")[0]
PARAMS = LabelParams(horizons=(15,), half_life_days=((15, 1),), step_minutes=((15, 5),),
                     k_grid=(Fraction(1),), rr_grid=(Fraction(1),))


def trade(net=UR, **changes):
    row = Trade(G, START, "T", 5, net, 100_000, 50_000, 2 * UR, 0, net, 100_000, 50_000)
    return replace(row, **changes)


class EvaluateTests(unittest.TestCase):
    def test_csv_selection_counts_and_daily_totals(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "labels__BTCUSDT__2024-01.csv.gz"
            rows = [f"{START},15,1,1,T,1000000,1000,10,2,2000000,T:5:2000000:100000:50000",
                    ",".join([str(START + 300_000), "15", "1", "1", "V"] + [""] * 6),
                    f"{START + 600_000},15,1,1,T,1000000,1000,10,2,2000000,S:2:-1000000:200000:-50000|T:2:2000000:100000:-50000",
                    f"{START + 900_000},15,1,1,T,1000000,1000,10,2,2000000,X:10:::"]
            with gzip.open(path, "wt", encoding="ascii", newline="") as stream:
                stream.write(",".join(PARAMS.header) + "\n" + "\n".join(rows) + "\n")
            columns = load_geometry_columns(directory, "BTCUSDT", ["2024-01"], [G], PARAMS)
            signals = [("BTCUSDT", START + i * 300_000, 1, 15) for i in range(5)]
            chosen = select(columns, signals)
            daily = daily_series(chosen, "development")
            result = metrics(chosen, daily)
        self.assertEqual((result["signals"], result["trades"], result["usable_trades"]), (5, 3, 2))
        self.assertEqual((result["non_trades"], result["not_in_grid"], result["X_count"]), ({"V": 1}, 1, 1))
        self.assertEqual(result["outcome_counts"], {"T": 1, "S": 1, "E": 0, "L": 0, "X": 1})
        self.assertEqual(result["ambiguity_share"], "0.333333333333")
        self.assertEqual(result["cost_grid_mean_net_r"], {"0": "0.65", "1": "0.5", "2": "0.35", "3": "0.2"})
        self.assertEqual(result["mean_funding_r"], "0")
        self.assertEqual(result["profit_factor"], "2")
        self.assertEqual(result["payoff_ratio"], "2")
        self.assertEqual(sum(daily), sum(net_at(row) for row in chosen if row.outcome != "X"))
        self.assertEqual((daily.typecode, daily[0], sum(daily[1:])), ("q", UR, 0))
        self.assertEqual(len(daily), 547)  # 366 days in 2024, then 181 days through June 2025

    def test_cost_identity_fractional_multiplier_and_wallet_floor(self):
        row = trade(-UR, outcome="S", cost_ur=200_000, opt_net_ur=2 * UR, opt_cost_ur=100_000, amb=1)
        self.assertEqual(net_at(row, 1), row.net_ur)
        self.assertEqual(net_at(row, 0), row.net_ur + row.cost_ur)
        self.assertEqual(net_at(row, Fraction(1, 2)), -900_000)
        self.assertEqual(net_at(row, 100), -2 * UR)
        self.assertEqual(net_at(row, 100, "opt"), -2 * UR)
        self.assertEqual(net_at(row, 1, "opt"), 2 * UR)
        self.assertEqual(net_at(trade(0, cost_ur=1), Fraction(1, 2)), Fraction(1, 2))
        with self.assertRaises(ValueError):
            net_at(trade(0, outcome="X"))
        for m in (-1, "1", True):
            with self.assertRaises(ValueError):
                net_at(row, m)
        with self.assertRaises(ValueError):
            net_at(row, policy="middle")

    def test_optimistic_sign_flip_and_entry_day_attribution(self):
        row = trade(-UR, outcome="S", opt_net_ur=2 * UR, amb=1, signal_ms=START + DAY_MS - 60_000)
        daily = daily_series([row], "development")
        self.assertGreater(row.exit_ms, START + DAY_MS)
        self.assertEqual((daily[0], daily[1]), (-UR, 0))
        self.assertTrue(metrics([row], daily)["sign_flip"])
        self.assertFalse(metrics([trade(0)], [0])["sign_flip"])
        with self.assertRaises(ValueError):
            daily_series([replace(row, signal_ms=START - 1)], "development")

    def test_quantiles_median_and_exact_decimal_rounding(self):
        rows = [trade(i * UR, wallet_ur=200 * UR) for i in range(1, 101)]
        result = metrics(rows, [sum(row.net_ur for row in rows)])
        self.assertEqual(result["quantiles_net_r"], {str(p): str(p) for p in (1, 5, 25, 75, 95, 99)})
        self.assertEqual(result["mean_net_r"], "50.5")
        self.assertEqual(result["median_net_r"], "50.5")
        self.assertEqual((result["worst_net_r"], result["best_net_r"]), ("1", "100"))
        self.assertEqual(decimal_text(Fraction(1, 3)), "0.333333333333")
        self.assertEqual(decimal_text(Fraction(125, 100), places=1), "1.2")
        self.assertEqual(decimal_text(Fraction(135, 100), places=1), "1.4")

    def test_drawdown_and_sample_daily_statistics(self):
        daily = [value * UR for value in (2, -1, -2, 1, 2, -1)]
        self.assertEqual(drawdown(daily), (3 * UR, 3))
        self.assertEqual(drawdown([-2 * UR, -UR, 3 * UR]), (3 * UR, 2))
        result = metrics([], daily)
        self.assertEqual((result["max_drawdown_r"], result["longest_underwater_days"]), ("3", 3))
        result = metrics([], [-UR, 0, UR])
        self.assertEqual(result["daily_std_r"], "1.0")
        self.assertEqual(result["annualized_sharpe"], "0.0")
        self.assertEqual(result["sortino"], "0.0")
        empty = metrics([], [])
        self.assertIsNone(empty["mean_net_r"])
        self.assertIsNone(empty["daily_mean_r"])
        self.assertIsNone(empty["annualized_sharpe"])

    def test_purged_signal_is_not_counted_as_missing_grid(self):
        from market_analysis.benchmark.label_store import GeometryColumns
        column = GeometryColumns(G)
        column.purged_signal_ms.append(START)
        chosen = select(column, [("BTCUSDT", START, 1, 15), ("BTCUSDT", START + 1, 1, 15)])
        self.assertEqual((chosen.signals, chosen.purged, chosen.not_in_grid), (2, 1, 1))
        other = GeometryColumns(G._replace(rr_index=1))
        with self.assertRaises(ValueError):
            select({G: column, other.geometry: other}, [])

    def test_bootstrap_deterministic_and_constant_series(self):
        daily = array("q", [UR, -UR, 0, 2 * UR, 0, -UR])
        first = bootstrap_ci(daily, B=80, seed=7, stream_prefix="q/mean")
        self.assertEqual(first, bootstrap_ci(daily, B=80, seed=7, stream_prefix="q/mean"))
        self.assertLessEqual(Fraction(first["lower"]), Fraction(first["upper"]))
        constant = bootstrap_ci([2 * UR] * 20, B=40, seed=7, stream_prefix="q/constant")
        self.assertEqual((constant["lower"], constant["upper"]), ("2", "2"))
        self.assertIsNone(constant["t_statistic"])
        small = bootstrap_ci([-UR, 0, 2 * UR], B=40, seed=7, stream_prefix="q/t")
        self.assertAlmostEqual(float(small["t_statistic"]), 1 / sqrt(7))
        with self.assertRaises(ValueError):
            bootstrap_ci([], B=10, seed=7, stream_prefix="q/empty")
        with self.assertRaises(ValueError):
            bootstrap_ci(daily, B=0, seed=7, stream_prefix="q/invalid")


if __name__ == "__main__":
    unittest.main()
