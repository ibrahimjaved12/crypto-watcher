"""Level-reaction event study L0 (#185): levels, arm/touch, statuses, returns, terciles, bootstrap (synthetic)."""
from __future__ import annotations

from array import array
from fractions import Fraction
import importlib.util
from pathlib import Path
import re
import sys
import tempfile
import unittest

import numpy as np

from market_analysis import data_lake
from market_analysis.benchmark import level_study as ls
from market_analysis.benchmark import levels as lv
from market_analysis.benchmark.bars import BarSeries
from market_analysis.benchmark.candles import CandleSeries
from market_analysis.benchmark.hidden_guard import HiddenStretchLocked

START = data_lake.month_bounds_ms("2024-01")[0]  # Monday 2024-01-01 00:00 UTC
MINUTE, DAY = 60_000, 86_400_000
S = 10 ** 8
L = 100 * S
S60, S240 = 0.01, 0.02  # support: away at low >= 102, touch at low <= 100.1


def bars_from(highs, lows=None, opens=None, flags=None, start=START):
    lows = lows or highs
    opens = opens or highs
    count = len(highs)
    columns = {"open": array("q", opens), "high": array("q", highs), "low": array("q", lows),
               "close": array("q", opens)}
    for name in ("mark_open", "mark_high", "mark_low", "mark_close"):
        columns[name] = array("q", opens)
    for name in ("volume", "taker_buy_volume", "trades"):
        columns[name] = array("q", [1] * count)
    return BarSeries("BTCUSDT", start, count, flags=array("H", flags or [0] * count), **columns)


def context(bars, s60=S60, s240=S240):
    return ls.make_context(bars, np.full(bars.minutes, s60), np.full(bars.minutes, s240))


def price(value):
    return int(round(value * S))


def path(*segments):
    out = []
    for value, minutes in segments:
        out += [price(value)] * minutes
    return out


SUPPORT = lv.Level("round", "grid", L, 0, lv.FOREVER_MS, 1)


class ArmTouchTests(unittest.TestCase):
    def test_approach_without_arming_gives_no_event(self):
        bars = bars_from(path((101, 60), (100, 60), (101, 60), (100, 60)))
        self.assertEqual(ls.detect_events(context(bars), SUPPORT, "support"), [])

    def test_chop_counts_once_and_rearming_gives_a_second_event(self):
        chop = path((103, 30), (100, 10), (100.5, 10), (100, 10), (100.5, 10), (100, 10))
        bars = bars_from(chop + path((103, 30), (100, 10)))
        events = ls.detect_events(context(bars), SUPPORT, "support")
        self.assertEqual(events, [30, 110])  # one event in the chop, one after re-arming at 103

    def test_arming_older_than_a_day_does_not_count(self):
        bars = bars_from(path((103, 10), (101, 1500), (100, 10)))
        self.assertEqual(ls.detect_events(context(bars), SUPPORT, "support"), [])

    def test_resistance_side_and_validity_window(self):
        bars = bars_from(path((97, 30), (99.95, 10)))
        self.assertEqual(ls.detect_events(context(bars), SUPPORT, "resistance"), [30])
        late = lv.Level("round", "grid", L, START + 20 * MINUTE, lv.FOREVER_MS, 1)
        self.assertEqual(ls.detect_events(context(bars), late, "resistance"), [30])  # arms at minute 20..29
        later = lv.Level("round", "grid", L, START + 30 * MINUTE, lv.FOREVER_MS, 1)
        self.assertEqual(ls.detect_events(context(bars), later, "resistance"), [])  # never armed while valid


class StatusTests(unittest.TestCase):
    def classify(self, highs, lows=None, flags=None, end=None):
        bars = bars_from(highs, lows, flags=flags)
        return ls.classify(context(bars), 0, L, "support", end or bars.minutes)

    def test_bounce_penetration_neither(self):
        self.assertEqual(self.classify(path((100, 5), (101.5, 1), (100, 1500))), ("B", None))
        self.assertEqual(self.classify(path((100, 5), (99.5, 1), (100, 1500))), ("P", 5))
        self.assertEqual(self.classify(path((100, 1500))), ("T", None))

    def test_same_minute_and_t0_double_touch_are_ambiguous(self):
        highs = path((100, 5), (101.5, 1), (100, 1500))
        lows = path((100, 5), (99.5, 1), (100, 1500))
        self.assertEqual(self.classify(highs, lows), ("A", None))
        highs = path((101.5, 1), (100, 1500))
        lows = path((99.5, 1), (100, 1500))
        self.assertEqual(self.classify(highs, lows), ("A", None))

    def test_exclusions(self):
        flags = [0] * 1501
        flags[3] = data_lake.FLAG_NO_AGGTRADES
        self.assertEqual(self.classify(path((100, 5), (101.5, 1), (100, 1495)), flags=flags), ("X", None))
        self.assertEqual(self.classify(path((100, 1500)), end=1000), ("I", None))


class LevelSetTests(unittest.TestCase):
    def test_prev_day_and_week_unusable_before_the_period_closes(self):
        bars = bars_from(path((100, 7 * 1440), (105, 7 * 1440), (100, 60)))
        days = lv.prev_period_levels(bars, "day")
        self.assertEqual(days[0].valid_from_ms, START + DAY)
        self.assertEqual(days[0].valid_to_ms, START + 2 * DAY)
        self.assertTrue(all(level.valid_from_ms % DAY == 0 for level in days))
        weeks = lv.prev_period_levels(bars, "week")
        self.assertEqual([(w.side_hint, w.price, w.valid_from_ms) for w in weeks],
                         [("high", price(100), START + 7 * DAY), ("low", price(100), START + 7 * DAY),
                          ("high", price(105), START + 14 * DAY), ("low", price(105), START + 14 * DAY)])
        flags = [0] * bars.minutes
        flags[100] = data_lake.FLAG_NO_AGGTRADES
        broken = bars_from(path((100, 7 * 1440), (105, 7 * 1440), (100, 60)), flags=flags)
        self.assertNotIn(START + DAY, [level.valid_from_ms for level in lv.prev_period_levels(broken, "day")])

    def test_swing_known_after_n_candles_and_broken_by_invalid(self):
        highs = [10, 11, 12, 13, 14, 20, 14, 13, 12, 11, 10, 9]
        lows = [h - 1 for h in highs]

        def candles(valid):
            return CandleSeries("BTCUSDT", 240, START, list(highs), list(highs), list(lows), list(highs), valid)

        series = candles([True] * 12)
        swings = lv.swing_levels(series)
        high = [level for level in swings if level.side_hint == "high"]
        self.assertEqual(len(high), 1)
        self.assertEqual((high[0].price, high[0].valid_from_ms), (20, series.end_ms[10]))
        self.assertEqual(high[0].valid_to_ms, series.end_ms[10] + 90 * DAY)
        valid = [True] * 12
        valid[8] = False
        self.assertEqual([level for level in lv.swing_levels(candles(valid)) if level.side_hint == "high"], [])

    def test_round_placebo_grids_are_reproducible_and_away_from_the_grid(self):
        for symbol in data_lake.SYMBOLS:
            spacing = lv.ROUND_SPACING[symbol]
            offsets = lv.placebo_round_grids(symbol)
            self.assertEqual(offsets, lv.placebo_round_grids(symbol))
            self.assertEqual(len(offsets), 20)
            self.assertTrue(all(3 * spacing <= 10 * offset < 7 * spacing for offset in offsets), symbol)
        self.assertNotEqual(lv.placebo_round_grids("BTCUSDT", seed=1), lv.placebo_round_grids("BTCUSDT"))
        grid = lv.round_levels("BTCUSDT", price(39_500), price(50_500))
        self.assertEqual([level.price for level in grid], [price(40_000 + 1000 * m) for m in range(11)])
        self.assertEqual([level.tier for level in grid][:2], [10000, 1000])
        self.assertEqual(grid[5].tier, 5000)

    def test_offset_placebo_levels_are_fixed_at_creation(self):
        real = [lv.Level("prev_day", "high", L, START + DAY, START + 2 * DAY, 1)]
        sigma = 2 * 10 ** 18  # 0.02 of price
        groups = lv.placebo_offset_levels(real, [sigma])
        self.assertEqual(len(groups[0]), 20)
        self.assertIn(L + L // 100, [level.price for level in groups[0]])  # d = +0.5
        self.assertIn(L - L * 14 // 500, [level.price for level in groups[0]])  # d = -1.4
        self.assertTrue(all((p.valid_from_ms, p.valid_to_ms) == (START + DAY, START + 2 * DAY) for p in groups[0]))
        self.assertEqual(lv.placebo_offset_levels(real, [None]), [[]])
        self.assertEqual(set(lv.PLACEBO_OFFSETS), {Fraction(s * t, 10) for t in range(5, 15) for s in (1, -1)})


class ReturnTests(unittest.TestCase):
    def test_sign_entry_at_next_open_and_exclusions(self):
        opens = path((100, 1), (500, 1), (100, 1), (102, 10_000))  # minute 1 spike must not be the entry
        bars = bars_from(opens, opens, opens)
        ctx = context(bars)
        bp, units = ls.forward_returns(ctx, 1, 1, bars.minutes)
        self.assertAlmostEqual(bp[0], 200.0)  # 100 -> 102 from the open of minute 2
        self.assertAlmostEqual(units[0], 0.02 / 0.01)
        short_bp, _ = ls.forward_returns(ctx, 1, -1, bars.minutes)
        self.assertAlmostEqual(short_bp[0], -200.0)
        cut_bp, _ = ls.forward_returns(ctx, 1, 1, 2000)
        self.assertEqual([np.isnan(v) for v in cut_bp], [h + 2 >= 2000 for h in ls.RETURN_HORIZONS])
        flags = [0] * bars.minutes
        flags[30] = data_lake.FLAG_NO_AGGTRADES
        dirty = context(bars_from(opens, opens, opens, flags=flags))
        dirty_bp, _ = ls.forward_returns(dirty, 1, 1, bars.minutes)
        self.assertEqual([np.isnan(v) for v in dirty_bp], [True] * len(ls.RETURN_HORIZONS))


class TercileTests(unittest.TestCase):
    def test_terciles_use_development_data_only(self):
        start = data_lake.month_bounds_ms("2025-06")[0] + 29 * DAY  # 2025-06-30, last development day
        bars = bars_from(path((100, 2 * 1440)), start=start)
        s240 = np.concatenate([np.linspace(0.01, 0.03, 1440), np.full(1440, 0.5)])  # validation part is huge
        ctx = ls.make_context(bars, np.full(bars.minutes, S60), s240)
        lo, hi = ls.tercile_thresholds(ctx)
        self.assertTrue(0.01 < lo < hi < 0.03)


class BootstrapTests(unittest.TestCase):
    def test_deterministic_and_brackets_the_difference(self):
        rng = np.random.default_rng(9)
        days = 60
        real_den = rng.integers(5, 10, days).astype(float)
        real_num = np.round(real_den * 0.6)
        placebo_den = rng.integers(20, 40, days).astype(float)
        placebo_num = np.round(placebo_den * 0.2)
        weights = ls.bootstrap_weights(days, B=400)
        np.testing.assert_array_equal(weights, ls.bootstrap_weights(days, B=400))
        self.assertTrue(np.all(weights.sum(axis=1) == days))
        first = ls.bootstrap_difference(weights, real_num, real_den, placebo_num, placebo_den)
        self.assertEqual(first, ls.bootstrap_difference(weights, real_num, real_den, placebo_num, placebo_den))
        lo, hi = first["ci95"]
        self.assertTrue(lo <= first["difference"] <= hi)
        self.assertGreater(lo, 0)


def fake_events(rows):
    """rows: (day, side, placebo, tier, tercile, status letter)."""
    table = {name: np.array([row[i] if name != "status" else ls.STATUSES.index(row[i]) for row in rows],
                            dtype=np.int64) for i, name in enumerate(ls.EVENT_FIELDS)}
    for kind in ("touch_bp", "touch_sigma", "cont_bp", "cont_sigma"):
        table[kind] = np.full((len(rows), len(ls.RETURN_HORIZONS)), 1.5)
    return table


class ReportAndGuardTests(unittest.TestCase):
    def test_public_lines_have_counts_only(self):
        rows = [(1, 0, 0, 10000, 0, "B"), (2, 0, 0, 1000, 1, "P"), (2, 1, 1, 1, 2, "B"), (3, 0, 1, 1, 0, "T")]
        results = {"BTCUSDT": {"tercile_thresholds": [0.01, 0.02], "levels": {"round": {"real": 3, "placebo": 60}},
                               "events": {"round": fake_events(rows)}}}
        report = ls.build_report("development", results, ("round",), code_commit="local",
                                 created_utc="2026-10-09T00:00:00Z", B=50)
        lines = ls.public_lines(report)
        self.assertEqual(lines[1], "BTCUSDT set=round segment=development events=2 placebo_events=2")
        self.assertRegex(lines[-1], r"report hash [0-9a-f]{64}\Z")
        for line in lines[1:-1]:
            self.assertRegex(line, r"[A-Z]+USDT set=[a-z0-9_]+ segment=[a-z]+ events=[0-9]+ placebo_events=[0-9]+\Z")
            self.assertIsNone(re.search(r"share|rate|return|bp|sigma|ci|\.", line))
        self.assertEqual(report["n_cells"], 2 * 2 * 4 + 4)
        self.assertTrue(ls.report_paths(report)[0].startswith("reports/levels/development__"))
        self.assertIn("B/(B+P)", ls.markdown(report))

    def test_hidden_segment_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(HiddenStretchLocked):
                ls.run_symbol(directory, "BTCUSDT", "hidden")
        scripts = Path(__file__).resolve().parents[2] / "scripts" / "research"
        definition = importlib.util.spec_from_file_location("test_level_study_script", scripts / "level_study.py")
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
