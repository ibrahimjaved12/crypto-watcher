"""Hansen's SPA test and White's Reality Check for #182.

Inputs are daily net results per trial as a (T days, K trials) int64 matrix in
units of 1e-6 R (higher is better), and an optional benchmark series (default
zero: "no edge net of costs"). ``d = f - f0`` is formed exactly in int64.

Determinism: every sum is an exact integer sum, the bootstrap variance comes
from exact Python-int moments of the integer bootstrap sums, and floats appear
only in elementwise IEEE operations and ``sqrt``. No float matrix product or
float reduction enters the verdict, so results do not depend on BLAS, thread
count or sharding. Randomness is the counter-based stationary bootstrap.

Statistics (Hansen 2005):
- ``dbar_k = sum_t d / T``; ``dstar[b, k]`` is replicate ``b``'s mean;
  ``omega_k = sqrt(T) * std_b(dstar[:, k])`` (population std);
  ``t_k = sqrt(T) * dbar_k / omega_k``.
- ``T_spa = max(0, max_k t_k)``. Recentering ``g_k``: lower ``max(dbar_k, 0)``,
  consistent ``dbar_k`` if ``t_k >= -sqrt(2 ln ln T)`` else 0, upper ``dbar_k``;
  ``T*_b = max(0, max_k sqrt(T) * (dstar[b, k] - g_k) / omega_k)``.
- Reality Check (unstudentized): ``T_rc = max(0, max_k sqrt(T) * dbar_k)`` and
  ``T*_b = max(0, max_k sqrt(T) * (dstar[b, k] - dbar_k))``.
- p-value = count(T*_b > T_obs) / B. The headline is ``p_consistent``.
  Boundary rule: when ``T_obs == 0`` (no trial beats the benchmark, or no
  non-degenerate trial exists) there is nothing to reject and the p-value is
  1.0; without it, all-zero bootstrap statistics would give p = 0.

A trial with ``omega_k == 0`` (constant net result) is degenerate: its t-stat is
0 and it is excluded from every maximum, so it never rejects.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

from .bootstrap import bootstrap_indices, bootstrap_mean_matrix, default_mean_block

_INT64_LIMIT = 2 ** 63
MIN_DAYS = 16  # ln(ln(T)) > 0 for the consistent recentering


@dataclass(frozen=True)
class _Studentized:
    T: int
    K: int
    B: int
    mean_block: int
    dbar: np.ndarray       # (K,) float64
    dstar: np.ndarray      # (B, K) float64
    omega: np.ndarray      # (K,) float64
    t: np.ndarray          # (K,) float64; 0 for degenerate trials
    valid: np.ndarray      # (K,) bool; False for degenerate trials


def _max_abs(array: np.ndarray) -> int:
    return max(int(array.max()), -int(array.min())) if array.size else 0


def _studentize(f, f0, B: int, mean_block, seed: int, stream_prefix: str) -> _Studentized:
    """Shared path for SPA and StepM: exact sums, bootstrap means, omega and t-stats."""
    if not isinstance(f, np.ndarray) or f.dtype != np.int64 or f.ndim != 2:
        raise ValueError("f must be a (T, K) int64 array")
    T, K = f.shape
    if T < MIN_DAYS or K < 1:
        raise ValueError(f"need T >= {MIN_DAYS} days and K >= 1 trials, got T={T}, K={K}")
    if f0 is None:
        f0 = np.zeros(T, dtype=np.int64)
    if not isinstance(f0, np.ndarray) or f0.dtype != np.int64 or f0.shape != (T,):
        raise ValueError("f0 must be a (T,) int64 array")
    if _max_abs(f) + _max_abs(f0) >= _INT64_LIMIT // 2:
        raise OverflowError("f - f0 could exceed int64")
    if type(B) is not int or B < 1:
        raise ValueError(f"B must be an int >= 1, got {B!r}")
    mean_block = default_mean_block(T) if mean_block is None else mean_block
    d = f - f0[:, None]
    if _max_abs(d) * T >= _INT64_LIMIT:
        raise OverflowError("column sums could exceed int64")
    sums = d.sum(axis=0)                       # exact int64
    indices = bootstrap_indices(T, mean_block, seed, stream_prefix, range(B))
    star_sums = bootstrap_mean_matrix(d, indices)  # exact int64 (B, K)
    dbar = sums.astype(np.float64) / T
    dstar = star_sums.astype(np.float64) / T
    # Exact bootstrap variance of the sums from Python-int moments, then one correctly
    # rounded division: omega_k = sqrt(var_b(sum) / T) = sqrt(T) * std_b(mean).
    exact = star_sums.astype(object)
    first, second = exact.sum(axis=0), (exact * exact).sum(axis=0)
    omega = np.array([math.sqrt((B * int(second[k]) - int(first[k]) ** 2) / (B * B * T)) for k in range(K)],
                     dtype=np.float64)
    valid = omega > 0
    root_t = math.sqrt(T)
    t = np.zeros(K, dtype=np.float64)
    t[valid] = root_t * dbar[valid] / omega[valid]
    return _Studentized(T, K, B, mean_block, dbar, dstar, omega, t, valid)


def _p_value(statistics: np.ndarray, observed: float) -> float:
    if observed <= 0.0:
        return 1.0  # nothing beats the benchmark: never a rejection (see module docstring)
    return int(np.count_nonzero(statistics > observed)) / len(statistics)


def _studentized_max(stats: _Studentized, centre: np.ndarray) -> np.ndarray:
    """max(0, max over valid k of sqrt(T) * (dstar[b, k] - centre_k) / omega_k) per replicate."""
    if not stats.valid.any():
        return np.zeros(stats.B, dtype=np.float64)
    columns = math.sqrt(stats.T) * (stats.dstar[:, stats.valid] - centre[stats.valid]) / stats.omega[stats.valid]
    return np.maximum(columns.max(axis=1), 0.0)


@dataclass(frozen=True)
class SpaResult:
    t_stats: tuple
    dbar: tuple
    omega: tuple
    statistic: float
    rc_statistic: float
    p_lower: float
    p_consistent: float
    p_upper: float
    p_reality_check: float
    T: int
    K: int
    B: int
    mean_block: int
    seed: int
    stream_prefix: str

    def to_record(self) -> dict:
        """Every float rendered with repr(), so the record can be stored and hashed as text."""
        return {
            "t_stats": [repr(value) for value in self.t_stats],
            "dbar": [repr(value) for value in self.dbar],
            "omega": [repr(value) for value in self.omega],
            "statistic": repr(self.statistic),
            "rc_statistic": repr(self.rc_statistic),
            "p_lower": repr(self.p_lower),
            "p_consistent": repr(self.p_consistent),
            "p_upper": repr(self.p_upper),
            "p_reality_check": repr(self.p_reality_check),
            "T": self.T, "K": self.K, "B": self.B, "mean_block": self.mean_block,
            "seed": self.seed, "stream_prefix": self.stream_prefix,
        }


def spa_test(f, f0=None, *, B: int = 10_000, mean_block: int | None = None, seed: int,
             stream_prefix: str) -> SpaResult:
    """SPA (lower / consistent / upper) and Reality Check p-values; headline ``p_consistent``."""
    stats = _studentize(f, f0, B, mean_block, seed, stream_prefix)
    root_t = math.sqrt(stats.T)
    valid = stats.valid
    statistic = max(0.0, float(stats.t[valid].max())) if valid.any() else 0.0
    rc_statistic = max(0.0, float((root_t * stats.dbar[valid]).max())) if valid.any() else 0.0

    lower = np.maximum(stats.dbar, 0.0)
    threshold = -math.sqrt(2.0 * math.log(math.log(stats.T)))
    consistent = np.where(stats.t >= threshold, stats.dbar, 0.0)
    upper = stats.dbar

    if valid.any():
        rc_star = np.maximum((root_t * (stats.dstar[:, valid] - stats.dbar[valid])).max(axis=1), 0.0)
    else:
        rc_star = np.zeros(stats.B, dtype=np.float64)

    return SpaResult(
        t_stats=tuple(float(value) for value in stats.t),
        dbar=tuple(float(value) for value in stats.dbar),
        omega=tuple(float(value) for value in stats.omega),
        statistic=statistic,
        rc_statistic=rc_statistic,
        p_lower=_p_value(_studentized_max(stats, lower), statistic),
        p_consistent=_p_value(_studentized_max(stats, consistent), statistic),
        p_upper=_p_value(_studentized_max(stats, upper), statistic),
        p_reality_check=_p_value(rc_star, rc_statistic),
        T=stats.T, K=stats.K, B=stats.B, mean_block=stats.mean_block, seed=seed, stream_prefix=stream_prefix,
    )
