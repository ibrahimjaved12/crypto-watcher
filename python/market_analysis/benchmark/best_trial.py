"""Deflated Sharpe Ratio of the best of K trials, end to end, for #182.

``f`` is a (T days, K trials) int64 matrix of daily net results (1e-6 R). Each
non-constant trial's per-period Sharpe ratio comes from exact integer moments,
``SR_k = S_k * sqrt((T - 1) / T) / sqrt(c_kk)`` with ``c_kk = T * sum f**2 - S_k**2``
(mean over sample std, ddof = 1). Constant trials have no Sharpe ratio: they are
excluded from the cross-sectional Sharpe variance and from the choice of best.

The best trial (highest SR, ties to the lowest index) is deflated twice:
against ``SR0`` for the raw trial count ``n_trials`` (default K; an
experiment-log count may be larger) and against ``SR0`` for the effective count
``effective_trials(f, n_trials)`` (Bailey and Lopez de Prado). Its SR, skewness
and non-excess kurtosis come from ``dsr.sharpe_stats``.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import math

import numpy as np

from .dsr import deflated_sharpe_ratio, expected_max_sharpe, sharpe_stats
from .neff import _check_n_trials, _exact_column_moments, effective_trials


@dataclass(frozen=True)
class BestTrialDsr:
    best_index: int
    n_trials: int
    n_effective: float
    var_sharpe: float
    sr_hat: float
    sr0_raw: float
    sr0_effective: float
    dsr_raw: float
    dsr_effective: float
    n_obs: int
    skew: float
    kurtosis: float

    def to_record(self) -> dict:
        """Every float rendered with repr(), so the record can be stored and hashed as text."""
        record = {}
        for field in fields(self):
            value = getattr(self, field.name)
            record[field.name] = repr(value) if type(value) is float else value
        return record


def best_trial_dsr(f: np.ndarray, n_trials: int | None = None) -> BestTrialDsr:
    T, K, sums, centred_squares = _exact_column_moments(f)
    total = _check_n_trials(n_trials, K)
    root = math.sqrt((T - 1) / T)
    sharpe = {k: sums[k] * root / math.sqrt(centred_squares[k]) for k in range(K) if centred_squares[k] > 0}
    if len(sharpe) < 2:
        raise ValueError(f"need at least 2 non-constant trials, got {len(sharpe)}")
    values = list(sharpe.values())
    mean = math.fsum(values) / len(values)
    var_sharpe = math.fsum((value - mean) ** 2 for value in values) / (len(values) - 1)
    best = None
    for k, value in sharpe.items():  # ascending k: strict > keeps the lowest index on ties
        if best is None or value > sharpe[best]:
            best = k
    _, _, sr_hat, skew, kurtosis, n_obs = sharpe_stats(f[:, best].tolist())
    n_effective = effective_trials(f, total)
    sr0_raw = expected_max_sharpe(total, var_sharpe)
    sr0_effective = expected_max_sharpe(n_effective, var_sharpe)
    return BestTrialDsr(
        best_index=best, n_trials=total, n_effective=n_effective, var_sharpe=var_sharpe, sr_hat=sr_hat,
        sr0_raw=sr0_raw, sr0_effective=sr0_effective,
        dsr_raw=deflated_sharpe_ratio(sr_hat, sr0_raw, n_obs, skew, kurtosis),
        dsr_effective=deflated_sharpe_ratio(sr_hat, sr0_effective, n_obs, skew, kurtosis),
        n_obs=n_obs, skew=skew, kurtosis=kurtosis,
    )
