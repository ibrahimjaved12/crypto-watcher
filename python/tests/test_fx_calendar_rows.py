"""Economic calendar rows (#401): strict mapping to CalendarEvent, fail-closed coverage, reschedules.

Every event here is SYNTHETIC test data shaped like get_fx_calendar_events output, not a real schedule.
"""

from datetime import datetime, timezone
import unittest

from market_analysis.fx.calendar_rows import (
    CALENDAR_HORIZON_SHORT, CALENDAR_MAX_AGE_MS, CALENDAR_NEVER_FETCHED, CALENDAR_STALE, calendar_coverage,
    calendar_events_from_rows,
)
from market_analysis.instruments import forex_instrument
from market_analysis.news_blackout import CalendarEvent, EventIndex, Impact, blackout

MINUTE = 60_000
HOUR = 60 * MINUTE
DAY = 24 * HOUR


def utc(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()) * 1000


NFP = utc("2026-11-06T13:30:00")
EURUSD = forex_instrument("EURUSD")


def row(ts_ms, first_seen_ms, *, key="nfp-2026-11", impact="high", currency="USD", title="Non-Farm Payrolls",
        source="test-feed", event_id=None):
    return {"event_id": event_id or f"{source}:{key}:{ts_ms}", "ts_ms": ts_ms, "currency": currency,
            "impact": impact, "title": title, "first_seen_ms": first_seen_ms, "source": source, "tags": []}


class MappingTests(unittest.TestCase):
    def test_rows_become_calendar_events_the_index_accepts(self):
        rows = [row(NFP, NFP - 7 * DAY), row(NFP + DAY, NFP - 6 * DAY, key="pmi", currency="EUR", impact="medium",
                                            title="PMI")]
        events = calendar_events_from_rows(rows)
        self.assertEqual([type(event) for event in events], [CalendarEvent, CalendarEvent])
        first = events[0]
        self.assertEqual((first.event_id, first.ts_ms, first.currency, first.impact, first.title, first.first_seen_ms,
                          first.source, first.tags),
                         (f"test-feed:nfp-2026-11:{NFP}", NFP, "USD", Impact.HIGH, "Non-Farm Payrolls",
                          NFP - 7 * DAY, "test-feed", frozenset()))
        self.assertIs(events[1].impact, Impact.MEDIUM)
        index = EventIndex(events)
        self.assertEqual(len(index), 2)
        self.assertTrue(blackout(EURUSD, NFP, index).active)
        self.assertEqual(calendar_events_from_rows([]), [])
        self.assertEqual(calendar_events_from_rows([{**row(NFP, NFP), "tags": ["oil"]}])[0].tags, frozenset({"oil"}))

    def test_a_malformed_element_raises_and_nothing_is_dropped(self):
        good = row(NFP, NFP - DAY)
        bad = [
            "not an object",
            {key: value for key, value in good.items() if key != "first_seen_ms"},
            {**good, "extra": 1},
            {**good, "impact": "critical"},
            {**good, "currency": "usd"},
            {**good, "ts_ms": float(NFP)},
            {**good, "first_seen_ms": None},
            {**good, "event_id": ""},
            {**good, "tags": "oil"},
            {**good, "tags": [["oil"]]},
            {**good, "title": None},
        ]
        for element in bad:
            with self.subTest(element=element), self.assertRaises(ValueError):
                calendar_events_from_rows([good, element])
        with self.assertRaises(ValueError):
            calendar_events_from_rows({"rows": [good]})


class CoverageTests(unittest.TestCase):
    NOW = NFP - 2 * DAY

    def status(self, **overrides):
        base = {"source": "test-feed", "last_ok_ms": self.NOW - HOUR, "last_ok_covered_to_ms": self.NOW + 5 * DAY,
                "last_error_ms": None, "last_error": None}
        return {**base, **overrides}

    def test_each_reason_in_order_and_the_ok_case(self):
        needed = self.NOW + 3 * DAY
        self.assertEqual(calendar_coverage(None, self.NOW, needed), (False, CALENDAR_NEVER_FETCHED))
        self.assertEqual(calendar_coverage(self.status(last_ok_ms=None, last_ok_covered_to_ms=None,
                                                       last_error_ms=self.NOW, last_error="HTTP 503"),
                                           self.NOW, needed), (False, CALENDAR_NEVER_FETCHED))
        stale = self.status(last_ok_ms=self.NOW - CALENDAR_MAX_AGE_MS - 1)
        self.assertEqual(calendar_coverage(stale, self.NOW, needed), (False, CALENDAR_STALE))
        # Stale wins over a short horizon (order of the reasons).
        self.assertEqual(calendar_coverage({**stale, "last_ok_covered_to_ms": self.NOW}, self.NOW, needed),
                         (False, CALENDAR_STALE))
        self.assertEqual(calendar_coverage(self.status(last_ok_ms=self.NOW - CALENDAR_MAX_AGE_MS), self.NOW, needed),
                         (True, None), "exactly max age is still fresh")
        self.assertEqual(calendar_coverage(self.status(last_ok_covered_to_ms=needed - 1), self.NOW, needed),
                         (False, CALENDAR_HORIZON_SHORT))
        self.assertEqual(calendar_coverage(self.status(last_ok_covered_to_ms=needed), self.NOW, needed), (True, None))
        self.assertEqual(calendar_coverage(self.status(), self.NOW, needed), (True, None))
        # A later error does not hide a fresh ok fetch; the reasons only look at the ok fetch.
        self.assertEqual(calendar_coverage(self.status(last_error_ms=self.NOW, last_error="timeout"), self.NOW, needed),
                         (True, None))
        self.assertEqual(calendar_coverage(self.status(), self.NOW, needed, max_age_ms=30 * MINUTE),
                         (False, CALENDAR_STALE))

    def test_malformed_inputs_raise(self):
        with self.assertRaises(ValueError):
            calendar_coverage(self.status(), float(self.NOW), self.NOW)
        with self.assertRaises(ValueError):
            calendar_coverage(self.status(last_ok_ms="yesterday"), self.NOW, self.NOW)
        with self.assertRaises(ValueError):
            calendar_coverage(["not", "a", "status"], self.NOW, self.NOW)
        with self.assertRaises(ValueError):
            calendar_coverage(self.status(), self.NOW, self.NOW, max_age_ms=-1)


class RescheduleTests(unittest.TestCase):
    def test_the_new_time_counts_only_from_when_it_was_first_seen(self):
        original_seen = NFP - 14 * DAY
        moved = NFP + DAY
        # The reschedule is discovered late, 10 minutes before the new time (inside its ±30 min window).
        moved_seen = moved - 10 * MINUTE
        events = calendar_events_from_rows([row(NFP, original_seen), row(moved, moved_seen)])
        self.assertNotEqual(events[0].event_id, events[1].event_id)
        index = EventIndex(events)
        before_discovery = moved - 20 * MINUTE
        self.assertFalse(blackout(EURUSD, before_discovery, index).active)
        self.assertFalse(blackout(EURUSD, moved_seen - 1, index).active)
        decision = blackout(EURUSD, moved_seen, index)
        self.assertTrue(decision.active)
        self.assertEqual([event.event_id for event in decision.events], [f"test-feed:nfp-2026-11:{moved}"])
        # The superseded slot is not retracted: the old time stays blacked out (conservative).
        self.assertTrue(blackout(EURUSD, NFP, index).active)


if __name__ == "__main__":
    unittest.main()
