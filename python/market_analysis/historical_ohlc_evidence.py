"""Immutable Binance USD-M trade-price OHLC evidence for replay research.

The collection is auxiliary to canonical movement replay. Its first-seen time
is an archive exchange-close surrogate, not a historical socket receipt.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import json
from types import MappingProxyType
from typing import Mapping

from .movement import BINANCE_USDM, TRADE_PRICE
from .movement_history import MINUTE_MS
from .movement_metrics import EXCHANGE


OHLC_EVIDENCE_VERSION = "binance-usdm-trade-ohlc-evidence-v1"
OHLC_AVAILABILITY_BASIS = "exchange-close-time-surrogate"
OHLC_INTERVAL = "1m"
NO_AVAILABLE_CANDLE = "NO_AVAILABLE_CANDLE"
MISSING_PRECEDING_MINUTE = "MISSING_PRECEDING_MINUTE"
PRECEDING_NOT_YET_AVAILABLE = "PRECEDING_NOT_YET_AVAILABLE"
_MAX_SAFE_TIMESTAMP = 9_007_199_254_740_991
_INFINITY = _MAX_SAFE_TIMESTAMP + 1


def _timestamp(value, name):
    if type(value) is not int or not 0 <= value <= _MAX_SAFE_TIMESTAMP:
        raise ValueError(f"{name} must be a nonnegative safe integer timestamp")
    return value


def _digest(value):
    return (isinstance(value, str) and len(value) == 64
            and all(character in "0123456789abcdef" for character in value))


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


@dataclass(frozen=True)
class CompletedTradeOHLCCandle:
    symbol: str
    instrument_id: str
    open_time_ms: int
    close_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    first_seen_at_ms: int
    interval: str = OHLC_INTERVAL
    provider: str = BINANCE_USDM
    exchange: str = EXCHANGE
    price_type: str = TRADE_PRICE
    finalized: bool = True
    availability_basis: str = OHLC_AVAILABILITY_BASIS

    def __post_init__(self):
        if (not isinstance(self.symbol, str) or not self.symbol.isascii()
                or not self.symbol.isalnum() or self.symbol != self.symbol.upper()
                or self.instrument_id != f"{BINANCE_USDM}:{self.symbol}"):
            raise ValueError("OHLC candle requires a Binance USD-M instrument identity")
        opening = _timestamp(self.open_time_ms, "OHLC open")
        closing = _timestamp(self.close_time_ms, "OHLC close")
        first_seen = _timestamp(self.first_seen_at_ms, "OHLC first seen")
        if (self.interval != OHLC_INTERVAL or opening % MINUTE_MS
                or closing != opening + MINUTE_MS - 1
                or first_seen < closing + 1 or self.finalized is not True):
            raise ValueError("OHLC candle must be a finalized, aligned 1m interval")
        if ((self.provider, self.exchange, self.price_type, self.availability_basis)
                != (BINANCE_USDM, EXCHANGE, TRADE_PRICE, OHLC_AVAILABILITY_BASIS)):
            raise ValueError("OHLC candle provenance or availability basis is invalid")
        for name in ("open", "high", "low", "close"):
            price = getattr(self, name)
            if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
                raise ValueError(f"OHLC {name} must be a finite positive Decimal")
        if not (self.low <= self.open <= self.high
                and self.low <= self.close <= self.high):
            raise ValueError("OHLC prices are inconsistent")


@dataclass(frozen=True)
class TradeOHLCCandleWithPreviousClose:
    candle: CompletedTradeOHLCCandle
    previous_close: Decimal | None
    previous_close_unavailable_reason: str | None

    def __post_init__(self):
        if (self.previous_close is None) == (self.previous_close_unavailable_reason is None):
            raise ValueError("previous close requires either a value or an unavailable reason")
        if (self.previous_close_unavailable_reason is not None
                and self.previous_close_unavailable_reason not in (
                    MISSING_PRECEDING_MINUTE, PRECEDING_NOT_YET_AVAILABLE)):
            raise ValueError("invalid previous-close unavailable reason")


@dataclass(frozen=True)
class TradeOHLCAsOfResult:
    dataset_content_sha256: str
    evidence_sha256: str
    symbol: str
    interval: str
    evaluation_boundary_time_ms: int
    candles: tuple[TradeOHLCCandleWithPreviousClose, ...]
    no_candle_reason: str | None
    latest_open_time_ms: int | None
    latest_close_time_ms: int | None
    latest_first_seen_at_ms: int | None


@dataclass(frozen=True)
class _SymbolIndex:
    candles: tuple[CompletedTradeOHLCCandle, ...]
    opens: tuple[int, ...]
    tree_size: int
    min_first_seen: tuple[int, ...]

    @classmethod
    def build(cls, candles):
        ordered = tuple(candles)
        size = 1
        while size < len(ordered):
            size *= 2
        tree = [_INFINITY] * (size * 2)
        for index, candle in enumerate(ordered):
            tree[size + index] = candle.first_seen_at_ms
        for index in range(size - 1, 0, -1):
            tree[index] = min(tree[index * 2], tree[index * 2 + 1])
        return cls(ordered, tuple(item.open_time_ms for item in ordered),
                   size, tuple(tree))

    def visible_indices(self, boundary: int, limit: int | None) -> tuple[int, ...]:
        """Find latest eligible opens without scanning all unavailable rows."""
        end_index = bisect_right(self.opens, boundary - MINUTE_MS)
        found = []

        def visit(node: int, start: int, end: int) -> None:
            if (start >= end_index or self.min_first_seen[node] > boundary
                    or limit is not None and len(found) >= limit):
                return
            if end - start == 1:
                candle = self.candles[start]
                if candle.close_time_ms < boundary and candle.first_seen_at_ms <= boundary:
                    found.append(start)
                return
            middle = (start + end) // 2
            visit(node * 2 + 1, middle, end)
            visit(node * 2, start, middle)

        visit(1, 0, self.tree_size)
        found.reverse()
        return tuple(found)


@dataclass(frozen=True)
class BinanceTradeOHLCEvidence:
    dataset_id: str
    dataset_version: str
    dataset_content_sha256: str
    configured_symbols: tuple[str, ...]
    candles: tuple[CompletedTradeOHLCCandle, ...]
    evidence_version: str = OHLC_EVIDENCE_VERSION
    evidence_sha256: str = field(init=False)
    _indexes: Mapping[str, _SymbolIndex] = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        symbols = tuple(self.configured_symbols)
        if (not isinstance(self.dataset_id, str) or not self.dataset_id
                or not isinstance(self.dataset_version, str) or not self.dataset_version
                or not _digest(self.dataset_content_sha256)
                or not symbols or any(
                    not isinstance(symbol, str) or not symbol.isascii()
                    or not symbol.isalnum() or symbol != symbol.upper()
                    for symbol in symbols)
                or len(set(symbols)) != len(symbols)):
            raise ValueError("OHLC evidence requires verified dataset and universe identity")
        if self.evidence_version != OHLC_EVIDENCE_VERSION:
            raise ValueError("unsupported OHLC evidence version")
        by_key = {}
        for candle in self.candles:
            if not isinstance(candle, CompletedTradeOHLCCandle) or candle.symbol not in symbols:
                raise ValueError("OHLC candle is outside the configured universe")
            key = (candle.symbol, candle.open_time_ms)
            previous = by_key.get(key)
            if previous is not None and previous != candle:
                raise ValueError(f"conflicting OHLC candle for {candle.symbol} {candle.open_time_ms}")
            by_key[key] = candle
        ordered = tuple(by_key[key] for key in sorted(by_key))
        grouped = {symbol: [] for symbol in symbols}
        for candle in ordered:
            grouped[candle.symbol].append(candle)
        indexes = MappingProxyType({symbol: _SymbolIndex.build(grouped[symbol])
                                    for symbol in symbols})
        payload = {
            "evidence_version": self.evidence_version,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "dataset_content_sha256": self.dataset_content_sha256,
            "provider": BINANCE_USDM,
            "exchange": EXCHANGE,
            "price_type": TRADE_PRICE,
            "interval": OHLC_INTERVAL,
            "availability_basis": OHLC_AVAILABILITY_BASIS,
            "configured_symbols": sorted(symbols),
            "candles": [[item.symbol, item.instrument_id,
                          item.open_time_ms, item.close_time_ms,
                          _decimal_text(item.open), _decimal_text(item.high),
                          _decimal_text(item.low), _decimal_text(item.close),
                          item.first_seen_at_ms, item.finalized]
                         for item in ordered],
        }
        digest = hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True).encode("ascii")).hexdigest()
        object.__setattr__(self, "configured_symbols", symbols)
        object.__setattr__(self, "candles", ordered)
        object.__setattr__(self, "_indexes", indexes)
        object.__setattr__(self, "evidence_sha256", digest)

    def as_of(self, symbol: str, interval: str,
              evaluation_boundary_time_ms: int, *,
              limit: int | None = None) -> TradeOHLCAsOfResult:
        """Return causally available 1m candles in ascending open-time order."""
        if symbol not in self._indexes or interval != OHLC_INTERVAL:
            raise ValueError("OHLC as-of requires a configured symbol and 1m interval")
        boundary = _timestamp(evaluation_boundary_time_ms, "OHLC evaluation boundary")
        if boundary % 5_000:
            raise ValueError("OHLC evaluation boundary must align to five seconds")
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError("OHLC limit must be a positive integer")
        index = self._indexes[symbol]
        rows = []
        for position in index.visible_indices(boundary, limit):
            candle = index.candles[position]
            prior = index.candles[position - 1] if position else None
            if prior is None or prior.open_time_ms != candle.open_time_ms - MINUTE_MS:
                rows.append(TradeOHLCCandleWithPreviousClose(
                    candle, None, MISSING_PRECEDING_MINUTE))
            elif prior.close_time_ms >= boundary or prior.first_seen_at_ms > boundary:
                rows.append(TradeOHLCCandleWithPreviousClose(
                    candle, None, PRECEDING_NOT_YET_AVAILABLE))
            else:
                rows.append(TradeOHLCCandleWithPreviousClose(candle, prior.close, None))
        latest = rows[-1].candle if rows else None
        return TradeOHLCAsOfResult(
            self.dataset_content_sha256, self.evidence_sha256, symbol,
            interval, boundary, tuple(rows),
            None if rows else NO_AVAILABLE_CANDLE,
            latest.open_time_ms if latest is not None else None,
            latest.close_time_ms if latest is not None else None,
            latest.first_seen_at_ms if latest is not None else None,
        )
