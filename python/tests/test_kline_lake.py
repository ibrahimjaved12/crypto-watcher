"""Kline lake (rk-): bars from 1m klines, missing/no-trade minutes, tags, parity, rk/rd stitching, checksum stop."""
from __future__ import annotations

import gzip
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

from market_analysis import data_lake as lake
from market_analysis.benchmark.bars import MISSING, read_bars_csv
from market_analysis.benchmark.market_data import load_symbol_bars

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "research"
MONTH = "2023-12"
START, END, DAYS = lake.month_bounds_ms(MONTH)


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"test_{name}_script", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    previous = sys.path[:]
    try:
        sys.path.insert(0, str(SCRIPTS))
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = previous
    return module


def kline(t, o="100", h="101", l="99", c="100.5", volume="2", quote="200.5", trades="3", taker="1", taker_quote="100"):
    return f"{t},{o},{h},{l},{c},{volume},{t + 59_999},{quote},{trades},{taker},{taker_quote},0"


def zip_rows(lines, header=False):
    buffer = io.BytesIO()
    body = (["open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,"
             "taker_buy_quote_volume,ignore"] if header else []) + list(lines)
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("x.csv", "".join(line + "\n" for line in body))
    buffer.seek(0)
    return buffer


def index(lines, month=MONTH, family="klines", header=False):
    return lake.index_klines(lake.read_zip_csv(zip_rows(lines, header), 12), month, family)


def rk_bytes(lines, month=MONTH, header=False, cross=True) -> bytes:
    k = index(lines, month, header=header)
    others = {name: index(lines, month, name) for name in ("markPriceKlines", "indexPriceKlines",
                                                           "premiumIndexKlines")} if cross else {}
    out = io.BytesIO()
    lake.write_rk_bars_csv_gz(out, month, klines=k, mark=others.get("markPriceKlines"),
                              index=others.get("indexPriceKlines"), premium=others.get("premiumIndexKlines"))
    return out.getvalue()


class KlineBarTests(unittest.TestCase):
    def test_bars_from_klines_with_and_without_header(self):
        lines = [kline(START), kline(START + 60_000, trades="0", o="100.5", h="100.5", l="100.5", c="100.5",
                                     volume="0", quote="0", taker="0", taker_quote="0")]
        for header in (False, True):
            data = rk_bytes(lines, header=header)
            bars = read_bars_csv(io.BytesIO(data), "BTCUSDT", MONTH)  # the bars1m-v1 reader loads rk bars as-is
            self.assertEqual(bars.minutes, DAYS * 1440)
            self.assertEqual((bars.open[0], bars.high[0], bars.close[0]), (100 * 10 ** 8, 101 * 10 ** 8,
                                                                           1005 * 10 ** 7))
            self.assertEqual((bars.volume[0], bars.trades[0]), (2 * 10 ** 8, 3))
            self.assertEqual(bars.flags[0], lake.FLAG_KLINE_SOURCE)
            # a no-trade kline repeats the previous close: not a traded price, so it is empty and flagged
            self.assertEqual(bars.open[1], MISSING)
            self.assertTrue(bars.flags[1] & lake.FLAG_NO_AGGTRADES)
            # every other minute has no kline row: marked, never filled
            self.assertEqual(bars.close[2], MISSING)
            self.assertTrue(bars.flags[2] & lake.FLAG_KLINE_ROW_MISSING and bars.flags[2] & lake.FLAG_KLINE_SOURCE)
            self.assertTrue(bars.flags[2] & lake.FLAG_MARK_MISSING)

    def test_flag_counts_and_missing_cross_checks(self):
        out = io.BytesIO()
        written = lake.write_rk_bars_csv_gz(out, MONTH, klines=index([kline(START)]))
        counts = written["flag_counts"]
        self.assertEqual(written["rows"], DAYS * 1440)
        self.assertEqual(counts["KLINE_SOURCE"], DAYS * 1440)
        self.assertEqual(counts["KLINE_ROW_MISSING"], DAYS * 1440 - 1)
        self.assertEqual(counts["MARK_MISSING"], DAYS * 1440)
        header = gzip.decompress(out.getvalue()).decode("ascii").split("\n", 1)[0]
        self.assertEqual(header, ",".join(lake.COLUMNS))

    def test_tags_and_months(self):
        self.assertEqual(lake.rk_release_tag("BTCUSDT", "2021-03"), "rk-BTCUSDT-2021-03-r1")
        for month in ("2020-11", "2024-01"):
            with self.assertRaises(ValueError):
                lake.rk_release_tag("BTCUSDT", month)
        with self.assertRaises(ValueError):
            lake.rk_release_tag("ADAUSDT", "2021-03")
        self.assertEqual(lake.validate_lake_tag("rk-ETHUSDT-2022-01-r2"), "rk-ETHUSDT-2022-01-r2")
        self.assertEqual(lake.validate_lake_tag("rd-ETHUSDT-2024-01-r1"), "rd-ETHUSDT-2024-01-r1")
        with self.assertRaises(ValueError):
            lake.validate_lake_tag("dk1-ETHUSDT-2020-01_2026-09-r1")
        with self.assertRaises(ValueError):
            lake.validate_tag("rk-ETHUSDT-2022-01-r1")  # the rd validator stays rd-only
        self.assertEqual((lake.bar_source("2023-12"), lake.bar_source("2024-01")), ("kline", "aggtrade"))


class ParityTests(unittest.TestCase):
    def test_compare_bars_counts_differences(self):
        base = [kline(START), kline(START + 60_000)]
        changed = [kline(START, h="102", volume="2.5"), kline(START + 60_000)]
        result = lake.compare_bars(io.BytesIO(rk_bytes(base)), io.BytesIO(rk_bytes(changed)))
        self.assertEqual(result["minutes"], DAYS * 1440)
        self.assertEqual((result["both_priced"], result["ohlc_differs"], result["high_differs"]), (2, 1, 1))
        self.assertEqual((result["open_differs"], result["volume_differs"]), (0, 1))
        self.assertAlmostEqual(result["max_abs_high_diff_bp"], round(1 / 102 * 1e4, 4))
        self.assertEqual(result["neither_priced"], DAYS * 1440 - 2)
        self.assertEqual(result["total_volume"], {"kline": "4", "aggtrade": "4.5"})
        only = lake.compare_bars(io.BytesIO(rk_bytes(base)), io.BytesIO(rk_bytes(base[:1])))
        self.assertEqual((only["both_priced"], only["kline_only_priced"]), (1, 1))
        kb = load_script("klines_build")
        lines = kb.parity_lines("BTCUSDT", result)
        self.assertIn("| ohlc_differs | 1 |", lines)
        self.assertTrue(lines[0].startswith("### Kline vs aggTrades parity BTCUSDT 2024-01"))


class StitchTests(unittest.TestCase):
    def test_rk_then_rd_at_the_2024_boundary(self):
        last = END - 60_000
        rk = rk_bytes([kline(START), kline(last)])
        rd_bars = lake.MinuteBars("2024-01")
        rd_bars.consume([["1", "100", "1", "10", "10", str(END), "false"]])
        rd = io.BytesIO()
        lake.write_bars_csv_gz(rd, rd_bars)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / lake.bars_asset_name("BTCUSDT", "2023-12")).write_bytes(rk)
            (Path(tmp) / lake.bars_asset_name("BTCUSDT", "2024-01")).write_bytes(rd.getvalue())
            series = load_symbol_bars(tmp, "BTCUSDT", "2023-12", "2024-01")
        boundary = DAYS * 1440
        self.assertEqual(series.minutes, (DAYS + 31) * 1440)  # no duplicate, no gap
        self.assertEqual(series.open_time(boundary), END)
        self.assertTrue(series.flags[boundary - 1] & lake.FLAG_KLINE_SOURCE)
        self.assertFalse(series.flags[boundary] & lake.FLAG_KLINE_SOURCE)
        self.assertEqual((series.close[boundary - 1], series.close[boundary]), (1005 * 10 ** 7, 100 * 10 ** 8))


class BuilderTests(unittest.TestCase):
    def test_checksum_failure_stops_the_run(self):
        kb = load_script("klines_build")
        builder = sys.modules["data_lake_build"]

        def fake_fetch(url, destination, attempts=4):
            if url.endswith(".CHECKSUM"):
                return b"a" * 64 + b"  x.zip\n", "", 0
            destination.write_bytes(b"zip")
            return None, "b" * 64, 3

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(builder, "fetch", fake_fetch):
            with self.assertRaisesRegex(RuntimeError, "CHECKSUM MISMATCH"):
                kb.main(["--symbol", "BTCUSDT", "--month", "2021-01", "--workdir", tmp])
            self.assertFalse(any(Path(tmp).iterdir()))  # the mismatching zip is removed

    def test_argument_guards(self):
        kb = load_script("klines_build")
        with tempfile.TemporaryDirectory() as tmp:
            for argv in (["--symbol", "BTCUSDT", "--month", "2024-01"], ["--symbol", "ADAUSDT", "--month", "2021-01"],
                         ["--symbol", "BTCUSDT", "--parity", "--publish"]):
                with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
                    kb.main([*argv, "--workdir", tmp])


if __name__ == "__main__":
    unittest.main()
