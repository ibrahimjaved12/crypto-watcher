"""Forex signals (#374): reuse the baselines, date at the candle end, suppress unsafe entries visibly."""

from array import array
from datetime import datetime, timezone
import hashlib
import unittest
from unittest.mock import patch

from market_analysis.benchmark.bars import MISSING
from market_analysis.fx.bars import FLAG_CLOSED, FLAG_MISSING_OPEN, FLAG_PRESENT, FxBarSeries
from market_analysis.fx.candles import build_fx_candles
from market_analysis.fx.signals import generate_fx_signals
from market_analysis.instruments import forex_instrument
from market_analysis.trading_calendar import FX_WEEK

MINUTE = 60_000
EURUSD = forex_instrument("EURUSD")
EMA = "ema_cross_20_50:60"
PRICE, JUMP = 108_000_000, 500_000


def utc(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()) * 1000


START = utc("2026-10-04T21:00")  # a Sunday open: more than 102 hourly candles of warm-up before every case


def fixture(end, jump_ms, drop=(), start=START):
    """Flat bid price, then a one-step jump at ``jump_ms`` (an exact EMA20/EMA50 cross on that candle)."""
    minutes = (end - start) // MINUTE
    columns = {name: array("q", [MISSING]) * minutes for name in ("open", "high", "low", "close", "spread", "tick_volume")}
    flags = array("H", [FLAG_CLOSED]) * minutes
    t = FX_WEEK.next_open_ms(start)
    while t < end:
        run_end = min(FX_WEEK.open_until_ms(t), end)
        for minute in range(t, run_end, MINUTE):
            offset = (minute - start) // MINUTE
            if minute in drop:
                flags[offset] = FLAG_MISSING_OPEN
                continue
            price = PRICE if minute < jump_ms else PRICE + JUMP
            for name in ("open", "high", "low", "close"):
                columns[name][offset] = price
            columns["spread"][offset], columns["tick_volume"][offset] = 1000, 1
            flags[offset] = FLAG_PRESENT
        if run_end >= end:
            break
        t = FX_WEEK.next_open_ms(run_end)
    bars = FxBarSeries("fx:EURUSD", "exness-mt5-demo", "EURUSD", FX_WEEK.calendar_id, start, minutes, 1000,
                       flags=flags, **columns)
    return bars, {60: build_fx_candles(bars, 60, FX_WEEK)}


def run(bars, candles, ids=(EMA,)):
    return generate_fx_signals(EURUSD, bars, candles, FX_WEEK, bars.start_ms, bars.end_ms, list(ids))


def expected_id(ms, side=1):
    return hashlib.sha256(f"{EMA}|ta-v1|EURUSD|{ms}|{side}".encode("ascii")).hexdigest()


class SignalTests(unittest.TestCase):
    def test_known_cross_identity_and_sessions(self):
        bars, candles = fixture(utc("2026-10-14T16:00"), jump_ms=utc("2026-10-14T12:00"))
        batch = run(bars, candles)
        signal_ms = utc("2026-10-14T13:00")
        self.assertEqual([(s.signal_ms, s.side, s.symbol) for s in batch.signals], [(signal_ms, 1, "EURUSD")])
        self.assertEqual(batch.signals[0].signal_id, expected_id(signal_ms))
        self.assertEqual(batch.sessions[expected_id(signal_ms)], ("london", "new_york"))
        self.assertEqual((batch.suppressed, batch.reasons), ([], {}))
        self.assertEqual(run(bars, candles), batch)  # idempotent

    def test_monday_entry_is_emitted(self):
        bars, candles = fixture(utc("2026-10-12T14:00"), jump_ms=utc("2026-10-12T09:00"))
        batch = run(bars, candles)
        self.assertEqual([s.signal_ms for s in batch.signals], [utc("2026-10-12T10:00")])

    def test_window_that_crosses_the_weekend_is_suppressed(self):
        bars, candles = fixture(utc("2026-10-16T21:00"), jump_ms=utc("2026-10-16T19:00"))
        batch = run(bars, candles)
        self.assertEqual(batch.signals, [])
        self.assertEqual([(s.signal.signal_ms, s.reason) for s in batch.suppressed],
                         [(utc("2026-10-16T20:00"), "window_exceeds_session")])
        self.assertEqual(batch.suppressed[0].signal.signal_id, expected_id(utc("2026-10-16T20:00")))

    def test_signal_at_the_close_is_suppressed(self):
        bars, candles = fixture(utc("2026-10-18T22:00"), jump_ms=utc("2026-10-16T20:00"))
        batch = run(bars, candles)
        self.assertEqual([(s.signal.signal_ms, s.reason) for s in batch.suppressed],
                         [(utc("2026-10-16T21:00"), "market_closed_at_entry")])
        self.assertEqual(batch.signals, [])

    def test_missing_entry_minute_is_suppressed(self):
        entry = utc("2026-10-12T10:00")
        bars, candles = fixture(utc("2026-10-12T14:00"), jump_ms=utc("2026-10-12T09:00"), drop={entry})
        batch = run(bars, candles)
        self.assertEqual([(s.signal.signal_ms, s.reason) for s in batch.suppressed],
                         [(entry, "entry_minute_missing")])


class SelectionTests(unittest.TestCase):
    def test_placebo_incompatible_insufficient_and_unknown(self):
        bars, candles = fixture(utc("2026-10-12T14:00"), jump_ms=utc("2026-10-12T09:00"))
        batch = run(bars, candles, ["placebo-v1:" + EMA])
        self.assertEqual(batch.reasons, {"placebo-v1:" + EMA: "placebo_not_supported_for_fx"})
        refused = ([], {EMA: ("market_not_supported:forex",)})
        with patch("market_analysis.fx.signals.compatible_strategy_ids", return_value=refused):
            batch = run(bars, candles)
        self.assertEqual(batch.reasons, {EMA: "incompatible:market_not_supported:forex"})
        self.assertEqual(batch.signals, [])
        short, short_candles = fixture(utc("2026-10-12T14:00"), jump_ms=utc("2026-10-12T09:00"),
                                       start=utc("2026-10-11T21:00"))
        self.assertEqual(run(short, short_candles).reasons, {EMA: "insufficient_history"})
        with self.assertRaises(ValueError):
            run(bars, candles, ["nope:60"])
        with self.assertRaises(ValueError):
            generate_fx_signals(EURUSD, bars, candles, FX_WEEK, 10, 5, [EMA])


if __name__ == "__main__":
    unittest.main()
