"""Point-in-time fixed-point indicators on closed candles (#182 slice F).

Integers only. EMA/Wilder/ATR/MACD values are scaled by ``2**SHIFT`` (price
units of the bars, themselves 10**8-scaled ints); every update floors exactly
once, so results are deterministic on every platform. Inputs are lists with
``None`` for an invalid candle (``CandleSeries.column``). Outputs have the same
length with ``None`` wherever no value is published.

Reset: an invalid candle resets ALL indicator state. ``_per_run`` splits the
inputs into maximal runs of valid candles and computes every indicator on each
run from scratch, so no state can cross an invalid candle.

Warm-up rule: a value is published only after ``ceil(4 / alpha)`` consecutive
valid candles from the (re)start, i.e. on run index ``ceil(4 / alpha) - 1``.
With alpha = 2/(n+1) for an EMA that is 2(n+1) candles (EMA20: 42, EMA50: 102,
EMA200: 402); with alpha = 1/n for Wilder averages (RSI, ATR) 4n candles (56 for
n = 14). After 4/alpha candles the seed's weight is below e**-4 (about 2%), so
the published value no longer depends materially on where the run started.
MACD sums its stages: the line is published after EMA26's 54 candles, the
signal after 54 + EMA9's 20 = 74 candles. Bollinger bands are a plain window and
are published once the 20-candle window is full of valid candles.

Each value uses inputs up to and including its own index only (no look-ahead).
"""
from __future__ import annotations

from fractions import Fraction

SHIFT = 20
ONE = 1 << SHIFT


def ema_warmup(n: int) -> int:
    """ceil(4 / alpha) candles with alpha = 2 / (n + 1)."""
    return 2 * (n + 1)


def wilder_warmup(n: int) -> int:
    """ceil(4 / alpha) candles with alpha = 1 / n."""
    return 4 * n


def _period(n) -> None:
    if type(n) is not int or n < 1:
        raise ValueError("indicator period must be a positive integer")


def _runs(*columns):
    """Maximal [start, end) index runs where every column is not None."""
    length = len(columns[0])
    if any(len(column) != length for column in columns):
        raise ValueError("indicator inputs must have equal lengths")
    start = None
    for index in range(length):
        present = all(column[index] is not None for column in columns)
        if present and start is None:
            start = index
        elif not present and start is not None:
            yield start, index
            start = None
    if start is not None:
        yield start, length


def _per_run(compute, *columns, outputs: int = 1):
    """Shared reset helper: compute(*run_slices) on each valid run, None elsewhere."""
    results = [[None] * len(columns[0]) for _ in range(outputs)]
    for start, end in _runs(*columns):
        parts = compute(*(column[start:end] for column in columns))
        if outputs == 1:
            parts = (parts,)
        for result, part in zip(results, parts):
            result[start:end] = part
    return results[0] if outputs == 1 else tuple(results)


def _gate(values: list, warmup: int) -> list:
    """Publish only from run index warmup - 1 on."""
    return [None if index < warmup - 1 else value for index, value in enumerate(values)]


def _ema_run(xs, n: int, shift: int = SHIFT) -> list:
    """Unpublished EMA of one run: integer SMA seed at index n - 1, then exact floored updates."""
    out = [None] * len(xs)
    if len(xs) < n:
        return out
    e = (sum(xs[:n]) << shift) // n
    out[n - 1] = e
    for index in range(n, len(xs)):
        e = e + (((xs[index] << shift) - e) * 2) // (n + 1)
        out[index] = e
    return out


def _wilder_run(xs, n: int) -> list:
    out = [None] * len(xs)
    if len(xs) < n:
        return out
    average = (sum(xs[:n]) << SHIFT) // n
    out[n - 1] = average
    for index in range(n, len(xs)):
        average = (average * (n - 1) + (xs[index] << SHIFT)) // n
        out[index] = average
    return out


def ema(values, n: int) -> list:
    """EMA(n) scaled by 2**SHIFT, published after ema_warmup(n) valid candles."""
    _period(n)
    return _per_run(lambda xs: _gate(_ema_run(xs, n), ema_warmup(n)), values)


def wilder(values, n: int) -> list:
    """Wilder average (alpha = 1/n) scaled by 2**SHIFT, published after wilder_warmup(n) valid inputs."""
    _period(n)
    return _per_run(lambda xs: _gate(_wilder_run(xs, n), wilder_warmup(n)), values)


def _rsi_run(closes, n: int) -> list:
    gains = [max(b - a, 0) for a, b in zip(closes, closes[1:])]
    losses = [max(a - b, 0) for a, b in zip(closes, closes[1:])]
    g, l = _wilder_run(gains, n), _wilder_run(losses, n)
    # Change j is between candles j and j + 1, so candle i uses averages up to change i - 1.
    pairs = [None] + [None if a is None else (a, b) for a, b in zip(g, l)]
    return _gate(pairs[:len(closes)], wilder_warmup(n))


def rsi(closes, n: int = 14) -> list:
    """(g, l) Wilder averages of gains and losses; RSI = 100 * g / (g + l), never divided here."""
    _period(n)
    return _per_run(lambda xs: _rsi_run(xs, n), closes)


def rsi_at_or_below(g: int, l: int, level) -> bool | None:
    """RSI <= level without division; None (undefined) when g + l == 0."""
    if g + l == 0:
        return None
    return 100 * g <= level * (g + l)


def rsi_at_or_above(g: int, l: int, level) -> bool | None:
    """RSI >= level without division; None (undefined) when g + l == 0."""
    if g + l == 0:
        return None
    return 100 * g >= level * (g + l)


def _macd_run(closes, fast: int, slow: int, signal: int):
    e_fast, e_slow = _ema_run(closes, fast), _ema_run(closes, slow)
    line = [None if a is None or b is None else a - b for a, b in zip(e_fast, e_slow)]
    first = slow - 1  # the line exists from the slow EMA's seed on
    sig = [None] * len(closes)
    if len(closes) > first:
        # The line is already scaled: EMA without a further shift, seeded by the SMA of its first values.
        sig[first:] = _ema_run(line[first:], signal, shift=0)
    line_warmup = max(ema_warmup(fast), ema_warmup(slow))
    return _gate(line, line_warmup), _gate(sig, line_warmup + ema_warmup(signal))


def macd(closes, fast: int = 12, slow: int = 26, signal: int = 9):
    """(line, signal) lists scaled by 2**SHIFT; line = EMA(fast) - EMA(slow)."""
    for n in (fast, slow, signal):
        _period(n)
    if fast >= slow:
        raise ValueError("MACD needs fast < slow")
    return _per_run(lambda xs: _macd_run(xs, fast, slow, signal), closes, outputs=2)


def _atr_run(highs, lows, closes, n: int) -> list:
    ranges = [max(h - l, abs(h - c), abs(l - c)) for h, l, c in zip(highs[1:], lows[1:], closes)]
    average = _wilder_run(ranges, n)
    return _gate([None] + average, wilder_warmup(n))


def atr(highs, lows, closes, n: int = 14) -> list:
    """Wilder ATR scaled by 2**SHIFT; true range from the second candle of a run."""
    _period(n)
    return _per_run(lambda h, l, c: _atr_run(h, l, c, n), highs, lows, closes)


class Bollinger:
    """Bollinger bands (population sigma) tested exactly, with no square root.

    With S = sum x and D = n * sum x**2 - S**2 over the n closes ending at i:
    c < mean - k * sigma  iff  S - n*c > 0 and (S - n*c)**2 > k**2 * D, and
    c > mean + k * sigma  iff  n*c - S > 0 and (n*c - S)**2 > k**2 * D.
    """

    def __init__(self, closes, n: int = 20, k=2):
        _period(n)
        if type(k) not in (int, Fraction) or k <= 0:
            raise ValueError("k must be a positive int or Fraction")
        self.n, self.k2 = n, Fraction(k) ** 2
        self.closes = list(closes)
        self.sums = _per_run(self._window_run, self.closes)

    def _window_run(self, xs) -> list:
        n, out = self.n, [None] * len(xs)
        total = squares = 0
        for index, x in enumerate(xs):
            total += x
            squares += x * x
            if index >= n:
                old = xs[index - n]
                total -= old
                squares -= old * old
            if index >= n - 1:
                out[index] = (total, n * squares - total * total)
        return out

    def _test(self, index: int, sign: int) -> bool | None:
        window = self.sums[index]
        if window is None:
            return None
        total, spread = window
        distance = sign * (self.n * self.closes[index] - total)
        return distance > 0 and distance * distance > self.k2 * spread

    def below_lower(self, index: int) -> bool | None:
        """Close strictly below the lower band; None when the band is not published."""
        return self._test(index, -1)

    def above_upper(self, index: int) -> bool | None:
        """Close strictly above the upper band; None when the band is not published."""
        return self._test(index, 1)


def bollinger(closes, n: int = 20, k=2) -> Bollinger:
    return Bollinger(closes, n, k)
