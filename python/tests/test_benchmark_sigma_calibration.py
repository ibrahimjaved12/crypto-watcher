"""Sigma calibration audit (#220 slice A): z statistics, variance ratios, barrier theory, guard (synthetic data)."""
from __future__ import annotations

from array import array
import importlib.util
from math import exp
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

from market_analysis import data_lake
from market_analysis.benchmark import calibration as cal
from market_analysis.benchmark.bars import BarSeries
from market_analysis.benchmark.hidden_guard import HiddenStretchLocked
from market_analysis.benchmark.label_store import Geometry, GeometryColumns
from market_analysis.benchmark.labels import LabelParams

START = data_lake.month_bounds_ms("2024-01")[0]
DAY_MS = 86_400_000


def brownian_series(days=10, sigma_per_minute=0.0005, seed=11):
    """Geometric Brownian path, open = close = high = low per minute (prices scaled by 10**8)."""
    minutes = days * 1440
    steps = np.random.default_rng(seed).standard_normal(minutes) * sigma_per_minute
    prices = [int(round(50_000 * 10 ** 8 * exp(value))) for value in np.cumsum(steps)]
    columns = {name: array("q", prices) for name in ("open", "high", "low", "close",
                                                    "mark_open", "mark_high", "mark_low", "mark_close")}
    columns.update({name: array("q", [0] * minutes) for name in ("volume", "taker_buy_volume", "trades")})
    return BarSeries("BTCUSDT", START, minutes, flags=array("H", [0] * minutes), **columns)


class ZStatisticsTests(unittest.TestCase):
    def test_brownian_series_gives_sd_near_one(self):
        series = brownian_series()
        result = cal.audit_series(series, START + 2 * DAY_MS, START + 10 * DAY_MS, horizons=(15,), half_lives=(1,))
        row = result["horizons"]["15"]["1"]
        self.assertGreater(row["z"]["n"], 2000)
        self.assertEqual(sum(row["skipped"].values()), 0)
        self.assertTrue(0.9 <= row["z"]["sd"] <= 1.1, row["z"]["sd"])
        self.assertTrue(row["sd_ok"])
        self.assertIsNone(row["barrier_ok"])  # only 240 m rows carry a barrier verdict
        self.assertTrue(row["pass"])
        self.assertEqual(len(row["z_by_hour"]), 24)
        self.assertEqual(sum(hour["n"] for hour in row["z_by_hour"]), row["z"]["n"])

    def test_entries_are_the_label_grid(self):
        series, params = brownian_series(days=3), LabelParams()
        first, end = START + DAY_MS, START + 3 * DAY_MS
        for horizon in (15, 60, 240):
            entries = cal.entry_indices(series, first, end, horizon, params)
            signal_ms = START + entries * 60_000
            self.assertTrue(np.all(signal_ms % (params.step(horizon) * 60_000) == 0))
            self.assertTrue(np.all(signal_ms >= first))
            self.assertTrue(np.all(signal_ms + params.window(horizon) * 60_000 < end))

    def test_z_stats_of_constructed_values(self):
        stats = cal.z_stats([-3.5, -1.5, 0.0, 0.5, 2.5])
        self.assertEqual(stats["n"], 5)
        self.assertAlmostEqual(stats["mean_abs"], 1.6)
        self.assertAlmostEqual(stats["share_abs_gt"]["1"], 0.6)
        self.assertAlmostEqual(stats["share_abs_gt"]["3"], 0.2)
        self.assertAlmostEqual(stats["quantiles"]["50"], 0.0)
        self.assertEqual(cal.z_stats([])["n"], 0)


class VarianceRatioTests(unittest.TestCase):
    def test_iid_series_is_near_one(self):
        returns = np.random.default_rng(3).standard_normal(200_000)
        for q in cal.VR_QS:
            with self.subTest(q=q):
                self.assertLess(abs(cal.variance_ratio(returns, q)["vr"] - 1), 0.08)

    def test_negative_autocorrelation_is_below_one(self):
        noise = np.random.default_rng(4).standard_normal(100_000)
        returns = np.empty_like(noise)
        returns[0] = noise[0]
        for i in range(1, noise.size):
            returns[i] = -0.3 * returns[i - 1] + noise[i]
        for q in cal.VR_QS:
            with self.subTest(q=q):
                self.assertLess(cal.variance_ratio(returns, q)["vr"], 0.8)

    def test_q_sums_need_every_return(self):
        sums = cal.q_sums([1.0, 2.0, np.nan, 4.0, 5.0, 6.0], 2)
        np.testing.assert_array_equal(sums, [np.nan, 3.0, np.nan, np.nan, 9.0, 11.0])

    def test_expanding_window_uses_no_future_data(self):
        rng = np.random.default_rng(5)
        returns = rng.standard_normal(5_000)
        returns[rng.integers(0, 5_000, 200)] = np.nan
        cut = 3_000
        changed = returns.copy()
        changed[cut:] = rng.standard_normal(5_000 - cut) * 7 + 3
        for q in cal.VR_QS:
            with self.subTest(q=q):
                before = cal.expanding_variance_ratio(returns, q, min_q_sums=100)
                after = cal.expanding_variance_ratio(changed, q, min_q_sums=100)
                np.testing.assert_array_equal(before[:cut], after[:cut])
                self.assertTrue(np.isnan(before[:q + 50]).all())
                # The last expanding value is the full-sample estimator.
                self.assertAlmostEqual(before[-1], cal.variance_ratio(returns, q)["vr"], places=9)


class BarrierTheoryTests(unittest.TestCase):
    def test_series_expansion_known_values(self):
        self.assertAlmostEqual(cal.expiry_probability(3.0, 2.0, 4), 0.55, delta=0.01)   # k 2, rr 1.5
        self.assertAlmostEqual(cal.expiry_probability(1.0, 1.0, 4), 0.01, delta=0.005)  # k 1, rr 1
        self.assertAlmostEqual(cal.expiry_probability(2.0, 1.0, 4), 0.12, delta=0.01)   # k 1, rr 2
        self.assertAlmostEqual(cal.expiry_probability(4.0, 2.0, 4), 0.64, delta=0.01)   # k 2, rr 2
        self.assertAlmostEqual(cal.target_first_probability(3.0, 2.0), 0.4)

    def test_discrete_widening_raises_expiry(self):
        widening = cal.discrete_widening(15, 240)  # 0.5826 * sigma_step / sigma_h, sigma_step = sigma_h sqrt(15/240)
        self.assertAlmostEqual(widening, 0.5826 / 4)
        self.assertAlmostEqual(cal.expiry_probability(3 + widening, 2 + widening, 4), 0.60, delta=0.01)
        # Widened barriers are further away, so more trades reach the time limit: every cell's share rises.
        summary = cal.barrier_summary({}, LabelParams())
        self.assertEqual(len(summary["geometries"]), 8)
        for row in summary["geometries"]:
            with self.subTest(k=row["k"], rr=row["rr"]):
                self.assertGreater(row["theory"]["expiry_widened"], row["theory"]["expiry_continuous"])

    def test_implied_sigma_ratio_round_trip(self):
        observed = cal.expiry_probability(3 / 0.85, 2 / 0.85, 4)
        self.assertAlmostEqual(cal.implied_sigma_ratio(observed, 3.0, 2.0, 4), 0.85, places=6)
        self.assertIsNone(cal.implied_sigma_ratio(float("nan"), 3.0, 2.0, 4))

    def test_barrier_summary_counts_outcomes(self):
        params = LabelParams()
        columns = {}
        for geometry in cal.barrier_geometries("BTCUSDT", params):
            column = GeometryColumns(geometry)
            if geometry.k == 2 and geometry.rr_index == 1:
                for outcome in "TTSEEEEEEX":
                    for name in column.data:
                        column[name].append(ord(outcome) if name == "outcome" else 0)
                column.non_trades["signal_ms"].append(0)
                column.non_trades["reason"].append(ord("V"))
            columns[Geometry(*geometry)] = column
        summary = cal.barrier_summary(columns, params)
        row = next(item for item in summary["geometries"] if item["k"] == "2" and item["rr"] == "3/2")
        both = row["sides"]["both"]
        self.assertEqual(both["trades"], 20)
        self.assertEqual(both["counts"]["E"], 12)
        self.assertAlmostEqual(both["shares"]["E"], 0.6)
        self.assertEqual(both["non_trades"], {"V": 2})
        self.assertAlmostEqual(both["target_first_resolved"], 4 / 6)
        self.assertTrue(row["expiry_within_tolerance"])  # 0.60 vs BGK-widened theory about 0.60
        self.assertFalse(summary["pass"])  # the other geometries have no trades


    def test_tiny_theory_cell_tolerates_one_point_deviation(self):
        widening = cal.discrete_widening(15, 240)
        theory = cal.expiry_probability(1 + widening, 1 + widening, 4)  # k 1, rr 1: a few percent
        self.assertLess(theory, 0.05)
        # 1 pp is far beyond 10 % relative here: only the 2 pp absolute floor accepts it.
        self.assertGreater(0.01, cal.EXPIRY_TOLERANCE_REL * theory)
        for observed in (theory - 0.01, theory + 0.01):
            self.assertTrue(cal.expiry_within_tolerance(observed, theory), observed)
        self.assertFalse(cal.expiry_within_tolerance(theory + 0.021, theory))

    def test_headline_cell_deviation_beyond_band_fails(self):
        widening = cal.discrete_widening(15, 240)
        theory = cal.expiry_probability(3 + widening, 2 + widening, 4)  # k 2, rr 1.5: about 60 %
        tolerance = max(cal.EXPIRY_TOLERANCE_REL * theory, cal.EXPIRY_TOLERANCE_ABS)
        # Here the 10 % relative term (about 6.0 pp) dominates the 2 pp floor, so a 5 pp deviation is
        # still inside the band under this rule; deviations beyond about 6 pp fail on either side.
        self.assertAlmostEqual(tolerance, 0.060, delta=0.001)
        for observed in (theory + 0.065, theory - 0.065, float("nan")):
            self.assertFalse(cal.expiry_within_tolerance(observed, theory), observed)


class ReportAndGuardTests(unittest.TestCase):
    def test_public_lines_carry_only_allowed_fields(self):
        series = brownian_series(days=4)
        result = cal.audit_series(series, START + 2 * DAY_MS, START + 4 * DAY_MS, horizons=(15,), half_lives=(1,))
        report = cal.build_report("development", {"BTCUSDT": result}, horizons=(15,), half_lives=(1,),
                                  params=LabelParams(), code_commit="local", created_utc="2026-10-09T00:00:00Z",
                                  data_snapshot_id=None)
        lines = cal.public_lines(report)
        self.assertEqual(lines[0], "calibration audit segment development")
        self.assertRegex(lines[-1], r"report hash [0-9a-f]{64}\Z")
        for line in lines[1:-1]:
            self.assertRegex(line, r"[A-Z]+USDT h=(15|60|240) hl=[137] n=[0-9]+ sd=(PASS|FAIL) "
                                   r"barrier=(PASS|FAIL|NA) (PASS|FAIL)\Z")
        self.assertIn(" barrier=NA ", lines[1])  # 15 m row: no barrier verdict
        json_name, md_name = cal.report_paths(report)
        self.assertTrue(json_name.startswith("reports/calibration/development__"))
        self.assertIn("## BTCUSDT", cal.markdown(report))
        report["segment"] = "validation"
        with self.assertRaises(ValueError):
            cal.public_lines(report)

    def test_verdicts_are_independent(self):
        row = {"z": {"n": 10}, "sd_ok": True, "barrier_ok": None, "pass": True}
        result = {"horizons": {"240": {"7": dict(row)}, "60": {"7": dict(row)}}}
        cal.set_barrier_verdict(result, False)
        failed = result["horizons"]["240"]["7"]
        self.assertEqual((failed["sd_ok"], failed["barrier_ok"], failed["pass"]), (True, False, False))
        self.assertIsNone(result["horizons"]["60"]["7"]["barrier_ok"])
        report = cal.build_report("development", {"BTCUSDT": result}, horizons=(60, 240), half_lives=(7,),
                                  params=LabelParams(), code_commit="local", created_utc="2026-10-09T00:00:00Z",
                                  data_snapshot_id=None)
        lines = cal.public_lines(report)
        self.assertIn("BTCUSDT h=240 hl=7 n=10 sd=PASS barrier=FAIL FAIL", lines)
        self.assertIn("BTCUSDT h=60 hl=7 n=10 sd=PASS barrier=NA PASS", lines)
        cal.set_barrier_verdict(result, True)
        self.assertTrue(result["horizons"]["240"]["7"]["pass"])

    def test_hidden_segment_is_refused_by_the_guard(self):
        with self.assertRaises(HiddenStretchLocked):
            cal.check_segment("hidden")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(HiddenStretchLocked):
                cal.audit_symbol(directory, directory, "BTCUSDT", "hidden")
        self.assertEqual(len(cal.check_segment("development")), 18)
        self.assertEqual(len(cal.check_segment("validation")), 6)

    def test_script_refuses_hidden_before_any_access(self):
        scripts = Path(__file__).resolve().parents[2] / "scripts" / "research"
        definition = importlib.util.spec_from_file_location("test_calibration_script", scripts / "calibration_audit.py")
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
        with self.assertRaises(ValueError):
            module.AuditProgress._check("evaluate", {})
        module.AuditProgress._check("audit", {"symbol_index": 1, "symbols": 6})


if __name__ == "__main__":
    unittest.main()
