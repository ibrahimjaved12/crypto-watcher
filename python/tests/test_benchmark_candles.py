"""Epoch-aligned closed candles, validity and signal times."""
from array import array
import unittest

from market_analysis import data_lake
from market_analysis.benchmark.bars import MISSING, BarSeries
from market_analysis.benchmark.candles import CandleSeries, build_candles

START = data_lake.month_bounds_ms("2024-01")[0]  # 2024-01-01 00:00 UTC
HOUR = 3_600_000


def make_bars(minutes, start_ms=START, price=lambda i: 100_000 + (i * 37) % 101 * 10, flags=None):
    o = array("q", (price(i) for i in range(minutes)))
    c = array("q", (price(i) + 5 for i in range(minutes)))
    h = array("q", (max(a, b) + 3 for a, b in zip(o, c)))
    l = array("q", (min(a, b) - 3 for a, b in zip(o, c)))
    zeros = array("q", [0]) * minutes
    return BarSeries("BTCUSDT", start_ms, minutes, open=o, high=h, low=l, close=c, volume=zeros,
                     taker_buy_volume=zeros, trades=zeros, mark_open=o, mark_high=h, mark_low=l, mark_close=c,
                     flags=array("H", flags or [0] * minutes))


class CandleTests(unittest.TestCase):
    def test_epoch_alignment_and_ohlc(self):
        bars = make_bars(24 * 60 + 30)
        candles = build_candles(bars, 240)
        self.assertEqual(len(candles), 6)  # the trailing partial candle is not built
        self.assertEqual([(candles.open_ms(i) - START) // HOUR for i in range(3)], [0, 4, 8])
        self.assertEqual(candles.open[1], bars.open[240])
        self.assertEqual(candles.close[1], bars.close[479])
        self.assertEqual(candles.high[1], max(bars.high[240:480]))
        self.assertEqual(candles.low[1], min(bars.low[240:480]))
        self.assertTrue(all(candles.valid))
        with self.assertRaises(ValueError):
            build_candles(make_bars(600, start_ms=START + 60 * 60_000), 240)  # 01:00 is not a 4h boundary
        self.assertEqual(len(build_candles(make_bars(600, start_ms=START + HOUR), 60)), 10)
        with self.assertRaises(ValueError):
            build_candles(bars, 30)

    def test_signal_time_is_end_of_last_minute(self):
        bars = make_bars(120)
        for minutes in (15, 60):
            candles = build_candles(bars, minutes)
            for index in range(len(candles)):
                last_minute = (index + 1) * minutes - 1
                self.assertEqual(candles.end_ms[index], bars.open_time(last_minute) + 60_000)

    def test_any_compromised_or_missing_minute_invalidates(self):
        flags = [0] * 60
        flags[17] = data_lake.FLAG_MARK_MISSING
        candles = build_candles(make_bars(60, flags=flags), 15)
        self.assertEqual(candles.valid, [True, False, True, True])
        self.assertEqual(candles.close[1], MISSING)
        self.assertEqual(candles.column("close")[1], None)
        flags[17] = data_lake.FLAG_KLINE_VOLUME_DIFFERS  # informational flag, not compromising
        self.assertTrue(all(build_candles(make_bars(60, flags=flags), 15).valid))
        bars = make_bars(60)
        bars.low[50] = MISSING
        self.assertEqual(build_candles(bars, 15).valid, [True, True, True, False])

    def test_series_validation(self):
        with self.assertRaises(ValueError):
            CandleSeries("BTCUSDT", 60, START + 60_000, [1], [1], [1], [1], [True])
        with self.assertRaises(ValueError):
            CandleSeries("BTCUSDT", 60, START, [1], [1], [1], [1, 2], [True])


if __name__ == "__main__":
    unittest.main()
