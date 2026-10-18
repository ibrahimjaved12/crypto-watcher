"""Dukascopy decoder (#402 part 1): bi5 records, exact prices, minute bars, strictness, URL month base.

The bi5 bytes are built in the test from SYNTHETIC ticks; no real file is used.
"""

from datetime import datetime, timezone
from fractions import Fraction
import lzma
import struct
import unittest

from market_analysis.fx.dukascopy import POINT_DIVISORS, Tick, bars_from_ticks, decode_bi5, hour_url

HOUR = int(datetime(2024, 1, 16, 10, tzinfo=timezone.utc).timestamp()) * 1000


def bi5(*records):
    return lzma.compress(b"".join(struct.pack(">IIIff", *record) for record in records))


# offset ms, ask, bid (EURUSD scaled by 10**5), ask volume, bid volume
THREE = [(1_250, 108_512, 108_500, 1.5, 2.25), (30_000, 108_520, 108_505, 0.75, 1.0), (61_000, 108_530, 108_515, 3.0, 0.5)]


class DecodeTests(unittest.TestCase):
    def test_times_and_exact_prices(self):
        ticks = decode_bi5(bi5(*THREE), HOUR, POINT_DIVISORS["EURUSD"])
        self.assertEqual([t.time_ms for t in ticks], [HOUR + 1_250, HOUR + 30_000, HOUR + 61_000])
        self.assertEqual(ticks[0], Tick(HOUR + 1_250, Fraction(108_500, 100_000), Fraction(108_512, 100_000), 2.25, 1.5))
        self.assertEqual(ticks[0].bid, Fraction(217, 200))  # 1.08500 exactly, no float division
        self.assertIsInstance(ticks[1].ask, Fraction)

    def test_empty_hours(self):
        self.assertEqual(decode_bi5(b"", HOUR, 100_000), [])
        self.assertEqual(decode_bi5(lzma.compress(b""), HOUR, 100_000), [])

    def test_strictness(self):
        with self.assertRaisesRegex(ValueError, "multiple of 20"):
            decode_bi5(lzma.compress(struct.pack(">IIIff", *THREE[0]) + b"\x00" * 7), HOUR, 100_000)
        with self.assertRaisesRegex(ValueError, "past the hour"):
            decode_bi5(bi5((3_600_000, 108_512, 108_500, 1.0, 1.0)), HOUR, 100_000)
        self.assertEqual(len(decode_bi5(bi5((3_599_999, 108_512, 108_500, 1.0, 1.0)), HOUR, 100_000)), 1)
        for record in [(0, 0, 108_500, 1.0, 1.0), (0, 108_512, 0, 1.0, 1.0)]:
            with self.subTest(record=record), self.assertRaisesRegex(ValueError, "zero price"):
                decode_bi5(bi5(record), HOUR, 100_000)
        with self.assertRaises(ValueError):
            decode_bi5(bi5(*THREE), HOUR, 0)


class BarTests(unittest.TestCase):
    def test_minute_bars_ohlc_spread_and_count(self):
        bars = bars_from_ticks(decode_bi5(bi5(*THREE), HOUR, 100_000), 5)
        self.assertEqual(bars, [
            {"time_basis": "utc", "transport": "history", "open_time_ms": HOUR, "open": 1.085, "high": 1.08505,
             "low": 1.085, "close": 1.08505, "spread_points": 15, "tick_volume": 2},
            {"time_basis": "utc", "transport": "history", "open_time_ms": HOUR + 60_000, "open": 1.08515,
             "high": 1.08515, "low": 1.08515, "close": 1.08515, "spread_points": 15, "tick_volume": 1},
        ])
        # The spread is the minute's LAST tick; a 3-digit symbol uses its own digits.
        jpy = [Tick(HOUR, Fraction(157_120, 1000), Fraction(157_135, 1000), 1.0, 1.0)]
        self.assertEqual(bars_from_ticks(jpy, 3)[0]["spread_points"], 15)
        self.assertEqual(bars_from_ticks([], 5), [])

    def test_out_of_order_ticks_and_negative_spread_raise(self):
        ticks = decode_bi5(bi5(*THREE), HOUR, 100_000)
        with self.assertRaisesRegex(ValueError, "earlier"):
            bars_from_ticks([ticks[1], ticks[0]], 5)
        self.assertEqual(len(bars_from_ticks([ticks[0], ticks[0]], 5)), 1, "equal times are allowed")
        with self.assertRaisesRegex(ValueError, "ask below bid"):
            bars_from_ticks([Tick(HOUR, Fraction(2), Fraction(1), 1.0, 1.0)], 5)


class UrlTests(unittest.TestCase):
    def test_month_base_both_ways(self):
        self.assertEqual(hour_url("EURUSD", 2024, 1, 16, 10),
                         "https://datafeed.dukascopy.com/datafeed/EURUSD/2024/00/16/10h_ticks.bi5")
        self.assertEqual(hour_url("EURUSD", 2024, 1, 16, 10, zero_based_month=False),
                         "https://datafeed.dukascopy.com/datafeed/EURUSD/2024/01/16/10h_ticks.bi5")
        self.assertEqual(hour_url("xauusd", 2024, 12, 7, 0),
                         "https://datafeed.dukascopy.com/datafeed/XAUUSD/2024/11/07/00h_ticks.bi5")
        for args in [("EURUSD", 2024, 0, 1, 0), ("EURUSD", 2024, 13, 1, 0), ("EURUSD", 2024, 1, 1, 24), ("EUR/USD", 2024, 1, 1, 0)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                hour_url(*args)


if __name__ == "__main__":
    unittest.main()
