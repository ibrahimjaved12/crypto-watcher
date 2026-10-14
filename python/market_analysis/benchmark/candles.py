"""Closed, epoch-aligned OHLCV candles from exact 1-minute bars (#182 slice F).

A ``minutes``-candle opens at a UTC epoch multiple of ``minutes`` (a 240-minute
candle opens at 00:00, 04:00, ... UTC; a 1440-minute candle at 00:00 UTC). A weekly
(10080-minute) candle opens on Monday 00:00 UTC: ``(start - WEEK_ANCHOR_MS) % week == 0``
(plain epoch multiples of 7 days start on Thursday, because 1970-01-01 was a Thursday).
Each candle covers exactly ``minutes`` 1-minute bars: open = first minute's open,
high = max high, low = min low, close = last minute's close. A trailing partial candle is never built: every candle is closed.

A candle is VALID only if none of its minutes has a MISSING price or a
``COMPROMISED_FLAGS`` bit. Base volume and taker-buy base volume are sums over
the same complete interval. Invalid candles keep ``MISSING`` in every price and
quantity, never a partial sum; indicators reset on them.

``end_ms[i]`` (open time + minutes) is the end of the candle's last 1-minute bar,
which is exactly a label-grid decision time for the horizon equal to the candle
length (labels-v1: the signal is the end of minute ``signal_ms / 60_000 - 1``).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import data_lake
from .bars import COMPROMISED_FLAGS, MISSING, BarSeries

# The label engine and strategy registry (make_specs, all_specs, GRID_STEP, strategy_config)
# accept TIMEFRAMES only. PATTERN_TIMEFRAMES adds daily and weekly candles for candle-pattern
# and multi-day studies (owner decision 2026-10-15, #348).
TIMEFRAMES = (15, 60, 240)
PATTERN_TIMEFRAMES = (15, 60, 240, 1440, 10080)
WEEK_MINUTES = 10080
WEEK_ANCHOR_MS = 4 * 86_400_000  # 1970-01-05 00:00 UTC, a Monday


def alignment_anchor_ms(minutes: int) -> int:
    return WEEK_ANCHOR_MS if minutes == WEEK_MINUTES else 0


def _check_minutes(minutes) -> None:
    if type(minutes) is not int or minutes not in PATTERN_TIMEFRAMES:
        raise ValueError(f"candle minutes must be one of {PATTERN_TIMEFRAMES}")


def _alignment_error(minutes: int, start_ms) -> str | None:
    """None if ``start_ms`` is a valid candle open time for ``minutes``, else the reason."""
    step = minutes * data_lake.MINUTE_MS
    if type(start_ms) is not int or (start_ms - alignment_anchor_ms(minutes)) % step:
        if minutes == WEEK_MINUTES:
            return f"weekly candles must open on Monday 00:00 UTC, got start {start_ms!r}"
        return f"start {start_ms!r} is not a UTC epoch multiple of {minutes} minutes"
    return None


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
    # Price-only callers remain compatible; absent quantities are unavailable,
    # never fabricated zero volume. build_candles always supplies both columns.
    volume: list = field(default_factory=list)
    taker_buy_volume: list = field(default_factory=list)
    end_ms: list = field(init=False, repr=False)

    def __post_init__(self):
        _check_minutes(self.minutes)
        step = self.minutes * data_lake.MINUTE_MS
        error = _alignment_error(self.minutes, self.start_ms)
        if error:
            raise ValueError(f"start_ms: {error}")
        count = len(self.valid)
        for name in ("volume", "taker_buy_volume"):
            if getattr(self, name) == []:
                object.__setattr__(self, name, [MISSING] * count)
        for name in ("open", "high", "low", "close", "valid", "volume", "taker_buy_volume"):
            column = getattr(self, name)
            if type(column) is not list or len(column) != count:
                raise ValueError(f"column {name} must be a list of {count} values")
        if any(type(value) is not bool for value in self.valid):
            raise ValueError("valid must hold bools")
        for name in ("open", "high", "low", "close", "volume", "taker_buy_volume"):
            if any(type(value) is not int for value in getattr(self, name)):
                raise ValueError(f"column {name} must hold ints")
        object.__setattr__(self, "end_ms", [self.start_ms + (i + 1) * step for i in range(count)])

    def __len__(self) -> int:
        return len(self.valid)

    def open_ms(self, index: int) -> int:
        return self.start_ms + index * self.minutes * data_lake.MINUTE_MS

    def column(self, name: str) -> list:
        """A column with None for invalid/unavailable values (the indicators' input form)."""
        if name in ("volume", "taker_buy_volume"):
            return [value if ok and value != MISSING else None
                    for value, ok in zip(getattr(self, name), self.valid)]
        return [value if ok else None for value, ok in zip(getattr(self, name), self.valid)]


def build_candles(series: BarSeries, minutes: int) -> CandleSeries:
    _check_minutes(minutes)
    error = _alignment_error(minutes, series.start_ms)
    if error:
        raise ValueError(f"{series.symbol} bars cannot build {minutes}-minute candles: {error}")
    columns = {name: [] for name in ("open", "high", "low", "close")}
    volume, taker_buy_volume = [], []
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
        volume.append(sum(series.volume[first:end]) if ok else MISSING)
        taker_buy_volume.append(sum(series.taker_buy_volume[first:end]) if ok else MISSING)
        valid.append(ok)
    return CandleSeries(series.symbol, minutes, series.start_ms, valid=valid,
                        volume=volume, taker_buy_volume=taker_buy_volume, **columns)
