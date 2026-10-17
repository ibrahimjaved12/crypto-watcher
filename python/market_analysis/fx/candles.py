"""Forex candles in trading time (#374): a closed market is no candle, not a gap.

``benchmark.indicators`` resets every indicator on an invalid candle. If weekends were invalid candles,
every Monday would wipe every indicator and a 200-candle EMA could never warm up. So a bucket with no
open minute and no row (weekend, holiday, daily break) is OMITTED: the next candle simply follows in
trading time. A minute missing while the market is open is real data loss and invalidates its candle.

ASSUMPTION (verify on the Exness demo before comparing with the broker's charts): buckets are UTC-epoch
multiples (``FX_CANDLE_ANCHOR_MS = 0``; a 240-minute candle opens at 00:00, 04:00, ... UTC). That equals
a broker whose MT5 server time is GMT+0; compare a downloaded H4 bar with the 240-minute bucket.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from ..benchmark.bars import MISSING
from ..forward.bars_adapter import MINUTE_MS
from ..trading_calendar import TradingCalendar
from .bars import FLAG_MISSING_OPEN, FLAG_PRESENT, FLAG_ROW_WHILE_CLOSED, FxBarSeries

FX_TIMEFRAMES = (15, 60, 240)
FX_CANDLE_ANCHOR_MS = 0
_INT_COLUMNS = ("open_times", "open", "high", "low", "close", "volume", "taker_buy_volume", "end_ms")


@dataclass(frozen=True)
class FxCandleSeries:
    """Duck-types ``benchmark.candles.CandleSeries`` with explicit (non-uniform) open times.

    ``volume`` is the SUM OF TICK VOLUME (a count of price updates), not traded volume; forex CFDs have
    no central tape. ``taker_buy_volume`` is all ``MISSING``. ``partial`` marks a bucket that contains
    closed minutes; its ``end_ms`` is the end of its last open (or printed) minute, so a signal on the
    Friday close is dated at the close, never inside the weekend.
    """

    symbol: str
    minutes: int
    open_times: List[int]
    open: List[int]
    high: List[int]
    low: List[int]
    close: List[int]
    valid: List[bool]
    volume: List[int]
    taker_buy_volume: List[int]
    partial: List[bool]
    end_ms: List[int]

    def __post_init__(self):
        if self.minutes not in FX_TIMEFRAMES:
            raise ValueError(f"minutes must be one of {FX_TIMEFRAMES}")
        count = len(self.valid)
        for name in (*_INT_COLUMNS, "valid", "partial"):
            column = getattr(self, name)
            if type(column) is not list or len(column) != count:
                raise ValueError(f"column {name} must be a list of {count} values")
        for name in ("valid", "partial"):
            if any(type(value) is not bool for value in getattr(self, name)):
                raise ValueError(f"{name} must hold bools")
        for name in _INT_COLUMNS:
            if any(type(value) is not int for value in getattr(self, name)):
                raise ValueError(f"column {name} must hold ints")
        if any(b <= a for a, b in zip(self.open_times, self.open_times[1:])):
            raise ValueError("open_times must be strictly increasing")

    @property
    def start_ms(self) -> int:
        return self.open_times[0] if self.open_times else 0

    def __len__(self) -> int:
        return len(self.valid)

    def open_ms(self, index: int) -> int:
        return self.open_times[index]

    def column(self, name: str) -> list:
        """A column with None for invalid/unavailable values (the indicators' input form)."""
        if name in ("volume", "taker_buy_volume"):
            return [value if ok and value != MISSING else None
                    for value, ok in zip(getattr(self, name), self.valid)]
        return [value if ok else None for value, ok in zip(getattr(self, name), self.valid)]


def build_fx_candles(bars: FxBarSeries, minutes: int, calendar: TradingCalendar) -> FxCandleSeries:
    if minutes not in FX_TIMEFRAMES:
        raise ValueError(f"minutes must be one of {FX_TIMEFRAMES}")
    step = minutes * MINUTE_MS
    first = (bars.start_ms - FX_CANDLE_ANCHOR_MS) // step * step + FX_CANDLE_ANCHOR_MS
    last_minute = bars.end_ms - MINUTE_MS
    out = {name: [] for name in (*_INT_COLUMNS, "valid", "partial")}
    for bucket in range(first, last_minute + 1, step):
        open_slots = present = 0
        has_closed = missing_open = open_outside = False
        last_active = None  # last minute that is open or has a row
        rows = []
        for minute in range(bucket, bucket + step, MINUTE_MS):
            if bars.start_ms <= minute < bars.end_ms:
                index = (minute - bars.start_ms) // MINUTE_MS
                flag = bars.flags[index]
                is_open = flag in (FLAG_PRESENT, FLAG_MISSING_OPEN)
                if flag in (FLAG_PRESENT, FLAG_ROW_WHILE_CLOSED):
                    present += 1
                    rows.append(index)
                missing_open = missing_open or flag == FLAG_MISSING_OPEN
                if is_open or flag == FLAG_ROW_WHILE_CLOSED:
                    last_active = minute
            else:
                is_open = calendar.is_open(minute)
                open_outside = open_outside or is_open
                if is_open:
                    last_active = minute
            open_slots += is_open
            has_closed = has_closed or not is_open
        if open_slots == 0 and present == 0:
            continue  # closed market: no candle, indicators carry on
        if open_outside:
            continue  # an edge bucket the series only partly covers is never built
        valid = not missing_open and present > 0
        if valid:
            out["open"].append(bars.open[rows[0]])
            out["close"].append(bars.close[rows[-1]])
            out["high"].append(max(bars.high[i] for i in rows))
            out["low"].append(min(bars.low[i] for i in rows))
            out["volume"].append(sum(bars.tick_volume[i] for i in rows))
        else:
            for name in ("open", "high", "low", "close", "volume"):
                out[name].append(MISSING)
        out["open_times"].append(bucket)
        out["valid"].append(valid)
        out["taker_buy_volume"].append(MISSING)
        out["partial"].append(has_closed)
        out["end_ms"].append(last_active + MINUTE_MS if has_closed else bucket + step)
    return FxCandleSeries(bars.symbol, minutes, **out)


__all__ = ["FX_CANDLE_ANCHOR_MS", "FX_TIMEFRAMES", "FxCandleSeries", "build_fx_candles"]
