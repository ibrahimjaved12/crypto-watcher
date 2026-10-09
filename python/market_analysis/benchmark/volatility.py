"""Point-in-time EWMA volatility of 5-minute returns for the #182 label engine. Integers only.

Blocks are 5 minutes (block ``j`` = minutes ``5j .. 5j+4``, its close is
``close[5j+4]``). A block close is valid when present and its minute carries no
``COMPROMISED_FLAGS`` bit; a return exists only between two valid consecutive
block closes: ``r2 = (c_j - c_{j-1})**2 * VAR_SCALE // c_{j-1}**2`` (squared
simple return scaled by 10**20).

State ``(A, W, n)`` starts at 0 and, on each block with a return, updates
``A = (LAM*A >> 40) + r2``, ``W = (LAM*W >> 40) + 2**40``, ``n += 1`` and
``var = (A << 40) // W``. ``A/W`` is the bias-corrected EWMA (no warm-up bias);
``LAM / 2**40`` is the per-block decay for a half-life of 1, 3 or 7 days. A
block without a return changes nothing (no decay). ``variance[j]`` is published
once ``n >= 288 * half_life_days`` (one half-life of observations), else
MISSING; blocks without a return carry the previous published value.

Point in time: ``variance[j]`` uses only closes up to the end of block ``j``.
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass
from fractions import Fraction
from math import isqrt

from .bars import COMPROMISED_FLAGS, MISSING, BarSeries

VAR_SCALE = 10 ** 20
LAMBDA_SHIFT = 40
BLOCK_MINUTES = 5
BLOCK_MS = BLOCK_MINUTES * 60_000
BLOCKS_PER_DAY = 288
# round(2**40 * 2**(-1 / (288 * days))): half-life of 1, 3 and 7 days in 5-minute steps.
LAMBDA_NUM = {1: 1096868547930, 3: 1098629894259, 7: 1099133655364}
_INT64_MAX = 2 ** 63 - 1


@dataclass(frozen=True)
class VarianceSeries:
    half_life_days: int
    variance: array      # array('q'), one value per 5-minute block, MISSING before warm-up
    observations: int    # number of returns that entered the EWMA


def build_variance(series: BarSeries, half_life_days: int) -> VarianceSeries:
    if half_life_days not in LAMBDA_NUM:
        raise ValueError(f"half_life_days must be one of {sorted(LAMBDA_NUM)}, got {half_life_days!r}")
    if series.start_ms % BLOCK_MS:
        raise ValueError(f"series start {series.start_ms} is not aligned to a 5-minute block")
    lam = LAMBDA_NUM[half_life_days]
    needed = BLOCKS_PER_DAY * half_life_days
    blocks = series.minutes // BLOCK_MINUTES
    close, flags = series.close, series.flags
    variance = array("q", [MISSING]) * blocks
    A = W = n = 0
    published = MISSING
    previous = None  # valid close of block j - 1, else None
    for j in range(blocks):
        minute = BLOCK_MINUTES * j + BLOCK_MINUTES - 1
        price = close[minute]
        valid = price != MISSING and not flags[minute] & COMPROMISED_FLAGS
        if valid and previous is not None:
            change = price - previous
            r2 = change * change * VAR_SCALE // (previous * previous)
            A = ((lam * A) >> LAMBDA_SHIFT) + r2
            W = ((lam * W) >> LAMBDA_SHIFT) + (1 << LAMBDA_SHIFT)
            n += 1
            var = (A << LAMBDA_SHIFT) // W
            if var > _INT64_MAX:
                raise OverflowError(f"block {j}: EWMA variance {var} exceeds int64")
            published = var if n >= needed else MISSING
        variance[j] = published
        previous = price if valid else None
    return VarianceSeries(half_life_days, variance, n)


def horizon_sigma(var: int, horizon_minutes: int) -> int:
    """Horizon volatility scaled by 10**20: isqrt(var * (horizon_minutes // 5) * VAR_SCALE)."""
    if var == MISSING or var < 0:
        raise ValueError(f"variance unavailable: {var!r}")
    if horizon_minutes < BLOCK_MINUTES or horizon_minutes % BLOCK_MINUTES:
        raise ValueError(f"horizon must be a positive multiple of 5 minutes, got {horizon_minutes!r}")
    return isqrt(var * (horizon_minutes // BLOCK_MINUTES) * VAR_SCALE)


# ---------------------------------------------------------------- intraday seasonality (labels-v2, #220)
#
# The plain EWMA above is left untouched (lb1 stays reproducible). The seasonal model splits
# variance into a level and an intraday shape: 48 UTC half-hour slots of 6 blocks each.

DAY_MS = 86_400_000
SLOTS_PER_DAY = 48
BLOCKS_PER_SLOT = BLOCKS_PER_DAY // SLOTS_PER_DAY
FACTOR_SCALE = 1_000_000          # factor 1.0 == FACTOR_SCALE; the 48 slots of a day sum to 48 * FACTOR_SCALE
SEASONAL_WINDOW_DAYS = 28
SEASONAL_MIN_DAYS = 14
VALID_DAY_BLOCKS = -(-9 * BLOCKS_PER_DAY // 10)  # ceil(90 % of 288) = 260 blocks with a return
_U_SCALE = 10 ** 12


@dataclass(frozen=True)
class SeasonalSeries:
    """Per UTC day (absolute day number ``ms // DAY_MS``), 48 slot factors scaled by FACTOR_SCALE.

    ``factors[(day - first_day) * 48 + slot]``; MISSING for a day without a factor. The factors of
    day D use only blocks of days < D, so they are known at 00:00 UTC of D.
    """

    first_day: int
    days: int
    window_days: int
    factors: array  # array('q')

    def day_factors(self, day: int):
        """The 48 factors of absolute day ``day`` (a tuple), or None when unavailable."""
        index = day - self.first_day
        if not 0 <= index < self.days:
            return None
        values = tuple(self.factors[index * SLOTS_PER_DAY:(index + 1) * SLOTS_PER_DAY])
        return None if values[0] == MISSING else values


def _block_r2(series: BarSeries):
    """r2 of every block (None when the block has no return), the same integers as build_variance."""
    if series.start_ms % BLOCK_MS:
        raise ValueError(f"series start {series.start_ms} is not aligned to a 5-minute block")
    close, flags = series.close, series.flags
    out = []
    previous = None
    for j in range(series.minutes // BLOCK_MINUTES):
        minute = BLOCK_MINUTES * j + BLOCK_MINUTES - 1
        price = close[minute]
        valid = price != MISSING and not flags[minute] & COMPROMISED_FLAGS
        if valid and previous is not None:
            change = price - previous
            out.append(change * change * VAR_SCALE // (previous * previous))
        else:
            out.append(None)
        previous = price if valid else None
    return out


def _normalise(sums, counts):
    """Integer factors averaging exactly FACTOR_SCALE (largest remainder, ties to the lower slot); None if
    a slot has no observation or a zero mean."""
    if any(count == 0 for count in counts):
        return None
    raw = [Fraction(total, count) for total, count in zip(sums, counts)]
    total = sum(raw)
    if total <= 0 or any(value == 0 for value in raw):
        return None
    exact = [value * SLOTS_PER_DAY * FACTOR_SCALE / total for value in raw]
    floors = [value.numerator // value.denominator for value in exact]
    remainder = SLOTS_PER_DAY * FACTOR_SCALE - sum(floors)
    order = sorted(range(SLOTS_PER_DAY), key=lambda s: (-(exact[s] - floors[s]), s))
    for slot in order[:remainder]:
        floors[slot] += 1
    return None if min(floors) <= 0 else floors


def seasonal_factors(series: BarSeries, window_days: int = SEASONAL_WINDOW_DAYS) -> SeasonalSeries:
    """Point-in-time intraday factors (see SeasonalSeries).

    For each previous day (of the ``window_days`` calendar days before D) with at least 90 % of its
    288 blocks carrying a return: u[block] = r2[block] / mean(r2 over that day's blocks with a
    return); slot value = mean of u over the slot's blocks and days; the 48 values are normalised
    to average exactly 1. MISSING until ``SEASONAL_MIN_DAYS`` qualifying days exist in the window.
    Compromised and missing blocks are skipped, never filled.
    """
    if type(window_days) is not int or window_days < SEASONAL_MIN_DAYS:
        raise ValueError(f"window_days must be an int >= {SEASONAL_MIN_DAYS}")
    r2 = _block_r2(series)
    first_day = series.start_ms // DAY_MS
    last_day = (series.start_ms + max(len(r2) - 1, 0) * BLOCK_MS) // DAY_MS
    days = last_day - first_day + 1 if r2 else 0
    blocks_by_day = [[] for _ in range(days)]
    for j, value in enumerate(r2):
        if value is None:
            continue
        ms = series.start_ms + j * BLOCK_MS
        blocks_by_day[ms // DAY_MS - first_day].append(((ms % DAY_MS) // (BLOCK_MS * BLOCKS_PER_SLOT), value))
    qualified = []  # per day: (slot sums of u scaled by _U_SCALE, slot counts), None when not qualifying
    for index, blocks in enumerate(blocks_by_day):
        total = sum(value for _, value in blocks)
        if len(blocks) < VALID_DAY_BLOCKS or total <= 0:
            qualified.append(None)
            continue
        sums, counts = [0] * SLOTS_PER_DAY, [0] * SLOTS_PER_DAY
        n = len(blocks)
        for slot, value in blocks:  # u = r2 * n / total, fixed point
            sums[slot] += value * n * _U_SCALE // total
            counts[slot] += 1
        qualified.append((sums, counts))
    factors = array("q", [MISSING]) * (days * SLOTS_PER_DAY)
    for index in range(days):
        window = [qualified[i] for i in range(max(0, index - window_days), index) if qualified[i] is not None]
        if len(window) < SEASONAL_MIN_DAYS:
            continue
        sums = [sum(day[0][s] for day in window) for s in range(SLOTS_PER_DAY)]
        counts = [sum(day[1][s] for day in window) for s in range(SLOTS_PER_DAY)]
        values = _normalise(sums, counts)
        if values is not None:
            factors[index * SLOTS_PER_DAY:(index + 1) * SLOTS_PER_DAY] = array("q", values)
    return SeasonalSeries(first_day, days, window_days, factors)


def build_variance_deseasonalised(series: BarSeries, half_life_days: int, factors: SeasonalSeries) -> VarianceSeries:
    """``build_variance`` fed ``r2 * FACTOR_SCALE // f`` (f = the factor of the block's day and slot).

    A block whose day has no factor carries no return (no update, no decay), so the EWMA level is
    never mixed with raw intraday-cycle returns. Same warm-up, publication and int64 guards.
    """
    if half_life_days not in LAMBDA_NUM:
        raise ValueError(f"half_life_days must be one of {sorted(LAMBDA_NUM)}, got {half_life_days!r}")
    lam = LAMBDA_NUM[half_life_days]
    needed = BLOCKS_PER_DAY * half_life_days
    r2_blocks = _block_r2(series)
    variance = array("q", [MISSING]) * len(r2_blocks)
    A = W = n = 0
    published = MISSING
    cache_day, cache = None, None
    for j, r2 in enumerate(r2_blocks):
        if r2 is not None:
            ms = series.start_ms + j * BLOCK_MS
            day = ms // DAY_MS
            if day != cache_day:
                cache_day, cache = day, factors.day_factors(day)
            if cache is not None:
                adjusted = r2 * FACTOR_SCALE // cache[(ms % DAY_MS) // (BLOCK_MS * BLOCKS_PER_SLOT)]
                A = ((lam * A) >> LAMBDA_SHIFT) + adjusted
                W = ((lam * W) >> LAMBDA_SHIFT) + (1 << LAMBDA_SHIFT)
                n += 1
                var = (A << LAMBDA_SHIFT) // W
                if var > _INT64_MAX:
                    raise OverflowError(f"block {j}: EWMA variance {var} exceeds int64")
                published = var if n >= needed else MISSING
        variance[j] = published
    return VarianceSeries(half_life_days, variance, n)


def horizon_sigma_seasonal(var_deseason: int, factors_day, entry_block: int, horizon_minutes: int) -> int:
    """Horizon volatility scaled by 10**20 under the seasonal model.

    ``entry_block`` is the block-of-day index (0..287) of the horizon's first block. Variance =
    var_deseason * sum of f[slot] over the horizon's blocks; blocks past midnight still use the
    entry day's factors (known at entry). Equals ``horizon_sigma`` when every factor is 1.
    """
    if var_deseason == MISSING or var_deseason < 0:
        raise ValueError(f"variance unavailable: {var_deseason!r}")
    if horizon_minutes < BLOCK_MINUTES or horizon_minutes % BLOCK_MINUTES:
        raise ValueError(f"horizon must be a positive multiple of 5 minutes, got {horizon_minutes!r}")
    if factors_day is None or len(factors_day) != SLOTS_PER_DAY:
        raise ValueError("factors of the entry day are unavailable")
    if type(entry_block) is not int or not 0 <= entry_block < BLOCKS_PER_DAY:
        raise ValueError(f"entry_block must be a block-of-day index, got {entry_block!r}")
    weight = sum(factors_day[((entry_block + i) % BLOCKS_PER_DAY) // BLOCKS_PER_SLOT]
                 for i in range(horizon_minutes // BLOCK_MINUTES))
    return isqrt(var_deseason * weight * VAR_SCALE // FACTOR_SCALE)
