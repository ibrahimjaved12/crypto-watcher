"""Six fixed textbook TA baselines as benchmark StrategySpecs (#182 slice F).

Evaluated on CLOSED candles only. Every rule is a stateless one-step cross: at
candle i it compares the published values at i - 1 and i (both must be
published, so no signal precedes warm-up or straddles an invalid candle). Up
cross: previous <= level and current > level; down cross mirrored. A signal at
candle i is emitted at its end_ms, the decision time of the label horizon equal
to the candle length; entry is the next minute's open (labels-v1), so nothing
after the decision time is used.

Each (strategy, timeframe) is one StrategySpec, version ``ta-v1``. These are
reference baselines with textbook parameters, not tuned strategies.
"""
from __future__ import annotations

from .. import data_lake
from .candles import TIMEFRAMES, CandleSeries
from .indicators import (SHIFT, atr, bollinger, ema, ema_warmup, macd, rsi, rsi_at_or_above, rsi_at_or_below,
                         wilder_warmup)
from .runner import StrategySpec

VERSION = "ta-v1"
GRID_STEP = {15: 5, 60: 15, 240: 60}  # label decision-time grid (minutes) per horizon


def _level_cross(values, level=0) -> list:
    sides = [0] * len(values)
    for index in range(1, len(values)):
        previous, current = values[index - 1], values[index]
        if previous is None or current is None:
            continue
        if previous <= level < current:
            sides[index] = 1
        elif previous >= level > current:
            sides[index] = -1
    return sides


def _difference(a, b) -> list:
    return [None if x is None or y is None else x - y for x, y in zip(a, b)]


def ema_cross(candles: CandleSeries, fast: int, slow: int) -> list:
    closes = candles.column("close")
    return _level_cross(_difference(ema(closes, fast), ema(closes, slow)))


def rsi_reversion(candles: CandleSeries, n: int, lower: int, upper: int) -> list:
    pairs = rsi(candles.column("close"), n)
    sides = [0] * len(pairs)
    for index in range(1, len(pairs)):
        previous, current = pairs[index - 1], pairs[index]
        if previous is None or current is None:
            continue
        was_low, is_low = rsi_at_or_below(*previous, lower), rsi_at_or_below(*current, lower)
        was_high, is_high = rsi_at_or_above(*previous, upper), rsi_at_or_above(*current, upper)
        if was_low is None or is_low is None:  # undefined RSI (no movement at all)
            continue
        if was_low and not is_low:      # prev <= lower, now > lower
            sides[index] = 1
        elif was_high and not is_high:  # prev >= upper, now < upper
            sides[index] = -1
    return sides


def macd_cross(candles: CandleSeries, fast: int, slow: int, signal: int) -> list:
    line, sig = macd(candles.column("close"), fast, slow, signal)
    return _level_cross(_difference(line, sig))


def bollinger_reversion(candles: CandleSeries, n: int, k: int) -> list:
    bands = bollinger(candles.column("close"), n, k)
    sides = [0] * len(candles)
    for index in range(1, len(candles)):
        was_below, is_below = bands.below_lower(index - 1), bands.below_lower(index)
        was_above, is_above = bands.above_upper(index - 1), bands.above_upper(index)
        if None in (was_below, is_below, was_above, is_above):
            continue
        if was_below and not is_below:    # back above the lower band
            sides[index] = 1
        elif was_above and not is_above:  # back below the upper band
            sides[index] = -1
    return sides


def keltner_breakout(candles: CandleSeries, n_ema: int, n_atr: int, k: int) -> list:
    closes = candles.column("close")
    middle = ema(closes, n_ema)
    width = atr(candles.column("high"), candles.column("low"), closes, n_atr)
    scaled = [None if c is None else c << SHIFT for c in closes]
    upper = [None if m is None or w is None else m + k * w for m, w in zip(middle, width)]
    lower = [None if m is None or w is None else m - k * w for m, w in zip(middle, width)]
    sides = [0] * len(closes)
    for index in range(1, len(closes)):
        values = (scaled[index - 1], scaled[index], upper[index - 1], upper[index], lower[index - 1], lower[index])
        if None in values:
            continue
        previous, current, up_previous, up_current, low_previous, low_current = values
        if previous <= up_previous and current > up_current:
            sides[index] = 1
        elif previous >= low_previous and current < low_current:
            sides[index] = -1
    return sides


# name -> (kind, parameters, rule, first candle index that can carry a signal)
STRATEGIES = {
    "ema_cross_20_50": ("ema_cross", {"fast": 20, "slow": 50}, ema_cross, ema_warmup(50)),
    "ema_cross_50_200": ("ema_cross", {"fast": 50, "slow": 200}, ema_cross, ema_warmup(200)),
    "rsi_14_reversion": ("rsi_reversion", {"n": 14, "lower": 30, "upper": 70}, rsi_reversion, wilder_warmup(14)),
    "macd_12_26_9": ("macd_cross", {"fast": 12, "slow": 26, "signal": 9}, macd_cross,
                     ema_warmup(26) + ema_warmup(9)),
    "bollinger_20_2_reversion": ("bollinger_reversion", {"n": 20, "k": 2}, bollinger_reversion, 20),
    "keltner_breakout_20_14_2": ("keltner_breakout", {"n_ema": 20, "n_atr": 14, "k": 2}, keltner_breakout,
                                 max(ema_warmup(20), wilder_warmup(14))),
}


def first_signal_index(name: str) -> int:
    """Run index of the first candle whose previous and current values are both published."""
    return STRATEGIES[name][3]


def strategy_sides(name: str, candles: CandleSeries) -> list:
    """+1 / -1 / 0 per candle; a value at i depends on candles 0..i only."""
    _, parameters, rule, _ = STRATEGIES[name]
    return rule(candles, **parameters)


def strategy_config(name: str, minutes: int) -> dict:
    kind, parameters, _, _ = STRATEGIES[name]
    return {"kind": kind, **parameters, "timeframe_min": minutes}


def make_specs(symbol_candles: dict, name: str, minutes: int, *, first_ms: int, end_ms: int) -> StrategySpec:
    """One StrategySpec: (symbol, candle end_ms, side, minutes) for signals in [first_ms, end_ms).

    Indicators run over the whole loaded history; only the window's signals are
    emitted. The label horizon equals the candle length.
    """
    if name not in STRATEGIES:
        raise ValueError(f"unknown TA strategy {name!r}")
    if type(minutes) is not int or minutes not in TIMEFRAMES:
        raise ValueError(f"timeframe must be one of {TIMEFRAMES}")
    if type(first_ms) is not int or type(end_ms) is not int or end_ms < first_ms:
        raise ValueError("signal window must be integer [first_ms, end_ms)")
    grid = GRID_STEP[minutes] * data_lake.MINUTE_MS
    signals = []
    for symbol in sorted(symbol_candles):
        candles = symbol_candles[symbol][minutes]
        if candles.symbol != symbol or candles.minutes != minutes:
            raise ValueError(f"{symbol} {minutes}m candles do not match their key")
        for index, side in enumerate(strategy_sides(name, candles)):
            signal_ms = candles.end_ms[index]
            if side and first_ms <= signal_ms < end_ms:
                if signal_ms % grid:
                    raise ValueError(f"{symbol} signal {signal_ms} is off the {minutes}m label grid")
                signals.append((symbol, signal_ms, side, minutes))
    return StrategySpec(name, VERSION, strategy_config(name, minutes), signals)


def all_specs(symbol_candles: dict, *, first_ms: int, end_ms: int) -> list:
    """The 18 baseline specs: six strategies x (15, 60, 240) minutes."""
    return [make_specs(symbol_candles, name, minutes, first_ms=first_ms, end_ms=end_ms)
            for name in STRATEGIES for minutes in TIMEFRAMES]
