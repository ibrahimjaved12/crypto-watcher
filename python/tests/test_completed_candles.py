"""Small deterministic #91 contract fixtures; no providers, clocks or storage."""
from dataclasses import fields, replace
from decimal import Decimal
import unittest

from market_analysis.completed_candles import (
    COMPLETED_CANDLE_CONTRACT_VERSION, ArchiveCandleProvenance, CompletedCandle,
    CompletedCandleObservation, CompletedCandleSeries, CompletedCandleSeriesIdentity,
    RestCandleProvenance, WebSocketCandleProvenance,
)


def identity():
    return CompletedCandleSeriesIdentity("binance-usdm", "binance", "futures", "perpetual",
        "binance-usdm:BTCUSDT", "BTCUSDT", "BTCUSDT", "trade", "native-kline", 1)


def candle(opening=60_000):
    return CompletedCandle(identity(), opening, opening + 59_999,
        Decimal("100"), Decimal("102"), Decimal("99"), Decimal("101"), Decimal("2"), Decimal("202.25"))


class CompletedCandleTests(unittest.TestCase):
    def test_market_fact_interval_and_distinct_observations(self):
        fact = candle()
        self.assertEqual(fact.end_time_exclusive_ms, 120_000)
        provenance = (RestCandleProvenance("/fapi/v1/klines", 120_100),
            WebSocketCandleProvenance("wss://stream", 119_997, 120_120),
            ArchiveCandleProvenance("dataset", "v1", "a" * 64))
        observations = tuple(CompletedCandleObservation(fact, item) for item in provenance)
        self.assertTrue(all(item.candle == fact for item in observations))
        self.assertNotEqual(observations[0], observations[1])
        series = CompletedCandleSeries(COMPLETED_CANDLE_CONTRACT_VERSION, identity(), observations)
        self.assertEqual(series.market_candles, (fact,))
        self.assertFalse(hasattr(fact, "complete"))
        self.assertFalse(hasattr(fact, "is_completed"))
        self.assertEqual(provenance[1].source_event_time_ms, 119_997)
        self.assertFalse(hasattr(provenance[0], "source_event_time_ms"))
        for name in ("source_event_time_ms", "received_at_ms", "retrieved_at_ms"):
            self.assertFalse(hasattr(provenance[2], name))

    def test_invalid_market_facts(self):
        for changes in ({"open_time_ms": 60_001}, {"close_time_ms": 120_000},
                {"open_time_ms": True}, {"close_time_ms": 9_007_199_254_740_992},
                {"open": Decimal("0")}, {"high": Decimal("Infinity")},
                {"low": Decimal("NaN")}, {"close": 101.0}, {"high": Decimal("100")},
                {"low": Decimal("102")}, {"base_volume": Decimal("-1")},
                {"quote_volume": Decimal("-1")}, {"quote_volume": Decimal("Infinity")}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(candle(), **changes)

    def test_strict_series_identity_without_watchlist(self):
        for name, value in (("provider", "other"), ("exchange", "okx"),
                ("market_type", "spot"), ("contract_type", "dated"), ("price_type", "mark"),
                ("series_basis", "aggtrade"), ("timeframe_minutes", 15), ("timeframe_minutes", True),
                ("instrument_id", "BTCUSDT"), ("symbol", "ETHUSDT"), ("native_symbol", "btcusdt")):
            with self.subTest(name=name), self.assertRaises(ValueError):
                replace(identity(), **{name: value})
        other = replace(identity(), symbol="1000PEPEUSDT", native_symbol="1000PEPEUSDT",
                        instrument_id="binance-usdm:1000PEPEUSDT")
        self.assertEqual(other.symbol, "1000PEPEUSDT")

    def test_provenance_fields_and_required_timestamps(self):
        with self.assertRaises(TypeError):
            WebSocketCandleProvenance("ws", 1)
        with self.assertRaises(ValueError):
            WebSocketCandleProvenance("ws", None, 1)
        with self.assertRaises(TypeError):
            RestCandleProvenance("rest", 1, source_event_time_ms=1)
        with self.assertRaises(TypeError):
            ArchiveCandleProvenance("d", "v", "a" * 64, received_at_ms=1)
        with self.assertRaises(ValueError):
            ArchiveCandleProvenance("d", "v", "bad")
        with self.assertRaises(ValueError):
            RestCandleProvenance("", 1)
        self.assertEqual([item.name for item in fields(RestCandleProvenance)],
                         ["endpoint", "retrieved_at_ms"])

    def test_conflicts_order_gaps_and_empty_series(self):
        provenance = RestCandleProvenance("rest", 1)
        obs = lambda fact: CompletedCandleObservation(fact, provenance)
        series = CompletedCandleSeries(COMPLETED_CANDLE_CONTRACT_VERSION, identity(),
            (obs(candle(180_000)), obs(candle()), obs(candle())))
        self.assertEqual(tuple(item.open_time_ms for item in series.market_candles), (60_000, 180_000))
        self.assertEqual(series.missing_open_times_ms, (120_000,))
        with self.assertRaises(ValueError):
            CompletedCandleSeries(COMPLETED_CANDLE_CONTRACT_VERSION, identity(),
                                 (obs(candle()), obs(replace(candle(), quote_volume=Decimal("999")))))
        with self.assertRaises(ValueError):
            CompletedCandleSeries("v2", identity(), ())
        empty = CompletedCandleSeries(COMPLETED_CANDLE_CONTRACT_VERSION, identity(), ())
        self.assertEqual(empty.market_candles, ())
        self.assertEqual(empty.missing_open_times_ms, ())
