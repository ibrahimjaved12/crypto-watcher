"""TA baseline crosses: exact firing, warm-up, reset, no look-ahead, label grid, guarded loading."""
import random
from tempfile import TemporaryDirectory
import unittest

from market_analysis import data_lake
from market_analysis.benchmark.bars import MISSING
from market_analysis.benchmark.candles import CandleSeries
from market_analysis.benchmark.hidden_guard import HiddenStretchLocked
from market_analysis.benchmark.labels import LabelParams
from market_analysis.benchmark.market_data import load_symbol_bars
from market_analysis.benchmark.ta_strategies import (
    GRID_STEP, STRATEGIES, VERSION, all_specs, first_signal_index, make_specs, strategy_sides,
)

START = data_lake.month_bounds_ms("2024-01")[0]


def series(closes, minutes=60, symbol="BTCUSDT", spread=10 ** 6, start_ms=START):
    valid = [c is not None for c in closes]
    close = [MISSING if c is None else c for c in closes]
    high = [MISSING if c is None else c + spread for c in closes]
    low = [MISSING if c is None else c - spread for c in closes]
    return CandleSeries(symbol, minutes, start_ms, list(close), high, low, close, valid)


def walk(seed, length, x=10 ** 10, step=10 ** 8):
    rng = random.Random(seed)
    out = []
    for _ in range(length):
        x = max(step, x + rng.randint(-step, step))
        out.append(x)
    return out


class StrategyTests(unittest.TestCase):
    def test_ema_crosses_fire_once_per_cross(self):
        closes = [10 ** 10] * 500 + [2 * 10 ** 10] * 150 + [10 ** 10] * 150
        for name in ("ema_cross_20_50", "ema_cross_50_200"):
            sides = strategy_sides(name, series(closes))
            self.assertEqual([(i, s) for i, s in enumerate(sides) if s],
                             [(500, 1), (650 + [i for i, s in enumerate(sides[650:]) if s][0], -1)])
            self.assertEqual(sides.count(1), 1)
            self.assertEqual(sides.count(-1), 1)

    def test_rsi_crosses_fire_once_per_cross(self):
        closes, x = [], 10 ** 10
        for step in [10 ** 6, -10 ** 6] * 50 + [-10 ** 7] * 30 + [10 ** 7] * 60 + [-10 ** 7] * 60:
            x += step
            closes.append(x)
        sides = strategy_sides("rsi_14_reversion", series(closes))
        fired = [(i, s) for i, s in enumerate(sides) if s]
        self.assertEqual([s for _, s in fired], [1, -1])
        self.assertTrue(130 <= fired[0][0] < 190 <= fired[1][0])  # long in the rise, short in the fall

    def test_no_signal_before_warmup_or_after_reset(self):
        closes = walk(7, 4000)
        k = 2000
        closes[k] = None
        for name in STRATEGIES:
            sides = strategy_sides(name, series(closes))
            first = first_signal_index(name)
            with self.subTest(name=name):
                self.assertTrue(all(s == 0 for s in sides[:first]))
                self.assertTrue(all(s == 0 for s in sides[k:k + 1 + first]))
                self.assertTrue(any(sides[first:k]))  # the strategies do fire on a random walk

    def test_no_look_ahead(self):
        closes = walk(8, 1200)
        rng = random.Random(9)
        for name in STRATEGIES:
            full = strategy_sides(name, series(closes))
            for t in (450, 800, 1100):
                changed = closes[:t + 1] + [rng.randint(10 ** 8, 3 * 10 ** 10) for _ in closes[t + 1:]]
                with self.subTest(name=name, t=t):
                    self.assertEqual(strategy_sides(name, series(changed))[:t + 1], full[:t + 1])
                    self.assertEqual(strategy_sides(name, series(closes[:t + 1])), full[:t + 1])

    def test_ta_grid_is_on_the_label_grid(self):
        # TA 4h signals stay on the hour (60) while the 4h label grid is 15 minutes (slice H1).
        self.assertEqual(GRID_STEP, {15: 5, 60: 15, 240: 60})
        for minutes, step in GRID_STEP.items():
            self.assertEqual(step % LabelParams().step(minutes), 0)

    def test_specs_window_grid_and_config(self):
        data = {}
        for offset, symbol in enumerate(("BTCUSDT", "ETHUSDT")):
            data[symbol] = {minutes: series(walk(10 + offset + minutes, 1200), minutes, symbol)
                            for minutes in (15, 60, 240)}
        first_ms, end_ms = START + 30 * 86_400_000, START + 120 * 86_400_000
        specs = all_specs(data, first_ms=first_ms, end_ms=end_ms)
        self.assertEqual(len(specs), 18)
        self.assertEqual(len({(spec.strategy_id, spec.config["timeframe_min"]) for spec in specs}), 18)
        for spec in specs:
            minutes = spec.config["timeframe_min"]
            self.assertEqual(spec.strategy_version, VERSION)
            self.assertEqual(spec.config["kind"], STRATEGIES[spec.strategy_id][0])
            for symbol, ms, side, horizon in spec.signals:
                self.assertEqual(horizon, minutes)
                self.assertTrue(first_ms <= ms < end_ms)
                self.assertEqual(ms % ({15: 5, 60: 15, 240: 60}[minutes] * 60_000), 0)
                self.assertEqual(ms % (LabelParams().step(minutes) * 60_000), 0)  # also a label row
                self.assertIn(side, (-1, 1))
        self.assertTrue(any(spec.signals for spec in specs))
        spec = make_specs(data, "rsi_14_reversion", 60, first_ms=START, end_ms=START + 10 ** 12)
        expected = sum(1 for symbol in data for s in strategy_sides("rsi_14_reversion", data[symbol][60]) if s)
        self.assertEqual(len(spec.signals), expected)
        with self.assertRaises(ValueError):
            make_specs(data, "unknown", 60, first_ms=first_ms, end_ms=end_ms)
        with self.assertRaises(ValueError):
            make_specs({"BTCUSDT": {60: data["ETHUSDT"][60]}}, "rsi_14_reversion", 60, first_ms=0, end_ms=1)

    def test_hidden_months_refused_without_token_before_any_file(self):
        with TemporaryDirectory() as directory:
            with self.assertRaises(HiddenStretchLocked):
                load_symbol_bars(directory, "BTCUSDT", "2025-12", "2026-01")
            with self.assertRaises(FileNotFoundError):  # guard passes for validation months; no file yet
                load_symbol_bars(directory, "BTCUSDT", "2025-11", "2025-12")


if __name__ == "__main__":
    unittest.main()
