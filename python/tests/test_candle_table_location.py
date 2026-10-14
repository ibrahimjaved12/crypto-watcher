"""candle-table groups loc-v1, sweep-v1, htf-v1 and the additive level sets (hand-computed, small data)."""
from array import array
import unittest

from market_analysis import data_lake
from market_analysis.benchmark import levels as lv
from market_analysis.benchmark.bars import MISSING, BarSeries
from market_analysis.benchmark.candles import CandleSeries
from market_analysis.daily_lake import DAY_MS, DailySeries
from market_analysis.patterns import candle_location as loc
from market_analysis.patterns import candle_table as ct
from market_analysis.patterns.daily_candles import candles_from_daily

S = 10 ** 8
MIN15 = 15 * 60_000
H4 = 240 * 60_000
START = data_lake.month_bounds_ms("2024-01")[0]  # 2024-01-01 00:00 UTC, a Monday
COLUMNS = ct.REGISTRY.columns()


def series(minutes, start, o, h, l, c, symbol="ETHUSDT", valid=None):
    count = len(o)
    return CandleSeries(symbol, minutes, start, open=list(o), high=list(h), low=list(l), close=list(c),
                        valid=list(valid) if valid else [True] * count, volume=[100 * S] * count,
                        taker_buy_volume=[50 * S] * count)


def flat(count, tail=()):
    """``count`` candles high 110 low 100 open=close 105 (TR 10: atr_prev = 10 from candle 15 on), then ``tail``."""
    rows = [(105, 110, 100, 105)] * count + list(tail)
    o, h, l, c = ([int(row[k] * S) for row in rows] for k in range(4))
    return series(15, START, o, h, l, c)


def level(set_name, side, price, valid_from=0, valid_to=lv.FOREVER_MS):
    return lv.Level(set_name, side, int(price * S), valid_from, valid_to, 1)


def context(candles, levels=None, htf=None):
    return ct.TableContext(candles.symbol, candles.minutes, candles, [0.01] * len(candles), "test",
                           ct.table_segments(candles.minutes), None, levels=levels or {}, htf=htf or {})


def rows(ctx):
    return [dict(zip(COLUMNS, values)) for values in ct.build_rows(ctx)]


class LocationTests(unittest.TestCase):
    def test_touch_and_distances_by_hand(self):
        candles = flat(15, [(105, 108, 102, 106)])
        levels = {"prev_day": [level("prev_day", "high", 110.5), level("prev_day", "low", 101)],
                  "swing_tf": [level("swing_tf", "high", 120)]}
        row = rows(context(candles, levels))[15]
        self.assertEqual(row["atr_prev"], 10.0)
        # ETH round grid every 100: 100 below (touched: band [99.5, 110.5]), 200 above.
        self.assertAlmostEqual(row["dist_dn_round_atr"], 0.6)
        self.assertAlmostEqual(row["dist_up_round_atr"], 9.4)
        self.assertIs(row["touch_round"], True)
        self.assertEqual(row["approach_side_round"], -1)  # close_(i-1) = 105 above 100: support
        self.assertAlmostEqual(row["dist_up_prev_day_atr"], 0.45)
        self.assertAlmostEqual(row["dist_dn_prev_day_atr"], 0.5)
        self.assertIs(row["touch_prev_day"], True)  # 110.5 = high + 0.25 atr exactly (inclusive)
        self.assertEqual(row["approach_side_prev_day"], 1)  # nearest touched: 110.5, approached from below
        self.assertAlmostEqual(row["dist_up_swing_tf_atr"], 1.4)
        self.assertIsNone(row["dist_dn_swing_tf_atr"])
        self.assertIs(row["touch_swing_tf"], False)
        self.assertIsNone(row["approach_side_swing_tf"])
        self.assertIsNone(row["touch_prev_week"])  # source not supplied: missing
        self.assertIn("touch_prev_week", row["location_missing"].split("|"))
        self.assertEqual((row["any_touch"], row["nearest_set"], row["level_count_near"]), (True, "prev_day", 2))
        self.assertAlmostEqual(row["nearest_level_dist_atr"], 0.45)
        self.assertEqual(row["touch_approach_side"], 1)
        self.assertAlmostEqual(row["dist_ath_atr"], 0.4)
        self.assertAlmostEqual(row["dist_atl_atr"], 0.6)
        self.assertIs(row["new_ath_in_candle"], False)
        self.assertEqual(row["status"], "insufficient")  # location never sets status; trend history is short

    def test_level_validity_window(self):
        candles = flat(18)
        opened = candles.open_ms(16)

        def dist(valid_from, valid_to):
            levels = {"prev_day": [level("prev_day", "high", 107, valid_from, valid_to)]}
            return rows(context(candles, levels))[16]["dist_up_prev_day_atr"]

        self.assertAlmostEqual(dist(opened, opened + H4), 0.2)  # valid_from == open: usable
        self.assertIsNone(dist(opened + 1, opened + H4))  # valid only 1 ms after the open
        self.assertIsNone(dist(opened - H4, opened))  # valid_to == open (exclusive)
        index = loc.LevelIndex([level("prev_day", "high", 107, opened, opened + MIN15)])
        self.assertEqual(len(index.usable(opened)), 1)
        self.assertEqual(index.usable(opened + MIN15), [])
        self.assertEqual(len(index.usable(opened)), 1)  # an earlier time restarts the sweep
        self.assertEqual(index.usable(opened - 1), [])

    def test_new_ath(self):
        candles = flat(16, [(105, 112, 104, 111)])
        row = rows(context(candles))[16]
        self.assertIs(row["new_ath_in_candle"], True)
        self.assertEqual(row["dist_ath_atr"], 0.1)


class PeriodLevelTests(unittest.TestCase):
    def daily(self, start, count, invalid=()):
        highs = [(100 + j) * S for j in range(count)]
        lows = [(90 - j) * S for j in range(count)]
        for j in invalid:
            highs[j] = lows[j] = MISSING
        mids = [MISSING if j in invalid else 95 * S for j in range(count)]
        return candles_from_daily(DailySeries("BTCUSDT", start, count, open=array("q", mids),
                                              high=array("q", highs), low=array("q", lows),
                                              close=array("q", mids), volume=array("q", [S] * count),
                                              taker_buy_volume=array("q", [S] * count)), 1440)

    def test_weekly_monday_prev_week(self):
        days = self.daily(START - 2 * DAY_MS, 2 + 14)  # Sat 2023-12-30 .. Sun 2024-01-14
        levels = lv.prev_period_levels_daily(days, "week")
        self.assertEqual([(lvl.side_hint, lvl.valid_from_ms, lvl.valid_to_ms) for lvl in levels[:2]],
                         [("high", START + 7 * DAY_MS, START + 14 * DAY_MS),
                          ("low", START + 7 * DAY_MS, START + 14 * DAY_MS)])
        self.assertEqual((levels[0].price, levels[1].price), (108 * S, 82 * S))  # days 2..8 of the series
        self.assertEqual(len(levels), 4)  # the partial leading weekend makes no level
        broken = lv.prev_period_levels_daily(self.daily(START - 2 * DAY_MS, 16, invalid={4}), "week")
        self.assertEqual([lvl.valid_from_ms for lvl in broken], [START + 14 * DAY_MS] * 2)

    def test_prev_month_daily_and_bars(self):
        days = self.daily(START, 31 + 29 + 3)  # Jan, Feb 2024 (leap) and 3 days of March
        levels = lv.prev_period_levels_daily(days, "month")
        feb, mar = data_lake.month_bounds_ms("2024-02")[0], data_lake.month_bounds_ms("2024-03")[0]
        apr = data_lake.month_bounds_ms("2024-04")[0]
        self.assertEqual([(lvl.set_name, lvl.price, lvl.valid_from_ms, lvl.valid_to_ms) for lvl in levels],
                         [("prev_month", 130 * S, feb, mar), ("prev_month", 60 * S, feb, mar),
                          ("prev_month", 159 * S, mar, apr), ("prev_month", 31 * S, mar, apr)])
        minutes = 32 * 1440
        prices = array("q", (100 * S + i for i in range(minutes)))
        zeros = array("q", [0]) * minutes
        bars = BarSeries("BTCUSDT", START, minutes, open=prices, high=prices, low=prices, close=prices,
                         volume=zeros, taker_buy_volume=zeros, trades=zeros, mark_open=prices, mark_high=prices,
                         mark_low=prices, mark_close=prices, flags=array("H", [0] * minutes))
        bar_levels = lv.prev_month_levels(bars)
        self.assertEqual([(lvl.price, lvl.valid_from_ms, lvl.valid_to_ms) for lvl in bar_levels],
                         [(100 * S + 31 * 1440 - 1, feb, mar), (100 * S, feb, mar)])


def day_candles(start, highs, invalid=(), closes=None):
    count = len(highs)
    closes = closes or [105] * count
    col = lambda values: array("q", (MISSING if j in invalid else int(v * S)  # noqa: E731
                                     for j, v in enumerate(values)))
    return candles_from_daily(DailySeries("ETHUSDT", start, count, open=col(closes), high=col(highs),
                                          low=col([100] * count), close=col(closes),
                                          volume=array("q", [S] * count), taker_buy_volume=array("q", [S] * count)),
                              1440)


class OpenTimeRuleTests(unittest.TestCase):
    """Regression: a period level is never tested against the candle that formed it."""

    def test_last_4h_candle_of_a_day_sees_the_previous_day(self):
        def row(spike):
            count = 24  # four days of 4h candles; the last one is 20:00-24:00 of day 3
            o, h, l, c = [105 * S] * count, [110 * S] * count, [100 * S] * count, [105 * S] * count
            h[23], c[23] = int(spike * S), 106 * S
            candles = series(240, START, o, h, l, c)
            levels = []
            for day in range(4):  # what prev_period_levels yields: known at the day's end, valid the next day
                hi = max(h[6 * day:6 * day + 6]) / S
                start = START + (day + 1) * DAY_MS
                levels += [level("prev_day", "high", hi, start, start + DAY_MS),
                           level("prev_day", "low", 100, start, start + DAY_MS)]
            return rows(context(candles, {"prev_day": levels}))[23]

        first = row(125)
        self.assertAlmostEqual(first["dist_up_prev_day_atr"], 0.4)  # day 2's high 110, not day 3's 125
        self.assertAlmostEqual(first["dist_dn_prev_day_atr"], 0.6)
        self.assertEqual(row(140)["dist_up_prev_day_atr"], first["dist_up_prev_day_atr"])

    def daily_row(self, start, count, index):
        def at(spike):
            highs = [110] * count
            highs[index] = spike
            days = day_candles(start, highs)
            levels = {"prev_week": lv.prev_period_levels_daily(days, "week"),
                      "prev_month": lv.prev_period_levels_daily(days, "month")}
            return rows(context(days, levels))[index]
        return at(130), at(160)

    def test_sunday_daily_row_sees_the_previous_week(self):
        first, second = self.daily_row(START, 28, 20)  # day 20 = Sunday of the third week
        self.assertAlmostEqual(first["dist_up_prev_week_atr"], 0.5)  # week 2's high 110
        self.assertEqual(first["dist_up_prev_week_atr"], second["dist_up_prev_week_atr"])

    def test_month_end_daily_row_sees_the_previous_month(self):
        december = data_lake.month_bounds_ms("2023-12")[0]
        first, second = self.daily_row(december, 31 + 31 + 1, 61)  # 2024-01-31
        self.assertAlmostEqual(first["dist_up_prev_month_atr"], 0.5)  # December's high 110
        self.assertEqual(first["dist_up_prev_month_atr"], second["dist_up_prev_month_atr"])


class DailySourcedLevelTests(unittest.TestCase):
    december = data_lake.month_bounds_ms("2023-12")[0]

    def test_first_table_month_has_prev_month_from_dk1(self):
        days = day_candles(self.december, [120] * 31 + [110] * 5)
        candles = flat(16)
        shared = loc.shared_levels(days=days)
        self.assertNotIn("prev_week@daily", shared)
        levels = loc.context_levels(15, candles, shared)
        row = rows(context(candles, levels))[15]
        self.assertAlmostEqual(row["dist_up_prev_month_atr"], 1.5)  # December's high 120
        self.assertIsNotNone(row["dist_up_prev_week_atr"])  # week of 2023-12-25 (complete in dk1)

    def test_incomplete_or_invalid_periods_make_no_level(self):
        invalid = day_candles(self.december, [120] * 31 + [110] * 32, invalid={10})
        self.assertEqual([x.valid_from_ms for x in lv.prev_period_levels_daily(invalid, "month")],
                         [data_lake.month_bounds_ms("2024-02")[0]] * 2)  # December dropped, January kept
        late = day_candles(self.december + 4 * DAY_MS, [120] * 27 + [110] * 5)  # starts 2023-12-05
        self.assertEqual(lv.prev_period_levels_daily(late, "month"), [])
        weeks = [x.valid_from_ms for x in lv.prev_period_levels_daily(invalid, "week")]
        self.assertIn(self.december + 10 * DAY_MS, weeks)  # Dec 4-10 is complete and valid
        self.assertNotIn(self.december + 17 * DAY_MS, weeks)  # Dec 11-17 holds the invalid day 10


class AthTests(unittest.TestCase):
    def test_intraday_rows_are_seeded_by_dk1(self):
        history = day_candles(START - 10 * DAY_MS, [110] * 3 + [200] + [110] * 6)
        candles = flat(16)
        ctx = ct.TableContext(candles.symbol, 15, candles, [0.01] * 16, "test", ct.table_segments(15), None,
                              history_days=history)
        row = rows(ctx)[15]
        self.assertAlmostEqual(row["dist_ath_atr"], 9.5)  # (200 - 105) / 10
        self.assertIs(row["new_ath_in_candle"], False)

    def test_spike_inside_the_current_day_counts_from_its_own_candle(self):
        history = day_candles(START - 2 * DAY_MS, [150, 150, 999])  # day 3 = the current day: not used
        candles = flat(16, [(105, 160, 104, 106), (105, 110, 100, 105)])
        ath, _, before, _ = loc.running_extremes(candles, history)
        self.assertEqual((ath[15], ath[16], ath[17]), (150 * S, 160 * S, 160 * S))
        self.assertEqual((before[16], before[17]), (150 * S, 160 * S))
        table = rows(ct.TableContext(
            candles.symbol, 15, candles, [0.01] * 18, "test", ct.table_segments(15), None, history_days=history))
        self.assertEqual((table[16]["new_ath_in_candle"], table[17]["new_ath_in_candle"]), (True, False))
        self.assertAlmostEqual(table[15]["dist_ath_atr"], 4.5)

    def test_weekly_row_uses_days_of_its_own_week(self):
        highs = [110] * 14
        highs[9] = 300
        days = day_candles(START, highs)
        weeks = candles_from_daily(DailySeries("ETHUSDT", START, 14, *(array("q", [105 * S] * 14),) * 4,
                                               array("q", [S] * 14), array("q", [S] * 14)), 10080)
        ath, _, before, _ = loc.running_extremes(weeks, days)
        self.assertEqual((ath[0], ath[1], before[1]), (110 * S, 300 * S, 110 * S))

    def test_no_seed_keeps_series_only_extremes(self):
        candles = flat(3, [(105, 120, 90, 105)])
        ath, atl, before, low_before = loc.running_extremes(candles)
        self.assertEqual(ath, [110 * S] * 3 + [120 * S])
        self.assertEqual(before, [None, 110 * S, 110 * S, 110 * S])
        self.assertEqual((atl[3], low_before[3]), (90 * S, 100 * S))


class SweepTests(unittest.TestCase):
    levels = {"prev_day": [level("prev_day", "high", 109), level("prev_day", "low", 101)]}

    def row(self, tail):
        return rows(context(flat(25, [tail]), self.levels))[25]  # prior20: high 110, low 100

    def test_sweep_up_down_both(self):
        up = self.row((105, 111.5, 104, 108.5))
        self.assertEqual((up["sweep_up_prev_day"], up["sweep_up_prior20"], up["sweep_dn_prev_day"]), (True, True, False))
        self.assertEqual((up["sweep_any_up"], up["sweep_both"], up["sweep_dir"]), (True, False, -1))
        self.assertAlmostEqual(up["sweep_depth_atr"], 0.25)  # 111.5 - 109 = 2.5 = 0.25 atr
        self.assertIsNone(up["sweep_up_swing_tf"])  # source not supplied
        down = self.row((105, 106, 98.5, 101.5))
        self.assertEqual((down["sweep_dn_prev_day"], down["sweep_dn_prior20"], down["sweep_dir"]), (True, True, 1))
        both = self.row((105, 111.5, 98.5, 105))
        self.assertEqual((both["sweep_both"], both["sweep_dir"]), (True, 0))
        self.assertAlmostEqual(both["sweep_depth_atr"], 0.25)

    def test_close_back_inside_and_pierce_rules(self):
        inside = self.row((105, 111.5, 104, 109.5))  # closes above the prev_day high, below prior20
        self.assertEqual((inside["sweep_up_prev_day"], inside["sweep_up_prior20"]), (False, True))
        shallow = self.row((105, 110, 104, 108))  # 110 is not above 109 + 0.1 atr = 110
        self.assertIs(shallow["sweep_up_prev_day"], False)
        none = self.row((105, 108, 102, 106))
        self.assertEqual((none["sweep_any_up"], none["sweep_any_dn"], none["sweep_dir"]), (False, False, 0))
        self.assertIsNone(none["sweep_depth_atr"])
        self.assertNotIn("sweep_depth_atr", none["sweep_missing"].split("|"))


class HtfTests(unittest.TestCase):
    def four_hour(self, count=60, jump=None):
        closes = [(100 + k) * S for k in range(count)]
        if jump is not None:
            closes[jump] = 300 * S
        opens = [closes[0]] + closes[:-1]
        return series(240, START, opens, [max(a, b) + S for a, b in zip(opens, closes)],
                      [min(a, b) - S for a, b in zip(opens, closes)], closes)

    def test_last_closed_index_never_uses_a_forming_candle(self):
        c240 = self.four_hour()
        self.assertEqual(loc.last_closed_index(c240, c240.end_ms[10]), 10)
        self.assertEqual(loc.last_closed_index(c240, c240.end_ms[10] - 1), 9)  # candle 10 ends 1 ms later
        self.assertIsNone(loc.last_closed_index(c240, c240.end_ms[0] - 1))

    def test_rows_read_the_last_closed_higher_candle(self):
        k = 54
        count15 = 16 * (k + 2)
        flat15 = series(15, START, [105 * S] * count15, [106 * S] * count15, [104 * S] * count15,
                        [105 * S] * count15)
        before = rows(context(flat15, htf={240: self.four_hour()}))
        after = rows(context(flat15, htf={240: self.four_hour(jump=k)}))
        forming = 16 * (k + 1) - 2  # decides 15 minutes before 4h candle k closes
        closed = 16 * (k + 1) - 1   # decides exactly at its close, but opened while k was forming
        following = closed + 1      # opens at k's close
        self.assertEqual(flat15.end_ms[closed], self.four_hour().end_ms[k])
        names = [n for n in COLUMNS if n.endswith("_4h") or "_4h_" in n]
        self.assertEqual(len(names), 4)
        for row in (forming, closed):
            self.assertEqual({n: before[row][n] for n in names}, {n: after[row][n] for n in names})
        self.assertNotEqual(before[following]["extension_4h_atr"], after[following]["extension_4h_atr"])
        state = loc.htf_state(self.four_hour())
        ind = state.ind[k - 1]
        self.assertEqual(before[forming]["trend_dir_4h"], 1)
        self.assertEqual(before[forming]["adx_4h"], ind["adx"])
        self.assertAlmostEqual(before[forming]["ema_gap_4h_atr"], (ind["ema_fast"] - ind["ema_slow"]) / ind["atr"])
        self.assertIsNone(before[forming]["trend_dir_1h"])  # 15m maps to 1h and 4h; 1h not supplied: missing
        self.assertIn("trend_dir_1h", before[forming]["htf_missing"].split("|"))
        self.assertNotIn("trend_dir_1d", before[forming]["htf_missing"].split("|"))  # not applicable to 15m

    def test_htf_map(self):
        self.assertEqual(loc.HTF_MAP, {15: (60, 240), 60: (240, 1440), 240: (1440, 10080), 1440: (10080,),
                                       10080: ()})


class LevelSetTests(unittest.TestCase):
    def test_level_sets_and_swing4h_unchanged(self):
        self.assertEqual(lv.LEVEL_SETS, ("round", "prev_day", "prev_week", "swing4h"))
        self.assertEqual(lv.PATTERN_LEVEL_SETS, ("round", "prev_day", "prev_week", "prev_month", "swing_tf",
                                                 "swing4h", "swing1d", "swing1w"))
        highs = [100, 101, 102, 103, 104, 110, 104, 103, 102, 101, 100, 99]
        c240 = series(240, START, [h * S - S for h in highs], [h * S for h in highs],
                      [h * S - 2 * S for h in highs], [h * S - S for h in highs], symbol="BTCUSDT")
        old = lv.swing_levels(c240)
        self.assertEqual(old, [lv.Level("swing4h", "high", 110 * S, c240.end_ms[10],
                                        c240.end_ms[10] + 90 * DAY_MS, 1)])
        self.assertEqual(old, lv.swing_levels(c240, lv.SWING_N, lv.SWING_VALID_DAYS, "swing4h"))
        tf = lv.pattern_swing_levels(c240, "swing_tf")
        self.assertEqual([(x.set_name, x.price, x.valid_to_ms - x.valid_from_ms) for x in tf],
                         [("swing_tf", 110 * S, 90 * DAY_MS)])
        c60 = series(60, START, c240.open, c240.high, c240.low, c240.close)
        with self.assertRaises(ValueError):
            lv.swing_levels(c60)  # other timeframes need an explicit set name
        self.assertEqual([x.valid_to_ms - x.valid_from_ms for x in lv.pattern_swing_levels(c60, "swing_tf")],
                         [30 * DAY_MS])
        with self.assertRaises(ValueError):
            lv.pattern_swing_levels(c60, "swing1d")

    def test_groups_registered(self):
        versions = ct.REGISTRY.versions()
        self.assertEqual((versions["location"], versions["sweep"], versions["htf"]), ("loc-v1", "sweep-v1", "htf-v1"))
        self.assertEqual(len(set(COLUMNS)), len(COLUMNS))
        self.assertTrue({"location_ok", "sweep_missing", "htf_ok"} <= set(COLUMNS))


if __name__ == "__main__":
    unittest.main()
