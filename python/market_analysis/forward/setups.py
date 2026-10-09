"""Forward setups with exactly the label geometry of ``labels._row`` (#239 P10, sigma model P16).

Sigma model (P16, 2026-10-10): ``ewma-robust-hcal`` (schema ``labels-v3``, lb3h releases), the
model the calibration audit found closest to theory. ``ForwardSigma`` wraps ``RobustSigma`` with
``hcal=True`` exactly as ``labels.build_labels`` does: the robust |r| level of the 5-minute block
ending at the decision, the entry day's |r| slot factors, times the point-in-time horizon
multiplier c_h of ``volatility.horizon_calibration`` on the label-step grid. c_h needs 60 days of
completed past windows after the robust sigma exists (28-day slot-factor warm-up plus the level
warm-up), so the forward job sends about 120 days of 1-minute history. Over a rolling request window
c_h is an expanding median from the window start (point in time, never using the future).
Until a sigma exists ``build_setup`` returns ``NO_SIGMA`` (no setups at all) and ``evaluate``
records ``no_sigma`` for the symbol: it never falls back to another model. The ``ewma-seasonal``
(labels-v2) geometry stays available for ``params`` with that model (status V when unavailable).

For a signal at ``s`` (end of minute ``d``) the entry is the open of minute ``e = d + 1``:
``d_ticks = ceil(k * sigma * p0 / (10**20 * tick))``, stop ``p0 - side * d_ticks * tick``, target
``p0 + side * ceil(rr * d_ticks) * tick``, time limit ``4 * horizon`` minutes; statuses as the labels:
C (entry minute compromised), P (entry open off the tick grid), G (stop narrower than
``min_stop_ticks``), N (stop at or below zero, or no admissible leverage), T (a tradeable setup).
Grid: ``k = 2`` and ``rr in {1.5, 2}``; every (k, rr) is its own setup with its own id, so K is
visible. A setup is immutable and carries every input: for hcal, ``var`` is the robust level,
``factor_weight`` the c_h multiplier (``HCAL_SCALE`` fixed point), ``sigma`` the horizon sigma.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from fractions import Fraction
import hashlib

from ..benchmark import costs
from ..benchmark.bars import MISSING, BarSeries
from ..benchmark.canonical import exact_from_str, exact_to_str
from ..benchmark.labels import LabelParams
from ..benchmark.robust_sigma import ROBUST_MODELS, RobustSigma
from ..benchmark.scan import next_compromised
from ..benchmark.volatility import (BLOCK_MINUTES, BLOCK_MS, BLOCKS_PER_SLOT, DAY_MS, VAR_SCALE,
                                    build_variance_deseasonalised, horizon_sigma_seasonal, seasonal_factors)
from . import VERSIONS
from .bars_adapter import MINUTE_MS
from .signals import Signal

BIG_FIELDS = ("var", "factor_weight", "sigma")
FORWARD_PARAMS = LabelParams(sigma_model="ewma-robust-hcal", k_grid=(Fraction(2),),
                             rr_grid=(Fraction(3, 2), Fraction(2)))
NO_SIGMA = ()  # build_setup result when the robust (hcal) sigma does not exist yet: emit nothing


class ForwardSigma:
    """Point-in-time sigma inputs of one symbol's bars for ``params`` (same numbers as the labels)."""

    def __init__(self, bars: BarSeries, params: LabelParams = FORWARD_PARAMS):
        self.params = params
        self.robust = None
        if params.sigma_model in ROBUST_MODELS:
            self.robust = RobustSigma(bars, hcal=params.sigma_model == "ewma-robust-hcal")
        elif params.seasonal:
            self.factors = seasonal_factors(bars)
            self.variance = {days: build_variance_deseasonalised(bars, days, self.factors).variance
                             for days in {params.half_life(h) for h in params.horizons}}
        else:
            raise ValueError(f"forward setups do not support sigma model {params.sigma_model!r}")

    def robust_point(self, horizon: int, signal_ms: int):
        """(level, multiplier or None, sigma) of the robust models, or None while unavailable."""
        params = self.params
        half_life, step = params.half_life(horizon), params.step(horizon)
        sigma = self.robust.sigma(horizon, half_life, step, signal_ms)
        if sigma is None:
            return None
        level = self.robust.level_at(half_life, signal_ms)
        multiplier = self.robust.multiplier(horizon, half_life, step, signal_ms) if self.robust.hcal else None
        return level, multiplier, sigma


@dataclass(frozen=True)
class Setup:
    setup_id: str
    signal_id: str
    strategy_id: str
    version: str
    symbol: str
    side: int
    horizon_min: int
    signal_ms: int
    entry_ms: int
    k: str
    rr: str
    status: str
    half_life_days: int
    window_minutes: int
    var: int | None = None
    factor_weight: int | None = None
    sigma: int | None = None
    p0: int | None = None
    tick: int | None = None
    d_ticks: int | None = None
    stop: int | None = None
    target: int | None = None
    label_leverage: int | None = None
    params_hash: str = ""
    versions: tuple = ()

    def to_dict(self) -> dict:
        """JSON form: var, factor_weight and sigma (above 2**53) as exact decimal text."""
        out = asdict(self)
        out["versions"] = dict(self.versions)
        for name in BIG_FIELDS:
            out[name] = None if out[name] is None else str(out[name])
        return out

    @staticmethod
    def from_dict(value: dict) -> "Setup":
        data = dict(value)
        data["versions"] = tuple(sorted(dict(data.get("versions") or {}).items()))
        for name in BIG_FIELDS:
            data[name] = None if data.get(name) is None else int(data[name])
        setup = Setup(**data)
        if setup.setup_id != setup_id(setup.signal_id, exact_from_str(setup.k), exact_from_str(setup.rr),
                                      setup.params_hash):
            raise ValueError("setup_id does not match its saved fields")
        return setup


def setup_id(signal_id: str, k: Fraction, rr: Fraction, params_hash: str) -> str:
    text = f"{signal_id}|{exact_to_str(k)}|{exact_to_str(rr)}|{params_hash}"
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def build_setup(signal: Signal, bars: BarSeries, sigma: ForwardSigma, *, tick: int,
                params: LabelParams = FORWARD_PARAMS, next_comp=None) -> tuple[Setup, ...] | None:
    """One setup per (k, rr) of ``params``; None while the entry minute is not in ``bars`` yet;
    ``NO_SIGMA`` (empty) when a robust-model sigma does not exist yet (never another model).

    ``sigma``: ``ForwardSigma(bars, params)``; ``tick``: the tick used for the price grid.
    """
    if sigma.params != params:
        raise ValueError("sigma inputs were built for other label parameters")
    if signal.horizon_min not in params.horizons:
        raise ValueError(f"horizon {signal.horizon_min} has no label parameters")
    if (signal.signal_ms - bars.start_ms) % MINUTE_MS:
        raise ValueError("signal is not on a minute boundary of the bars")
    d = (signal.signal_ms - bars.start_ms) // MINUTE_MS - 1
    e = d + 1
    if d < 0:
        raise ValueError("signal precedes the bars")
    if e >= bars.minutes:
        return None
    horizon, side = signal.horizon_min, signal.side
    half_life = params.half_life(horizon)
    robust = None
    if sigma.robust is not None:
        robust = sigma.robust_point(horizon, signal.signal_ms)
        if robust is None:
            return NO_SIGMA
    params_hash = params.identity()
    versions = tuple(sorted(VERSIONS.items()))
    model = params.cost_model
    nc = next_compromised(bars) if next_comp is None else next_comp
    if robust is None:
        block = (d + 1) // BLOCK_MINUTES - 1
        var = sigma.variance[half_life][block] if block >= 0 else MISSING
        day_factors = sigma.factors.day_factors(bars.open_time(e) // DAY_MS)
        entry_block = (signal.signal_ms % DAY_MS) // BLOCK_MS
    out = []
    for k in params.k_grid:
        for rr in params.rr_grid:
            base = dict(setup_id=setup_id(signal.signal_id, k, rr, params_hash), signal_id=signal.signal_id,
                        strategy_id=signal.strategy_id, version=signal.version, symbol=signal.symbol, side=side,
                        horizon_min=horizon, signal_ms=signal.signal_ms, entry_ms=signal.signal_ms,
                        k=exact_to_str(k), rr=exact_to_str(rr), half_life_days=half_life,
                        window_minutes=params.window(horizon), params_hash=params_hash, versions=versions)
            if robust is not None:
                level, multiplier, horizon_sigma = robust
                base.update(var=level, factor_weight=multiplier)
            else:
                if var == MISSING or day_factors is None:
                    out.append(Setup(**base, status="V"))
                    continue
                weight = sum(day_factors[((entry_block + i) % (24 * 60 // BLOCK_MINUTES)) // BLOCKS_PER_SLOT]
                             for i in range(horizon // BLOCK_MINUTES))
                base.update(var=var, factor_weight=weight)
            if nc[e] == e:
                out.append(Setup(**base, status="C"))
                continue
            p0 = bars.open[e]
            base.update(p0=p0, tick=tick)
            if p0 % tick:
                out.append(Setup(**base, status="P"))
                continue
            if robust is None:
                horizon_sigma = horizon_sigma_seasonal(var, day_factors, entry_block, horizon)
            d_ticks = _ceil_div(k.numerator * horizon_sigma * p0, k.denominator * VAR_SCALE * tick)
            base.update(sigma=horizon_sigma, d_ticks=d_ticks)
            if d_ticks < params.min_stop_ticks:
                out.append(Setup(**base, status="G"))
                continue
            stop = p0 - side * d_ticks * tick
            if stop <= 0:
                out.append(Setup(**base, status="N", stop=stop))
                continue
            target = p0 + side * _ceil_div(rr.numerator * d_ticks, rr.denominator) * tick
            fill_in = costs.entry_fill_price(side, p0, bars.high[e], bars.low[e], model)
            leverage = costs.max_admissible_leverage(side, fill_in, abs(fill_in - stop), model)
            out.append(Setup(**base, status="T" if leverage is not None else "N", stop=stop, target=target,
                             label_leverage=leverage))
    return tuple(out)
