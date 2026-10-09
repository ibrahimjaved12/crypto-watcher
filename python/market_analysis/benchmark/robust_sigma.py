"""Robust and horizon-calibrated sigma models (``ewma-robust`` / ``ewma-robust-hcal``, labels-v3, #220 P12).

``RobustSigma(bars, hcal)`` holds the |r| slot factors (``seasonal_factors_abs``) and the robust scale
series per half-life (``build_scale_robust``). ``sigma(horizon, half_life, step, signal_ms)`` is the
horizon sigma (10**20 scale) of a decision at ``signal_ms`` (entry = the minute opening then), from
the block ending at the decision and the entry day's factors; None when unavailable. With ``hcal``
it is multiplied by the point-in-time horizon multiplier c_h of ``volatility.horizon_calibration``
(the label-step grid of that horizon), None until 60 days of past windows exist. Used by
``labels.build_labels`` and ``calibration.audit_series`` so both see the same numbers.
"""
from __future__ import annotations

from .bars import MISSING, BarSeries
from .volatility import (BLOCK_MINUTES, BLOCK_MS, DAY_MS, HCAL_SCALE, build_scale_robust, horizon_calibration,
                         horizon_sigma_robust, seasonal_factors_abs)

ROBUST_MODELS = ("ewma-robust", "ewma-robust-hcal")
_MINUTE_MS = 60_000


class RobustSigma:
    def __init__(self, bars: BarSeries, hcal: bool):
        self.bars = bars
        self.hcal = hcal
        self.factors = seasonal_factors_abs(bars)
        self._levels: dict = {}
        self._calibration: dict = {}

    def levels(self, half_life: int):
        if half_life not in self._levels:
            self._levels[half_life] = build_scale_robust(self.bars, half_life, self.factors).variance
        return self._levels[half_life]

    def level_at(self, half_life: int, signal_ms: int) -> int:
        block = (signal_ms - self.bars.start_ms) // _MINUTE_MS // BLOCK_MINUTES - 1  # block ending at the decision
        return self.levels(half_life)[block] if 0 <= block < len(self.levels(half_life)) else MISSING

    def robust(self, horizon: int, half_life: int, signal_ms: int) -> int | None:
        scale = self.level_at(half_life, signal_ms)
        day = self.factors.day_factors(signal_ms // DAY_MS)
        if scale == MISSING or day is None:
            return None
        return horizon_sigma_robust(scale, day, (signal_ms % DAY_MS) // BLOCK_MS, horizon)

    def multiplier(self, horizon: int, half_life: int, step: int, signal_ms: int) -> int | None:
        key = (horizon, half_life, step)
        if key not in self._calibration:
            self._calibration[key] = horizon_calibration(
                self.bars, horizon, step, lambda entry_ms: self.robust(horizon, half_life, entry_ms))
        return self._calibration[key].get(signal_ms)

    def sigma(self, horizon: int, half_life: int, step: int, signal_ms: int) -> int | None:
        base = self.robust(horizon, half_life, signal_ms)
        if base is None or not self.hcal:
            return base
        c = self.multiplier(horizon, half_life, step, signal_ms)
        return None if c is None else base * c // HCAL_SCALE
