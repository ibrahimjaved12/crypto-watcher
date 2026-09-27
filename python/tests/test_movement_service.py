import unittest
from dataclasses import asdict
from decimal import Decimal
from types import SimpleNamespace

from market_analysis.api_models import (MovementBoundaryRequest,
                                        MovementClassificationRequest,
                                        MovementHistoryRegistrationRequest,
                                        MovementLifecycleRequest, MovementMetricsRequest)
from market_analysis.market_episode_lifecycle import (
    deserialize_market_episode_lifecycle_state, serialize_market_episode_lifecycle_state,
)
from market_analysis.movement import (DEFAULT_HISTORY_BUCKETS, MarketObservation,
                                      MovementBucketEngine)
from market_analysis.movement_metrics import MarketMovementConfig
from market_analysis.movement_classifier import ALGORITHM_VERSION, MarketClassifierConfig
from market_analysis.movement_service import MovementBoundaryService

BASE = 1_800_000_000_000
SESSION = "2af3e7c8-b777-4e58-9ad2-18e36daac160"


def request(boundary, symbols, session_id=SESSION):
    return MovementBoundaryRequest.model_validate({
        "schema_version": 1,
        "session_id": session_id,
        "boundary_time_ms": boundary,
        "symbols": symbols,
    })


def symbol_input(symbol, observations=(), source_state="LIVE", membership_epoch=1):
    return {
        "symbol": symbol,
        "instrument_id": f"binance-usdm:{symbol}",
        "membership_epoch": membership_epoch,
        "source_state": source_state,
        "observations": list(observations),
    }


def observation(trade_time, aggregate_id, price="100", quantity="1"):
    return {
        "price": price,
        "quantity": quantity,
        "event_time_ms": trade_time + 20,
        "trade_time_ms": trade_time,
        "aggregate_trade_id": aggregate_id,
        "received_at_ms": trade_time + 30,
    }


class MovementBoundaryServiceTests(unittest.TestCase):
    def test_snapshot_preserves_every_canonical_readiness_state_and_reason(self):
        states = (
            ("ready", None),
            ("warming", "insufficient_exact_live_history"),
            ("stale", "last_real_trade_expired"),
            ("missing_history", "noncontiguous_live_history"),
            ("unavailable", "source_unavailable_in_required_history"),
        )

        expected_status = {
            "ready": "READY",
            "warming": "WARMING",
            "stale": "STALE",
            "missing_history": "STALE",
            "unavailable": "STALE",
        }
        for state, reason in states:
            with self.subTest(state=state):
                class ReadinessEngine:
                    history = ()

                    def readiness(self, boundary, window, collector_state):
                        return SimpleNamespace(state=state, reason=reason)

                snapshot = MovementBoundaryService._snapshot(
                    "BTCUSDT", ReadinessEngine(), BASE, "LIVE"
                )
                for window in (1, 5, 15):
                    transported = snapshot["readiness"][window]
                    self.assertEqual(transported["state"], state)
                    self.assertEqual(transported["reason"], reason)
                    self.assertEqual(transported["status"], expected_status[state])

    def test_failed_later_symbol_does_not_commit_earlier_symbol_boundary(self):
        service = MovementBoundaryService()
        first = request(
            BASE,
            [
                symbol_input("BTCUSDT"),
                symbol_input("ETHUSDT", [observation(BASE + 5_000, 30)]),
            ],
        )
        initial = service.advance(first)
        self.assertEqual(len(initial["snapshots"]), 2)

        second = request(
            BASE + 5_000,
            [
                symbol_input("BTCUSDT", [observation(BASE + 5_000, 20)]),
                # Same trade time with a regressing aggregate ID fails after BTC
                # has been staged, but before either symbol state is committed.
                symbol_input("ETHUSDT", [observation(BASE + 5_000, 29)]),
            ],
        )
        with self.assertRaises(ValueError):
            service.advance(second)

        # Retry with valid ETH ordering. BTC's trade must still be applied exactly
        # once at this boundary, proving its failed staged advance was rolled back.
        retry = request(
            BASE + 5_000,
            [
                symbol_input("BTCUSDT", [observation(BASE + 5_000, 20)]),
                symbol_input("ETHUSDT", [observation(BASE + 5_000, 31)]),
            ],
        )
        result = service.advance(retry)
        btc = next(snapshot for snapshot in result["snapshots"] if snapshot["symbol"] == "BTCUSDT")
        btc_bucket = btc["buckets"][-1]
        self.assertEqual(btc_bucket["tradeCount"], 1)
        self.assertEqual(btc_bucket["baseQuantity"], 1)

    def test_exact_input_retry_returns_cached_result(self):
        service = MovementBoundaryService()
        body = request(BASE, [symbol_input("BTCUSDT", [observation(BASE, 1)])])
        first = service.advance(body)
        retried = service.advance(body)
        self.assertIs(retried, first)

    def test_high_precision_decimal_text_reaches_canonical_observation_unchanged(self):
        service = MovementBoundaryService()
        body = request(
            BASE,
            [symbol_input("BTCUSDT", [observation(
                BASE,
                1,
                "101.000000000000000001",
                "2.000000000000000009",
            )])],
        )
        service.advance(body)
        engine = service.sessions[SESSION]["engines"]["BTCUSDT"]
        observed = engine._last_real_observation

        self.assertEqual(observed.price, Decimal("101.000000000000000001"))
        self.assertEqual(observed.quantity, Decimal("2.000000000000000009"))

    def test_new_symbol_membership_epoch_replaces_only_that_canonical_engine(self):
        service = MovementBoundaryService()
        result = None
        for index in range(25):
            boundary = BASE + index * 5_000
            result = service.advance(request(
                boundary,
                [
                    symbol_input(
                        "BTCUSDT",
                        [observation(boundary, index + 1, str(100 + index))],
                        membership_epoch=7,
                    ),
                    symbol_input(
                        "ETHUSDT",
                        [observation(boundary, index + 101, str(200 + index))],
                        membership_epoch=1,
                    ),
                ],
            ))

        eth_before = next(
            item for item in result["snapshots"] if item["symbol"] == "ETHUSDT"
        )
        self.assertEqual(eth_before["readiness"][1]["state"], "ready")
        eth_epoch_1_engine = service.sessions[SESSION]["engines"]["ETHUSDT"]
        self.assertEqual(len(eth_epoch_1_engine.history), 25)
        btc_first_price = service.sessions[SESSION]["engines"]["BTCUSDT"].history[0].price

        # No omission request reaches Python between membership tenures. The lower
        # aggregate ID makes the fresh epoch-2 ordering state explicit below.
        boundary = BASE + 25 * 5_000
        replaced = service.advance(request(
            boundary,
            [
                symbol_input(
                    "BTCUSDT",
                    [observation(boundary, 26, "125")],
                    membership_epoch=7,
                ),
                symbol_input(
                    "ETHUSDT",
                    [observation(boundary, 1, "999")],
                    membership_epoch=2,
                ),
            ],
        ))

        eth_after = next(
            item for item in replaced["snapshots"] if item["symbol"] == "ETHUSDT"
        )
        btc_after = next(
            item for item in replaced["snapshots"] if item["symbol"] == "BTCUSDT"
        )
        self.assertEqual(len(eth_after["buckets"]), 1)
        self.assertEqual(eth_after["buckets"][0]["endpointPrice"], 999.0)
        self.assertEqual(eth_after["readiness"][1]["state"], "warming")
        self.assertEqual(service.sessions[SESSION]["membership_epochs"]["ETHUSDT"], 2)
        self.assertIsNot(
            service.sessions[SESSION]["engines"]["ETHUSDT"],
            eth_epoch_1_engine,
        )
        self.assertEqual(
            service.sessions[SESSION]["engines"]["ETHUSDT"]._last_accepted_order_key,
            (boundary, 1),
        )
        self.assertEqual(len(service.sessions[SESSION]["engines"]["BTCUSDT"].history), 26)
        self.assertEqual(btc_after["readiness"][1]["state"], "ready")
        self.assertEqual(
            service.sessions[SESSION]["engines"]["BTCUSDT"].history[0].price,
            btc_first_price,
        )
        self.assertEqual(service.sessions[SESSION]["membership_epochs"]["BTCUSDT"], 7)

    def test_evicted_session_restarts_empty_after_two_delayed_old_sessions(self):
        service = MovementBoundaryService(session_capacity=2)
        session_n = "3bf4e8d9-c888-4f69-8be3-29f47ebbd271"
        session_o1 = "4c05f9ea-d999-407a-9c04-30f58fccd382"
        session_o2 = "5d160afb-eaaa-418b-ad15-41a690dde493"
        n_first = request(
            BASE,
            [symbol_input("BTCUSDT", [observation(BASE, 1, "200")])],
            session_n,
        )
        o1_first = request(
            BASE,
            [symbol_input("BTCUSDT", [observation(BASE, 1, "300")])],
            session_o1,
        )
        o2_first = request(
            BASE,
            [symbol_input("BTCUSDT", [observation(BASE, 1, "400")])],
            session_o2,
        )
        service.advance(n_first)
        service.advance(o1_first)
        service.advance(o2_first)
        self.assertNotIn(session_n, service.sessions)
        self.assertEqual(
            service.sessions[session_o2]["engines"]["BTCUSDT"].history[0].price,
            Decimal("400"),
        )

        # N's next valid request is accepted as a fresh empty session, never a 409.
        n_next = request(
            BASE + 5_000,
            [symbol_input("BTCUSDT", [observation(BASE + 5_000, 2, "201")])],
            session_n,
        )
        n_result = service.advance(n_next)
        self.assertEqual(n_result["sessionId"], session_n)
        self.assertEqual(len(n_result["snapshots"][0]["buckets"]), 1)
        self.assertEqual(n_result["snapshots"][0]["readiness"][1]["state"], "warming")
        self.assertEqual(n_result["snapshots"][0]["buckets"][0]["endpointPrice"], 201.0)
        self.assertIn(session_n, service.sessions)
        self.assertNotIn(session_o1, service.sessions)
        self.assertEqual(
            service.sessions[session_n]["engines"]["BTCUSDT"].history[0].price,
            Decimal("201"),
        )
        self.assertEqual(
            service.sessions[session_o2]["engines"]["BTCUSDT"].history[0].price,
            Decimal("400"),
        )

    def test_small_response_cache_does_not_shrink_engine_history(self):
        service = MovementBoundaryService(cache_capacity=3, session_capacity=2)
        count = DEFAULT_HISTORY_BUCKETS + 5
        for index in range(1, count + 1):
            boundary = BASE + index * 5_000
            body = request(
                boundary,
                [symbol_input("BTCUSDT", [observation(boundary, boundary)])],
            )
            service.advance(body)

        session = service.sessions[SESSION]
        self.assertEqual(len(session["responses"]), 3)
        self.assertEqual(
            len(session["engines"]["BTCUSDT"].history),
            DEFAULT_HISTORY_BUCKETS,
        )

    def test_session_and_response_cache_capacities_must_be_positive_integers(self):
        for kwargs in (
            {"cache_capacity": 0},
            {"cache_capacity": True},
            {"session_capacity": 0},
            {"session_capacity": 1},
            {"session_capacity": 1.5},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    MovementBoundaryService(**kwargs)


class MovementMetricsAdapterTests(unittest.TestCase):
    SYMBOLS = ("BTCUSDT", "ETHUSDT", "ADAUSDT", "BNBUSDT", "SOLUSDT")

    @staticmethod
    def confirmed_episode_scope(**overrides):
        return {
            "direction": "BROAD_DROP",
            "universe_id": "watched",
            "universe_version": "watched-v1",
            "movement_algorithm_version": "market-movement-v1",
            "movement_config_version": MarketMovementConfig().version,
            "classifier_algorithm_version": ALGORITHM_VERSION,
            "classifier_config_version": MarketClassifierConfig().version,
            **overrides,
        }

    @classmethod
    def populated_service(cls, session_capacity=None, steps=360, rise_from_step=360):
        service = (MovementBoundaryService() if session_capacity is None else
                   MovementBoundaryService(session_capacity=session_capacity))
        service.advance(request(BASE, [symbol_input(symbol, [observation(BASE, index + 1)])
                                       for index, symbol in enumerate(cls.SYMBOLS)]))
        engines = service.sessions[SESSION]["engines"]
        for step in range(1, steps + 1):
            boundary = BASE + step * 5_000
            for index, symbol in enumerate(cls.SYMBOLS):
                price = Decimal("101") if step >= rise_from_step else Decimal("100")
                engine = engines[symbol]
                engine.observe((MarketObservation(
                    provider="binance-usdm", instrument_id=f"binance-usdm:{symbol}",
                    price_type="trade", price=price, quantity=Decimal("1"),
                    event_time_ms=boundary, trade_time_ms=boundary,
                    aggregate_trade_id=step * 10 + index,
                    received_at_ms=boundary,
                ),))
                engine.advance(boundary, "LIVE")
        return service

    @classmethod
    def history_request(cls, version="history-v1", close_delta=Decimal("0"),
                        compatible=True):
        candles = [{
            "open_time_ms": BASE - (65 - index) * 60_000,
            "close": str(Decimal("100") + Decimal(index % 7) / 100 +
                         Decimal(index) / 10000 +
                         (close_delta if index == 0 else Decimal("0"))),
            "volume": "1",
            "quote_volume": str(100 + index % 3),
        } for index in range(64)]
        return MovementHistoryRegistrationRequest.model_validate({
            "schema_version": 1, "session_id": SESSION,
            "history_version": version, "as_of_boundary_time_ms": BASE + 350 * 5_000,
            "universe_id": "watched",
            "universe_version": "watched-v1", "symbols": cls.SYMBOLS,
            "config": {**asdict(MarketMovementConfig()),
                       "historical_lookback_ms": 2 * 60 * 60_000,
                       "minimum_historical_coverage_ms": 30 * 60_000},
            "historical": [{
                "symbol": symbol,
                "instrument_compatible": compatible,
                "candles": candles,
            } for symbol in cls.SYMBOLS],
        })

    @staticmethod
    def metrics_request(boundary=BASE + 360 * 5_000, version="history-v1"):
        return MovementMetricsRequest.model_validate({
            "schema_version": 1, "session_id": SESSION,
            "evaluation_boundary_time_ms": boundary,
            "history_version": version, "universe_id": "watched",
            "universe_version": "watched-v1",
        })

    @classmethod
    def lifecycle_request(cls, boundary=BASE + 360 * 5_000, previous=None,
                          interrupt=False, version="history-v1", universe_version="watched-v1"):
        return MovementLifecycleRequest.model_validate({
            **cls.metrics_request(boundary, version).model_dump(mode="json"),
            "universe_version": universe_version,
            "previous_lifecycle_state": previous,
            "interrupt_previous_state": interrupt,
        })

    def test_canonical_engines_supply_metrics_without_mutating_history(self):
        service = self.populated_service()
        service.register_history(self.history_request())
        before = {symbol: engine.history for symbol, engine in
                  service.sessions[SESSION]["engines"].items()}
        result = service.calculate_metrics(self.metrics_request())
        evaluation = result["evaluation"]
        self.assertEqual(result["history_version"], "history-v1")
        self.assertEqual(evaluation["evaluation_boundary_time_ms"], BASE + 360 * 5_000)
        self.assertEqual(set(evaluation["windows"]), {"1", "5", "15"})
        self.assertEqual(evaluation["windows"]["1"]["eligible_count"], 5)
        self.assertTrue(evaluation["windows"]["1"]["market_wide_eligible"])
        self.assertGreater(evaluation["windows"]["1"]["symbols"][0]["current_return"]["value"], 0)
        self.assertEqual(before, {symbol: engine.history for symbol, engine in
                                  service.sessions[SESSION]["engines"].items()})
        # The service accepts no caller bucket/readiness input: only its engines
        # can produce the earlier finalized boundary's evidence.
        earlier = service.calculate_metrics(self.metrics_request(BASE + 355 * 5_000))
        self.assertEqual(earlier["evaluation"]["evaluation_boundary_time_ms"], BASE + 355 * 5_000)
        self.assertEqual(earlier["evaluation"]["windows"]["1"]["symbols"][0]["current_return"]["value"], 0)
        with self.assertRaisesRegex(ValueError, "predates registered history cutoff"):
            service.calculate_metrics(self.metrics_request(BASE + 349 * 5_000))

    def test_version_retries_identity_and_evicted_session_fail_closed(self):
        service = self.populated_service(session_capacity=2)
        self.assertEqual(service.register_history(self.history_request()),
                         service.register_history(self.history_request()))
        with self.assertRaises(ValueError):
            service.register_history(self.history_request(close_delta=Decimal("1")))
        with self.assertRaises(ValueError):
            service.calculate_metrics(self.metrics_request(version="wrong-version"))
        with self.assertRaises(ValueError):
            service.calculate_metrics(self.metrics_request(boundary=BASE + 365 * 5_000))
        unknown = MovementMetricsRequest.model_validate({
            **self.metrics_request().model_dump(mode="json"),
            "session_id": "4c05f9ea-d999-407a-9c04-30f58fccd382",
        })
        with self.assertRaises(ValueError):
            service.calculate_metrics(unknown)
        for session_id in ("4c05f9ea-d999-407a-9c04-30f58fccd382",
                           "5d160afb-eaaa-418b-ad15-41a690dde493"):
            service.advance(request(BASE, [symbol_input("BTCUSDT")], session_id))
        with self.assertRaises(ValueError):
            service.calculate_metrics(self.metrics_request())

    def test_new_membership_without_older_bucket_is_missing_symbol_input(self):
        service = self.populated_service()
        service.register_history(self.history_request())
        # This engine still exists, but joined after the requested old boundary.
        replacement = MovementBucketEngine("binance-usdm:SOLUSDT")
        replacement.advance(BASE + 360 * 5_000, "LIVE")
        service.sessions[SESSION]["engines"]["SOLUSDT"] = replacement
        result = service.calculate_metrics(self.metrics_request(BASE + 355 * 5_000))
        excluded = result["evaluation"]["windows"]["1"]["excluded_symbols"]
        self.assertIn({"symbol": "SOLUSDT", "reasons": ["MISSING_SYMBOL_INPUT"]}, excluded)

    def test_assessment_uses_same_boundary_and_exact_symbol_source_times(self):
        service = self.populated_service()
        service.register_history(self.history_request())
        request = MovementClassificationRequest.model_validate({
            **self.metrics_request().model_dump(mode="json"),
            "previous_confirmed_primary_episode": self.confirmed_episode_scope(),
        })
        first = service.calculate_assessment(request)
        self.assertEqual(first, service.calculate_assessment(request))
        self.assertEqual(first["evaluation"], service.calculate_metrics(self.metrics_request())["evaluation"])
        self.assertEqual((first["session_id"], first["history_version"]), (SESSION, "history-v1"))
        self.assertEqual(first["effective_previous_confirmed_primary_direction"], "BROAD_DROP")
        for window in ("1", "5", "15"):
            classification = first["classification"]["windows"][window]
            self.assertEqual(classification["movement_snapshot"], first["evaluation"]["windows"][window])
            self.assertEqual(classification["evaluation_boundary_time_ms"], BASE + 360 * 5_000)
            self.assertEqual([item["symbol"] for item in classification["source_time_evidence"]],
                             list(self.SYMBOLS))
            for item in classification["source_time_evidence"]:
                bucket = next(bucket for bucket in service.sessions[SESSION]["engines"][item["symbol"]].history
                              if bucket.boundary_time_ms == BASE + 360 * 5_000)
                self.assertEqual((item["last_real_trade_time_ms"], item["last_real_event_time_ms"],
                                  item["last_received_at_ms"]),
                                 (bucket.last_real_trade_time_ms, bucket.last_real_event_time_ms,
                                  bucket.last_received_at_ms))
            self.assertEqual(classification["prior_confirmed_episode_direction"],
                             "BROAD_DROP" if window == "5" else None)
        primary = first["classification"]["windows"]["5"]
        self.assertEqual(primary["direction_state"], "BROAD_RISE")
        self.assertEqual(primary["reversal_candidate"]["prior_confirmed_episode_direction"],
                         "BROAD_DROP")

    def test_assessment_ignores_prior_episode_outside_current_scope(self):
        service = self.populated_service()
        service.register_history(self.history_request())
        mismatches = {
            "universe_id": "other-universe",
            "universe_version": "other-universe-version",
            "movement_algorithm_version": "other-movement-algorithm",
            "movement_config_version": "other-movement-config",
            "classifier_algorithm_version": "other-classifier-algorithm",
            "classifier_config_version": "other-classifier-config",
        }
        for field, wrong_value in mismatches.items():
            with self.subTest(field=field):
                request = MovementClassificationRequest.model_validate({
                    **self.metrics_request().model_dump(mode="json"),
                    "previous_confirmed_primary_episode": self.confirmed_episode_scope(
                        **{field: wrong_value}),
                })
                result = service.calculate_assessment(request)
                self.assertIsNone(result["effective_previous_confirmed_primary_direction"])
                self.assertIsNone(result["classification"]["windows"]["5"]
                                  ["prior_confirmed_episode_direction"])
                self.assertIsNone(result["classification"]["windows"]["5"]
                                  ["reversal_candidate"])
                self.assertEqual(result["classification"]["windows"]["5"]
                                 ["direction_state"], "BROAD_RISE")

    def test_assessment_keeps_missing_exact_symbol_bucket_as_three_none_times(self):
        service = self.populated_service()
        service.register_history(self.history_request())
        replacement = MovementBucketEngine("binance-usdm:SOLUSDT")
        replacement.advance(BASE + 360 * 5_000, "LIVE")
        service.sessions[SESSION]["engines"]["SOLUSDT"] = replacement
        request = MovementClassificationRequest.model_validate({
            **self.metrics_request(BASE + 355 * 5_000).model_dump(mode="json"),
            "previous_confirmed_primary_episode": None,
        })
        result = service.calculate_assessment(request)
        for window in ("1", "5", "15"):
            evidence = result["classification"]["windows"][window]["source_time_evidence"]
            self.assertEqual(evidence[-1], {"symbol": "SOLUSDT",
                                            "last_real_trade_time_ms": None,
                                            "last_real_event_time_ms": None,
                                            "last_received_at_ms": None})

    def test_lifecycle_reuses_metrics_classification_and_canonical_state(self):
        service = self.populated_service(steps=361, rise_from_step=359)
        service.register_history(self.history_request())
        first_boundary = BASE + 359 * 5_000
        first = service.calculate_lifecycle(self.lifecycle_request(first_boundary))
        self.assertEqual(first["evaluation"], service.calculate_metrics(
            self.metrics_request(first_boundary))["evaluation"])
        self.assertEqual(first["classification"], service.calculate_assessment(
            MovementClassificationRequest.model_validate({
                **self.metrics_request(first_boundary).model_dump(mode="json"),
                "previous_confirmed_primary_episode": None,
            }))["classification"])
        self.assertEqual(first["lifecycle"]["transitions"], [])
        second = service.calculate_lifecycle(self.lifecycle_request(
            previous=first["lifecycle"]["serialized_state"]))
        self.assertEqual([event["transition"] for event in second["lifecycle"]["transitions"]],
                         ["STARTED"])
        serialized = second["lifecycle"]["serialized_state"]
        self.assertEqual(serialized, serialize_market_episode_lifecycle_state(
            deserialize_market_episode_lifecycle_state(serialized)))
        self.assertEqual(second["lifecycle"]["state_summary"]["active_episode_id"],
                         second["lifecycle"]["transitions"][0]["episode_id"])
        third = service.calculate_lifecycle(self.lifecycle_request(
            BASE + 361 * 5_000, previous=serialized))
        self.assertEqual(third["classification"]["windows"]["5"]
                         ["prior_confirmed_episode_direction"], "BROAD_RISE")
        self.assertEqual(third["lifecycle"]["transitions"], [])

    def test_lifecycle_restart_and_prior_scope_are_owned_by_python(self):
        service = self.populated_service(steps=361, rise_from_step=359)
        service.register_history(self.history_request())
        first = service.calculate_lifecycle(self.lifecycle_request(BASE + 359 * 5_000))
        active = service.calculate_lifecycle(self.lifecycle_request(
            previous=first["lifecycle"]["serialized_state"]))
        serialized = active["lifecycle"]["serialized_state"]
        restarted = service.calculate_lifecycle(self.lifecycle_request(
            BASE + 361 * 5_000, previous=serialized, interrupt=True))
        self.assertTrue(restarted["lifecycle"]["state_summary"]["interrupted"])
        self.assertEqual(restarted["lifecycle"]["serialized_state"]["pending_resume"]["count"], 1)
        self.assertEqual(restarted["lifecycle"]["transitions"], [])

        changed_history = self.history_request(version="history-v2").model_dump(mode="json")
        changed_history["universe_version"] = "watched-v2"
        service.register_history(MovementHistoryRegistrationRequest.model_validate(changed_history))
        changed = service.calculate_lifecycle(self.lifecycle_request(
            BASE + 361 * 5_000, previous=serialized, version="history-v2",
            universe_version="watched-v2"))
        self.assertIsNone(changed["classification"]["windows"]["5"]
                          ["prior_confirmed_episode_direction"])
        self.assertEqual(changed["lifecycle"]["transitions"][0]["transition"], "ENDED")
        with self.assertRaises(ValueError):
            service.calculate_lifecycle(self.lifecycle_request(
                BASE + 361 * 5_000, previous={"serialization_version": "market-episode-state-v1"}))

    def test_factual_instrument_compatibility_and_unknown_metadata(self):
        service = self.populated_service()
        service.register_history(self.history_request(compatible=False))
        excluded = service.calculate_metrics(self.metrics_request())["evaluation"]["windows"]["1"]["excluded_symbols"]
        self.assertIn("UNSUPPORTED_INSTRUMENT", excluded[0]["reasons"])
        service.register_history(self.history_request(version="history-v2", compatible=None))
        excluded = service.calculate_metrics(self.metrics_request(version="history-v2"))["evaluation"]["windows"]["1"]["excluded_symbols"]
        self.assertIn("SOURCE_UNAVAILABLE", excluded[0]["reasons"])
        self.assertNotIn("UNSUPPORTED_INSTRUMENT", excluded[0]["reasons"])

    def test_replacing_raw_history_version_preserves_same_boundary_result(self):
        service = self.populated_service()
        service.register_history(self.history_request())
        first = service.calculate_metrics(self.metrics_request())["evaluation"]
        service.register_history(self.history_request(version="history-v2"))
        second = service.calculate_metrics(self.metrics_request(version="history-v2"))["evaluation"]
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
