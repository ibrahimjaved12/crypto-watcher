"""Synthetic in-memory tests for the research data lake bar builder (#183)."""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import unittest
import zipfile

from market_analysis import data_lake as lake

MONTH = "2025-02"
START, END, DAYS = lake.month_bounds_ms(MONTH)


def zip_bytes(lines, name="data.csv", extra_members=()):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, "".join(line + "\n" for line in lines))
        for member in extra_members:
            archive.writestr(member, "x\n")
    buffer.seek(0)
    return buffer


def agg(agg_id, price, quantity, time_ms, buyer_maker=False, first=None, last=None):
    first = agg_id * 10 if first is None else first
    last = first if last is None else last
    return f"{agg_id},{price},{quantity},{first},{last},{time_ms},{'true' if buyer_maker else 'false'}"


def kline(open_time, volume="0", taker="0", o="1", h="1", l="1", c="1"):
    return f"{open_time},{o},{h},{l},{c},{volume},{open_time + 59_999},0,0,{taker},0,0"


def build(agg_lines, kline_lines=None, mark_lines=None):
    bars = lake.MinuteBars(MONTH)
    bars.consume(lake.read_zip_csv(zip_bytes(agg_lines), 7))
    klines = (lake.index_klines(lake.read_zip_csv(zip_bytes(kline_lines), 12), MONTH)
              if kline_lines is not None else None)
    mark = (lake.index_klines(lake.read_zip_csv(zip_bytes(mark_lines), 12), MONTH, "markPriceKlines")
            if mark_lines is not None else None)
    output = io.BytesIO()
    written = lake.write_bars_csv_gz(output, bars, klines=klines, mark=mark)
    raw = output.getvalue()
    rows = list(csv.DictReader(io.StringIO(gzip.decompress(raw).decode("ascii"))))
    return bars, written, rows, raw


class DecimalTests(unittest.TestCase):
    def test_exact_scaled_sums_and_rendering(self):
        self.assertEqual(lake.render_scaled(lake.parse_scaled("0.1") + lake.parse_scaled("0.2")), "0.3")
        self.assertEqual(lake.render_scaled(lake.parse_scaled("100.50000000")), "100.5")
        self.assertEqual(lake.render_scaled(0), "0")
        self.assertEqual(lake.render_scaled(lake.parse_scaled("0.00000001")), "0.00000001")
        self.assertEqual(lake.render_scaled(lake.parse_scaled("-1.5")), "-1.5")
        self.assertEqual(lake.canonical_decimal("001.2300"), "1.23")
        self.assertEqual(lake.canonical_decimal("0.000"), "0")
        total = lake.ExactSum()
        for text in ("0.1", "0.02", "3", "0.000000001"):
            total.add(text)
        self.assertEqual(total.text(), "3.120000001")

    def test_more_than_eight_fractional_digits_and_malformed_values_raise(self):
        for text in ("0.123456789", "1e5", "1.", ".5", "1_0", "", "nan"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                lake.parse_scaled(text)
        with self.assertRaises(ValueError):
            lake.MinuteBars(MONTH).consume([agg(1, "100.123456789", "1", START).split(",")])


class BarTests(unittest.TestCase):
    def test_bucket_boundary(self):
        _, _, rows, _ = build([agg(1, "10", "1", START + 59_999), agg(2, "11", "2", START + 60_000)])
        self.assertEqual(len(rows), DAYS * 1440)
        self.assertEqual((rows[0]["open_time_ms"], rows[0]["volume"], rows[0]["close"]), (str(START), "1", "10"))
        self.assertEqual((rows[1]["open_time_ms"], rows[1]["volume"], rows[1]["open"]), (str(START + 60_000), "2", "11"))

    def test_exact_volumes_quote_and_taker_side(self):
        bars, _, rows, _ = build([
            agg(1, "0.1", "0.1", START, buyer_maker=False),   # taker bought
            agg(2, "0.2", "0.2", START + 1, buyer_maker=True),  # taker sold
            agg(3, "3", "0.00000001", START + 2, buyer_maker=False, first=30, last=34),
        ])
        row = rows[0]
        self.assertEqual(row["volume"], "0.30000001")
        self.assertEqual(row["quote_volume"], "0.05000003")  # 0.01 + 0.04 + 0.00000003
        self.assertEqual(row["taker_buy_volume"], "0.10000001")
        self.assertEqual(row["taker_buy_quote_volume"], "0.01000003")
        self.assertEqual((row["agg_rows"], row["trades"]), ("3", "7"))
        self.assertEqual(bars.stats()["total_quote_volume"], "0.05000003")
        with self.assertRaises(ValueError):
            lake.MinuteBars(MONTH).consume([agg(1, "1", "1", START).replace(",false", ",maybe").split(",")])

    def test_ohlc_by_agg_id_order_and_empty_minutes(self):
        # File order differs from agg id order inside the minute.
        _, written, rows, _ = build([agg(7, "105", "1", START + 5), agg(5, "100", "1", START + 30),
                                     agg(9, "90", "1", START + 1), agg(6, "120", "1", START + 2)])
        row = rows[0]
        self.assertEqual((row["open"], row["high"], row["low"], row["close"]), ("100", "120", "90", "90"))
        self.assertEqual((row["first_agg_id"], row["last_agg_id"]), ("5", "9"))
        empty = rows[1]
        self.assertEqual((empty["open"], empty["high"], empty["low"], empty["close"]), ("", "", "", ""))
        self.assertEqual((empty["volume"], empty["quote_volume"], empty["agg_rows"], empty["trades"]),
                         ("0", "0", "0", "0"))
        self.assertTrue(int(empty["flags"]) & lake.FLAG_NO_AGGTRADES)
        self.assertFalse(int(row["flags"]) & lake.FLAG_NO_AGGTRADES)
        self.assertEqual(written["flag_counts"]["NO_AGGTRADES"], DAYS * 1440 - 1)

    def test_id_statistics_and_rows_outside_month(self):
        bars, _, rows, _ = build([agg(1, "1", "1", START - 1), agg(2, "1", "1", START), agg(5, "1", "1", START),
                                  agg(4, "1", "1", START), agg(6, "1", "1", END)])
        stats = bars.stats()
        self.assertEqual((stats["rows"], stats["rows_outside_month"]), (5, 2))
        self.assertEqual((stats["id_gaps"], stats["id_not_increasing"]), (1, 1))
        self.assertEqual(rows[0]["agg_rows"], "3")

    def test_column_order(self):
        _, _, rows, raw = build([agg(1, "1", "1", START)])
        header = gzip.decompress(raw).decode("ascii").split("\n", 1)[0].split(",")
        self.assertEqual(tuple(header), lake.COLUMNS)
        self.assertEqual(header[13:22], list(lake.KLINE_CHECK_COLUMNS))
        self.assertEqual(header[-1], "flags")
        self.assertEqual(len(header), 13 + 9 + 12 + 1)


class FlagTests(unittest.TestCase):
    def test_kline_cross_check_flags(self):
        minute1, minute2, minute3 = START, START + 60_000, START + 120_000
        _, written, rows, _ = build(
            [agg(1, "10", "1.5", minute1), agg(2, "10", "2", minute2), agg(3, "10", "1", minute3, buyer_maker=True)],
            kline_lines=[kline(minute1, volume="1.500", taker="1.5"), kline(minute2, volume="0", taker="0")],
            mark_lines=[kline(minute1)])
        first, second, third = (int(rows[i]["flags"]) for i in range(3))
        self.assertEqual(first & (lake.FLAG_KLINE_VOLUME_DIFFERS | lake.FLAG_KLINE_TAKER_DIFFERS
                                  | lake.FLAG_KLINE_ROW_MISSING | lake.FLAG_MARK_MISSING), 0)
        self.assertEqual(rows[0]["k_volume"], "1.500")  # stored as published
        self.assertTrue(second & lake.FLAG_KLINE_VOLUME_DIFFERS)
        self.assertTrue(second & lake.FLAG_KLINE_ZERO_BUT_TRADES)
        self.assertTrue(second & lake.FLAG_KLINE_TAKER_DIFFERS)
        self.assertTrue(second & lake.FLAG_MARK_MISSING)
        self.assertTrue(third & lake.FLAG_KLINE_ROW_MISSING)
        self.assertEqual(rows[2]["k_open"], "")
        self.assertTrue(first & lake.FLAG_INDEX_MISSING and first & lake.FLAG_PREMIUM_MISSING)
        self.assertEqual(written["flag_counts"]["KLINE_ZERO_BUT_TRADES"], 1)
        self.assertEqual(written["flag_counts"]["KLINE_ROW_MISSING"], DAYS * 1440 - 2)

    def test_kline_duplicates_are_counted_not_failures(self):
        index = lake.index_klines([kline(START, "1").split(","), kline(START, "2").split(","),
                                   kline(START - 60_000).split(",")], MONTH)
        stats = index.stats()
        self.assertEqual((stats["rows"], stats["duplicate_rows"], stats["duplicate_conflicts"],
                          stats["rows_outside_month"]), (1, 1, 1, 1))
        self.assertEqual(index.rows[START][4], "1")  # first row wins
        self.assertEqual(stats["missing_minutes"], DAYS * 1440 - 1)


class ArchiveTests(unittest.TestCase):
    def test_header_line_tolerance_and_column_count(self):
        header = "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker"
        rows = list(lake.read_zip_csv(zip_bytes([header, agg(1, "1", "1", START)]), 7))
        self.assertEqual(len(rows), 1)
        with self.assertRaises(ValueError):
            list(lake.read_zip_csv(zip_bytes([agg(1, "1", "1", START) + ",extra"]), 7))
        with self.assertRaises(ValueError):
            list(lake.read_zip_csv(zip_bytes([agg(1, "1", "1", START)], extra_members=("second.csv",)), 7))
        with self.assertRaises(ValueError):
            list(lake.read_zip_csv(zip_bytes([agg(1, "1", "1", START)], name="data.txt"), 7))

    def test_funding_header_interval_and_month(self):
        lines = ["calc_time,funding_interval_hours,last_funding_rate",
                 f"{START},8,0.00010000", f"{START + 4 * 3_600_000},4,-0.00002500", f"{END},8,0.0001"]
        output = io.BytesIO()
        stats = lake.write_funding_csv_gz(output, lake.read_zip_csv(zip_bytes(lines), 3), MONTH)
        text = gzip.decompress(output.getvalue()).decode("ascii")
        self.assertEqual(text, "calc_time_ms,funding_interval_hours,last_funding_rate\n"
                               f"{START},8,0.00010000\n{START + 4 * 3_600_000},4,-0.00002500\n")
        self.assertEqual((stats["rows"], stats["rows_outside_month"], stats["interval_hours"]), (2, 1, ["4", "8"]))

    def test_deterministic_bytes(self):
        lines = [agg(1, "97000.10", "0.003", START), agg(2, "97001", "0.5", START + 61_000, buyer_maker=True)]
        klines = [kline(START, "0.003", "0.003")]
        first, second = build(lines, klines)[3], build(lines, klines)[3]
        self.assertEqual(first, second)
        self.assertEqual(hashlib.sha256(first).hexdigest(), hashlib.sha256(second).hexdigest())
        self.assertEqual(first[4:8], b"\0\0\0\0")  # gzip mtime 0
        self.assertFalse(first[3] & 0x08)  # no embedded file name


class NamingTests(unittest.TestCase):
    def test_checksum_line_parsing(self):
        digest = "a" * 64
        self.assertEqual(lake.parse_checksum(f"{digest}  BTCUSDT-aggTrades-2025-01.zip\n"), digest)
        self.assertEqual(lake.parse_checksum(f"{digest.upper()} *file.zip"), digest)
        for bad in ("", "abc file.zip", "g" * 64 + " file.zip", "a" * 65 + " file.zip"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                lake.parse_checksum(bad)

    def test_release_tag_validation(self):
        self.assertEqual(lake.release_tag("BTCUSDT", "2025-01"), "rd-BTCUSDT-2025-01-r1")
        for bad in ("rd-BTCUSDT-2025-13-r1", "rd-ADAUSDT-2025-01-r1", "rd-BTCUSDT-2025-01-r0",
                    "rd-BTCUSDT-2025-1-r1", "recovery-BTCUSDT-2025-01-r1", "rd-BTCUSDT-2025-01-r1 ", "rd-btcusdt-2025-01-r1"):
            with self.subTest(tag=bad), self.assertRaises(ValueError):
                lake.validate_tag(bad)
        with self.assertRaises(ValueError):
            lake.release_tag("ADAUSDT", "2025-01")
        with self.assertRaises(ValueError):
            lake.release_tag("BTCUSDT", "2025-1")

    def test_months_and_urls(self):
        self.assertEqual(lake.months_between("2024-11", "2025-02"), ["2024-11", "2024-12", "2025-01", "2025-02"])
        start, end, days = lake.month_bounds_ms("2024-02")
        self.assertEqual((days, end - start), (29, 29 * 86_400_000))
        january_end = lake.month_bounds_ms("2025-01")[1]
        self.assertFalse(lake.month_finished("2025-01", january_end - 1))
        self.assertTrue(lake.month_finished("2025-01", january_end))
        self.assertEqual(lake.source_url("markPriceKlines", "BTCUSDT", "2025-01"),
                         f"{lake.BINANCE_BASE}/markPriceKlines/BTCUSDT/1m/BTCUSDT-1m-2025-01.zip")
        self.assertEqual(lake.raw_asset_name("klines", "BTCUSDT-1m-2025-01.zip"), "raw__klines__BTCUSDT-1m-2025-01.zip")


if __name__ == "__main__":
    unittest.main()
