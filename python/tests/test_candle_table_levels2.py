"""candle-table group lv2-v1: liquidity pools, anchored VWAP and volume profile (hand-computed, small data)."""
from array import array
import unittest

from market_analysis import data_lake
from market_analysis.benchmark import levels as lv
from market_analysis.benchmark.bars import MISSING, BarSeries
from market_analysis.benchmark.candles import CandleSeries
from market_analysis.daily_lake import DAY_MS
from market_analysis.patterns import candle_levels2 as lv2
from market_analysis.patterns import candle_table as ct

S = 10 ** 8
START = data_lake.month_bounds_ms("2024-01")[0]
COLUMNS = ct.REGISTRY.columns()


def candles(minutes, rows, volume=None, valid=None):
    o, h, l, c = ([int(round(row[k] * S)) for row in rows] for k in range(4))
    count = len(rows)
    volume = volume or [100 * S] * count
    return CandleSeries("ETHUSDT", minutes, START, open=o, high=h, low=l, close=c,
                        valid=list(valid) if valid else [True] * count, volume=list(volume),
                        taker_buy_volume=[S] * count)


def context(series, **extra):
    return ct.TableContext(series.symbol, series.minutes, series, [0.01] * len(series), "test",
                           ct.table_segments(series.minutes), None, **extra)


def rows(ctx):
    return [dict(zip(COLUMNS, values)) for values in ct.build_rows(ctx)]


BASE = (105, 110, 100, 105)


def pool_days(count=40, spike=True, pierce=True):
    rows_ = [BASE] * count
    rows_[5] = (105, 120, 100, 105)
    rows_[15] = (105, 120.1, 100, 105)
    if pierce:
        rows_[22] = (105, 121.5, 100, 110)  # pierces the pool and closes back below (not a swing: 25 is higher)
    if spike:
        rows_[25] = (105, 123, 100, 122)  # closes beyond the pool: invalidates it
    return candles(1440, rows_)


class PoolTests(unittest.TestCase):
    def test_clustering_and_invalidation_by_close(self):
        days = pool_days()
        pools = lv2.liquidity_pools(days)
        self.assertEqual(pools, [lv.Level("pool", "high", 12005000000, days.end_ms[18], days.end_ms[25], 2)])

    def test_invalidation_after_90_days(self):
        days = pool_days(count=120, spike=False, pierce=False)
        (pool,) = lv2.liquidity_pools(days)
        self.assertEqual((pool.valid_from_ms, pool.valid_to_ms), (days.end_ms[18], days.end_ms[18] + 90 * DAY_MS))

    def test_rows_use_pools_only_after_confirmation(self):
        days = pool_days()
        ctx = context(days)
        table = rows(ctx)
        self.assertIsNone(table[18]["pool_dist_up_atr"])  # opens before the confirming candle closes
        atr = ctx.ind[18]["atr"]
        self.assertAlmostEqual(table[19]["pool_dist_up_atr"], (120.05 - 105) / atr)
        self.assertEqual(table[19]["pool_touches_up"], 2)
        self.assertIsNone(table[19]["pool_dist_dn_atr"])
        self.assertIs(table[19]["pool_swept"], False)
        self.assertIs(table[22]["pool_swept"], True)
        self.assertIsNone(table[26]["pool_dist_up_atr"])  # invalidated by candle 25's close

    def test_intraday_rows_without_daily_history_have_no_pools(self):
        row = rows(context(candles(15, [BASE] * 20)))[16]
        self.assertIsNone(row["pool_dist_up_atr"])
        self.assertIn("pool_dist_up_atr", row["levels2_missing"].split("|"))


class AvwapTests(unittest.TestCase):
    def test_four_candle_example(self):
        series = candles(15, [(10, 12, 8, 10), (11, 14, 10, 12), (12, 15, 11, 13), (13, 20, 1, 9)],
                         volume=[1 * S, 3 * S, 2 * S, 50 * S])
        # typical prices 10, 12, 13 with volumes 1, 3, 2: (10 + 36 + 26) / 6 = 12; candle 3 is not read
        self.assertAlmostEqual(lv2.anchored_vwap(series, 0, 3), 12.0)
        self.assertIsNone(lv2.anchored_vwap(series, 2, 2))
        gap = candles(15, [(10, 12, 8, 10)] * 3, volume=[S, MISSING, S])
        self.assertIsNone(lv2.anchored_vwap(gap, 0, 3))
        broken = candles(15, [(10, 12, 8, 10)] * 3, valid=[True, False, True])
        self.assertIsNone(lv2.anchored_vwap(broken, 0, 3))

    def rows_with_swing_low(self, volume=None):
        data = [BASE] * 20
        data[10] = (105, 110, 90, 105)
        series = candles(15, data, volume=volume)
        ctx = context(series)
        return series, ctx, rows(ctx)

    def test_anchor_is_used_only_after_confirmation(self):
        series, ctx, table = self.rows_with_swing_low()
        self.assertIsNone(table[15]["avwap_lo_dist_atr"])  # swing 10 is confirmed at the end of candle 15
        atr = ctx.ind[15]["atr"]
        expected = lv2.anchored_vwap(series, 10, 16)
        self.assertAlmostEqual(table[16]["avwap_lo_dist_atr"], (expected - 105) / atr)
        self.assertIsNone(table[16]["avwap_hi_dist_atr"])  # no swing high
        day = lv2.anchored_vwap(series, 0, 16)
        self.assertAlmostEqual(table[16]["vwap_day_dist_atr"], (day - 105) / atr)

    def test_missing_volume_empties_vwaps(self):
        volume = [100 * S] * 20
        volume[12] = MISSING
        _, _, table = self.rows_with_swing_low(volume)
        self.assertIsNone(table[16]["avwap_lo_dist_atr"])
        self.assertIsNone(table[16]["vwap_day_dist_atr"])
        self.assertIn("vwap_day_dist_atr", table[16]["levels2_missing"].split("|"))

    def test_daily_rows_have_no_day_vwap_column(self):
        row = rows(context(candles(1440, [BASE] * 20)))[16]
        self.assertNotIn("vwap_day_dist_atr", row["levels2_missing"].split("|"))


class ProfileTests(unittest.TestCase):
    def test_value_area_by_hand(self):
        # total 30, 70 % = 21: POC 1 (10); up pair 2+3 = 11 > down pair 0+(-1) = 9 -> 21: done
        self.assertEqual(lv2.value_area({-1: 4, 0: 5, 1: 10, 2: 3, 3: 8}), (1, 1, 3))
        # tie between the single bins above and below: the upper side is added
        self.assertEqual(lv2.value_area({0: 5, 1: 10, 2: 5}), (1, 1, 2))
        self.assertEqual(lv2.value_area({4: 3, 5: 3}), (4, 4, 5))  # POC tie: the lower bin
        self.assertIsNone(lv2.value_area({}))

    def test_daily_profiles_are_point_in_time(self):
        minutes = 31 * 1440
        closes = array("q", ((100 if m < 15 * 1440 else 105 if m < 30 * 1440 else 200) * S for m in range(minutes)))
        volume = array("q", ((1 if m < 15 * 1440 else 2 if m < 30 * 1440 else 1000) * S for m in range(minutes)))
        zeros = array("q", [0]) * minutes
        bars = BarSeries("ETHUSDT", START, minutes, open=closes, high=closes, low=closes, close=closes,
                         volume=volume, taker_buy_volume=zeros, trades=zeros, mark_open=closes, mark_high=closes,
                         mark_low=closes, mark_close=closes, flags=array("H", [0] * minutes))
        profiles = lv2.daily_profiles(bars)
        self.assertEqual(list(profiles), [START + 30 * DAY_MS])  # needs 30 complete earlier days
        poc, val, vah = profiles[START + 30 * DAY_MS][30]
        self.assertLess(abs(poc / 105 - 1), 0.001)  # day 30's 200 is not read
        self.assertLessEqual(val, 100)
        self.assertGreaterEqual(vah, 105)
        self.assertNotIn(90, profiles[START + 30 * DAY_MS])

    def test_row_columns(self):
        series = candles(15, [BASE] * 16)
        row = rows(context(series, profiles={START: {30: (106.0, 100.0, 108.0)}}))[15]
        self.assertAlmostEqual(row["dist_poc_30d_atr"], 0.1)
        self.assertAlmostEqual(row["dist_val_30d_atr"], -0.5)
        self.assertAlmostEqual(row["dist_vah_30d_atr"], 0.3)
        self.assertEqual((row["in_value_area_30d"], row["touch_poc_30d"]), (True, True))
        self.assertIsNone(row["dist_poc_90d_atr"])

    def test_group_registered(self):
        self.assertEqual(ct.REGISTRY.versions()["levels2"], "lv2-v1")
        self.assertEqual(len(set(COLUMNS)), len(COLUMNS))


if __name__ == "__main__":
    unittest.main()
