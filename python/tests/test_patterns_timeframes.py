"""Daily and weekly candles: Monday alignment, the dk1 adapter and its parity with 1m-built days (#348)."""
from array import array
import unittest

from market_analysis import data_lake
from market_analysis.benchmark.bars import COMPROMISED_FLAGS, MISSING, BarSeries
from market_analysis.benchmark.candles import (PATTERN_TIMEFRAMES, TIMEFRAMES, WEEK_ANCHOR_MS, CandleSeries,
                                               build_candles)
from market_analysis.benchmark.ta_strategies import GRID_STEP, STRATEGIES, make_specs
from market_analysis.daily_lake import DAY_MS, DailySeries
from market_analysis.patterns.daily_candles import candles_from_daily, daily_parity, first_monday_ms

START = data_lake.month_bounds_ms("2024-01")[0]  # 2024-01-01 00:00 UTC, a Monday
DAY = 1440
S = 10 ** 8


def make_bars(minutes, start_ms=START, flags=None):
    o = array("q", (100 * S + (i * 37) % 101 * 1000 for i in range(minutes)))
    c = array("q", (value + 500 for value in o))
    h = array("q", (max(a, b) + 300 for a, b in zip(o, c)))
    l = array("q", (min(a, b) - 300 for a, b in zip(o, c)))
    volume = array("q", (S + i for i in range(minutes)))
    taker = array("q", (S // 2 + i for i in range(minutes)))
    zeros = array("q", [0]) * minutes
    return BarSeries("BTCUSDT", start_ms, minutes, open=o, high=h, low=l, close=c, volume=volume,
                     taker_buy_volume=taker, trades=zeros, mark_open=o, mark_high=h, mark_low=l, mark_close=c,
                     flags=array("H", flags or [0] * minutes))


def make_daily(days, start_ms=START):
    """Day i: open 100+i, high 110+i, low 90+i, close 101+i, volume 1000+i, taker 400+i (all scaled)."""
    def column(base):
        return array("q", ((base + i) * S for i in range(days)))
    return DailySeries("BTCUSDT", start_ms, days, open=column(100), high=column(110), low=column(90),
                       close=column(101), volume=column(1000), taker_buy_volume=column(400))


def daily_from_bars(bars, days, offset=0):
    """dk1-style days from ``bars`` beginning at minute ``offset`` (a UTC day start)."""
    columns = {name: array("q") for name in ("open", "high", "low", "close", "volume", "taker_buy_volume")}
    for day in range(days):
        a, b = offset + day * DAY, offset + (day + 1) * DAY
        columns["open"].append(bars.open[a])
        columns["high"].append(max(bars.high[a:b]))
        columns["low"].append(min(bars.low[a:b]))
        columns["close"].append(bars.close[b - 1])
        columns["volume"].append(sum(bars.volume[a:b]))
        columns["taker_buy_volume"].append(sum(bars.taker_buy_volume[a:b]))
    return DailySeries(bars.symbol, bars.start_ms + offset * 60_000, days, **columns)


def empty_candles(minutes, start_ms):
    return CandleSeries("BTCUSDT", minutes, start_ms, open=[], high=[], low=[], close=[], valid=[])


class TimeframeSetTests(unittest.TestCase):
    def test_label_engine_timeframes_unchanged(self):
        self.assertEqual(TIMEFRAMES, (15, 60, 240))
        self.assertEqual(PATTERN_TIMEFRAMES, (15, 60, 240, 1440, 10080))
        self.assertEqual(set(GRID_STEP), set(TIMEFRAMES))
        name = next(iter(STRATEGIES))
        for minutes in (1440, 10080):
            with self.assertRaises(ValueError):
                make_specs({}, name, minutes, first_ms=START, end_ms=START + DAY_MS)


class AlignmentTests(unittest.TestCase):
    def test_monday_weekly_start_accepted_thursday_rejected(self):
        self.assertEqual(WEEK_ANCHOR_MS, 4 * DAY_MS)
        self.assertEqual(empty_candles(10080, START).start_ms, START)
        thursday = START + 3 * DAY_MS  # 2024-01-04, a plain epoch multiple of 7 days
        self.assertEqual(thursday % (7 * DAY_MS), 0)
        with self.assertRaisesRegex(ValueError, rf"Monday.*{thursday}"):
            empty_candles(10080, thursday)
        with self.assertRaisesRegex(ValueError, rf"Monday.*{thursday}"):
            build_candles(make_bars(7 * DAY, start_ms=thursday), 10080)
        self.assertEqual(empty_candles(1440, thursday).start_ms, thursday)
        with self.assertRaises(ValueError):
            empty_candles(1440, START + 60_000)
        with self.assertRaises(ValueError):
            empty_candles(720, START)
        self.assertEqual(first_monday_ms(START), START)
        self.assertEqual(first_monday_ms(START + 1), START + 7 * DAY_MS)
        self.assertEqual(first_monday_ms(START - 2 * DAY_MS), START)


class BuildFromMinutesTests(unittest.TestCase):
    def test_daily_from_1m_bars_invalid_minute_and_trailing_partial(self):
        flags = [0] * (3 * DAY + 100)
        flags[DAY + 7] = COMPROMISED_FLAGS
        bars = make_bars(3 * DAY + 100, flags=flags)
        candles = build_candles(bars, 1440)
        self.assertEqual(len(candles), 3)  # the trailing partial day is never built
        self.assertEqual(candles.valid, [True, False, True])
        self.assertEqual((candles.open[0], candles.close[0]), (bars.open[0], bars.close[DAY - 1]))
        self.assertEqual(candles.high[2], max(bars.high[2 * DAY:3 * DAY]))
        self.assertEqual(candles.low[2], min(bars.low[2 * DAY:3 * DAY]))
        self.assertEqual(candles.volume[0], sum(bars.volume[:DAY]))
        self.assertEqual([candles.open[1], candles.volume[1], candles.taker_buy_volume[1]], [MISSING] * 3)
        self.assertEqual(candles.end_ms[0], START + DAY_MS)

    def test_weekly_from_1m_bars(self):
        bars = make_bars(7 * DAY + 60)
        candles = build_candles(bars, 10080)
        self.assertEqual(len(candles), 1)
        self.assertTrue(candles.valid[0])
        self.assertEqual(candles.close[0], bars.close[7 * DAY - 1])
        self.assertEqual(candles.end_ms[0], START + 7 * DAY_MS)


class DailyAdapterTests(unittest.TestCase):
    def test_daily_candles_copy_scaled_values(self):
        daily = make_daily(3)
        daily.volume[1] = MISSING  # overflowing base volume: prices stay valid
        daily.close[2] = MISSING
        candles = candles_from_daily(daily, 1440)
        self.assertEqual((candles.minutes, candles.start_ms, len(candles)), (1440, START, 3))
        self.assertEqual(candles.valid, [True, True, False])
        self.assertEqual(candles.high[0], 110 * S)
        self.assertEqual(candles.volume[1], MISSING)
        self.assertEqual(candles.taker_buy_volume[1], 401 * S)
        self.assertEqual(candles.column("volume"), [1000 * S, None, None])
        self.assertEqual(candles.open[2], MISSING)
        with self.assertRaises(ValueError):
            candles_from_daily(daily, 240)

    def test_weekly_drops_partial_weeks_at_both_ends(self):
        daily = make_daily(2 + 14 + 3, start_ms=START - 2 * DAY_MS)  # Sat 2023-12-30 .. Wed 2024-01-17
        weeks = candles_from_daily(daily, 10080)
        self.assertEqual((weeks.start_ms, len(weeks)), (START, 2))
        self.assertEqual(weeks.valid, [True, True])
        # week 0 = days 2..8 of the series
        self.assertEqual(weeks.open[0], 102 * S)
        self.assertEqual(weeks.close[0], 109 * S)
        self.assertEqual(weeks.high[0], 118 * S)
        self.assertEqual(weeks.low[0], 92 * S)
        self.assertEqual(weeks.volume[0], sum((1000 + i) * S for i in range(2, 9)))
        self.assertEqual(weeks.open[1], 109 * S)
        self.assertEqual(weeks.end_ms[1], START + 14 * DAY_MS)
        self.assertEqual(len(candles_from_daily(make_daily(6), 10080)), 0)

    def test_weekly_volume_missing_if_any_day_missing_prices_stay_valid(self):
        daily = make_daily(21)
        daily.volume[3] = MISSING
        daily.low[16] = MISSING  # a price gap invalidates week 2 entirely
        weeks = candles_from_daily(daily, 10080)
        self.assertEqual(weeks.valid, [True, True, False])
        self.assertEqual(weeks.volume[0], MISSING)
        self.assertEqual(weeks.taker_buy_volume[0], sum((400 + i) * S for i in range(7)))
        self.assertEqual((weeks.open[0], weeks.close[0]), (100 * S, 107 * S))
        self.assertEqual(weeks.volume[1], sum((1000 + i) * S for i in range(7, 14)))
        self.assertEqual([weeks.high[2], weeks.volume[2], weeks.taker_buy_volume[2]], [MISSING] * 3)


class ParityTests(unittest.TestCase):
    def test_one_differing_high(self):
        bars = make_bars(3 * DAY)
        daily = daily_from_bars(bars, 3)
        built_high = daily.high[1]
        daily.high[1] = built_high + 1234
        report = daily_parity(daily, bars)
        self.assertEqual(report["days_compared"], 3)
        self.assertEqual(report["days_with_any_difference"], 1)
        self.assertEqual(report["first_differing_dates"], ["2024-01-02"])
        expected = {name: 0 for name in ("open", "high", "low", "close", "volume", "taker_buy_volume")}
        expected["high"] = 1234 * 10 ** 6 // (built_high + 1234)
        self.assertEqual(report["max_relative_difference_ppm"], expected)
        self.assertEqual(report["field_days_compared"]["volume"], 3)

    def test_skips_invalid_and_uncovered_days(self):
        flags = [0] * (3 * DAY)
        flags[5] = COMPROMISED_FLAGS
        bars = make_bars(3 * DAY + 30, start_ms=START - 30 * 60_000, flags=[0] * 30 + flags)
        daily = daily_from_bars(bars, 3, offset=30)
        daily.volume[2] = MISSING
        report = daily_parity(daily, bars)
        self.assertEqual(report["days_compared"], 2)  # day 0 has a compromised minute
        self.assertEqual(report["days_with_any_difference"], 0)
        self.assertEqual(report["field_days_compared"]["volume"], 1)
        self.assertEqual(report["field_days_compared"]["high"], 2)


if __name__ == "__main__":
    unittest.main()
