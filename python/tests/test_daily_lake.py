"""Daily bars and funding history dk1 (#222/#223): parsing, coverage, outputs, loaders, guard (synthetic zips)."""
from __future__ import annotations

import gzip
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
import zipfile

from market_analysis import daily_lake as dk
from market_analysis import data_lake
from market_analysis import metrics_lake
from market_analysis.benchmark.bars import MISSING
from market_analysis.benchmark.hidden_guard import HiddenStretchLocked

DAY = 86_400_000
MARCH = data_lake.month_bounds_ms("2024-03")[0]
APRIL = data_lake.month_bounds_ms("2024-04")[0]
KLINE_HEADER = ("open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,"
                "taker_buy_quote_volume,ignore")


def kline(open_time, o="100", h="110", l="90", c="105", v="1000"):
    return f"{open_time},{o},{h},{l},{c},{v},{open_time + DAY - 1},105000.5,1234,500,52500.25,0"


def make_zip(lines, header=None, name="x.csv"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        member = zipfile.ZipInfo(name, date_time=(2024, 1, 1, 0, 0, 0))
        archive.writestr(member, "\n".join(([header] if header else []) + list(lines)) + "\n")
    return buffer.getvalue()


def month_klines(start, days, **values):
    return [kline(start + i * DAY, **values) for i in range(days)]


class ParseTests(unittest.TestCase):
    def test_with_and_without_header(self):
        plain, stats = dk.parse_kline_zip(make_zip(month_klines(MARCH, 31)), "2024-03", "a.zip")
        headed, _ = dk.parse_kline_zip(make_zip(month_klines(MARCH, 31), KLINE_HEADER), "2024-03", "b.zip")
        self.assertEqual(plain, headed)
        self.assertEqual(stats, {"rows": 31, "rows_outside_month": 0})
        self.assertEqual(plain[0].values, ("100", "110", "90", "105", "1000", "105000.5", "1234", "500", "52500.25"))

    def test_misaligned_open_time_names_file_and_line(self):
        lines = month_klines(MARCH, 3)
        lines[1] = kline(MARCH + DAY + 60_000)
        with self.assertRaises(dk.DailyFormatError) as caught:
            dk.parse_kline_zip(make_zip(lines, KLINE_HEADER), "2024-03", "BTCUSDT-1d-2024-03.zip")
        self.assertIn("BTCUSDT-1d-2024-03.zip line 3", str(caught.exception))
        with self.assertRaises(dk.DailyFormatError):
            dk.parse_kline_zip(make_zip([kline(MARCH, o="abc")]), "2024-03", "c.zip")

    def test_outside_month_dropped_and_duplicates_counted(self):
        rows, stats = dk.parse_kline_zip(make_zip([kline(MARCH - DAY), *month_klines(MARCH, 2), kline(APRIL)]),
                                         "2024-03", "d.zip")
        self.assertEqual(stats, {"rows": 2, "rows_outside_month": 2})
        again, _ = dk.parse_kline_zip(make_zip([kline(MARCH, c="106"), kline(MARCH + DAY)]), "2024-03", "e.zip")
        merged, duplicates, conflicts = dk.merge_daily(rows + again)
        self.assertEqual((len(merged), duplicates, conflicts), (2, 2, 1))
        self.assertEqual(merged[0].values[3], "105")  # first row wins

    def test_coverage_gap_and_sanity_counters(self):
        lines = [kline(MARCH), kline(MARCH + 2 * DAY, h="80", l="90"), kline(MARCH + 3 * DAY, c="200"),
                 kline(MARCH + 4 * DAY, v="0", o="0", h="160", c="150"), kline(MARCH + 5 * DAY, c="1.05E2")]
        rows, _ = dk.parse_kline_zip(make_zip(lines), "2024-03", "f.zip")
        stats = dk.coverage(rows, duplicates=0, conflicts=0, rows_outside_month=0,
                            funding_times=[MARCH, MARCH + 8 * 3_600_000, MARCH + 24 * 3_600_000],
                            funding_intervals=[8, 8, 8])
        self.assertEqual((stats["first_day"], stats["last_day"]), ("2024-03-01", "2024-03-06"))
        self.assertEqual((stats["days_expected"], stats["days_present"]), (6, 5))
        self.assertEqual(stats["missing_days"], ["2024-03-02"])
        self.assertEqual(stats["high_below_low"], 1)
        self.assertEqual(stats["close_outside_low_high"], 2)  # 105 above high 80; 200 above high 110
        self.assertEqual((stats["nonpositive_prices"], stats["zero_volume_days"]), (1, 1))
        self.assertEqual(stats["max_abs_daily_log_return_day"], "2024-03-04")  # 105 -> 200 is the largest move
        self.assertEqual((stats["funding_settlements"], stats["funding_gaps_over_9h"]), (3, 1))
        self.assertEqual((stats["funding_interval_hours"], stats["funding_max_gap_hours"]), (["8"], "16"))

    def test_funding_months_merged_under_one_header(self):
        header = "calc_time,funding_interval_hours,last_funding_rate"
        march = make_zip([f"{MARCH + i * 8 * 3_600_000},8,0.0001" for i in range(3)], header)
        april = make_zip([f"{APRIL + i * 8 * 3_600_000},8,9.8E-7" for i in range(2)], header)
        lines = dk.funding_month_lines(march, "2024-03", "m.zip")[0] + dk.funding_month_lines(april, "2024-04",
                                                                                              "a.zip")[0]
        buffer = io.BytesIO()
        self.assertEqual(dk.write_funding_range_csv_gz(buffer, lines), 5)
        text = gzip.decompress(buffer.getvalue()).decode().splitlines()
        self.assertEqual(text[0], ",".join(data_lake.FUNDING_COLUMNS))
        self.assertEqual(sum(1 for line in text if line.startswith("calc_time")), 1)
        self.assertEqual(text[-1], f"{APRIL + 8 * 3_600_000},8,0.00000098,9.8E-7")

    def test_tags(self):
        self.assertEqual(dk.release_tag("BTCUSDT", "2020-01", "2026-09"), "dk1-BTCUSDT-2020-01_2026-09-r1")
        for bad in ("dk1-BTCUSDT-2026-09_2020-01-r1", "dk1-ADAUSDT-2020-01_2026-09-r1", "dk1-BTCUSDT-2020-01-r1",
                    "rd-BTCUSDT-2024-03-r1"):
            with self.assertRaises(ValueError):
                dk.validate_tag(bad)
        with self.assertRaises(ValueError):
            data_lake.validate_tag("dk1-BTCUSDT-2020-01_2026-09-r1")  # the rd- tag rule is unchanged
        self.assertEqual(dk.kline_url("ETHUSDT", "2020-01"), f"{data_lake.BINANCE_BASE}/klines/ETHUSDT/1d/"
                                                             "ETHUSDT-1d-2020-01.zip")


class CollectTests(unittest.TestCase):
    def test_404_is_a_gap_and_a_mismatch_is_an_error(self):
        body = make_zip(month_klines(MARCH, 31))

        def fetcher(url, corrupt=False):
            if "fundingRate" in url:
                return None  # no funding archive: a gap
            if url.endswith(".CHECKSUM"):
                return f"{hashlib.sha256(b'x' if corrupt else body).hexdigest()}  f.zip\n".encode()
            return body

        sources = dk.collect("BTCUSDT", ["2024-03"], fetcher)
        self.assertIsNone(sources[("funding", "2024-03")])
        self.assertEqual(sources[("klines", "2024-03")]["file"], "BTCUSDT-1d-2024-03.zip")
        with self.assertRaises(dk.ChecksumMismatch):
            dk.collect("BTCUSDT", ["2024-03"], lambda url: fetcher(url, corrupt=True))


class OutputAndLoaderTests(unittest.TestCase):
    def write_files(self, directory, rows):
        with (Path(directory) / dk.daily_asset_name("BTCUSDT", "2024-03", "2024-04")).open("wb") as stream:
            dk.write_daily_csv_gz(stream, rows)
        lines = [f"{MARCH + 8 * 3_600_000},8,0.0001,0.0001\n", f"{APRIL + 16 * 3_600_000},8,-0.0002,-0.0002\n"]
        with (Path(directory) / dk.funding_asset_name("BTCUSDT", "2024-03", "2024-04")).open("wb") as stream:
            dk.write_funding_range_csv_gz(stream, lines)

    def test_gap_day_is_missing_and_series_starts_at_the_first_day(self):
        rows, _ = dk.parse_kline_zip(make_zip([kline(MARCH + 10 * DAY), kline(MARCH + 12 * DAY, c="107.5")]),
                                     "2024-03", "g.zip")
        with tempfile.TemporaryDirectory() as directory:
            self.write_files(directory, rows)
            series = dk.load_symbol_daily(directory, "BTCUSDT", "2024-03", "2024-04")
            self.assertEqual((series.start_ms, series.days), (MARCH + 10 * DAY, 3))
            self.assertEqual(series.close[1], MISSING)
            self.assertEqual(series.close[2], 10_750_000_000)
            self.assertEqual(series.end_ms, MARCH + 13 * DAY)
            self.assertEqual(series.open_time(2), MARCH + 12 * DAY)
            funding = dk.load_symbol_funding_range(directory, "BTCUSDT", "2024-04", "2024-04")
            self.assertEqual(funding.calc_time_ms, (APRIL + 16 * 3_600_000,))
            self.assertEqual((funding.start_ms, funding.end_ms), (APRIL, data_lake.month_bounds_ms("2024-04")[1]))

    def test_loader_refuses_hidden_months_without_a_token(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(HiddenStretchLocked):
                dk.load_symbol_daily(directory, "BTCUSDT", "2025-12", "2026-01")
            with self.assertRaises(HiddenStretchLocked):
                dk.load_symbol_funding_range(directory, "BTCUSDT", "2026-01", "2026-02")

    def test_csv_and_tar_are_reproducible(self):
        rows, _ = dk.parse_kline_zip(make_zip(month_klines(MARCH, 31)), "2024-03", "h.zip")
        outputs = []
        for _ in range(2):
            daily, tar = io.BytesIO(), io.BytesIO()
            dk.write_daily_csv_gz(daily, rows)
            metrics_lake.write_raw_tar(tar, {"b.zip": b"2", "a.zip": b"1"})
            outputs.append((daily.getvalue(), tar.getvalue()))
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(gzip.decompress(outputs[0][0]).decode().splitlines()[0], ",".join(dk.CSV_COLUMNS))


if __name__ == "__main__":
    unittest.main()
