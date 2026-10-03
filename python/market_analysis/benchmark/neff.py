"""Effective number of independent trials for #182 (Bailey and Lopez de Prado, 2014).

``N_eff = rho_bar + (1 - rho_bar) * N`` where ``rho_bar`` is the average
OFF-DIAGONAL pairwise correlation of the K trial series (clamped to [0, 1]) and
``N`` is the number of trials tried (default K; an experiment-log count may be
larger). The result is clamped to [1, N] and rounded to 9 decimals.

The earlier participation-ratio estimate ``K**2 / sum(rho_ij**2)`` is
ANTI-CONSERVATIVE and must not be used for the Deflated Sharpe Ratio: for
one-factor noise trials with pairwise correlation 0.5 and K = 1000 it gives
about 4, and the best of 1,000 pure-noise trials then passes DSR > 0.95 in about
40% of simulations (0.5% with raw N). This estimate measured 0.5% to 4% false
passes at correlations 0.5 and 0.8.

Computation (no K x K object, no float matrix product): exact integer column
sums ``S_k`` and squares ``G_kk``, ``c_kk = T * G_kk - S_k**2`` in Python ints (a
column is constant iff ``c_kk == 0``). For non-constant columns
``w_tk = (T * f_tk - S_k) / sqrt(T * c_kk)`` (centred exactly in int64), so
``rho_ij = sum_t w_ti * w_tj``. With ``u_t = fsum_k w_tk``, the sum of ``rho_ij`` over
all ordered pairs including i = j is ``fsum_t u_t**2``; subtracting the number of
non-constant columns leaves the off-diagonal sum. Constant columns are their own
independent trials: they contribute 0 to every correlation sum but still count
in the ``K * (K - 1)`` pairs. Memory is O(T * K).
"""
from __future__ import annotations

import math

import numpy as np

_INT64_LIMIT = 2 ** 63


def _exact_column_moments(f: np.ndarray) -> tuple[int, int, list[int], list[int]]:
    """(T, K, S_k, c_kk) as Python ints, with c_kk = T * sum f**2 - S_k**2 (0 iff constant)."""
    if not isinstance(f, np.ndarray) or f.dtype != np.int64 or f.ndim != 2:
        raise ValueError("f must be a (T, K) int64 array")
    T, K = f.shape
    if T < 2 or K < 1:
        raise ValueError(f"need T >= 2 and K >= 1, got T={T}, K={K}")
    largest = max(int(f.max()), -int(f.min()))
    if T * largest * largest >= _INT64_LIMIT:
        raise OverflowError(f"T * max|f|**2 = {T * largest * largest} exceeds int64; rescale the inputs")
    sums = [int(value) for value in f.sum(axis=0)]           # |S| <= T * max|f|: exact
    squares = [int(value) for value in (f * f).sum(axis=0)]  # <= T * max|f|**2: exact
    return T, K, sums, [T * squares[k] - sums[k] * sums[k] for k in range(K)]


def _check_n_trials(n_trials, K: int) -> int:
    if n_trials is None:
        return K
    if type(n_trials) is not int or n_trials < K:
        raise ValueError(f"n_trials must be an int >= K = {K}, got {n_trials!r}")
    return n_trials


def effective_trials(f: np.ndarray, n_trials: int | None = None) -> float:
    """``rho_bar + (1 - rho_bar) * n_trials`` clamped to [1, n_trials] (n_trials defaults to K)."""
    T, K, sums, centred_squares = _exact_column_moments(f)
    total = _check_n_trials(n_trials, K)
    if K == 1:
        # No pair to correlate: every one of the n_trials counts (1.0 for a single trial).
        return round(float(total), 9)
    varying = [k for k in range(K) if centred_squares[k] > 0]
    off_diagonal = 0.0
    if varying:
        centred = T * f[:, varying] - np.array([sums[k] for k in varying], dtype=np.int64)  # exact int64
        scale = np.array([math.sqrt(T * centred_squares[k]) for k in varying], dtype=np.float64)
        w = centred.astype(np.float64) / scale
        u = [math.fsum(row) for row in w.tolist()]
        off_diagonal = math.fsum(value * value for value in u) - len(varying)
    rho_bar = min(max(off_diagonal / (K * (K - 1)), 0.0), 1.0)
    n_eff = rho_bar + (1 - rho_bar) * total
    return round(min(max(n_eff, 1.0), float(total)), 9)
