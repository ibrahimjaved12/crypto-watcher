"""Trading calendar (#374): FX week edges across DST, rollover, sessions, always-open crypto."""

from datetime import datetime, timezone
import unittest

from market_analysis.instruments import InstrumentKind, crypto_instrument, forex_instrument
from market_analysis.trading_calendar import (
    ALWAYS_OPEN, FX_WEEK, MINUTE_MS, OPEN_FOREVER_MS, FxWeekCalendar, LocalBreak, calendar_for,
)

HOUR = 60 * MINUTE_MS


def utc(text):
    """'2026-10-16T21:00' as UTC epoch milliseconds."""
    moment = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
    return int(moment.timestamp()) * 1000


# (close of the previous week, open of the next week, gap hours)
WEEKS = (
    (utc("2026-10-16T21:00"), utc("2026-10-18T21:00"), 48),  # normal
    (utc("2026-10-30T21:00"), utc("2026-11-01T22:00"), 49),  # US fall-back
    (utc("2026-03-06T22:00"), utc("2026-03-08T21:00"), 47),  # US spring-forward
)


class FxWeekTests(unittest.TestCase):
    def test_week_vectors_and_boundaries(self):
        for close, open_, gap in WEEKS:
            with self.subTest(close=close):
                self.assertEqual(FX_WEEK.week_window(close - 1)[1], close)
                self.assertEqual(FX_WEEK.week_window(close)[0], open_)
                self.assertEqual(FX_WEEK.week_window(open_)[0], open_)
                self.assertEqual(open_ - close, gap * HOUR)
                self.assertTrue(FX_WEEK.is_open(close - 1))
                self.assertFalse(FX_WEEK.is_open(close))
                self.assertFalse(FX_WEEK.is_open(open_ - 1))
                self.assertTrue(FX_WEEK.is_open(open_))
                self.assertEqual(FX_WEEK.open_until_ms(close - MINUTE_MS), close)
                self.assertIsNone(FX_WEEK.open_until_ms(close))

    def test_next_open_from_the_weekend(self):
        saturday = utc("2026-10-17T12:00")
        self.assertFalse(FX_WEEK.is_open(saturday))
        self.assertEqual(FX_WEEK.next_open_ms(saturday), utc("2026-10-18T21:00"))
        self.assertEqual(FX_WEEK.next_open_ms(utc("2026-11-01T21:30")), utc("2026-11-01T22:00"))
        self.assertEqual(FX_WEEK.next_open_ms(utc("2026-10-14T12:00")), utc("2026-10-14T12:00"))

    def test_open_minutes(self):
        for probe in (utc("2026-10-14T12:00"), utc("2026-11-04T12:00")):  # normal, after fall-back
            open_, close = FX_WEEK.week_window(probe)
            self.assertEqual(FX_WEEK.open_minutes(open_, close), 7200)
        self.assertEqual(FX_WEEK.open_minutes(utc("2026-10-16T20:00"), utc("2026-10-18T22:00")), 120)
        self.assertEqual(FX_WEEK.open_minutes(utc("2026-10-17T00:00"), utc("2026-10-17T23:00")), 0)

    def test_daily_break(self):
        calendar = FxWeekCalendar(daily_breaks=(LocalBreak((17, 0), (18, 0)),))
        monday_break, monday_reopen = utc("2026-10-12T21:00"), utc("2026-10-12T22:00")  # 17:00/18:00 EDT
        self.assertFalse(calendar.is_open(monday_break + 30 * MINUTE_MS))
        self.assertTrue(calendar.is_open(monday_reopen))
        self.assertEqual(calendar.open_until_ms(utc("2026-10-12T20:00")), monday_break)
        self.assertEqual(calendar.next_open_ms(monday_break), monday_reopen)
        sunday_open = utc("2026-10-11T21:00")
        self.assertFalse(calendar.is_open(sunday_open))  # the break moves the Sunday open to 18:00
        self.assertEqual(calendar.next_open_ms(sunday_open), sunday_open + HOUR)
        with self.assertRaises(ValueError):
            LocalBreak((18, 0), (17, 0))

    def test_closures(self):
        start, end = utc("2026-10-13T10:00"), utc("2026-10-13T14:00")
        calendar = FxWeekCalendar(closures=((start, end),))
        self.assertTrue(calendar.is_open(start - 1))
        self.assertFalse(calendar.is_open(start))
        self.assertFalse(calendar.is_open(end - 1))
        self.assertTrue(calendar.is_open(end))
        self.assertEqual(calendar.open_until_ms(start - MINUTE_MS), start)
        self.assertEqual(calendar.next_open_ms(start), end)
        self.assertEqual(calendar.open_minutes(start - HOUR, end + HOUR), 120)
        noon = utc("2026-10-13T12:00")
        sub_minute = FxWeekCalendar(closures=((noon + 30_000, noon + 45_000),))
        self.assertEqual(sub_minute.open_minutes(noon, noon + 2 * MINUTE_MS), 2)  # 12:00 and 12:01 are open
        with self.assertRaises(ValueError):
            FxWeekCalendar(closures=((end, start),))
        with self.assertRaises(ValueError):
            FxWeekCalendar(closures=((start, end), (start + 1, end + 1)))


class RolloverTests(unittest.TestCase):
    def test_normal_week_has_five_rollovers_with_triple_wednesday(self):
        open_, close = FX_WEEK.week_window(utc("2026-10-14T12:00"))
        events = FX_WEEK.rollover_events(open_, close)
        self.assertEqual([instant for instant, _ in events],
                         [utc(f"2026-10-{day}T21:00") for day in (12, 13, 14, 15, 16)])
        self.assertEqual([multiplier for _, multiplier in events], [1, 1, 3, 1, 1])
        self.assertEqual(sum(multiplier for _, multiplier in events), 7)

    def test_fall_back_shifts_utc_instant_not_local_time(self):
        events = FX_WEEK.rollover_events(utc("2026-10-30T00:00"), utc("2026-11-03T00:00"))
        self.assertEqual([instant for instant, _ in events],
                         [utc("2026-10-30T21:00"), utc("2026-11-02T22:00")])

    def test_entry_at_rollover_is_after_it_and_exit_at_rollover_is_charged(self):
        rollover = utc("2026-10-13T21:00")
        self.assertEqual(FX_WEEK.rollover_events(rollover, rollover + HOUR), [])
        self.assertEqual(FX_WEEK.rollover_events(rollover - HOUR, rollover), [(rollover, 1)])


class SessionTests(unittest.TestCase):
    def overlap_minutes(self, day):
        start = utc(f"{day}T00:00")
        return sum(1 for minute in range(1440)
                   if {"london", "new_york"} <= set(FX_WEEK.session_tags(start + minute * MINUTE_MS)))

    def test_tags_and_dst_dependent_overlap(self):
        self.assertEqual(FX_WEEK.session_tags(utc("2026-10-14T12:00")), ("london", "new_york"))
        self.assertIn("sydney", FX_WEEK.session_tags(utc("2026-10-14T20:00")))
        friday = utc("2026-10-16T20:00")  # FX open; Sydney is already Saturday 07:00
        self.assertTrue(FX_WEEK.is_open(friday))
        self.assertNotIn("sydney", FX_WEEK.session_tags(friday))
        self.assertEqual(FX_WEEK.session_tags(utc("2026-10-17T12:00")), ())
        self.assertEqual(self.overlap_minutes("2026-10-14"), 240)
        self.assertEqual(self.overlap_minutes("2026-10-26"), 300)  # only the EU has left DST
        self.assertEqual(self.overlap_minutes("2026-11-03"), 240)


class CalendarForTests(unittest.TestCase):
    def test_always_open_and_instrument_mapping(self):
        t = utc("2026-10-17T12:00")
        self.assertTrue(ALWAYS_OPEN.is_open(t))
        self.assertEqual(ALWAYS_OPEN.next_open_ms(t), t)
        self.assertEqual(ALWAYS_OPEN.open_until_ms(t), OPEN_FOREVER_MS)
        self.assertEqual(ALWAYS_OPEN.rollover_events(t, t + 7 * 24 * HOUR), [])
        self.assertEqual(ALWAYS_OPEN.open_minutes(t, t + HOUR), 60)
        self.assertIs(calendar_for(crypto_instrument("BTCUSDT")), ALWAYS_OPEN)
        self.assertIs(calendar_for(forex_instrument("EURUSD")), FX_WEEK)
        with self.assertRaises(ValueError):
            calendar_for(forex_instrument("EURUSD", kind=InstrumentKind.FUTURE))

    def test_input_validation(self):
        t = utc("2026-10-14T12:00")
        for bad in (float(t), True, str(t)):
            with self.assertRaises(ValueError):
                FX_WEEK.is_open(bad)
            with self.assertRaises(ValueError):
                ALWAYS_OPEN.is_open(bad)
        with self.assertRaises(ValueError):
            FX_WEEK.open_minutes(t + 1, t + HOUR)
        with self.assertRaises(ValueError):
            FX_WEEK.open_minutes(t + HOUR, t)


if __name__ == "__main__":
    unittest.main()
