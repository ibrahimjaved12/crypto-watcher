"""Effective number of trials for #182: N_eff = K**2 / sum_ij rho_ij**2.

Correlations come from exact integer covariance numerators. With column sums
``S_i`` and Gram entries ``G_ij = sum_t f_ti f_tj`` (an int64 integer matrix
product: exact and order independent, no BLAS), the centred numerator
``sum_t (T f_ti - S_i)(T f_tj - S_j) = T * (T G_ij - S_i S_j)``; the common factor
``T`` cancels in ``rho``, so ``c_ij = T G_ij - S_i S_j`` is formed in Python ints.
Magnitude bounds are checked before the int64 products and an OverflowError is
raised instead of wrapping. ``rho_ij = c_ij / sqrt(c_ii c_jj)`` is an elementwise
float division; no eigendecomposition is used.

A constant column (``c_ii == 0``) is uncorrelated with everything (``rho_ii = 1``,
others 0) and counts as its own independent trial. The result is clamped to
[1, K] and rounded to 9 decimals.
"""
from __future__ import annotations

import math

import numpy as np

_INT64_LIMIT = 2 ** 63


def effective_trials(f: np.ndarray) -> float:
    if not isinstance(f, np.ndarray) or f.dtype != np.int64 or f.ndim != 2:
        raise ValueError("f must be a (T, K) int64 array")
    T, K = f.shape
    if T < 2 or K < 1:
        raise ValueError(f"need T >= 2 and K >= 1, got T={T}, K={K}")
    largest = max(int(f.max()), -int(f.min()))
    if T * largest * largest >= _INT64_LIMIT:
        raise OverflowError(f"T * max|f|**2 = {T * largest * largest} exceeds int64; rescale the inputs")
    gram = f.T @ f        # exact int64 (bound checked above)
    sums = f.sum(axis=0)  # exact int64: |S| <= T * max|f| < T * max|f|**2 bound
    S = [int(value) for value in sums]
    c = [[T * int(gram[i, j]) - S[i] * S[j] for j in range(K)] for i in range(K)]
    roots = [math.sqrt(c[i][i]) for i in range(K)]
    squares = []
    for i in range(K):
        for j in range(K):
            if i == j:
                rho = 1.0
            elif c[i][i] == 0 or c[j][j] == 0:
                rho = 0.0
            else:
                rho = c[i][j] / roots[i] / roots[j]
            squares.append(rho * rho)
    n_eff = K * K / math.fsum(squares)
    return round(min(max(n_eff, 1.0), float(K)), 9)
