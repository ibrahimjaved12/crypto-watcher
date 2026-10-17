"""News blackout filter (#374), level 1: scheduled timing only.

Pure decision logic: no clock, no I/O, no database. An entry is inside a blackout when a scheduled
event that affects the instrument, at or above ``rule.min_impact``, puts the decision time inside
``[event - before, event + after]`` (inclusive). Point in time: an event counts only if it was
already known at the decision time (``first_seen_ms <= ts_ms``), so a backtest never uses a schedule
published later. An event may be first seen after its scheduled time (late ingestion); it then
affects only decisions from its discovery to the end of its window.

Modes: ``off`` (no effect), ``tag`` (keep and mark), ``suppress`` (drop new entries and keep them in
a "would have fired" list with the deciding events). The window lengths are UNVERIFIED placeholders.
Crypto instruments are never affected (no blackout is invented for crypto). Interpreting surprises
or reactions is later research.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Tuple

from .instruments import AssetClass, Instrument, Market


class BlackoutMode(str, Enum):
    OFF = "off"
    TAG = "tag"
    SUPPRESS = "suppress"


class Impact(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


_RANK = {Impact.LOW: 0, Impact.MEDIUM: 1, Impact.HIGH: 2}
MINUTE_MS = 60_000

# Dollar-index basket currencies. The index weights are not modelled: any of them triggers.
DXY_CURRENCIES = frozenset({"USD", "EUR", "JPY", "GBP", "CAD", "SEK", "CHF"})


def _int(name: str, value: object) -> int:
    if type(value) is not int:  # rejects bool and float
        raise ValueError(f"{name} must be an int of UTC epoch milliseconds")
    return value


def _currency(value: object) -> str:
    if type(value) is not str or len(value) != 3 or not value.isalpha() or not value.isupper():
        raise ValueError(f"currency must be a three-letter upper-case ISO 4217 code, got {value!r}")
    return value


@dataclass(frozen=True)
class CalendarEvent:
    event_id: str
    ts_ms: int
    currency: str
    impact: Impact
    title: str
    first_seen_ms: int
    tags: FrozenSet[str] = frozenset()
    source: str = ""

    def __post_init__(self):
        if type(self.event_id) is not str or not self.event_id:
            raise ValueError("event_id must be a non-empty string")
        _int("ts_ms", self.ts_ms)
        _int("first_seen_ms", self.first_seen_ms)
        _currency(self.currency)
        object.__setattr__(self, "impact", Impact(self.impact))
        if type(self.title) is not str or type(self.source) is not str:
            raise ValueError("title and source must be strings")
        tags = frozenset(self.tags)
        if not all(type(tag) is str and tag for tag in tags):
            raise ValueError("tags must be non-empty strings")
        object.__setattr__(self, "tags", tags)


@dataclass(frozen=True)
class BlackoutRule:
    """Window lengths per impact (ms before / after the event). Defaults are UNVERIFIED placeholders."""

    before_ms: Dict[Impact, int]
    after_ms: Dict[Impact, int]
    min_impact: Impact = Impact.HIGH

    def __post_init__(self):
        for name in ("before_ms", "after_ms"):
            windows = getattr(self, name)
            if not isinstance(windows, dict):
                raise ValueError(f"{name} must be a dict of Impact -> ms")
            normalized = {Impact(impact): value for impact, value in windows.items()}
            if set(normalized) != set(Impact):
                raise ValueError(f"{name} must map every Impact to a window")
            for impact, value in normalized.items():
                if type(value) is not int or value < 0:
                    raise ValueError(f"{name}[{impact.value}] must be a non-negative int of ms")
            object.__setattr__(self, name, normalized)
        object.__setattr__(self, "min_impact", Impact(self.min_impact))


DEFAULT_RULE = BlackoutRule(
    before_ms={Impact.HIGH: 30 * MINUTE_MS, Impact.MEDIUM: 15 * MINUTE_MS, Impact.LOW: 0},
    after_ms={Impact.HIGH: 30 * MINUTE_MS, Impact.MEDIUM: 15 * MINUTE_MS, Impact.LOW: 0},
)


def affected_currencies(instrument: Instrument) -> FrozenSet[str]:
    """Currencies whose scheduled events can move the instrument; empty for crypto."""
    if instrument.market is Market.CRYPTO:
        return frozenset()
    if instrument.asset_class is AssetClass.FX:
        return frozenset({instrument.base, instrument.quote})
    if instrument.asset_class is AssetClass.INDEX and instrument.base == "DXY":
        return DXY_CURRENCIES
    return frozenset({instrument.quote})  # metals, energy, other indices: the quote currency


def instrument_tags(instrument: Instrument) -> FrozenSet[str]:
    """Event tags that affect the instrument regardless of currency."""
    if instrument.market is Market.CRYPTO:
        return frozenset()
    if instrument.base.startswith("XAU"):
        return frozenset({"gold"})
    if instrument.asset_class is AssetClass.ENERGY:
        return frozenset({"oil"})
    return frozenset()


def event_affects(event: CalendarEvent, instrument: Instrument) -> bool:
    return event.currency in affected_currencies(instrument) or bool(event.tags & instrument_tags(instrument))


class EventIndex:
    """Events sorted by ``(ts_ms, event_id)`` with a parallel time list for ``bisect``."""

    def __init__(self, events: Iterable[CalendarEvent]):
        items = list(events)
        if not all(isinstance(event, CalendarEvent) for event in items):
            raise ValueError("EventIndex takes CalendarEvent values")
        ids = [event.event_id for event in items]
        if len(set(ids)) != len(ids):
            raise ValueError("event ids must be unique")
        items.sort(key=lambda event: (event.ts_ms, event.event_id))
        self._events: Tuple[CalendarEvent, ...] = tuple(items)
        self._ts: List[int] = [event.ts_ms for event in items]

    def __len__(self) -> int:
        return len(self._events)

    def window_candidates(self, lo_ms: int, hi_ms: int) -> Tuple[CalendarEvent, ...]:
        """Events with ``lo_ms <= ts_ms <= hi_ms``, in O(log n + k)."""
        return self._events[bisect_left(self._ts, _int("lo_ms", lo_ms)):bisect_right(self._ts, _int("hi_ms", hi_ms))]


@dataclass(frozen=True)
class BlackoutDecision:
    active: bool
    events: Tuple[CalendarEvent, ...] = ()
    window_start_ms: Optional[int] = None
    window_end_ms: Optional[int] = None


INACTIVE = BlackoutDecision(False)


def blackout(instrument: Instrument, ts_ms: int, index: EventIndex, rule: BlackoutRule = DEFAULT_RULE
             ) -> BlackoutDecision:
    """Pure: is ``ts_ms`` inside a blackout window for ``instrument``, using only events known by then."""
    ts_ms = _int("ts_ms", ts_ms)
    if not affected_currencies(instrument) and not instrument_tags(instrument):
        return INACTIVE
    # an event at E covers ts when E - before <= ts <= E + after, i.e. E in [ts - after, ts + before]
    candidates = index.window_candidates(ts_ms - max(rule.after_ms.values()), ts_ms + max(rule.before_ms.values()))
    hits = [event for event in candidates
            if event.first_seen_ms <= ts_ms
            and _RANK[event.impact] >= _RANK[rule.min_impact]
            and event.ts_ms - rule.before_ms[event.impact] <= ts_ms <= event.ts_ms + rule.after_ms[event.impact]
            and event_affects(event, instrument)]
    if not hits:
        return INACTIVE
    return BlackoutDecision(True, tuple(hits),
                            min(event.ts_ms - rule.before_ms[event.impact] for event in hits),
                            max(event.ts_ms + rule.after_ms[event.impact] for event in hits))


@dataclass(frozen=True)
class BlackoutResult:
    kept: List[Any] = field(default_factory=list)
    tagged: List[Tuple[Any, BlackoutDecision]] = field(default_factory=list)
    suppressed: List[Tuple[Any, BlackoutDecision]] = field(default_factory=list)


def apply_blackout(items: Iterable[Any], instrument: Instrument, index: EventIndex, mode: BlackoutMode,
                   rule: BlackoutRule = DEFAULT_RULE,
                   ts_of: Callable[[Any], int] = lambda item: item.signal_ms) -> BlackoutResult:
    """Apply a mode to new entries. ``kept`` keeps the input order; ``suppressed`` is the
    "would have fired" log, each item with the decision naming its events."""
    mode = BlackoutMode(mode)
    items = list(items)
    if mode is BlackoutMode.OFF:
        return BlackoutResult(kept=items)
    result = BlackoutResult()
    for item in items:
        decision = blackout(instrument, ts_of(item), index, rule)
        if not decision.active:
            result.kept.append(item)
        elif mode is BlackoutMode.TAG:
            result.kept.append(item)
            result.tagged.append((item, decision))
        else:
            result.suppressed.append((item, decision))
    return result


__all__ = [
    "BlackoutDecision", "BlackoutMode", "BlackoutResult", "BlackoutRule", "CalendarEvent", "DEFAULT_RULE",
    "DXY_CURRENCIES", "EventIndex", "Impact", "affected_currencies", "apply_blackout", "blackout",
    "event_affects", "instrument_tags",
]
