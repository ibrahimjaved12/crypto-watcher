"""Seasonal (intraday) sigma and labels-v2 (#220 P8): point in time, normalisation, calibration (synthetic bars)."""
from __future__ import annotations

from array import array
from dataclasses import replace
import hashlib
import io
from math import isqrt
import unittest

import numpy as np

from market_analysis import data_lake
from market_analysis.benchmark import calibration as cal
from market_analysis.benchmark import volatility as vol
from market_analysis.benchmark.bars import BarSeries
from market_analysis.benchmark.labels import LabelParams, read_label_csv
from test_benchmark_labels import PARAMS, build, csv_bytes

START = data_lake.month_bounds_ms("2024-01")[0]
DAY_MS = 86_400_000
SCALE = vol.FACTOR_SCALE
LB1_FIXTURE_SHA256 = "323d7cb4d1d2fe94a366364515f4fa93e24b1875945fe4012fdfe05d40f535f7"  # labels-v1 before #220 P8
LB1_DEFAULT_IDENTITY = "26038d2bfd1f22be38bf9c160b064f9468731240df0e7946a28c3e199a21df4b"
LB1_FIXTURE_IDENTITY = "ceb87ac997af70a1e6cdea44bc4d7854da968dbb314703039d5cc4e0d45c9b27"


def series_from(log_steps, flags=None) -> BarSeries:
    prices = [int(round(50_000 * 10 ** 8 * value)) for value in np.exp(np.cumsum(log_steps))]
    minutes = len(prices)
    columns = {name: array("q", prices) for name in ("open", "high", "low", "close",
                                                    "mark_open", "mark_high", "mark_low", "mark_close")}
    columns.update({name: array("q", [0] * minutes) for name in ("volume", "taker_buy_volume", "trades")})
    return BarSeries("BTCUSDT", START, minutes, flags=array("H", flags or [0] * minutes), **columns)


def seasonal_steps(days, seed=5, sigma=0.0004, loud_hours=(13, 14, 15), loud=3.0):
    minutes = days * 1440
    hours = (np.arange(minutes) // 60) % 24
    scale = np.where(np.isin(hours, loud_hours), loud, 1.0) * sigma
    return np.random.default_rng(seed).standard_normal(minutes) * scale


class FactorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.steps = seasonal_steps(20)
        cls.factors = vol.seasonal_factors(series_from(cls.steps))

    def test_factors_use_only_previous_days(self):
        changed = self.steps.copy()
        day = 17
        changed[day * 1440 + 600:(day + 1) * 1440] *= 4  # day 17's returns change
        other = vol.seasonal_factors(series_from(changed))
        first = START // DAY_MS
        for d in range(first, first + day + 1):
            self.assertEqual(self.factors.day_factors(d), other.day_factors(d), d)
        self.assertNotEqual(self.factors.day_factors(first + day + 1), other.day_factors(first + day + 1))

    def test_factors_average_exactly_one_and_warm_up(self):
        first = START // DAY_MS
        for d in range(first, first + 14):  # 14 qualifying previous days are needed
            self.assertIsNone(self.factors.day_factors(d))
        for d in range(first + 14, first + 20):
            values = self.factors.day_factors(d)
            self.assertEqual(len(values), 48)
            self.assertEqual(sum(values), 48 * SCALE)
            self.assertTrue(all(value > 0 for value in values))
        loud = self.factors.day_factors(first + 19)
        self.assertGreater(min(loud[26:32]), 3 * SCALE)  # 13:00-16:00 UTC carry 9x the variance
        self.assertLess(max(loud[:26]), 0.85 * SCALE)

    def test_compromised_blocks_are_skipped(self):
        steps = seasonal_steps(16, seed=8)
        flags = [0] * len(steps)
        crazy = steps.copy()
        bad = data_lake.FLAG_NO_AGGTRADES
        for minute in range(5 * 1440 + 300, 5 * 1440 + 330):  # a compromised stretch with absurd prices
            flags[minute] = bad
            crazy[minute] += 0.5 if minute % 2 else -0.5
        crazy[5 * 1440 + 330] -= crazy[5 * 1440 + 300:5 * 1440 + 330].sum() - steps[5 * 1440 + 300:5 * 1440 + 330].sum()
        clean = vol.seasonal_factors(series_from(steps, flags))
        dirty = vol.seasonal_factors(series_from(crazy, flags))
        first = START // DAY_MS
        self.assertEqual(clean.day_factors(first + 15), dirty.day_factors(first + 15))
        # A day below 90 % valid blocks does not qualify: day 14 then lacks its 14th previous day.
        mostly_bad = [bad if 3 * 1440 <= minute < 3 * 1440 + 200 else 0 for minute in range(len(steps))]
        self.assertIsNotNone(clean.day_factors(first + 14))
        self.assertIsNone(vol.seasonal_factors(series_from(steps, mostly_bad)).day_factors(first + 14))


class HorizonSigmaTests(unittest.TestCase):
    def test_midnight_crossing_uses_the_entry_day_factors(self):
        factors = tuple(SCALE + (slot - 23) * 10_000 for slot in range(48))
        factors = tuple(value + (48 * SCALE - sum(factors)) * (slot == 0) for slot, value in enumerate(factors))
        var = 7 * 10 ** 12
        sigma = vol.horizon_sigma_seasonal(var, factors, 285, 30)  # blocks 285..287 then 0..2
        weight = 3 * factors[47] + 3 * factors[0]
        self.assertEqual(sigma, isqrt(var * weight * vol.VAR_SCALE // SCALE))
        flat = (SCALE,) * 48
        for block, horizon in ((0, 15), (100, 240), (287, 60)):
            self.assertEqual(vol.horizon_sigma_seasonal(var, flat, block, horizon), vol.horizon_sigma(var, horizon))
        with self.assertRaises(ValueError):
            vol.horizon_sigma_seasonal(var, None, 0, 15)

    def test_overflow_guard_matches_build_variance(self):
        steps = seasonal_steps(16, seed=2)
        steps[15 * 1440 + 400] = 14.0  # an absurd jump (still an int64 price) on a day with factors
        series = series_from(steps)
        with self.assertRaises(OverflowError):
            vol.build_variance(series, 1)
        with self.assertRaises(OverflowError):
            vol.build_variance_deseasonalised(series, 1, vol.seasonal_factors(series))


class CalibrationTests(unittest.TestCase):
    def test_seasonal_sigma_calibrates_every_hour_where_plain_ewma_does_not(self):
        series = series_from(seasonal_steps(26, seed=3))
        result = cal.audit_series(series, START + 18 * DAY_MS, START + 26 * DAY_MS, horizons=(15,), half_lives=(1,),
                                  sigma_models=("ewma", "ewma-seasonal"))
        plain, seasonal = result["horizons"]["15"]["1"], result["horizons_seasonal"]["15"]["1"]
        self.assertEqual(seasonal["sigma_model"], "ewma-seasonal")
        by_hour = {row["hour"]: row for row in seasonal["z_by_hour"]}
        for hour in range(24):
            self.assertTrue(0.75 <= by_hour[hour]["sd"] <= 1.3, (hour, by_hour[hour]["sd"]))
        plain_hour = {row["hour"]: row for row in plain["z_by_hour"]}
        self.assertGreater(plain_hour[14]["sd"], 1.6)
        self.assertLess(plain_hour[3]["sd"], 0.85)
        self.assertFalse(plain["robust_ok"])

    def test_robust_statistics_of_a_heavy_tailed_fixture(self):
        z = np.random.default_rng(1).standard_t(5, 200_000) * np.sqrt(3 / 5)  # unit variance, kurtosis 9
        stats = cal.z_stats(z)
        self.assertAlmostEqual(stats["sd"], 1.0, delta=0.03)
        self.assertAlmostEqual(stats["robust_sd"], 0.8346, delta=0.02)   # 1.4826 MAD < sd for heavy tails
        self.assertAlmostEqual(stats["mean_abs_ratio"], 0.921, delta=0.02)
        normal = cal.z_stats(np.random.default_rng(2).standard_normal(200_000))
        self.assertAlmostEqual(normal["robust_sd"], 1.0, delta=0.02)
        self.assertAlmostEqual(normal["mean_abs_ratio"], 1.0, delta=0.02)
        hours = [dict(normal, hour=h) for h in range(24)]
        self.assertTrue(cal.robust_ok(normal, hours))
        self.assertFalse(cal.robust_ok(stats, hours))
        self.assertFalse(cal.robust_ok(normal, hours[:23] + [dict(stats, hour=23)]))


class LabelSchemaTests(unittest.TestCase):
    def test_ewma_model_reproduces_lb1_byte_for_byte(self):
        self.assertEqual(LabelParams().identity(), LB1_DEFAULT_IDENTITY)
        self.assertEqual(PARAMS.identity(), LB1_FIXTURE_IDENTITY)
        explicit = replace(PARAMS, schema=None, sigma_model="ewma")
        self.assertEqual(explicit, PARAMS)
        digest = hashlib.sha256(b"".join(csv_bytes(rows, explicit) for _, rows in build(params=explicit)))
        self.assertEqual(digest.hexdigest(), LB1_FIXTURE_SHA256)

    def test_labels_v2_schema_header_and_round_trip(self):
        v2 = replace(PARAMS, schema=None, sigma_model="ewma-seasonal")
        self.assertEqual(v2.schema, "labels-v2")
        self.assertIn("sigma_ewma_seasonal", v2.header)
        self.assertNotEqual(v2.identity(), PARAMS.identity())
        self.assertEqual(v2.to_record()["sigma_model"], "ewma-seasonal")
        with self.assertRaises(ValueError):
            LabelParams(schema="labels-v1", sigma_model="ewma-seasonal")
        rows = [row for _, month_rows in build(params=v2) for row in month_rows]
        self.assertTrue(rows and all(row.status in ("V", "I") for row in rows))  # 2 days: no factors yet
        parsed = list(read_label_csv(io.BytesIO(csv_bytes(rows, v2)), v2))
        self.assertEqual(parsed, rows)
        with self.assertRaises(ValueError):
            list(read_label_csv(io.BytesIO(csv_bytes(rows, v2)), PARAMS))  # a v1 reader refuses v2 files


if __name__ == "__main__":
    unittest.main()
