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
                received_at_ms=None, aggregate_trade_id=None):
    return MarketObservation(
        provider="binance-usdm",
        instrument_id=INSTRUMENT,
        price_type="trade",
        price=Decimal(price),
        quantity=Decimal(quantity),
        event_time_ms=event_time_ms,
        trade_time_ms=event_time_ms if trade_time_ms is None else trade_time_ms,
        aggregate_trade_id=(
            event_time_ms if aggregate_trade_id is None else aggregate_trade_id
        ),
        received_at_ms=event_time_ms if received_at_ms is None else received_at_ms,
    )


class MovementBucketTests(unittest.TestCase):
    def setUp(self):
        self.engine = MovementBucketEngine(INSTRUMENT)

    def test_trade_time_uses_right_closed_buckets_and_event_time_remains_provenance(self):
        first_boundary = BASE + BUCKET_INTERVAL_MS
        first = [
            observation(first_boundary + 50, "100", "2", trade_time_ms=first_boundary - 1,
                        received_at_ms=BASE + 9_000),
            observation(first_boundary - 50, "101", "3", trade_time_ms=first_boundary,
                        received_at_ms=BASE + 10_000),
            observation(first_boundary + 1, "999", "50", trade_time_ms=first_boundary + 1),
        ]
        self.assertEqual(self.engine.observe(first), 3)

        bucket = self.engine.advance(first_boundary, "LIVE")
        self.assertEqual(bucket.boundary_time_ms, first_boundary)
        self.assertEqual(bucket.price, Decimal("101"))
        self.assertEqual(bucket.base_volume, Decimal("5"))
        self.assertEqual(bucket.quote_volume, Decimal("503"))
        self.assertEqual(bucket.trade_count, 2)
        self.assertEqual(bucket.last_real_event_time_ms, first_boundary - 50)
        self.assertEqual(bucket.last_received_at_ms, BASE + 10_000)
        self.assertFalse(bucket.carried_forward)

        next_bucket = self.engine.advance(first_boundary + BUCKET_INTERVAL_MS, "LIVE")
        self.assertEqual(next_bucket.price, Decimal("999"))
        self.assertEqual(next_bucket.trade_count, 1)

    def test_fresh_price_carries_forward_but_expires_at_trade_age_limit(self):
        first_boundary = BASE + BUCKET_INTERVAL_MS
        self.engine.observe([observation(first_boundary, trade_time_ms=first_boundary)])
        self.engine.advance(first_boundary, "LIVE")

        for step in range(1, MAX_LAST_TRADE_AGE_MS // BUCKET_INTERVAL_MS + 1):
            bucket = self.engine.advance(first_boundary + step * BUCKET_INTERVAL_MS, "LIVE")
            self.assertEqual(bucket.price, Decimal("100"))
            self.assertTrue(bucket.carried_forward)
            self.assertEqual(bucket.trade_count, 0)
            self.assertEqual(bucket.last_real_trade_time_ms, first_boundary)

        expired = self.engine.advance(
            first_boundary + MAX_LAST_TRADE_AGE_MS + BUCKET_INTERVAL_MS, "LIVE"
        )
        self.assertIsNone(expired.price)
        self.assertEqual(expired.last_real_price, Decimal("100"))
        self.assertFalse(expired.carried_forward)
        status = self.engine.readiness(expired.boundary_time_ms, 1, ENDPOINT)
        self.assertEqual(status.state, "stale")
        self.assertEqual(status.last_real_trade_age_ms, MAX_LAST_TRADE_AGE_MS + BUCKET_INTERVAL_MS)

    def test_event_and_receive_times_do_not_replace_trade_time_freshness(self):
        boundary = BASE + BUCKET_INTERVAL_MS
        trade_time = boundary - 1
        observation_value = observation(
            boundary + 30_000,
            trade_time_ms=trade_time,
            received_at_ms=boundary + 60_000,
        )
        self.engine.observe([observation_value])
        bucket = self.engine.advance(boundary, "LIVE")

        self.assertEqual(bucket.last_real_event_time_ms, boundary + 30_000)
        self.assertEqual(bucket.last_real_trade_time_ms, trade_time)
        self.assertEqual(bucket.last_received_at_ms, boundary + 60_000)
        self.assertEqual(bucket.price, Decimal("100"))

    def test_finalized_bucket_is_immutable_and_late_events_are_counted(self):
        boundary = BASE + BUCKET_INTERVAL_MS
        original = observation(boundary, "100", "2")
        self.engine.observe([original])
        finalized = self.engine.advance(boundary, "LIVE")

        late = observation(
            boundary + 20_000,
            "200",
            "9",
            trade_time_ms=boundary - 1,
            received_at_ms=boundary + 30_000,
        )
        self.assertEqual(self.engine.observe([late]), 0)
        self.assertEqual(self.engine.rejected_late_observations, 1)
        self.assertEqual(self.engine.history[-1], finalized)
        self.assertEqual(finalized.price, Decimal("100"))
        self.assertEqual(finalized.base_volume, Decimal("2"))

    def test_old_trade_time_with_later_event_and_receive_cannot_enter_new_bucket(self):
        boundary = BASE + BUCKET_INTERVAL_MS
        self.engine.observe([observation(boundary, "100", "2")])
        self.engine.advance(boundary, "LIVE")

        delayed = observation(
            boundary + 60_000,
            "900",
            "80",
            trade_time_ms=boundary - 1,
            received_at_ms=boundary + 70_000,
        )
        self.assertEqual(self.engine.observe([delayed]), 0)
        self.assertEqual(self.engine.rejected_late_observations, 1)
        following = self.engine.advance(boundary + BUCKET_INTERVAL_MS, "LIVE")
        self.assertEqual(following.price, Decimal("100"))
        self.assertEqual(following.base_volume, Decimal(0))
        self.assertEqual(following.quote_volume, Decimal(0))
        self.assertEqual(following.trade_count, 0)
        self.assertEqual(following.last_real_event_time_ms, boundary)
        self.assertEqual(following.last_received_at_ms, boundary)

    def test_same_trade_time_uses_later_aggregate_id_as_endpoint(self):
        boundary = BASE + BUCKET_INTERVAL_MS
        observations = [
            observation(
                boundary + 100,
                "101",
                "2",
                trade_time_ms=boundary,
                received_at_ms=boundary + 500,
                aggregate_trade_id=700,
            ),
            observation(
                boundary - 100,
                "102",
                "3",
                trade_time_ms=boundary,
                received_at_ms=boundary + 400,
                aggregate_trade_id=701,
            ),
        ]
        self.assertEqual(self.engine.observe(observations), 2)
        bucket = self.engine.advance(boundary, "LIVE")

        self.assertEqual(bucket.price, Decimal("102"))
        self.assertEqual(bucket.trade_count, 2)
        self.assertEqual(bucket.base_volume, Decimal("5"))
        self.assertEqual(bucket.quote_volume, Decimal("508"))
        self.assertEqual(bucket.last_real_event_time_ms, boundary - 100)
        self.assertEqual(bucket.last_received_at_ms, boundary + 400)

    def test_canonical_order_is_validated_across_observe_calls(self):
        boundary = BASE + BUCKET_INTERVAL_MS
        first = observation(
            boundary - 2,
            "100",
            trade_time_ms=boundary - 1,
            aggregate_trade_id=20,
        )
        second = observation(
            boundary - 1,
            "101",
            trade_time_ms=boundary - 1,
            aggregate_trade_id=21,
        )
        self.assertEqual(self.engine.observe([first]), 1)
        self.assertEqual(self.engine.observe([second]), 1)
        bucket = self.engine.advance(boundary, "LIVE")

        self.assertEqual(bucket.price, Decimal("101"))
        self.assertEqual(bucket.trade_count, 2)
        self.assertEqual(bucket.base_volume, Decimal("2"))

    def test_cross_call_order_regression_is_rejected_without_mutating_pending_bucket(self):
        boundary = BASE + BUCKET_INTERVAL_MS
        accepted = observation(
            boundary - 1,
            "100",
            "2",
            trade_time_ms=boundary - 1,
            aggregate_trade_id=30,
        )
        regression = observation(
            boundary,
            "999",
            "90",
            trade_time_ms=boundary - 1,
            aggregate_trade_id=29,
        )
        self.assertEqual(self.engine.observe([accepted]), 1)
        with self.assertRaises(ValueError):
            self.engine.observe([regression])

        bucket = self.engine.advance(boundary, "LIVE")
        self.assertEqual(bucket.price, Decimal("100"))
        self.assertEqual(bucket.base_volume, Decimal("2"))
        self.assertEqual(bucket.quote_volume, Decimal("200"))
        self.assertEqual(bucket.trade_count, 1)

    def test_in_batch_order_regression_is_rejected_atomically(self):
        boundary = BASE + BUCKET_INTERVAL_MS
        earlier_key = observation(
            boundary,
            "100",
            trade_time_ms=boundary - 1,
            aggregate_trade_id=49,
        )
        later_key = observation(
            boundary + 1,
            "101",
            trade_time_ms=boundary - 1,
            aggregate_trade_id=50,
        )
        with self.assertRaises(ValueError):
            self.engine.observe([later_key, earlier_key])

        bucket = self.engine.advance(boundary, "LIVE")
        self.assertEqual(bucket.trade_count, 0)
        self.assertIsNone(bucket.price)

    def test_replaying_same_ordered_observations_produces_identical_buckets(self):
        first_boundary = BASE + BUCKET_INTERVAL_MS
        batches = [
            [
                observation(
                    first_boundary + 20,
                    "100",
                    "2",
                    trade_time_ms=first_boundary - 1,
                    aggregate_trade_id=40,
                ),
                observation(
                    first_boundary - 20,
                    "101",
                    "3",
                    trade_time_ms=first_boundary,
                    aggregate_trade_id=41,
                ),
            ],
            [
                observation(
                    first_boundary - 30,
                    "102",
                    "4",
                    trade_time_ms=first_boundary,
                    aggregate_trade_id=42,
                )
            ],
        ]

        def replay():
            engine = MovementBucketEngine(INSTRUMENT)
            for batch in batches:
                self.assertEqual(engine.observe(batch), len(batch))
            engine.advance(first_boundary, "LIVE")
            engine.advance(first_boundary + BUCKET_INTERVAL_MS, "LIVE")
            return engine.history

        self.assertEqual(replay(), replay())

    def test_history_is_bounded_and_supports_two_adjacent_fifteen_minute_windows(self):
        count = DEFAULT_HISTORY_BUCKETS + 10
        boundaries = [BASE + index * BUCKET_INTERVAL_MS for index in range(1, count + 1)]
        for boundary in boundaries:
            self.assertEqual(
                self.engine.observe([observation(boundary, trade_time_ms=boundary)]), 1
            )
            self.engine.advance(boundary, "LIVE")

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
                    engine.advance(boundary, "LIVE")
                early = engine.readiness(boundaries[-2], window_minutes, ENDPOINT)
                self.assertEqual(early.state, "warming")
                self.assertEqual(len(early.history), required_count - 1)

                engine.observe([observation(boundaries[-1], trade_time_ms=boundaries[-1])])
                engine.advance(boundaries[-1], "LIVE")
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
        self.engine.advance(boundary, "LIVE")
        restarted = MovementBucketEngine(INSTRUMENT)

        self.assertEqual(restarted.history, ())
        restarted.observe([observation(boundary + BUCKET_INTERVAL_MS)])
        bucket = restarted.advance(boundary + BUCKET_INTERVAL_MS, "LIVE")
        result = restarted.readiness(bucket.boundary_time_ms, 1, ENDPOINT)

        self.assertEqual(len(restarted.history), 1)
        self.assertEqual(result.state, "warming")
        self.assertEqual(result.reason, "insufficient_exact_live_history")

    def test_readiness_exposes_collector_health_and_missing_history(self):
        boundary = BASE + BUCKET_INTERVAL_MS
        self.engine.observe([observation(boundary)])
        self.engine.advance(boundary, "LIVE")

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
            self.engine.advance(boundary, "LIVE")

        result = self.engine.readiness(boundaries[-1], 1, ENDPOINT)
        self.assertEqual(result.state, "unavailable")
        self.assertEqual(result.reason, "no_real_trade_history")
        self.assertTrue(all(bucket.price is None for bucket in result.history))

    def test_stale_gap_blocks_readiness_until_it_leaves_adjacent_history(self):
        first_boundary = BASE + BUCKET_INTERVAL_MS
        self.engine.observe([observation(first_boundary, trade_time_ms=first_boundary)])
        self.engine.advance(first_boundary, "LIVE")

        # Let the real trade expire, producing finalized buckets without prices.
        gap_end = first_boundary + 4 * BUCKET_INTERVAL_MS
        for boundary in range(
            first_boundary + BUCKET_INTERVAL_MS,
            gap_end + BUCKET_INTERVAL_MS,
            BUCKET_INTERVAL_MS,
        ):
            self.engine.advance(boundary, "LIVE")
        self.assertIsNone(self.engine.history[-1].price)

        recovery_start = gap_end + BUCKET_INTERVAL_MS
        for index in range(WINDOW_BUCKETS[1]):
            boundary = recovery_start + index * BUCKET_INTERVAL_MS
            self.engine.observe([observation(boundary, trade_time_ms=boundary)])
            self.engine.advance(boundary, "LIVE")
            result = self.engine.readiness(boundary, 1, ENDPOINT)
            if len(result.history) < WINDOW_BUCKETS[1]:
                self.assertEqual(result.state, "warming")
            elif index < WINDOW_BUCKETS[1] - 1:
                self.assertEqual(result.state, "stale")
                self.assertEqual(result.reason, "unusable_price_history")
            else:
                self.assertEqual(result.state, "ready")
                self.assertTrue(all(bucket.price is not None for bucket in result.history))

    def test_outage_preserves_factual_provenance_but_breaks_price_carry(self):
        boundary = BASE + BUCKET_INTERVAL_MS
        first = observation(
            boundary - 100,
            "100",
            trade_time_ms=boundary,
            received_at_ms=boundary + 100,
            aggregate_trade_id=1,
        )
        self.engine.observe([first])
        self.engine.advance(boundary, "LIVE")

        recovering = self.engine.advance(
            boundary + BUCKET_INTERVAL_MS,
            "RECOVERING",
        )
        self.assertIsNone(recovering.price)
        self.assertFalse(recovering.carried_forward)
        self.assertEqual(recovering.last_real_trade_time_ms, boundary)
        self.assertEqual(recovering.last_real_event_time_ms, boundary - 100)
        self.assertEqual(recovering.last_received_at_ms, boundary + 100)

        resumed = self.engine.advance(
            boundary + 2 * BUCKET_INTERVAL_MS,
            "LIVE",
        )
        self.assertIsNone(resumed.price)
        self.assertFalse(resumed.carried_forward)
        self.assertEqual(resumed.last_real_trade_time_ms, boundary)
        self.assertEqual(resumed.last_real_event_time_ms, boundary - 100)
        self.assertEqual(resumed.last_received_at_ms, boundary + 100)
        self.assertEqual(
            self.engine.readiness(
                boundary + 2 * BUCKET_INTERVAL_MS,
                1,
                "LIVE",
            ).last_real_trade_age_ms,
            2 * BUCKET_INTERVAL_MS,
        )

        new_boundary = boundary + 3 * BUCKET_INTERVAL_MS
        new_trade = observation(
            new_boundary - 50,
            "110",
            trade_time_ms=new_boundary,
            received_at_ms=new_boundary + 25,
            aggregate_trade_id=2,
        )
        self.engine.observe([new_trade])
        renewed = self.engine.advance(new_boundary, "LIVE")
        self.assertEqual(renewed.price, Decimal("110"))
        self.assertFalse(renewed.carried_forward)

        carried = self.engine.advance(new_boundary + BUCKET_INTERVAL_MS, "LIVE")
        self.assertEqual(carried.price, Decimal("110"))
        self.assertTrue(carried.carried_forward)
        self.assertEqual(carried.last_real_trade_time_ms, new_boundary)
        self.assertEqual(carried.last_real_event_time_ms, new_boundary - 50)
        self.assertEqual(carried.last_received_at_ms, new_boundary + 25)

    def test_short_source_outage_is_not_bridged_and_ages_out_for_every_window(self):
        for window_minutes, required_count in ((1, 25), (5, 121), (15, 361)):
            with self.subTest(window_minutes=window_minutes):
                engine = MovementBucketEngine(INSTRUMENT)
                boundary = BASE
                for _ in range(required_count):
                    boundary += BUCKET_INTERVAL_MS
                    engine.observe([observation(boundary, trade_time_ms=boundary)])
                    engine.advance(boundary, "LIVE")
                self.assertEqual(
                    engine.readiness(boundary, window_minutes, "LIVE").state, "ready"
                )

                outage_state = {
                    1: "RECOVERING",
                    5: "STALE",
                    15: "UNAVAILABLE",
                }[window_minutes]
                outage_boundary = boundary + BUCKET_INTERVAL_MS
                outage = engine.advance(outage_boundary, outage_state)
                self.assertEqual(outage.source_state, outage_state)
                self.assertIsNone(outage.price)
                self.assertTrue(outage_boundary - boundary < MAX_LAST_TRADE_AGE_MS)

                # Returning LIVE without a new real trade cannot reuse the pre-outage price.
                recovery_boundary = outage_boundary + BUCKET_INTERVAL_MS
                resumed = engine.advance(recovery_boundary, "LIVE")
                self.assertIsNone(resumed.price)
                self.assertTrue(
                    engine.readiness(recovery_boundary, window_minutes, "LIVE").state
                    != "ready"
                )

                for recovered_count in range(1, required_count + 1):
                    recovery_boundary += BUCKET_INTERVAL_MS
                    engine.observe([
                        observation(recovery_boundary, trade_time_ms=recovery_boundary)
                    ])
                    engine.advance(recovery_boundary, "LIVE")
                    result = engine.readiness(recovery_boundary, window_minutes, "LIVE")
                    if recovered_count < required_count:
                        self.assertNotEqual(result.state, "ready")
                        self.assertTrue(
                            any(
                                bucket.source_state != "LIVE" or bucket.price is None
                                for bucket in result.history
                            )
                        )
                    else:
                        self.assertEqual(result.state, "ready")
                        self.assertTrue(
                            all(bucket.source_state == "LIVE" and bucket.price is not None
                                for bucket in result.history)
                        )

    def test_boundaries_must_be_explicit_aligned_and_consecutive(self):
        with self.assertRaises(ValueError):
            self.engine.advance(BASE + 1, "LIVE")
        self.engine.advance(BASE, "LIVE")
        with self.assertRaises(ValueError):
            self.engine.advance(BASE + 2 * BUCKET_INTERVAL_MS, "LIVE")


if __name__ == "__main__":
    unittest.main()
