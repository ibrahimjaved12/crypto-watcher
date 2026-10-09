"""Forward engine (#239 P10): collector adapter, signals, setups and outcomes against build_labels (synthetic)."""
from __future__ import annotations

from array import array
from fractions import Fraction
import random
import unittest

from market_analysis import data_lake
from market_analysis.benchmark.bars import MISSING, BarSeries
from market_analysis.benchmark.labels import LabelParams, build_labels
from market_analysis.benchmark.scan import next_compromised
from market_analysis.benchmark.volatility import build_variance_deseasonalised, seasonal_factors
from market_analysis.forward import bars_adapter as ba
from market_analysis.forward import signals as sg
from market_analysis.forward.outcomes import resolve_setup
from market_analysis.forward.setups import Setup, build_setup

START = data_lake.month_bounds_ms("2025-01")[0]
MINUTE = 60_000
TICK = 10 ** 6
PARAMS = LabelParams(horizons=(15,), half_life_days=((15, 1),), step_minutes=((15, 15),), k_grid=(Fraction(2),),
                     rr_grid=(Fraction(3, 2), Fraction(2)), sigma_model="ewma-seasonal")


def walk_bars(minutes, seed=7, start=START) -> BarSeries:
    rng = random.Random(seed)
    close = 100_000 * TICK
    o, h, l, c = [], [], [], []
    for minute in range(minutes):
        opened = close
        scale = 40 if 13 <= (minute // 60) % 24 < 16 else 15
        close = max(TICK, opened + rng.randint(-scale, scale) * TICK)
        o.append(opened)
        h.append(max(opened, close) + rng.randint(0, 5) * TICK)
        l.append(min(opened, close) - rng.randint(0, 5) * TICK)
        c.append(close)
    columns = [array("q", values) for values in (o, h, l, c)]
    zeros = [array("q", [0] * minutes) for _ in range(3)]
    return BarSeries("BTCUSDT", start, minutes, *columns, *zeros, *[array("q", values) for values in (o, h, l, c)],
                     array("H", [0] * minutes))


def truncated(bars: BarSeries, minutes: int) -> BarSeries:
    cut = [array(column.typecode, column[:minutes]) for column in
           (bars.open, bars.high, bars.low, bars.close, bars.volume, bars.taker_buy_volume, bars.trades,
            bars.mark_open, bars.mark_high, bars.mark_low, bars.mark_close, bars.flags)]
    return BarSeries(bars.symbol, bars.start_ms, minutes, *cut)


def row(open_ms, price=100.5, transport="rest"):
    return {"open_time_ms": open_ms, "close_time_ms": open_ms + MINUTE - 1, "open": price, "high": price + 1,
            "low": price - 1, "close": price, "volume": 2.5, "transport": transport,
            "source_event_at_ms": None if transport == "rest" else open_ms + MINUTE,
            "provider": "binance-usdm", "symbol": "BTCUSDT", "price_type": "trade", "timeframe_minutes": 1}


class AdapterTests(unittest.TestCase):
    def test_scaling_rule_is_shortest_repr_then_half_even(self):
        self.assertEqual(ba.to_scaled(0.1), 10_000_000)
        self.assertEqual(ba.to_scaled(65432.1), 6_543_210_000_000)
        self.assertEqual(ba.to_scaled(1.000000005), 100_000_000)   # ...0.5 -> even
        self.assertEqual(ba.to_scaled(1.000000015), 100_000_002)   # ...1.5 -> even
        for bad in (float("nan"), float("inf"), -1.0, True, "1"):
            with self.assertRaises(ba.CollectorRowError):
                ba.to_scaled(bad)

    def test_gaps_alignment_and_flags(self):
        first = START + 30 * MINUTE  # not a 4h boundary: the series starts at the boundary before it
        rows = [row(first), row(first + MINUTE, 101.25, "websocket"), row(first + 3 * MINUTE)]
        bars = ba.bars_from_collector_rows("BTCUSDT", rows)
        self.assertEqual((bars.start_ms, bars.minutes), (START, 34))
        self.assertEqual(bars.open[31], 10_125_000_000)
        self.assertEqual(bars.mark_low[31], bars.low[31])  # mark proxy
        self.assertEqual(bars.open[32], MISSING)
        self.assertEqual(bars.flags[32], ba.MISSING_MINUTE_FLAGS)
        self.assertEqual(bars.flags[31], ba.PRESENT_MINUTE_FLAGS)
        self.assertEqual(ba.missing_minutes(bars), 1)
        self.assertEqual(sorted(ba.candles_from_bars(bars)), [15, 60, 240])

    def test_rejected_rows(self):
        cases = {"alignment": [row(START + 1)], "order": [row(START + MINUTE), row(START)],
                 "provenance": [{**row(START), "source_event_at_ms": START}],
                 "identity": [{**row(START), "symbol": "ETHUSDT"}],
                 "ohlc": [{**row(START), "high": 50.0}], "empty": []}
        for code, rows in cases.items():
            with self.assertRaises(ba.CollectorRowError) as caught:
                ba.bars_from_collector_rows("BTCUSDT", rows)
            self.assertEqual(caught.exception.code, code)


class SignalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = walk_bars(40 * 1440, seed=3)

    def test_registry_and_idempotent_ids(self):
        self.assertEqual(sum(s.kind == "ta" for s in sg.FORWARD_STRATEGIES.values()),
                         sum(s.kind == "placebo" for s in sg.FORWARD_STRATEGIES.values()))
        ids = ["ema_cross_20_50:60", "placebo-v1:ema_cross_20_50:60"]
        window = (START + 35 * 86_400_000, START + 40 * 86_400_000)
        first, reasons = sg.generate_signals("BTCUSDT", self.bars, *window, ids)
        again, _ = sg.generate_signals("BTCUSDT", self.bars, *window, ids)
        self.assertEqual(first, again)
        self.assertTrue(first)
        self.assertEqual(len({s.signal_id for s in first}), len(first))
        signal = first[0]
        self.assertEqual(signal.signal_id, sg.signal_id(signal.strategy_id, signal.version, "BTCUSDT",
                                                        signal.signal_ms, signal.side))
        self.assertTrue(all(window[0] < s.signal_ms <= window[1] - MINUTE for s in first))
        self.assertEqual(reasons["placebo-v1:ema_cross_20_50:60"], "rate:trailing-30d")

    def test_insufficient_history_is_a_reason_not_a_fill(self):
        short = truncated(self.bars, 2 * 1440)
        signals, reasons = sg.generate_signals("BTCUSDT", short, START, short.end_ms, ["ema_cross_50_200:240"])
        self.assertEqual((signals, reasons), ([], {"ema_cross_50_200:240": "insufficient_history"}))

    def test_placebo_is_reproducible_and_matches_the_rate(self):
        strategy = sg.FORWARD_STRATEGIES["placebo-v1:rsi_14_reversion:15"]
        frozen = sg.ForwardStrategy(strategy.strategy_id, strategy.version, "placebo", strategy.name, 15, 15,
                                    strategy.matched_id, Fraction(1, 4))
        original = sg.FORWARD_STRATEGIES[frozen.strategy_id]
        sg.FORWARD_STRATEGIES[frozen.strategy_id] = frozen
        try:
            window = (START + 5 * 86_400_000, START + 40 * 86_400_000 - 2 * MINUTE)
            signals, reasons = sg.generate_signals("BTCUSDT", self.bars, *window, [frozen.strategy_id])
        finally:
            sg.FORWARD_STRATEGIES[frozen.strategy_id] = original
        decisions = (window[1] - window[0]) // sg.PLACEBO_STEP_MS
        self.assertAlmostEqual(len(signals) / decisions, 0.25, delta=0.05)
        self.assertNotIn(frozen.strategy_id, reasons)
        self.assertTrue(all(s.signal_ms % sg.PLACEBO_STEP_MS == 0 for s in signals))
        sides = [s.side for s in signals]
        self.assertTrue(0.35 < sides.count(1) / len(sides) < 0.65)
        self.assertEqual(sg.placebo_draw("x:15", "BTCUSDT", window[0]), sg.placebo_draw("x:15", "BTCUSDT", window[0]))


class LabelEquivalenceTests(unittest.TestCase):
    """Forward setups and resolutions equal build_labels (labels-v2) row for row on the same bars."""

    @classmethod
    def setUpClass(cls):
        cls.bars = walk_bars(17 * 1440, seed=5)
        cls.rows = [r for _, month in build_labels("BTCUSDT", cls.bars, ba_empty(cls.bars), PARAMS,
                                                   {"2025-01": TICK}) for r in month]
        cls.factors = seasonal_factors(cls.bars)
        cls.variance = {1: build_variance_deseasonalised(cls.bars, 1, cls.factors).variance}
        cls.nc = next_compromised(cls.bars)

    def setups_for(self, label_row):
        signal = sg.Signal("sig", "test:15", "test", "BTCUSDT", label_row.signal_ms, label_row.side, 15)
        return build_setup(signal, self.bars, self.factors, self.variance, tick=TICK, params=PARAMS,
                           next_comp=self.nc)

    def test_geometry_and_resolution_match_every_label_row(self):
        compared = traded = 0
        for label_row in self.rows:
            if label_row.status == "I":
                continue
            setups = self.setups_for(label_row)
            self.assertEqual(len(setups), 2)
            for index, setup in enumerate(setups):
                self.assertEqual(setup.status, label_row.status, label_row.signal_ms)
                if setup.status != "T":
                    continue
                self.assertEqual((setup.p0, setup.sigma, setup.d_ticks, setup.label_leverage),
                                 (label_row.p0, label_row.sigma, label_row.d_ticks, label_row.leverage))
                resolution = resolve_setup(setup, self.bars)
                pair = label_row.cells[index]
                self.assertEqual((resolution.net_ur, resolution.cost_ur, resolution.fund_ur, resolution.exit_offset),
                                 (pair.pess.net_ur, pair.pess.cost_ur, pair.pess.fund_ur, pair.pess.exit_offset))
                self.assertEqual(resolution.status, "ambiguous" if pair.opt else pair.pess.outcome)
                self.assertEqual(resolution.wallet_ur, label_row.wallet_ur)
                traded += 1
            compared += 1
        self.assertGreater(traded, 100)
        self.assertGreater(compared, 1500)

    def test_incremental_resolution_reaches_the_same_final_result(self):
        tradeable = [s for r in self.rows if r.status == "T" for s in self.setups_for(r)][:12]
        for setup in tradeable:
            full = resolve_setup(setup, self.bars)
            e = (setup.entry_ms - self.bars.start_ms) // MINUTE
            previous = None
            for cut in (e, e + 1, e + 7, e + 23, e + 59, e + 61):
                current = resolve_setup(setup, truncated(self.bars, min(cut, self.bars.minutes)), previous=previous)
                if previous is not None and previous.final:
                    self.assertIs(current, previous)  # a final resolution never changes
                if cut == e:
                    self.assertEqual(current.status, "pending")
                previous = current
            self.assertEqual(previous, full)

    def test_setup_round_trip_and_immutability(self):
        setup = next(s for r in self.rows if r.status == "T" for s in self.setups_for(r))
        self.assertEqual(Setup.from_dict(setup.to_dict()), setup)
        with self.assertRaises(ValueError):
            Setup.from_dict({**setup.to_dict(), "rr": "3"})
        with self.assertRaises(Exception):
            setup.stop = 1

    def test_ambiguous_minute_is_not_resolved_optimistically(self):
        p0 = 100_000 * TICK
        setup = Setup("a", "s", "test:15", "test", "BTCUSDT", 1, 15, START + MINUTE, START + MINUTE, "2", "3/2", "T", 1,
                      60, stop=p0 - 100 * TICK, target=p0 + 150 * TICK, p0=p0, tick=TICK, label_leverage=None)
        minutes = 10
        o = [p0] * minutes
        h = [p0 + TICK] * minutes
        l = [p0 - TICK] * minutes
        h[4], l[4] = p0 + 160 * TICK, p0 - 120 * TICK  # stop and target in one minute, open in between
        columns = [array("q", values) for values in (o, h, l, o)]
        bars = BarSeries("BTCUSDT", START, minutes, *columns, *[array("q", [0] * minutes) for _ in range(3)],
                         *[array("q", values) for values in (o, h, l, o)], array("H", [0] * minutes))
        resolution = resolve_setup(setup, bars)
        self.assertEqual(resolution.status, "ambiguous")
        self.assertLess(resolution.net_ur, 0)
        self.assertEqual(resolution.optimistic["outcome"], "T")
        self.assertGreater(resolution.optimistic["net_ur"], 0)


def ba_empty(bars):
    from market_analysis.forward.outcomes import empty_funding
    return empty_funding(bars)


if __name__ == "__main__":
    unittest.main()
