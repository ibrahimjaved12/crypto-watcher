"""positioning-v1 (#188): fade the account long/short ratio at 240 m (synthetic metrics, < 10 s)."""
from __future__ import annotations

from fractions import Fraction
import random
from tempfile import TemporaryDirectory
import unittest

import numpy as np

from market_analysis import data_lake
from market_analysis import metrics_lake as mx
from market_analysis.benchmark import experiment_run as er
from market_analysis.benchmark import positioning as pos
from market_analysis.benchmark.hidden_guard import HiddenStretchLocked
from market_analysis.benchmark.order_flow import robust_z
from market_analysis.benchmark.segments import segment_months

MINUTE = 60_000
HOUR = 60 * MINUTE
DAY = 24 * HOUR
MONTH_START = data_lake.month_bounds_ms("2024-01")[0]
NOW = "2026-10-10T12:00:00Z"


def metrics(days: int, value=None, seed: int = 3) -> mx.MetricsSeries:
    """A loader-shaped grid: index i is the row usable at start + i * 5 min (stamped 5 min earlier)."""
    start = MONTH_START + mx.POINT_IN_TIME_LAG_PERIODS * mx.PERIOD_MS
    periods = days * mx.ROWS_PER_DAY
    rng = random.Random(seed)
    columns = {name: np.full(periods, np.nan) for name in mx.VALUE_COLUMNS}
    columns[pos.SERIES] = np.array([value if value is not None else round(1.5 + rng.gauss(0, 0.1), 4)
                                    for _ in range(periods)])
    return mx.MetricsSeries("BTCUSDT", start, periods, columns)


def index_of_stamp(series: mx.MetricsSeries, create_time_ms: int) -> int:
    return (mx.usable_from_ms(create_time_ms) - series.start_ms) // mx.PERIOD_MS


class ValueTests(unittest.TestCase):
    def test_usable_from_boundary(self):
        series = metrics(2, value=1.0)
        stamp = MONTH_START + 10 * HOUR  # a row stamped exactly on the hour
        series.columns[pos.SERIES][index_of_stamp(series, stamp)] = 2.5
        self.assertEqual(mx.usable_from_ms(stamp), stamp + 5 * MINUTE)
        self.assertEqual(pos.value_at(series, stamp + 5 * MINUTE), 2.5)  # usable from create_time + 5 min
        self.assertEqual(pos.value_at(series, stamp + 5 * MINUTE - 1), 1.0)  # one ms earlier: the previous row
        self.assertNotEqual(pos.value_at(series, stamp), 2.5)  # never at its own stamp

    def test_gap_drops_the_decision_never_filled(self):
        series = metrics(40)
        decision = MONTH_START + 35 * DAY
        series.columns[pos.SERIES][index_of_stamp(series, decision - 5 * MINUTE)] = np.nan
        self.assertIsNone(pos.value_at(series, decision))
        self.assertIsNotNone(pos.value_at(series, decision - 5 * MINUTE))  # the older row exists but is not used
        zs = dict(pos.z_series(series, decision_step_min=60, history_days=30, min_history=500))
        self.assertIsNone(zs[decision])
        self.assertIsNotNone(zs[decision + HOUR])
        signals = pos.fade_signals([(decision - HOUR, 0.0), (decision, None), (decision + HOUR, 3.0)],
                                   theta=Fraction(2), rearm_below=Fraction(1), cooldown_ms=240 * MINUTE)
        self.assertEqual(signals, [(decision + HOUR, -1)])


class ZTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.series = metrics(40)
        cls.zs = pos.z_series(cls.series, decision_step_min=60, history_days=30, min_history=500)

    def test_hourly_grid_and_minimum_history(self):
        times = [t for t, _ in self.zs]
        self.assertTrue(all(t % HOUR == 0 for t in times))
        self.assertTrue(all(b - a == HOUR for a, b in zip(times, times[1:])))
        defined = [t for t, z in self.zs if z is not None]
        self.assertEqual(defined[0], times[500])  # 500 earlier hourly values needed

    def test_same_phase_z_uses_only_the_previous_30_days(self):
        decision = MONTH_START + 36 * DAY
        history = sorted(pos.value_at(self.series, t) for t in range(decision - 30 * DAY, decision, HOUR))
        self.assertEqual(len(history), 720)
        expected = robust_z(history, pos.value_at(self.series, decision), 500)
        self.assertEqual(dict(self.zs)[decision], expected)
        # A value older than 30 days does not move the z; the current value never enters its own history.
        changed = metrics(40)
        changed.columns[pos.SERIES][index_of_stamp(changed, decision - 31 * DAY - 5 * MINUTE)] = 99.0
        zs = dict(pos.z_series(changed, decision_step_min=60, history_days=30, min_history=500))
        self.assertEqual(zs[decision], expected)


class RuleTests(unittest.TestCase):
    RULE = dict(theta=Fraction(5, 2), rearm_below=Fraction(1), cooldown_ms=240 * MINUTE)

    def test_fade_direction(self):
        self.assertEqual(pos.fade_signals([(0, 2.6)], **self.RULE), [(0, -1)])  # crowded longs: SHORT
        self.assertEqual(pos.fade_signals([(0, -2.6)], **self.RULE), [(0, 1)])  # crowded shorts: LONG
        self.assertEqual(pos.fade_signals([(0, 2.4), (HOUR, -2.4)], **self.RULE), [])

    def test_rearm_and_cooldown(self):
        zs = [(0, 3.0), (1 * HOUR, 3.0), (2 * HOUR, 0.5), (3 * HOUR, -3.0), (4 * HOUR, -3.0), (5 * HOUR, 0.2),
              (6 * HOUR, 3.0), (8 * HOUR, 3.0)]
        # 0 h signal; 1 h disarmed; 2 h re-armed; 3 h inside the 240 m cooldown (suppressed, no state change);
        # 4 h = 240 m after the signal: allowed while still armed; 5 h re-armed; 6 h inside the cooldown
        # of the 4 h signal (suppressed); 8 h fires.
        self.assertEqual(pos.fade_signals(zs, **self.RULE), [(0, -1), (4 * HOUR, 1), (8 * HOUR, -1)])

    def test_symbol_signals_are_on_the_label_grid(self):
        series = metrics(36)
        rows = pos.symbol_signals("pos_gls_fade_c20", series, 240, first_ms=MONTH_START,
                                  end_ms=MONTH_START + 36 * DAY, label_step_min=15)
        self.assertTrue(rows)
        self.assertTrue(all(symbol == "BTCUSDT" and t % HOUR == 0 and side in (1, -1) and h == 240
                            for symbol, t, side, h in rows))
        with self.assertRaises(ValueError):
            pos.symbol_signals("pos_gls_fade_c20", series, 60, first_ms=0, end_ms=1, label_step_min=15)


class FamilyTests(unittest.TestCase):
    def question(self, **changes):
        values = dict(seed=7, label_revision=1, data_revision=1, created_utc=NOW, family="positioning-v1")
        values.update(changes)
        return er.make_question("pos-gls-fade-v1-240m", "Fade crowded account positioning", 240,
                                list(pos.STRATEGIES), **values)

    def test_question_is_v2_hcal_with_nine_variants(self):
        question = self.question()
        self.assertEqual((question["schema"], question["sigma_model"]), ("question-v2", "ewma-robust-hcal"))
        self.assertEqual((question["k_values"], question["rr_indices"]), (["2"], [1, 2, 3]))
        self.assertEqual(len(er.build_geometries(question)), 6 * 2 * 1 * 3)
        plan = er.build_plan(question, list(pos.STRATEGIES), NOW, "snapshot-1")
        self.assertEqual(plan.variants, 9)
        self.assertEqual(plan.strategies[0]["config"]["direction"], "fade")
        with self.assertRaises(er.QuestionError):  # the family fixes its label model
            self.question(sigma_model="ewma")

    def test_hidden_is_refused_before_any_file_is_opened(self):
        with TemporaryDirectory() as directory:
            with self.assertRaises(HiddenStretchLocked):
                pos.load_metrics(directory, "BTCUSDT", segment_months("hidden")[-1], 240, None, None)
            with self.assertRaises(HiddenStretchLocked):
                er.signal_counts(self.question(), "hidden", directory)


if __name__ == "__main__":
    unittest.main()
