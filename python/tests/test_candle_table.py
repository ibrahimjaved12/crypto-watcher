"""candle-table-v1: hand-computed geometry, point-in-time guards, outcomes, sigma, joins and writer (small data)."""
from array import array
import gzip
import io
from math import log, sqrt
import random
import unittest

from market_analysis import data_lake
from market_analysis.benchmark.bars import MISSING
from market_analysis.benchmark.candles import CandleSeries
from market_analysis.benchmark.hidden_guard import HiddenStretchLocked
from market_analysis.daily_lake import DAY_MS, DailySeries
from market_analysis.patterns import candle_table as ct
from market_analysis.patterns.daily_candles import candles_from_daily

S = 10 ** 8
MIN15 = 15 * 60_000
START = data_lake.month_bounds_ms("2024-01")[0]  # 2024-01-01 00:00 UTC, a Monday
DAILY_START = data_lake.month_bounds_ms("2021-01")[0]
COLUMNS = ct.REGISTRY.columns()


def series(minutes, start, o, h, l, c, *, volume=None, taker=None, valid=None, symbol="BTCUSDT"):
    count = len(o)
    valid = list(valid) if valid is not None else [True] * count
    blank = lambda column: [value if ok else MISSING for value, ok in zip(column, valid)]  # noqa: E731
    return CandleSeries(symbol, minutes, start, open=blank(o), high=blank(h), low=blank(l), close=blank(c),
                        valid=valid, volume=blank(volume or [100 * S] * count),
                        taker_buy_volume=blank(taker or [50 * S] * count))


def walk(count, seed, minutes=1440, start=DAILY_START):
    rng = random.Random(seed)
    price, o, h, l, c = 100 * S, [], [], [], []
    for _ in range(count):
        close = max(S, int(price * (1 + rng.gauss(0, 0.01))))
        o.append(price)
        c.append(close)
        h.append(max(price, close) + rng.randint(1, S // 2))
        l.append(min(price, close) - rng.randint(1, S // 2))
        price = close
    volume = [rng.randint(50, 150) * S for _ in range(count)]
    taker = [v * rng.randint(30, 70) // 100 for v in volume]
    return series(minutes, start, o, h, l, c, volume=volume, taker=taker)


def context(candles, sigma=None, regime=None):
    sigma = [0.01] * len(candles) if sigma is None else sigma
    return ct.TableContext(candles.symbol, candles.minutes, candles, sigma, "test", ct.table_segments(candles.minutes),
                           regime)


def rows_by_open(ctx):
    return {row["open_ms"]: row for row in (dict(zip(COLUMNS, values)) for values in ct.build_rows(ctx))}


def flat_then(last, count=15):
    """``count`` candles high 110 low 100 open=close 105 (TR 10, so ATR14 through candle 14 = 10), then ``last``."""
    o = [105 * S] * count + [last[0]]
    h = [110 * S] * count + [last[1]]
    l = [100 * S] * count + [last[2]]
    c = [105 * S] * count + [last[3]]
    return rows_by_open(context(series(15, START, o, h, l, c)))[START + count * MIN15]


class GeometryTests(unittest.TestCase):
    def test_body_ratio_flag_boundaries(self):
        base = 100 * S
        row = flat_then((base, base + 10 * S, base, base + S // 2))  # body 0.5 of range 10 = 0.05
        self.assertEqual(row["atr_prev"], 10.0)
        self.assertEqual(row["body_ratio"], 0.05)
        self.assertIs(row["doji_b05"], True)
        self.assertIs(row["doji_b10"], True)
        row = flat_then((base, base + 10 * S, base, base + S // 2 + 1))
        self.assertIs(row["doji_b05"], False)
        self.assertIs(row["doji_b10"], True)
        row = flat_then((base, base + 10 * S, base, base + S))  # exactly 0.10
        self.assertIs(row["doji_b10"], True)
        row = flat_then((base, base + 10 * S, base, base + S + 1))
        self.assertIs(row["doji_b10"], False)
        self.assertIsNone(row["doji_subtype"])  # not applicable outside doji_b10
        self.assertNotIn("doji_subtype", row["geometry_missing"].split("|"))
        self.assertEqual(row["color"], 1)

    def test_size_flag_boundaries(self):
        base = 100 * S
        row = flat_then((base, base + 5 * S, base, base + 5 * S))  # range_atr exactly 0.5
        self.assertEqual(row["range_atr"], 0.5)
        self.assertEqual((row["size_ge_05"], row["size_ge_10"]), (True, False))
        row = flat_then((base, base + 5 * S - 1, base, base + S))
        self.assertEqual((row["size_ge_05"], row["size_ge_10"]), (False, False))
        row = flat_then((base + 10 * S, base + 10 * S, base, base))  # range_atr exactly 1.0, red
        self.assertEqual((row["range_atr"], row["size_ge_10"], row["color"]), (1.0, True, -1))
        self.assertEqual(row["body_atr"], 1.0)

    def test_doji_subtypes(self):
        low, high = 100 * S, 110 * S
        cases = {"dragonfly": 109 * S + S // 2, "gravestone": 100 * S + S // 2, "long_legged": 105 * S,
                 "standard": 108 * S + S // 2}
        for subtype, price in cases.items():
            row = flat_then((price, high, low, price))
            self.assertEqual(row["doji_subtype"], subtype)
            self.assertEqual(row["color"], 0)

    def test_invalid_candle_fills_nothing(self):
        candles = walk(30, 1, minutes=15, start=START)
        valid = list(candles.valid)
        valid[20] = False
        broken = series(15, START, candles.open, candles.high, candles.low, candles.close, valid=valid)
        row = rows_by_open(context(broken))[START + 20 * MIN15]
        self.assertEqual((row["status"], row["reasons"]), ("invalid", "invalid_candle"))
        self.assertEqual(row["bar_source"], "aggtrade")  # provenance is filled even for an invalid candle
        self.assertTrue(all(row[name] is None for name in COLUMNS[8:]))


class LookAheadTests(unittest.TestCase):
    def test_prev_columns_ignore_candle_i_and_later(self):
        count, i = 260, 200
        candles = walk(count, 7)
        sigma = [0.02 + 0.0001 * j for j in range(count)]
        base = rows_by_open(context(candles, sigma))[candles.open_ms(i)]
        self.assertIsNotNone(base["atr_pct_prev"])
        self.assertIsNotNone(base["bb_width_pct_prev"])
        self.assertIsNotNone(base["ret3_prev_sigma"])
        scale = lambda column: [v if j < i else v * 13 // 10 for j, v in enumerate(column)]  # noqa: E731
        changed = series(1440, DAILY_START, scale(candles.open), scale(candles.high), scale(candles.low),
                         scale(candles.close), volume=scale(candles.volume), taker=candles.taker_buy_volume)
        changed_sigma = [s if j < i else 9.0 for j, s in enumerate(sigma)]
        row = rows_by_open(context(changed, changed_sigma))[candles.open_ms(i)]
        prev = [name for name, _ in (*ct.GEOMETRY_COLUMNS, *ct.TREND_VOL_COLUMNS) if "_prev" in name]
        self.assertEqual(len(prev), 15)
        prev += ["trend_dir", "taker_ratio_prior20"]  # also read candles through i - 1 only
        self.assertEqual({name: row[name] for name in prev}, {name: base[name] for name in prev})
        self.assertNotEqual(row["close"], base["close"])

    def test_outcomes_ignore_candles_after_window(self):
        count, i = 120, 60
        candles = walk(count, 11)
        base = rows_by_open(context(candles))[candles.open_ms(i)]
        cut = i + max(ct.FORWARD_CANDLES)
        scale = lambda column: [v if j <= cut else v * 2 for j, v in enumerate(column)]  # noqa: E731
        changed = series(1440, DAILY_START, scale(candles.open), scale(candles.high), scale(candles.low),
                         scale(candles.close))
        row = rows_by_open(context(changed))[candles.open_ms(i)]
        outcome = [name for name, _ in ct.OUTCOME_COLUMNS]
        self.assertEqual(base["fwd_status"], "ok")
        self.assertEqual({name: row[name] for name in outcome}, {name: base[name] for name in outcome})


class OutcomeTests(unittest.TestCase):
    first = data_lake.month_bounds_ms("2025-07")[0] - 16 * MIN15  # 2025-06-30 20:00, development

    def candles(self, valid=None):
        count = 24
        o = [(100 + j) * S for j in range(count)]
        c = [value + S // 2 for value in o]
        h = [value + S for value in o]
        l = [value - S for value in o]
        return series(15, self.first, o, h, l, c, valid=valid)

    def test_returns_and_excursions(self):
        candles = self.candles()
        row = rows_by_open(context(candles, [0.01] * 24))[candles.open_ms(4)]
        entry = candles.open[5]
        self.assertAlmostEqual(row["ret_1_bp"], 1e4 * log(candles.close[5] / entry))
        self.assertAlmostEqual(row["ret_2_sigma"], log(candles.close[6] / entry) / 0.01)
        self.assertAlmostEqual(row["mfe_long_4_sigma"], log(candles.high[8] / entry) / 0.01)
        self.assertAlmostEqual(row["mae_long_4_sigma"], log(entry / candles.low[5]) / 0.01)
        self.assertAlmostEqual(row["range1_sigma"], log(candles.high[5] / candles.low[5]) / 0.01)
        self.assertAlmostEqual(row["absret1_sigma"], abs(log(candles.close[5] / entry)) / 0.01)
        self.assertAlmostEqual(row["ln_range1_sigma"], log(row["range1_sigma"]))
        self.assertEqual((row["fwd_status"], row["fwd_ok_4"]), ("ok", True))

    def test_segment_end_is_never_crossed(self):
        candles = self.candles()
        rows = rows_by_open(context(candles))
        last_dev = rows[candles.open_ms(14)]  # opens 23:30, decides 23:45 (development)
        self.assertEqual(last_dev["segment"], "development")
        self.assertEqual((last_dev["fwd_ok_1"], last_dev["fwd_ok_2"]), (True, False))
        self.assertIsNone(last_dev["ret_2_bp"])
        boundary = rows[candles.open_ms(15)]  # decides at 2025-07-01 00:00: validation, its window is too
        self.assertEqual((boundary["segment"], boundary["fwd_status"]), ("validation", "ok"))
        edge = rows[candles.open_ms(13)]
        self.assertEqual((edge["fwd_ok_2"], edge["fwd_ok_4"]), (True, False))
        later = [v if j <= 15 else v * 3 for j, v in enumerate(candles.close)]
        changed = series(15, self.first, candles.open, [max(a, b) for a, b in zip(candles.high, later)],
                         candles.low, later)
        again = rows_by_open(context(changed))[candles.open_ms(14)]
        outcome = [name for name, _ in ct.OUTCOME_COLUMNS]
        self.assertEqual({n: again[n] for n in outcome}, {n: last_dev[n] for n in outcome})

    def test_invalid_window_and_missing_sigma(self):
        valid = [True] * 24
        valid[7] = False
        candles = self.candles(valid)
        rows = rows_by_open(context(candles))
        row = rows[candles.open_ms(5)]  # window i+1..i+2 contains candle 7
        self.assertEqual((row["fwd_status"], row["fwd_ok_1"], row["fwd_ok_2"]), ("ok", True, False))
        self.assertEqual(rows[candles.open_ms(6)]["fwd_status"], "window_invalid")
        sigma = [0.01] * 24
        sigma[3] = None
        row = rows_by_open(context(self.candles(), sigma))[candles.open_ms(3)]
        self.assertEqual((row["fwd_status"], row["fwd_ok_1"]), ("sigma_missing", False))
        self.assertIsNotNone(row["ret_1_bp"])
        self.assertIsNone(row["ret_1_sigma"])


class SigmaTests(unittest.TestCase):
    def days(self, count, closes=None):
        closes = closes or [(110 if j % 2 else 100) * S for j in range(count)]
        daily = DailySeries("BTCUSDT", START, count, open=array("q", closes), high=array("q", closes),
                            low=array("q", closes), close=array("q", closes), volume=array("q", [S] * count),
                            taker_buy_volume=array("q", [S] * count))
        return daily, candles_from_daily(daily, 1440)

    def test_daily_sigma_warm_up_value_and_point_in_time(self):
        _, days = self.days(42)
        sigma = ct.daily_sigma(days)
        self.assertEqual(sigma[:30], [None] * 30)  # day j has j returns; published at 30
        self.assertAlmostEqual(sigma[30], log(1.1), places=12)  # constant |r| => EWMA = r^2 exactly
        closes = list(days.close)
        closes[35] = 200 * S
        _, moved = self.days(42, closes)
        changed = ct.daily_sigma(moved)
        self.assertEqual(changed[:35], sigma[:35])
        self.assertNotEqual(changed[35], sigma[35])

    def test_weekly_sigma_is_sunday_daily_sigma_times_sqrt7(self):
        daily, days = self.days(7 * 7)
        weeks = candles_from_daily(daily, 10080)
        sigma_days = ct.daily_sigma(days)
        weekly = ct.weekly_sigma(days, weeks, sigma_days)
        self.assertEqual(len(weekly), 7)
        for k in range(7):
            expected = sigma_days[7 * k + 6]
            self.assertEqual(weekly[k], None if expected is None else expected * sqrt(7))
        self.assertIsNone(weekly[3])  # Sunday of week 3 = day 27 < 30 returns
        self.assertIsNotNone(weekly[4])


class JoinAndVolumeTests(unittest.TestCase):
    def test_regime_join_at_midnight(self):
        join = ct.RegimeJoin(START, 3, {"mkt_t1": ["+1", "-1", ""]}, {"BTCUSDT": {"sym_t1": ["-1", "+1", "+1"]}})
        self.assertEqual(join.row("BTCUSDT", START + DAY_MS - 1), {"rg_mkt_t1": "+1", "rg_sym_t1": "-1"})
        self.assertEqual(join.row("BTCUSDT", START + DAY_MS), {"rg_mkt_t1": "-1", "rg_sym_t1": "+1"})
        self.assertIsNone(join.row("BTCUSDT", START + 3 * DAY_MS))
        self.assertIsNone(join.row("ETHUSDT", START))
        flat = [100 * S] * 3
        days = series(1440, START, flat, [101 * S] * 3, [99 * S] * 3, flat)
        rows = rows_by_open(context(days, regime=join))
        self.assertEqual((rows[START]["rg_mkt_t1"], rows[START]["rg_sym_t1"]), ("-1", "+1"))  # decision = day 1
        self.assertEqual(rows[START + DAY_MS]["rg_mkt_t1"], None)  # empty label stays empty
        self.assertIn("rg_mkt_t1", rows[START + DAY_MS]["regime_missing"].split("|"))

    def test_slot_volume(self):
        per_day = 96
        count = 20 * per_day + 1
        volume = [(j // per_day + 1) * S for j in range(count)]  # day d -> d + 1
        flat = [100 * S] * count
        candles = series(15, START, flat, [101 * S] * count, [99 * S] * count, flat, volume=volume)
        row = rows_by_open(context(candles))[candles.open_ms(20 * per_day)]
        self.assertEqual(row["rel_volume_slot"], 21 / 10.5)  # median of 1..20
        valid = [not (j % per_day == 0 and j < 11 * per_day) for j in range(count)]  # only 9 prior values left
        sparse = series(15, START, flat, [101 * S] * count, [99 * S] * count, flat, volume=volume, valid=valid)
        row = rows_by_open(context(sparse))[candles.open_ms(20 * per_day)]
        self.assertIsNone(row["rel_volume_slot"])
        self.assertIn("rel_volume_slot", row["volume_missing"].split("|"))

    def test_squeeze_counting(self):
        count = 45
        o = [100 * S] * count
        c = list(o)
        h = [101 * S] * count
        l = [99 * S] * count
        c[40], h[40] = 200 * S, 200 * S  # a jump: Bollinger width far outside Keltner
        rows = rows_by_open(context(series(1440, DAILY_START, o, h, l, c)))
        at = lambda i: rows[DAILY_START + i * DAY_MS]  # noqa: E731
        self.assertIsNone(at(20)["squeeze_on_prev"])  # ATR20 through candle 19 is not ready
        self.assertEqual((at(21)["squeeze_on_prev"], at(21)["squeeze_bars_prev"]), (True, 1))
        self.assertEqual((at(23)["squeeze_on_prev"], at(23)["squeeze_bars_prev"]), (True, 3))
        self.assertEqual((at(41)["squeeze_on_prev"], at(41)["squeeze_bars_prev"]), (False, 0))

    def test_calendar_intraday(self):
        candles = walk(40, 3, minutes=15, start=START + 7 * 3_600_000 + 30 * 60_000)  # Monday 07:30
        rows = rows_by_open(context(candles))
        row = rows[candles.open_ms(1)]  # 07:45 -> decision 08:00 (a settlement)
        self.assertEqual((row["utc_dow"], row["utc_hour"], row["session_bin"]), (0, 7, "asia"))
        self.assertEqual((row["funding_in_candle"], row["mins_to_funding"]), (False, 0))
        row = rows[candles.open_ms(2)]  # 08:00 open contains the settlement
        self.assertEqual((row["funding_in_candle"], row["mins_to_funding"], row["session_bin"]), (True, 465, "europe"))
        self.assertIs(row["is_weekend"], False)


class WriterAndGuardTests(unittest.TestCase):
    def table(self):
        candles = walk(40, 5, minutes=15, start=START)
        counts = {}
        buffer = io.BytesIO()
        written = ct.write_table(buffer, ct.build_rows(context(candles)), counts=counts)
        return buffer.getvalue(), written, counts

    def test_writer_is_deterministic(self):
        first, written, counts = self.table()
        second, _, _ = self.table()
        self.assertEqual(first, second)
        lines = gzip.decompress(first).decode("ascii").splitlines()
        self.assertEqual(lines[0], ",".join(COLUMNS))
        self.assertEqual(written, len(lines) - 1)
        self.assertEqual(written, 40)
        self.assertEqual(sum(counts["rows"].values()), 40)
        self.assertEqual(ct.cell(True), "1")
        self.assertEqual(ct.cell(None), "")
        self.assertEqual(ct.cell(0.1), "0.1")
        self.assertEqual(ct.cell(float("nan")), "")
        with self.assertRaises(ct.CandleTableError):
            ct.cell("a,b")

    def test_manifest_check(self):
        data, written, counts = self.table()
        files = [{"name": ct.file_name("BTCUSDT", 15), "rows": written, "sha256": "0" * 64}]
        common = dict(files=files, counts=counts, inputs={"dk1": {}}, through_month="2025-12",
                      parity={"BTCUSDT": {"days_compared": 0}}, symbols=["BTCUSDT"], timeframes=[15])
        manifest = ct.build_manifest(code_commit="a", created_utc="2026-10-15T00:00:00Z", **common)
        ct.check_manifest(manifest)
        other = ct.build_manifest(code_commit="b", created_utc="2026-10-16T00:00:00Z", **common)
        self.assertEqual(manifest["content_hash"], other["content_hash"])
        self.assertEqual(ct.report_dir(manifest), f"reports/candle-table/ct1__{manifest['content_hash'][:16]}")
        self.assertEqual(files[0]["name"], "candles__BTCUSDT__15__2021-01_2025-12.csv.gz")
        self.assertEqual(manifest["group_versions"]["geometry"], "geo-v1")
        self.assertEqual(manifest["group_versions"]["trend_vol"], "tv-v2")
        tampered = {**manifest, "through_month": "2025-11"}
        with self.assertRaises(ValueError):
            ct.check_manifest(tampered)
        self.assertTrue(ct.public_lines(manifest)[-1].endswith(manifest["content_hash"]))

    def test_hidden_months_refused(self):
        with self.assertRaises(HiddenStretchLocked):
            ct.guard_months("2026-01")
        with self.assertRaises(HiddenStretchLocked):  # before any file is opened
            ct.load_daily_inputs("/nonexistent", ["BTCUSDT"], "2026-03")
        with self.assertRaises(ct.CandleTableError):
            ct.check_through_month("2026-01")
        self.assertEqual(ct.check_through_month("2025-12"), "2025-12")
        hidden = data_lake.month_bounds_ms("2026-01")[0]
        for minutes in ct.TABLE_TIMEFRAMES:
            self.assertTrue(all(end <= hidden for _, _, end in ct.table_segments(minutes)))
        self.assertEqual(ct.table_segments(1440)[0][:2], ("development-ext", data_lake.month_bounds_ms("2020-01")[0]))

    def test_duplicate_columns_rejected(self):
        registry = ct.default_registry()
        with self.assertRaises(ct.CandleTableError):
            registry.register(ct.Group("extras", "ex-v1", (("close", "a duplicate"),), lambda ctx, i: {}))
        with self.assertRaises(ct.CandleTableError):
            registry.register(ct.Group("extras", "ex-v1", (("a", "x"), ("a", "y")), lambda ctx, i: {}))
        with self.assertRaises(ct.CandleTableError):
            registry.register(ct.Group("other", "o-v1", (("geometry_ok", "clash with a flag"),), lambda ctx, i: {}))
        with self.assertRaises(ct.CandleTableError):
            registry.register(ct.Group("geometry", "geo-v2", (("new_col", "doc"),), lambda ctx, i: {}))
        registry.register(ct.Group("extras", "ex-v1", (("dist_prev_high", "doc"),), lambda ctx, i: {}))
        self.assertIn("extras_ok", registry.columns())
        self.assertEqual(len(set(COLUMNS)), len(COLUMNS))


if __name__ == "__main__":
    unittest.main()
