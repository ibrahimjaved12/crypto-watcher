"""Chronological splits and time-based purge/embargo primitives for #182."""
from __future__ import annotations

from ..data_lake import month_bounds_ms, months_between

SEGMENTS = {
    "development": ("2024-01", "2025-07"),
    "validation": ("2025-07", "2026-01"),
    "hidden": ("2026-01", "2026-10"),
}

# Daily-frequency research only (owner decision D2, 2026-10-09; #222 P9). Kept OUT of SEGMENTS on
# purpose: every intraday path (labels, experiments, calibration, level study, screen) looks segments
# up in SEGMENTS and therefore refuses it. 2020 is the indicator warm-up year, never evaluated.
DAILY_SEGMENTS = {
    "development-ext": ("2021-01", "2025-07"),
}


def daily_segment_bounds_ms(name: str) -> tuple[int, int]:
    """Bounds of a daily-research segment: the intraday segments plus DAILY_SEGMENTS."""
    first, end = {**SEGMENTS, **DAILY_SEGMENTS}[name]
    return month_bounds_ms(first)[0], month_bounds_ms(end)[0]


def daily_segment_months(name: str) -> list[str]:
    first, end = {**SEGMENTS, **DAILY_SEGMENTS}[name]
    return months_between(first, end)[:-1]


def _integer(value: int) -> None:
    if type(value) is not int:
        raise TypeError("time values must be integers")


def _range(start: int, end: int) -> None:
    _integer(start)
    _integer(end)
    if end < start:
        raise ValueError("range end precedes start")


def segment_bounds_ms(name: str) -> tuple[int, int]:
    first, end = SEGMENTS[name]
    return month_bounds_ms(first)[0], month_bounds_ms(end)[0]


def segment_of(ms: int) -> str | None:
    _integer(ms)
    for name in SEGMENTS:
        first, end = segment_bounds_ms(name)
        if first <= ms < end:
            return name
    return None


def segment_months(name: str) -> list[str]:
    first, end = SEGMENTS[name]
    return months_between(first, end)[:-1]


def worst_case_window_end_ms(signal_ms: int, horizon_min: int, time_limit_multiple: int = 4) -> int:
    """Outcome-independent end; never substitute a realized exit time."""
    for value in (signal_ms, horizon_min, time_limit_multiple):
        _integer(value)
    if horizon_min < 0 or time_limit_multiple < 0:
        raise ValueError("horizon and time limit multiple must be non-negative")
    return signal_ms + horizon_min * time_limit_multiple * 60_000


def eligible(segment: str, signal_ms: int, window_end_ms: int) -> bool:
    """The closed label window must fit inside the half-open segment."""
    _range(signal_ms, window_end_ms)
    first, end = segment_bounds_ms(segment)
    return first <= signal_ms <= window_end_ms < end


def excluded_interval(test_start_ms: int, test_end_ms: int, span_ms: int,
                      embargo_ms: int) -> tuple[int, int]:
    """Half-open interval of excluded training signal times."""
    _range(test_start_ms, test_end_ms)
    for value in (span_ms, embargo_ms):
        _integer(value)
        if value < 0:
            raise ValueError("span and embargo must be non-negative")
    return test_start_ms - span_ms, test_end_ms + embargo_ms


def allowed_ranges(domain_start: int, domain_end: int, test_ranges,
                   span_ms: int, embargo_ms: int) -> list[tuple[int, int]]:
    """Complement of the union of expanded test ranges, clipped to the domain.

    Empty test ranges exclude nothing. Adjacent exclusions are merged; returned
    training ranges are sorted, maximal and non-empty. All units are milliseconds.
    """
    _range(domain_start, domain_end)
    excluded_interval(domain_start, domain_end, span_ms, embargo_ms)
    excluded = []
    for first, end in test_ranges:
        left, right = excluded_interval(first, end, span_ms, embargo_ms)
        if first != end:
            left, right = max(domain_start, left), min(domain_end, right)
            if left < right:
                excluded.append((left, right))
    result = []
    cursor = domain_start
    for left, right in sorted(excluded):
        if cursor < left:
            result.append((cursor, left))
        cursor = max(cursor, right)
    if cursor < domain_end:
        result.append((cursor, domain_end))
    return result
