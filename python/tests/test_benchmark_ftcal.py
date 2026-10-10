"""ewma-robust-ftcal (candidate, audit only): the sup|W| constant, the path statistic M_e, point in time, guards."""
from __future__ import annotations

from array import array
from dataclasses import replace
from functools import lru_cache
from math import exp, log, pi, sqrt
import unittest

import numpy as np

from market_analysis import data_lake
from market_analysis.benchmark import calibration as cal
from market_analysis.benchmark import volatility as vol
from market_analysis.benchmark.bars import MISSING, BarSeries
from market_analysis.benchmark.labels import CANDIDATE_SIGMA_MODELS, SIGMA_MODELS, LabelParams
from market_analysis.benchmark.robust_sigma import CANDIDATE_MODELS, FTCAL_MODEL, ROBUST_MODELS, RobustSigma
from market_analysis.forward.setups import FORWARD_PARAMS, ForwardSigma

START = data_lake.month_bounds_ms("2024-01")[0]
MINUTE = 60_000
DAY = 86_400_000
SIGMA = 2 * 10 ** 17        # sigma / VAR_SCALE = 0.002 (log scale)


@lru_cache(maxsize=4)
def walk(minutes: int, seed=1, drift_scale=0.0006) -> BarSeries:
    """Deterministic 1-minute bars; high/low straddle open and close by a small random excursion."""
    rng = np.random.default_rng(seed)
    price = 50_000 * np.exp(np.cumsum(drift_scale * rng.standard_normal(minutes + 1)))
    opens, closes = price[:-1], price[1:]
    up = 1 + 0.0004 * rng.random(minutes)
    down = 1 - 0.0004 * rng.random(minutes)
    columns = {"open": opens, "close": closes, "high": np.maximum(opens, closes) * up,
               "low": np.minimum(opens, closes) * down}
    columns = {name: array("q", [int(round(v * 10 ** 8)) for v in values]) for name, values in columns.items()}
    columns.update({name: array("q", columns["close"]) for name in ("mark_open", "mark_high", "mark_low", "mark_close")})
    columns.update({name: array("q", [0] * minutes) for name in ("volume", "taker_buy_volume", "trades")})
    return BarSeries("BTCUSDT", START, minutes, flags=array("H", [0] * minutes), **columns)


class SupAbsConstantTests(unittest.TestCase):
    def test_series_and_median(self):
        self.assertAlmostEqual(vol.sup_abs_cdf(1.0), 0.3708, places=3)
        self.assertAlmostEqual(vol.FTCAL_SUP_ABS_MEDIAN, 1.148973258, places=8)
        self.assertAlmostEqual(vol.sup_abs_cdf(vol.FTCAL_SUP_ABS_MEDIAN), 0.5, places=10)
        grid = [0.2 * i for i in range(1, 25)]
        values = [vol.sup_abs_cdf(x) for x in grid]
        self.assertEqual(values, sorted(values))
        self.assertEqual(vol.sup_abs_cdf(0.0), 0.0)
        # The series against its own first term for large x is the known tail 1 - (4/pi) * ... : sanity at x = 3
        self.assertGreater(vol.sup_abs_cdf(3.0), 0.99)

    def test_simulation_agrees_after_the_discrete_monitoring_correction(self):
        # 2e5 paths of 250 steps, fixed seed. A discrete path maximum at b corresponds to the continuous
        # barrier b + 0.5826 / sqrt(N) (Broadie-Glasserman-Kou), so P_sim(max <= 1) ~ cdf(1 + 0.5826 / sqrt(N)).
        rng = np.random.default_rng(20261011)
        steps, paths, chunk = 250, 200_000, 20_000
        hits = 0
        for _ in range(paths // chunk):
            walk_ = np.cumsum(rng.standard_normal((chunk, steps)), axis=1) / sqrt(steps)
            hits += int((np.abs(walk_).max(axis=1) <= 1.0).sum())
        expected = vol.sup_abs_cdf(1.0 + 0.5826 / sqrt(steps))
        self.assertAlmostEqual(hits / paths, expected, delta=0.01)


class PathStatisticTests(unittest.TestCase):
    def test_hand_built_five_bar_window(self):
        highs, lows = [101, 103, 102, 104, 103], [99, 98, 97, 100, 99]
        self.assertAlmostEqual(vol.path_extreme_log(100, highs, lows), log(104 / 100), places=15)   # up leg dominates
        self.assertAlmostEqual(vol.path_extreme_log(100, highs, [99, 90, 97, 100, 99]), -log(90 / 100), places=15)

    def _scalar_c(self, bars, horizon, step, sigma, t_ms, skip_minute=None):
        values = []
        first = bars.start_ms + MINUTE
        first += -first % (step * MINUTE)
        for entry in range(first, bars.end_ms, step * MINUTE):
            e = (entry - bars.start_ms) // MINUTE
            x = e + horizon
            if x >= bars.minutes or entry + horizon * MINUTE > t_ms:
                continue
            if skip_minute is not None and e <= skip_minute <= x:  # an unusable minute inside the window or at its exit
                continue
            m = vol.path_extreme_log(bars.open[e], bars.high[e:x], bars.low[e:x])
            values.append((entry, m / (sigma / vol.VAR_SCALE)))
        ratios = sorted(r for _, r in values)
        n = len(ratios)
        median = ratios[n // 2] if n % 2 else (ratios[n // 2 - 1] + ratios[n // 2]) / 2
        return round(min(max(median / vol.FTCAL_SUP_ABS_MEDIAN, 0.5), 2.0) * vol.FTCAL_SCALE), values[0][0]

    def test_calibration_matches_a_scalar_recomputation_and_waits_60_days(self):
        bars = walk(75 * 1440)
        out = vol.path_calibration(bars, 15, 15, lambda entry_ms: SIGMA)
        first_entry = min(out)
        self.assertIsNone(out[first_entry])
        last = max(out)
        expected, first_counted = self._scalar_c(bars, 15, 15, SIGMA, last)
        self.assertEqual(out[last], expected)
        self.assertGreaterEqual(last - first_counted, 60 * DAY)
        # None strictly until the first counted window is 60 days old.
        first_ok = min(t for t, v in out.items() if v is not None)
        self.assertGreaterEqual(first_ok - first_counted, 60 * DAY)
        self.assertLess(max(t for t in out if t < first_ok) - first_counted, 60 * DAY)

    def test_clip_to_half_and_two(self):
        bars = walk(75 * 1440)
        self.assertEqual(vol.path_calibration(bars, 15, 15, lambda entry_ms: 10 ** 12)[max(range(bars.start_ms + MINUTE, bars.end_ms, 15 * MINUTE))],
                         2 * vol.FTCAL_SCALE)
        self.assertEqual(vol.path_calibration(bars, 15, 15, lambda entry_ms: 10 ** 21)[max(range(bars.start_ms + MINUTE, bars.end_ms, 15 * MINUTE))],
                         vol.FTCAL_SCALE // 2)

    def test_point_in_time_ignores_windows_that_complete_after_t(self):
        bars = walk(75 * 1440)
        cut = 70 * 1440                                   # minute index from which the future differs
        future = {name: array("q", getattr(bars, name)) for name in ("open", "high", "low", "close")}
        for name in future:
            for i in range(cut, bars.minutes):
                future[name][i] = future[name][i] * 3 if name != "low" else future[name][i] // 3
        other = replace(bars, **future)
        base = vol.path_calibration(bars, 60, 15, lambda entry_ms: SIGMA)
        changed = vol.path_calibration(other, 60, 15, lambda entry_ms: SIGMA)
        before = [t for t in base if (t - bars.start_ms) // MINUTE < cut]
        self.assertTrue(any(base[t] is not None for t in before), "some calibrated entries precede the change")
        self.assertTrue(all(base[t] == changed[t] for t in before))
        self.assertTrue(any(base[t] != changed[t] for t in base if (t - bars.start_ms) // MINUTE > cut + 60))

    def test_a_missing_minute_drops_every_window_that_contains_it(self):
        bars = walk(75 * 1440)
        highs = array("q", bars.high)
        gap = 5 * 1440 + 7
        highs[gap] = MISSING
        out = vol.path_calibration(replace(bars, high=highs), 15, 15, lambda entry_ms: SIGMA)
        last = max(out)
        expected, _ = self._scalar_c(bars, 15, 15, SIGMA, last, skip_minute=gap)
        self.assertEqual(out[last], expected)


class CandidateGuardTests(unittest.TestCase):
    def test_label_params_refuse_the_candidate_unless_allowed(self):
        with self.assertRaisesRegex(ValueError, "candidate"):
            LabelParams(sigma_model=FTCAL_MODEL)
        allowed = LabelParams(sigma_model=FTCAL_MODEL, allow_candidate=True)
        self.assertEqual(allowed.schema, "labels-v3")
        record = allowed.to_record()
        self.assertIn("ftcal", record["robust"])
        self.assertNotIn("allow_candidate", record)
        self.assertNotEqual(allowed.identity(), LabelParams(sigma_model="ewma-robust-hcal").identity())
        self.assertNotIn(FTCAL_MODEL, SIGMA_MODELS)
        self.assertIn(FTCAL_MODEL, CANDIDATE_SIGMA_MODELS)

    def test_existing_identities_do_not_depend_on_the_new_flag(self):
        for model in SIGMA_MODELS:
            plain = LabelParams(sigma_model=model)
            flagged = LabelParams(sigma_model=model, allow_candidate=True)
            self.assertEqual(plain.identity(), flagged.identity())
            self.assertNotIn("ftcal", plain.to_record().get("robust", {}))

    def test_forward_harness_rejects_the_candidate_and_keeps_hcal(self):
        self.assertEqual(FORWARD_PARAMS.sigma_model, "ewma-robust-hcal")
        candidate = LabelParams(sigma_model=FTCAL_MODEL, allow_candidate=True)
        with self.assertRaisesRegex(ValueError, "forward setups do not support"):
            ForwardSigma(walk(2000), candidate)

    def test_robust_sigma_does_not_mix_calibrations(self):
        with self.assertRaises(ValueError):
            RobustSigma(walk(2000), hcal=True, ftcal=True)
        self.assertNotIn(FTCAL_MODEL, ROBUST_MODELS)
        self.assertEqual(CANDIDATE_MODELS, (FTCAL_MODEL,))

    def test_the_audit_knows_the_candidate_but_does_not_default_to_it(self):
        self.assertIn(FTCAL_MODEL, cal.SIGMA_MODELS)
        self.assertNotIn(FTCAL_MODEL, cal.DEFAULT_SIGMA_MODELS)
        self.assertEqual(cal.MODEL_KEYS[FTCAL_MODEL], ("horizons_robust_ftcal", "barriers_robust_ftcal"))
        self.assertEqual(cal._models((FTCAL_MODEL, "ewma")), ("ewma", FTCAL_MODEL))


if __name__ == "__main__":
    unittest.main()
