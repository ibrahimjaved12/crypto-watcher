"""Power projection before anything is frozen (#182 slice F).

Same normal-approximation formula as ``power.py``, projected to another
segment length: with z = z(1 - alpha/2) + z(power) and the development daily
sigma (sample, calendar days including zero days, R units),
  min detectable edge per trade at T days = z * sigma / sqrt(T) / trades_per_day,
  required days for an edge e per trade   = ceil((z * sigma / (e * trades_per_day))**2).
Everything is exact Fractions except the two normal quantiles: the standard
library offers them only as floats (``statistics.NormalDist``), so each is
converted to an exact Fraction once and nothing else touches a float. Square
roots are integer square roots at 10**-18 resolution.
"""
from __future__ import annotations

from fractions import Fraction
from math import ceil, isqrt
from statistics import NormalDist

from .evaluate import daily_moments, decimal_text
from .segments import segment_bounds_ms

DAY_MS = 86_400_000
HIDDEN_DAYS = (lambda bounds: (bounds[1] - bounds[0]) // DAY_MS)(segment_bounds_ms("hidden"))  # 273
_ROOT_SCALE = 10 ** 18


def _sqrt(value: Fraction) -> Fraction:
    """floor(sqrt(value) * 10**18) / 10**18 for a non-negative Fraction."""
    value = Fraction(value)
    if value < 0:
        raise ValueError("square root of a negative value")
    return Fraction(isqrt(value.numerator * _ROOT_SCALE ** 2 // value.denominator), _ROOT_SCALE)


def z_value(alpha=Fraction(1, 20), power=Fraction(4, 5)) -> Fraction:
    alpha, power = Fraction(alpha), Fraction(power)
    if not 0 < alpha < 1 or not 0 < power < 1:
        raise ValueError("alpha and power must be in (0, 1)")
    quantile = NormalDist().inv_cdf
    value = Fraction(quantile(float(1 - alpha / 2))) + Fraction(quantile(float(power)))
    if value <= 0:
        raise ValueError("power must exceed alpha/2")
    return value


def project_power(daily, trades_per_day, target_days: int, smallest_edge_r=Fraction(1, 10),
                  alpha=Fraction(1, 20), power=Fraction(4, 5)) -> dict:
    """Project a development daily series to a target segment of target_days calendar days."""
    if type(target_days) is not int or target_days < 1:
        raise ValueError("target_days must be a positive integer")
    rate, edge = Fraction(trades_per_day), Fraction(smallest_edge_r)
    if rate < 0 or edge <= 0:
        raise ValueError("trades_per_day must be >= 0 and smallest_edge_r > 0")
    values, _, variance = daily_moments(daily)
    z = z_value(alpha, power)
    sigma = _sqrt(variance) if variance is not None else None
    mde = required = None
    if sigma is not None and rate:
        mde = z * sigma / _sqrt(Fraction(target_days)) / rate
        required = ceil((z * sigma / (edge * rate)) ** 2)
    return {"development_days": len(values), "target_days": target_days,
            "trades_per_day": decimal_text(rate), "sigma_daily_r": decimal_text(sigma),
            "z": decimal_text(z), "mde_per_trade_r": decimal_text(mde),
            "smallest_edge_r": decimal_text(edge), "required_days": required,
            "detectable": mde is not None and mde <= edge,
            "alpha": decimal_text(Fraction(alpha)), "power": decimal_text(Fraction(power))}


def project_selection(selection, daily, target_segment_days: int = HIDDEN_DAYS, **options) -> dict:
    """project_power for a C1 Selection and its development daily series (rate as in power_gate)."""
    if not len(daily):
        raise ValueError("daily series is empty")
    return project_power(daily, Fraction(len(selection), len(daily)), target_segment_days, **options)


def judgeable_report(results: dict, target_segment_days: int = HIDDEN_DAYS, **options) -> dict:
    """{spec name: projection} for {name: (selection, development daily)}, plus the judgeable names.

    Use it on the 18 TA baselines' development runs to see which could be judged
    on the 273-day hidden stretch at their development trade rate.
    """
    projections = {name: project_selection(selection, daily, target_segment_days, **options)
                   for name, (selection, daily) in sorted(results.items())}
    return {"target_days": target_segment_days, "projections": projections,
            "judgeable": [name for name, row in projections.items() if row["detectable"]]}
