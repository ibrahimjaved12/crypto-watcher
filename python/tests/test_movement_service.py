import unittest
from types import SimpleNamespace

from market_analysis.api_models import MovementBoundaryRequest
from market_analysis.movement import MovementBucketEngine
from market_analysis.movement_service import MovementBoundaryService

BASE = 1_800_000_000_000
SESSION = "2af3e7c8-b777-4e58-9ad2-18e36daac160"


def request(boundary, symbols):
    return MovementBoundaryRequest.model_validate({
        "schema_version": 1,
        "session_id": SESSION,
        "boundary_time_ms": boundary,
        "symbols": symbols,
    })


def symbol_input(symbol, observations=(), source_state="LIVE"):
    return {
        "symbol": symbol,
        "instrument_id": f"binance-usdm:{symbol}",
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


if __name__ == "__main__":
    unittest.main()
