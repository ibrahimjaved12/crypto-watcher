import unittest
from decimal import Decimal

from market_analysis.movement import (
    BUCKET_INTERVAL_MS,
    DEFAULT_HISTORY_BUCKETS,
    MAX_LAST_TRADE_AGE_MS,
    MarketObservation,
    MovementBucketEngine,
    WINDOW_BUCKETS,
)

BASE = 1_800_000_000_000
INSTRUMENT = "binance-usdm:BTCUSDT"
ENDPOINT = "LIVE"


def observation(event_time_ms, price="100", quantity="1", trade_time_ms=None,
                received_at_ms=None):
    return MarketObservation(
        provider="binance-usdm",
        instrument_id=INSTRUMENT,
        price_type="trade",
        price=Decimal(price),
        quantity=Decimal(quantity),
        event_time_ms=event_time_ms,
        trade_time_ms=event_time_ms if trade_time_ms is None else trade_time_ms,
        received_at_ms=event_time_ms if received_at_ms is None else received_at_ms,
    )


class MovementBucketTests(unittest.TestCase):
    def setUp(self):
        self.engine = MovementBucketEngine(INSTRUMENT)

    def test_event_times_use_right_closed_five_second_buckets(self):
        first_boundary = BASE + BUCKET_INTERVAL_MS
        first = [
            observation(first_boundary - 1, "100", "2", received_at_ms=BASE + 9_000),
            observation(first_boundary, "101", "3", received_at_ms=BASE + 10_000),
            observation(first_boundary + 1, "999", "50"),
        ]
        self.assertEqual(self.engine.observe(first), 3)

        bucket = self.engine.advance(first_boundary)
        self.assertEqual(bucket.boundary_time_ms, first_boundary)
        self.assertEqual(bucket.price, Decimal("101"))
        self.assertEqual(bucket.base_volume, Decimal("5"))
        self.assertEqual(bucket.quote_volume, Decimal("503"))
        self.assertEqual(bucket.trade_count, 2)
        self.assertEqual(bucket.last_real_event_time_ms, first_boundary)
        self.assertEqual(bucket.last_received_at_ms, BASE + 10_000)
        self.assertFalse(bucket.carried_forward)

        next_bucket = self.engine.advance(first_boundary + BUCKET_INTERVAL_MS)
        self.assertEqual(next_bucket.price, Decimal("999"))
        self.assertEqual(next_bucket.trade_count, 1)

    def test_fresh_price_carries_forward_but_expires_at_trade_age_limit(self):
        first_boundary = BASE + BUCKET_INTERVAL_MS
        self.engine.observe([observation(first_boundary, trade_time_ms=first_boundary)])
        self.engine.advance(first_boundary)

        for step in range(1, MAX_LAST_TRADE_AGE_MS // BUCKET_INTERVAL_MS + 1):
            bucket = self.engine.advance(first_boundary + step * BUCKET_INTERVAL_MS)
            self.assertEqual(bucket.price, Decimal("100"))
            self.assertTrue(bucket.carried_forward)
            self.assertEqual(bucket.trade_count, 0)
            self.assertEqual(bucket.last_real_trade_time_ms, first_boundary)

        expired = self.engine.advance(first_boundary + MAX_LAST_TRADE_AGE_MS + BUCKET_INTERVAL_MS)
        self.assertIsNone(expired.price)
        self.assertEqual(expired.last_real_price, Decimal("100"))
        self.assertFalse(expired.carried_forward)
        status = self.engine.readiness(expired.boundary_time_ms, 1, ENDPOINT)
        self.assertEqual(status.state, "stale")
        self.assertEqual(status.last_real_trade_age_ms, MAX_LAST_TRADE_AGE_MS + BUCKET_INTERVAL_MS)

    def test_event_and_receive_times_do_not_replace_trade_time_freshness(self):
        boundary = BASE + BUCKET_INTERVAL_MS
        trade_time = boundary - MAX_LAST_TRADE_AGE_MS
        observation_value = observation(
            boundary,
            trade_time_ms=trade_time,
            received_at_ms=boundary + 30_000,
        )
        self.engine.observe([observation_value])
        bucket = self.engine.advance(boundary)

        self.assertEqual(bucket.last_real_event_time_ms, boundary)
        self.assertEqual(bucket.last_real_trade_time_ms, trade_time)
        self.assertEqual(bucket.last_received_at_ms, boundary + 30_000)
        self.assertEqual(bucket.price, Decimal("100"))

    def test_finalized_bucket_is_immutable_and_late_events_are_counted(self):
        boundary = BASE + BUCKET_INTERVAL_MS
        original = observation(boundary, "100", "2")
        self.engine.observe([original])
        finalized = self.engine.advance(boundary)

        late = observation(boundary - 1, "200", "9")
        self.assertEqual(self.engine.observe([late]), 0)
        self.assertEqual(self.engine.rejected_late_observations, 1)
        self.assertEqual(self.engine.history[-1], finalized)
        self.assertEqual(finalized.price, Decimal("100"))
        self.assertEqual(finalized.base_volume, Decimal("2"))

    def test_history_is_bounded_and_supports_two_adjacent_fifteen_minute_windows(self):
        count = DEFAULT_HISTORY_BUCKETS + 10
        boundaries = [BASE + index * BUCKET_INTERVAL_MS for index in range(1, count + 1)]
        for boundary in boundaries:
            self.assertEqual(
                self.engine.observe([observation(boundary, trade_time_ms=boundary)]), 1
            )
            self.engine.advance(boundary)

        history = self.engine.history
        self.assertEqual(len(history), DEFAULT_HISTORY_BUCKETS)
        self.assertEqual(history[0].boundary_time_ms, boundaries[-DEFAULT_HISTORY_BUCKETS])
        result = self.engine.readiness(boundaries[-1], 15, ENDPOINT)
        self.assertEqual(result.state, "ready")
        self.assertEqual(len(result.history), 361)
        self.assertEqual(
            result.history[0].boundary_time_ms,
            boundaries[-1] - 30 * 60_000,
        )

    def test_adjacent_window_readiness_requires_both_endpoint_anchors(self):
        for window_minutes, required_count in ((1, 25), (5, 121), (15, 361)):
            with self.subTest(window_minutes=window_minutes):
                engine = MovementBucketEngine(INSTRUMENT)
                self.assertEqual(WINDOW_BUCKETS[window_minutes], required_count)
                boundaries = [
                    BASE + index * BUCKET_INTERVAL_MS
                    for index in range(1, required_count + 1)
                ]

                for boundary in boundaries[:-1]:
                    engine.observe([observation(boundary, trade_time_ms=boundary)])
                    engine.advance(boundary)
                early = engine.readiness(boundaries[-2], window_minutes, ENDPOINT)
                self.assertEqual(early.state, "warming")
                self.assertEqual(len(early.history), required_count - 1)

                engine.observe([observation(boundaries[-1], trade_time_ms=boundaries[-1])])
                engine.advance(boundaries[-1])
                ready = engine.readiness(boundaries[-1], window_minutes, ENDPOINT)
                self.assertEqual(ready.state, "ready")
                self.assertEqual(len(ready.history), required_count)
                self.assertEqual(ready.history[-1].boundary_time_ms, boundaries[-1])
                self.assertEqual(
                    ready.history[0].boundary_time_ms,
                    boundaries[-1] - 2 * window_minutes * 60_000,
                )
                self.assertEqual(ready.history[0].price, Decimal("100"))

    def test_restart_starts_empty_and_reports_warming_without_reconstructed_buckets(self):
        boundary = BASE + BUCKET_INTERVAL_MS
        self.engine.observe([observation(boundary)])
        self.engine.advance(boundary)
        restarted = MovementBucketEngine(INSTRUMENT)

        self.assertEqual(restarted.history, ())
        restarted.observe([observation(boundary + BUCKET_INTERVAL_MS)])
        bucket = restarted.advance(boundary + BUCKET_INTERVAL_MS)
        result = restarted.readiness(bucket.boundary_time_ms, 1, ENDPOINT)

        self.assertEqual(len(restarted.history), 1)
        self.assertEqual(result.state, "warming")
        self.assertEqual(result.reason, "insufficient_exact_live_history")

    def test_readiness_exposes_collector_health_and_missing_history(self):
        boundary = BASE + BUCKET_INTERVAL_MS
        self.engine.observe([observation(boundary)])
        self.engine.advance(boundary)

        recovering = self.engine.readiness(boundary, 1, "RECOVERING")
        self.assertEqual(recovering.state, "unavailable")
        stale = self.engine.readiness(boundary, 1, "STALE")
        self.assertEqual(stale.state, "stale")
        missing = self.engine.readiness(boundary - BUCKET_INTERVAL_MS, 1, ENDPOINT)
        self.assertEqual(missing.state, "missing_history")

    def test_empty_pre_trade_buckets_never_make_history_ready(self):
        count = WINDOW_BUCKETS[1]
        boundaries = [
            BASE + index * BUCKET_INTERVAL_MS for index in range(1, count + 1)
        ]
        for boundary in boundaries:
            self.engine.advance(boundary)

        result = self.engine.readiness(boundaries[-1], 1, ENDPOINT)
        self.assertEqual(result.state, "unavailable")
        self.assertEqual(result.reason, "no_real_trade_history")
        self.assertTrue(all(bucket.price is None for bucket in result.history))

    def test_stale_gap_blocks_readiness_until_it_leaves_adjacent_history(self):
        first_boundary = BASE + BUCKET_INTERVAL_MS
        self.engine.observe([observation(first_boundary, trade_time_ms=first_boundary)])
        self.engine.advance(first_boundary)

        # Let the real trade expire, producing finalized buckets without prices.
        gap_end = first_boundary + 4 * BUCKET_INTERVAL_MS
        for boundary in range(
            first_boundary + BUCKET_INTERVAL_MS,
            gap_end + BUCKET_INTERVAL_MS,
            BUCKET_INTERVAL_MS,
        ):
            self.engine.advance(boundary)
        self.assertIsNone(self.engine.history[-1].price)

        recovery_start = gap_end + BUCKET_INTERVAL_MS
        for index in range(WINDOW_BUCKETS[1]):
            boundary = recovery_start + index * BUCKET_INTERVAL_MS
            self.engine.observe([observation(boundary, trade_time_ms=boundary)])
            self.engine.advance(boundary)
            result = self.engine.readiness(boundary, 1, ENDPOINT)
            if len(result.history) < WINDOW_BUCKETS[1]:
                self.assertEqual(result.state, "warming")
            elif index < WINDOW_BUCKETS[1] - 1:
                self.assertEqual(result.state, "stale")
                self.assertEqual(result.reason, "unusable_price_history")
            else:
                self.assertEqual(result.state, "ready")
                self.assertTrue(all(bucket.price is not None for bucket in result.history))

    def test_boundaries_must_be_explicit_aligned_and_consecutive(self):
        with self.assertRaises(ValueError):
            self.engine.advance(BASE + 1)
        self.engine.advance(BASE)
        with self.assertRaises(ValueError):
            self.engine.advance(BASE + 2 * BUCKET_INTERVAL_MS)


if __name__ == "__main__":
    unittest.main()