"""Exact funding settlements for the #182 benchmark harness.

Loads the ``funding__SYMBOL__MONTH.csv.gz`` files published by the research data
lake. Rates are exact ``Fraction`` values of the published decimals (some are
published in exponent form, e.g. 9.8E-7). The funding interval is never assumed:
each settlement keeps its own ``interval_hours`` and ``interval_report`` measures
how often the schedule left a row's own interval.

Settlement mark price (documented rule): the mark price at a settlement is the
``mark_open`` of the bar minute that contains the settlement time.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter
import csv
from dataclasses import dataclass
from fractions import Fraction
import re

from .. import data_lake
from .bars import MISSING, BarSeries, _text

_INTEGER = re.compile(r"-?[0-9]+\Z")
_HOUR_MS = 3_600_000


@dataclass(frozen=True)
class FundingSeries:
    """Settlements inside the half-open range [start_ms, end_ms), strictly increasing."""

    calc_time_ms: tuple
    interval_hours: tuple
    rate: tuple
    start_ms: int
    end_ms: int

    def __post_init__(self):
        if not len(self.calc_time_ms) == len(self.interval_hours) == len(self.rate):
            raise ValueError("funding columns have different lengths")
        if self.start_ms >= self.end_ms:
            raise ValueError("funding series range is empty")
        previous = None
        for calc_time in self.calc_time_ms:
            if not self.start_ms <= calc_time < self.end_ms:
                raise ValueError(f"settlement {calc_time} outside [{self.start_ms}, {self.end_ms})")
            if previous is not None and calc_time <= previous:
                raise ValueError(f"settlement times not strictly increasing at {calc_time}")
            previous = calc_time

    @staticmethod
    def concat(parts) -> "FundingSeries":
        """Contiguous months joined into one strictly increasing series."""
        parts = list(parts)
        if not parts:
            raise ValueError("concat needs at least one funding series")
        for previous, part in zip(parts, parts[1:]):
            if part.start_ms != previous.end_ms:
                raise ValueError(f"funding series are not contiguous: {previous.end_ms} then {part.start_ms}")
        return FundingSeries(tuple(t for part in parts for t in part.calc_time_ms),
                             tuple(h for part in parts for h in part.interval_hours),
                             tuple(r for part in parts for r in part.rate),
                             parts[0].start_ms, parts[-1].end_ms)

    def events_between(self, after_ms: int, upto_ms: int) -> list[tuple[int, Fraction, int]]:
        """Settlements with after_ms < calc_time_ms <= upto_ms as (calc_time_ms, rate, interval_hours).

        Both bounds must lie inside the covered range (``upto_ms`` strictly before
        ``end_ms``: a settlement at ``end_ms`` belongs to the next series).
        """
        if not self.start_ms <= after_ms <= upto_ms < self.end_ms:
            raise ValueError(f"[{after_ms}, {upto_ms}] is not inside the covered funding range "
                             f"[{self.start_ms}, {self.end_ms})")
        low = bisect_right(self.calc_time_ms, after_ms)
        high = bisect_right(self.calc_time_ms, upto_ms)
        return [(self.calc_time_ms[i], self.rate[i], self.interval_hours[i]) for i in range(low, high)]

    def index_at_or_after(self, ms: int) -> int:
        """First settlement index with calc_time_ms >= ms (len when none)."""
        return bisect_left(self.calc_time_ms, ms)


def read_funding_csv(fileobj, month: str) -> FundingSeries:
    """One month of data-lake funding rows (gzip binary or text CSV); header-only is allowed."""
    start, end, _ = data_lake.month_bounds_ms(month)
    reader = csv.reader(_text(fileobj))
    header = next(reader, None)
    if header is None or ",".join(header) != ",".join(data_lake.FUNDING_COLUMNS):
        raise ValueError(f"{month} funding: header differs from {','.join(data_lake.FUNDING_COLUMNS)}")
    times, intervals, rates = [], [], []
    line = 0
    for row in reader:
        line += 1
        where = f"{month} funding line {line}"
        if len(row) != len(data_lake.FUNDING_COLUMNS):
            raise ValueError(f"{where}: expected {len(data_lake.FUNDING_COLUMNS)} columns, got {len(row)}")
        calc_text, interval_text, rate_text = row[0], row[1], row[2]
        if not _INTEGER.match(calc_text) or not _INTEGER.match(interval_text):
            raise ValueError(f"{where}: calc_time_ms/funding_interval_hours must be integers")
        calc_time, interval = int(calc_text), int(interval_text)
        if not start <= calc_time < end:
            raise ValueError(f"{where}: calc_time_ms {calc_time} outside {month}")
        if times and calc_time <= times[-1]:
            raise ValueError(f"{where}: calc_time_ms {calc_time} is not after {times[-1]}")
        if interval <= 0:
            raise ValueError(f"{where}: funding_interval_hours must be positive")
        try:
            rate = Fraction(data_lake.exact_decimal(rate_text))
        except ValueError as error:
            raise ValueError(f"{where} column last_funding_rate: {error}") from None
        times.append(calc_time)
        intervals.append(interval)
        rates.append(rate)
    return FundingSeries(tuple(times), tuple(intervals), tuple(rates), start, end)


def settlement_mark(bars: BarSeries, calc_time_ms: int) -> int | None:
    """Mark price at a settlement: the mark_open of the minute containing it (None if absent)."""
    offset = calc_time_ms - bars.start_ms
    if not 0 <= offset < bars.minutes * data_lake.MINUTE_MS:
        return None
    value = bars.mark_open[offset // data_lake.MINUTE_MS]
    return None if value == MISSING else value


def interval_report(series: FundingSeries) -> dict:
    """Settlements per interval_hours value, and consecutive pairs whose spacing (in hours)
    differs from the earlier row's interval_hours. Spacing is compared exactly."""
    counts = Counter(series.interval_hours)
    mismatches = sum(1 for i in range(len(series.calc_time_ms) - 1)
                     if Fraction(series.calc_time_ms[i + 1] - series.calc_time_ms[i], _HOUR_MS)
                     != series.interval_hours[i])
    return {
        "settlements": len(series.calc_time_ms),
        "interval_hours_counts": {str(hours): counts[hours] for hours in sorted(counts)},
        "spacing_mismatches": mismatches,
    }
