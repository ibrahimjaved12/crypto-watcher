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
