"""News blackout filter (#374): inclusive windows, currency/tag mapping, point in time, modes."""

from collections import namedtuple
from datetime import datetime, timezone
import unittest

from market_analysis.instruments import (
    FOREX_DEFAULTS, FOREX_OPTIONAL, crypto_instrument, forex_instrument,
)
from market_analysis.news_blackout import (
    DEFAULT_RULE, BlackoutMode, BlackoutRule, CalendarEvent, EventIndex, Impact, apply_blackout, blackout,
)

MINUTE = 60_000
DAY = 1440 * MINUTE


def utc(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()) * 1000


NFP = utc("2026-11-06T13:30:00")
SEEN = NFP - 14 * DAY


def event(event_id, ts, currency, impact, title="", first_seen=None, tags=()):
    return CalendarEvent(event_id, ts, currency, impact, title, SEEN if first_seen is None else first_seen,
                         frozenset(tags))


EVENTS = [
    event("nfp", NFP, "USD", Impact.HIGH, "Non-Farm Payrolls"),
    event("eur-pmi", NFP + DAY, "EUR", Impact.MEDIUM, "PMI"),
    event("usd-low", NFP + 2 * DAY, "USD", Impact.LOW, "Low"),
    event("crude", NFP + 3 * DAY, "USD", Impact.MEDIUM, "Crude Oil Inventories", tags={"oil"}),
]
INDEX = EventIndex(EVENTS)
EURUSD, USDJPY, EURGBP = forex_instrument("EURUSD"), forex_instrument("USDJPY"), forex_instrument("EURGBP")
XAUUSD, USOIL = FOREX_DEFAULTS[0], FOREX_DEFAULTS[3]
DXY = FOREX_OPTIONAL[0]
BTC = crypto_instrument("BTCUSDT")
Signal = namedtuple("Signal", "name signal_ms")


class WindowTests(unittest.TestCase):
    def test_inclusive_ends_and_currency_mapping(self):
        self.assertTrue(blackout(EURUSD, NFP - 30 * MINUTE, INDEX).active)
        self.assertFalse(blackout(EURUSD, NFP - 30 * MINUTE - 1, INDEX).active)
        self.assertTrue(blackout(EURUSD, NFP + 30 * MINUTE, INDEX).active)
        self.assertFalse(blackout(EURUSD, NFP + 30 * MINUTE + 1, INDEX).active)
        decision = blackout(USDJPY, NFP, INDEX)
        self.assertEqual([e.event_id for e in decision.events], ["nfp"])
        self.assertEqual((decision.window_start_ms, decision.window_end_ms), (NFP - 30 * MINUTE, NFP + 30 * MINUTE))
        self.assertFalse(blackout(EURGBP, NFP, INDEX).active)
        inactive = blackout(EURGBP, NFP, INDEX)
        self.assertEqual((inactive.events, inactive.window_start_ms, inactive.window_end_ms), ((), None, None))

    def test_metals_energy_index_and_crypto(self):
        self.assertTrue(blackout(XAUUSD, NFP, INDEX).active)
        gold = EventIndex([event("cny-gold", NFP, "CNY", Impact.HIGH, tags={"gold"})])
        self.assertTrue(blackout(XAUUSD, NFP, gold).active)
        self.assertFalse(blackout(EURUSD, NFP, gold).active)
        crude = NFP + 3 * DAY
        self.assertFalse(blackout(USOIL, crude, INDEX).active)  # medium is below the default HIGH
        medium = BlackoutRule(DEFAULT_RULE.before_ms, DEFAULT_RULE.after_ms, min_impact=Impact.MEDIUM)
        self.assertTrue(blackout(USOIL, crude, INDEX, medium).active)
        oil_elsewhere = EventIndex([event("cad-oil", NFP, "CAD", Impact.HIGH, tags={"oil"})])
        self.assertTrue(blackout(USOIL, NFP, oil_elsewhere).active)
        eur_high = EventIndex([event("ecb", NFP, "EUR", Impact.HIGH)])
        self.assertTrue(blackout(DXY, NFP, eur_high).active)
        self.assertFalse(blackout(BTC, NFP, INDEX).active)
        self.assertFalse(blackout(BTC, NFP, eur_high).active)

    def test_point_in_time(self):
        late = EventIndex([event("late", NFP, "USD", Impact.HIGH, first_seen=NFP - 10 * MINUTE)])
        self.assertFalse(blackout(EURUSD, NFP - 20 * MINUTE, late).active)  # not yet published
        self.assertTrue(blackout(EURUSD, NFP - 10 * MINUTE, late).active)
        self.assertTrue(blackout(EURUSD, NFP - 5 * MINUTE, late).active)

    def test_event_discovered_after_release(self):
        found = EventIndex([event("late-nfp", NFP, "USD", Impact.HIGH, first_seen=NFP + 5 * MINUTE)])  # 13:35
        self.assertFalse(blackout(EURUSD, NFP + 4 * MINUTE, found).active)  # before discovery
        self.assertTrue(blackout(EURUSD, NFP + 5 * MINUTE, found).active)
        self.assertTrue(blackout(EURUSD, NFP + 10 * MINUTE, found).active)  # 13:40 is suppressed
        self.assertFalse(blackout(EURUSD, NFP + 30 * MINUTE + 1, found).active)  # window over

    def test_overlapping_windows_union(self):
        both = EventIndex([event("a", NFP, "USD", Impact.HIGH), event("b", NFP + 20 * MINUTE, "USD", Impact.HIGH)])
        decision = blackout(EURUSD, NFP + 10 * MINUTE, both)
        self.assertEqual([e.event_id for e in decision.events], ["a", "b"])
        self.assertEqual((decision.window_start_ms, decision.window_end_ms),
                         (NFP - 30 * MINUTE, NFP + 50 * MINUTE))


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.signals = [Signal("before", NFP - 2 * 60 * MINUTE), Signal("inside", NFP),
                        Signal("edge", NFP + 30 * MINUTE), Signal("after", NFP + 2 * 60 * MINUTE)]

    def test_off_never_touches_the_index(self):
        class Exploding(EventIndex):
            def window_candidates(self, lo_ms, hi_ms):
                raise AssertionError("OFF must not look up events")

        result = apply_blackout(self.signals, EURUSD, Exploding(EVENTS), BlackoutMode.OFF)
        self.assertEqual((result.kept, result.tagged, result.suppressed), (self.signals, [], []))

    def test_tag_keeps_and_marks(self):
        result = apply_blackout(self.signals, EURUSD, INDEX, BlackoutMode.TAG)
        self.assertEqual(result.kept, self.signals)
        self.assertEqual([item.name for item, _ in result.tagged], ["inside", "edge"])
        self.assertEqual(result.suppressed, [])

    def test_suppress_splits_in_order_with_decisions(self):
        result = apply_blackout(self.signals, EURUSD, INDEX, "suppress")
        self.assertEqual([item.name for item in result.kept], ["before", "after"])
        self.assertEqual([item.name for item, _ in result.suppressed], ["inside", "edge"])
        self.assertEqual(result.suppressed[0][1].events[0].event_id, "nfp")
        crypto = apply_blackout(self.signals, BTC, INDEX, BlackoutMode.SUPPRESS)
        self.assertEqual(crypto.kept, self.signals)

    def test_custom_ts_of(self):
        setups = [{"id": 1, "entry_ms": NFP}, {"id": 2, "entry_ms": NFP + DAY // 2}]
        result = apply_blackout(setups, EURUSD, INDEX, BlackoutMode.SUPPRESS, ts_of=lambda s: s["entry_ms"])
        self.assertEqual([s["id"] for s in result.kept], [2])


class ValidationTests(unittest.TestCase):
    def test_bad_inputs(self):
        with self.assertRaises(ValueError):
            EventIndex([event("x", NFP, "USD", Impact.HIGH), event("x", NFP + 1, "USD", Impact.HIGH)])
        for currency in ("usd", "US", "USDT", 840):
            with self.assertRaises(ValueError):
                event("z", NFP, currency, Impact.HIGH)
        with self.assertRaises(ValueError):
            BlackoutRule({Impact.HIGH: 0}, DEFAULT_RULE.after_ms)
        with self.assertRaises(ValueError):
            BlackoutRule(DEFAULT_RULE.before_ms, {**DEFAULT_RULE.after_ms, Impact.LOW: -1})
        with self.assertRaises(ValueError):
            blackout(EURUSD, float(NFP), INDEX)


class PerformanceTests(unittest.TestCase):
    def test_lookups_use_bisect(self):
        sizes = []

        class Counting(EventIndex):
            def window_candidates(self, lo_ms, hi_ms):
                found = super().window_candidates(lo_ms, hi_ms)
                sizes.append(len(found))
                return found

        start = utc("2026-01-05T00:00:00")
        currencies = ("USD", "EUR", "JPY", "GBP", "CAD")
        index = Counting(event(f"e{i}", start + i * 7 * MINUTE, currencies[i % 5], Impact.HIGH, first_seen=start - DAY)
                         for i in range(5000))
        for j in range(2000):
            blackout(EURUSD, start + j * 17 * MINUTE, index)
        self.assertEqual(len(sizes), 2000)
        self.assertLessEqual(max(sizes), 9)  # a 60-minute span of 7-minute spaced events, never the whole list


if __name__ == "__main__":
    unittest.main()
