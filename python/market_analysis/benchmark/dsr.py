"""Deflated Sharpe Ratio (Bailey and Lopez de Prado) for #182. Standard library only.

All Sharpe ratios here are per-period (daily) ratios, never annualized.

- ``sharpe_stats``: mean, sample std (ddof=1), SR = mean/std, and the population
  standard skewness and NON-excess kurtosis (a normal distribution has 3).
- ``expected_max_sharpe``: the expected maximum Sharpe ratio of ``N``
  independent trials with Sharpe variance ``var_sharpe`` under the null,
  ``SR0 = sqrt(V) * ((1 - g) * Z(1 - 1/N) + g * Z(1 - 1/(N e)))`` with the
  Euler-Mascheroni constant ``g``. ``N`` may be fractional (an effective number
  of trials); for ``N <= 1`` there is no selection and ``SR0 = 0``.
- ``deflated_sharpe_ratio``: ``Phi((SR - SR0) * sqrt(n - 1) /
  sqrt(1 - skew * SR + (kurt - 1) / 4 * SR**2))``.
"""
from __future__ import annotations

import math
from statistics import NormalDist
from typing import Sequence

EULER_MASCHERONI = 0.5772156649015329
_NORMAL = NormalDist()


def sharpe_stats(series: Sequence[int | float]) -> tuple[float, float, float, float, float, int]:
    """(mean, std ddof=1, sr, skew, kurtosis_nonexcess, n); std == 0 raises ValueError."""
    values = [float(value) for value in series]
    n = len(values)
    if n < 2:
        raise ValueError(f"need at least 2 observations, got {n}")
    if not all(math.isfinite(value) for value in values):
        raise ValueError("series contains a non-finite value")
    mean = math.fsum(values) / n
    deviations = [value - mean for value in values]
    sum_squares = math.fsum(deviation * deviation for deviation in deviations)
    if sum_squares == 0:
        raise ValueError("series has zero standard deviation; the Sharpe ratio is undefined")
    std = math.sqrt(sum_squares / (n - 1))
    m2 = sum_squares / n
    m3 = math.fsum(deviation ** 3 for deviation in deviations) / n
    m4 = math.fsum(deviation ** 4 for deviation in deviations) / n
    return mean, std, mean / std, m3 / m2 ** 1.5, m4 / (m2 * m2), n


def expected_max_sharpe(n_trials: float, var_sharpe: float) -> float:
    """Expected maximum per-period Sharpe ratio of ``n_trials`` null trials (0.0 when n_trials <= 1)."""
    if not math.isfinite(n_trials) or n_trials <= 0:
        raise ValueError(f"n_trials must be a finite positive number, got {n_trials!r}")
    if not math.isfinite(var_sharpe) or var_sharpe < 0:
        raise ValueError(f"var_sharpe must be finite and >= 0, got {var_sharpe!r}")
    if n_trials <= 1:
        return 0.0
    first, second = 1 - 1 / n_trials, 1 - 1 / (n_trials * math.e)
    if not (0 < first < 1 and 0 < second < 1):
        raise ValueError(f"n_trials {n_trials!r} gives quantiles outside (0, 1)")
    g = EULER_MASCHERONI
    return math.sqrt(var_sharpe) * ((1 - g) * _NORMAL.inv_cdf(first) + g * _NORMAL.inv_cdf(second))


def deflated_sharpe_ratio(sr_hat: float, sr0: float, n_obs: int, skew: float, kurtosis_nonexcess: float) -> float:
    """Probability that the true per-period Sharpe ratio exceeds ``sr0`` given ``sr_hat``."""
    if kurtosis_nonexcess < 1:
        raise ValueError(f"kurtosis_nonexcess must be >= 1 (normal = 3; was excess kurtosis passed?), "
                         f"got {kurtosis_nonexcess!r}")
    if n_obs < 3:
        raise ValueError(f"n_obs must be >= 3, got {n_obs!r}")
    radicand = 1 - skew * sr_hat + (kurtosis_nonexcess - 1) / 4 * sr_hat ** 2
    if not radicand > 0:
        raise ValueError(f"non-positive variance term {radicand!r} for sr_hat={sr_hat!r}, skew={skew!r}")
    return _NORMAL.cdf((sr_hat - sr0) * math.sqrt(n_obs - 1) / math.sqrt(radicand))
