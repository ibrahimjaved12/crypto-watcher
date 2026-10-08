"""Closed, epoch-aligned OHLC candles from exact 1-minute bars (#182 slice F).

A ``minutes``-candle opens at a UTC epoch multiple of ``minutes`` (a 240-minute
candle opens at 00:00, 04:00, ... UTC) and covers exactly ``minutes`` 1-minute
bars: open = first minute's open, high = max high, low = min low, close = last
minute's close. A trailing partial candle is never built: every candle is closed.

A candle is VALID only if none of its minutes has a MISSING price or a
``COMPROMISED_FLAGS`` bit. Invalid candles keep ``MISSING`` in every price so no
consumer can mistake them for data; indicators reset on them.

``end_ms[i]`` (open time + minutes) is the end of the candle's last 1-minute bar,
which is exactly a label-grid decision time for the horizon equal to the candle
length (labels-v1: the signal is the end of minute ``signal_ms / 60_000 - 1``).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import data_lake
from .bars import COMPROMISED_FLAGS, MISSING, BarSeries

TIMEFRAMES = (15, 60, 240)


@dataclass(frozen=True)
class CandleSeries:
    symbol: str
    minutes: int
    start_ms: int
    open: list
    high: list
    low: list
    close: list
    valid: list
    end_ms: list = field(init=False, repr=False)

    def __post_init__(self):
        if self.minutes not in TIMEFRAMES or type(self.minutes) is not int:
            raise ValueError(f"candle minutes must be one of {TIMEFRAMES}")
        step = self.minutes * data_lake.MINUTE_MS
        if type(self.start_ms) is not int or self.start_ms % step:
            raise ValueError(f"start_ms must be a UTC epoch multiple of {self.minutes} minutes")
        count = len(self.valid)
        for name in ("open", "high", "low", "close", "valid"):
            column = getattr(self, name)
            if type(column) is not list or len(column) != count:
                raise ValueError(f"column {name} must be a list of {count} values")
        if any(type(value) is not bool for value in self.valid):
            raise ValueError("valid must hold bools")
        for name in ("open", "high", "low", "close"):
            if any(type(value) is not int for value in getattr(self, name)):
                raise ValueError(f"column {name} must hold ints")
        object.__setattr__(self, "end_ms", [self.start_ms + (i + 1) * step for i in range(count)])

    def __len__(self) -> int:
        return len(self.valid)

    def open_ms(self, index: int) -> int:
        return self.start_ms + index * self.minutes * data_lake.MINUTE_MS

    def column(self, name: str) -> list:
        """A price column with None for invalid candles (the indicators' input form)."""
        return [value if ok else None for value, ok in zip(getattr(self, name), self.valid)]


def build_candles(series: BarSeries, minutes: int) -> CandleSeries:
    if type(minutes) is not int or minutes not in TIMEFRAMES:
        raise ValueError(f"candle minutes must be one of {TIMEFRAMES}")
    if series.start_ms % (minutes * data_lake.MINUTE_MS):
        raise ValueError(f"{series.symbol} bars start at {series.start_ms}, not on a {minutes}-minute epoch boundary")
    columns = {name: [] for name in ("open", "high", "low", "close")}
    valid = []
    for first in range(0, series.minutes - minutes + 1, minutes):
        end = first + minutes
        highs, lows = series.high[first:end], series.low[first:end]
        ok = (MISSING not in series.open[first:end] and MISSING not in series.close[first:end]
              and MISSING not in highs and MISSING not in lows
              and not any(flag & COMPROMISED_FLAGS for flag in series.flags[first:end]))
        if ok:
            values = (series.open[first], max(highs), min(lows), series.close[end - 1])
        else:
            values = (MISSING,) * 4
        for name, value in zip(("open", "high", "low", "close"), values):
            columns[name].append(int(value))
        valid.append(ok)
    return CandleSeries(series.symbol, minutes, series.start_ms, valid=valid, **columns)
