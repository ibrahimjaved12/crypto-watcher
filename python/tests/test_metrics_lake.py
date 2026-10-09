"""Binance metrics archive phase 1 (#224): header-driven parsing, coverage, outputs, guard (synthetic zips)."""
from __future__ import annotations

import calendar
import hashlib
import importlib.util
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile

from market_analysis import data_lake
from market_analysis import metrics_lake as mx
from market_analysis.benchmark.hidden_guard import HiddenStretchLocked

DAY = "2024-03-01"
DAY_START = calendar.timegm(time.strptime(DAY, "%Y-%m-%d")) * 1000


def stamp_text(ms, kind="datetime"):
    if kind == "epoch_ms":
        return str(ms)
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ms // 1000))


def make_zip(day=DAY, rows=None, header=mx.EXPECTED_HEADER, kind="datetime", oi=lambda i: "1000.5", symbol="BTCUSDT"):
    start = calendar.timegm(time.strptime(day, "%Y-%m-%d")) * 1000
    if rows is None:
        rows = [f"{stamp_text(start + i * mx.PERIOD_MS, kind)},{symbol},{oi(i)},50000000.25,1.2,1.3,1.1,0.95"
                for i in range(mx.ROWS_PER_DAY)]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        member = zipfile.ZipInfo(f"{symbol}-metrics-{day}.csv", date_time=(2024, 1, 1, 0, 0, 0))  # deterministic
        archive.writestr(member, "\n".join([",".join(header), *rows]) + "\n")
    return buffer.getvalue()


class ParseTests(unittest.TestCase):
    def test_expected_header_is_accepted(self):
        rows, info = mx.parse_daily_zip(make_zip(), "BTCUSDT", DAY)
        self.assertEqual(len(rows), 288)
        self.assertEqual(info["header"], ",".join(mx.EXPECTED_HEADER))
        self.assertEqual(rows[1].create_time_ms, DAY_START + mx.PERIOD_MS)
        self.assertEqual(rows[0].values, ("1000.5", "50000000.25", "1.2", "1.3", "1.1", "0.95"))

    def test_other_header_fails_with_the_observed_header(self):
        header = ("create_time", "symbol", "open_interest", "a", "b", "c", "d", "e")
        with self.assertRaises(mx.MetricsFormatError) as caught:
            mx.parse_daily_zip(make_zip(header=header, rows=[]), "BTCUSDT", DAY)
        self.assertIn("observed: create_time,symbol,open_interest,a,b,c,d,e", str(caught.exception))

    def test_both_timestamp_formats(self):
        text_rows, text_info = mx.parse_daily_zip(make_zip(kind="datetime"), "BTCUSDT", DAY)
        ms_rows, ms_info = mx.parse_daily_zip(make_zip(kind="epoch_ms"), "BTCUSDT", DAY)
        self.assertEqual((text_info["timestamp_format"], ms_info["timestamp_format"]), ("datetime", "epoch_ms"))
        self.assertEqual([r.create_time_ms for r in text_rows], [r.create_time_ms for r in ms_rows])
        mixed = [f"{stamp_text(DAY_START)},BTCUSDT,1,1,1,1,1,1", f"{DAY_START + 300000},BTCUSDT,1,1,1,1,1,1"]
        with self.assertRaises(mx.MetricsFormatError):
            mx.parse_daily_zip(make_zip(rows=mixed), "BTCUSDT", DAY)

    def test_bad_values_and_symbol(self):
        with self.assertRaises(mx.MetricsFormatError):
            mx.parse_daily_zip(make_zip(oi=lambda i: "abc"), "BTCUSDT", DAY)
        with self.assertRaises(mx.MetricsFormatError):
            mx.parse_daily_zip(make_zip(symbol="ETHUSDT"), "BTCUSDT", DAY)
        rows, info = mx.parse_daily_zip(make_zip(oi=lambda i: ""), "BTCUSDT", DAY)
        self.assertEqual((len(rows), info["empty_values"]), (288, 288))

    def test_checksum_and_tags(self):
        digest = hashlib.sha256(b"x").hexdigest()
        self.assertEqual(data_lake.parse_checksum(f"{digest}  BTCUSDT-metrics-{DAY}.zip\n"), digest)
        self.assertEqual(mx.release_tag("BTCUSDT", "2024-03"), "mx-BTCUSDT-2024-03-r1")
        self.assertEqual(mx.validate_tag("mx-XRPUSDT-2020-01-r12"), "mx-XRPUSDT-2020-01-r12")
        for bad in ("rd-BTCUSDT-2024-03-r1", "mx-ADAUSDT-2024-03-r1", "mx-BTCUSDT-2024-13-r1", "mx-BTCUSDT-2024-03-r0"):
            with self.assertRaises(ValueError):
                mx.validate_tag(bad)
        with self.assertRaises(ValueError):
            data_lake.validate_tag("mx-BTCUSDT-2024-03-r1")  # the rd- tag rule is unchanged
        self.assertEqual(mx.source_url("BTCUSDT", DAY), "https://data.binance.vision/data/futures/um/daily/metrics/"
                                                         f"BTCUSDT/BTCUSDT-metrics-{DAY}.zip")
        self.assertEqual(mx.usable_from_ms(DAY_START), DAY_START + 300_000)


class CoverageTests(unittest.TestCase):
    def test_missing_duplicate_stuck_and_jump(self):
        day1, _ = mx.parse_daily_zip(make_zip("2024-03-01", oi=lambda i: "100" if i < 50 else str(100 + i)),
                                     "BTCUSDT", "2024-03-01")
        day3, _ = mx.parse_daily_zip(make_zip("2024-03-03", oi=lambda i: str(5000 + i)), "BTCUSDT", "2024-03-03")
        day3 = day3 + [day3[-1]]  # duplicate timestamp
        stats = mx.coverage({"2024-03-01": day1, "2024-03-02": None, "2024-03-03": day3})
        self.assertEqual((stats["days_present"], stats["days_missing"]), (2, ["2024-03-02"]))
        self.assertEqual(stats["duplicate_timestamps"], 1)
        self.assertEqual(stats["longest_identical_open_interest_run"], 50)
        self.assertEqual(stats["open_interest_day_jumps_above_5x"], 1)  # 387 -> 5287
        self.assertEqual(stats["open_interest_day_jumps_below_0_2x"], 0)
        self.assertEqual(stats["days_not_expected_rows"], 1)
        self.assertEqual(stats["first_ms"], day1[0].create_time_ms)
        rows, conflicts = mx.merged_rows({"2024-03-01": day1, "2024-03-03": day3})
        self.assertEqual((len(rows), conflicts), (576, 0))

    def test_csv_round_trip_and_raw_tar(self):
        rows, _ = mx.parse_daily_zip(make_zip(), "BTCUSDT", DAY)
        buffer = io.BytesIO()
        self.assertEqual(mx.write_metrics_csv_gz(buffer, rows), 288)
        self.assertEqual(mx.read_metrics_csv(io.BytesIO(buffer.getvalue()), "BTCUSDT", "2024-03"), rows)
        again = io.BytesIO()
        mx.write_metrics_csv_gz(again, rows)
        self.assertEqual(buffer.getvalue(), again.getvalue())  # deterministic
        tar = io.BytesIO()
        mx.write_raw_tar(tar, {"b.zip": b"2", "a.zip": b"1"})
        with tarfile.open(fileobj=io.BytesIO(tar.getvalue())) as archive:
            self.assertEqual(archive.getnames(), ["a.zip", "b.zip"])
            self.assertEqual(archive.extractfile("b.zip").read(), b"2")


class FetchAndGuardTests(unittest.TestCase):
    def fetcher(self, missing=(), corrupt=()):
        def fetch(url):
            day = url.split("-metrics-")[1][:10]
            body = make_zip(day)
            if day in missing:
                return None
            if url.endswith(".CHECKSUM"):
                digest = hashlib.sha256(b"other" if day in corrupt else body).hexdigest()
                return f"{digest}  file.zip\n".encode()
            return body
        return fetch

    def test_404_is_a_gap_and_a_mismatch_is_an_error(self):
        days = mx.collect_month("BTCUSDT", "2024-02", self.fetcher(missing=("2024-02-10",)))
        self.assertEqual(len(days), 29)
        self.assertIsNone(days["2024-02-10"])
        self.assertEqual(days["2024-02-11"]["file"], "BTCUSDT-metrics-2024-02-11.zip")
        with self.assertRaises(mx.ChecksumMismatch):
            mx.collect_month("BTCUSDT", "2024-02", self.fetcher(corrupt=("2024-02-03",)))

    def test_build_records_gaps_in_the_manifest(self):
        scripts = Path(__file__).resolve().parents[2] / "scripts" / "research"
        definition = importlib.util.spec_from_file_location("test_metrics_build_script", scripts / "metrics_build.py")
        module = importlib.util.module_from_spec(definition)
        previous = sys.path[:]
        try:
            sys.path.insert(0, str(scripts))
            definition.loader.exec_module(module)
        finally:
            sys.path[:] = previous
        with tempfile.TemporaryDirectory() as directory:
            tag = mx.release_tag("BTCUSDT", "2024-02")
            manifest, uploads = module.build("BTCUSDT", "2024-02", tag, Path(directory),
                                             self.fetcher(missing=("2024-02-10",)))
            self.assertEqual(manifest["coverage"]["days_missing"], ["2024-02-10"])
            self.assertEqual(manifest["days"]["2024-02-10"], {"status": "missing"})
            self.assertEqual(manifest["timestamp_format"], "datetime")
            self.assertEqual([upload["name"] for upload in uploads],
                             ["metrics__BTCUSDT__2024-02.csv.gz", "metrics-raw__BTCUSDT__2024-02.tar",
                              "metrics__BTCUSDT__2024-02.manifest.json"])
            empty, nothing = module.build("BTCUSDT", "2024-02", tag, Path(directory) / "empty",
                                          lambda url: None)
            self.assertEqual((empty, nothing), (None, []))

    def test_loader_refuses_hidden_months_without_a_token(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(HiddenStretchLocked):
                mx.load_symbol_metrics(directory, "BTCUSDT", "2025-12", "2026-01")


if __name__ == "__main__":
    unittest.main()
