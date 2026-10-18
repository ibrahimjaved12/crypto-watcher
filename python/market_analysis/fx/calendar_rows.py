"""Economic calendar rows -> ``CalendarEvent`` and a fail-closed coverage check (#401, part of #374).

Pure: no clock, no I/O. ``calendar_events_from_rows`` maps the elements returned by the operational
database's ``get_fx_calendar_events`` (append-only observations, point in time by ``first_seen_ms``)
onto ``news_blackout.CalendarEvent``. A rescheduled event is a separate event with its own
``event_id`` and ``first_seen_ms``, so ``blackout`` sees the new time only from when it was known.

``calendar_coverage`` fails closed: a blackout built from a feed that has silently stopped would
quietly switch itself off, so a caller must refuse to trade (or to evaluate) unless the latest ok
fetch is recent and covers the needed horizon. Its default maximum age is an UNVERIFIED placeholder.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from ..news_blackout import CalendarEvent, Impact

HOUR_MS = 3_600_000
# UNVERIFIED placeholder: the fetch schedule is chosen with the live fetcher (#401 part 2).
CALENDAR_MAX_AGE_MS = 6 * HOUR_MS

CALENDAR_NEVER_FETCHED = "calendar_never_fetched"
CALENDAR_STALE = "calendar_stale"
CALENDAR_HORIZON_SHORT = "calendar_horizon_short"

_ROW_KEYS = frozenset({"event_id", "ts_ms", "currency", "impact", "title", "first_seen_ms", "source", "tags"})


def calendar_events_from_rows(rows) -> List[CalendarEvent]:
    """Strict: every element must be a complete row; a malformed one raises ``ValueError``, never dropped."""
    if not isinstance(rows, (list, tuple)):
        raise ValueError("calendar rows must be a list")
    events = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != _ROW_KEYS:
            raise ValueError(f"calendar row {index} must be an object with exactly {sorted(_ROW_KEYS)}")
        tags = row["tags"]
        if not isinstance(tags, list) or not all(type(tag) is str for tag in tags):
            raise ValueError(f"calendar row {index} tags must be a list of strings")
        events.append(CalendarEvent(
            event_id=row["event_id"], ts_ms=row["ts_ms"], currency=row["currency"], impact=Impact(row["impact"]),
            title=row["title"], first_seen_ms=row["first_seen_ms"], tags=frozenset(tags), source=row["source"],
        ))
    return events


def _int(name: str, value: object) -> int:
    if type(value) is not int:  # rejects bool and float
        raise ValueError(f"{name} must be an int of UTC epoch milliseconds")
    return value


def calendar_coverage(status: Optional[dict], now_ms: int, needed_to_ms: int,
                      max_age_ms: int = CALENDAR_MAX_AGE_MS) -> Tuple[bool, Optional[str]]:
    """Fail closed. ``status`` is one source's object from ``get_fx_calendar_status`` (or None).

    Reasons, checked in this order: ``calendar_never_fetched`` (no status or no ok fetch),
    ``calendar_stale`` (the latest ok fetch is older than ``max_age_ms``), ``calendar_horizon_short``
    (it does not cover up to ``needed_to_ms``). Otherwise ``(True, None)``. The default
    ``max_age_ms`` (6 h) is UNVERIFIED.
    """
    now_ms, needed_to_ms, max_age_ms = _int("now_ms", now_ms), _int("needed_to_ms", needed_to_ms), \
        _int("max_age_ms", max_age_ms)
    if max_age_ms < 0:
        raise ValueError("max_age_ms must be >= 0")
    if status is not None and not isinstance(status, dict):
        raise ValueError("status must be an object or None")
    if status is None or status.get("last_ok_ms") is None:
        return False, CALENDAR_NEVER_FETCHED
    if now_ms - _int("last_ok_ms", status["last_ok_ms"]) > max_age_ms:
        return False, CALENDAR_STALE
    covered_to = status.get("last_ok_covered_to_ms")
    if covered_to is None or _int("last_ok_covered_to_ms", covered_to) < needed_to_ms:
        return False, CALENDAR_HORIZON_SHORT
    return True, None


__all__ = [
    "CALENDAR_HORIZON_SHORT", "CALENDAR_MAX_AGE_MS", "CALENDAR_NEVER_FETCHED", "CALENDAR_STALE",
    "calendar_coverage", "calendar_events_from_rows",
]
