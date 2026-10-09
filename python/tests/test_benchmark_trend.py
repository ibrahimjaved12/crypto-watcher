"""Multi-day trend Mode B (#222): states, sizing, band, filters, costs, funding, controls, verdict, ledger (synthetic)."""
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
from market_analysis.benchmark import trend as tr
from market_analysis.benchmark.bars import MISSING
from market_analysis.benchmark.experiment_log import ExperimentLog, TrialStatus
from market_analysis.benchmark.funding import FundingSeries
from market_analysis.benchmark.hidden_guard import HiddenStretchLocked
from market_analysis.benchmark import calibration as cal
from market_analysis.benchmark.label_store import load_geometry_columns
from market_analysis.benchmark.labels import LabelParams
from market_analysis.benchmark.segments import (SEGMENTS, daily_segment_bounds_ms, segment_bounds_ms,
                                                segment_of)
from market_analysis.daily_lake import DAY_MS, DailySeries

NAN = float("nan")
S = 10 ** 8
HOUR = 3_600_000
DEV_FIRST, DEV_END = segment_bounds_ms("development")
WARMUP_START = data_lake.month_bounds_ms("2022-09")[0]  # > 400 days before 2024-01: every indicator is warm


def daily(closes, opens=None, symbol="BTCUSDT", start=WARMUP_START) -> DailySeries:
    closes = [MISSING if value is None else int(value) for value in closes]
    opens = closes if opens is None else [MISSING if value is None else int(value) for value in opens]
    count = len(closes)
    return DailySeries(symbol, start, count, open=array("q", opens), high=array("q", closes),
                       low=array("q", closes), close=array("q", closes), volume=array("q", [1] * count),
                       taker_buy_volume=array("q", [1] * count))


def funding(start, end, rate=Fraction(1, 10_000)) -> FundingSeries:
    times = tuple(range(start, end, 8 * HOUR))
    return FundingSeries(times, (8,) * len(times), (rate,) * len(times), start, end)


def random_walk(days, seed, drift=0.0005, vol=0.03):
    steps = np.random.default_rng(seed).normal(drift, vol, days)
    return np.round(100 * S * np.exp(np.cumsum(steps)))


def symbol_data(symbol="BTCUSDT", seed=1, rate=Fraction(1, 10_000)):
    days = (DEV_END - WARMUP_START) // DAY_MS
    closes = random_walk(days, seed)
    opens = np.concatenate([[closes[0]], closes[:-1]])  # open = previous close
    series = daily(closes, opens, symbol)
    return tr.prepare_symbol(series, funding(WARMUP_START, DEV_END, rate), DEV_FIRST, DEV_END)


def assert_nan_equal(case, actual, expected):
    np.testing.assert_array_equal(np.isnan(actual), np.isnan(np.asarray(expected, dtype=float)))
    np.testing.assert_allclose(np.nan_to_num(actual), np.nan_to_num(np.asarray(expected, dtype=float)))


class ChannelStateTests(unittest.TestCase):
    def test_entry_midline_exit_short_mirror_and_jump_through_both_bounds(self):
        close = np.array([10, 11, 12, 13, 12.6, 12.2, 11, 9, 8, 9.6, 10.5, 20, 5])
        # d3 breakout long; d4 holds above mid 12; d5 below mid 12.5 -> flat; d6 below lower -> short;
        # d9 above mid 9.5 -> flat; d10 breakout long; d12 jumps from long below the lower bound -> short.
        expected = [NAN, NAN, NAN, 1, 1, 0, -1, -1, -1, 0, 1, 1, -1]
        assert_nan_equal(self, tr.channel_states(close, 3), expected)

    def test_channel_excludes_the_decision_close(self):
        upper, lower = tr.prior_extremes(np.array([1.0, 2, 3, 4]), 2)
        assert_nan_equal(self, upper, [NAN, NAN, 2, 3])
        assert_nan_equal(self, lower, [NAN, NAN, 1, 2])

    def test_gap_leaves_the_component_undefined_and_restarts_from_flat(self):
        close = np.array([10, 11, 12, 13, 13.5, NAN, 14, 15, 16, 15.9, 15.8])
        states = tr.channel_states(close, 3)
        self.assertTrue(np.isnan(states[5:9]).all())  # the gap is inside every window up to d = 8
        self.assertEqual(states[4], 1)
        self.assertEqual(states[9], 0)   # restarted flat: 15.9 is inside [14, 16]
        self.assertEqual(states[10], 0)

    def test_ensemble_warm_up_and_position_day_shift(self):
        close = random_walk(400, 3) / S
        signal = tr.ensemble_signal(close)
        self.assertTrue(np.isnan(signal[:360]).all())
        self.assertFalse(np.isnan(signal[360:]).any())
        self.assertTrue(set(np.round(signal[360:] * 9).astype(int)) <= set(range(-9, 10)))
        inputs = tr.signal_inputs(tr.VARIANTS[0], close)
        assert_nan_equal(self, inputs.signal[1:], signal[:-1])
        self.assertTrue(np.isnan(inputs.signal[:361]).all())


class SizingAndBandTests(unittest.TestCase):
    def test_size_clips_and_is_undefined_without_sigma(self):
        weights = tr.size([1.0, 0.5, -1.0, NAN, 1.0], [0.5, 0.25, 0.05, 0.5, 0.0], 0.25)
        assert_nan_equal(self, weights, [0.5, 0.5, -2.0, NAN, NAN])

    def test_band_rule(self):
        path = tr.apply_band([0.5, 0.52, 0.6, -0.1, NAN, 0.3, 0.0])
        np.testing.assert_allclose(path, [0.5, 0.5, 0.6, -0.1, 0.0, 0.3, 0.0])

    def test_band_on_many_paths_matches_single_paths(self):
        targets = np.array([[0.5, -1.0], [0.53, -0.95], [0.7, NAN], [0.69, 1.0]])
        both = tr.apply_band(targets, w0=np.array([0.0, -1.0]))
        np.testing.assert_allclose(both[:, 0], tr.apply_band(targets[:, 0]))
        np.testing.assert_allclose(both[:, 1], tr.apply_band(targets[:, 1], w0=-1.0))
        np.testing.assert_allclose(both[:, 1], [-1.0, -1.0, 0.0, 1.0])

    def test_block_refuses_new_positions_only(self):
        np.testing.assert_allclose(tr.apply_band([0.5, 0.5], np.array([True, False])), [0.0, 0.5])
        np.testing.assert_allclose(tr.apply_band([0.5, 0.8], np.array([False, True])), [0.5, 0.8])


class SignalTests(unittest.TestCase):
    def test_tsmom_sign(self):
        rising, falling = np.arange(1.0, 41.0), np.arange(40.0, 0.0, -1.0)
        assert_nan_equal(self, tr.tsmom_signal(rising, 7)[:9], [NAN] * 7 + [1, 1])
        self.assertTrue((tr.tsmom_signal(falling, 7)[7:] == -1).all())
        self.assertEqual(tr.tsmom_signal(np.array([1.0, 2.0, 1.0]), 2)[2], 0)

    def test_ma_score(self):
        self.assertTrue((tr.ma_score(np.arange(1.0, 260.0))[199:] == 4).all())
        self.assertTrue((tr.ma_score(np.arange(260.0, 1.0, -1.0))[199:] == 0).all())
        self.assertTrue(np.isnan(tr.ma_score(np.arange(1.0, 260.0))[:199]).all())

    def test_long_only_and_filters(self):
        close = random_walk(900, 11, drift=0.0) / S
        by_name = {variant.name: tr.signal_inputs(variant, close) for variant in tr.VARIANTS}
        base, long_only = by_name["ens_ls_25"].signal, by_name["ens_lo_25"].signal
        assert_nan_equal(self, long_only, np.where(np.isnan(base), NAN, np.maximum(base, 0)))
        self.assertTrue((base[~np.isnan(base)] < 0).any())
        score = tr.shift1(tr.ma_score(close))
        filtered = by_name["ens_ls_25_mfilter"].signal
        defined = ~np.isnan(score) & ~np.isnan(base)
        zero_long = defined & (base > 0) & (score < 3)
        zero_short = defined & (base < 0) & (score > 1)
        self.assertTrue(zero_long.any() and zero_short.any())
        self.assertTrue((filtered[zero_long | zero_short] == 0).all())
        keep = defined & ~zero_long & ~zero_short
        np.testing.assert_allclose(filtered[keep], base[keep])
        self.assertTrue(np.isnan(filtered[np.isnan(score)]).all())
        rv = by_name["ens_ls_25_rvfilter"]
        percentile = tr.shift1(tr.realized_vol_percentile(close))
        self.assertTrue(np.isnan(rv.signal[np.isnan(percentile)]).all())
        np.testing.assert_array_equal(rv.block, percentile >= 70)
        self.assertTrue(rv.block.any() and (~rv.block[~np.isnan(percentile)]).any())

    def test_percentile_rank_includes_the_day(self):
        assert_nan_equal(self, tr.percentile_rank(np.array([3.0, 1.0, 2.0, 4.0]), 3), [NAN, NAN, 200 / 3, 100])

    def test_no_look_ahead(self):
        close = random_walk(700, 5) / S
        for variant in tr.VARIANTS:
            inputs = tr.signal_inputs(variant, close)
            weights = tr.apply_band(tr.size(inputs.signal, inputs.sigma, variant.target), inputs.block)
            for t in (450, 699):
                changed = close.copy()
                changed[t:] *= 1.7
                other = tr.signal_inputs(variant, changed)
                moved = tr.apply_band(tr.size(other.signal, other.sigma, variant.target), other.block)
                np.testing.assert_array_equal(weights[:t + 1], moved[:t + 1], err_msg=variant.name)


class CostAndFundingTests(unittest.TestCase):
    def setUp(self):
        self.data = tr.SymbolData("BTCUSDT", np.ones(3), np.arange(3), np.array([0.01, 0.0, 0.0]),
                                  np.array([0.0003, 0.0003, 0.0]))

    def test_funding_sign_long_pays_positive_rates(self):
        held = tr.Leg(np.array([1.0, -1.0, 0.0]), np.array([1.0, -1.0, 0.0]), np.ones(3, dtype=bool))
        np.testing.assert_allclose(tr.net(held, self.data), [0.01 - 0.0003, 0.0003, 0.0])

    def test_cost_at_1x_and_2x(self):
        opened = tr.Leg(np.array([0.0, 0.0, 1.0]), np.array([0.0, 0.0, 0.0]), np.ones(3, dtype=bool))
        np.testing.assert_allclose(tr.net(opened, self.data)[2], -0.0011)
        np.testing.assert_allclose(tr.net(opened, self.data, 2)[2], -0.0022)

    def test_long_and_short_legs(self):
        flip = tr.Leg(np.array([1.0, -1.0, 0.0]), np.array([0.0, 1.0, -1.0]), np.ones(3, dtype=bool))
        long, short = tr.net(flip, self.data, part="long"), tr.net(flip, self.data, part="short")
        np.testing.assert_allclose(long, [0.01 - 0.0011 - 0.0003, -0.0011, 0.0])
        np.testing.assert_allclose(short, [0.0, -0.0011 + 0.0003, -0.0011])

    def test_day_funding_window(self):
        start = DEV_FIRST
        times = (start, start + 8 * HOUR, start + 16 * HOUR, start + DAY_MS, start + DAY_MS + 8 * HOUR)
        rates = tuple(Fraction(n, 10_000) for n in (1, 2, 3, 4, 5))
        series = FundingSeries(times, (8,) * 5, rates, start, start + 3 * DAY_MS)
        self.assertAlmostEqual(tr.day_funding(series, start), 9 / 10_000)  # open excluded, next open included
        self.assertTrue(np.isnan(tr.day_funding(series, start - DAY_MS)))

    def test_portfolio_mean_over_active_symbols(self):
        values = [np.array([0.01, 0.02, 0.0]), np.array([0.03, 0.0, 0.0])]
        actives = [np.array([True, True, False]), np.array([True, False, False])]
        np.testing.assert_allclose(tr.portfolio(values, actives), [0.02, 0.02, 0.0])
        self.assertEqual(tr.to_ppm([0.0000016, -0.0000024, 0.01]), [2, -2, 10_000])


class ControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = symbol_data()

    def test_buy_and_hold_control(self):
        variant = tr.VARIANTS[0]
        leg, inputs = tr.real_path(variant, self.data, constant_signal=True)
        expected = tr.apply_band(tr.size(np.where(np.isnan(inputs.sigma), NAN, 1.0), inputs.sigma, variant.target))
        np.testing.assert_allclose(leg.weight, expected[self.data.index])
        self.assertTrue((leg.weight > 0).all() and leg.active.all())

    def test_shift_offsets_and_zero_shift_reproduces_the_real_stream(self):
        T = len(self.data.index)
        offsets = tr.shift_offsets(T, 50)
        np.testing.assert_array_equal(offsets, tr.shift_offsets(T, 50))
        self.assertTrue(((offsets >= 30) & (offsets <= T - 30)).all())
        self.assertEqual(len(tr.shift_offsets(59, 50)), 0)
        symbols = {"BTCUSDT": self.data}
        for variant in (tr.VARIANTS[0], tr.VARIANTS[-1]):
            leg, inputs = tr.real_path(variant, self.data)
            real = sum(tr.to_ppm(tr.portfolio([tr.net(leg, self.data)], [leg.active])))
            sums = tr.placebo_sums(variant, symbols, {"BTCUSDT": leg}, {"BTCUSDT": inputs}, np.array([T, 2 * T]))
            self.assertEqual(sums.tolist(), [real, real])

    def test_newey_west_alpha(self):
        x = np.random.default_rng(2).normal(0, 0.02, 400)
        y = 0.001 + 0.5 * x + np.random.default_rng(3).normal(0, 0.001, 400)
        result = tr.newey_west_alpha(y, x)
        self.assertAlmostEqual(result["beta"], 0.5, delta=0.02)
        self.assertGreater(result["t_alpha"], 5)
        self.assertIsNone(tr.newey_west_alpha(y, np.ones(400))["t_alpha"])

    def test_without_best_month(self):
        result = tr.without_best_month([5, 1, 9, -1], ["2024-01", "2024-01", "2024-02", "2024-03"])
        self.assertEqual(result["best_month"], "2024-02")
        self.assertEqual(result["mean_daily"], "0.000001666667")


class VerdictTests(unittest.TestCase):
    GOOD = dict(stepm_rejected=True, ci_lower="0.0001", t_statistic="4.2", required_t=3.5, mean_1x="0.001",
                mean_2x="0.0008", mean_ex_best="0.0005", p_placebo=0.01, alpha_t=2.5)

    def test_verdict_rule(self):
        self.assertEqual(tr.verdict(**self.GOOD), "PASS")
        self.assertEqual(tr.verdict(**{**self.GOOD, "mean_2x": "-0.0001"}), "FRAGILE")
        self.assertEqual(tr.verdict(**{**self.GOOD, "mean_ex_best": "0"}), "FRAGILE")
        for key, value in (("stepm_rejected", False), ("ci_lower", "0"), ("t_statistic", "3.4"),
                           ("p_placebo", 0.06), ("alpha_t", 1.9), ("alpha_t", None), ("mean_1x", "-0.001")):
            self.assertEqual(tr.verdict(**{**self.GOOD, key: value, "mean_2x": "-1"}), "FAIL", key)


class EvaluationTests(unittest.TestCase):
    def test_ledger_report_and_public_lines(self):
        symbols = {"BTCUSDT": symbol_data("BTCUSDT", 1)}
        with tempfile.TemporaryDirectory() as directory:
            log = ExperimentLog(Path(directory) / "trend-v1.jsonl")
            kwargs = dict(code_commit="abc", data_snapshot_id="snap", now_utc="2026-10-09T00:00:00Z", B=40,
                          n_shifts=20)
            report = tr.evaluate_trend(symbols, "development", log, **kwargs)
            records = [record for _, record in log.read()]
            self.assertEqual(len(records), tr.K)
            self.assertEqual([record.strategy_id for record in records], [v.name for v in tr.VARIANTS])
            for record in records:
                self.assertEqual((record.question_id, record.family_id, record.strategy_version, record.split_id),
                                 ("trend-v1", "trend", "trend-v1", "development"))
                self.assertTrue(record.counts_toward_n)
                self.assertEqual(record.count_reason, "variant evaluated")
                self.assertIn(record.result_summary["verdict"], ("PASS", "FRAGILE", "FAIL"))
            self.assertEqual(log.trial_count(question_id="trend-v1"), tr.K)
            again = tr.evaluate_trend(symbols, "development", log, **kwargs)
            self.assertEqual(again["report_hash"], report["report_hash"])
            self.assertTrue(all(record.status is TrialStatus.REPLAY for _, record in log.read()[tr.K:]))
            self.assertEqual(log.trial_count(question_id="trend-v1"), tr.K)
        entry = report["variants"][0]
        self.assertEqual(entry["T_days"], (DEV_END - DEV_FIRST) // DAY_MS)
        self.assertIsNotNone(entry["power"]["mu_min_daily"])
        self.assertEqual(report["n_trials"], tr.K)
        self.assertTrue(tr.report_paths(report)[0].startswith("reports/trend/development__"))
        self.assertIn("mu_min", tr.markdown(report))
        lines = tr.public_lines(report)
        self.assertEqual(len(lines), tr.K + 2)
        for line in lines[1:-1]:
            self.assertRegex(line, r"^[a-z0-9_]+ segment=development T=\d+ symbols=\d+$")
        text = "\n".join(lines)
        for word in ("PASS", "FAIL", "FRAGILE", "mean", "sharpe", "alpha", "placebo"):
            self.assertNotIn(word, text)
        self.assertTrue(re.fullmatch(r"report hash [0-9a-f]{64}", lines[-1]))

    def test_hidden_segment_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(HiddenStretchLocked):
                tr.load_inputs(directory, "hidden")
            with self.assertRaises(HiddenStretchLocked):
                tr.evaluate_trend({}, "hidden", ExperimentLog(Path(directory) / "x.jsonl"), code_commit="a",
                                  data_snapshot_id="s", now_utc="2026-10-09T00:00:00Z")
        scripts = Path(__file__).resolve().parents[2] / "scripts" / "research"
        definition = importlib.util.spec_from_file_location("test_trend_run_script", scripts / "trend_run.py")
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


EXT_FIRST, EXT_END = daily_segment_bounds_ms("development-ext")
EXT_START = data_lake.month_bounds_ms("2020-01")[0]
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "research"


def load_script(name: str):
    definition = importlib.util.spec_from_file_location(f"test_ext_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(definition)
    previous = sys.path[:]
    try:
        sys.path.insert(0, str(SCRIPTS))
        definition.loader.exec_module(module)
    finally:
        sys.path[:] = previous
    return module


def ext_symbol(symbol="BTCUSDT", seed=1, start=EXT_START, missing=()):
    days = (EXT_END - start) // DAY_MS
    closes = list(random_walk(days, seed))
    opens = [closes[0]] + closes[:-1]
    for day in missing:
        closes[day] = opens[day] = None
        if day + 1 < days:
            opens[day + 1] = None  # the open after a gap is unknown too in this fixture
    series = daily(closes, opens, symbol, start)
    return tr.prepare_symbol(series, funding(start, EXT_END), EXT_FIRST, EXT_END)


class ExtendedWindowTests(unittest.TestCase):
    def test_bounds_and_no_overlap_with_validation_or_hidden(self):
        self.assertEqual((EXT_FIRST, EXT_END), (data_lake.month_bounds_ms("2021-01")[0],
                                                data_lake.month_bounds_ms("2025-07")[0]))
        self.assertEqual((EXT_END - EXT_FIRST) // DAY_MS, 1642)
        for name in ("validation", "hidden"):
            first, end = segment_bounds_ms(name)
            self.assertTrue(EXT_END <= first or end <= EXT_FIRST, name)
        self.assertNotIn("development-ext", SEGMENTS)
        self.assertEqual(segment_of(EXT_FIRST), None)  # intraday segment lookup never sees the daily window
        months = tr.check_daily_segment("development-ext")
        self.assertEqual((months[0], months[-1], len(months)), ("2021-01", "2025-06", 54))

    def test_intraday_paths_reject_development_ext(self):
        with self.assertRaises(ValueError):
            cal.check_segment("development-ext")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(KeyError):
                load_geometry_columns(directory, "BTCUSDT", [], [], LabelParams(), segment="development-ext")
            for name in ("calibration_audit", "level_study", "screen"):
                module = load_script(name)
                workdir = Path(directory) / name
                with self.assertRaises(module.PublicError, msg=name):
                    module.main(["--workdir", str(workdir), "--segment", "development-ext"])
                self.assertFalse(workdir.exists())
        runner = load_script("experiment_run")
        with self.assertRaises(SystemExit):
            runner.parse_args(["run", "--question-id", "q", "--segment", "development-ext"])

    def test_late_listing_joins_when_all_components_are_defined(self):
        listing = data_lake.month_bounds_ms("2020-09")[0] + 13 * DAY_MS  # 2020-09-14, like SOLUSDT
        data = ext_symbol("SOLUSDT", 4, start=listing)
        leg, inputs = tr.real_path(tr.VARIANTS[0], data)  # ens_ls_25: the 360-day channel decides
        first_defined = int(np.argmax(leg.active))
        self.assertFalse(leg.active[:first_defined].any())
        self.assertEqual(EXT_FIRST + first_defined * DAY_MS, listing + 361 * DAY_MS)
        self.assertTrue((leg.weight[:first_defined] == 0).all())

    def test_missing_days_are_missing_and_flat_not_filled(self):
        gap = (EXT_FIRST - EXT_START) // DAY_MS + 200
        data = ext_symbol(missing=(gap,))
        self.assertTrue(np.isnan(data.close[gap]))
        segment_day = gap - (EXT_FIRST - EXT_START) // DAY_MS
        self.assertFalse(data.evaluable[segment_day])
        leg, _ = tr.real_path(tr.VARIANTS[4], data)  # tsmom_7: EWMA sigma restarts after the gap
        # The gap day's own weight was set from closes before it; from the next day on the position is flat
        # until the EWMA sigma has 120 returns again (nothing is filled across the gap).
        self.assertTrue((leg.weight[segment_day + 1:segment_day + 1 + tr.EWMA_MIN_RETURNS] == 0).all())
        self.assertFalse(leg.active[segment_day])
        self.assertNotEqual(leg.weight[segment_day - 1], 0)

    def test_funding_with_mixed_intervals_sums_every_payment(self):
        start = EXT_FIRST
        hours = (2, 4, 8, 16, 24, 26)
        series = FundingSeries(tuple(start + h * HOUR for h in hours), (2, 2, 4, 8, 8, 2),
                               tuple(Fraction(n, 100_000) for n in (1, 2, 3, 4, 5, 6)), start, start + 2 * DAY_MS)
        self.assertAlmostEqual(tr.day_funding(series, start), 15 / 100_000)  # 02, 04, 08, 16 and next 00:00

    def test_yearly_rows_sum_to_the_total_stream(self):
        T = (EXT_END - EXT_FIRST) // DAY_MS
        months = tr._month_labels(EXT_FIRST, T)
        daily_values = [int(v) for v in np.random.default_rng(3).integers(-5000, 5000, T)]
        actives = [np.ones(T, dtype=bool), np.arange(T) > 400]
        rows = tr.descriptive_periods(daily_values, months, actives)
        years = rows["descriptive_by_year"]
        self.assertEqual([row["period"] for row in years], ["2021", "2022", "2023", "2024", "2025H1"])
        self.assertEqual(sum(row["T_days"] for row in years), T)
        total = sum(Fraction(row["sum"]) for row in years)
        self.assertEqual(total, Fraction(sum(daily_values), tr.PPM))
        self.assertEqual((years[0]["symbols_defined"], years[1]["symbols_defined"]), (1, 2))
        self.assertEqual(rows["descriptive_pre_2024"]["period"], "pre-2024 (descriptive)")
        self.assertEqual(Fraction(rows["descriptive_pre_2024"]["sum"]), sum(Fraction(r["sum"]) for r in years[:3]))
        dev = tr.descriptive_periods(daily_values[:10], ["2024-01"] * 10, [np.ones(10, dtype=bool)])
        self.assertIsNone(dev["descriptive_pre_2024"])

    def test_extended_run_keeps_k_and_required_t(self):
        symbols = {"BTCUSDT": ext_symbol()}
        with tempfile.TemporaryDirectory() as directory:
            log = ExperimentLog(Path(directory) / "trend-v1.jsonl")
            kwargs = dict(code_commit="abc", now_utc="2026-10-09T00:00:00Z", B=40, n_shifts=20)
            report = tr.evaluate_trend(symbols, "development-ext", log, data_snapshot_id="snap-a", **kwargs)
            again = tr.evaluate_trend(symbols, "development-ext", log, data_snapshot_id="snap-b", **kwargs)
            records = [record for _, record in log.read()]
        self.assertEqual(len(records), 2 * tr.K)
        self.assertTrue(all(r.split_id == "development-ext" and r.counts_toward_n for r in records))
        self.assertEqual({r.result_summary["window"] for r in records}, {"2021-01..2025-06"})
        self.assertEqual((report["n_trials"], again["n_trials"]), (tr.K, tr.K))
        self.assertEqual(report["required_t"], again["required_t"])
        entry = report["variants"][0]
        self.assertEqual(entry["T_days"], 1642)
        self.assertEqual([row["period"] for row in entry["descriptive_by_year"]],
                         ["2021", "2022", "2023", "2024", "2025H1"])
        self.assertEqual(sum(Fraction(row["sum"]) for row in entry["descriptive_by_year"]),
                         Fraction(entry["net_1x"]["sum"]))
        self.assertIn("pre-2024 (descriptive)", tr.markdown(report))
        for line in tr.public_lines(report)[1:-1]:
            self.assertRegex(line, r"^[a-z0-9_]+ segment=development-ext T=1642 symbols=1$")


if __name__ == "__main__":
    unittest.main()
