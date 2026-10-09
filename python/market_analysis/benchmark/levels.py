"""Point-in-time price levels for the level-reaction event study L0 (#185). Pure functions, integers.

Prices are integers scaled by 10**8 (the bars' scale). Every level carries
``valid_from_ms`` (the first minute open time at which it may be used: the level is
known then) and ``valid_to_ms`` (exclusive). A level is never used before
``valid_from_ms``.

Level sets:

- ``round``: a fixed grid per symbol (``ROUND_SPACING``). BTC tier 10000 / 5000 / 1000
  by divisibility of the price in USDT; every other symbol tier 1. Valid for the
  whole series.
- ``prev_day`` / ``prev_week``: high and low of the previous UTC day / UTC week
  (weeks start Monday 00:00 UTC), known from the start of the next period and valid
  for that next period only. A period with any missing or compromised minute
  produces no level (its extremes are not trustworthy).
- ``swing4h``: fractal swing high (low) on closed 4h candles: candle ``i``'s high is
  strictly above (low strictly below) every high (low) of candles ``i-n..i-1`` and
  ``i+1..i+n``; all ``2n+1`` candles must be valid (an invalid candle breaks the
  fractal). Known at the end of candle ``i+n``, valid for ``valid_days`` from then.

Placebos (same detector, artificial levels): shifted round grids with offset
``spacing * (0.3 + 0.4 * U_j)`` (``U_j`` from ``rng.u64_words``, 53-bit mantissa,
computed in exact integer arithmetic so every placebo grid is at least 0.3 spacing
away from the real grid), and for the other sets 20 offset levels
``L + d * sigma_240(valid_from) * L`` per real level, ``d`` in +/-0.5..+/-1.4, fixed at
the real level's creation and valid over the same interval.

``rng.u64_words`` forbids ``|`` in a stream name, so the round placebo stream is
``level-placebo:<SYMBOL>:round`` (the spec's ``level-placebo|<SYMBOL>|round`` with
``:`` separators).
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .. import data_lake
from .bars import COMPROMISED_FLAGS, MISSING, BarSeries
from .candles import CandleSeries
from .rng import u64_words

SCALE = 10 ** 8
LEVEL_SETS = ("round", "prev_day", "prev_week", "swing4h")
# Grid spacing in scaled price units: BTC 1000, ETH 100, BNB 50, SOL 10, XRP 0.10, DOGE 0.01 USDT.
ROUND_SPACING = {"BTCUSDT": 1000 * SCALE, "ETHUSDT": 100 * SCALE, "BNBUSDT": 50 * SCALE, "SOLUSDT": 10 * SCALE,
                 "XRPUSDT": SCALE // 10, "DOGEUSDT": SCALE // 100}
BTC_TIERS = (10000, 5000, 1000)
PLACEBO_COUNT = 20
PLACEBO_SEED = 20261009
PLACEBO_OFFSETS = tuple(sign * Fraction(tenths, 10) for tenths in range(5, 15) for sign in (1, -1))
SWING_N = 5
SWING_VALID_DAYS = 90
FOREVER_MS = 2 ** 62
_MINUTE = data_lake.MINUTE_MS
_DAY = 1440 * _MINUTE
_WEEK = 7 * _DAY
_SIGMA_SCALE = 10 ** 20


@dataclass(frozen=True)
class Level:
    set_name: str
    side_hint: str     # "grid", "high" or "low"
    price: int         # scaled by 10**8
    valid_from_ms: int
    valid_to_ms: int   # exclusive
    tier: int


def round_tier(symbol: str, price: int) -> int:
    if symbol != "BTCUSDT":
        return 1
    for tier in BTC_TIERS:
        if price % (tier * SCALE) == 0:
            return tier
    return 1


def round_levels(symbol: str, lo: int, hi: int, offset: int = 0, *, valid_from_ms: int = 0,
                 valid_to_ms: int = FOREVER_MS) -> list[Level]:
    """Grid levels ``offset + m * spacing`` inside [lo, hi] (scaled prices); placebo grids (offset != 0) are tier 1."""
    data_lake.validate_symbol(symbol)
    spacing = ROUND_SPACING[symbol]
    if type(offset) is not int or not 0 <= offset < spacing:
        raise ValueError("offset must be an int in [0, spacing)")
    first = -(-(lo - offset) // spacing)
    levels = []
    for m in range(first, (hi - offset) // spacing + 1):
        price = offset + m * spacing
        if price <= 0:
            continue
        tier = round_tier(symbol, price) if offset == 0 else 1
        levels.append(Level("round", "grid", price, valid_from_ms, valid_to_ms, tier))
    return levels


def placebo_round_grids(symbol: str, seed: int = PLACEBO_SEED, count: int = PLACEBO_COUNT) -> list[int]:
    """Offsets of the shifted placebo grids: spacing * (0.3 + 0.4 * U_j), j = 0..count-1, exact integers."""
    data_lake.validate_symbol(symbol)
    spacing = ROUND_SPACING[symbol]
    offsets = []
    for j in range(count):
        mantissa = int(u64_words(seed, f"level-placebo:{symbol}:round", j, 1)[0]) >> 11  # 53 bits: U = m / 2**53
        offsets.append(3 * spacing // 10 + (4 * spacing * mantissa) // (10 * 2 ** 53))
    return offsets


def _week_start(ms: int) -> int:
    day = ms // _DAY
    return (day - (day + 3) % 7) * _DAY  # 1970-01-01 was a Thursday


def prev_period_levels(bars: BarSeries, period: str = "day") -> list[Level]:
    """High/low of every complete, fully valid UTC day (week) of the bars, valid during the next period."""
    if period not in ("day", "week"):
        raise ValueError("period must be 'day' or 'week'")
    set_name = "prev_day" if period == "day" else "prev_week"
    length = _DAY if period == "day" else _WEEK
    if period == "day":
        start = bars.start_ms + (-bars.start_ms % _DAY)
    else:
        start = _week_start(bars.start_ms)
        if start < bars.start_ms:
            start += _WEEK
    levels = []
    high, low, flags = bars.high, bars.low, bars.flags
    period_start = start
    while period_start + length <= bars.end_ms:
        first = (period_start - bars.start_ms) // _MINUTE
        last = first + length // _MINUTE
        highs, lows = high[first:last], low[first:last]
        ok = MISSING not in highs and MISSING not in lows and not any(f & COMPROMISED_FLAGS for f in flags[first:last])
        if ok:
            valid_from, valid_to = period_start + length, period_start + 2 * length
            levels.append(Level(set_name, "high", max(highs), valid_from, valid_to, 1))
            levels.append(Level(set_name, "low", min(lows), valid_from, valid_to, 1))
        period_start += length
    return levels


def swing_levels(candles_4h: CandleSeries, n: int = SWING_N, valid_days: int = SWING_VALID_DAYS) -> list[Level]:
    """Fractal swing highs/lows, known at the end of candle i+n, valid for ``valid_days`` from then."""
    if candles_4h.minutes != 240:
        raise ValueError("swing levels use 4h candles")
    levels = []
    count = len(candles_4h)
    high, low, valid = candles_4h.high, candles_4h.low, candles_4h.valid
    for i in range(n, count - n):
        window = range(i - n, i + n + 1)
        if not all(valid[j] for j in window):
            continue
        known = candles_4h.end_ms[i + n]
        others = [j for j in window if j != i]
        if all(high[i] > high[j] for j in others):
            levels.append(Level("swing4h", "high", high[i], known, known + valid_days * _DAY, 1))
        if all(low[i] < low[j] for j in others):
            levels.append(Level("swing4h", "low", low[i], known, known + valid_days * _DAY, 1))
    return levels


def placebo_offset_levels(levels, sigma240_at_valid_from, offsets=PLACEBO_OFFSETS) -> list[list[Level]]:
    """Per real level, its placebo levels L + d * sigma_240 * L (sigma scaled by 10**20, exact rounding).

    ``sigma240_at_valid_from[i]`` is the real level's point-in-time sigma at its
    ``valid_from_ms`` (None: no placebo for that level). The offsets are fixed here,
    at creation, and the placebo levels share the real level's validity interval.
    """
    out = []
    for level, sigma in zip(levels, sigma240_at_valid_from):
        if sigma is None:
            out.append([])
            continue
        group = []
        for d in offsets:
            shift = Fraction(level.price * sigma, _SIGMA_SCALE) * d
            price = level.price + round(shift)
            if price > 0:
                group.append(Level(level.set_name, level.side_hint, price, level.valid_from_ms, level.valid_to_ms,
                                   level.tier))
        out.append(group)
    return out
