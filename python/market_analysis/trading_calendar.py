"""Trading calendars (#374): is a market open at an instant, until when, and when is rollover charged.

Pure functions over UTC epoch milliseconds (``int``). Crypto trades 24/7 (``AlwaysOpenCalendar``).
Forex CFDs trade the FX week, Sunday 17:00 to Friday 17:00 ``America/New_York``
(``FxWeekCalendar``); local 17:00 on a Sunday or Friday is never inside a DST transition (those happen
at 02:00), so the week edges are unambiguous. All comparisons are integer milliseconds; local wall
fields are only read to find dates, weekdays and hours.

Per-instrument breaks and holidays are not guessed here. Metals and oil CFDs may have a daily break
(and holiday closures) that must be read from the broker's symbol sessions and passed as
``daily_breaks`` / ``closures``.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from typing import List, Optional, Protocol, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .instruments import Instrument, InstrumentKind, Market

MINUTE_MS = 60_000
DAY_MS = 86_400_000
OPEN_FOREVER_MS = 2 ** 62

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Conventional session hours in each centre's local time (hour granularity). These are the usual
# market-convention hours, not exchange-official sessions.
SESSIONS: Tuple[Tuple[str, str, int, int], ...] = (
    ("sydney", "Australia/Sydney", 7, 16),
    ("tokyo", "Asia/Tokyo", 9, 18),
    ("london", "Europe/London", 8, 17),
    ("new_york", "America/New_York", 8, 17),
)


@lru_cache(maxsize=None)
def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise RuntimeError("timezone database missing: on Windows run 'pip install tzdata'") from None


def _check_ms(name: str, value: object) -> int:
    if type(value) is not int:  # rejects bool, float, str
        raise ValueError(f"{name} must be an int of UTC epoch milliseconds")
    return value


def _to_ms(aware: datetime) -> int:
    return (aware.astimezone(timezone.utc) - EPOCH) // timedelta(milliseconds=1)


def _local(ms: int, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(ms // 1000, tz)


def _at(day: date, local: Tuple[int, int], tz: ZoneInfo) -> int:
    return _to_ms(datetime.combine(day, time(*local), tzinfo=tz))


def _check_local(name: str, value: Tuple[int, int]) -> None:
    if (not isinstance(value, tuple) or len(value) != 2 or not all(type(v) is int for v in value)
            or not 0 <= value[0] <= 23 or not 0 <= value[1] <= 59):
        raise ValueError(f"{name} must be a local (hour, minute) tuple")


def _check_range(start_ms: object, end_ms: object, aligned: bool = False) -> Tuple[int, int]:
    start, end = _check_ms("start_ms", start_ms), _check_ms("end_ms", end_ms)
    if start > end:
        raise ValueError("start_ms must not be after end_ms")
    if aligned and (start % MINUTE_MS or end % MINUTE_MS):
        raise ValueError("start_ms and end_ms must be minute-aligned")
    return start, end


class TradingCalendar(Protocol):
    calendar_id: str

    def is_open(self, ms: int) -> bool: ...

    def next_open_ms(self, ms: int) -> int: ...

    def open_until_ms(self, ms: int) -> Optional[int]: ...

    def open_minutes(self, start_ms: int, end_ms: int) -> int: ...

    def rollover_events(self, start_ms: int, end_ms: int) -> List[Tuple[int, int]]: ...

    def session_tags(self, ms: int) -> Tuple[str, ...]: ...


@dataclass(frozen=True)
class AlwaysOpenCalendar:
    """24/7 market (crypto perpetuals): no weekend, no rollover (funding is a separate cost)."""

    calendar_id: str = "always-open-v1"

    def is_open(self, ms: int) -> bool:
        _check_ms("ms", ms)
        return True

    def next_open_ms(self, ms: int) -> int:
        return _check_ms("ms", ms)

    def open_until_ms(self, ms: int) -> Optional[int]:
        _check_ms("ms", ms)
        return OPEN_FOREVER_MS

    def open_minutes(self, start_ms: int, end_ms: int) -> int:
        start, end = _check_range(start_ms, end_ms, aligned=True)
        return (end - start) // MINUTE_MS

    def rollover_events(self, start_ms: int, end_ms: int) -> List[Tuple[int, int]]:
        _check_range(start_ms, end_ms)
        return []

    def session_tags(self, ms: int) -> Tuple[str, ...]:
        _check_ms("ms", ms)
        return ()


@dataclass(frozen=True)
class LocalBreak:
    """A daily break ``[start, end)`` in the calendar's local time, applied on every local date."""

    start: Tuple[int, int]
    end: Tuple[int, int]

    def __post_init__(self) -> None:
        _check_local("start", self.start)
        _check_local("end", self.end)
        if not self.start < self.end:
            raise ValueError("break start must be before its end on the same local date")


@dataclass(frozen=True)
class FxWeekCalendar:
    """The FX week; the defaults are Sunday 17:00 to Friday 17:00 New York, triple swap on Wednesday.

    ``closures`` are explicit UTC ``[start_ms, end_ms)`` full closures (sorted, non-overlapping).
    ``daily_breaks`` come from the broker's symbol sessions; none are assumed.
    """

    calendar_id: str = "fx-week-ny17-v1"
    tz_name: str = "America/New_York"
    open_weekday: int = 6
    open_local: Tuple[int, int] = (17, 0)
    close_weekday: int = 4
    close_local: Tuple[int, int] = (17, 0)
    daily_breaks: Tuple[LocalBreak, ...] = ()
    closures: Tuple[Tuple[int, int], ...] = ()
    triple_swap_weekday: int = 2
    rollover_local: Tuple[int, int] = (17, 0)

    def __post_init__(self) -> None:
        if not isinstance(self.calendar_id, str) or not self.calendar_id:
            raise ValueError("calendar_id must be a non-empty string")
        for name in ("open_weekday", "close_weekday", "triple_swap_weekday"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= 6:
                raise ValueError(f"{name} must be an int weekday 0..6 (Monday = 0)")
        for name in ("open_local", "close_local", "rollover_local"):
            _check_local(name, getattr(self, name))
        if not all(isinstance(item, LocalBreak) for item in self.daily_breaks):
            raise ValueError("daily_breaks must be LocalBreak values")
        previous_end = None
        for closure in self.closures:
            if (not isinstance(closure, tuple) or len(closure) != 2
                    or not all(type(v) is int for v in closure) or not closure[0] < closure[1]):
                raise ValueError("each closure must be an int (start_ms, end_ms) with start < end")
            if previous_end is not None and closure[0] < previous_end:
                raise ValueError("closures must be sorted and non-overlapping")
            previous_end = closure[1]

    @property
    def _tz(self) -> ZoneInfo:
        return _zone(self.tz_name)

    @property
    def _span_days(self) -> int:
        return (self.close_weekday - self.open_weekday) % 7 or 7

    def _window_from(self, start: date) -> Tuple[int, int]:
        return (_at(start, self.open_local, self._tz),
                _at(start + timedelta(days=self._span_days), self.close_local, self._tz))

    def week_window(self, ms: int) -> Tuple[int, int]:
        """The ``[open_ms, close_ms)`` week containing ``ms``, or the next one if ``ms`` is in the gap."""
        _check_ms("ms", ms)
        local = _local(ms, self._tz)
        start = local.date() - timedelta(days=(local.weekday() - self.open_weekday) % 7)
        open_ms, close_ms = self._window_from(start)
        if open_ms > ms:
            start -= timedelta(days=7)
            open_ms, close_ms = self._window_from(start)
        if ms >= close_ms:
            open_ms, close_ms = self._window_from(start + timedelta(days=7))
        return open_ms, close_ms

    def _break_at(self, ms: int) -> Optional[Tuple[int, int]]:
        tz = self._tz
        for day in {_local(ms + offset, tz).date() for offset in (-DAY_MS, 0, DAY_MS)}:
            for item in self.daily_breaks:
                start, end = _at(day, item.start, tz), _at(day, item.end, tz)
                if start <= ms < end:
                    return start, end
        return None

    def _next_break_start(self, ms: int) -> Optional[int]:
        if not self.daily_breaks:
            return None
        tz = self._tz
        today = _local(ms, tz).date()
        starts = [_at(today + timedelta(days=offset), item.start, tz)
                  for offset in (0, 1, 2) for item in self.daily_breaks]
        later = [start for start in starts if start > ms]
        return min(later) if later else None

    def _closure_at(self, ms: int) -> Optional[Tuple[int, int]]:
        index = bisect_right([start for start, _ in self.closures], ms) - 1
        if index >= 0 and ms < self.closures[index][1]:
            return self.closures[index]
        return None

    def _next_closure_start(self, ms: int) -> Optional[int]:
        index = bisect_right([start for start, _ in self.closures], ms)
        return self.closures[index][0] if index < len(self.closures) else None

    def is_open(self, ms: int) -> bool:
        open_ms, close_ms = self.week_window(ms)
        return (open_ms <= ms < close_ms and self._break_at(ms) is None
                and self._closure_at(ms) is None)

    def next_open_ms(self, ms: int) -> int:
        t = _check_ms("ms", ms)
        for _ in range(64):
            if self.is_open(t):
                return t
            open_ms, _close = self.week_window(t)
            if t < open_ms:
                t = open_ms
                continue
            pause = self._break_at(t) or self._closure_at(t)
            if pause is None or pause[1] <= t:
                raise RuntimeError(f"calendar {self.calendar_id} made no progress at {t}")
            t = pause[1]
        raise RuntimeError(f"calendar {self.calendar_id} found no open instant after {ms}")

    def open_until_ms(self, ms: int) -> Optional[int]:
        if not self.is_open(ms):
            return None
        ends = [self.week_window(ms)[1], self._next_break_start(ms), self._next_closure_start(ms)]
        return min(end for end in ends if end is not None)

    def open_minutes(self, start_ms: int, end_ms: int) -> int:
        """Minute-aligned instants in ``[start_ms, end_ms)`` at which the market is open.

        O(number of open runs). Runs may have sub-minute edges (closures take any ms), so each run
        ``[t, e)`` counts the aligned instants inside it: ``ceil(e / MINUTE) - ceil(t / MINUTE)``.
        """
        start, end = _check_range(start_ms, end_ms, aligned=True)
        total, t = 0, start
        while t < end:
            t = self.next_open_ms(t)
            if t >= end:
                break
            run_end = min(self.open_until_ms(t), end)
            total += -(-run_end // MINUTE_MS) - -(-t // MINUTE_MS)
            t = run_end
        return total

    def rollover_events(self, start_ms: int, end_ms: int) -> List[Tuple[int, int]]:
        """``(instant_ms, multiplier)`` for each Mon..Fri local rollover ``r`` with ``start < r <= end``.

        A position entered exactly at a rollover instant is after it; one exiting exactly at it is
        charged. Sunday never rolls over; ``triple_swap_weekday`` is charged three days.
        """
        start, end = _check_range(start_ms, end_ms)
        tz = self._tz
        day, last = _local(start, tz).date(), _local(end, tz).date()
        events = []
        while day <= last:
            if day.weekday() <= 4:
                instant = _at(day, self.rollover_local, tz)
                if start < instant <= end:
                    events.append((instant, 3 if day.weekday() == self.triple_swap_weekday else 1))
            day += timedelta(days=1)
        return events

    def session_tags(self, ms: int) -> Tuple[str, ...]:
        """Conventional sessions active at ``ms``: a local Monday..Friday within the centre's hours.

        ``()`` while the market is closed. Each centre's own weekday matters: Friday 20:00Z is
        Saturday morning in Sydney, so Sydney is not tagged then.
        """
        if not self.is_open(ms):
            return ()
        tags = []
        for name, tz_name, start_hour, end_hour in SESSIONS:
            local = _local(ms, _zone(tz_name))
            if local.weekday() <= 4 and start_hour <= local.hour < end_hour:
                tags.append(name)
        return tuple(tags)


ALWAYS_OPEN = AlwaysOpenCalendar()
FX_WEEK = FxWeekCalendar()


def calendar_for(instrument: Instrument) -> TradingCalendar:
    """Crypto is always open; forex CFDs use the FX week; futures fail closed until their sessions are known."""
    if instrument.market is Market.CRYPTO:
        return ALWAYS_OPEN
    if instrument.kind is InstrumentKind.SPOT_CFD:
        return FX_WEEK
    raise ValueError("no trading calendar defined for futures yet")


__all__ = [
    "ALWAYS_OPEN",
    "AlwaysOpenCalendar",
    "DAY_MS",
    "FX_WEEK",
    "FxWeekCalendar",
    "LocalBreak",
    "MINUTE_MS",
    "OPEN_FOREVER_MS",
    "SESSIONS",
    "TradingCalendar",
    "calendar_for",
]
