"""Forex candles (#374): closed market = no candle; real data loss = invalid; weekends never reset indicators."""

from array import array
from datetime import datetime, timezone
import unittest

from market_analysis.benchmark import ta_strategies as ta
from market_analysis.benchmark.bars import MISSING
from market_analysis.benchmark.candles import CandleSeries
from market_analysis.fx.bars import FLAG_CLOSED, FLAG_MISSING_OPEN, FLAG_PRESENT, FxBarSeries
from market_analysis.fx.candles import build_fx_candles
from market_analysis.trading_calendar import FX_WEEK

MINUTE = 60_000
HOUR = 60 * MINUTE


def utc(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()) * 1000


def series(start, end, price_of=lambda minute_ms: 108_000_000, drop=()):
    """Minute bars on [start, end): a row for every open minute (except ``drop``), none while closed."""
    minutes = (end - start) // MINUTE
    columns = {name: array("q", [MISSING]) * minutes for name in ("open", "high", "low", "close", "spread", "tick_volume")}
    flags = array("H", [FLAG_CLOSED]) * minutes
    for offset in range(minutes):
        t = start + offset * MINUTE
        if not FX_WEEK.is_open(t):
            continue
        if t in drop:
            flags[offset] = FLAG_MISSING_OPEN
            continue
        price = price_of(t)
        for name in ("open", "high", "low", "close"):
            columns[name][offset] = price
        columns["spread"][offset], columns["tick_volume"][offset] = 1000, 2
        flags[offset] = FLAG_PRESENT
    return FxBarSeries("fx:EURUSD", "exness-mt5-demo", "EURUSD", FX_WEEK.calendar_id, start, minutes, 1000,
                       flags=flags, **columns)


class CandleTests(unittest.TestCase):
    def test_weekend_has_no_candles(self):
        bars = series(utc("2026-10-16T19:00"), utc("2026-10-18T23:00"))
        candles = build_fx_candles(bars, 60, FX_WEEK)
        self.assertEqual(candles.open_times, [utc("2026-10-16T19:00"), utc("2026-10-16T20:00"),
                                              utc("2026-10-18T21:00"), utc("2026-10-18T22:00")])
        self.assertEqual(candles.valid, [True] * 4)
        # 20:00-21:00 is fully open (the close is its end), so it is not partial; it ends at the close
        self.assertEqual(candles.partial, [False] * 4)
        self.assertEqual(candles.end_ms[1], utc("2026-10-16T21:00"))
        self.assertEqual(candles.volume, [120] * 4)
        self.assertEqual(candles.taker_buy_volume, [MISSING] * 4)

    def test_friday_candle_straddling_the_close_ends_at_the_close(self):
        bars = series(utc("2026-10-16T16:00"), utc("2026-10-19T00:00"))
        candles = build_fx_candles(bars, 240, FX_WEEK)
        friday = candles.open_times.index(utc("2026-10-16T20:00"))
        self.assertTrue(candles.partial[friday])
        self.assertEqual(candles.end_ms[friday], utc("2026-10-16T21:00"))

    def test_missing_open_minute_invalidates_only_its_candle(self):
        bars = series(utc("2026-10-16T19:00"), utc("2026-10-18T23:00"), drop={utc("2026-10-16T19:30")})
        candles = build_fx_candles(bars, 60, FX_WEEK)
        self.assertEqual(candles.valid, [False, True, True, True])
        self.assertEqual(candles.close[0], MISSING)
        self.assertEqual(candles.column("volume"), [None, 120, 120, 120])
        self.assertEqual(candles.column("close")[0], None)

    def test_fall_back_reopen_bucket_is_partial(self):
        reopen = utc("2026-11-01T22:00")
        bars = series(reopen, utc("2026-11-02T01:00"), price_of=lambda t: 108_000_000 + (t - reopen) // MINUTE)
        candles = build_fx_candles(bars, 240, FX_WEEK)
        self.assertEqual(candles.open_times[0], utc("2026-11-01T20:00"))
        self.assertTrue(candles.partial[0])
        self.assertEqual(candles.open[0], 108_000_000)  # the 22:00Z minute's open
        self.assertEqual(candles.end_ms[0], utc("2026-11-02T00:00"))
        self.assertEqual(len(candles), 1)  # the 00:00 bucket is incomplete at the series end

    def test_rejects_daily_candles(self):
        bars = series(utc("2026-10-14T12:00"), utc("2026-10-14T13:00"))
        with self.assertRaises(ValueError):
            build_fx_candles(bars, 1440, FX_WEEK)


class NoWeekendResetTests(unittest.TestCase):
    def test_indicators_run_in_trading_time(self):
        start = utc("2026-10-04T21:00")  # Sunday open (EDT)
        state = [12345]

        hourly = {}

        def price_of(t):
            hour = t - t % HOUR
            if hour not in hourly:
                state[0] = (state[0] * 1103515245 + 12345) % 2 ** 31
                previous = hourly[max(hourly)] if hourly else 108_000_000
                hourly[hour] = previous + (state[0] % 2001 - 1000) * 1000
            return hourly[hour]

        trading_hours, t = 0, start
        while trading_hours < 400:  # find the end of 400 open hours
            if FX_WEEK.is_open(t):
                trading_hours += 1
            t += HOUR
        bars = series(start, t, price_of=price_of)
        fx = build_fx_candles(bars, 60, FX_WEEK)
        self.assertEqual(len(fx), 400)
        self.assertEqual(sum(fx.valid), 400)  # every non-weekend hour is a valid candle
        self.assertGreater(fx.open_times[-1] - fx.open_times[0], 399 * HOUR)  # weekends were skipped, not filled
        plain = CandleSeries("EURUSD", 60, start, list(fx.open), list(fx.high), list(fx.low), list(fx.close),
                             [True] * 400)
        for name in ("ema_cross_20_50", "rsi_14_reversion", "keltner_breakout_20_14_2"):
            fx_sides, plain_sides = ta.strategy_sides(name, fx), ta.strategy_sides(name, plain)
            self.assertEqual(fx_sides, plain_sides, name)
            first = ta.first_signal_index(name)
            self.assertFalse(any(fx_sides[:first]), name)  # warm-up counted in candles, not wall-clock
        self.assertTrue(any(ta.strategy_sides("ema_cross_20_50", fx)))


if __name__ == "__main__":
    unittest.main()
