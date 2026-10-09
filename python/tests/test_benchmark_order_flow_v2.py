"""Order-flow threshold ladder of-v2 (#188 of-v2a): replication of of-v1, threshold cells, config, family geometry."""
from array import array
from fractions import Fraction
import random
import unittest
from unittest.mock import patch

from market_analysis import data_lake
from market_analysis.benchmark import experiment_run as er
from market_analysis.benchmark import order_flow, order_flow_v2
from market_analysis.benchmark.bars import BarSeries
from market_analysis.benchmark.funding import FundingSeries

START = data_lake.month_bounds_ms("2024-03")[0]
MINUTE = 60_000
V = 1000
LINEAR_Z = lambda history, x, min_history: 10 * x  # noqa: E731  (z = 10 x window imbalance)


def make_bars(taker_buy):
    minutes = len(taker_buy)
    price = array("q", [10 ** 10] * minutes)
    columns = {name: price for name in ("open", "high", "low", "close", "mark_open", "mark_high", "mark_low",
                                        "mark_close")}
    return BarSeries("BTCUSDT", START, minutes, volume=array("q", [V] * minutes),
                     taker_buy_volume=array("q", taker_buy), trades=array("q", [1] * minutes),
                     flags=array("H", [0] * minutes), **columns)


def funding():
    return FundingSeries((), (), (), START, START + 40 * 86_400_000)


def tb_for(x):
    value = Fraction(V) * (1 + Fraction(x)) / 2
    assert value.denominator == 1
    return int(value)


def signals(name, bars):
    return order_flow_v2.symbol_signals(name, bars, funding(), 240, first_ms=START, end_ms=bars.end_ms,
                                        label_step_min=15)


class ReplicationTests(unittest.TestCase):
    def test_c20_reproduces_of_cum240_4h(self):
        self.assertEqual(order_flow_v2.STRATEGIES["of2_cum240_c20"][1], order_flow.STRATEGIES["of_cum240_4h"][1])
        self.assertIs(order_flow_v2.STRATEGIES["of2_cum240_c20"][2], order_flow.cum240)
        rng = random.Random(188)
        minutes = 24 * 1440  # 576 hourly decisions: more than min_history = 500
        taker_buy = [rng.randint(440, 560) for _ in range(minutes)]
        for hour in (530, 545, 560):  # strong one-sided hours late enough to have a full history
            for minute in range(hour * 60, hour * 60 + 120):
                taker_buy[minute] = 950
        bars = make_bars(taker_buy)
        expected = order_flow.symbol_signals("of_cum240_4h", bars, funding(), 240, first_ms=START,
                                             end_ms=bars.end_ms, label_step_min=15)
        self.assertTrue(expected)
        self.assertEqual(signals("of2_cum240_c20", bars), expected)


class ThresholdLadderTests(unittest.TestCase):
    def test_single_spike_of_height_2_7(self):
        # Hours 10..13 have window imbalance 0.27; the 240-minute window ending before hour 14 holds all of
        # it, so z (= 10 x imbalance) climbs 0.675, 1.35, 2.025, 2.7 and falls back below 1.
        taker_buy = [tb_for(0)] * (30 * 60)
        for minute in range(600, 840):
            taker_buy[minute] = tb_for(Fraction(27, 100))
        bars = make_bars(taker_buy)
        with patch.object(order_flow, "robust_z", LINEAR_Z):
            counts = {name: signals(name, bars) for name in order_flow_v2.STRATEGIES}
        self.assertEqual(counts["of2_cum240_c20"], [("BTCUSDT", START + 780 * MINUTE, 1, 240)])  # crosses at 2.025
        self.assertEqual(counts["of2_cum240_c25"], [("BTCUSDT", START + 840 * MINUTE, 1, 240)])  # crosses at 2.7
        self.assertEqual(counts["of2_cum240_c30"], [])
        self.assertEqual(counts["of2_cum240_c35"], [])

    def test_strategy_config_is_exact_text(self):
        thetas = {"of2_cum240_c20": "2", "of2_cum240_c25": "5/2", "of2_cum240_c30": "3", "of2_cum240_c35": "7/2"}
        self.assertEqual(sorted(order_flow_v2.STRATEGIES), sorted(thetas))
        for name, theta in thetas.items():
            config = order_flow_v2.strategy_config(name, 240)
            self.assertEqual(config, {"kind": "cum240", "theta": theta, "rearm_below": "1", "window_min": 240,
                                      "decision_step_min": 60, "history_days": 30, "min_history": 500,
                                      "cooldown_min": 240, "mad_scale": "7413/5000", "timeframe_min": 240,
                                      "k_values": ["2"], "rr_indices": [1, 2, 3]})
        spec = order_flow_v2.make_spec("of2_cum240_c35", 240, [])
        self.assertEqual(spec.strategy_version, "of-v2")
        with self.assertRaises(ValueError):
            order_flow_v2.symbol_signals("of_cum240_4h", make_bars([500] * 60), funding(), 240, first_ms=START,
                                         end_ms=START, label_step_min=15)
        with self.assertRaises(ValueError):
            order_flow_v2.symbol_signals("of2_cum240_c20", make_bars([500] * 60), funding(), 60, first_ms=START,
                                         end_ms=START, label_step_min=15)


class FamilyTests(unittest.TestCase):
    def question(self, **overrides):
        return er.make_question("of-v2a-240m", "threshold ladder", 240, list(order_flow_v2.STRATEGIES), seed=1,
                                label_revision=1, data_revision=1, created_utc="2026-10-09T00:00:00Z",
                                family="order-flow-v2", **overrides)

    def test_question_and_geometries(self):
        question = self.question()
        self.assertEqual((question["strategy_version"], question["k_values"], question["rr_indices"]),
                         ("of-v2", ["2"], [1, 2, 3]))
        with self.assertRaises(er.QuestionError):
            er.validate_question({**question, "rr_indices": [1]})
        geometries = er.build_geometries(question)
        self.assertEqual(len(geometries), 6 * 2 * 1 * 3)
        self.assertEqual({g.rr_index for g in geometries}, {1, 2, 3})
        self.assertEqual({g.horizon_min for g in geometries}, {240})
        with self.assertRaises(er.QuestionError):
            er.make_question("q", "h", 60, ["of2_cum240_c20"], seed=1, label_revision=1, data_revision=1,
                             created_utc="2026-10-09T00:00:00Z", family="order-flow-v2")

    def test_of_v1_family_is_unchanged(self):
        self.assertEqual(er.FAMILIES["order-flow"].geometry, {240: (("2",), (1,))})
        self.assertEqual(er.FAMILIES["order-flow"].strategy_version, "of-v1")
        self.assertEqual(er.FAMILIES["order-flow-v2"].geometry, {240: (("2",), (1, 2, 3))})


if __name__ == "__main__":
    unittest.main()
