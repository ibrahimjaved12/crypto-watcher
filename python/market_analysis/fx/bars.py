"""Forex 1-minute BID bars classified against a trading calendar (#374).

Unlike the crypto ``BarSeries`` (every minute expected, 24/7), a forex series knows when the market
is closed: a minute without a row while the calendar is closed is EXPECTED (``FLAG_CLOSED``), while a
minute without a row while the calendar is open is real data loss (``FLAG_MISSING_OPEN``). A row
stamped while the calendar says closed is kept and counted (``FLAG_ROW_WHILE_CLOSED``), never dropped.
Prices are BID prices scaled to 10**8; ``spread`` is ask - bid in the same scale.
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass
from fractions import Fraction

from ..benchmark.bars import MISSING
from ..forward.bars_adapter import MINUTE_MS, CollectorRowError, to_scaled
from ..instruments import Instrument, VenueSymbol
from ..trading_calendar import TradingCalendar

FLAG_PRESENT = 0
FLAG_MISSING_OPEN = 1
FLAG_CLOSED = 2
FLAG_ROW_WHILE_CLOSED = 4

TRANSPORTS = ("poll", "history", "stream")
_SCALE = 10 ** 8
_PRICE_COLUMNS = ("open", "high", "low", "close", "spread", "tick_volume")


@dataclass(frozen=True)
class FxBarSeries:
    instrument_id: str
    venue: str
    symbol: str
    calendar_id: str
    start_ms: int
    minutes: int
    tick: int
    open: array
    high: array
    low: array
    close: array
    spread: array
    tick_volume: array
    flags: array

    def __post_init__(self):
        for name in ("instrument_id", "venue", "symbol", "calendar_id"):
            if type(getattr(self, name)) is not str or not getattr(self, name):
                raise ValueError(f"{name} must be a non-empty string")
        for name in ("start_ms", "minutes", "tick"):
            if type(getattr(self, name)) is not int:
                raise ValueError(f"{name} must be an int")
        if self.start_ms % MINUTE_MS or self.minutes < 1 or self.tick < 1:
            raise ValueError("start_ms must be minute-aligned; minutes and tick must be positive")
        for name in (*_PRICE_COLUMNS, "flags"):
            column = getattr(self, name)
            typecode = "H" if name == "flags" else "q"
            if not isinstance(column, array) or column.typecode != typecode:
                raise ValueError(f"column {name} must be an array('{typecode}')")
            if len(column) != self.minutes:
                raise ValueError(f"column {name} has {len(column)} values for {self.minutes} minutes")

    @property
    def end_ms(self) -> int:
        return self.start_ms + self.minutes * MINUTE_MS

    def open_time(self, index: int) -> int:
        return self.start_ms + index * MINUTE_MS

    def index_of(self, ms: int) -> int:
        if type(ms) is not int or ms % MINUTE_MS or not self.start_ms <= ms < self.end_ms:
            raise ValueError(f"{ms!r} is not a minute of this series")
        return (ms - self.start_ms) // MINUTE_MS

    def anomalies(self) -> dict:
        return {"missing_open": sum(1 for flag in self.flags if flag == FLAG_MISSING_OPEN),
                "row_while_closed": sum(1 for flag in self.flags if flag == FLAG_ROW_WHILE_CLOSED)}


def _check_row(venue_symbol: VenueSymbol, point: Fraction, row, index: int) -> tuple:
    where = f"row {index}"
    if not isinstance(row, dict):
        raise CollectorRowError("malformed", f"{where} is not an object")
    if row.get("time_basis") != "utc":
        raise CollectorRowError("time_basis", f"{where} is not UTC (broker server time must be converted by the gateway)")
    if row.get("venue") != venue_symbol.venue or row.get("native_symbol") != venue_symbol.native_symbol:
        raise CollectorRowError("identity", f"{where} is not {venue_symbol.venue} {venue_symbol.native_symbol}")
    if row.get("transport") not in TRANSPORTS:
        raise CollectorRowError("provenance", f"{where} transport must be one of {TRANSPORTS}")
    open_ms = row.get("open_time_ms")
    if type(open_ms) is not int or open_ms % MINUTE_MS:
        raise CollectorRowError("alignment", f"{where} open_time_ms is not a UTC minute boundary")
    prices = [to_scaled(row.get(name)) for name in ("open", "high", "low", "close")]
    if min(prices) <= 0:
        raise CollectorRowError("ohlc", f"{where} prices must be positive")
    o, h, l, c = prices
    if h < max(o, c) or l > min(o, c):
        raise CollectorRowError("ohlc", f"{where} OHLC is not a valid candle")
    points = row.get("spread_points")
    if type(points) is not int or points < 0:
        raise CollectorRowError("spread", f"{where} spread_points must be an int >= 0")
    spread = points * point * _SCALE
    if spread.denominator != 1:
        raise CollectorRowError("spread", f"{where} spread is not representable at 10**8")
    volume = row.get("tick_volume")
    if type(volume) is not int or volume < 0:
        raise CollectorRowError("volume", f"{where} tick_volume must be an int >= 0")
    return open_ms, o, h, l, c, int(spread), volume


def fx_bars_from_rows(instrument: Instrument, venue_symbol: VenueSymbol, calendar: TradingCalendar,
                      rows) -> FxBarSeries:
    """Gateway rows (UTC, one per traded minute) -> a calendar-classified series spanning the first to
    the last row's minute (not aligned to any candle boundary)."""
    if venue_symbol.instrument_id != instrument.instrument_id:
        raise ValueError("venue symbol belongs to another instrument")
    if venue_symbol.tick_size is None:
        raise ValueError("tick size must come from the broker")
    point = Fraction(venue_symbol.tick_size)
    tick = point * _SCALE
    if tick.denominator != 1 or tick < 1:
        raise ValueError("tick size must be a positive integer at 10**8")
    rows = list(rows)
    if not rows:
        raise CollectorRowError("empty", "no rows")
    checked = []
    for index, row in enumerate(rows):
        current = _check_row(venue_symbol, point, row, index)
        if checked and current[0] <= checked[-1][0]:
            raise CollectorRowError("order", f"open time {current[0]} is not after {checked[-1][0]}")
        checked.append(current)
    start = checked[0][0]
    minutes = (checked[-1][0] - start) // MINUTE_MS + 1
    columns = {name: array("q", [MISSING]) * minutes for name in _PRICE_COLUMNS}
    flags = array("H", [FLAG_CLOSED]) * minutes
    for open_ms, *values in checked:
        offset = (open_ms - start) // MINUTE_MS
        for name, value in zip(_PRICE_COLUMNS, values):
            columns[name][offset] = value
    for offset in range(minutes):
        is_open = calendar.is_open(start + offset * MINUTE_MS)
        if columns["open"][offset] != MISSING:
            flags[offset] = FLAG_PRESENT if is_open else FLAG_ROW_WHILE_CLOSED
        else:
            flags[offset] = FLAG_MISSING_OPEN if is_open else FLAG_CLOSED
    return FxBarSeries(instrument.instrument_id, venue_symbol.venue, instrument.instrument_id.split(":", 1)[1],
                       calendar.calendar_id, start, minutes, int(tick), flags=flags, **columns)


def missing_open_minutes(bars: FxBarSeries) -> int:
    return bars.anomalies()["missing_open"]


__all__ = [
    "FLAG_CLOSED", "FLAG_MISSING_OPEN", "FLAG_PRESENT", "FLAG_ROW_WHILE_CLOSED", "FxBarSeries", "TRANSPORTS",
    "fx_bars_from_rows", "missing_open_minutes",
]
