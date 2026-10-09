"""Robust (|r|) and horizon-calibrated sigma, labels-v3 (#220 P12): recovery, tails, point in time, hcal, guards."""
from __future__ import annotations

from array import array
from dataclasses import replace
from fractions import Fraction
import hashlib
import io
from math import log
import unittest

import numpy as np

from market_analysis import data_lake
from market_analysis.benchmark import volatility as vol
from market_analysis.benchmark.bars import BarSeries
from market_analysis.benchmark.labels import LabelParams, build_labels, read_label_csv, write_label_csv
from market_analysis.benchmark.robust_sigma import RobustSigma
from market_analysis.forward.outcomes import empty_funding
from test_forward_engine import walk_bars

START = data_lake.month_bounds_ms("2024-01")[0]
DAY_MS = 86_400_000
MINUTE = 60_000
LB2_FIXTURE_SHA256 = "edc4d5169714ddaed996edf2f13161d19ba6e3524b0393aa0cc7208ef7eac77c"  # labels-v2 before P12
LB2_FIXTURE_IDENTITY = "8784ca2524de9ec9e1a5598a3fda89ca1a89482c1cc3aeeb7d0ed7cf4d05d07b"
V2 = LabelParams(horizons=(15,), half_life_days=((15, 1),), step_minutes=((15, 15),), k_grid=(Fraction(2),),
                 rr_grid=(Fraction(3, 2), Fraction(2)), sigma_model="ewma-seasonal")


def series_from(log_steps, start=START) -> BarSeries:
    prices = [int(round(50_000 * 10 ** 8 * value)) for value in np.exp(np.cumsum(log_steps))]
    minutes = len(prices)
    columns = {name: array("q", prices) for name in ("open", "high", "low", "close",
                                                    "mark_open", "mark_high", "mark_low", "mark_close")}
    columns.update({name: array("q", [0] * minutes) for name in ("volume", "taker_buy_volume", "trades")})
    return BarSeries("BTCUSDT", start, minutes, flags=array("H", [0] * minutes), **columns)


def last_scale(series, half_life=1) -> float:
    factors = vol.seasonal_factors_abs(series)
    return vol.build_scale_robust(series, half_life, factors).variance[-1] / vol.ABS_SCALE


class RobustScaleTests(unittest.TestCase):
    def test_gaussian_scale_recovers_sigma(self):
        sigma_minute = 0.0004
        series = series_from(np.random.default_rng(1).standard_normal(17 * 1440) * sigma_minute)
        self.assertAlmostEqual(last_scale(series) / (sigma_minute * 5 ** 0.5), 1.0, delta=0.08)

    def test_heavy_tails_give_a_smaller_scale_than_squared_returns(self):
        steps = np.random.default_rng(2).standard_t(3, 17 * 1440) / 3 ** 0.5 * 0.0004  # unit-variance t3
        series = series_from(steps)
        robust = last_scale(series, 7)
        squared = (vol.build_variance(series, 7).variance[-1] / vol.VAR_SCALE) ** 0.5
        self.assertLess(robust / squared, 0.9)

    def test_point_in_time(self):
        steps = np.random.default_rng(3).standard_normal(17 * 1440) * 0.0004
        base = series_from(steps)
        changed = steps.copy()
        cut = 16 * 1440 + 300
        changed[cut:] *= 5
        other = series_from(changed)
        f1, f2 = vol.seasonal_factors_abs(base), vol.seasonal_factors_abs(other)
        first = START // DAY_MS
        for day in range(first, first + 17):
            self.assertEqual(f1.day_factors(day), f2.day_factors(day))  # day 16 factors use days < 16 only
        s1 = vol.build_scale_robust(base, 1, f1).variance
        s2 = vol.build_scale_robust(other, 1, f2).variance
        block = cut // 5 - 1  # the last block that closes before the change
        self.assertEqual(s1[:block + 1], s2[:block + 1])
        self.assertNotEqual(s1[-1], s2[-1])

    def test_horizon_sigma_aggregates_independent_blocks(self):
        flat = (vol.FACTOR_SCALE,) * 48
        s = 3 * 10 ** 7  # 0.3 % per 5 minutes
        sigma = vol.horizon_sigma_robust(s, flat, 10, 60)
        self.assertEqual(sigma, vol.isqrt(12 * s * s * vol.VAR_SCALE))  # sqrt(12) * s for 12 blocks
        loud = tuple(2 * vol.FACTOR_SCALE if slot == 47 else vol.FACTOR_SCALE for slot in range(48))
        self.assertGreater(vol.horizon_sigma_robust(s, loud, 285, 30), vol.horizon_sigma_robust(s, flat, 285, 30))

    def test_overflow_guard(self):
        steps = np.random.default_rng(4).standard_normal(2 * 1440) * 0.0004
        steps[1440 + 300] = 14.0  # an absurd jump that still fits an int64 price
        series = series_from(steps)
        ones = vol.SeasonalSeries(START // DAY_MS, 2, 28, array("q", [1]) * 96)  # factor 1e-6: amplifies |r|
        with self.assertRaises(OverflowError):
            vol.build_scale_robust(series, 1, ones)


class HorizonCalibrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.series = series_from(np.random.default_rng(5).standard_normal(70 * 1440) * 0.0004)

    def calibrate(self, sigma_fraction):
        sigma = int(sigma_fraction * vol.VAR_SCALE)
        return vol.horizon_calibration(self.series, 60, 60, lambda entry_ms: sigma)

    def test_undefined_before_60_days_and_uses_only_completed_windows(self):
        true_sigma = 0.0004 * 60 ** 0.5
        c = self.calibrate(true_sigma)
        first_entry = START + 60 * MINUTE
        self.assertIsNone(c[first_entry + 59 * DAY_MS])
        t = first_entry + 61 * DAY_MS
        self.assertIsNotNone(c[t])
        opens = self.series.open
        ratios = []
        for entry_ms in range(first_entry, t, 3_600_000):
            e = (entry_ms - START) // MINUTE
            if entry_ms + 60 * MINUTE <= t:  # windows that ended at or before t only
                ratios.append(abs(log(opens[e + 60] / opens[e])) / true_sigma)
        expected = float(np.median(ratios)) / vol.HCAL_MEDIAN_ABS_NORMAL
        self.assertAlmostEqual(c[t] / vol.HCAL_SCALE, expected, places=5)
        self.assertAlmostEqual(c[t] / vol.HCAL_SCALE, 1.0, delta=0.1)  # a correct sigma calibrates to about 1

    def test_clipped(self):
        late = START + 69 * DAY_MS
        self.assertEqual(self.calibrate(10.0)[late], vol.HCAL_SCALE // 2)    # far too wide -> 0.5
        self.assertEqual(self.calibrate(1e-7)[late], 2 * vol.HCAL_SCALE)    # far too narrow -> 2


class LabelSchemaTests(unittest.TestCase):
    def test_lb2_is_byte_identical(self):
        self.assertEqual(V2.identity(), LB2_FIXTURE_IDENTITY)
        bars = walk_bars(16 * 1440, seed=9)
        out = b""
        for _, rows in build_labels("BTCUSDT", bars, empty_funding(bars), V2, {"2025-01": 10 ** 6}):
            buffer = io.BytesIO()
            write_label_csv(buffer, rows, V2)
            out += buffer.getvalue()
        self.assertEqual(hashlib.sha256(out).hexdigest(), LB2_FIXTURE_SHA256)

    def test_labels_v3_schema_and_round_trip(self):
        for model in ("ewma-robust", "ewma-robust-hcal"):
            v3 = replace(V2, schema=None, sigma_model=model)
            self.assertEqual(v3.schema, "labels-v3")
            self.assertIn("sigma_" + model.replace("-", "_"), v3.header)
            self.assertIn("robust", v3.to_record())
            bars = walk_bars(16 * 1440, seed=9)
            rows = [row for _, month in build_labels("BTCUSDT", bars, empty_funding(bars), v3, {"2025-01": 10 ** 6})
                    for row in month]
            if model == "ewma-robust":
                self.assertTrue(any(row.status == "T" for row in rows))
            else:
                self.assertTrue(all(row.status in ("V", "I") for row in rows))  # hcal needs 60 days of windows
            buffer = io.BytesIO()
            write_label_csv(buffer, rows, v3)
            self.assertEqual(list(read_label_csv(io.BytesIO(buffer.getvalue()), v3)), rows)

    def test_robust_sigma_matches_its_parts(self):
        bars = walk_bars(16 * 1440, seed=9)
        robust = RobustSigma(bars, hcal=False)
        t = bars.start_ms + 15 * DAY_MS + 30 * MINUTE
        block = (t - bars.start_ms) // MINUTE // 5 - 1
        expected = vol.horizon_sigma_robust(robust.levels(1)[block], robust.factors.day_factors(t // DAY_MS),
                                            (t % DAY_MS) // vol.BLOCK_MS, 15)
        self.assertEqual(robust.sigma(15, 1, 15, t), expected)


if __name__ == "__main__":
    unittest.main()
