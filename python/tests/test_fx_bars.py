"""Forex bars (#374): gateway row validation, spread scaling and calendar classification."""

from datetime import datetime, timezone
from fractions import Fraction
import unittest

from market_analysis.forward.bars_adapter import CollectorRowError
from market_analysis.fx.bars import (
    FLAG_CLOSED, FLAG_MISSING_OPEN, FLAG_PRESENT, FLAG_ROW_WHILE_CLOSED, fx_bars_from_rows, missing_open_minutes,
)
from market_analysis.instruments import VenueSymbol, forex_instrument
from market_analysis.trading_calendar import FX_WEEK

MINUTE = 60_000
EURUSD = forex_instrument("EURUSD")
VENUE = VenueSymbol("exness-mt5-demo", "EURUSDm", "fx:EURUSD", digits=5, tick_size=Fraction(1, 100_000))


def utc(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()) * 1000


def row(open_ms, **changes):
    values = {"time_basis": "utc", "venue": "exness-mt5-demo", "native_symbol": "EURUSDm", "open_time_ms": open_ms,
              "open": 1.08, "high": 1.0802, "low": 1.0799, "close": 1.0801, "spread_points": 12, "tick_volume": 7,
              "transport": "history"}
    values.update(changes)
    return values


class RowTests(unittest.TestCase):
    def assert_code(self, code, rows, venue=VENUE):
        with self.assertRaises(CollectorRowError) as caught:
            fx_bars_from_rows(EURUSD, venue, FX_WEEK, rows)
        self.assertEqual(caught.exception.code, code)

    def test_row_validation(self):
        t = utc("2026-10-14T12:00")
        self.assert_code("time_basis", [row(t, time_basis="server")])
        self.assert_code("identity", [row(t, venue="other")])
        self.assert_code("identity", [row(t, native_symbol="EURUSD")])
        self.assert_code("alignment", [row(t + 1)])
        self.assert_code("ohlc", [row(t, high=1.0)])
        self.assert_code("spread", [row(t, spread_points=-1)])
        self.assert_code("volume", [row(t, tick_volume=-1)])
        self.assert_code("provenance", [row(t, transport="ftp")])
        self.assert_code("order", [row(t), row(t)])
        self.assert_code("order", [row(t + MINUTE), row(t)])
        self.assert_code("empty", [])
        with self.assertRaises(ValueError):
            fx_bars_from_rows(EURUSD, VenueSymbol("exness-mt5-demo", "EURUSDm", "fx:EURUSD"), FX_WEEK, [row(t)])

    def test_spread_and_prices_are_scaled(self):
        t = utc("2026-10-14T12:00")
        bars = fx_bars_from_rows(EURUSD, VENUE, FX_WEEK, [row(t)])
        self.assertEqual((bars.symbol, bars.venue, bars.tick), ("EURUSD", "exness-mt5-demo", 1000))
        self.assertEqual(bars.spread[0], 12 * 1000)
        self.assertEqual((bars.open[0], bars.close[0], bars.tick_volume[0]), (108_000_000, 108_010_000, 7))
        self.assertEqual(bars.index_of(t), 0)
        with self.assertRaises(ValueError):
            bars.index_of(t + MINUTE)


class FlagTests(unittest.TestCase):
    def test_missing_open_closed_and_row_while_closed(self):
        friday = [utc("2026-10-16T20:58"), utc("2026-10-16T20:59")]  # 20:57 is missing (open)
        saturday = utc("2026-10-17T12:00")
        sunday = [utc("2026-10-18T21:00"), utc("2026-10-18T21:01"), utc("2026-10-18T21:02")]
        rows = [row(utc("2026-10-16T20:56")), *[row(t) for t in friday], row(saturday), *[row(t) for t in sunday]]
        bars = fx_bars_from_rows(EURUSD, VENUE, FX_WEEK, rows)
        flag = lambda text: bars.flags[bars.index_of(utc(text))]
        self.assertEqual(flag("2026-10-16T20:56"), FLAG_PRESENT)
        self.assertEqual(flag("2026-10-16T20:57"), FLAG_MISSING_OPEN)
        self.assertEqual(flag("2026-10-16T21:00"), FLAG_CLOSED)
        self.assertEqual(flag("2026-10-17T11:59"), FLAG_CLOSED)
        self.assertEqual(flag("2026-10-17T12:00"), FLAG_ROW_WHILE_CLOSED)
        self.assertEqual(flag("2026-10-18T20:59"), FLAG_CLOSED)
        self.assertEqual(flag("2026-10-18T21:00"), FLAG_PRESENT)
        self.assertEqual(bars.anomalies(), {"missing_open": 1, "row_while_closed": 1})
        self.assertEqual(missing_open_minutes(bars), 1)
        self.assertEqual(bars.end_ms, utc("2026-10-18T21:03"))


if __name__ == "__main__":
    unittest.main()
