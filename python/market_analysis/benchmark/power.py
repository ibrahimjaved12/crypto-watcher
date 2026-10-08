"""Normal-approximation power on calendar-day R; overlap count is informational.

The gate uses the full daily horizon (including zeros) and sample daily sigma.
It does not replace T with the greedy independent-trade count. Quantiles and
square roots necessarily use floats; reported floating results use repr.
"""
from __future__ import annotations

from fractions import Fraction
from math import ceil, isfinite, sqrt
from statistics import NormalDist

from .evaluate import daily_moments, decimal_text

ALPHA = Fraction(1, 20)
POWER = Fraction(4, 5)


def _number(value, name: str, *, positive=False) -> float:
    value = float(value)
    if not isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'non-negative'}")
    return value


def _z(alpha, power) -> float:
    alpha, power = float(alpha), float(power)
    if not 0 < alpha < 1 or not 0 < power < 1:
        raise ValueError("alpha and power must be in (0, 1)")
    value = NormalDist().inv_cdf(1 - alpha / 2) + NormalDist().inv_cdf(power)
    if value <= 0:
        raise ValueError("power must exceed alpha/2")
    return value


def required_days(edge_per_day, sigma_daily, alpha=ALPHA, power=POWER) -> int:
    edge = _number(edge_per_day, "edge", positive=True)
    sigma = _number(sigma_daily, "sigma")
    return ceil((_z(alpha, power) * sigma / edge) ** 2)


def min_detectable_edge_per_day(T: int, sigma_daily, alpha=ALPHA, power=POWER) -> float:
    if type(T) is not int or T < 1:
        raise ValueError("T must be a positive integer")
    return _z(alpha, power) * _number(sigma_daily, "sigma") / sqrt(T)


def min_detectable_edge_per_trade(T: int, sigma_daily, trades_per_day, alpha=ALPHA, power=POWER) -> float:
    return min_detectable_edge_per_day(T, sigma_daily, alpha, power) / _number(trades_per_day, "trades_per_day", positive=True)


def n_independent_greedy(trades) -> int:
    """Earliest-exit interval scheduling per symbol, using closed minute ranges.

    A trade entering at the accepted exit minute overlaps and is excluded.
    X is included since the measured observation window still overlaps.
    """
    last_exit, count = {}, 0
    for trade in sorted(trades, key=lambda row: (row.exit_ms, row.signal_ms, row.symbol)):
        if trade.exit_ms < trade.signal_ms:
            raise ValueError("trade exit precedes entry")
        if trade.symbol not in last_exit or trade.signal_ms > last_exit[trade.symbol]:
            last_exit[trade.symbol] = trade.exit_ms
            count += 1
    return count


def power_gate(daily, trades_per_day, smallest_edge_r=Fraction(1, 10)) -> dict:
    rate = _number(trades_per_day, "trades_per_day")
    edge = _number(smallest_edge_r, "smallest_edge_r", positive=True)
    values, _, variance = daily_moments(daily)
    T = len(values)
    sigma = sqrt(variance) if variance is not None else None
    mde = min_detectable_edge_per_trade(T, sigma, rate) if sigma is not None and rate else None
    return {"passes": mde is not None and mde <= edge, "mde_per_trade": repr(mde) if mde is not None else None,
            "T_days": T, "sigma_daily_r": repr(sigma) if sigma is not None else None,
            "trades_per_day": decimal_text(trades_per_day), "smallest_edge_r": decimal_text(smallest_edge_r),
            "alpha": decimal_text(ALPHA), "power": decimal_text(POWER)}
