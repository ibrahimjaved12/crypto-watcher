"""Forex Factory weekly feed parser (#401 part 2a): strict mapping, skips, keys, weeks, covered_to.

Every event here is SYNTHETIC and hand-written in the feed's shape; no third-party data is copied.
"""

from datetime import datetime, timezone
import copy
import unittest

from market_analysis.fx.calendar_feed import FF_SOURCE, ParsedWeek, parse_ff_week
from market_analysis.fx.calendar_rows import calendar_events_from_rows
from market_analysis.news_blackout import EventIndex, Impact

HOUR = 3_600_000
ROW_KEYS = {"source", "source_event_key", "event_ts", "currency", "impact", "title", "detail"}


def utc_ms(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()) * 1000


def ev(title, country, date, impact, forecast="", previous=""):
    return {"title": title, "country": country, "date": date, "impact": impact, "forecast": forecast,
            "previous": previous}


# New York week Sunday 2026-10-18 .. Saturday 2026-10-24 (EDT, -04:00).
WEEK = [
    ev(" CPI y/y ", "GBP", "2026-10-21T02:00:00-04:00", "High", "3.1%", "3.0%"),
    ev("Unemployment Claims", "USD", "2026-10-22T08:30:00-04:00", "Medium", "", "225K|227K"),
    ev("Bank Holiday", "JPY", "2026-10-19T00:00:00-04:00", "Holiday"),
    ev("G20 Meetings", "All", "2026-10-20T00:00:00-04:00", "Low"),
    ev("ECB Press Conference", "EUR", "2026-10-22T08:45:00-04:00", "High", " ", " "),
    ev("FOMC Member Speaks", "USD", "2026-10-23T10:00:00-04:00", "Low"),
    ev("FOMC Member Speaks", "USD", "2026-10-20T13:00:00-04:00", "Low"),
    ev("Trade Balance", "NZD", "2026-10-18T17:45:00-04:00", "Medium", "  -0.9B ", ""),
]
SUNDAY = "2026-10-18"


def parse(events=None):
    return parse_ff_week(copy.deepcopy(WEEK if events is None else events))


class MappingTests(unittest.TestCase):
    def test_rows_impacts_skips_and_payload_order(self):
        week = parse()
        self.assertIsInstance(week, ParsedWeek)
        self.assertEqual((week.skipped_holidays, week.skipped_all_low), (1, 1))
        self.assertEqual([row["title"] for row in week.rows], ["CPI y/y", "Unemployment Claims", "ECB Press Conference",
                                                               "FOMC Member Speaks", "FOMC Member Speaks", "Trade Balance"])
        self.assertEqual([row["impact"] for row in week.rows], ["high", "medium", "high", "low", "low", "medium"])
        for row in week.rows:
            self.assertEqual(set(row), ROW_KEYS)
            self.assertEqual(row["source"], FF_SOURCE)
            self.assertLessEqual(len(row["source_event_key"]), 200)
        self.assertEqual(week.rows[0], {
            "source": "forexfactory", "source_event_key": f"GBP|CPI y/y|{SUNDAY}|0", "event_ts": "2026-10-21T06:00:00Z",
            "currency": "GBP", "impact": "high", "title": "CPI y/y", "detail": {"forecast": "3.1%", "previous": "3.0%"},
        })

    def test_detail_keeps_pipes_and_omits_empty_values(self):
        rows = parse().rows
        self.assertEqual(rows[1]["detail"], {"previous": "225K|227K"})
        self.assertEqual(rows[2]["detail"], {})
        self.assertEqual(rows[5]["detail"], {"forecast": "-0.9B"})

    def test_offsets_become_utc_z(self):
        self.assertEqual(parse().rows[1]["event_ts"], "2026-10-22T12:30:00Z")
        # After the US clock change on 2026-11-01 New York is -05:00.
        after = parse([ev("ISM Manufacturing PMI", "USD", "2026-11-02T10:00:00-05:00", "High")])
        self.assertEqual(after.rows[0]["event_ts"], "2026-11-02T15:00:00Z")
        self.assertEqual(after.rows[0]["source_event_key"], "USD|ISM Manufacturing PMI|2026-11-01|0")
        # The same instant written with another offset is the same event time.
        same = parse([ev("ISM Manufacturing PMI", "USD", "2026-11-02T15:00:00+00:00", "High")])
        self.assertEqual(same.rows[0], after.rows[0])


class KeyTests(unittest.TestCase):
    def test_key_is_stable_when_the_time_moves_within_the_week(self):
        moved = copy.deepcopy(WEEK)
        moved[4]["date"] = "2026-10-23T09:15:00-04:00"
        before, after = parse().rows[2], parse(moved).rows[2]
        self.assertEqual(before["source_event_key"], after["source_event_key"])
        self.assertEqual(before["source_event_key"], f"EUR|ECB Press Conference|{SUNDAY}|0")
        self.assertNotEqual(before["event_ts"], after["event_ts"])

    def test_the_same_title_next_week_is_another_event(self):
        next_week = parse([ev("Unemployment Claims", "USD", "2026-10-29T08:30:00-04:00", "Medium")])
        self.assertEqual(next_week.rows[0]["source_event_key"], "USD|Unemployment Claims|2026-10-25|0")
        self.assertNotEqual(next_week.rows[0]["source_event_key"], parse().rows[1]["source_event_key"])

    def test_same_title_and_currency_in_one_week_get_ordinals_by_time(self):
        rows = parse().rows
        # Payload order: Friday's speech first, Tuesday's second; ordinals follow the event time.
        self.assertEqual(rows[3]["source_event_key"], f"USD|FOMC Member Speaks|{SUNDAY}|1")
        self.assertEqual(rows[4]["source_event_key"], f"USD|FOMC Member Speaks|{SUNDAY}|0")
        # Equal times fall back to payload position.
        tie = parse([ev("Speech", "USD", "2026-10-20T13:00:00-04:00", "Low"),
                     ev("Speech", "USD", "2026-10-20T13:00:00-04:00", "Low")])
        self.assertEqual([row["source_event_key"][-1] for row in tie.rows], ["0", "1"])
        # Another currency is its own sequence.
        other = parse([ev("Speech", "USD", "2026-10-20T13:00:00-04:00", "Low"),
                       ev("Speech", "EUR", "2026-10-20T13:00:00-04:00", "Low")])
        self.assertEqual([row["source_event_key"] for row in other.rows],
                         [f"USD|Speech|{SUNDAY}|0", f"EUR|Speech|{SUNDAY}|0"])


class WeekTests(unittest.TestCase):
    def test_covered_to_is_the_next_new_york_sunday(self):
        self.assertEqual(parse().covered_to_ms, utc_ms("2026-10-25T04:00:00"))  # Sun 00:00 EDT
        # The week of 2026-11-01 holds the clock change: Sun 00:00 EDT to Sun 00:00 EST is 169 hours.
        dst = parse([ev("ISM Manufacturing PMI", "USD", "2026-11-02T10:00:00-05:00", "High")])
        self.assertEqual(dst.covered_to_ms, utc_ms("2026-11-08T05:00:00"))
        self.assertEqual(dst.covered_to_ms - utc_ms("2026-11-01T04:00:00"), 169 * HOUR)

    def test_mixed_weeks_raise_even_through_a_skipped_row(self):
        with self.assertRaises(ValueError):
            parse(WEEK + [ev("Retail Sales m/m", "CAD", "2026-10-25T08:30:00-04:00", "Low")])
        with self.assertRaises(ValueError):
            parse(WEEK + [ev("Bank Holiday", "CHF", "2026-10-26T00:00:00-04:00", "Holiday")])
        # Saturday 23:30 New York is still this week; Sunday 00:00 is the next one.
        self.assertEqual(len(parse(WEEK + [ev("Late", "AUD", "2026-10-24T23:30:00-04:00", "Low")]).rows), 7)
        with self.assertRaises(ValueError):
            parse(WEEK + [ev("Late", "AUD", "2026-10-25T00:00:00-04:00", "Low")])

    def test_an_empty_result_raises(self):
        with self.assertRaises(ValueError):
            parse([WEEK[2], WEEK[3]])
        with self.assertRaises(ValueError):
            parse([])


class StrictTests(unittest.TestCase):
    def test_all_with_medium_or_high_impact_needs_an_owner_decision(self):
        for impact in ("Medium", "High"):
            with self.subTest(impact=impact), self.assertRaisesRegex(ValueError, "needs an owner decision"):
                parse(WEEK + [ev("G7 Statement", "All", "2026-10-21T09:00:00-04:00", impact)])

    def test_malformed_elements_raise_with_their_index(self):
        good = WEEK[0]
        bad = [
            ev("CPI y/y", "GBP", "2026-10-21T02:00:00", "High"),  # naive date
            ev("CPI y/y", "GBP", "2026-10-21", "High"),  # date only, naive
            ev("CPI y/y", "GBP", "next Tuesday", "High"),
            ev("CPI y/y", "GBP", "2026-10-21T02:00:00-04:00", "Critical"),
            ev("CPI y/y", "GBP", "2026-10-21T02:00:00-04:00", "high"),
            ev("CPI y/y", "gbp", "2026-10-21T02:00:00-04:00", "High"),
            ev("CPI y/y", "EU", "2026-10-21T02:00:00-04:00", "High"),
            ev("CPI y/y", "all", "2026-10-21T02:00:00-04:00", "Low"),
            ev("   ", "GBP", "2026-10-21T02:00:00-04:00", "High"),
            ev("x" * 301, "GBP", "2026-10-21T02:00:00-04:00", "High"),
            {**good, "id": "1"},
            {key: value for key, value in good.items() if key != "previous"},
            {**good, "forecast": None},
            {**good, "previous": 3.0},
            "not an object",
        ]
        for element in bad:
            with self.subTest(element=element), self.assertRaisesRegex(ValueError, "feed element 1"):
                parse([good, element])
        for payload in (None, {"events": WEEK}, "[]"):
            with self.assertRaises(ValueError):
                parse_ff_week(payload)

    def test_a_key_over_200_characters_raises(self):
        with self.assertRaisesRegex(ValueError, "source_event_key"):
            parse([ev("t" * 200, "USD", "2026-10-20T13:00:00-04:00", "Low")])


class CalendarRowCompatibilityTests(unittest.TestCase):
    def test_parsed_rows_match_the_calendar_event_shape_where_they_overlap(self):
        # Shape get_fx_calendar_events would return for these rows (no database is used here).
        stored = [{"event_id": f"{row['source']}:{row['source_event_key']}:{utc_ms(row['event_ts'][:-1])}",
                   "ts_ms": utc_ms(row["event_ts"][:-1]), "currency": row["currency"], "impact": row["impact"],
                   "title": row["title"], "first_seen_ms": utc_ms("2026-10-17T12:00:00"), "source": row["source"],
                   "tags": []} for row in parse().rows]
        events = calendar_events_from_rows(stored)
        self.assertEqual(len(EventIndex(events)), 6)
        self.assertEqual(events[0].impact, Impact.HIGH)
        self.assertEqual((events[0].currency, events[0].title, events[0].ts_ms),
                         ("GBP", "CPI y/y", utc_ms("2026-10-21T06:00:00")))


if __name__ == "__main__":
    unittest.main()
