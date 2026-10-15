"""Liquidation archive probe: listing XML parser (with pagination), period-gap computation, zip profile."""
from __future__ import annotations

import hashlib
import importlib.util
import io
from pathlib import Path
import sys
import unittest
import zipfile

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "research"


def load_probe():
    definition = importlib.util.spec_from_file_location("test_liquidation_probe_script",
                                                        SCRIPTS / "liquidation_probe.py")
    module = importlib.util.module_from_spec(definition)
    previous = sys.path[:]
    try:
        sys.path.insert(0, str(SCRIPTS))
        definition.loader.exec_module(module)
    finally:
        sys.path[:] = previous
    return module


probe = load_probe()
P = "data/futures/um/daily/liquidationSnapshot/BTCUSDT/"


def page(keys, truncated=False, marker=None, namespace=True):
    ns = ' xmlns="http://s3.amazonaws.com/doc/2006-03-01/"' if namespace else ""
    contents = "".join(f"<Contents><Key>{key}</Key><Size>{size}</Size></Contents>" for key, size in keys)
    tail = f"<NextMarker>{marker}</NextMarker>" if marker else ""
    return (f"<?xml version='1.0'?><ListBucketResult{ns}><Name>b</Name><Prefix>{P}</Prefix>"
            f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>{tail}{contents}</ListBucketResult>"
            ).encode()


def daily_key(day, suffix=".zip"):
    return f"{P}BTCUSDT-liquidationSnapshot-{day}{suffix}"


class ListingTests(unittest.TestCase):
    def test_parse_page_with_and_without_namespace(self):
        for namespace in (True, False):
            keys, truncated, marker = probe.parse_listing(page([("a", 5), ("b", 7)], True, "b", namespace))
            self.assertEqual((keys, truncated, marker), ([("a", 5), ("b", 7)], True, "b"))
        self.assertEqual(probe.parse_listing(page([]))[:2], ([], False))

    def test_rejects_bad_xml(self):
        for body in (b"<not-xml", b"<Other/>", b'<!DOCTYPE x [<!ENTITY y "z">]><ListBucketResult/>',
                     b"<ListBucketResult><Contents><Key>a</Key><Size>x</Size></Contents></ListBucketResult>"):
            with self.assertRaises(probe.ProbeError):
                probe.parse_listing(body)

    def test_pagination_follows_markers_until_exhausted(self):
        calls = []
        pages = [page([("k1", 1), ("k2", 2)], True, "k2"), page([("k3", 3)], True), page([("k4", 4)])]

        def fetcher(url):
            calls.append(url)
            return pages[len(calls) - 1]

        keys = probe.list_keys(P, fetcher)
        self.assertEqual([k for k, _ in keys], ["k1", "k2", "k3", "k4"])
        self.assertIn("prefix=data/futures/um/daily/liquidationSnapshot/BTCUSDT/", calls[0])
        self.assertIn("delimiter=/", calls[0])
        self.assertNotIn("marker", calls[0])
        self.assertIn("marker=k2", calls[1])  # NextMarker
        self.assertIn("marker=k3", calls[2])  # no NextMarker: the last key

    def test_stuck_marker_is_an_error(self):
        with self.assertRaises(probe.ProbeError):
            probe.list_keys(P, lambda url: page([("k1", 1)], True, "k1") if "marker" not in url
                            else page([("k1", 1)], True, "k1"))
        with self.assertRaises(probe.ProbeError):
            probe.list_keys(P, lambda url: page([("k%d" % len(url), 1)], True), max_pages=3)


class PeriodTests(unittest.TestCase):
    def test_split_keys(self):
        listing = [(daily_key("2024-01-01"), 10), (daily_key("2024-01-01", ".zip.CHECKSUM"), 1),
                   (daily_key("2024-01-03"), 20), (daily_key("2024-01-03", ".zip.CHECKSUM"), 1),
                   (P + "notes.txt", 3)]
        found = probe.split_keys("daily", "BTCUSDT", listing)
        self.assertEqual((found["periods"], found["keys"], found["bytes"], found["checksums"], found["other"]),
                         (["2024-01-01", "2024-01-03"], 2, 30, 2, 1))

    def test_missing_days_across_month_and_year_ends(self):
        days = ["2023-12-30", "2024-01-02", "2024-01-03", "2024-01-06"]
        missing = probe.missing_periods(days, "daily")
        self.assertEqual(missing, ["2023-12-31", "2024-01-01", "2024-01-04", "2024-01-05"])
        self.assertEqual(probe.compress_ranges(missing, "daily"), ["2023-12-31..2024-01-01", "2024-01-04..2024-01-05"])
        self.assertEqual(probe.missing_periods(["2024-02-29", "2024-03-02"], "daily"), ["2024-03-01"])  # leap year
        self.assertEqual(probe.missing_periods([], "daily"), [])
        self.assertEqual(probe.missing_periods(["2024-05-05"], "daily"), [])

    def test_missing_months(self):
        missing = probe.missing_periods(["2023-11", "2024-02", "2024-03", "2024-05"], "monthly")
        self.assertEqual(missing, ["2023-12", "2024-01", "2024-04"])
        self.assertEqual(probe.compress_ranges(missing, "monthly"), ["2023-12..2024-01", "2024-04"])

    def test_probe_periods_stop_before_the_hidden_stretch(self):
        days = [f"2025-12-{d:02d}" for d in range(1, 32)] + ["2026-01-01", "2026-01-02"]
        picked = probe.pick_probe_periods(days)
        self.assertEqual(picked, ["2025-12-01", "2025-12-16", "2025-12-31"])
        self.assertEqual(probe.pick_probe_periods(["2026-01-01"]), [])
        self.assertEqual(probe.pick_probe_periods(["2024-05"]), ["2024-05"])


def make_zip(rows, header="time,side,order_type,status,price"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("x.csv", "\n".join([header, *rows]) + "\n")
    return buffer.getvalue()


class ProfileTests(unittest.TestCase):
    def test_profile_counts_and_repeats(self):
        rows = ["1700000000000,SELL,LIMIT,FILLED,100.5", "1700000000000,SELL,LIMIT,FILLED,100.5",
                "1700000001000,BUY,LIMIT,FILLED,101", "1700000001000,BUY,LIMIT,FILLED,101",
                "1700000002000,BUY,LIMIT,EXPIRED,102"]
        profile = probe.profile_zip(make_zip(rows))
        self.assertEqual(profile.header, ("time", "side", "order_type", "status", "price"))
        self.assertEqual((profile.rows, profile.repeated), (5, 2))
        self.assertEqual((profile.first_ms, profile.last_ms), (1700000000000, 1700000002000))
        self.assertEqual(dict(profile.distinct["side"]), {"SELL": 2, "BUY": 3})
        self.assertEqual(dict(profile.distinct["status"]), {"FILLED": 4, "EXPIRED": 1})
        self.assertNotIn("price", profile.distinct)  # numeric columns are never tabulated
        self.assertNotIn("time", profile.distinct)
        self.assertEqual(probe._iso(profile.first_ms), "2023-11-14 22:13:20")

    def test_headerless_and_high_cardinality(self):
        profile = probe.profile_zip(make_zip(["1700000000,a%d" % i for i in range(30)], header="1700000000,a-1"))
        self.assertEqual(profile.header, ("col0", "col1"))
        self.assertEqual(profile.rows, 31)
        self.assertIsNone(profile.distinct["col1"])  # more than 12 distinct text values
        with self.assertRaises(probe.ProbeError):
            probe.profile_zip(make_zip_two_members())

    def test_units_and_merge(self):
        self.assertEqual(probe._milliseconds("1700000000"), 1700000000000)
        self.assertEqual(probe._milliseconds("1700000000000"), 1700000000000)
        self.assertEqual(probe._milliseconds("1700000000000000"), 1700000000000)
        self.assertIsNone(probe._milliseconds("x"))
        total = probe.Profile()
        for text in ("SELL", "BUY"):
            total.merge_distinct({"side": probe.Counter({text: 2})})
        total.merge_distinct({"side": None})
        self.assertIsNone(total.distinct["side"])


def make_zip_two_members():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.csv", "t\n1\n")
        archive.writestr("b.csv", "t\n2\n")
    return buffer.getvalue()


class DownloadTests(unittest.TestCase):
    def test_checksum_verification_and_gaps(self):
        body = b"zip-bytes"
        digest = hashlib.sha256(body).hexdigest()
        files = {"u.CHECKSUM": f"{digest}  u.zip\n".encode(), "u": body}
        self.assertEqual(probe.download_verified("u", files.get), body)
        self.assertIsNone(probe.download_verified("u", {"u": body}.get))  # no checksum: a recorded gap
        self.assertIsNone(probe.download_verified("u", {"u.CHECKSUM": files["u.CHECKSUM"]}.get))
        files["u"] = b"tampered"
        with self.assertRaises(probe.ProbeError):
            probe.download_verified("u", files.get)

    def test_probe_symbol_end_to_end_with_fakes(self):
        zip_bytes = make_zip(["1700000000000,SELL,LIMIT,FILLED,1", "1700000001000,BUY,LIMIT,FILLED,2"])
        digest = hashlib.sha256(zip_bytes).hexdigest()

        def lister(key_prefix):
            if "/daily/" not in key_prefix:
                return []
            return [(daily_key(day), 10) for day in ("2024-01-01", "2024-01-03", "2024-01-05")]

        def fetcher(url):
            return f"{digest}  f.zip\n".encode() if url.endswith(".CHECKSUM") else zip_bytes

        result = probe.probe_symbol("BTCUSDT", lister, fetcher)
        daily = result["families"]["daily"]
        self.assertEqual((daily["keys"], daily["missing"], daily["missing_ranges"]), (3, 2, ["2024-01-02", "2024-01-04"]))
        self.assertEqual(result["families"]["monthly"]["keys"], 0)
        self.assertEqual((result["probe_family"], len(result["zips"]), result["rows"]), ("daily", 3, 6))
        self.assertEqual(result["distinct"]["side"], {"BUY": 3, "SELL": 3})
        lines = "\n".join(probe.report([result]))
        self.assertIn("observed header: `time,side,order_type,status,price`", lines)
        self.assertNotIn("1700000000000", lines)  # no raw row values in the public table

    def test_symbols_are_validated(self):
        self.assertEqual(probe.parse_symbols("BTCUSDT,ETHUSDT"), ["BTCUSDT", "ETHUSDT"])
        for bad in ("", "BTCUSDT,BTCUSDT", "FOOUSDT", "btcusdt", "BTCUSDT, ETHUSDT"):
            with self.assertRaises(probe.ProbeError):
                probe.parse_symbols(bad)


if __name__ == "__main__":
    unittest.main()
