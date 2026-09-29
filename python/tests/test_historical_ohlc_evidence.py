"""Causal as-of fixtures for auxiliary Binance USD-M trade-price OHLC."""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import unittest

from market_analysis.historical_ohlc_evidence import (
    BinanceTradeOHLCEvidence, CompletedTradeOHLCCandle,
    MISSING_PRECEDING_MINUTE, NO_AVAILABLE_CANDLE,
    OHLC_AVAILABILITY_BASIS, OHLC_EVIDENCE_VERSION,
    PRECEDING_NOT_YET_AVAILABLE,
)


BOUNDARY = int(datetime(2026, 8, 20, 10, 5, tzinfo=timezone.utc).timestamp() * 1000)
MINUTE = 60_000
DATASET_SHA = "a" * 64


def _candle(minutes_before, close, *, first_seen=None):
    opening = BOUNDARY - minutes_before * MINUTE
    return CompletedTradeOHLCCandle(
        "BTCUSDT", "binance-usdm:BTCUSDT", opening, opening + MINUTE - 1,
        Decimal("100"), Decimal("105"), Decimal("95"), Decimal(str(close)),
        opening + MINUTE if first_seen is None else first_seen)


def _collection(candles):
    return BinanceTradeOHLCEvidence(
        "binance-public-data-usdm", "binance-usdm-daily-archive-v1",
        DATASET_SHA, ("BTCUSDT",), tuple(candles))


class HistoricalOHLCEvidenceTests(unittest.TestCase):
    def test_completion_late_first_seen_gaps_and_previous_close(self):
        old = _candle(3, "100")           # 10:02–10:03
        late = _candle(2, "101", first_seen=BOUNDARY + 1_000)  # 10:03–10:04
        current = _candle(1, "102")       # 10:04–10:05
        newer = _candle(-1, "103")        # 10:06–10:07; no 10:05 candle
        evidence = _collection((newer, current, late, old))

        empty = evidence.as_of("BTCUSDT", "1m", BOUNDARY - 2 * MINUTE - 5_000)
        self.assertEqual(empty.candles, ())
        self.assertEqual(empty.no_candle_reason, NO_AVAILABLE_CANDLE)
        self.assertIsNone(empty.latest_close_time_ms)

        before = evidence.as_of("BTCUSDT", "1m", BOUNDARY - 5_000)
        self.assertEqual(tuple(row.candle.open_time_ms for row in before.candles),
                         (old.open_time_ms,))
        self.assertEqual(before.candles[0].previous_close_unavailable_reason,
                         MISSING_PRECEDING_MINUTE)

        at = evidence.as_of("BTCUSDT", "1m", BOUNDARY)
        self.assertEqual(tuple(row.candle.open_time_ms for row in at.candles),
                         (old.open_time_ms, current.open_time_ms))
        self.assertEqual(at.candles[-1].previous_close_unavailable_reason,
                         PRECEDING_NOT_YET_AVAILABLE)
        self.assertIsNone(at.candles[-1].previous_close)
        self.assertEqual(at.latest_first_seen_at_ms, BOUNDARY)
        self.assertEqual(at.dataset_content_sha256, DATASET_SHA)
        self.assertEqual(at.evidence_sha256, evidence.evidence_sha256)

        later = evidence.as_of("BTCUSDT", "1m", BOUNDARY + 5_000)
        self.assertEqual(tuple(row.candle.open_time_ms for row in later.candles),
                         (old.open_time_ms, late.open_time_ms,
                          current.open_time_ms))
        self.assertEqual(later.candles[-1].previous_close, late.close)
        self.assertIsNone(later.candles[-1].previous_close_unavailable_reason)
        self.assertEqual(evidence.as_of("BTCUSDT", "1m", BOUNDARY), at)

        recent = evidence.as_of("BTCUSDT", "1m", BOUNDARY + 5_000, limit=2)
        self.assertEqual(tuple(row.candle.open_time_ms for row in recent.candles),
                         (late.open_time_ms, current.open_time_ms))
        self.assertEqual(recent.candles[0].previous_close, old.close)

        much_later = evidence.as_of("BTCUSDT", "1m", BOUNDARY + 15 * MINUTE)
        self.assertEqual(much_later.candles[-1].candle, newer)
        self.assertEqual(much_later.candles[-1].previous_close_unavailable_reason,
                         MISSING_PRECEDING_MINUTE)
        self.assertEqual(much_later.latest_open_time_ms, newer.open_time_ms)
        self.assertEqual(much_later.latest_close_time_ms, newer.close_time_ms)
        self.assertIsNone(much_later.no_candle_reason)  # no invented stale threshold

    def test_deterministic_order_digest_and_query_validation(self):
        older, newer = _candle(3, "100"), _candle(1, "102")
        first = _collection((older, newer))
        reverse_with_duplicate = _collection((newer, older, older))
        self.assertEqual(first, reverse_with_duplicate)
        self.assertEqual(first.candles, (older, newer))
        self.assertEqual(first.evidence_sha256, reverse_with_duplicate.evidence_sha256)
        scaled_duplicate = replace(older, open=Decimal("100.00"),
                                   close=Decimal("100.0"))
        self.assertEqual(_collection((scaled_duplicate, older, newer)).evidence_sha256,
                         _collection((older, scaled_duplicate, newer)).evidence_sha256)
        self.assertEqual(first.evidence_version, OHLC_EVIDENCE_VERSION)
        self.assertEqual(older.availability_basis, OHLC_AVAILABILITY_BASIS)
        changed = _collection((older, replace(newer, high=Decimal("106"))))
        self.assertNotEqual(first.evidence_sha256, changed.evidence_sha256)
        for symbol, interval, boundary, limit in (
            ("ETHUSDT", "1m", BOUNDARY, None),
            ("BTCUSDT", "5m", BOUNDARY, None),
            ("BTCUSDT", "1m", BOUNDARY + 1_000, None),
            ("BTCUSDT", "1m", BOUNDARY, 0),
        ):
            with self.subTest(symbol=symbol, interval=interval,
                              boundary=boundary, limit=limit):
                with self.assertRaises(ValueError):
                    first.as_of(symbol, interval, boundary, limit=limit)

    def test_invalid_provenance_close_and_conflicting_duplicate(self):
        candle = _candle(1, "102")
        changes = (
            {"instrument_id": "binance-usdm:ETHUSDT"},
            {"provider": "other"}, {"exchange": "other"},
            {"price_type": "mark"}, {"interval": "5m"},
            {"availability_basis": "socket-receipt"},
            {"finalized": False}, {"first_seen_at_ms": BOUNDARY - 1},
            {"close": Decimal("106")}, {"open": 100},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(candle, **change)
        with self.assertRaisesRegex(ValueError, "conflicting OHLC"):
            _collection((candle, replace(candle, close=Decimal("103"))))
        with self.assertRaises(ValueError):
            BinanceTradeOHLCEvidence(
                "binance-public-data-usdm", "binance-usdm-daily-archive-v1",
                "A" * 64, ("BTCUSDT",), (candle,))


if __name__ == "__main__":
    unittest.main()
