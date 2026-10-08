"""Fixed-point indicators against exact Fraction recursions, resets, warm-up and exact bands."""
from fractions import Fraction
import math
import random
import unittest

from market_analysis.benchmark.indicators import (
    ONE, atr, bollinger, ema, macd, rsi, rsi_at_or_above, rsi_at_or_below, wilder,
)

TOLERANCE = Fraction(1, 10 ** 5)


def walk(seed, length, start=10 ** 10, step=10 ** 7):
    rng = random.Random(seed)
    values, x = [], start
    for _ in range(length):
        x = max(step, x + rng.randint(-step, step))
        values.append(x)
    return values


def candles(seed, length):
    closes = walk(seed, length)
    rng = random.Random(seed + 1)
    highs = [c + rng.randint(0, 10 ** 7) for c in closes]
    lows = [c - rng.randint(0, 10 ** 7) for c in closes]
    return highs, lows, closes


def ref_ema(xs, n, start=0):
    out = [None] * len(xs)
    e = Fraction(sum(xs[start:start + n]), n)
    out[start + n - 1] = e
    for i in range(start + n, len(xs)):
        e += (xs[i] - e) * Fraction(2, n + 1)
        out[i] = e
    return out


def ref_wilder(xs, n):
    out = [None] * len(xs)
    a = Fraction(sum(xs[:n]), n)
    out[n - 1] = a
    for i in range(n, len(xs)):
        a = (a * (n - 1) + xs[i]) / n
        out[i] = a
    return out


class IndicatorTests(unittest.TestCase):
    def close(self, fixed, exact, floor=10):
        """Relative difference below 1e-5 (relative to max(|exact|, 10 price units) near zero)."""
        self.assertIsNotNone(fixed)
        scale = max(abs(exact), floor)
        self.assertLessEqual(abs(Fraction(fixed, ONE) - exact), TOLERANCE * scale)

    def test_ema_and_wilder_match_exact_recursions(self):
        xs = walk(1, 600)
        for n in (20, 50, 200):
            fixed, exact = ema(xs, n), ref_ema(xs, n)
            for i in range(len(xs)):
                if fixed[i] is not None:
                    self.close(fixed[i], exact[i])
            self.assertEqual(next(i for i, v in enumerate(fixed) if v is not None), 2 * (n + 1) - 1)
        fixed, exact = wilder(xs, 14), ref_wilder(xs, 14)
        self.assertEqual(next(i for i, v in enumerate(fixed) if v is not None), 55)
        for i in range(55, len(xs)):
            self.close(fixed[i], exact[i])

    def test_rsi_and_atr_match_exact_recursions(self):
        highs, lows, closes = candles(2, 500)
        changes = [b - a for a, b in zip(closes, closes[1:])]
        g = ref_wilder([max(d, 0) for d in changes], 14)
        l = ref_wilder([max(-d, 0) for d in changes], 14)
        pairs = rsi(closes, 14)
        self.assertEqual(next(i for i, v in enumerate(pairs) if v is not None), 55)
        for i in range(55, len(closes)):
            fixed_g, fixed_l = pairs[i]
            total = g[i - 1] + l[i - 1]
            self.close(fixed_g, g[i - 1], floor=total)
            self.close(fixed_l, l[i - 1], floor=total)
        ranges = [max(h - lo, abs(h - c), abs(lo - c)) for h, lo, c in zip(highs[1:], lows[1:], closes)]
        exact = ref_wilder(ranges, 14)
        fixed = atr(highs, lows, closes, 14)
        self.assertEqual(next(i for i, v in enumerate(fixed) if v is not None), 55)
        for i in range(55, len(closes)):
            self.close(fixed[i], exact[i - 1])

    def test_macd_matches_exact_recursion(self):
        xs = walk(3, 500)
        fast, slow = ref_ema(xs, 12), ref_ema(xs, 26)
        line = [None if a is None or b is None else a - b for a, b in zip(fast, slow)]
        signal = ref_ema(line, 9, start=25)
        fixed_line, fixed_signal = macd(xs)
        self.assertEqual(next(i for i, v in enumerate(fixed_line) if v is not None), 53)
        self.assertEqual(next(i for i, v in enumerate(fixed_signal) if v is not None), 73)
        for i in range(73, len(xs)):
            self.close(fixed_line[i], line[i])
            self.close(fixed_signal[i], signal[i])

    def test_invalid_candle_resets_every_indicator(self):
        highs, lows, closes = candles(4, 400)
        k = 150
        for column in (highs, lows, closes):
            column[k] = None
        tail = (highs[k + 1:], lows[k + 1:], closes[k + 1:])
        self.assertEqual(ema(closes, 20)[k + 1:], ema(tail[2], 20))
        self.assertIsNone(ema(closes, 20)[k])
        self.assertEqual(ema(closes, 20)[k + 41], None)  # warm-up restarts: 42 valid candles after k
        self.assertIsNotNone(ema(closes, 20)[k + 42])
        self.assertEqual(rsi(closes)[k + 1:], rsi(tail[2]))
        self.assertEqual(atr(highs, lows, closes)[k + 1:], atr(*tail))
        line, signal = macd(closes)
        self.assertEqual((line[k + 1:], signal[k + 1:]), macd(tail[2]))
        bands, fresh = bollinger(closes), bollinger(tail[2])
        self.assertEqual([bands.below_lower(i) for i in range(k + 1, 400)],
                         [fresh.below_lower(i) for i in range(400 - k - 1)])
        self.assertIsNone(bands.below_lower(k + 19))
        self.assertIsNotNone(bands.below_lower(k + 20))

    def test_bollinger_exact_test_agrees_with_floats(self):
        rng = random.Random(5)
        closes, x = [], 10 ** 10
        for _ in range(3000):
            x = max(10 ** 8, x + int(rng.gauss(0, 1) * 10 ** 7) + (int(rng.gauss(0, 1) * 10 ** 8) if rng.random() < 0.05 else 0))
            closes.append(x)
        bands = bollinger(closes)
        hits = {"below": 0, "above": 0}
        for i in range(19, len(closes)):
            window = closes[i - 19:i + 1]
            mean = sum(window) / 20
            sigma = math.sqrt(sum((v - mean) ** 2 for v in window) / 20)
            for name, exact, boundary in (("below", bands.below_lower(i), mean - 2 * sigma),
                                          ("above", bands.above_upper(i), mean + 2 * sigma)):
                if abs(closes[i] - boundary) <= 1e-9 * closes[i]:
                    continue
                expected = closes[i] < boundary if name == "below" else closes[i] > boundary
                self.assertEqual(exact, expected, (i, name))
                hits[name] += expected
        self.assertGreater(hits["below"], 0)
        self.assertGreater(hits["above"], 0)
        self.assertIsNone(bands.below_lower(18))

    def test_division_free_rsi_helpers(self):
        rng = random.Random(6)
        cases = [(3, 7), (7, 3), (0, 5), (5, 0)] + [(rng.randint(0, 10 ** 12), rng.randint(0, 10 ** 12))
                                                     for _ in range(2000)]
        for g, l in cases:
            value = Fraction(100 * g, g + l)
            for level in (30, 70, Fraction(101, 2)):
                self.assertEqual(rsi_at_or_below(g, l, level), value <= level)
                self.assertEqual(rsi_at_or_above(g, l, level), value >= level)
        self.assertTrue(rsi_at_or_below(3, 7, 30))  # exactly 30
        self.assertIsNone(rsi_at_or_below(0, 0, 30))
        self.assertTrue(all(v is None for v in rsi([10 ** 9] * 80)[:55]))
        self.assertEqual(rsi([10 ** 9] * 80)[60], (0, 0))  # flat: published but RSI undefined


if __name__ == "__main__":
    unittest.main()
