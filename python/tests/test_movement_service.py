import unittest
from dataclasses import asdict
from decimal import Decimal
from types import SimpleNamespace

from market_analysis.api_models import (MovementBoundaryRequest,
                                        MovementHistoryRegistrationRequest,
                                        MovementMetricsRequest)
from market_analysis.movement import (DEFAULT_HISTORY_BUCKETS, MarketObservation,
                                      MovementBucketEngine)
from market_analysis.movement_metrics import MarketMovementConfig
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

    @classmethod
    def populated_service(cls):
        service = MovementBoundaryService()
        service.advance(request(BASE, [symbol_input(symbol, [observation(BASE, index + 1)])
                                       for index, symbol in enumerate(cls.SYMBOLS)]))
        engines = service.sessions[SESSION]["engines"]
        for step in range(1, 361):
            boundary = BASE + step * 5_000
            for index, symbol in enumerate(cls.SYMBOLS):
                price = Decimal("101") if step == 360 else Decimal("100")
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
    def history_request(cls, version="history-v1", returns=None):
        return MovementHistoryRegistrationRequest.model_validate({
            "schema_version": 1, "session_id": SESSION,
            "history_version": version, "universe_id": "watched",
            "universe_version": "watched-v1", "symbols": cls.SYMBOLS,
            "config": asdict(MarketMovementConfig()),
            "historical": [{
                "symbol": symbol,
                "windows": {str(window): {
                    "returns": returns if returns is not None else [-0.02, -0.01, 0.01, 0.02],
                    "usable_coverage_ms": 3 * 24 * 60 * 60 * 1000,
                    "previous_notional_volumes": ["1200"] * 20,
                } for window in (1, 5, 15)},
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

    def test_version_retries_identity_and_evicted_session_fail_closed(self):
        service = self.populated_service()
        self.assertEqual(service.register_history(self.history_request()),
                         service.register_history(self.history_request()))
        with self.assertRaises(ValueError):
            service.register_history(self.history_request(returns=[-0.1, -0.01, 0.01, 0.1]))
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


if __name__ == "__main__":
    unittest.main()
