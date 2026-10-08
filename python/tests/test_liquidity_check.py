"""Liquidity check script (#183): kline sums, ratios to the smallest frozen symbol, ranks (no network)."""
from decimal import Decimal
from fractions import Fraction
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile


def load_script():
    scripts = Path(__file__).resolve().parents[2] / "scripts" / "research"
    definition = importlib.util.spec_from_file_location("test_liquidity_check_script", scripts / "liquidity_check.py")
    module = importlib.util.module_from_spec(definition)
    previous = sys.path[:]
    try:
        sys.path.insert(0, str(scripts))
        definition.loader.exec_module(module)
    finally:
        sys.path[:] = previous
    return module


HEADER = ("open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,"
          "taker_buy_quote_volume,ignore\n")


def kline(open_time, quote, trades):
    return f"{open_time},1,1,1,1,5,{open_time + 59999},{quote},{trades},2,2,0\n"


class LiquidityCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lc = load_script()

    def test_sum_with_and_without_header_and_checksum(self):
        body = kline(0, "1.10", 3) + kline(60000, "2.25", 4)
        for text in (body, HEADER + body):
            totals = self.lc.sum_klines(io.BytesIO(text.encode()))
            self.assertEqual((totals.quote_volume, totals.trades, totals.rows), (Decimal("3.35"), 7, 2))
        with self.assertRaises(ValueError):
            self.lc.sum_klines(io.BytesIO((body + HEADER).encode()))

        def fake_fetch(url, destination, attempts=4):
            with zipfile.ZipFile(destination, "w") as archive:
                archive.writestr("X-1m-2024-01.csv", HEADER + body)
            return None, hashlib.sha256(destination.read_bytes()).hexdigest(), destination.stat().st_size

        with TemporaryDirectory() as directory:
            workdir = Path(directory)
            with patch.object(self.lc, "published_checksum", lambda url: None):
                self.assertIsNone(self.lc.month_totals("HYPEUSDT", "2024-01", workdir))
            with patch.object(self.lc, "published_checksum", lambda url: "0" * 64), \
                    patch.object(self.lc, "fetch", fake_fetch), self.assertRaisesRegex(RuntimeError, "CHECKSUM"):
                self.lc.month_totals("ADAUSDT", "2024-01", workdir)
            self.assertEqual(os.listdir(workdir), [])  # zips never kept
            self.assertTrue(self.lc.kline_url("1000PEPEUSDT", "2024-01").endswith(
                "/um/monthly/klines/1000PEPEUSDT/1m/1000PEPEUSDT-1m-2024-01.zip"))

    def test_ratios_ranks_and_missing_months(self):
        lc, months = self.lc, ["2024-01", "2024-02"]
        frozen = ("F1", "F2")
        volumes = {("F1", "2024-01"): 100, ("F2", "2024-01"): 40, ("F1", "2024-02"): 50, ("F2", "2024-02"): 80,
                   ("A", "2024-01"): 20, ("A", "2024-02"): 100, ("B", "2024-02"): 25}
        totals = {key: lc.MonthTotals(Decimal(value), 1, 1) for key, value in volumes.items()}
        rows = lc.summarize(totals, months, candidates=("A", "B", "C"), frozen=frozen)
        by = {row["symbol"]: row for row in rows}
        self.assertEqual((by["A"]["months"], by["A"]["min_ratio"], by["A"]["median_ratio"], by["A"]["min_rank"]),
                         (2, Fraction(1, 2), Fraction(5, 4), 1))   # 20/40 and 100/50
        self.assertEqual((by["B"]["months"], by["B"]["min_ratio"], by["B"]["min_rank"]), (1, Fraction(1, 2), 4))
        self.assertEqual((by["C"]["months"], by["C"]["median_ratio"], by["C"]["min_rank"]), (0, None, None))
        self.assertEqual([row["symbol"] for row in rows], ["A", "B", "C"])
        lines = lc.table(rows, months, 5)
        self.assertIn("| A | 2/2 | 0.500 | 1.250 | 1 |", lines)
        self.assertIn("| C | 0/2 | - | - | - |", lines)
        del totals[("F2", "2024-02")]
        with self.assertRaisesRegex(ValueError, "frozen"):
            lc.summarize(totals, months, candidates=("A",), frozen=frozen)


if __name__ == "__main__":
    unittest.main()
