"""Exact cost, slippage, funding and liquidation math for the #182 benchmark harness.

Everything is per ONE unit of base asset; results scale linearly with size, so
R multiples do not depend on it. Prices are integers scaled by 10**8 (as in the
bar series); every derived amount is an exact ``Fraction`` in the same scale.
No floats are used.

Sides are +1 (long) and -1 (short). Sign conventions match
``market_analysis.futures_execution``: funding cash is ``-side * mark * rate``
(positive rate: longs pay, shorts receive) and the isolated-margin liquidation
price uses Binance's one-way formula.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from fractions import Fraction
import hashlib
from math import floor

from .. import data_lake

_BPS = 10_000
_FRACTION_FIELDS = ("taker_rate", "maker_rate", "market_slip_floor_bps", "market_slip_range_mult",
                    "stop_slip_floor_bps", "stop_slip_range_mult", "liquidation_fee_rate", "mmr",
                    "cost_multiplier", "funding_multiplier")


def _exact_text(value: Fraction) -> str:
    return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


def _side(side: int) -> int:
    if side not in (1, -1) or type(side) is not int:
        raise ValueError(f"side must be +1 (long) or -1 (short), got {side!r}")
    return side


def _price(value, name: str) -> Fraction:
    if type(value) not in (int, Fraction) or value <= 0:
        raise ValueError(f"{name} must be a positive int or Fraction, got {value!r}")
    return Fraction(value)


@dataclass(frozen=True)
class CostModel:
    version: str
    taker_rate: Fraction
    maker_rate: Fraction
    market_slip_floor_bps: Fraction
    market_slip_range_mult: Fraction
    stop_slip_floor_bps: Fraction
    stop_slip_range_mult: Fraction
    liquidation_fee_rate: Fraction
    mmr: Fraction
    leverage_cap: int
    liq_buffer_multiple: int
    cost_multiplier: Fraction
    funding_multiplier: Fraction

    def __post_init__(self):
        if type(self.version) is not str or not self.version:
            raise ValueError("cost model version must be a non-empty string")
        for name in _FRACTION_FIELDS:
            value = getattr(self, name)
            if type(value) is int:
                value = Fraction(value)
                object.__setattr__(self, name, value)
            if type(value) is not Fraction or value < 0:
                raise ValueError(f"{name} must be a non-negative Fraction, got {value!r}")
        if self.mmr >= 1:
            raise ValueError("mmr must be below 1")
        for name in ("leverage_cap", "liq_buffer_multiple"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive int, got {value!r}")

    def to_record(self) -> dict:
        record = {}
        for field in fields(self):
            value = getattr(self, field.name)
            record[field.name] = _exact_text(value) if type(value) is Fraction else value
        return record

    def identity(self) -> str:
        return hashlib.sha256(data_lake.canonical_json(self.to_record()).encode("ascii")).hexdigest()


COST_MODEL_V1 = CostModel(
    version="cost-v1",
    taker_rate=Fraction(5, 10_000),
    maker_rate=Fraction(2, 10_000),
    market_slip_floor_bps=Fraction(1),
    market_slip_range_mult=Fraction(1, 10),
    stop_slip_floor_bps=Fraction(2),
    stop_slip_range_mult=Fraction(1, 4),
    liquidation_fee_rate=Fraction(1, 80),
    mmr=Fraction(1, 100),
    leverage_cap=20,
    liq_buffer_multiple=3,
    cost_multiplier=Fraction(1),
    funding_multiplier=Fraction(1),
)

ASSUMPTIONS_V1 = (
    "taker_rate 0.05% and maker_rate 0.02% are the VIP0 USD-M futures schedule from the Binance fee FAQ.",
    "liquidation_fee_rate (1/80) is an UNVERIFIED placeholder chosen to be conservative.",
    "mmr (1% maintenance margin, first bracket) is an UNVERIFIED placeholder chosen to be conservative.",
    "leverage_cap (20x) is an UNVERIFIED placeholder chosen to be conservative.",
    "The market and stop slippage parameters (floors in bps and fractions of the fill minute's range) are "
    "UNVERIFIED placeholders chosen to be conservative.",
)


def with_multiplier(model: CostModel, multiplier) -> CostModel:
    """Copy with ``cost_multiplier`` (fees and slippage) set, for the 0x/1x/2x/3x cost grid."""
    return replace(model, cost_multiplier=Fraction(multiplier))


# ---------------------------------------------------------------- slippage and fills


def _slippage(reference: int, high: int, low: int, floor_bps: Fraction, range_mult: Fraction,
              model: CostModel) -> Fraction:
    reference = _price(reference, "reference open")
    high, low = _price(high, "high"), _price(low, "low")
    if high < low:
        raise ValueError(f"high {high} below low {low}")
    return model.cost_multiplier * max(reference * floor_bps / _BPS, range_mult * (high - low))


def market_slippage(ref_open: int, high: int, low: int, model: CostModel) -> Fraction:
    """Market-order price distance from the fill minute's own open/high/low."""
    return _slippage(ref_open, high, low, model.market_slip_floor_bps, model.market_slip_range_mult, model)


def stop_slippage(ref_open: int, high: int, low: int, model: CostModel) -> Fraction:
    """Stop-order price distance from the fill minute's own open/high/low."""
    return _slippage(ref_open, high, low, model.stop_slip_floor_bps, model.stop_slip_range_mult, model)


def entry_fill_price(side: int, ref_open: int, high: int, low: int, model: CostModel) -> Fraction:
    """Market entry: a long buys at open + slippage, a short sells at open - slippage."""
    return Fraction(ref_open) + _side(side) * market_slippage(ref_open, high, low, model)


def stop_fill_price(side: int, stop_price, minute_open: int, high: int, low: int, model: CostModel) -> Fraction:
    """Stop exit. A long's stop sells at min(stop, open) - slippage; a short's buys at max(stop, open) + slippage.

    A gap through the stop fills from the (worse) minute open, never at the stop.
    """
    side = _side(side)
    stop = _price(stop_price, "stop price")
    opened = _price(minute_open, "minute open")
    slip = stop_slippage(minute_open, high, low, model)
    return min(stop, opened) - slip if side == 1 else max(stop, opened) + slip


# ---------------------------------------------------------------- fees and funding


def fee(price, rate: Fraction, model: CostModel) -> Fraction:
    """Fee per unit at a fill price: price * rate * cost_multiplier."""
    if type(rate) not in (int, Fraction) or rate < 0:
        raise ValueError(f"fee rate must be a non-negative Fraction, got {rate!r}")
    return _price(price, "fill price") * rate * model.cost_multiplier


def funding_cash(side: int, mark_price, rate: Fraction, model: CostModel) -> Fraction:
    """Funding cash per unit (positive = received): -side * mark * rate * funding_multiplier."""
    if type(rate) not in (int, Fraction):
        raise ValueError(f"funding rate must be an int or Fraction, got {rate!r}")
    return -_side(side) * _price(mark_price, "mark price") * Fraction(rate) * model.funding_multiplier


# ---------------------------------------------------------------- liquidation


def liquidation_price(side: int, entry, leverage: int, mmr: Fraction, cum=0) -> Fraction:
    """Isolated, one-way, quantity 1, wallet balance entry/leverage:

    LP = (WB + cum - side * entry) / (mmr - side)
    """
    side = _side(side)
    entry = _price(entry, "entry")
    if type(leverage) is not int or leverage < 1:
        raise ValueError(f"leverage must be an int >= 1, got {leverage!r}")
    mmr = Fraction(mmr)
    if not 0 <= mmr < 1:
        raise ValueError(f"mmr must be in [0, 1), got {mmr!r}")
    wallet = entry / leverage
    return (wallet + Fraction(cum) - side * entry) / (mmr - side)


def max_admissible_leverage(side: int, entry, stop_distance, model: CostModel) -> int | None:
    """Highest integer leverage L in [1, leverage_cap] whose liquidation is at least
    ``liq_buffer_multiple * stop_distance`` away from entry; None if even L = 1 fails.

    Closed form (cum = 0, from ``liquidation_price``): the distance is
    ``entry * (1/L - mmr) / (1 - side * mmr)`` for both sides, so the condition
    ``distance >= k * d`` is ``1/L >= mmr + k * d * (1 - side * mmr) / entry``.
    """
    side = _side(side)
    entry = _price(entry, "entry")
    if type(stop_distance) not in (int, Fraction) or stop_distance < 0:
        raise ValueError(f"stop_distance must be a non-negative int or Fraction, got {stop_distance!r}")
    need = model.liq_buffer_multiple * Fraction(stop_distance)
    threshold = model.mmr + need * (1 - side * model.mmr) / entry
    leverage = model.leverage_cap if threshold <= 0 else min(model.leverage_cap, floor(1 / threshold))
    if leverage < 1:
        return None
    # Verify the closed form against the liquidation formula itself.
    if abs(entry - liquidation_price(side, entry, leverage, model.mmr)) < need:
        raise ArithmeticError("closed-form leverage does not satisfy the liquidation buffer")
    return leverage


def liquidation_loss(side: int, entry, lp, model: CostModel) -> Fraction:
    """Price loss per unit when liquidated (pessimistic: filled at ``lp``, plus the liquidation fee)."""
    side = _side(side)
    return side * (_price(entry, "entry") - _price(lp, "liquidation price")) \
        + model.liquidation_fee_rate * Fraction(lp) * model.cost_multiplier
