"""Exact 1-minute premium-index columns from the data-lake bars (#182 slice I1).

``bars.read_bars_csv`` skips the ``premium_*`` cross-check columns; this reader
loads them, plus the trade ``close`` and ``flags`` that the funding/basis family
needs for price returns and validity, in one pass over the same
``bars1m__SYMBOL__MONTH.csv.gz`` files.

Premium values are Binance premium-index klines as published (signed decimals,
8 fractional digits), held as integers at scale 10**8; anything finer raises
instead of being rounded. A minute without a premium row holds ``MISSING`` and
carries ``FLAG_PREMIUM_MISSING``; a mismatch between the two is a data error.
Such a minute is invalid for every premium quantity. Months must be complete,
as in ``bars.read_bars_csv``.
"""
from __future__ import annotations

from array import array
import csv
from dataclasses import dataclass

from .. import data_lake
from .bars import COMPROMISED_FLAGS, MISSING, _integer, _text

PREMIUM_COLUMNS = ("premium_open", "premium_high", "premium_low", "premium_close")
_COLUMN = {name: position for position, name in enumerate(data_lake.COLUMNS)}


@dataclass(frozen=True)
class PremiumSeries:
    """Contiguous minutes of one symbol; index ``i`` is the minute opening at ``start_ms + i*60_000``."""

    symbol: str
    start_ms: int
    minutes: int
    premium_open: array
    premium_high: array
    premium_low: array
    premium_close: array
    close: array
    flags: array

    def __post_init__(self):
        for name in (*PREMIUM_COLUMNS, "close", "flags"):
            column = getattr(self, name)
            if not isinstance(column, array) or column.typecode != ("H" if name == "flags" else "q"):
                raise ValueError(f"column {name} must be an array('{'H' if name == 'flags' else 'q'}')")
            if len(column) != self.minutes:
                raise ValueError(f"column {name} has {len(column)} values for {self.minutes} minutes")

    @property
    def end_ms(self) -> int:
        return self.start_ms + self.minutes * data_lake.MINUTE_MS

    def premium_valid(self, index: int) -> bool:
        return not self.flags[index] & data_lake.FLAG_PREMIUM_MISSING

    def price_valid(self, index: int) -> bool:
        """A trade close exists and the minute carries no COMPROMISED flag."""
        return self.close[index] != MISSING and not self.flags[index] & COMPROMISED_FLAGS

    @staticmethod
    def concat(parts) -> "PremiumSeries":
        parts = list(parts)
        if not parts:
            raise ValueError("concat needs at least one premium series")
        first = parts[0]
        for previous, part in zip(parts, parts[1:]):
            if part.symbol != first.symbol:
                raise ValueError(f"cannot concat {part.symbol} onto {first.symbol}")
            if part.start_ms != previous.end_ms:
                raise ValueError(f"premium series are not contiguous: {previous.end_ms} then {part.start_ms}")
        columns = {}
        for name in (*PREMIUM_COLUMNS, "close", "flags"):
            joined = array("H" if name == "flags" else "q")
            for part in parts:
                joined.extend(getattr(part, name))
            columns[name] = joined
        return PremiumSeries(first.symbol, first.start_ms, sum(part.minutes for part in parts), **columns)


def read_premium_csv(fileobj, symbol: str, month: str) -> PremiumSeries:
    """One complete month of premium columns (+ close, flags) from data-lake bars."""
    data_lake.validate_symbol(symbol)
    start, _, days = data_lake.month_bounds_ms(month)
    expected = days * 1440
    reader = csv.reader(_text(fileobj))
    header = next(reader, None)
    if header is None or ",".join(header) != ",".join(data_lake.COLUMNS):
        raise ValueError(f"{symbol} {month} bars: header differs from the data lake schema "
                         f"{data_lake.LAKE_SCHEMA_VERSION}")
    columns = {name: array("q") for name in (*PREMIUM_COLUMNS, "close")}
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
            if _integer(row[_COLUMN[field]]) != start + (line - 1) * data_lake.MINUTE_MS:
                raise ValueError("gap or duplicate minute")
            field = "flags"
            value = _integer(row[_COLUMN[field]])
            if not 0 <= value <= 0xFFFF:
                raise ValueError(f"flags out of range: {value}")
            flags.append(value)
            texts = [row[_COLUMN[name]] for name in PREMIUM_COLUMNS]
            missing = value & data_lake.FLAG_PREMIUM_MISSING
            if any(texts) == bool(missing) or (not missing and not all(texts)):
                raise ValueError("premium values and the PREMIUM_MISSING flag disagree")
            for field, text in zip(PREMIUM_COLUMNS, texts):
                columns[field].append(MISSING if missing else data_lake.parse_published_scaled(text))
            field = "close"
            text = row[_COLUMN[field]]
            close = MISSING if text == "" else data_lake.parse_published_scaled(text)
            if close != MISSING and close <= 0:
                raise ValueError(f"non-positive price {text!r}")
            columns[field].append(close)
        except (ValueError, OverflowError) as error:
            raise ValueError(f"{where} column {field}: {error}") from None
    if line != expected:
        raise ValueError(f"{symbol} {month} bars: expected {expected} data lines, got {line}")
    return PremiumSeries(symbol, start, expected, flags=flags, **columns)
