"""candle-table: bar_source, development-ext segments, 2020 daily start, daily universe and the study mapping."""
import io
import random
import unittest
from unittest import mock

from market_analysis import daily_lake as dk
from market_analysis import data_lake
from market_analysis.benchmark.candles import CandleSeries
from market_analysis.patterns import candle_table as ct
from market_analysis.patterns import pattern_study as ps

S = 10 ** 8
MIN15 = 15 * 60_000
DAY = 86_400_000
RD_START = data_lake.month_bounds_ms("2024-01")[0]
COLUMNS = ct.REGISTRY.columns()


def walk(count, minutes, start, seed=1, symbol="BTCUSDT"):
    rng = random.Random(seed)
    price, o, h, l, c = 100 * S, [], [], [], []
    for _ in range(count):
        close = max(S, int(price * (1 + rng.gauss(0, 0.01))))
        o.append(price)
        c.append(close)
        h.append(max(price, close) + rng.randint(1, S // 2))
        l.append(min(price, close) - rng.randint(1, S // 2))
        price = close
    return CandleSeries(symbol, minutes, start, open=o, high=h, low=l, close=c, valid=[True] * count,
                        volume=[100 * S] * count, taker_buy_volume=[50 * S] * count)


def rows(candles):
    ctx = ct.TableContext(candles.symbol, candles.minutes, candles, [0.01] * len(candles), "test",
                          ct.table_segments(candles.minutes), None)
    return [dict(zip(COLUMNS, values)) for values in ct.build_rows(ctx)]


class SegmentTests(unittest.TestCase):
    def test_intraday_segments(self):
        segments = ct.table_segments(15)
        self.assertEqual([name for name, _, _ in segments], ["development-ext", "development", "validation"])
        self.assertEqual(segments[0][1:], (data_lake.month_bounds_ms("2021-01")[0], RD_START))
        self.assertEqual(segments[1][1], RD_START)
        hidden = data_lake.month_bounds_ms("2026-01")[0]
        self.assertTrue(all(end <= hidden for _, _, end in segments))

    def test_daily_table_starts_2020_and_other_daily_code_is_unchanged(self):
        self.assertEqual(ct.table_segments(1440)[0][:2], ("development-ext", data_lake.month_bounds_ms("2020-01")[0]))
        from market_analysis.benchmark.segments import DAILY_SEGMENTS, SEGMENTS
        self.assertEqual(DAILY_SEGMENTS["development-ext"], ("2021-01", "2025-07"))
        self.assertNotIn("development-ext", SEGMENTS)  # intraday paths outside the table never accept it
        self.assertEqual((ct.INTRADAY_FIRST_MONTH, ct.INTRADAY_BARS_FIRST_MONTH, ct.DAILY_FIRST_MONTH),
                         ("2021-01", "2020-12", "2020-01"))


class BarSourceTests(unittest.TestCase):
    def test_kline_before_2024_and_aggtrade_after(self):
        start = RD_START - 16 * MIN15
        table = rows(walk(32, 15, start))
        before = [r for r in table if r["open_ms"] < RD_START - MIN15]
        after = [r for r in table if r["open_ms"] >= RD_START]
        self.assertEqual({(r["bar_source"], r["segment"]) for r in before}, {("kline", "development-ext")})
        self.assertEqual({(r["bar_source"], r["segment"]) for r in after}, {("aggtrade", "development")})
        # the 23:45 candle is built from 2023 (kline) minutes; its decision time 2024-01-01 00:00 is in development
        boundary = next(r for r in table if r["open_ms"] == RD_START - MIN15)
        self.assertEqual((boundary["bar_source"], boundary["segment"]), ("kline", "development"))
        self.assertEqual(ct.REGISTRY.versions()["source"], "src-v1")

    def test_daily_rows_are_kline_and_counted(self):
        table = list(ct.build_rows(ct.TableContext("BTCUSDT", 1440, walk(60, 1440, data_lake.month_bounds_ms(
            "2020-01")[0]), [0.01] * 60, "test", ct.table_segments(1440), None)))
        where = COLUMNS.index("bar_source")
        self.assertEqual({row[where] for row in table}, {"kline"})
        counts = {}
        ct.write_table(io.BytesIO(), iter(table), counts=counts)
        self.assertEqual(counts["bar_source"], {"BTCUSDT|1440|kline": 60})
        manifest = ct.build_manifest(files=[], counts=counts, inputs={}, through_month="2025-12", parity={},
                                     symbols=["BTCUSDT"], timeframes=[1440], code_commit="a", created_utc="b")
        self.assertEqual(manifest["bar_source_counts"], {"BTCUSDT|1440|kline": 60})
        self.assertEqual(manifest["first_ok_decision_ms"], {})  # no regime join here: every row is insufficient
        ct.check_manifest(manifest)

    def test_first_ok_is_the_earliest_ok_decision(self):
        where = {"symbol": 0, "tf": 1, "status": 2, "segment": 3, "decision_ms": 4}
        counts = {}
        for row in (("BTCUSDT", 1440, "insufficient", "development-ext", 1 * DAY),
                    ("BTCUSDT", 1440, "ok", "development-ext", 5 * DAY),
                    ("BTCUSDT", 1440, "ok", "development-ext", 3 * DAY),
                    ("BTCUSDT", 1440, "ok", "validation", 9 * DAY), ("ETHUSDT", 1440, "invalid", "development-ext", 0)):
            ct.count_row(counts, row, where)
        self.assertEqual(counts["first_ok"], {"BTCUSDT|1440|ok|development-ext": 3 * DAY,
                                              "BTCUSDT|1440|ok|validation": 9 * DAY})


class UniverseTests(unittest.TestCase):
    def test_empty_extras_keep_the_six(self):
        self.assertEqual(data_lake.DAILY_EXTRA_SYMBOLS, ())
        self.assertEqual(data_lake.DAILY_UNIVERSE, data_lake.SYMBOLS)
        with self.assertRaises(ValueError):
            data_lake.validate_daily_symbol("ADAUSDT")
        with self.assertRaises(ValueError):
            dk.release_tag("ADAUSDT", "2020-01", "2026-09")
        self.assertEqual(dk.release_tag("BTCUSDT", "2020-01", "2026-09"), "dk1-BTCUSDT-2020-01_2026-09-r1")
        self.assertEqual(dk.funding_url("BTCUSDT", "2021-01"),
                         data_lake.source_url("fundingRate", "BTCUSDT", "2021-01"))
        manifest = ct.build_manifest(files=[], counts={}, inputs={}, through_month="2025-12", parity={},
                                     symbols=list(data_lake.SYMBOLS), timeframes=[1440], code_commit="a",
                                     created_utc="b")
        self.assertEqual(set(manifest["universe"].values()), {"core"})

    def test_an_extra_symbol_is_daily_only_and_flagged(self):
        universe = (*data_lake.SYMBOLS, "ADAUSDT")
        with mock.patch.object(data_lake, "DAILY_UNIVERSE", universe):
            self.assertEqual(data_lake.universe_flag("ADAUSDT"), "extra")
            self.assertTrue(ct.file_name("ADAUSDT", 1440).startswith("candles__ADAUSDT__1440__2020-01_"))
            with self.assertRaises(ValueError):
                ct.file_name("ADAUSDT", 15)
            manifest = ct.build_manifest(files=[], counts={}, inputs={}, through_month="2025-12", parity={},
                                         symbols=["BTCUSDT", "ADAUSDT"], timeframes=[1440], code_commit="a",
                                         created_utc="b")
            self.assertEqual(manifest["universe"], {"BTCUSDT": "core", "ADAUSDT": "extra"})
        table = rows(walk(40, 1440, data_lake.month_bounds_ms("2021-01")[0], symbol="ADAUSDT"))
        self.assertTrue(all(row["touch_round"] is None for row in table))  # no fixed grid: unavailable


class StudyMappingTests(unittest.TestCase):
    def test_development_reads_development_ext_too(self):
        self.assertEqual(ps.STUDY_SEGMENTS["development"]["intraday"], ("development-ext", "development"))
        self.assertEqual(ps.STUDY_SEGMENTS["validation"]["intraday"], ("validation",))
        self.assertEqual(ps.UNIVERSES, ("core", "core+extra"))


if __name__ == "__main__":
    unittest.main()
