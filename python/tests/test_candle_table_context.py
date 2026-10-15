"""candle-table groups prev (prev-v1), ctx2 (ctx2-v1) and sigma_seas (ss-v1): hand-built frames, point in time."""
from math import log
import random
import unittest

from market_analysis import data_lake
from market_analysis.benchmark.candles import CandleSeries
from market_analysis.patterns import candle_context as cx
from market_analysis.patterns import candle_table as ct

S = 10 ** 8
MIN = data_lake.MINUTE_MS
DAY = 1440 * MIN
START = data_lake.month_bounds_ms("2024-01")[0]  # a Monday
DAILY_START = data_lake.month_bounds_ms("2021-01")[0]
COLUMNS = ct.REGISTRY.columns()
NEW_GROUPS = ("prev", "ctx2", "lv3")


def series(minutes, start, o, h, l, c, *, volume=None, valid=None, symbol="BTCUSDT"):
    n = len(o)
    return CandleSeries(symbol, minutes, start, open=list(o), high=list(h), low=list(l), close=list(c),
                        valid=list(valid) if valid is not None else [True] * n,
                        volume=list(volume) if volume is not None else [100 * S] * n,
                        taker_buy_volume=[50 * S] * n)


def walk(count, seed, minutes=1440, start=DAILY_START):
    rng = random.Random(seed)
    price, o, h, l, c, v = 100 * S, [], [], [], [], []
    for _ in range(count):
        close = max(S, int(price * (1 + rng.gauss(0, 0.01))))
        o.append(price)
        c.append(close)
        h.append(max(price, close) + rng.randint(1, S // 2))
        l.append(min(price, close) - rng.randint(1, S // 2))
        v.append(rng.randint(50, 150) * S)
        price = close
    return series(minutes, start, o, h, l, c, volume=v)


def context(candles, sigma=None, history_days=None):
    sigma = [0.01] * len(candles) if sigma is None else sigma
    return ct.TableContext(candles.symbol, candles.minutes, candles, sigma, "test",
                           ct.table_segments(candles.minutes), None, history_days=history_days)


def rows(ctx):
    return [dict(zip(COLUMNS, values)) for values in ct.build_rows(ctx)]


def flat(count, minutes=1440, start=DAILY_START):
    """``count`` candles open = close 105, high 110, low 100 (ATR14 = 10 once warm)."""
    return [105 * S] * count, [110 * S] * count, [100 * S] * count, [105 * S] * count


def with_last(minutes, start, count, last, prev=None):
    o, h, l, c = flat(count)
    if prev is not None:
        o[-1], h[-1], l[-1], c[-1] = prev
    return series(minutes, start, o + [last[0]], h + [last[1]], l + [last[2]], c + [last[3]])


class PrevTests(unittest.TestCase):
    def test_confirm_dir_on_a_fixed_frame(self):
        cases = {112: (1, 0.2), 95: (-1, 0.5), 105: (0, None), 110: (0, None)}
        for close, (direction, excess) in cases.items():
            candles = with_last(1440, DAILY_START, 20, (105 * S, max(close, 105) * S, min(close, 105) * S, close * S))
            row = rows(context(candles))[-1]
            self.assertEqual(row["confirm_dir"], direction, close)
            if excess is None:
                self.assertIsNone(row["confirm_excess_atr"])  # not applicable when confirm_dir = 0
            else:
                self.assertAlmostEqual(row["confirm_excess_atr"], excess)
        self.assertEqual((row["prev_high"], row["prev_low"]), (110.0, 100.0))
        self.assertTrue(row["prev_doji_b10"])
        self.assertEqual(row["prev_doji_subtype"], "long_legged")
        self.assertEqual(row["prev_body_ratio"], 0.0)
        self.assertAlmostEqual(row["prev_range_atr"], 1.0)

    def test_previous_invalid_candle_empties_the_group(self):
        candles = with_last(1440, DAILY_START, 20, (105 * S, 112 * S, 104 * S, 112 * S))
        broken = series(1440, DAILY_START, candles.open, candles.high, candles.low, candles.close,
                        valid=[True] * 19 + [False, True])
        row = rows(context(broken))[-1]
        self.assertIsNone(row["confirm_dir"])
        self.assertIsNone(row["prev_high"])
        self.assertFalse(row["prev_ok"])
        self.assertNotIn("prev", row["reasons"].split(";"))  # prev never sets status

    def test_prev_subtype_matches_geometry(self):
        table = rows(context(walk(80, 3)))
        for before, row in zip(table, table[1:]):
            self.assertEqual(row["prev_doji_subtype"], before["doji_subtype"])
            self.assertEqual(row["prev_doji_b10"], before["doji_b10"])
            if before["range_atr"] is not None:
                self.assertAlmostEqual(row["prev_range_atr"], before["range_atr"])


class Ctx2Tests(unittest.TestCase):
    def rising(self, count=70):
        c = [(100 + k) * S for k in range(count)]
        o = [value - S // 2 for value in c]
        return o, [value + S for value in c], [value - 2 * S for value in c], c

    def test_trend_agree(self):
        o, h, l, c = self.rising()
        row = rows(context(series(1440, DAILY_START, o, h, l, c)))[-1]
        self.assertEqual((row["trend_dir"], row["trend_px20_prev"], row["trend_px50_prev"]), (1, 1, 1))
        self.assertTrue(row["trend_agree"])
        c[-2] = o[-2] = l[-2] = 100 * S  # candle i-1 closes far below EMA20: px20 flips, EMA20 still above EMA50
        row = rows(context(series(1440, DAILY_START, o, h, l, c)))[-1]
        self.assertEqual((row["trend_dir"], row["trend_px20_prev"]), (1, -1))
        self.assertFalse(row["trend_agree"])

    def zigzag(self, drift, count=90):
        mids = [1000 + drift * k + (3.0 * (k % 12) if k % 12 <= 6 else 3.0 * (12 - k % 12)) for k in range(count)]
        o = [int(m * S) for m in mids]
        return series(1440, DAILY_START, o, [int((m + 0.2) * S) for m in mids], [int((m - 0.2) * S) for m in mids], o)

    def test_swing_trend_dir(self):
        self.assertEqual(rows(context(self.zigzag(0.5)))[-1]["swing_trend_dir"], 1)
        self.assertEqual(rows(context(self.zigzag(-0.5)))[-1]["swing_trend_dir"], -1)
        self.assertEqual(rows(context(self.zigzag(0.0)))[-1]["swing_trend_dir"], 0)
        self.assertIsNone(rows(context(self.zigzag(0.5, 14)))[-1]["swing_trend_dir"])  # fewer than two swings

    def test_swing_confirmation_lag(self):
        candles = self.zigzag(0.5)
        highs, _ = cx._last_two_swings(candles, 5)
        confirmations = cx._swings(candles, 5)
        first_two = sorted(k for k, items in confirmations.items() for side, _ in items if side == "high")[:2]
        self.assertIsNone(highs[first_two[1]])        # confirmed at the end of that candle: not yet known
        self.assertIsNotNone(highs[first_two[1] + 1])

    def test_prior20_reclaim(self):
        for prev, expected in (((105 * S, 110 * S, 98 * S, 105 * S), 1), ((105 * S, 112 * S, 100 * S, 105 * S), -1),
                               ((105 * S, 112 * S, 98 * S, 105 * S), 0), ((105 * S, 110 * S, 98 * S, 99 * S), 0)):
            candles = with_last(1440, DAILY_START, 25, (105 * S, 110 * S, 100 * S, 105 * S), prev=prev)
            self.assertEqual(rows(context(candles))[-1]["prior20_reclaim_dir"], expected, prev)

    def test_climax_age_five_candles_back(self):
        n = 40
        o, h, l, c = flat(n)
        c = [int(105.5 * S)] * n
        volume = [100 * S] * n
        volume[n - 1 - 5] = 1000 * S   # row i = n - 1: candle i-5
        c[n - 1 - 4] = 115 * S         # the largest |return| at i-4
        h[n - 1 - 4] = 116 * S
        candles = series(1440, DAILY_START, o, h, l, c, volume=volume)
        row = rows(context(candles))[-1]
        self.assertEqual(row["climax_age6"], 5)
        self.assertAlmostEqual(row["climax_vol_max6"], 10.0)  # 1000 / mean of the 20 earlier candles (all 100)
        self.assertAlmostEqual(row["climax_ret_sigma6"], log(115 / 105) / 0.01)

    def test_htf_trend_dir_uses_days_closed_before_the_open(self):
        o, h, l, c = self.rising(80)
        days = series(1440, DAILY_START, o, h, l, c)
        weeks_start = DAILY_START + 3 * DAY  # 2021-01-04, a Monday (2021-01-01 is a Friday)
        week = walk(8, 2, minutes=10080, start=weeks_start)
        ctx = context(week, [0.03] * len(week), history_days=days)
        table = rows(ctx)
        self.assertEqual(table[-1]["htf_trend_dir"], 1)
        k = cx.last_closed_index(days, week.open_ms(len(week) - 1))
        self.assertEqual(days.end_ms[k], week.open_ms(len(week) - 1))  # the day ending at the open, not later
        self.assertIsNone(rows(context(week, [0.03] * len(week)))[-1]["htf_trend_dir"])


class PointInTimeTests(unittest.TestCase):
    def check(self, candles, i, sigma=None, history_days=None):
        base = rows(context(candles, sigma, history_days))
        later = [v if j <= i else v * 13 // 10 for j, v in enumerate(candles.close)]
        high = [max(a, b) for a, b in zip(candles.high, later)]
        changed = series(candles.minutes, candles.start_ms, candles.open, high, candles.low, later,
                         volume=[v if j <= i else v * 3 for j, v in enumerate(candles.volume)])
        other = rows(context(changed, sigma, history_days))
        names = [name for group in ct.REGISTRY.groups if group.id in NEW_GROUPS for name in group.names]
        names.append("sigma_seas_factor")
        row_a = next(r for r in base if r["open_ms"] == candles.open_ms(i))
        row_b = next(r for r in other if r["open_ms"] == candles.open_ms(i))
        for name in names:
            self.assertEqual(row_a[name], row_b[name], name)

    def test_new_groups_read_nothing_after_candle_i(self):
        self.check(walk(200, 7), 150)
        self.check(walk(400, 9, minutes=240, start=START), 300)


class SigmaSeasTests(unittest.TestCase):
    def hourly(self, days, amplitude, valid=None):
        n = days * 24
        o = [100 * S] * n
        c = [int(100 * S * (1 + 0.001 * amplitude(k % 24))) for k in range(n)]
        return series(60, START, o, c, o, c, valid=valid)

    @staticmethod
    def brute(candles, sigma, i):
        step = candles.minutes * MIN
        end = candles.end_ms[i]
        obs = []
        for k in range(i):  # rows k <= i - 1
            j = k + 1
            if candles.valid[k] and candles.valid[j] and sigma[k] and end - 60 * DAY < candles.end_ms[j] <= end:
                value = abs(log(candles.close[j] / candles.open[j])) / sigma[k]
                obs.append(((candles.open_ms(j) % DAY) // step, value))
        slot = ((candles.open_ms(i) + step) % DAY) // step
        mine = [v for s, v in obs if s == slot]
        if len(mine) < 30:
            return None
        return (sum(mine) / len(mine)) / (sum(v for _, v in obs) / len(obs))

    def test_flat_is_one(self):
        candles = self.hourly(70, lambda slot: 1)
        out = cx.seasonal_factors(candles, [0.01] * len(candles))
        self.assertIsNone(out[29 * 24])  # fewer than 30 observations of the slot
        for i in (31 * 24, 50 * 24 + 7, len(candles) - 2):
            self.assertAlmostEqual(out[i], 1.0)

    def test_seasonal_pattern_and_brute_force(self):
        candles = self.hourly(70, lambda slot: 3 if slot == 13 else 1)
        sigma = [0.01] * len(candles)
        out = cx.seasonal_factors(candles, sigma)
        i = 65 * 24 + 12  # candle i+1 opens at 13:00
        self.assertAlmostEqual(out[i], log(1.003) / ((23 * log(1.001) + log(1.003)) / 24), places=6)
        self.assertGreater(out[i], 2.5)
        self.assertLess(out[i + 1], 1.0)
        rng = random.Random(5)
        valid = [rng.random() > 0.02 for _ in range(70 * 24)]
        noisy = self.hourly(70, lambda slot: 1 + (slot % 5), valid)
        noisy_sigma = [0.01 * (1 + (k % 7) / 10) for k in range(len(noisy))]
        out = cx.seasonal_factors(noisy, noisy_sigma)
        for i in (30 * 24 + 3, 61 * 24 + 11, 69 * 24 + 22):
            expected = self.brute(noisy, noisy_sigma, i)
            if expected is None:
                self.assertIsNone(out[i])
            else:
                self.assertAlmostEqual(out[i], expected)

    def test_factor_reads_nothing_after_candle_i(self):
        candles = self.hourly(40, lambda slot: 1 + slot % 3)
        sigma = [0.01] * len(candles)
        i = 35 * 24
        later = [v if j <= i else v * 2 for j, v in enumerate(candles.close)]
        changed = series(60, START, candles.open, later, candles.open, later)
        self.assertEqual(cx.seasonal_factors(candles, sigma)[:i + 1], cx.seasonal_factors(changed, sigma)[:i + 1])

    def test_columns(self):
        candles = self.hourly(40, lambda slot: 2 if slot == 5 else 1)
        table = rows(context(candles))
        row = next(r for r in table if r["open_ms"] == START + (35 * 24 + 4) * 60 * MIN)
        self.assertGreater(row["sigma_seas_factor"], 1.5)
        self.assertAlmostEqual(row["ret_1_sigma_seas"], row["ret_1_sigma"] / row["sigma_seas_factor"])
        self.assertAlmostEqual(row["ln_range1_sigma_seas"], log(row["range1_sigma"] / row["sigma_seas_factor"]))
        self.assertIsNone(table[-1]["ret_1_sigma_seas"])  # no next candle: like ret_1_sigma
        daily = rows(context(walk(40, 1)))[-1]
        self.assertTrue(all(daily[name] is None for name, _ in cx.SIGMA_SEAS_COLUMNS))
        self.assertEqual(daily["sigma_seas_missing"], "")  # not applicable on daily rows, not missing


class RegistryTests(unittest.TestCase):
    def test_versions(self):
        versions = ct.REGISTRY.versions()
        self.assertEqual((versions["prev"], versions["ctx2"], versions["lv3"], versions["sigma_seas"]),
                         ("prev-v1", "ctx2-v1", "lv3-v1", "ss-v1"))
        self.assertEqual((versions["geometry"], versions["trend_vol"], versions["location"]),
                         ("geo-v1", "tv-v2", "loc-v1"))
        for group in ("prev", "ctx2", "lv3", "sigma_seas"):
            self.assertNotIn(group, ct.FEATURE_GROUPS_FOR_STATUS)


if __name__ == "__main__":
    unittest.main()
