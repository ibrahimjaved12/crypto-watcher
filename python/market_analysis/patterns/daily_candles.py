"""Daily and weekly candles from the ``dk1`` daily history, and its parity with 1m-built days (#348).

``candles_from_daily`` turns a ``daily_lake.DailySeries`` (array('q') columns scaled by
10**8, ``MISSING`` on a gap day) into a ``CandleSeries`` of 1440 or 10080 minutes. The
scaled integers are copied unchanged, so the result has the same scale as candles built
from 1-minute bars.

A day is price-valid iff open, high, low and close are all present. A MISSING base
volume does NOT invalidate prices: ``load_symbol_daily`` stores a base volume that
overflows int64 at scale 10**8 (DOGE, XRP) as MISSING while its prices fit. Volume and
taker-buy volume are carried per day; a price-invalid day keeps MISSING in every column.

Weekly candles group 7 consecutive days starting Monday 00:00 UTC. The first week starts
at the first Monday 00:00 UTC at or after ``daily.start_ms``; a leading and a trailing
partial week are dropped. A week is price-valid iff all 7 days are: open = Monday open,
close = Sunday close, high/low = extremes. Its volume and taker-buy volume are the sums
only if all 7 days have them, else MISSING (never a partial sum).

``daily_parity`` compares the dk1 days with 1440-minute candles built from 1-minute bars;
it is pure (no I/O) and returns only ints and strings.
"""
from __future__ import annotations

from dataclasses import fields, replace
from datetime import datetime, timezone

from .. import data_lake
from ..benchmark.bars import MISSING, BarSeries
from ..benchmark.candles import WEEK_ANCHOR_MS, WEEK_MINUTES, CandleSeries, build_candles
from ..daily_lake import DAY_MS, DailySeries

DAILY_MINUTES = 1440
_PRICES = ("open", "high", "low", "close")
_QUANTITIES = ("volume", "taker_buy_volume")
PARITY_FIELDS = (*_PRICES, *_QUANTITIES)
_MAX_LISTED_DATES = 10
_PPM = 1_000_000


def _price_valid(daily: DailySeries, index: int) -> bool:
    return all(getattr(daily, name)[index] != MISSING for name in _PRICES)


def _daily_candles(daily: DailySeries) -> CandleSeries:
    columns = {name: [] for name in PARITY_FIELDS}
    valid = []
    for index in range(daily.days):
        ok = _price_valid(daily, index)
        for name in PARITY_FIELDS:
            columns[name].append(int(getattr(daily, name)[index]) if ok else MISSING)
        valid.append(ok)
    return CandleSeries(daily.symbol, DAILY_MINUTES, daily.start_ms, valid=valid, **columns)


def first_monday_ms(ms: int) -> int:
    """The first Monday 00:00 UTC at or after ``ms``."""
    week = WEEK_MINUTES * data_lake.MINUTE_MS
    return ms + (-(ms - WEEK_ANCHOR_MS)) % week


def _weekly_candles(daily: DailySeries) -> CandleSeries:
    start = first_monday_ms(daily.start_ms)
    first_day = (start - daily.start_ms) // DAY_MS
    weeks = max(0, (daily.days - first_day) // 7)
    columns = {name: [] for name in PARITY_FIELDS}
    valid = []
    for week in range(weeks):
        days = range(first_day + 7 * week, first_day + 7 * week + 7)
        ok = all(_price_valid(daily, day) for day in days)
        if ok:
            columns["open"].append(int(daily.open[days[0]]))
            columns["high"].append(max(int(daily.high[day]) for day in days))
            columns["low"].append(min(int(daily.low[day]) for day in days))
            columns["close"].append(int(daily.close[days[-1]]))
        else:
            for name in _PRICES:
                columns[name].append(MISSING)
        for name in _QUANTITIES:
            values = [int(getattr(daily, name)[day]) for day in days]
            columns[name].append(sum(values) if ok and MISSING not in values else MISSING)
        valid.append(ok)
    return CandleSeries(daily.symbol, WEEK_MINUTES, start, valid=valid, **columns)


def candles_from_daily(daily: DailySeries, minutes: int) -> CandleSeries:
    """1440-minute (one per dk1 day) or 10080-minute (Monday-aligned) candles of ``daily``."""
    if type(minutes) is not int or minutes not in (DAILY_MINUTES, WEEK_MINUTES):
        raise ValueError(f"daily candles support minutes {DAILY_MINUTES} or {WEEK_MINUTES}, got {minutes!r}")
    if daily.start_ms % DAY_MS:
        raise ValueError(f"{daily.symbol} daily series starts at {daily.start_ms}, not a UTC day start")
    return _daily_candles(daily) if minutes == DAILY_MINUTES else _weekly_candles(daily)


def _day_aligned(bars: BarSeries) -> BarSeries | None:
    """``bars`` from their first UTC day start on (None if no minute is left)."""
    offset = (-bars.start_ms % DAY_MS) // data_lake.MINUTE_MS
    if offset == 0:
        return bars
    if offset >= bars.minutes:
        return None
    columns = {item.name: getattr(bars, item.name)[offset:] for item in fields(bars)
               if item.name not in ("symbol", "start_ms", "minutes")}
    return replace(bars, start_ms=bars.start_ms + offset * data_lake.MINUTE_MS,
                   minutes=bars.minutes - offset, **columns)


def _relative_ppm(a: int, b: int) -> int:
    """floor(|a - b| * 10**6 / max(|a|, |b|)); 0 when equal (symmetric, bounded by 2 * 10**6)."""
    if a == b:
        return 0
    return abs(a - b) * _PPM // max(abs(a), abs(b))


def _date(ms: int) -> str:
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def daily_parity(daily: DailySeries, bars: BarSeries) -> dict:
    """Compare dk1 days with the 1440-minute candles built from ``bars``.

    A day is compared iff it is fully covered by both and price-valid in both (a
    1m-built day is valid only if none of its minutes is missing or compromised).
    Volume fields are compared only on days where dk1 has them (``field_days_compared``).
    Relative differences are floor(|a - b| * 10**6 / max(|a|, |b|)) in integer ppm.
    """
    if daily.symbol != bars.symbol:
        raise ValueError(f"symbols differ: daily {daily.symbol}, bars {bars.symbol}")
    if daily.start_ms % DAY_MS:
        raise ValueError(f"{daily.symbol} daily series starts at {daily.start_ms}, not a UTC day start")
    max_ppm = {name: 0 for name in PARITY_FIELDS}
    compared = {name: 0 for name in PARITY_FIELDS}
    days_compared, differing = 0, []
    aligned = _day_aligned(bars)
    built = build_candles(aligned, DAILY_MINUTES) if aligned is not None else None
    for index in range(len(built) if built is not None else 0):
        open_ms = built.open_ms(index)
        day = (open_ms - daily.start_ms) // DAY_MS
        if not built.valid[index] or not 0 <= day < daily.days or not _price_valid(daily, day):
            continue
        days_compared += 1
        different = False
        for name in PARITY_FIELDS:
            reference, value = int(getattr(daily, name)[day]), getattr(built, name)[index]
            if reference == MISSING:
                continue
            compared[name] += 1
            if reference != value:
                different = True
                max_ppm[name] = max(max_ppm[name], _relative_ppm(value, reference))
        if different:
            differing.append(_date(open_ms))
    return {"days_compared": days_compared, "days_with_any_difference": len(differing),
            "field_days_compared": compared, "max_relative_difference_ppm": max_ppm,
            "first_differing_dates": differing[:_MAX_LISTED_DATES]}
