"""Romano-Wolf studentized step-down (StepM) for #182.

Uses exactly the same t-statistics, omegas and bootstrap means as ``spa.py``
(shared ``_studentize`` path, counter-based stationary bootstrap, exact integer
sums). Step ``s``: for every replicate ``b``,
``M_b = max over active k of sqrt(T) * (dstar[b, k] - dbar_k) / omega_k``; the
critical value is the ``ceil((1 - alpha) * B)``-th smallest ``M_b`` (1-based order
statistic, no interpolation); active trials with ``t_k > c`` are rejected and
removed; stop when a step rejects nothing. ``(1 - alpha) * B`` is evaluated
exactly from ``repr(alpha)`` so 0.05 means 1/20, not its binary approximation.

Degenerate trials (``omega_k == 0``) are excluded from the active set from the
start and are never rejected.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import math

import numpy as np

from .spa import _studentize


@dataclass(frozen=True)
class StepMResult:
    rejected: tuple
    critical_values: tuple
    steps: int
    alpha: float
    B: int
    t_stats: tuple
    T: int
    K: int
    mean_block: int
    seed: int
    stream_prefix: str

    def to_record(self) -> dict:
        """Every float rendered with repr(), so the record can be stored and hashed as text."""
        return {
            "rejected": list(self.rejected),
            "critical_values": [repr(value) for value in self.critical_values],
            "steps": self.steps,
            "alpha": repr(self.alpha),
            "B": self.B,
            "t_stats": [repr(value) for value in self.t_stats],
            "T": self.T, "K": self.K, "mean_block": self.mean_block,
            "seed": self.seed, "stream_prefix": self.stream_prefix,
        }


def critical_rank(alpha: float, B: int) -> int:
    """ceil((1 - alpha) * B) with alpha taken exactly from its decimal repr."""
    if not 0 < alpha < 1:
        raise ValueError(f"alpha must be in (0, 1), got {alpha!r}")
    rank = math.ceil((1 - Fraction(repr(alpha))) * B)
    if not 1 <= rank <= B:
        raise ValueError(f"alpha {alpha!r} with B {B} gives no usable order statistic")
    return rank


def stepm(f, f0=None, *, alpha: float = 0.05, B: int = 10_000, mean_block: int | None = None, seed: int,
          stream_prefix: str) -> StepMResult:
    stats = _studentize(f, f0, B, mean_block, seed, stream_prefix)
    rank = critical_rank(alpha, stats.B)
    root_t = math.sqrt(stats.T)
    # Same elementwise expression as the SPA upper / Reality Check recentering, studentized.
    centred = np.zeros_like(stats.dstar)
    centred[:, stats.valid] = (root_t * (stats.dstar[:, stats.valid] - stats.dbar[stats.valid])
                               / stats.omega[stats.valid])
    active = [k for k in range(stats.K) if stats.valid[k]]
    rejected, critical_values = [], []
    while active:
        maxima = centred[:, active].max(axis=1)
        critical = float(np.sort(maxima)[rank - 1])
        critical_values.append(critical)
        newly = [k for k in active if stats.t[k] > critical]
        if not newly:
            break
        rejected.extend(newly)
        active = [k for k in active if k not in newly]
    return StepMResult(
        rejected=tuple(sorted(rejected)), critical_values=tuple(critical_values), steps=len(critical_values),
        alpha=alpha, B=stats.B, t_stats=tuple(float(value) for value in stats.t), T=stats.T, K=stats.K,
        mean_block=stats.mean_block, seed=seed, stream_prefix=stream_prefix,
    )
