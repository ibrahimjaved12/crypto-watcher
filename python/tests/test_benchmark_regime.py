"""Regime/season table and calendar hypotheses (#223): definitions, point in time, hysteresis, bootstrap (synthetic)."""
from __future__ import annotations

from array import array
from datetime import date
from fractions import Fraction
import gzip
import io
import tempfile
import unittest

import numpy as np

from market_analysis import data_lake
from market_analysis.benchmark import regime as rg
from market_analysis.benchmark.bars import BarSeries
from market_analysis.benchmark.funding import FundingSeries
from market_analysis.benchmark.hidden_guard import HiddenStretchLocked
from market_analysis.daily_lake import DAY_MS, DailySeries

NAN = float("nan")
S = 10 ** 8
HOUR = 3_600_000
START = data_lake.month_bounds_ms("2020-01")[0]


def daily(closes, symbol="BTCUSDT", start=START) -> DailySeries:
    closes = [int(value) for value in closes]
    count = len(closes)
    column = array("q", closes)
    return DailySeries(symbol, start, count, open=column, high=column, low=column, close=column,
                       volume=array("q", [1] * count), taker_buy_volume=array("q", [1] * count))


def walk(days, seed):
    return np.round(100 * S * np.exp(np.cumsum(np.random.default_rng(seed).normal(0.0005, 0.03, days))))


def funding(start, end, rates=None) -> FundingSeries:
    times = tuple(range(start, end, 8 * HOUR))
    rates = rates or [Fraction(k % 7 - 2, 100_000) for k in range(len(times))]
    return FundingSeries(times, (8,) * len(times), tuple(rates), start, end)


def assert_nan_equal(actual, expected):
    expected = np.asarray(expected, dtype=float)
    np.testing.assert_array_equal(np.isnan(actual), np.isnan(expected))
    np.testing.assert_allclose(np.nan_to_num(actual), np.nan_to_num(expected))


class DefinitionTests(unittest.TestCase):
    def test_sma_cross_drawdown_mayer_on_a_rising_path(self):
        close = np.arange(1.0, 261.0)
        features = rg.close_features(close)
        self.assertTrue(np.isnan(features["t1"][:199]).all() and (features["t1"][199:] == 1).all())
        self.assertTrue((features["t2"][199:] == 1).all())
        self.assertTrue((features["dd"] == 0).all())
        self.assertAlmostEqual(features["mm"][199], 200 / 100.5)

    def test_drawdown_from_the_running_maximum(self):
        assert_nan_equal(rg.close_features(np.array([100.0, 120, 90, 60, 130]))["dd"], [0, 0, -0.25, -0.5, 0])
        self.assertEqual([rg.dd_state(v) for v in (-0.19, -0.2, -0.5, -0.51, NAN)],
                         ["bull", "neutral", "neutral", "bear", ""])

    def test_thresholds(self):
        self.assertEqual([rg.mm_flag(v) for v in (2.41, 2.4, 0.8, 0.79)],
                         ["overheated", "normal", "normal", "capitulation"])
        self.assertEqual([rg.rv_state(v) for v in (29.9, 30.0, 70.0, 70.1, NAN)],
                         ["calm", "normal", "normal", "stressed", ""])
        self.assertEqual([rg.fr_state(v) for v in (Fraction(1, 10_000), Fraction(101, 1_000_000), Fraction(0),
                                                   Fraction(-1, 10 ** 6), None)],
                         ["neutral", "crowded_long", "neutral", "crowded_short", ""])

    def test_realized_vol_percentile_warm_up(self):
        features = rg.close_features(walk(500, 1) / S)
        self.assertTrue(np.isnan(features["rv30"][:30]).all() and not np.isnan(features["rv30"][30]))
        self.assertTrue(np.isnan(features["rv_pct"][:394]).all() and not np.isnan(features["rv_pct"][394:]).any())
        self.assertTrue(((features["rv_pct"][394:] > 0) & (features["rv_pct"][394:] <= 100)).all())

    def test_halving_bins_at_the_boundary_dates(self):
        cases = {date(2024, 4, 19): (47, "30-48"), date(2024, 4, 20): (0, "0-6"), date(2024, 10, 19): (5, "0-6"),
                 date(2024, 10, 20): (6, "6-18"), date(2025, 10, 19): (17, "6-18"),
                 date(2025, 10, 20): (18, "18-30"), date(2026, 10, 20): (30, "30-48")}
        for day, (months, label) in cases.items():
            self.assertEqual(rg.halving_months(day), months, day)
            self.assertEqual(rg.halving_bin(months), label, day)
        self.assertIsNone(rg.halving_months(date(2012, 11, 27)))
        self.assertEqual((rg.halving_bin(None), rg.halving_bin(48)), ("", "48+"))

    def test_season_rule(self):
        self.assertEqual(rg.season(1, 1, -0.1), "Bull")
        self.assertEqual(rg.season(1, 1, -0.2), "Transition")
        self.assertEqual(rg.season(-1, -1, -0.6), "Bear")
        self.assertEqual(rg.season(1, -1, 0.0), "Transition")
        self.assertEqual(rg.season(-1, 1, -0.6), "Transition")
        self.assertEqual(rg.season(NAN, 1, 0.0), "")

    def test_hysteresis(self):
        labels = ["Bull", "Bull", "Bear", "Bear", "Bull", "Bear", "Bear", "Bear", "", "Bear"]
        self.assertEqual(rg.hysteresis(labels),
                         ["Bull", "Bull", "Bull", "Bull", "Bull", "Bull", "Bull", "Bear", "", "Bear"])
        self.assertEqual(rg.hysteresis(["Bull", "Bear", "Transition", "Transition", "Transition"]),
                         ["Bull", "Bull", "Bull", "Bull", "Transition"])

    def test_funding_mean_window_and_normalization(self):
        day = START + 31 * DAY_MS
        times = (day - 30 * DAY_MS, day - 2 * DAY_MS, day - DAY_MS, day)
        series = FundingSeries(times, (8, 4, 8, 8), (Fraction(3, 10_000), Fraction(1, 20_000), Fraction(1, 10_000),
                                                     Fraction(5, 10_000)), START, START + 40 * DAY_MS)
        means = rg.funding_means(series, [day, START + 29 * DAY_MS])
        self.assertEqual(means, [Fraction(5, 30_000), None])  # the 4h rate counts double; t itself excluded

    def test_per_symbol_descriptors(self):
        er = rg.efficiency_ratio(np.arange(1.0, 50.0))
        self.assertTrue(np.isnan(er[:20]).all())
        np.testing.assert_allclose(er[20:], 1.0)
        close = np.exp(np.cumsum(np.random.default_rng(4).normal(0, 0.02, 3000)))
        vr = rg.variance_ratio(close)
        self.assertTrue(np.isnan(vr[:180]).all() and not np.isnan(vr[180:]).any())
        self.assertAlmostEqual(float(np.mean(vr[180:])), 1.0, delta=0.1)
        self.assertAlmostEqual(float(np.mean(rg.hurst_from_vr(vr[180:]))), 0.5, delta=0.05)
        self.assertEqual(float(rg.hurst_from_vr(np.array([1.0]))[0]), 0.5)

    def test_label_statistics(self):
        stats = rg.label_statistics(["a", "a", "b", "", "b", "b", "a"])
        self.assertEqual((stats["defined_days"], stats["runs"], stats["flips"]), (6, 4, 2))
        self.assertEqual(stats["states"], {"a": 3, "b": 3})
        self.assertEqual(stats["mean_duration_days"], 1.5)
        self.assertEqual(stats["flip_rate_per_year"], 2 / 4 * 365)


class TableTests(unittest.TestCase):
    DAYS = 700

    def inputs(self, btc_close=None, rates=None):
        end = START + self.DAYS * DAY_MS
        btc = daily(walk(self.DAYS, 7) if btc_close is None else btc_close)
        return {"BTCUSDT": btc, "ETHUSDT": daily(walk(self.DAYS, 8), "ETHUSDT")}, funding(START, end, rates), end

    def test_warm_up_empties_and_layout(self):
        series, paid, end = self.inputs()
        rows, columns = rg.build_table(series, paid, START, end)
        self.assertEqual(len(rows), 2 * self.DAYS)
        self.assertEqual([row[1] for row in rows[:2]], ["BTCUSDT", "ETHUSDT"])
        index = {name: i for i, name in enumerate(rg.COLUMNS)}
        btc = rows[::2]
        self.assertEqual(btc[0][0], "2020-01-01")
        self.assertTrue(all(row[index["mkt_t1"]] == "" for row in btc[:200]) and btc[200][index["mkt_t1"]])
        self.assertTrue(all(row[index["mkt_rv_pct"]] == "" for row in btc[:395]) and btc[395][index["mkt_rv_pct"]])
        self.assertTrue(all(row[index["mkt_fr30"]] == "" for row in btc[:30]) and btc[30][index["mkt_fr30"]])
        self.assertTrue(all(row[index["sym_vr5"]] == "" for row in btc[:181]) and btc[181][index["sym_vr5"]])
        self.assertEqual(btc[200][index["halving_bin"]], "0-6")  # 2020-07-19, two months after 2020-05-11
        self.assertEqual(columns["market"]["mkt_season_h"], rg.hysteresis(columns["market"]["mkt_season"]))

    def test_no_look_ahead(self):
        series, paid, end = self.inputs()
        rows, _ = rg.build_table(series, paid, START, end)
        t = 450
        changed = np.asarray(series["BTCUSDT"].close, dtype=np.int64).copy()
        changed[t:] = changed[t:] * 3 // 2
        rates = list(paid.rate)
        cut = START + t * DAY_MS
        rates = [rate * 9 if time >= cut else rate for time, rate in zip(paid.calc_time_ms, rates)]
        other, other_paid, _ = self.inputs(changed, rates)
        moved, _ = rg.build_table(other, other_paid, START, end)
        self.assertEqual(rows[:2 * (t + 1)], moved[:2 * (t + 1)])
        self.assertNotEqual(rows[2 * (t + 1):], moved[2 * (t + 1):])

    def test_deterministic_output_bytes(self):
        series, paid, end = self.inputs()
        rows, columns = rg.build_table(series, paid, START, end)
        first, second = io.BytesIO(), io.BytesIO()
        self.assertEqual(rg.write_table(first, rows), len(rows))
        rg.write_table(second, rg.build_table(series, paid, START, end)[0])
        self.assertEqual(first.getvalue(), second.getvalue())
        text = gzip.decompress(first.getvalue()).decode("ascii").splitlines()
        self.assertEqual(text[0], ",".join(rg.COLUMNS))
        self.assertEqual(len(text), len(rows) + 1)
        diagnostics = rg.diagnostics(columns, START, end)
        self.assertEqual(set(diagnostics), {"all", "development_validation"})
        manifest = rg.build_manifest(rows=len(rows), table_sha256="0" * 64, first_day=rows[0][0],
                                     last_day=rows[-1][0], diagnostics=diagnostics, calendar=[],
                                     code_commit="abc", created_utc="2026-10-09T00:00:00Z")
        self.assertEqual(rg.public_lines(manifest), [f"regime table regime-v1 rows={len(rows)}",
                                                     "calendar hypotheses=0",
                                                     f"report hash {manifest['report_hash']}"])
        self.assertTrue(rg.report_paths(manifest)[0].startswith("reports/regime/regime-v1__"))
        self.assertIn("Calendar hypotheses", rg.markdown(manifest))

    def test_hidden_months_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(HiddenStretchLocked):
                rg.load_inputs(directory, last_month="2026-01")


class CalendarTests(unittest.TestCase):
    def test_bootstrap_determinism_and_cannot_pass_line(self):
        months = [f"2024-{m:02d}" for m in range(1, 13)] + [f"2025-{m:02d}" for m in range(1, 13)]
        values = list(np.random.default_rng(9).normal(0, 0.1, 24))
        group = [month.endswith("-10") for month in months]
        first = rg.month_block_bootstrap(months, values, group, B=300, stream="regime-test")
        self.assertEqual(first, rg.month_block_bootstrap(months, values, group, B=300, stream="regime-test"))
        self.assertEqual((first["n_months"], first["group_months"], first["n_group"]), (24, 2, 2))
        self.assertLess(first["replicates_defined"], 300)  # some resamples hold no October
        low, high = first["interval95"]
        self.assertLessEqual(low, high)
        self.assertIn("C(24,2) = 276", rg.cannot_pass_line(24, 2, first["difference"], first["bootstrap_se"],
                                                           first["t"]))
        self.assertTrue(rg.cannot_pass_line(24, None, 0.01, 0.005, 2.0).startswith(
            "cannot pass t >= 3.3 with this n: 24 months"))
        self.assertIn("descriptive only", rg.cannot_pass_line(24, None, 0.05, 0.01, 5.0))

    def test_monthly_and_daily_hypotheses_use_development_and_validation_only(self):
        start = data_lake.month_bounds_ms("2023-12")[0]
        btc = daily(walk(31 + 731 + 40, 12), start=start)  # through early 2026: ignored after 2025-12
        h1, h2, h4 = rg.h1_october(btc, B=200), rg.h2_halving(btc, B=200), rg.h4_weekend(btc, B=200)
        self.assertEqual((h1["n_months"], h1["n_group"], h1["n_rest"]), (24, 2, 22))
        self.assertIn("C(24,2)", h1["caveat"])
        self.assertEqual(h2["n_group"] + h2["n_rest"], 731)
        self.assertEqual(h2["n_group"], (data_lake.month_bounds_ms("2025-10")[0] + 19 * DAY_MS
                                         - data_lake.month_bounds_ms("2024-10")[0] - 19 * DAY_MS) // DAY_MS)
        self.assertEqual(h4["n_group"], 208)  # Saturdays and Sundays of 2024-2025 (104 + 104)
        self.assertTrue(h4["caveat"].startswith("cannot pass") or "descriptive only" in h4["caveat"])

    def test_late_window_returns_and_costs(self):
        first = rg.CALENDAR_FIRST_MS
        minutes = 3 * 1440
        opens = [int(100 * S * 1.01 ** (minute // 1440)) for minute in range(minutes)]
        column = array("q", opens)
        bars = BarSeries("BTCUSDT", first, minutes, open=column, high=column, low=column, close=column,
                         volume=array("q", [1] * minutes), taker_buy_volume=array("q", [1] * minutes),
                         trades=array("q", [1] * minutes), mark_open=column, mark_high=column, mark_low=column,
                         mark_close=column, flags=array("H", [0] * minutes))
        starts, returns = rg.window_returns(bars)
        self.assertEqual(starts, [first, first + DAY_MS])  # the last day lacks the next 00:00 open
        np.testing.assert_allclose(returns[:, 7], 0.01, rtol=1e-6)
        np.testing.assert_allclose(returns[:, :7], 0.0, atol=1e-12)
        result = rg.h3_late_window(bars, B=50)
        self.assertEqual(result["taker_round_trip"], "0.0012")
        self.assertEqual(result["maker_round_trip"], "0.0004")
        self.assertAlmostEqual(result["mean_net_taker"], result["mean_gross_21_24"] - 0.0012)
        self.assertAlmostEqual(result["mean_net_maker"], result["mean_gross_21_24"] - 0.0004)
        self.assertAlmostEqual(result["difference"], result["mean_gross_21_24"])
        self.assertEqual(result["days"], 2)


if __name__ == "__main__":
    unittest.main()
