"""Positioning screen (#188, #224): point-in-time metrics, gaps, trailing z, hidden guard, quadrants, public lines."""
from __future__ import annotations

from array import array
from pathlib import Path
import re
import tempfile
import unittest

import numpy as np

from market_analysis import data_lake
from market_analysis import metrics_lake as mx
from market_analysis.benchmark import order_flow
from market_analysis.benchmark import screen as sc
from market_analysis.benchmark.bars import BarSeries
from market_analysis.benchmark.hidden_guard import HiddenStretchLocked
from test_benchmark_screen import synthetic_results

MONTH = "2024-01"
START = data_lake.month_bounds_ms(MONTH)[0]
MINUTE, HOUR, DAY = 60_000, 3_600_000, 86_400_000


def row(create_ms, toptrader="", oi=""):
    return mx.MetricsRow(create_ms, (oi, "", "", toptrader, "", ""))


def flat_bars(minutes):
    prices = array("q", [10 ** 10] * minutes)
    columns = {name: prices for name in ("open", "high", "low", "close", "mark_open", "mark_high", "mark_low",
                                         "mark_close")}
    return BarSeries("BTCUSDT", START, minutes, volume=array("q", [1] * minutes),
                     taker_buy_volume=array("q", [0] * minutes), trades=array("q", [1] * minutes),
                     flags=array("H", [0] * minutes), **columns)


def grid_metrics(values, column="sum_toptrader_long_short_ratio"):
    """A MetricsSeries whose index i is usable from START + (i + 1) * PERIOD (as the range loader builds it)."""
    columns = {name: np.full(len(values), np.nan) for name in mx.VALUE_COLUMNS}
    columns[column] = np.asarray(values, dtype=float)
    return mx.MetricsSeries("BTCUSDT", START + mx.PERIOD_MS, len(values), columns)


class PointInTimeTests(unittest.TestCase):
    def test_usable_from_and_gaps(self):
        decision = START + 5 * HOUR
        self.assertGreater(mx.usable_from_ms(decision - 4 * MINUTE), decision)  # 4 min before: not yet usable
        self.assertEqual(mx.usable_from_ms(decision - 5 * MINUTE), decision)    # 5 min before: usable
        rows = [row(decision - 10 * MINUTE, "1.25"), row(decision - 5 * MINUTE, "1.5"), row(decision, "2.5"),
                row(decision + 5 * MINUTE, "")]  # decision + 10 min: no row at all
        with tempfile.TemporaryDirectory() as directory:
            with (Path(directory) / mx.csv_asset_name("BTCUSDT", MONTH)).open("wb") as stream:
                mx.write_metrics_csv_gz(stream, rows)
            metrics = mx.load_symbol_metrics_range(directory, "BTCUSDT", MONTH, MONTH)
        self.assertEqual(sc.positioning_value(metrics, "toptrader-ls", decision), 1.5)  # never the row at T
        self.assertEqual(sc.positioning_value(metrics, "toptrader-ls", decision + 5 * MINUTE), 2.5)
        self.assertIsNone(sc.positioning_value(metrics, "toptrader-ls", decision + 10 * MINUTE))  # empty value
        self.assertIsNone(sc.positioning_value(metrics, "toptrader-ls", decision + 15 * MINUTE))  # missing row
        self.assertIsNone(sc.positioning_value(metrics, "toptrader-ls", decision - 2 * HOUR))     # before data

    def test_gap_drops_the_decision_and_is_not_added_to_history(self):
        periods = 40 * 288
        values = np.random.default_rng(1).random(periods) + 1
        gap_decision = START + 25 * DAY
        values[(gap_decision - START) // mx.PERIOD_MS - 1] = np.nan  # the period usable at the decision
        z = {ms: (value, raw) for ms, value, raw in sc.positioning_z_series(flat_bars(40 * 1440),
                                                                            grid_metrics(values), "toptrader-ls")}
        self.assertEqual(z[gap_decision], (None, None))
        self.assertIsNotNone(z[gap_decision + HOUR][0])
        self.assertIsNone(sc.positioning_value(grid_metrics(values), "taker-ls-1h", gap_decision))

    def test_z_uses_only_the_previous_30_days(self):
        periods = 40 * 288
        values = np.random.default_rng(2).random(periods) + 1
        bars = flat_bars(40 * 1440)
        base = {ms: value for ms, value, _ in sc.positioning_z_series(bars, grid_metrics(values), "toptrader-ls")}
        t = START + 35 * DAY
        self.assertIsNone(base[START + 20 * DAY])  # 480 hourly values < 500
        changed = values.copy()
        changed[:(t - 30 * DAY - START) // mx.PERIOD_MS - 1] = 50.0  # every value usable before t - 30 d
        other = {ms: value for ms, value, _ in sc.positioning_z_series(bars, grid_metrics(changed), "toptrader-ls")}
        self.assertEqual(base[t], other[t])
        history = sorted(float(values[(ms - START) // mx.PERIOD_MS - 1]) for ms in range(t - 30 * DAY, t, HOUR))
        current = float(values[(t - START) // mx.PERIOD_MS - 1])
        self.assertAlmostEqual(base[t], float(order_flow.robust_z(history, current, sc.POSITIONING_MIN_HISTORY)))

    def test_hidden_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(HiddenStretchLocked):
                mx.load_symbol_metrics_range(directory, "BTCUSDT", "2025-12", "2026-01")
            with self.assertRaises(HiddenStretchLocked):
                sc.run_symbol(directory, "BTCUSDT", "hidden", "oi-chg-4h", directory)


class QuadrantAndReportTests(unittest.TestCase):
    def test_quadrant_dead_zone(self):
        sigma = np.full(6, 0.01)
        past_bp = np.array([24.0, -24.0, 26.0, -26.0, 26.0, 26.0])  # 0.24 / 0.26 sigma in bp
        oi = np.array([0.1, 0.1, 0.1, 0.1, -0.1, 0.0])
        self.assertEqual(sc.quadrant_index(oi, past_bp, sigma).tolist(), [-1, -1, 0, 1, 2, -1])
        self.assertEqual(sc.quadrant_index([np.nan], [100.0], [0.01]).tolist(), [-1])

    def test_public_lines_have_no_rates_or_returns(self):
        results = synthetic_results(np.random.default_rng(3), symbols=2, per_symbol=400)
        rng = np.random.default_rng(4)
        for result in results.values():
            result["raw"] = rng.standard_normal(len(result["z"])) * 0.02
        report = sc.build_report("development", results, series="oi-chg-4h", code_commit="local",
                                 created_utc="2026-10-09T00:00:00Z", B=30)
        self.assertEqual(report["series_x_horizons_examined"],
                         len(sc.POSITIONING_SERIES) * len(sc.POSITIONING_HORIZONS))
        self.assertEqual([table["horizon_min"] for table in report["quadrants"]], list(sc.QUADRANT_HORIZONS))
        lines = sc.public_lines(report)
        self.assertEqual(lines[0], "screen oi-chg-4h segment development")
        self.assertRegex(lines[-1], r"report hash [0-9a-f]{64}\Z")
        for line in lines[1:-1]:
            self.assertRegex(line, r"(S[0-9] h=[0-9]+ segment=development n=[0-9]+ excluded_compromised=[0-9]+ "
                                   r"excluded_past_end=[0-9]+|quadrant [a-z_]+ h=[0-9]+ n=[0-9]+)\Z")
            self.assertIsNone(re.search(r"beta|\bbp\b|\bic\b|rate|return|\.", line))
        self.assertEqual(sum(line.startswith("quadrant ") for line in lines), 4 * len(sc.QUADRANT_HORIZONS))
        self.assertIn("oi_up_price_up", sc.markdown(report))


if __name__ == "__main__":
    unittest.main()
