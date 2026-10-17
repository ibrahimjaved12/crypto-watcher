"""Forex ATR geometry (#374): exact ATR, tick-rounded stop/target from p0, failure statuses, identity."""

from array import array
from datetime import datetime, timezone
import unittest

from market_analysis.benchmark.bars import MISSING
from market_analysis.forward.signals import FORWARD_STRATEGIES, make_signal
from market_analysis.fx.bars import FLAG_CLOSED, FLAG_MISSING_OPEN, FLAG_PRESENT, FxBarSeries
from market_analysis.fx.candles import build_fx_candles
from market_analysis.fx.costs import FX_COST_MODEL_V1, with_multiplier
from market_analysis.fx.setups import FX_GEOMETRY_PARAMS, build_fx_setups, fx_params_hash
from market_analysis.trading_calendar import FX_WEEK

MINUTE = 60_000
C = 108_000_000      # 1.08000
HALF_RANGE = 50_000  # candle high - low = 0.00100
SPREAD = 10_000      # 0.00010
STRATEGY = FORWARD_STRATEGIES["ema_cross_20_50:60"]


def utc(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()) * 1000


SIGNAL_MS = utc("2026-10-14T13:00")


def fixture(start=utc("2026-10-11T21:00"), end=utc("2026-10-14T18:00")):
    minutes = (end - start) // MINUTE
    cols = {name: array("q", [MISSING]) * minutes for name in ("open", "high", "low", "close", "spread", "tick_volume")}
    flags = array("H", [FLAG_CLOSED]) * minutes
    t = FX_WEEK.next_open_ms(start)
    while t < end:
        run_end = min(FX_WEEK.open_until_ms(t), end)
        for minute in range(t, run_end, MINUTE):
            o = (minute - start) // MINUTE
            cols["open"][o] = cols["close"][o] = C
            cols["high"][o], cols["low"][o] = C + HALF_RANGE, C - HALF_RANGE
            cols["spread"][o], cols["tick_volume"][o] = SPREAD, 1
            flags[o] = FLAG_PRESENT
        if run_end >= end:
            break
        t = FX_WEEK.next_open_ms(run_end)
    return FxBarSeries("fx:EURUSD", "exness-mt5-demo", "EURUSD", FX_WEEK.calendar_id, start, minutes, 1000,
                       flags=flags, **cols)


def setups(side=1, bars=None, model=FX_COST_MODEL_V1):
    bars = bars or fixture()
    candles = {60: build_fx_candles(bars, 60, FX_WEEK)}
    return build_fx_setups(make_signal(STRATEGY, "EURUSD", SIGNAL_MS, side), candles, bars, FX_WEEK, model)


class GeometryTests(unittest.TestCase):
    def test_long_and_short_levels(self):
        long_setups = setups(1)
        self.assertEqual([(s.k, s.rr, s.status) for s in long_setups], [("2", "3/2", "T"), ("2", "2", "T")])
        first, second = long_setups
        self.assertEqual(first.atr, 100_000 * 2 ** 20)
        self.assertEqual((first.d_ticks, first.tick, first.spread0), (200, 1000, SPREAD))
        self.assertEqual(first.p0, C + SPREAD)  # a long buys at the ask
        self.assertEqual(first.stop, first.p0 - 200_000)
        self.assertEqual(first.target, first.p0 + 300_000)
        self.assertEqual(second.target, second.p0 + 400_000)
        self.assertEqual(first.window_minutes, 240)
        short = setups(-1)[0]
        self.assertEqual(short.p0, C)  # a short sells at the bid
        self.assertEqual((short.stop, short.target), (C + 200_000, C - 300_000))
        self.assertIsNotNone(first.entry_fill)
        self.assertIsNotNone(first.risk_distance)

    def test_failure_statuses(self):
        few = setups(bars=fixture(start=utc("2026-10-14T10:00")))
        self.assertEqual({(s.status, s.reason) for s in few}, {("V", "atr_unavailable")})
        self.assertIsNone(few[0].p0)
        e = lambda bars: bars.index_of(SIGNAL_MS)
        missing = fixture()
        missing.flags[e(missing)] = FLAG_MISSING_OPEN
        self.assertEqual(setups(bars=missing)[0].status, "C")
        wide = fixture()
        wide.spread[e(wide)] = 50_000  # 5 spreads = 250000 > the 200000 stop
        self.assertEqual((setups(bars=wide)[0].status, setups(bars=wide)[0].reason), ("G", "stop_narrower_than_spreads"))
        off = fixture()
        off.open[e(off)] = C + 1
        self.assertEqual((setups(bars=off)[0].status, setups(bars=off)[0].reason), ("P", "open_off_tick_grid"))
        with self.assertRaises(ValueError):
            bars = fixture()
            build_fx_setups(make_signal(STRATEGY, "EURUSD", SIGNAL_MS + MINUTE, 1),
                            {60: build_fx_candles(bars, 60, FX_WEEK)}, bars, FX_WEEK, FX_COST_MODEL_V1)

    def test_identity(self):
        first, second = setups(1)
        self.assertNotEqual(first.setup_id, second.setup_id)
        self.assertEqual([s.setup_id for s in setups(1)], [first.setup_id, second.setup_id])
        self.assertNotEqual(fx_params_hash(FX_GEOMETRY_PARAMS, FX_COST_MODEL_V1),
                            fx_params_hash(FX_GEOMETRY_PARAMS, with_multiplier(FX_COST_MODEL_V1, 2)))
        self.assertNotEqual(setups(1, model=with_multiplier(FX_COST_MODEL_V1, 2))[0].setup_id, first.setup_id)
        self.assertEqual(first.to_dict()["geometry_version"], "fx-atr-v1")


if __name__ == "__main__":
    unittest.main()
