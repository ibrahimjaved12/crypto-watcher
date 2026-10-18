"""Forex symbol spec rows -> ``VenueSymbol`` and ``FxContract`` (#432, part of #374).

Pure: no clock, no I/O. ``fx_contract_from_spec_row`` maps one element returned by the operational
database's ``get_fx_symbol_specs`` (append-only observations of the broker's contract details, the
latest per instrument as of a time) onto ``instruments.VenueSymbol`` and ``fx.costs.FxContract``.
Contract details always come from the broker, never guessed. Numbers are converted from the exact
decimal text of the JSON number (``Fraction(str(value))``), never through float arithmetic. It is
strict like ``calendar_events_from_rows``: a wrong key set, a non-number, a bool where a number is
required or a point that is not ``10**-digits`` raises ``ValueError``.

``tick_value`` is in ``account_currency`` (MT5 reports it in the account currency); this module does
not convert it.
"""
from __future__ import annotations

from fractions import Fraction
from typing import Tuple

from ..instruments import VenueSymbol
from .costs import FxContract

_TEXT_KEYS = frozenset({"venue", "instrument_id", "native_symbol", "base_currency", "profit_currency",
                        "margin_currency", "account_currency", "spec_hash"})
_INT_KEYS = frozenset({"digits", "trade_mode", "swap_mode", "swap_rollover3days", "observed_at_ms"})
_NUMBER_KEYS = frozenset({"point", "tick_size", "tick_value", "contract_size", "volume_min", "volume_max",
                          "volume_step", "swap_long", "swap_short"})
_ROW_KEYS = _TEXT_KEYS | _INT_KEYS | _NUMBER_KEYS


def _check_row(row) -> None:
    if not isinstance(row, dict) or set(row) != _ROW_KEYS:
        raise ValueError(f"spec row must be an object with exactly {sorted(_ROW_KEYS)}")
    for key in _TEXT_KEYS:
        if type(row[key]) is not str or not row[key]:
            raise ValueError(f"spec row {key} must be a non-empty string")
    for key in _INT_KEYS:
        if type(row[key]) is not int:  # rejects bool and float
            raise ValueError(f"spec row {key} must be an integer")
    for key in _NUMBER_KEYS:
        if type(row[key]) not in (int, float):  # rejects bool
            raise ValueError(f"spec row {key} must be a number")


def _exact(row, key: str) -> Fraction:
    return Fraction(str(row[key]))


def fx_point(row) -> Fraction:
    """MT5 SYMBOL_POINT of the row as an exact Fraction; it must equal ``10**-digits``."""
    _check_row(row)
    point = _exact(row, "point")
    if row["digits"] < 0 or point != Fraction(1, 10 ** row["digits"]):
        raise ValueError("spec row point must equal 10**-digits")
    return point


def fx_contract_from_spec_row(row) -> Tuple[VenueSymbol, FxContract]:
    fx_point(row)
    venue_symbol = VenueSymbol(
        venue=row["venue"], native_symbol=row["native_symbol"], instrument_id=row["instrument_id"],
        digits=row["digits"], tick_size=_exact(row, "tick_size"), contract_size=_exact(row, "contract_size"),
    )
    contract = FxContract.from_venue_symbol(
        venue_symbol, min_lot=_exact(row, "volume_min"), lot_step=_exact(row, "volume_step"),
        base=row["base_currency"], quote=row["profit_currency"],
    )
    return venue_symbol, contract


__all__ = ["fx_contract_from_spec_row", "fx_point"]
