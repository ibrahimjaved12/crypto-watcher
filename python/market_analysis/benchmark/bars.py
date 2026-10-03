"""Exact 1-minute bar series for the #182 benchmark harness.

Loads the ``bars1m__SYMBOL__MONTH.csv.gz`` files published by the research data
lake (``market_analysis.data_lake``) into compact ``array('q')`` columns. Prices
and base-asset quantities stay integers scaled by 10**8; a minute without
aggTrades (or without a mark row) holds ``MISSING`` in its price columns. Quote
volumes (scale 10**16, which can overflow int64), kline cross-check columns and
index/premium columns are not loaded.

Every month must be complete: consecutive minutes from the month start, no gap,
no duplicate. Any violation raises ValueError naming the data line and column.
"""
from __future__ import annotations

from array import array
import csv
from dataclasses import dataclass
from functools import reduce
import gzip
import io
from math import gcd
import re

from .. import data_lake

MISSING = -(2 ** 63)
_INTEGER = re.compile(r"-?[0-9]+\Z")
_PRICE_COLUMNS = ("open", "high", "low", "close", "mark_open", "mark_high", "mark_low", "mark_close")
_QUANTITY_COLUMNS = ("volume", "taker_buy_volume")
_COLUMN = {name: position for position, name in enumerate(data_lake.COLUMNS)}
COMPROMISED_FLAGS = (data_lake.FLAG_NO_AGGTRADES | data_lake.FLAG_KLINE_ZERO_BUT_TRADES
                     | data_lake.FLAG_MARK_MISSING)


@dataclass(frozen=True)
class BarSeries:
    """Contiguous minutes of one symbol; index ``i`` is the minute opening at ``start_ms + i*60_000``.

    Scan forward with plain index access to the columns (no copies needed).
    """

    symbol: str
    start_ms: int
    minutes: int
    open: array
    high: array
    low: array
    close: array
    volume: array
    taker_buy_volume: array
    trades: array
    mark_open: array
    mark_high: array
    mark_low: array
    mark_close: array
    flags: array

    def __post_init__(self):
        for name in (*_PRICE_COLUMNS, *_QUANTITY_COLUMNS, "trades", "flags"):
            column = getattr(self, name)
            if not isinstance(column, array) or column.typecode != ("H" if name == "flags" else "q"):
                raise ValueError(f"column {name} must be an array('{'H' if name == 'flags' else 'q'}')")
            if len(column) != self.minutes:
                raise ValueError(f"column {name} has {len(column)} values for {self.minutes} minutes")

    @property
    def end_ms(self) -> int:
        """Exclusive end: open time of the minute after the last one."""
        return self.start_ms + self.minutes * data_lake.MINUTE_MS

    def open_time(self, index: int) -> int:
        return self.start_ms + index * data_lake.MINUTE_MS

    def index_of(self, ms: int) -> int:
        """Minute index of an open time; ValueError if not a minute open time inside the series."""
        offset = ms - self.start_ms
        if type(ms) is not int or offset % data_lake.MINUTE_MS or not 0 <= offset < self.minutes * data_lake.MINUTE_MS:
            raise ValueError(f"{ms!r} is not a minute open time inside {self.symbol} "
                             f"[{self.start_ms}, {self.end_ms})")
        return offset // data_lake.MINUTE_MS

    @staticmethod
    def concat(parts) -> "BarSeries":
        """Exactly contiguous parts of one symbol joined into one series."""
        parts = list(parts)
        if not parts:
            raise ValueError("concat needs at least one bar series")
        first = parts[0]
        for previous, part in zip(parts, parts[1:]):
            if part.symbol != first.symbol:
                raise ValueError(f"cannot concat {part.symbol} onto {first.symbol}")
            if part.start_ms != previous.end_ms:
                raise ValueError(f"bar series are not contiguous: {previous.end_ms} then {part.start_ms}")
        columns = {}
        for name in (*_PRICE_COLUMNS, *_QUANTITY_COLUMNS, "trades", "flags"):
            joined = array("H" if name == "flags" else "q")
            for part in parts:
                joined.extend(getattr(part, name))
            columns[name] = joined
        return BarSeries(first.symbol, first.start_ms, sum(part.minutes for part in parts), **columns)


def _text(fileobj):
    """Text lines of a gzip binary file object, or of an already-text file object."""
    if isinstance(fileobj.read(0), str):
        return fileobj
    return io.TextIOWrapper(gzip.GzipFile(fileobj=fileobj, mode="rb"), encoding="ascii", newline="")


def read_bars_csv(fileobj, symbol: str, month: str) -> BarSeries:
    """One complete month of data-lake bars (gzip binary or text CSV)."""
    data_lake.validate_symbol(symbol)
    start, end, days = data_lake.month_bounds_ms(month)
    expected = days * 1440
    reader = csv.reader(_text(fileobj))
    header = next(reader, None)
    if header is None or ",".join(header) != ",".join(data_lake.COLUMNS):
        raise ValueError(f"{symbol} {month} bars: header differs from the data lake schema "
                         f"{data_lake.LAKE_SCHEMA_VERSION}")
    columns = {name: array("q") for name in (*_PRICE_COLUMNS, *_QUANTITY_COLUMNS, "trades")}
    flags = array("H")
    line = 0
    for row in reader:
        line += 1
        where = f"{symbol} {month} bars line {line}"
        if len(row) != len(data_lake.COLUMNS):
            raise ValueError(f"{where}: expected {len(data_lake.COLUMNS)} columns, got {len(row)}")
        if line > expected:
            raise ValueError(f"{where}: more than the {expected} minutes of {month}")
        field = "open_time_ms"
        try:
            open_time = _integer(row[_COLUMN[field]])
            if open_time != start + (line - 1) * data_lake.MINUTE_MS:
                raise ValueError(f"expected open time {start + (line - 1) * data_lake.MINUTE_MS}, got {open_time} "
                                 "(gap or duplicate minute)")
            for field in _PRICE_COLUMNS:
                text = row[_COLUMN[field]]
                columns[field].append(MISSING if text == "" else _positive_scaled(text))
            for field in _QUANTITY_COLUMNS:
                columns[field].append(_scaled(row[_COLUMN[field]]))
            field = "trades"
            columns[field].append(_count(row[_COLUMN[field]]))
            field = "flags"
            value = _count(row[_COLUMN[field]])
            if value > 0xFFFF:
                raise ValueError(f"flags out of range: {value}")
            flags.append(value)
        except (ValueError, OverflowError) as error:
            raise ValueError(f"{where} column {field}: {error}") from None
    if line != expected:
        raise ValueError(f"{symbol} {month} bars: expected {expected} data lines, got {line}")
    return BarSeries(symbol, start, expected, flags=flags, **columns)


def _integer(text: str) -> int:
    if not _INTEGER.match(text):
        raise ValueError(f"invalid integer {text!r}")
    return int(text)


def _count(text: str) -> int:
    value = _integer(text)
    if value < 0:
        raise ValueError(f"negative count {value}")
    return value


def _scaled(text: str) -> int:
    value = data_lake.parse_published_scaled(text)
    if value < 0:
        raise ValueError(f"negative quantity {text!r}")
    return value


def _positive_scaled(text: str) -> int:
    value = data_lake.parse_published_scaled(text)
    if value <= 0:
        raise ValueError(f"non-positive price {text!r}")
    return value


def infer_tick(series: BarSeries) -> int:
    """Point-in-time tick size (scaled): gcd of every present trade open/high/low/close.

    The Binance exchange-info endpoint is not available to us, so the tick is
    inferred from the prices actually traded in the series.
    """
    prices = (value for column in (series.open, series.high, series.low, series.close)
              for value in column if value != MISSING)
    tick = reduce(gcd, prices, 0)
    if tick <= 0:
        raise ValueError(f"{series.symbol}: no trade prices to infer a tick size from")
    return tick


def compromised_in(series: BarSeries, start_index: int, end_index: int, mask: int = COMPROMISED_FLAGS) -> bool:
    """True when any minute in the inclusive index range has a flag in ``mask``."""
    if not 0 <= start_index <= end_index < series.minutes:
        raise ValueError(f"index range [{start_index}, {end_index}] outside 0..{series.minutes - 1}")
    flags = series.flags
    return any(flags[index] & mask for index in range(start_index, end_index + 1))
