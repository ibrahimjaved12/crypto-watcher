"""Stationary bootstrap (Politis-Romano) with counter-based randomness for #182.

Replicate ``b`` uses stream ``f"{stream_prefix}/rep/{b}"``: words ``[0, T)`` are
start draws (``word % T``) and words ``[T, 2T)`` are restart draws. Position 0
always starts fresh; position ``t >= 1`` restarts iff its restart word is below
``floor(2**64 / mean_block)``, so a block continues with probability
``1 - 1/mean_block`` (geometric blocks with mean ``mean_block``). Indices wrap
circularly. Every replicate is a pure function of its coordinates, so a row
computed alone equals the same row in any batch or shard.

The mean block length defaults to ``default_mean_block``; the Politis-White
optimal block-length estimator is deliberately deferred to v2.
"""
from __future__ import annotations

from typing import Sequence

import numpy as np

from .rng import u64_words

_GATHER_BYTES = 64 * 1024 * 1024
_INT64_LIMIT = 2 ** 63


def _int_cbrt_ceil(value: int) -> int:
    """Smallest integer c with c**3 >= value (integer arithmetic only)."""
    low, high = 0, 1
    while high ** 3 < value:
        high *= 2
    while low < high:
        middle = (low + high) // 2
        if middle ** 3 >= value:
            high = middle
        else:
            low = middle + 1
    return low


def default_mean_block(T: int) -> int:
    """min(max(ceil(T**(1/3)), 5), max(T // 10, 1)), in integer arithmetic."""
    if type(T) is not int or T < 1:
        raise ValueError(f"T must be an int >= 1, got {T!r}")
    return min(max(_int_cbrt_ceil(T), 5), max(T // 10, 1))


def restart_threshold(mean_block: int) -> int:
    """floor(2**64 / mean_block) as an exact int (2**64 when mean_block == 1)."""
    if type(mean_block) is not int or mean_block < 1:
        raise ValueError(f"mean_block must be an int >= 1, got {mean_block!r}")
    return (1 << 64) // mean_block


def restart_mask(restart_words: np.ndarray, mean_block: int) -> np.ndarray:
    """True where a block restarts (position 0 is forced by the caller)."""
    threshold = restart_threshold(mean_block)
    if threshold >= 1 << 64:
        # mean_block == 1: every word is below 2**64, so every position restarts.
        # 2**64 does not fit in uint64, hence the explicit branch.
        return np.ones(len(restart_words), dtype=bool)
    return restart_words < np.uint64(threshold)


def _check_T(T: int) -> None:
    if type(T) is not int or T < 1:
        raise ValueError(f"T must be an int >= 1, got {T!r}")


def stationary_indices(T: int, mean_block: int, seed: int, stream_prefix: str, replicate: int) -> np.ndarray:
    """(T,) int64 circular stationary-bootstrap indices of one replicate."""
    _check_T(T)
    if type(replicate) is not int or replicate < 0:
        raise ValueError(f"replicate must be an int >= 0, got {replicate!r}")
    words = u64_words(seed, f"{stream_prefix}/rep/{replicate}", 0, 2 * T)
    starts = (words[:T] % np.uint64(T)).astype(np.int64)
    restart = restart_mask(words[T:], mean_block)
    restart[0] = True
    positions = np.arange(T, dtype=np.int64)
    last_restart = np.maximum.accumulate(np.where(restart, positions, 0))
    return (starts[last_restart] + (positions - last_restart)) % T


def bootstrap_indices(T: int, mean_block: int, seed: int, stream_prefix: str,
                      replicates: range | Sequence[int]) -> np.ndarray:
    """(len(replicates), T) int64 rows, row ``i`` = ``stationary_indices(..., replicates[i])``."""
    _check_T(T)
    rows = [stationary_indices(T, mean_block, seed, stream_prefix, replicate) for replicate in replicates]
    if not rows:
        return np.empty((0, T), dtype=np.int64)
    return np.stack(rows)


def bootstrap_mean_matrix(d: np.ndarray, indices: np.ndarray) -> np.ndarray:
    """(B, K) int64 per-replicate column SUMS of ``d[indices[b]]`` (exact; divide by T as float64).

    Gathers run in chunks of replicates so one chunk stays under about 64 MB.
    """
    if not isinstance(d, np.ndarray) or d.dtype != np.int64 or d.ndim != 2:
        raise ValueError("d must be a (T, K) int64 array")
    if not isinstance(indices, np.ndarray) or indices.dtype != np.int64 or indices.ndim != 2:
        raise ValueError("indices must be a (B, T) int64 array")
    T, K = d.shape
    if indices.shape[1] != T:
        raise ValueError(f"indices rows have {indices.shape[1]} positions for T = {T}")
    if indices.size and (indices.min() < 0 or indices.max() >= T):
        raise ValueError("bootstrap indices outside [0, T)")
    largest = max(int(d.max()), -int(d.min())) if d.size else 0  # Python ints: no abs() wrap
    if largest * T >= _INT64_LIMIT:
        raise OverflowError("per-replicate sums could exceed int64")
    B = indices.shape[0]
    sums = np.empty((B, K), dtype=np.int64)
    chunk = max(1, _GATHER_BYTES // max(1, 8 * T * K))
    for begin in range(0, B, chunk):
        sums[begin:begin + chunk] = d[indices[begin:begin + chunk]].sum(axis=1)
    return sums
