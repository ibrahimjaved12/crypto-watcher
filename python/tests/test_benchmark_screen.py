"""Screening mode (#220 slice B): cum240 refactor, forward returns, regression, buckets, dose-response (synthetic)."""
from __future__ import annotations

from array import array
from fractions import Fraction
import importlib.util
from math import log
from pathlib import Path
import random
import re
import sys
import tempfile
import unittest

import numpy as np

from market_analysis import data_lake
from market_analysis.benchmark import order_flow
from market_analysis.benchmark import screen as sc
from market_analysis.benchmark.bars import BarSeries, CompromisedIndex
from market_analysis.benchmark.costs import COST_MODEL_V1
from market_analysis.benchmark.funding import FundingSeries
from market_analysis.benchmark.hidden_guard import HiddenStretchLocked

START = data_lake.month_bounds_ms("2024-03")[0]
MINUTE = 60_000


def make_bars(taker_buy, opens=None, flags=None):
    minutes = len(taker_buy)
    opens = opens or [10 ** 10] * minutes
    columns = {name: array("q", opens) for name in ("open", "high", "low", "close", "mark_open", "mark_high",
                                                    "mark_low", "mark_close")}
    return BarSeries("BTCUSDT", START, minutes, volume=array("q", [1000] * minutes),
                     taker_buy_volume=array("q", taker_buy), trades=array("q", [1] * minutes),
                     flags=array("H", flags or [0] * minutes), **columns)


def reference_cum240(bars, *, theta, rearm_below, window_min, decision_step_min, history_days, min_history,
                     cooldown_min):
    """The cum240 body as it was before the z-series extraction (behavior reference)."""
    compromised = CompromisedIndex(bars)
    history, zs = order_flow.TrailingHistory(history_days * 1440 * MINUTE), []
    step = decision_step_min * MINUTE
    first = -bars.start_ms % step // MINUTE
    for e in range(first, bars.minutes + 1, decision_step_min):
        signal_ms = bars.start_ms + e * MINUTE
        d = e - 1
        value = None
        if d - window_min + 1 >= 0 and not compromised.any_in(d - window_min + 1, d):
            value = order_flow.imbalance(sum(bars.volume[d - window_min + 1:d + 1]),
                                         sum(bars.taker_buy_volume[d - window_min + 1:d + 1]))
        z = None if value is None else order_flow.robust_z(history.at(signal_ms), value, min_history)
        if value is not None:
            history.add(signal_ms, value)
        zs.append((signal_ms, z))
    return [(signal_ms, order_flow.sign(z)) for signal_ms, z in order_flow.crossing_signals(
        zs, theta=theta, rearm_below=rearm_below, cooldown_ms=cooldown_min * MINUTE)]


class RefactorTests(unittest.TestCase):
    def test_cum240_unchanged(self):
        rng = random.Random(220)
        taker = [rng.randint(430, 570) for _ in range(6 * 1440)]
        for minute in range(4000, 4120):
            taker[minute] = 950
        flags = [0] * len(taker)
        flags[2000] = data_lake.FLAG_NO_AGGTRADES
        bars = make_bars(taker, flags=flags)
        params = dict(order_flow.STRATEGIES["of_cum240_4h"][1], history_days=2, min_history=30)
        funding = FundingSeries((), (), (), START, START + 10 * 86_400_000)
        expected = reference_cum240(bars, **params)
        self.assertTrue(expected)
        self.assertEqual(order_flow.cum240(bars, funding, **params), expected)
        zs = order_flow.cum240_z_series(bars, window_min=240, decision_step_min=60, history_days=2, min_history=30)
        self.assertEqual([(ms, order_flow.sign(z)) for ms, z in order_flow.crossing_signals(
            zs, theta=Fraction(2), rearm_below=Fraction(1), cooldown_ms=240 * MINUTE)], expected)


class ForwardReturnTests(unittest.TestCase):
    def test_entry_next_open_exit_open_e_plus_h_and_exclusions(self):
        opens = [10 ** 10 + 10 ** 7 * i for i in range(600)]
        flags = [0] * 600
        flags[450] = data_lake.FLAG_NO_AGGTRADES
        bars = make_bars([500] * 600, opens=opens, flags=flags)
        r, compromised, past_end = sc.forward_returns(bars, np.array([10, 300, 400, 500]), 60, 550)
        self.assertAlmostEqual(r[0], log(opens[70] / opens[10]) * 1e4)
        self.assertAlmostEqual(r[1], log(opens[360] / opens[300]) * 1e4)
        self.assertEqual((bool(compromised[2]), bool(np.isnan(r[2]))), (True, True))  # minute 450 inside
        self.assertEqual((bool(past_end[3]), bool(np.isnan(r[3]))), (True, True))      # exit 560 >= end 550
        r2, _, past = sc.forward_returns(bars, np.array([10]), 60, 70)
        self.assertTrue(past[0] and np.isnan(r2[0]))  # exit at the segment end is excluded


def synthetic_results(rng, symbols=3, per_symbol=1500, beta=0.5, drift=0.0, n_days=547):
    results = {}
    for s in range(symbols):
        z = rng.standard_normal(per_symbol) * 1.5
        past = rng.standard_normal(per_symbol) * 50
        sigma = 0.01 + 0.005 * rng.random(per_symbol)
        result = {"symbol": f"S{s}", "day": rng.integers(0, n_days, per_symbol), "hour": rng.integers(0, 24,
                                                                                                  per_symbol),
                  "z": z, "past240": past, "sigma240": sigma, "returns": {}, "funding_bp": {}, "excluded": {}}
        for h in sc.HORIZONS:
            result["returns"][h] = drift + beta * z + rng.standard_normal(per_symbol)
            result["funding_bp"][h] = np.full(per_symbol, 0.5)
            result["excluded"][h] = {"compromised": 0, "past_end": 0}
        results[f"S{s}"] = result
    return results


class RegressionTests(unittest.TestCase):
    def test_recovers_beta_with_and_without_controls(self):
        results = synthetic_results(np.random.default_rng(1))
        out = sc.screen_horizon(results, 60, 547, B=50)
        for label in ("with_controls", "without_controls"):
            beta = out[label]["coefficients"]["z"]["beta"]
            self.assertAlmostEqual(beta, 0.5, delta=0.05)
        self.assertGreater(out["ic_spearman"], 0.3)

    def test_clustering_resists_duplicated_data(self):
        rng = np.random.default_rng(2)
        clusters = np.repeat(np.arange(100), 10)
        x = rng.standard_normal(1000) + np.repeat(rng.standard_normal(100), 10)
        y = 0.5 * x + np.repeat(rng.standard_normal(100), 10) + rng.standard_normal(1000)
        X = np.column_stack([np.ones(1000), x])
        base = sc.ols_clustered(y, X, clusters)
        twice = sc.ols_clustered(np.concatenate([y, y]), np.vstack([X, X]), np.concatenate([clusters, clusters]))
        self.assertLess(twice["se_naive"][1] / base["se_naive"][1], 0.75)
        self.assertGreater(twice["se"][1] / base["se"][1], 0.9)


class BucketTests(unittest.TestCase):
    def test_bucket_boundaries(self):
        values = [1.99, 2.0, 2.49, 2.5, 2.99, 3.0, 3.49, 3.5, 10.0, float("nan")]
        self.assertEqual(sc.bucket_index(values).tolist(), [-1, 0, 0, 1, 1, 2, 2, 3, 3, -1])

    def test_drift_adjustment_removes_constant_drift(self):
        side = np.array([1, -1, 1, -1])
        r = np.full(4, 7.0)
        self.assertEqual(sc.drift_adjusted(side, r, 7.0).tolist(), [0.0, 0.0, 0.0, 0.0])
        out = sc.screen_horizon(synthetic_results(np.random.default_rng(3), beta=0.0, drift=25.0), 60, 547, B=50)
        for row in out["buckets"]:
            if row["n"] > 30:
                self.assertLess(abs(row["drift_adjusted_bp"]["mean"]), 1.0)
                self.assertAlmostEqual(abs(row["gross_bp"]["mean"]), 25.0, delta=1.0)

    def test_cost_is_the_cost_model_round_trip(self):
        expected = 2 * (COST_MODEL_V1.taker_rate * 10_000 + COST_MODEL_V1.market_slip_floor_bps)
        self.assertEqual(sc.ROUND_TRIP_COST_BP, float(expected))
        self.assertEqual(sc.ROUND_TRIP_COST_BP, 12.0)

    def test_dose_response_slope(self):
        rng = np.random.default_rng(4)
        bucket = rng.integers(0, 4, 4000)
        clusters = rng.integers(0, 200, 4000)
        weights = sc.cluster_weights(200, B=200)
        monotone = sc.dose_response(bucket, 2.0 * bucket + rng.standard_normal(4000), clusters, weights)
        self.assertAlmostEqual(monotone["slope_bp_per_bucket"], 2.0, delta=0.2)
        self.assertGreater(monotone["slope_ci95"][0], 0)
        self.assertAlmostEqual(monotone["spearman"], 1.0)
        flat = sc.dose_response(bucket, rng.standard_normal(4000), clusters, weights)
        self.assertLess(abs(flat["slope_bp_per_bucket"]), 0.2)
        self.assertLessEqual(flat["slope_ci95"][0], flat["slope_bp_per_bucket"])
        self.assertLessEqual(flat["slope_bp_per_bucket"], flat["slope_ci95"][1])


class ReportAndGuardTests(unittest.TestCase):
    def test_public_lines_carry_counts_only(self):
        results = synthetic_results(np.random.default_rng(5), symbols=2, per_symbol=400)
        report = sc.build_report("development", results, code_commit="local", created_utc="2026-10-09T00:00:00Z",
                                 B=50)
        self.assertEqual(report["series_x_horizons_examined"], 6)
        lines = sc.public_lines(report)
        self.assertEqual(lines[0], "screen of-cum240-z segment development")
        self.assertRegex(lines[-1], r"report hash [0-9a-f]{64}\Z")
        self.assertEqual(len(lines), 2 + 2 * len(sc.HORIZONS))
        for line in lines[1:-1]:
            self.assertRegex(line, r"S[0-9] h=[0-9]+ segment=development n=[0-9]+ excluded_compromised=[0-9]+ "
                                   r"excluded_past_end=[0-9]+\Z")
            self.assertIsNone(re.search(r"beta|bp|ic|\.", line))
        self.assertTrue(sc.report_paths(report)[0].startswith("reports/screens/of-cum240-z__development__"))
        self.assertIn("Dose-response", sc.markdown(report))

    def test_hidden_segment_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(HiddenStretchLocked):
                sc.run_symbol(directory, "BTCUSDT", "hidden")
        scripts = Path(__file__).resolve().parents[2] / "scripts" / "research"
        definition = importlib.util.spec_from_file_location("test_screen_script", scripts / "screen.py")
        module = importlib.util.module_from_spec(definition)
        previous = sys.path[:]
        try:
            sys.path.insert(0, str(scripts))
            definition.loader.exec_module(module)
        finally:
            sys.path[:] = previous
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory) / "work"
            with self.assertRaises(module.PublicError):
                module.main(["--workdir", str(workdir), "--segment", "hidden"])
            self.assertFalse(workdir.exists())


if __name__ == "__main__":
    unittest.main()
