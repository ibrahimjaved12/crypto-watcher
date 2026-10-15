"""candle-table group lv3 (lv3-v1): period opens (second-candle rule, week/month boundaries) and touch_sig."""
import io
import unittest

from market_analysis import data_lake
from market_analysis.benchmark import levels as lv
from market_analysis.benchmark.candles import CandleSeries
from market_analysis.patterns import candle_levels3 as lv3
from market_analysis.patterns import candle_table as ct

S = 10 ** 8
MIN = data_lake.MINUTE_MS
DAY = 1440 * MIN
START = data_lake.month_bounds_ms("2024-01")[0]  # Monday 2024-01-01
FEB = data_lake.month_bounds_ms("2024-02")[0]    # Thursday 2024-02-01
DAILY_START = data_lake.month_bounds_ms("2021-01")[0]
COLUMNS = ct.REGISTRY.columns()


def series(minutes, start, o, h, l, c, valid=None, symbol="BTCUSDT"):
    n = len(o)
    return CandleSeries(symbol, minutes, start, open=list(o), high=list(h), low=list(l), close=list(c),
                        valid=list(valid) if valid is not None else [True] * n, volume=[100 * S] * n,
                        taker_buy_volume=[50 * S] * n)


def stepped(minutes, start, count, price_of=lambda k: 100 + k):
    """Candle k: open = price_of(k), close = open + 0.5, high = open + 1, low = open - 1 (USDT)."""
    o = [price_of(k) * S for k in range(count)]
    return series(minutes, start, o, [p + S for p in o], [p - S for p in o], [p + S // 2 for p in o])


def context(candles, history_days=None):
    return ct.TableContext(candles.symbol, candles.minutes, candles, [0.01] * len(candles), "test",
                           ct.table_segments(candles.minutes), None, history_days=history_days)


def rows(ctx):
    return {row["open_ms"]: row for row in (dict(zip(COLUMNS, values)) for values in ct.build_rows(ctx))}


class PeriodOpenTests(unittest.TestCase):
    def test_second_candle_rule_and_day_boundaries(self):
        candles = stepped(240, START, 6 * 4)
        levels = lv3.period_open_levels(candles, "day", 240)
        self.assertEqual([(lvl.valid_from_ms, lvl.valid_to_ms) for lvl in levels],
                         [(START + d * DAY + 240 * MIN, START + (d + 1) * DAY) for d in range(4)])
        self.assertEqual([lvl.price for lvl in levels], [candles.open[6 * d] for d in range(4)])
        same_on_15m = lv3.period_open_levels(candles, "day", 15)
        self.assertEqual(same_on_15m[0].valid_from_ms, START + 15 * MIN)

    def test_week_and_month_boundaries(self):
        count = 6 * 45  # 2024-01-01 .. 2024-02-14 on 4h candles
        candles = stepped(240, START, count)
        weeks = lv3.period_open_levels(candles, "week", 240)
        self.assertEqual(weeks[0].valid_from_ms, START + 240 * MIN)  # 2024-01-01 is a Monday
        self.assertEqual(weeks[1].valid_from_ms - weeks[1].valid_to_ms + 7 * DAY, 240 * MIN)
        self.assertTrue(all((lvl.valid_from_ms - 240 * MIN - START) % (7 * DAY) == 0 for lvl in weeks))
        months = lv3.period_open_levels(candles, "month", 240)
        self.assertEqual([(lvl.valid_from_ms, lvl.valid_to_ms) for lvl in months],
                         [(START + 240 * MIN, FEB), (FEB + 240 * MIN, data_lake.month_bounds_ms("2024-03")[0])])
        self.assertEqual(months[1].price, candles.open[(FEB - START) // (240 * MIN)])

    def test_invalid_first_candle_gives_no_level(self):
        candles = stepped(240, START, 12)
        valid = [True] * 12
        valid[6] = False  # the first candle of 2024-01-02
        broken = series(240, START, candles.open, candles.high, candles.low, candles.close, valid=valid)
        starts = [lvl.valid_from_ms for lvl in lv3.period_open_levels(broken, "day", 240)]
        self.assertEqual(starts, [START + 240 * MIN])

    def test_weekly_rows_and_not_applicable_sets(self):
        self.assertEqual(lv3.period_open_levels(stepped(10080, DAILY_START + 3 * DAY, 3), "week", 10080), [])
        self.assertEqual(lv3.OPEN_SETS_BY_TF[1440], ("week_open", "month_open"))
        self.assertEqual(lv3.OPEN_SETS_BY_TF[10080], ("month_open",))

    def test_row_columns_follow_the_second_candle_rule(self):
        # flat candles at 105 (open 105): the day open is touched by every candle once usable
        n = 6 * 20
        o = [105 * S] * n
        candles = series(240, START, o, [110 * S] * n, [100 * S] * n, o)
        table = rows(context(candles))
        day_start = START + 18 * DAY
        first, second = table[day_start], table[day_start + 240 * MIN]
        self.assertIsNone(first["touch_day_open"])             # the period's first candle: not applicable
        self.assertIsNone(first["dist_up_day_open_atr"])
        self.assertNotIn("touch_day_open", first["lv3_missing"].split("|"))
        self.assertTrue(second["touch_day_open"])
        self.assertEqual((second["dist_up_day_open_atr"], second["dist_dn_day_open_atr"]), (0.0, 0.0))
        self.assertEqual(second["level_side_day_open"], 1)    # at close: the +1 side
        self.assertTrue(second["touch_sig"])                  # week_open / month_open at 105 as well
        self.assertEqual(second["sig_nearest_set"], "week_open")
        self.assertEqual(second["sig_nearest_level_dist_atr"], 0.0)

    def test_level_side_and_distances(self):
        candles = stepped(240, START, 6 * 20)
        table = rows(context(candles))
        row = table[START + (18 * 6 + 3) * 240 * MIN]  # 4th candle of the day: price has risen 3 since the open
        self.assertEqual(row["level_side_day_open"], 1)
        self.assertIsNone(row["dist_up_day_open_atr"])
        self.assertGreater(row["dist_dn_day_open_atr"], 0)
        down = stepped(240, START, 6 * 20, price_of=lambda k: 300 - k)
        row = rows(context(down))[START + (18 * 6 + 3) * 240 * MIN]
        self.assertEqual(row["level_side_day_open"], -1)
        self.assertGreater(row["dist_up_day_open_atr"], 0)


class TouchSigTests(unittest.TestCase):
    def test_round_sig_is_the_coarse_grid(self):
        # the grid covering [lo, hi] plus the nearest coarse level beyond each end (loc-v1's round rule)
        self.assertEqual(lv3._sig_round("BTCUSDT", 52_000 * S, 49_000 * S, 56_000 * S),
                         [45_000 * S, 50_000 * S, 55_000 * S, 60_000 * S])
        self.assertTrue(all(lv.round_tier("BTCUSDT", p) >= 5000 for p in lv3._sig_round("BTCUSDT", 52_000 * S,
                                                                                      40_000 * S, 70_000 * S)))
        self.assertEqual(lv3._sig_round("DOGEUSDT", 12 * S // 100, 11 * S // 100, 13 * S // 100),
                         [10 * S // 100, 15 * S // 100])

    def test_share_on_a_synthetic_frame(self):
        candles = stepped(240, START, 6 * 30, price_of=lambda k: 100 + (k % 37))
        counts = {}
        table = list(ct.build_rows(context(candles)))
        ct.write_table(io.BytesIO(), iter(table), counts=counts)
        where = COLUMNS.index("touch_sig")
        filled = [row[where] for row in table if row[where] is not None]
        self.assertEqual(counts["touch_sig_rows"], {"240": len(filled)})
        self.assertEqual(counts.get("touch_sig_true", {}).get("240", 0), sum(filled))
        share = ct.touch_sig_share(counts)["240"]
        self.assertEqual(share["share"], f"{sum(filled) / len(filled):.6f}")
        manifest = ct.build_manifest(files=[], counts=counts, inputs={}, through_month="2025-12", parity={},
                                     symbols=["BTCUSDT"], timeframes=[240], code_commit="a", created_utc="b")
        self.assertEqual(manifest["touch_sig_share"]["240"]["rows"], len(filled))
        ct.check_manifest(manifest)
        self.assertTrue(any(line.startswith("touch_sig touched/rows tf240=") for line in ct.public_lines(manifest)))

    def test_outside_range_is_listed_not_tuned(self):
        counts = {"touch_sig_rows": {"15": 1000, "60": 100, "240": 50}, "touch_sig_true": {"15": 100, "60": 30}}
        shares = ct.touch_sig_share(counts)
        self.assertEqual(list(shares), ["15", "60", "240"])
        self.assertEqual(shares["240"], {"rows": 50, "touched": 0, "share": "0.000000"})
        manifest = ct.build_manifest(files=[], counts=counts, inputs={}, through_month="2025-12", parity={},
                                     symbols=["BTCUSDT"], timeframes=[15, 60, 240], code_commit="a", created_utc="b")
        self.assertEqual(manifest["touch_sig_outside_range"], ["240", "60"])
        self.assertIn("outside", ct.public_lines(manifest)[1])


if __name__ == "__main__":
    unittest.main()
