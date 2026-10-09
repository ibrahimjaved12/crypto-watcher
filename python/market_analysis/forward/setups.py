"""Forward setups with exactly the labels-v2 geometry (``labels._row`` with ``sigma_model=ewma-seasonal``).

For a signal at ``s`` (end of minute ``d``) the entry is the open of minute ``e = d + 1``:
- variance: the deseasonalised EWMA (``build_variance_deseasonalised``, half-life by horizon as in
  ``LabelParams``) of the 5-minute block ending at minute ``d``;
- sigma: ``horizon_sigma_seasonal`` with the entry day's factors (``seasonal_factors``);
- ``d_ticks = ceil(k * sigma * p0 / (10**20 * tick))``, stop ``p0 - side * d_ticks * tick``, target
  ``p0 + side * ceil(rr * d_ticks) * tick``, time limit ``4 * horizon`` minutes;
- statuses as the labels: V (no variance or no factors for the entry day), C (entry minute
  compromised), P (entry open off the tick grid), G (stop narrower than ``min_stop_ticks``), N (stop
  at or below zero, or no admissible leverage), T (a tradeable setup).
Grid: ``k = 2`` and ``rr in {1.5, 2}``; every (k, rr) is its own setup with its own id, so K is
visible. A setup is immutable and carries every input (sigma, variance, factor weight, p0, tick,
prices, label leverage, parameter hash and versions).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from fractions import Fraction
import hashlib

from ..benchmark import costs
from ..benchmark.bars import MISSING, BarSeries
from ..benchmark.canonical import exact_from_str, exact_to_str
from ..benchmark.labels import LabelParams
from ..benchmark.scan import next_compromised
from ..benchmark.volatility import (BLOCK_MINUTES, BLOCK_MS, BLOCKS_PER_SLOT, DAY_MS, VAR_SCALE,
                                    horizon_sigma_seasonal)
from . import VERSIONS
from .bars_adapter import MINUTE_MS
from .signals import Signal

FORWARD_PARAMS = LabelParams(sigma_model="ewma-seasonal", k_grid=(Fraction(2),),
                             rr_grid=(Fraction(3, 2), Fraction(2)))


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
        out = asdict(self)
        out["versions"] = dict(self.versions)
        return out

    @staticmethod
    def from_dict(value: dict) -> "Setup":
        data = dict(value)
        data["versions"] = tuple(sorted(dict(data.get("versions") or {}).items()))
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


def build_setup(signal: Signal, bars: BarSeries, factors, variance, *, tick: int, params: LabelParams = FORWARD_PARAMS,
                next_comp=None) -> tuple[Setup, ...] | None:
    """One setup per (k, rr) of ``params``, or None while the entry minute is not in ``bars`` yet.

    ``variance``: {half_life_days: variance array of ``build_variance_deseasonalised``} on ``bars``;
    ``factors``: ``seasonal_factors(bars)``; ``tick``: the tick used for the price grid.
    """
    if not params.seasonal:
        raise ValueError("forward setups use the ewma-seasonal (labels-v2) geometry")
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
    params_hash = params.identity()
    versions = tuple(sorted(VERSIONS.items()))
    model = params.cost_model
    nc = next_compromised(bars) if next_comp is None else next_comp
    block = (d + 1) // BLOCK_MINUTES - 1
    var = variance[half_life][block] if block >= 0 else MISSING
    day = bars.open_time(e) // DAY_MS
    day_factors = factors.day_factors(day)
    entry_block = (signal.signal_ms % DAY_MS) // BLOCK_MS
    out = []
    for k in params.k_grid:
        for rr in params.rr_grid:
            base = dict(setup_id=setup_id(signal.signal_id, k, rr, params_hash), signal_id=signal.signal_id,
                        strategy_id=signal.strategy_id, version=signal.version, symbol=signal.symbol, side=side,
                        horizon_min=horizon, signal_ms=signal.signal_ms, entry_ms=signal.signal_ms,
                        k=exact_to_str(k), rr=exact_to_str(rr), half_life_days=half_life,
                        window_minutes=params.window(horizon), params_hash=params_hash, versions=versions)
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
            sigma = horizon_sigma_seasonal(var, day_factors, entry_block, horizon)
            d_ticks = _ceil_div(k.numerator * sigma * p0, k.denominator * VAR_SCALE * tick)
            base.update(sigma=sigma, d_ticks=d_ticks)
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
