"""Order-flow family of-v1: exact robust z, state machine, cooldown, invalidation, funding hours, no look-ahead."""
from array import array
from fractions import Fraction
import random
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from market_analysis import data_lake
from market_analysis.benchmark import order_flow
from market_analysis.benchmark.bars import BarSeries
from market_analysis.benchmark.funding import FundingSeries
from market_analysis.benchmark.hidden_guard import HiddenStretchLocked
from market_analysis.benchmark.market_data import load_symbol_funding

START = data_lake.month_bounds_ms("2024-03")[0]
HOUR = 3_600_000
MINUTE = 60_000
V = 1000


def make_bars(taker_buy, volume=None, flags=None):
    minutes = len(taker_buy)
    volume = volume or [V] * minutes
    price = array("q", [10 ** 10] * minutes)
    columns = {name: price for name in ("open", "high", "low", "close", "mark_open", "mark_high", "mark_low",
                                        "mark_close")}
    return BarSeries("BTCUSDT", START, minutes, volume=array("q", volume), taker_buy_volume=array("q", taker_buy),
                     trades=array("q", [1] * minutes), flags=array("H", flags or [0] * minutes), **columns)


def funding(times=()):
    return FundingSeries(tuple(times), (8,) * len(times), (Fraction(0),) * len(times), START, START + 40 * 86_400_000)


def tb_for(x):
    """Taker-buy volume of a V-volume minute whose imbalance is x."""
    value = Fraction(V) * (1 + Fraction(x)) / 2
    assert value.denominator == 1
    return int(value)


TOH = dict(theta=Fraction(3, 2), decision_offset_min=15, history_days=30, min_history=500, cooldown_min=240,
           funding_guard_min=1)
CUM = dict(theta=Fraction(2), rearm_below=Fraction(1), window_min=60, decision_step_min=60, history_days=30,
           min_history=500, cooldown_min=240)
LINEAR_Z = lambda history, x, min_history: 10 * x  # noqa: E731  (isolates the state machine from the z-score)


def reference_median_mad(values):
    def median(items):
        items = sorted(items)
        n = len(items)
        return items[n // 2] if n % 2 else (items[n // 2 - 1] + items[n // 2]) / 2
    middle = median(values)
    return middle, median([abs(v - middle) for v in values])


class RobustZTests(unittest.TestCase):
    def test_known_median_mad_and_exact_z(self):
        values = [Fraction(v) for v in (1, 2, 3, 4, 100)]
        self.assertEqual(order_flow.median_mad(values), (3, 1))
        self.assertEqual(order_flow.robust_z(values, Fraction(5), 5), Fraction(10000, 7413))  # 2 / (14826/10000)
        self.assertIsNone(order_flow.robust_z(values, Fraction(5), 6))
        self.assertEqual(order_flow.median_mad([Fraction(v) for v in (1, 2, 4, 7)]), (3, Fraction(3, 2)))
        self.assertIsNone(order_flow.robust_z([Fraction(v) for v in (1, 1, 1, 2)], Fraction(9), 1))  # MAD 0
        self.assertEqual(order_flow.imbalance(1000, 750), Fraction(1, 2))
        self.assertIsNone(order_flow.imbalance(0, 0))

    def test_matches_brute_force(self):
        rng = random.Random(5)
        for n in range(1, 60):
            values = sorted(Fraction(rng.randint(-20, 20), rng.randint(1, 4)) for _ in range(n))
            self.assertEqual(order_flow.median_mad(values), reference_median_mad(values), values)


class TopOfHourTests(unittest.TestCase):
    def build(self):
        # hour j -> z of its HH:00 minute (10 x imbalance); signal at j h 15 m when |z| >= 3/2
        z = {1: 2, 2: 2, 5: -2, 9: 2, 10: 2, 11: 2, 15: 2}
        taker_buy = [V // 2] * (17 * 60)
        for hour, value in z.items():
            taker_buy[hour * 60] = tb_for(Fraction(value, 10))
        flags = [0] * len(taker_buy)
        flags[10 * 60] = data_lake.FLAG_NO_AGGTRADES  # compromised HH:00 minute: no signal
        times = (START + 9 * HOUR + MINUTE,             # within +-1 minute (inclusive): hour 9 excluded
                 START + 13 * HOUR - MINUTE - 1000,     # outside the guard
                 START + 15 * HOUR + MINUTE + 1000)     # outside the guard: hour 15 still signals
        return make_bars(taker_buy, flags=flags), funding(times)

    def test_signals_cooldown_compromised_and_funding_hours(self):
        bars, events = self.build()
        with patch.object(order_flow, "robust_z", LINEAR_Z):
            signals = order_flow.toh1m(bars, events, **TOH)
        self.assertEqual(signals, [(START + 1 * HOUR + 15 * MINUTE, 1),   # hour 2 inside cooldown
                                   (START + 5 * HOUR + 15 * MINUTE, -1),  # exactly 240 minutes later
                                   (START + 11 * HOUR + 15 * MINUTE, 1),  # 9 funding, 10 compromised
                                   (START + 15 * HOUR + 15 * MINUTE, 1)])

    def test_real_z_spike_and_history_excludes_funding_hours(self):
        rng = random.Random(11)
        taker_buy = [rng.randint(450, 550) for _ in range(40 * 60)]
        taker_buy[30 * 60] = V  # imbalance 1 at 30:00
        params = dict(TOH, history_days=2, min_history=20, cooldown_min=1)  # noise signals must not block the spike
        signals = order_flow.toh1m(make_bars(taker_buy), funding(), **params)
        self.assertIn((START + 30 * HOUR + 15 * MINUTE, 1), signals)
        self.assertTrue(all(ms >= START + 20 * HOUR + 15 * MINUTE for ms, _ in signals))  # 20 values first
        # A funding hour is no history value either: hour 30 sees hours 0..28 only.
        sizes = []

        def spy(history, x, min_history):
            sizes.append(len(history))
            return None

        with patch.object(order_flow, "robust_z", spy):
            order_flow.toh1m(make_bars(taker_buy), funding((START + 29 * HOUR,)), **params)
        # 39 calls (hours 0..39 minus hour 29); hour 30 sees 29 values, not 30
        self.assertEqual(sizes, list(range(39)))


class CumulativeTests(unittest.TestCase):
    Z = [0, 0, 25, 25, 0, 25, 0, 0, -25, -15, -15, -15, -15, -15, -25, 5, 25, 0, 0, 0, 0, None, 25, 0, 25, 0]

    def bars(self):
        taker_buy, flags = [], []
        for value in self.Z:
            taker_buy += [tb_for(Fraction(value or 0, 100))] * 60
            flags += [0] * 60
        flags[21 * 60 + 30] = data_lake.FLAG_NO_AGGTRADES  # hour 21's window is invalid
        return make_bars(taker_buy, flags=flags)

    def test_crossing_rearm_cooldown_and_restart(self):
        with patch.object(order_flow, "robust_z", LINEAR_Z):
            signals = order_flow.cum240(self.bars(), funding(), **CUM)
        # Hour j is decided at (j + 1) h. j2 crosses; j3 no crossing; j5 cooldown; j8 crosses (armed since j4);
        # j14 crosses while disarmed (|z| never < 1 since j8); j15 re-arms, j16 signals; j21 invalid so j22
        # has no previous value; j24 signals.
        self.assertEqual(signals, [(START + 3 * HOUR, 1), (START + 9 * HOUR, -1), (START + 17 * HOUR, 1),
                                   (START + 25 * HOUR, 1)])

    def test_default_window_is_240_minutes_and_invalidated_by_any_compromised_minute(self):
        taker_buy = [V // 2] * (8 * 60)
        flags = [0] * len(taker_buy)
        flags[60] = data_lake.FLAG_MARK_MISSING
        seen = []
        with patch.object(order_flow, "robust_z", lambda history, x, m: seen.append(x) or None):
            order_flow.cum240(make_bars(taker_buy, flags=flags), funding(), **dict(CUM, window_min=240))
        # decisions at 4h..8h; the 4h and 5h windows contain minute 60 -> only 6h, 7h, 8h are valid
        self.assertEqual(len(seen), 3)


class LookAheadTests(unittest.TestCase):
    def test_bars_after_minute_d_are_never_used(self):
        rng = random.Random(3)
        minutes = 10 * 1440
        taker_buy = [rng.randint(300, 700) for _ in range(minutes)]
        bars = make_bars(taker_buy)
        cases = ((order_flow.toh1m, dict(TOH, history_days=1, min_history=10)),
                 (order_flow.cum240, dict(CUM, window_min=240, history_days=1, min_history=10)))
        for rule, params in cases:
            full = rule(bars, funding(), **params)
            self.assertTrue(full)
            cut = full[len(full) // 2][0]
            e = (cut - START) // MINUTE  # first unobserved minute of the decision at cut
            changed = taker_buy[:e] + [rng.randint(0, V) for _ in range(minutes - e)]
            flags = [0] * e + [rng.choice((0, data_lake.FLAG_NO_AGGTRADES)) for _ in range(minutes - e)]
            again = rule(make_bars(changed, flags=flags), funding(), **params)
            with self.subTest(rule=rule.__name__):
                self.assertEqual([s for s in again if s[0] <= cut], [s for s in full if s[0] <= cut])


class SpecTests(unittest.TestCase):
    def test_config_window_and_grid(self):
        config = order_flow.strategy_config("of_cum240_4h", 240)
        self.assertEqual(config, {"kind": "cum240", "theta": "2", "rearm_below": "1", "window_min": 240,
                                  "decision_step_min": 60, "history_days": 30, "min_history": 500,
                                  "cooldown_min": 240, "mad_scale": "7413/5000", "timeframe_min": 240,
                                  "k_values": ["2"], "rr_indices": [1]})
        self.assertEqual(order_flow.strategy_config("of_toh1m_4h", 240)["theta"], "3/2")
        with self.assertRaises(ValueError):
            order_flow.symbol_signals("of_toh1m_4h", make_bars([500] * 60), funding(), 60, first_ms=0, end_ms=1,
                                      label_step_min=15)
        with self.assertRaises(ValueError):
            order_flow.symbol_signals("rsi_14_reversion", make_bars([500] * 60), funding(), 240, first_ms=0,
                                      end_ms=1, label_step_min=15)

    def test_funding_loader_is_guarded_before_reading(self):
        with TemporaryDirectory() as directory, self.assertRaises(HiddenStretchLocked):
            load_symbol_funding(directory, "BTCUSDT", "2025-12", "2026-01")


if __name__ == "__main__":
    unittest.main()
