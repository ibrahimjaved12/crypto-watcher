"""Integer EWMA volatility for the #182 label engine."""
from __future__ import annotations

from array import array
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from fractions import Fraction
import random
import unittest

from market_analysis import data_lake
from market_analysis.benchmark.bars import MISSING, BarSeries
from market_analysis.benchmark.volatility import (
    LAMBDA_NUM, VAR_SCALE, build_variance, horizon_sigma,
)

START = data_lake.month_bounds_ms("2025-01")[0]
TICK = 10 ** 6


def series_from_closes(closes, flags=None, start=START):
    count = len(closes)
    columns = {name: array("q", closes) for name in ("open", "high", "low", "close",
                                                    "mark_open", "mark_high", "mark_low", "mark_close")}
    columns.update({name: array("q", [0] * count) for name in ("volume", "taker_buy_volume", "trades")})
    return BarSeries("BTCUSDT", start, count, flags=array("H", flags or [0] * count), **columns)


def random_walk(minutes, seed=1, price=1000 * 10 ** 8):
    rng = random.Random(seed)
    closes = []
    for _ in range(minutes):
        price += rng.randint(-30, 30) * TICK
        closes.append(price)
    return closes


def choppy(minutes, seed=2):
    """Closes drawn from a few fixed levels, so the exact Fraction oracle stays small and fast."""
    rng = random.Random(seed)
    levels = [(100_000 + offset) * TICK for offset in (-60, -35, -20, -5, 0, 10, 25, 40, 70)]
    return [rng.choice(levels) for _ in range(minutes)]


class LambdaTests(unittest.TestCase):
    def test_lambda_constants_match_decimal(self):
        with localcontext() as context:
            context.prec = 80
            for days, value in LAMBDA_NUM.items():
                exact = Decimal(2) ** 40 * (-(Decimal(2).ln()) / (288 * days)).exp()
                with self.subTest(days=days):
                    self.assertEqual(int(exact.to_integral_value(rounding=ROUND_HALF_EVEN)), value)


class VarianceTests(unittest.TestCase):
    def test_matches_fraction_recursion(self):
        closes = choppy(4 * 1440)
        result = build_variance(series_from_closes(closes), 1)
        lam = Fraction(LAMBDA_NUM[1], 2 ** 40)
        A = W = Fraction(0)
        checked = 0
        for j in range(1, len(closes) // 5):
            previous, current = closes[5 * j - 1], closes[5 * j + 4]
            A = lam * A + Fraction((current - previous) ** 2 * VAR_SCALE, previous * previous)
            W = lam * W + 1
            if result.variance[j] != MISSING:
                oracle = A / W
                self.assertLess(abs(Fraction(result.variance[j]) - oracle) / oracle, Fraction(1, 10 ** 9))
                checked += 1
        self.assertGreater(checked, 500)
        self.assertEqual(result.observations, len(closes) // 5 - 1)

    def test_warm_up_needs_one_half_life_of_returns(self):
        result = build_variance(series_from_closes(random_walk(2 * 1440)), 1)
        # Block 0 has no return, so block j has j returns: the first published value is block 288.
        self.assertTrue(all(value == MISSING for value in result.variance[:288]))
        self.assertNotEqual(result.variance[288], MISSING)
        self.assertTrue(all(value != MISSING for value in result.variance[288:]))

    def test_missing_or_compromised_close_skips_two_returns(self):
        closes = random_walk(2 * 1440)
        clean = build_variance(series_from_closes(closes), 1)
        minute = 5 * 100 + 4
        flags = [0] * len(closes)
        flags[minute] = data_lake.FLAG_NO_AGGTRADES
        compromised = build_variance(series_from_closes(closes, flags=flags), 1)
        self.assertEqual(compromised.observations, clean.observations - 2)
        missing = list(closes)
        missing[minute] = MISSING
        self.assertEqual(build_variance(series_from_closes(missing), 1).observations, clean.observations - 2)
        # Blocks without a return carry the previous value (here: still in warm-up, MISSING).
        self.assertEqual(compromised.variance[100], compromised.variance[99])
        self.assertEqual(compromised.variance[101], compromised.variance[100])

    def test_no_look_ahead(self):
        closes = random_walk(2 * 1440)
        base = build_variance(series_from_closes(closes), 1)
        j = 400
        changed = closes[:5 * j + 5] + [price * 2 for price in closes[5 * j + 5:]]
        perturbed = build_variance(series_from_closes(changed), 1)
        self.assertEqual(base.variance[:j + 1], perturbed.variance[:j + 1])
        self.assertNotEqual(base.variance[j + 1], perturbed.variance[j + 1])

    def test_horizon_sigma_and_validation(self):
        self.assertEqual(horizon_sigma(10 ** 16, 15), 1732050807568877293)  # isqrt(3e36) = floor(sqrt(3) * 1e18)
        self.assertEqual(horizon_sigma(4 * 10 ** 16, 5), 2 * 10 ** 18)  # sqrt(4e16 * 1 * 1e20)
        for var, horizon in ((MISSING, 15), (10 ** 16, 7), (10 ** 16, 0)):
            with self.subTest(var=var, horizon=horizon), self.assertRaises(ValueError):
                horizon_sigma(var, horizon)
        with self.assertRaises(ValueError):
            build_variance(series_from_closes(random_walk(20), start=START + 60_000), 1)
        with self.assertRaises(ValueError):
            build_variance(series_from_closes(random_walk(20)), 2)


if __name__ == "__main__":
    unittest.main()
