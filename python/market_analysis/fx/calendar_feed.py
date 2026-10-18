"""Forex Factory weekly feed -> ``record_fx_calendar`` rows and the fetch's ``covered_to`` (#401 part 2a).

Pure: no network, no clock, no I/O. ``parse_ff_week`` converts one weekly feed payload (a JSON list of
objects with exactly ``title``, ``country``, ``date``, ``impact``, ``forecast``, ``previous``, all
strings) into the rows the operational ``record_fx_calendar`` accepts, and the end of the feed's week
for ``record_fx_calendar_fetch`` (``covered_to``). It is strict: a malformed element raises
``ValueError`` naming its index; nothing is dropped silently.

Skipped, and counted, because they are not blackout events: ``Holiday`` rows, and ``country = All``
rows with ``Low`` impact. A ``Medium``/``High`` ``All`` row raises, so a real market-wide event is
never lost silently (it needs an owner decision on which currencies it affects).

The feed has no event id. ``source_event_key`` is ``currency|title|week_sunday|n``: ``week_sunday`` is
the Sunday starting the event's New York week and ``n`` the 0-based ordinal among events with the same
currency, title and week, by event time then payload position. The key survives a time change within
the week (a reschedule is a new observation of the same event) and differs for weekly recurring
titles. An event moved across a Sunday boundary gets another week and so becomes a new event.

``covered_to`` is the following Sunday 00:00 New York time: the end of the feed's OWN week, so a
horizon that reaches into next week needs next week's feed too (the live fetcher, #401 part 2b).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Tuple
from zoneinfo import ZoneInfo

FF_SOURCE = "forexfactory"
NEW_YORK = ZoneInfo("America/New_York")

_KEYS = frozenset({"title", "country", "date", "impact", "forecast", "previous"})
_IMPACT = {"Low": "low", "Medium": "medium", "High": "high"}
_HOLIDAY = "Holiday"
_CURRENCY = re.compile(r"[A-Z]{3}")
_MAX_TITLE = 300
_MAX_KEY = 200


@dataclass(frozen=True)
class ParsedWeek:
    rows: List[dict]  # exactly the keys record_fx_calendar requires, in payload order
    covered_to_ms: int  # UTC epoch ms of the following Sunday 00:00 New York time
    skipped_holidays: int
    skipped_all_low: int


def _week_sunday(utc: datetime) -> date:
    """The Sunday starting the New York week that contains ``utc``."""
    local = utc.astimezone(NEW_YORK)
    return local.date() - timedelta(days=(local.weekday() + 1) % 7)  # Monday = 0 ... Sunday = 6


def _ms(moment: datetime) -> int:
    return int(moment.timestamp()) * 1000 + moment.microsecond // 1000


def _parse_date(value: str, where: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{where} date is not ISO 8601") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{where} date has no UTC offset")
    return parsed.astimezone(timezone.utc)


def parse_ff_week(payload) -> ParsedWeek:
    if not isinstance(payload, list) or not payload:
        raise ValueError("feed payload must be a non-empty list")
    kept: List[Tuple[int, str, str, str, datetime, Dict[str, str]]] = []
    weeks = set()
    holidays = all_low = 0
    for index, element in enumerate(payload):
        where = f"feed element {index}"
        if not isinstance(element, dict) or set(element) != _KEYS:
            raise ValueError(f"{where} must be an object with exactly {sorted(_KEYS)}")
        if not all(type(value) is str for value in element.values()):
            raise ValueError(f"{where} values must all be strings")
        impact_text, country = element["impact"], element["country"]
        if impact_text != _HOLIDAY and impact_text not in _IMPACT:
            raise ValueError(f"{where} impact {impact_text!r} is not Low, Medium, High or Holiday")
        if country != "All" and not _CURRENCY.fullmatch(country):
            raise ValueError(f"{where} country must be a three-letter currency code or 'All'")
        utc = _parse_date(element["date"], where)
        title = element["title"].strip()
        if not 1 <= len(title) <= _MAX_TITLE:
            raise ValueError(f"{where} title must be 1..{_MAX_TITLE} characters")
        weeks.add(_week_sunday(utc))
        if impact_text == _HOLIDAY:
            holidays += 1
            continue
        if country == "All":
            if impact_text == "Low":
                all_low += 1
                continue
            raise ValueError(f"{where}: country 'All' with impact medium/high needs an owner decision")
        detail = {name: element[name].strip() for name in ("forecast", "previous") if element[name].strip()}
        kept.append((index, country, title, _IMPACT[impact_text], utc, detail))

    if len(weeks) != 1:
        raise ValueError("feed events span more than one New York Sunday-week")
    (sunday,) = weeks
    if not kept:
        raise ValueError("feed has no blackout events after skipping; an empty feed is a failed fetch")

    ordinals: Dict[int, int] = {}
    groups: Dict[Tuple[str, str], List[Tuple[datetime, int]]] = {}
    for index, currency, title, _impact, utc, _detail in kept:
        groups.setdefault((currency, title), []).append((utc, index))
    for members in groups.values():
        for n, (_utc, index) in enumerate(sorted(members)):
            ordinals[index] = n

    rows = []
    for index, currency, title, impact, utc, detail in kept:
        key = f"{currency}|{title}|{sunday.isoformat()}|{ordinals[index]}"
        if len(key) > _MAX_KEY:
            raise ValueError(f"feed element {index} source_event_key exceeds {_MAX_KEY} characters")
        rows.append({
            "source": FF_SOURCE, "source_event_key": key, "event_ts": utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "currency": currency, "impact": impact, "title": title, "detail": detail,
        })
    covered_to = datetime.combine(sunday + timedelta(days=7), time(0), tzinfo=NEW_YORK)
    return ParsedWeek(rows, _ms(covered_to), holidays, all_low)


__all__ = ["FF_SOURCE", "NEW_YORK", "ParsedWeek", "parse_ff_week"]
