"""candle-table groups deriv-v1 and flush-v1: hand-computed funding, metrics timing, OI change, flush flags."""
from array import array
from fractions import Fraction
import io
import random
from types import SimpleNamespace
import unittest

import numpy as np

from market_analysis import data_lake
from market_analysis import metrics_lake as mx
from market_analysis.benchmark import screen
from market_analysis.benchmark.bars import MISSING, BarSeries
from market_analysis.benchmark.candles import CandleSeries
from market_analysis.benchmark.funding import FundingSeries
from market_analysis.benchmark.premium import BasisSeries
from market_analysis.patterns import candle_derivatives as dv
from market_analysis.patterns import candle_table as ct

S = 10 ** 8
MIN = 60_000
HOUR = 60 * MIN
DAY = 24 * HOUR
START = data_lake.month_bounds_ms("2024-01")[0]
COLUMNS = ct.REGISTRY.columns()


def flat(count, minutes=15, close=None):
    c = close or [105 * S] * count
    return CandleSeries("BTCUSDT", minutes, START, open=[105 * S] * count, high=[110 * S] * count,
                        low=[100 * S] * count, close=list(c), valid=[True] * count, volume=[10 * S] * count,
                        taker_buy_volume=[5 * S] * count)


def metrics_series(start, periods, fill=100.0):
    columns = {name: np.full(periods, fill) for name in mx.VALUE_COLUMNS}
    return mx.MetricsSeries("BTCUSDT", start, periods, columns)


def rows(series, derivs):
    ctx = ct.TableContext(series.symbol, series.minutes, series, [0.01] * len(series), "test",
                          ct.table_segments(series.minutes), None, derivs=derivs)
    return ctx, [dict(zip(COLUMNS, values)) for values in ct.build_rows(ctx)]


class FundingTests(unittest.TestCase):
    T0 = START - 120 * DAY

    def series(self, extra=()):
        times = [self.T0 + 8 * HOUR * k for k in range(300)]
        rates = [Fraction(1 + k % 3, 10_000) for k in range(300)]
        for time, rate in extra:
            times.append(time)
            rates.append(rate)
        return FundingSeries(tuple(times), (8,) * len(times), tuple(rates), self.T0, self.T0 + 101 * DAY)

    def test_percentile_cum_and_clamp_by_hand(self):
        decision = self.T0 + 8 * HOUR * 298 + HOUR  # last settled: k = 298, rate 2e-4
        out = dv._funding(self.series(), decision)
        self.assertEqual(out["fund_last"], 0.0002)
        self.assertIs(out["fund_clamped"], False)
        # window [decision - 90 d, decision]: k = 29..298 (270 rates), 90 of them (k % 3 == 2) are 3e-4 > 2e-4
        self.assertAlmostEqual(out["fund_pct90"], 180 / 270)
        self.assertAlmostEqual(out["fund_cum_24h"], 0.0006)  # k = 296, 297, 298: 3 + 1 + 2 (1e-4)
        clamped = dv._funding(self.series(), self.T0 + 8 * HOUR * 297 + HOUR)
        self.assertEqual((clamped["fund_last"], clamped["fund_clamped"]), (0.0001, True))

    def test_settlement_time_is_floored_to_the_minute(self):
        decision = self.T0 + 8 * HOUR * 299 + 2 * HOUR
        late = self.series(extra=[(decision + 30_000, Fraction(5, 10_000))])  # floors to the decision minute
        self.assertEqual(dv._funding(late, decision)["fund_last"], 0.0005)
        later = self.series(extra=[(decision + MIN, Fraction(5, 10_000))])
        self.assertEqual(dv._funding(later, decision)["fund_last"], 0.0003)  # k = 299 (299 % 3 == 2)

    def test_uncovered_window_is_empty(self):
        out = dv._funding(self.series(), self.T0 + 30 * DAY)
        self.assertIsNone(out["fund_pct90"])  # the 90-day window starts before the series
        self.assertIsNotNone(out["fund_last"])
        self.assertEqual(dv._funding(None, START), dict.fromkeys(out))


class MetricsTimingTests(unittest.TestCase):
    def test_five_minute_usable_rule(self):
        decision = START + 10 * HOUR
        self.assertEqual(mx.usable_from_ms(decision - 5 * MIN), decision)  # stamped decision - 5 min: usable
        self.assertGreater(mx.usable_from_ms(decision - 4 * MIN), decision)  # stamped decision - 4 min: not
        on_grid = metrics_series(decision, 10, 7.0)  # period 0 = the row stamped decision - 5 min
        self.assertEqual(dv.metrics_value(on_grid, dv.OI_COLUMN, decision), 7.0)
        off_grid = metrics_series(decision + MIN, 10, 7.0)  # period 0 = the row stamped decision - 4 min
        self.assertIsNone(dv.metrics_value(off_grid, dv.OI_COLUMN, decision))

    def test_doi_with_missing_end_and_no_forward_fill(self):
        series = flat(20)
        metrics = metrics_series(START, 200)
        end_index = (series.end_ms[16] - START) // (5 * MIN)
        metrics.columns[dv.OI_COLUMN][end_index] = np.nan
        metrics.columns["count_long_short_ratio"][end_index] = np.nan
        metrics.columns[dv.OI_COLUMN][(series.end_ms[15] - START) // (5 * MIN)] = 110.0
        _, table = rows(series, dv.DerivInputs(metrics=metrics))
        self.assertIsNone(table[16]["oi_value_end"])  # the period before holds 100: never forward-filled
        self.assertEqual(table[16]["oi_value_start"], 110.0)  # usable at the open = candle 15's end
        self.assertIsNone(table[16]["doi_pct"])
        self.assertIsNone(table[16]["ls_global"])
        self.assertAlmostEqual(table[15]["doi_pct"], 0.1)  # 110 / 100 - 1
        self.assertIsNone(table[17]["doi_pct"])  # its open reads the missing period
        self.assertAlmostEqual(table[18]["doi_pct"], 0.0)
        self.assertIn("oi_value_end", table[16]["derivatives_missing"].split("|"))

    def test_rows_without_metrics_are_counted(self):
        series = flat(20)
        counts = {}
        ctx = ct.TableContext(series.symbol, 15, series, [0.01] * 20, "test", ct.table_segments(15), None)
        ct.write_table(io.BytesIO(), ct.build_rows(ctx), counts=counts)
        self.assertEqual(counts["oi_missing"], {"BTCUSDT|15": 20})

    def test_ls_global_z_matches_the_screen(self):
        rng = random.Random(4)
        days = 25
        minutes = days * 1440
        start = START + mx.PERIOD_MS  # load_symbol_metrics_range's grid start
        metrics = metrics_series(start, minutes // 5)
        metrics.columns["count_long_short_ratio"][:] = [1 + rng.random() for _ in range(minutes // 5)]
        prices = array("q", [100 * S] * minutes)
        zeros = array("q", [0]) * minutes
        bars = BarSeries("BTCUSDT", START, minutes, open=prices, high=prices, low=prices, close=prices,
                         volume=zeros, taker_buy_volume=zeros, trades=zeros, mark_open=prices, mark_high=prices,
                         mark_low=prices, mark_close=prices, flags=array("H", [0] * minutes))
        expected = {t: z for t, z, _ in screen.positioning_z_series(bars, metrics, "global-ls") if z is not None}
        mine = dv.hourly_ls_global_z(metrics)
        self.assertGreater(len(expected), 0)
        self.assertEqual({t: mine[t] for t in expected}, expected)


class PremiumTests(unittest.TestCase):
    def test_premium_last_and_basis(self):
        minutes = 30
        premium = array("q", [10_000 + i for i in range(minutes)])
        index = array("q", [100 * S] * minutes)
        index[29] = MISSING
        basis = BasisSeries("BTCUSDT", START, minutes, premium, index, array("H", [0] * minutes))
        mark = array("q", [100 * S + S // 100] * minutes)  # mark 100.01 vs index 100: 1 bp
        series = flat(2)
        _, table = rows(series, dv.DerivInputs(basis=basis, mark_close=mark))
        self.assertAlmostEqual(table[0]["prem_last"], 10_014 / S)
        self.assertAlmostEqual(table[0]["basis_bp"], 1.0)
        self.assertIsNone(table[1]["basis_bp"])  # index close missing at the last minute
        self.assertIsNone(table[0]["prem_z"])  # fewer than 100 earlier candle means

    def test_trailing_mean_z(self):
        times = list(range(0, 1000, 10))
        values = [float(i % 2) for i in range(100)] + [None]
        z = dv._trailing_mean_z(times + [1000], values, 10_000, 100)
        self.assertEqual(z[:100], [None] * 100)
        z = dv._trailing_mean_z(times + [1000], [float(i % 2) for i in range(100)] + [1.0], 10_000, 100)
        self.assertAlmostEqual(z[100], 1.0)  # mean 0.5, std 0.5


class FlushTests(unittest.TestCase):
    def flags(self, close, sigma, doi_z):
        series = flat(1, close=[close])
        return dv.flush(SimpleNamespace(candles=series, sigma=[sigma], doi_z=[doi_z]), 0)

    def test_thresholds(self):
        down, up = int(105 * S * 0.95), int(105 * S * 1.05)  # |ln| ~ 0.0513
        r = abs(np.log(0.95))
        self.assertEqual(self.flags(down, r / 3 - 1e-9, -3.0), {"flush_down": True, "flush_up": False, "flush_dir": -1})
        self.assertEqual(self.flags(down, r / 3 + 1e-6, -3.0)["flush_dir"], 0)  # return inside 3 sigma
        self.assertEqual(self.flags(down, r / 3 - 1e-9, -2.999)["flush_dir"], 0)  # OI did not collapse
        self.assertEqual(self.flags(up, 0.01, -5.0), {"flush_down": False, "flush_up": True, "flush_dir": 1})
        self.assertEqual(self.flags(up, None, -5.0), dict.fromkeys(("flush_down", "flush_up", "flush_dir")))
        self.assertIsNone(self.flags(up, 0.01, None)["flush_dir"])

    def test_groups_registered(self):
        versions = ct.REGISTRY.versions()
        self.assertEqual((versions["derivatives"], versions["flush"]), ("deriv-v1", "flush-v1"))
        self.assertEqual(len(set(COLUMNS)), len(COLUMNS))


if __name__ == "__main__":
    unittest.main()
