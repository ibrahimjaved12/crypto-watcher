"""Exact forex CFD costs (#374): spread, slippage, commission, swap, sizing and stop-out.

Conventions:
- MT5 candles are BID candles; each bar also carries ``spread`` (ask - bid). ``ask = bid + spread``.
- A long buys at the ask and sells at the bid; a short sells at the bid and buys at the ask.
- Triggers on bid candles: long stop ``bid_low <= stop``; long target ``bid_high >= target``;
  short stop (a buy at the ask) ``bid_high + spread >= stop``; short target ``bid_low + spread <= target``.
- The entry reference ``p0`` is the executable price before slippage (long ``bid_open + spread``,
  short ``bid_open``); stop and target are measured from it, so a stop is exactly -1 R and a target
  +rr R before slippage, commission and swap, and the spread shows up symmetrically as a tighter stop
  and a harder target rather than as a hidden P&L term.

Every function is pure and exact (``int`` / ``Fraction``, never floats). Price-to-price functions
(fills, slippage, triggers, stop-out price, max admissible leverage) are unit-agnostic: their
results are in the unit of their price arguments (scaled ints or real-price Fractions). Functions
that combine a price with base units or account money need REAL price units (callers convert scaled
ints with ``Fraction(x, 10**8)``): ``units_for_risk`` and ``required_margin``. Cash results
(``commission_cash``, ``swap_cash_quote``, ``required_margin``) are real currency amounts. Sides are +1 (long) and -1 (short). The Binance perpetual model in
``market_analysis.benchmark.costs`` is separate and unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from fractions import Fraction
import hashlib
from math import floor
from typing import Mapping, Optional, Protocol

from .. import data_lake
from ..instruments import VenueSymbol

_FRACTION_FIELDS = ("market_slip_spread_fraction", "market_slip_range_mult", "stop_slip_spread_fraction",
                    "stop_slip_range_mult", "commission_per_lot_side", "cost_multiplier", "swap_multiplier",
                    "stop_out_level")
_INT_FIELDS = ("leverage_cap", "stop_out_buffer_multiple")


def _exact_text(value: Fraction) -> str:
    return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


def _side(side: int) -> int:
    if type(side) is not int or side not in (1, -1):
        raise ValueError(f"side must be +1 (long) or -1 (short), got {side!r}")
    return side


def _price(value, name: str) -> Fraction:
    if type(value) not in (int, Fraction) or value <= 0:
        raise ValueError(f"{name} must be a positive int or Fraction, got {value!r}")
    return Fraction(value)


def _spread(value) -> Fraction:
    """Spreads may be zero (Raw/Zero accounts quote zero spreads), never negative."""
    if type(value) not in (int, Fraction) or value < 0:
        raise ValueError(f"spread must be a non-negative int or Fraction, got {value!r}")
    return Fraction(value)


def _range(high, low) -> Fraction:
    high, low = _price(high, "high"), _price(low, "low")
    if high < low:
        raise ValueError(f"high {high} below low {low}")
    return high - low


def _currency(value, name: str) -> str:
    if type(value) is not str or len(value) != 3 or not value.isalpha() or not value.isupper():
        raise ValueError(f"{name} must be a three-letter upper-case currency code, got {value!r}")
    return value


@dataclass(frozen=True)
class FxContract:
    """Contract details from the broker's symbol info (never guessed)."""

    contract_size: Fraction
    tick_size: Fraction
    min_lot: Fraction
    lot_step: Fraction
    base: str
    quote: str

    def __post_init__(self):
        for name in ("contract_size", "tick_size", "min_lot", "lot_step"):
            object.__setattr__(self, name, _price(getattr(self, name), name))
        if self.lot_step > self.min_lot or self.min_lot % self.lot_step:
            raise ValueError("min_lot must be a whole multiple of lot_step")
        if type(self.base) is not str or not self.base:
            raise ValueError("base must be a non-empty string")  # XAU, USOIL: not always a currency
        _currency(self.quote, "quote")

    @classmethod
    def from_venue_symbol(cls, venue_symbol: VenueSymbol, *, min_lot, lot_step, base: str,
                          quote: str) -> "FxContract":
        if venue_symbol.contract_size is None or venue_symbol.tick_size is None:
            raise ValueError("contract details must come from the broker")
        return cls(venue_symbol.contract_size, venue_symbol.tick_size, min_lot, lot_step, base, quote)


@dataclass(frozen=True)
class SwapRates:
    """Quote-currency cash per 1 base unit per rollover; POSITIVE = received."""

    long_per_unit: Fraction
    short_per_unit: Fraction

    def __post_init__(self):
        for name in ("long_per_unit", "short_per_unit"):
            value = getattr(self, name)
            if type(value) not in (int, Fraction):
                raise ValueError(f"{name} must be an int or Fraction, got {value!r}")
            object.__setattr__(self, name, Fraction(value))


class CostModelLike(Protocol):
    """A versioned, identity-hashed cost model. ``benchmark.costs.CostModel`` and ``FxCostModel``
    both satisfy it."""

    version: str

    def to_record(self) -> dict: ...

    def identity(self) -> str: ...


@dataclass(frozen=True)
class FxCostModel:
    version: str
    account_currency: str = "USD"
    market_slip_spread_fraction: Fraction = Fraction(1, 10)
    market_slip_range_mult: Fraction = Fraction(1, 20)
    stop_slip_spread_fraction: Fraction = Fraction(1, 2)
    stop_slip_range_mult: Fraction = Fraction(1, 4)
    commission_per_lot_side: Fraction = Fraction(0)
    cost_multiplier: Fraction = Fraction(1)
    swap_multiplier: Fraction = Fraction(1)
    leverage_cap: int = 100
    stop_out_level: Fraction = Fraction(1, 2)
    stop_out_buffer_multiple: int = 2

    def __post_init__(self):
        if type(self.version) is not str or not self.version:
            raise ValueError("cost model version must be a non-empty string")
        _currency(self.account_currency, "account_currency")
        for name in _FRACTION_FIELDS:
            value = getattr(self, name)
            if type(value) is int:
                value = Fraction(value)
                object.__setattr__(self, name, value)
            if type(value) is not Fraction or value < 0:
                raise ValueError(f"{name} must be a non-negative Fraction, got {value!r}")
        if self.stop_out_level >= 1:
            raise ValueError("stop_out_level must be below 1")
        for name in _INT_FIELDS:
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


FX_COST_MODEL_V1 = FxCostModel(version="fx-cost-v1")

ASSUMPTIONS_FX_V1 = (
    "Every FxCostModel default (slippage fractions, commission, swap multiplier, leverage_cap, stop_out_level, "
    "stop_out_buffer_multiple) is an UNVERIFIED placeholder until read from the demo account.",
    "commission_per_lot_side 0 assumes a Standard account, where the cost is inside the spread; Raw/Zero "
    "accounts charge a commission per lot and side.",
    "leverage_cap (100x) and stop_out_level (50%) must be read from the broker's account info before any "
    "result is trusted.",
    "The market and stop slippage parameters (fractions of the bar's spread and of its range) are UNVERIFIED "
    "placeholders chosen to be conservative; real slippage widens at news and rollover.",
)


def with_multiplier(model: FxCostModel, multiplier) -> FxCostModel:
    """Copy with ``cost_multiplier`` (slippage and commission) set, for the 0x/1x/2x/3x cost grid.

    The value goes through ``FxCostModel`` validation unchanged: an ``int`` or ``Fraction`` only
    (floats, bools and strings are rejected, never converted).
    """
    return replace(model, cost_multiplier=multiplier)


# ---------------------------------------------------------------- prices and fills


def ask(bid, spread) -> Fraction:
    return _price(bid, "bid") + _spread(spread)


def entry_reference(side: int, bid_open, spread) -> Fraction:
    """Executable price before slippage: a long buys at the ask, a short sells at the bid."""
    bid_open = _price(bid_open, "bid open")
    return bid_open + _spread(spread) if _side(side) == 1 else bid_open


def _slippage(spread, high, low, spread_fraction: Fraction, range_mult: Fraction,
              model: FxCostModel) -> Fraction:
    return model.cost_multiplier * max(_spread(spread) * spread_fraction, range_mult * _range(high, low))


def market_slippage(spread, high, low, model: FxCostModel) -> Fraction:
    return _slippage(spread, high, low, model.market_slip_spread_fraction, model.market_slip_range_mult, model)


def stop_slippage(spread, high, low, model: FxCostModel) -> Fraction:
    return _slippage(spread, high, low, model.stop_slip_spread_fraction, model.stop_slip_range_mult, model)


def entry_fill_price(side: int, p0, spread, high, low, model: FxCostModel) -> Fraction:
    return _price(p0, "p0") + _side(side) * market_slippage(spread, high, low, model)


def exit_price_at_market(side: int, bid, spread) -> Fraction:
    """A long sells at the bid; a short buys at the ask."""
    bid = _price(bid, "bid")
    return bid if _side(side) == 1 else bid + _spread(spread)


def market_exit_fill(side: int, bid, spread, high, low, model: FxCostModel) -> Fraction:
    return exit_price_at_market(side, bid, spread) - side * market_slippage(spread, high, low, model)


def stop_fill_price(side: int, stop, bid_open, spread, high, low, model: FxCostModel) -> Fraction:
    """A long's stop sells at ``min(stop, bid_open) - slip``; a short's buys at
    ``max(stop, ask_open) + slip``. A gap through the stop fills from the worse open."""
    side = _side(side)
    stop, bid_open = _price(stop, "stop"), _price(bid_open, "bid open")
    slip = stop_slippage(spread, high, low, model)
    if side == 1:
        return min(stop, bid_open) - slip
    return max(stop, bid_open + _spread(spread)) + slip


def target_fill_price(target) -> Fraction:
    """Limit fill at the target; no price improvement assumed (same as crypto)."""
    return _price(target, "target")


def stop_hit(side: int, stop, spread, high, low) -> bool:
    stop, spread = _price(stop, "stop"), _spread(spread)
    _range(high, low)
    return low <= stop if _side(side) == 1 else high + spread >= stop


def target_hit(side: int, target, spread, high, low) -> bool:
    target, spread = _price(target, "target"), _spread(spread)
    _range(high, low)
    return high >= target if _side(side) == 1 else low + spread <= target


# ---------------------------------------------------------------- cash


def commission_cash(units, contract: FxContract, model: FxCostModel) -> Fraction:
    """Account-currency commission for one side of a fill of ``units`` base units."""
    return _price(units, "units") / contract.contract_size * model.commission_per_lot_side * model.cost_multiplier


def swap_cash_quote(side: int, units, multiplier: int, rates: SwapRates, model: FxCostModel) -> Fraction:
    """Quote-currency swap for one rollover (positive = received); ``multiplier`` is 1 or 3 from the
    trading calendar's ``rollover_events``."""
    side = _side(side)
    units = _price(units, "units")
    if type(multiplier) is not int or multiplier < 1:
        raise ValueError(f"rollover multiplier must be a positive int, got {multiplier!r}")
    rate = rates.long_per_unit if side == 1 else rates.short_per_unit
    return units * rate * multiplier * model.swap_multiplier


def quote_to_account_rate(quote: str, account: str, prices: Mapping[str, object]) -> Fraction:
    """Account-currency value of 1 quote unit, from a direct (QUOTEACCOUNT) or inverse
    (ACCOUNTQUOTE) pair price in ``prices``."""
    quote, account = _currency(quote, "quote"), _currency(account, "account")
    if quote == account:
        return Fraction(1)
    if quote + account in prices:
        return _price(prices[quote + account], quote + account)
    if account + quote in prices:
        return 1 / _price(prices[account + quote], account + quote)
    raise ValueError(f"no conversion path from {quote} to {account}")


# ---------------------------------------------------------------- sizing and margin


def units_for_risk(risk_account, risk_distance_price, quote_to_account, contract: FxContract) -> Optional[Fraction]:
    """Base units whose loss over ``risk_distance_price`` equals ``risk_account``, floored to the lot
    step; ``None`` below the minimum lot.

    ``risk_distance_price`` must be in REAL price units (callers convert scaled ints with
    ``Fraction(x, 10**8)``), because ``contract_size`` and the account money are real units.
    """
    risk = _price(risk_account, "risk_account")
    distance = _price(risk_distance_price, "risk_distance_price")
    rate = _price(quote_to_account, "quote_to_account")
    step = contract.lot_step * contract.contract_size
    units = floor(risk / (distance * rate) / step) * step
    return None if units < contract.min_lot * contract.contract_size else Fraction(units)


def required_margin(units, price, leverage: int, quote_to_account) -> Fraction:
    """Account-currency margin ``units * price * quote_to_account / leverage``.

    ``price`` must be in REAL price units for a real cash result: EURUSD ``Fraction("1.08")`` with
    5000 units at 100x gives 54 USD, whereas the scaled ``108000000`` would give 5,400,000,000.
    """
    if type(leverage) is not int or leverage < 1:
        raise ValueError(f"leverage must be an int >= 1, got {leverage!r}")
    return (_price(units, "units") * _price(price, "price") * _price(quote_to_account, "quote_to_account")
            / leverage)


def max_admissible_leverage(fill, stop_distance, model: FxCostModel) -> Optional[int]:
    """Highest integer leverage whose stop-out is at least ``stop_out_buffer_multiple`` stop distances
    away, capped at ``leverage_cap``; ``None`` if even 1x fails.

    The isolated stop-out distance per unit price is ``fill * (1 - stop_out_level) / L``, independent
    of size and currency conversion; it must be ``>= buffer * stop_distance``.
    """
    fill, distance = _price(fill, "fill"), _price(stop_distance, "stop_distance")
    leverage = min(floor(fill * (1 - model.stop_out_level) / (model.stop_out_buffer_multiple * distance)),
                   model.leverage_cap)
    return leverage if leverage >= 1 else None


def stop_out_price(side: int, fill, leverage: int, model: FxCostModel) -> Fraction:
    """Long: below the fill on the bid; short: above the fill on the ask."""
    if type(leverage) is not int or leverage < 1:
        raise ValueError(f"leverage must be an int >= 1, got {leverage!r}")
    fill = _price(fill, "fill")
    return fill - _side(side) * fill * (1 - model.stop_out_level) / leverage


__all__ = [
    "ASSUMPTIONS_FX_V1", "CostModelLike", "FX_COST_MODEL_V1", "FxContract", "FxCostModel", "SwapRates",
    "ask", "commission_cash", "entry_fill_price", "entry_reference", "exit_price_at_market",
    "market_exit_fill", "market_slippage", "max_admissible_leverage", "quote_to_account_rate",
    "required_margin", "stop_fill_price", "stop_hit", "stop_out_price", "stop_slippage", "swap_cash_quote",
    "target_fill_price", "target_hit", "units_for_risk", "with_multiplier",
]
