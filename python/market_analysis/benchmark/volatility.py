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
from math import isqrt  # noqa: F401  (re-exported for tests)

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
    return _seasonal_from(series, window_days, _block_r2(series))


def _seasonal_from(series: BarSeries, window_days: int, r2) -> SeasonalSeries:
    """The seasonal recipe on any non-negative per-block integers (``r2`` or ``|r|``)."""
    if type(window_days) is not int or window_days < SEASONAL_MIN_DAYS:
        raise ValueError(f"window_days must be an int >= {SEASONAL_MIN_DAYS}")
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


# ---------------------------------------------------------------- robust scale (labels-v3, #220 P12)
#
# The squared-return EWMA is tail-dominated: with heavy-tailed returns it overstates the typical
# move. The robust model tracks the mean absolute 5-minute return instead (same half-lives, warm-up,
# fixed point and int64 guards), deseasonalised by slot factors computed on |r| with the same
# 28-day point-in-time recipe. Block scale s = EWMA(|r| / g_slot) / 0.797885 (E|Z| of a normal);
# horizon variance = sum over the horizon's blocks of (s * g_slot)^2 (independent increments).

ABS_SCALE = 10 ** 10                  # |r| scaled so that |r|**2 has the VAR_SCALE of r2
MEAN_ABS_NORMAL_PPM = 797_885         # E|Z| = sqrt(2 / pi), parts per million
HCAL_MEDIAN_ABS_NORMAL = 0.674490     # median of |Z|
HCAL_MIN_DAYS = 60
HCAL_CLIP = (0.5, 2.0)
HCAL_SCALE = 1_000_000                # c_h fixed point


def _block_abs(series: BarSeries):
    """|r| of every block scaled by ABS_SCALE (None without a return); same validity as ``_block_r2``."""
    if series.start_ms % BLOCK_MS:
        raise ValueError(f"series start {series.start_ms} is not aligned to a 5-minute block")
    close, flags = series.close, series.flags
    out = []
    previous = None
    for j in range(series.minutes // BLOCK_MINUTES):
        minute = BLOCK_MINUTES * j + BLOCK_MINUTES - 1
        price = close[minute]
        valid = price != MISSING and not flags[minute] & COMPROMISED_FLAGS
        out.append(abs(price - previous) * ABS_SCALE // previous if valid and previous is not None else None)
        previous = price if valid else None
    return out


def seasonal_factors_abs(series: BarSeries, window_days: int = SEASONAL_WINDOW_DAYS) -> SeasonalSeries:
    """``seasonal_factors`` on |r| instead of r2 (slot factors g of the robust model, mean 1)."""
    return _seasonal_from(series, window_days, _block_abs(series))


def build_scale_robust(series: BarSeries, half_life_days: int, factors: SeasonalSeries) -> VarianceSeries:
    """Per block the robust scale s (ABS_SCALE units) = EWMA(|r| * FACTOR_SCALE // g) / 0.797885.

    Same EWMA state, decay, warm-up and publication rule as ``build_variance`` (a block whose day has
    no factors carries no return); ``variance`` holds s, not a variance.
    """
    if half_life_days not in LAMBDA_NUM:
        raise ValueError(f"half_life_days must be one of {sorted(LAMBDA_NUM)}, got {half_life_days!r}")
    lam = LAMBDA_NUM[half_life_days]
    needed = BLOCKS_PER_DAY * half_life_days
    values = _block_abs(series)
    scale = array("q", [MISSING]) * len(values)
    A = W = n = 0
    published = MISSING
    cache_day, cache = None, None
    for j, value in enumerate(values):
        if value is not None:
            ms = series.start_ms + j * BLOCK_MS
            day = ms // DAY_MS
            if day != cache_day:
                cache_day, cache = day, factors.day_factors(day)
            if cache is not None:
                adjusted = value * FACTOR_SCALE // cache[(ms % DAY_MS) // (BLOCK_MS * BLOCKS_PER_SLOT)]
                A = ((lam * A) >> LAMBDA_SHIFT) + adjusted
                W = ((lam * W) >> LAMBDA_SHIFT) + (1 << LAMBDA_SHIFT)
                n += 1
                s = ((A << LAMBDA_SHIFT) // W) * 1_000_000 // MEAN_ABS_NORMAL_PPM
                if s > _INT64_MAX:
                    raise OverflowError(f"block {j}: robust scale {s} exceeds int64")
                published = s if n >= needed else MISSING
        scale[j] = published
    return VarianceSeries(half_life_days, scale, n)


def horizon_sigma_robust(scale: int, factors_day, entry_block: int, horizon_minutes: int) -> int:
    """Horizon sigma scaled by 10**20: sqrt(sum over the horizon's blocks of (s * g_slot)^2).

    Blocks past midnight use the entry day's factors (known at entry), as in the seasonal model.
    """
    if scale == MISSING or scale < 0:
        raise ValueError(f"scale unavailable: {scale!r}")
    if horizon_minutes < BLOCK_MINUTES or horizon_minutes % BLOCK_MINUTES:
        raise ValueError(f"horizon must be a positive multiple of 5 minutes, got {horizon_minutes!r}")
    if factors_day is None or len(factors_day) != SLOTS_PER_DAY:
        raise ValueError("factors of the entry day are unavailable")
    if type(entry_block) is not int or not 0 <= entry_block < BLOCKS_PER_DAY:
        raise ValueError(f"entry_block must be a block-of-day index, got {entry_block!r}")
    squares = sum((scale * factors_day[((entry_block + i) % BLOCKS_PER_DAY) // BLOCKS_PER_SLOT]) ** 2
                  for i in range(horizon_minutes // BLOCK_MINUTES))
    return isqrt(squares * VAR_SCALE // (FACTOR_SCALE * FACTOR_SCALE))


class _RunningMedian:
    """Streaming median with two heaps (O(log n) per insert)."""

    def __init__(self):
        import heapq
        self._heapq = heapq
        self.low, self.high = [], []  # max-heap (negated), min-heap

    def __len__(self):
        return len(self.low) + len(self.high)

    def add(self, value: float) -> None:
        heapq = self._heapq
        if not self.low or value <= -self.low[0]:
            heapq.heappush(self.low, -value)
        else:
            heapq.heappush(self.high, value)
        if len(self.low) > len(self.high) + 1:
            heapq.heappush(self.high, -heapq.heappop(self.low))
        elif len(self.high) > len(self.low):
            heapq.heappush(self.low, -heapq.heappop(self.high))

    def median(self) -> float:
        if len(self.low) > len(self.high):
            return -self.low[0]
        return (-self.low[0] + self.high[0]) / 2


def horizon_calibration(series: BarSeries, horizon_minutes: int, step_minutes: int, robust_sigma) -> dict:
    """{entry_ms: c_h (HCAL_SCALE fixed point) or None} for every entry on the ``step_minutes`` grid.

    c_h at entry t = median over every completed past window (entry e on the grid with
    open_time(e + h) <= t, robust sigma available, both opens valid) of
    |ln(open[e + h] / open[e])| / sigma_robust_h(e), divided by 0.674490 and clipped to [0.5, 2];
    None until the first counted window is at least 60 days before t. Expanding and point in time:
    ``robust_sigma(entry_ms) -> int | None`` gives sigma_robust_h (10**20 scale) at an entry.
    """
    from math import log

    minute_ms = 60_000
    step_ms = step_minutes * minute_ms
    first = series.start_ms + minute_ms
    first += -first % step_ms
    entries = range(first, series.end_ms, step_ms)
    opens, flags = series.open, series.flags

    def valid(index: int) -> bool:
        return 0 <= index < series.minutes and opens[index] != MISSING and not flags[index] & COMPROMISED_FLAGS

    pending = []  # (exit_ms, entry_ms, ratio) in exit order (entries are increasing)
    for entry_ms in entries:
        e = (entry_ms - series.start_ms) // minute_ms
        x = e + horizon_minutes
        if not (valid(e) and valid(x)):
            continue
        sigma = robust_sigma(entry_ms)
        if not sigma:
            continue
        ratio = abs(log(opens[x] / opens[e])) / (sigma / VAR_SCALE)
        pending.append((entry_ms + horizon_minutes * minute_ms, entry_ms, ratio))
    out = {}
    median = _RunningMedian()
    first_counted = None
    k = 0
    for entry_ms in entries:
        while k < len(pending) and pending[k][0] <= entry_ms:
            median.add(pending[k][2])
            if first_counted is None:
                first_counted = pending[k][1]
            k += 1
        if first_counted is None or entry_ms - first_counted < HCAL_MIN_DAYS * DAY_MS:
            out[entry_ms] = None
            continue
        c = min(max(median.median() / HCAL_MEDIAN_ABS_NORMAL, HCAL_CLIP[0]), HCAL_CLIP[1])
        out[entry_ms] = round(c * HCAL_SCALE)
    return out


# ---------------------------------------------------------------- path-extreme calibration (audit candidate)
#
# ``ewma-robust-ftcal`` (candidate, audit only; never used by the forward harness). hcal fixes the
# horizon multiplier on the MEDIAN of the terminal |ln(open[e+h]/open[e])| / sigma, which matches a
# Gaussian terminal median but not barrier hits, which depend on the path maximum and the tails. ftcal
# uses the PATH statistic M_e = max over the window's 1-minute bars of max(ln(high/open_e),
# -ln(low/open_e)) and the median of sup_{0<=s<=1}|W_s| of a standard Brownian motion.

FTCAL_MIN_DAYS = HCAL_MIN_DAYS
FTCAL_CLIP = HCAL_CLIP
FTCAL_SCALE = HCAL_SCALE


def sup_abs_cdf(x: float) -> float:
    """P(sup_{0<=s<=1} |W_s| <= x) = (4/pi) sum_{n>=0} (-1)^n / (2n+1) exp(-(2n+1)^2 pi^2 / (8 x^2))."""
    from math import exp, pi

    if x <= 0:
        return 0.0
    total = 0.0
    n = 0
    while True:
        term = exp(-((2 * n + 1) ** 2) * pi * pi / (8 * x * x)) / (2 * n + 1)
        total += -term if n % 2 else term
        if term < 1e-18 or n > 100_000:
            break
        n += 1
    return min(1.0, max(0.0, 4 / pi * total))


def sup_abs_median(tolerance: float = 1e-12) -> float:
    """Median of sup|W| on [0, 1]: P = 0.5 solved by bisection (computed, not remembered)."""
    low, high = 0.3, 5.0
    while high - low > tolerance:
        mid = (low + high) / 2
        if sup_abs_cdf(mid) < 0.5:
            low = mid
        else:
            high = mid
    return (low + high) / 2


FTCAL_SUP_ABS_MEDIAN = sup_abs_median()   # 1.148973258...


def path_extreme_log(open_e: int, highs, lows) -> float:
    """M_e = max(ln(max high / open_e), -ln(min low / open_e)) over one window's 1-minute bars (>= 0 when
    the window's range contains the open; the open's own minute is part of the window)."""
    from math import log

    return max(log(max(highs) / open_e), -log(min(lows) / open_e))


def path_calibration(series: BarSeries, horizon_minutes: int, step_minutes: int, robust_sigma) -> dict:
    """{entry_ms: c_h (FTCAL_SCALE fixed point) or None} on the ``step_minutes`` grid, like ``horizon_calibration``.

    For every completed past window (entry e on the grid with open_time(e + h) <= t, the opens at e and
    e + h valid, robust sigma available, and EVERY minute of [e, e + h) present and uncompromised so the
    path maximum is not understated) ratio_e = M_e / (sigma_robust_h(e) / VAR_SCALE). c_h(t) = median over
    those windows of ratio_e / FTCAL_SUP_ABS_MEDIAN, clipped to [0.5, 2]; None until the first counted
    window is at least 60 days before t. Expanding and point in time: a window enters the median only from
    the entry time equal to its exit (e + h), the same rule as ``horizon_calibration``.
    """
    import numpy as np

    minute_ms = 60_000
    step_ms = step_minutes * minute_ms
    first = series.start_ms + minute_ms
    first += -first % step_ms
    entries = range(first, series.end_ms, step_ms)
    n = series.minutes
    opens = np.frombuffer(series.open, dtype=np.int64)
    highs = np.frombuffer(series.high, dtype=np.int64)
    lows = np.frombuffer(series.low, dtype=np.int64)
    flags = np.frombuffer(series.flags, dtype=np.uint16)
    bad = (opens == MISSING) | (highs == MISSING) | (lows == MISSING) | ((flags & COMPROMISED_FLAGS) != 0)
    bad_prefix = np.concatenate(([0], np.cumsum(bad)))          # bad_prefix[i] = bad minutes in [0, i)

    candidates = []  # (entry_ms, e)
    for entry_ms in entries:
        e = (entry_ms - series.start_ms) // minute_ms
        x = e + horizon_minutes
        if e < 0 or x >= n or bad[x] or bad_prefix[x] - bad_prefix[e] != 0:
            continue
        sigma = robust_sigma(entry_ms)
        if not sigma:
            continue
        candidates.append((entry_ms, e, sigma))
    pending = []  # (exit_ms, entry_ms, ratio) in exit order
    if candidates:
        starts = np.array([e for _, e, _ in candidates], dtype=np.int64)
        extremes = np.empty(len(candidates))
        offsets = np.arange(horizon_minutes, dtype=np.int64)
        for lo in range(0, len(candidates), 4096):
            idx = starts[lo:lo + 4096, None] + offsets[None, :]
            open_e = opens[starts[lo:lo + 4096]].astype(np.float64)
            up = np.log(highs[idx].max(axis=1) / open_e)
            down = -np.log(lows[idx].min(axis=1) / open_e)
            extremes[lo:lo + 4096] = np.maximum(up, down)
        for (entry_ms, _, sigma), extreme in zip(candidates, extremes):
            pending.append((entry_ms + horizon_minutes * minute_ms, entry_ms, float(extreme) / (sigma / VAR_SCALE)))
    out = {}
    median = _RunningMedian()
    first_counted = None
    k = 0
    for entry_ms in entries:
        while k < len(pending) and pending[k][0] <= entry_ms:
            median.add(pending[k][2])
            if first_counted is None:
                first_counted = pending[k][1]
            k += 1
        if first_counted is None or entry_ms - first_counted < FTCAL_MIN_DAYS * DAY_MS:
            out[entry_ms] = None
            continue
        c = min(max(median.median() / FTCAL_SUP_ABS_MEDIAN, FTCAL_CLIP[0]), FTCAL_CLIP[1])
        out[entry_ms] = round(c * FTCAL_SCALE)
    return out
