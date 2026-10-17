"""Forex setups with a transparent ATR geometry (#374), version ``fx-atr-v1``.

The stop distance is ``k`` x the Wilder ATR of the signal's own timeframe at the signal candle
(point-in-time: candles up to and including it), rounded UP to whole ticks; the target is ``rr`` x
that distance, also in whole ticks. Prices are measured from ``p0``, the executable price before
slippage (long ``bid_open + spread``, short ``bid_open``), so the spread acts symmetrically as a
tighter stop and a harder target. The crypto robust seasonal sigma (``forward/setups.py``) is NOT
ported: doing that for forex (sessions, weekends) is a research item. A separate geometry version
keeps reports from ever mixing the two.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import asdict, dataclass
from fractions import Fraction
import hashlib
from math import ceil
from typing import Dict, List, Optional

from .. import data_lake
from ..benchmark.canonical import exact_to_str
from ..benchmark.indicators import SHIFT, atr
from ..forward.bars_adapter import MINUTE_MS
from ..forward.signals import Signal
from ..trading_calendar import TradingCalendar
from .bars import FLAG_MISSING_OPEN, FxBarSeries
from .candles import FxCandleSeries
from .costs import FxCostModel, entry_fill_price, entry_reference

FX_GEOMETRY_VERSION = "fx-atr-v1"


@dataclass(frozen=True)
class FxGeometryParams:
    atr_period: int = 14
    k_grid: tuple = (Fraction(2),)
    rr_grid: tuple = (Fraction(3, 2), Fraction(2))
    min_stop_spreads: int = 5       # UNVERIFIED placeholder
    time_limit_multiple: int = 4    # window = 4 x horizon, in open minutes inside one session

    def __post_init__(self):
        for name in ("atr_period", "min_stop_spreads", "time_limit_multiple"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive int")
        for name in ("k_grid", "rr_grid"):
            grid = tuple(Fraction(value) for value in getattr(self, name))
            if not grid or any(value <= 0 for value in grid) or any(
                    type(value) not in (int, Fraction) for value in getattr(self, name)):
                raise ValueError(f"{name} must be a non-empty tuple of positive ints or Fractions")
            object.__setattr__(self, name, grid)


FX_GEOMETRY_PARAMS = FxGeometryParams()


@dataclass(frozen=True)
class FxSetup:
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
    geometry_version: str
    reason: Optional[str]
    window_minutes: int
    atr: Optional[int] = None
    p0: Optional[int] = None
    spread0: Optional[int] = None
    tick: Optional[int] = None
    d_ticks: Optional[int] = None
    stop: Optional[int] = None
    target: Optional[int] = None
    entry_fill: Optional[str] = None
    risk_distance: Optional[str] = None
    params_hash: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def fx_params_hash(params: FxGeometryParams, model: FxCostModel) -> str:
    record = {"geometry": FX_GEOMETRY_VERSION, "atr_period": params.atr_period,
              "k_grid": [exact_to_str(k) for k in params.k_grid],
              "rr_grid": [exact_to_str(rr) for rr in params.rr_grid],
              "min_stop_spreads": params.min_stop_spreads, "time_limit_multiple": params.time_limit_multiple,
              "cost_model": model.identity()}
    return hashlib.sha256(data_lake.canonical_json(record).encode("ascii")).hexdigest()


def fx_setup_id(signal_id: str, k: Fraction, rr: Fraction, params_hash: str) -> str:
    text = "|".join([FX_GEOMETRY_VERSION, signal_id, exact_to_str(k), exact_to_str(rr), params_hash])
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _geometry(signal: Signal, candles: Dict[int, FxCandleSeries], bars: FxBarSeries, calendar: TradingCalendar,
              params: FxGeometryParams) -> dict:
    """The (k, rr)-independent part: {status, reason, window_minutes, atr, e, p0, spread0}."""
    window = params.time_limit_multiple * signal.horizon_min
    series = candles.get(signal.horizon_min)
    if series is None:
        raise ValueError(f"no {signal.horizon_min}-minute candles supplied")
    i = bisect_left(series.end_ms, signal.signal_ms)
    if i == len(series) or series.end_ms[i] != signal.signal_ms:
        raise ValueError("signal is not on the candle grid")
    out = {"window_minutes": window, "status": "T", "reason": None}
    value = atr(series.column("high"), series.column("low"), series.column("close"), params.atr_period)[i]
    if value is None:
        return {**out, "status": "V", "reason": "atr_unavailable"}
    out["atr"] = value
    if not calendar.is_open(signal.signal_ms):  # e.g. a signal at the Friday close, at bars.end_ms
        return {**out, "status": "C", "reason": "entry_minute_unavailable"}
    if not bars.start_ms <= signal.signal_ms < bars.end_ms:
        raise ValueError("entry minute is not in the bars yet")  # not decidable yet: the caller waits
    e = bars.index_of(signal.signal_ms)
    if bars.flags[e] == FLAG_MISSING_OPEN:
        return {**out, "status": "C", "reason": "entry_minute_unavailable"}
    if bars.open[e] % bars.tick:
        return {**out, "status": "P", "reason": "open_off_tick_grid"}
    if calendar.open_until_ms(signal.signal_ms) < signal.signal_ms + window * MINUTE_MS:
        return {**out, "status": "C", "reason": "window_exceeds_session"}
    out.update(e=e, spread0=bars.spread[e], p0=int(entry_reference(signal.side, bars.open[e], bars.spread[e])))
    return out


def build_fx_setups(signal: Signal, candles: Dict[int, FxCandleSeries], bars: FxBarSeries,
                    calendar: TradingCalendar, model: FxCostModel,
                    params: FxGeometryParams = FX_GEOMETRY_PARAMS) -> List[FxSetup]:
    """One setup per (k, rr) of the grids, sorted by (k, rr)."""
    if bars.symbol != signal.symbol:
        raise ValueError("bars belong to another symbol")
    params_hash = fx_params_hash(params, model)
    geometry = _geometry(signal, candles, bars, calendar, params)
    setups = []
    for k in sorted(params.k_grid):
        for rr in sorted(params.rr_grid):
            fields = {"setup_id": fx_setup_id(signal.signal_id, k, rr, params_hash), "signal_id": signal.signal_id,
                      "strategy_id": signal.strategy_id, "version": signal.version, "symbol": signal.symbol,
                      "side": signal.side, "horizon_min": signal.horizon_min, "signal_ms": signal.signal_ms,
                      "entry_ms": signal.signal_ms, "k": exact_to_str(k), "rr": exact_to_str(rr),
                      "status": geometry["status"], "geometry_version": FX_GEOMETRY_VERSION,
                      "reason": geometry["reason"], "window_minutes": geometry["window_minutes"],
                      "atr": geometry.get("atr"), "params_hash": params_hash}
            if geometry["status"] == "T":
                fields.update(_levels(signal.side, k, rr, geometry, bars, model, params))
            setups.append(FxSetup(**fields))
    return setups


def _levels(side: int, k: Fraction, rr: Fraction, geometry: dict, bars: FxBarSeries, model: FxCostModel,
            params: FxGeometryParams) -> dict:
    p0, spread0, e, tick = geometry["p0"], geometry["spread0"], geometry["e"], bars.tick
    out = {"p0": p0, "spread0": spread0, "tick": tick}
    d_ticks = ceil(Fraction(k * geometry["atr"]) / ((1 << SHIFT) * tick))
    d = d_ticks * tick
    if d_ticks < 1 or d < params.min_stop_spreads * spread0:
        return {**out, "d_ticks": d_ticks, "status": "G", "reason": "stop_narrower_than_spreads"}
    stop = p0 - side * d
    if stop <= 0:
        return {**out, "d_ticks": d_ticks, "status": "N", "reason": "stop_not_positive"}
    target = p0 + side * ceil(rr * d_ticks) * tick
    if target <= 0:  # a short with a wide stop or a large rr
        return {**out, "d_ticks": d_ticks, "stop": stop, "status": "N", "reason": "target_not_positive"}
    fill = entry_fill_price(side, p0, spread0, bars.high[e], bars.low[e], model)
    return {**out, "d_ticks": d_ticks, "stop": stop, "target": target, "entry_fill": exact_to_str(fill),
            "risk_distance": exact_to_str(abs(fill - stop))}


__all__ = ["FX_GEOMETRY_PARAMS", "FX_GEOMETRY_VERSION", "FxGeometryParams", "FxSetup", "build_fx_setups",
           "fx_params_hash", "fx_setup_id"]
