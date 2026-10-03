"""Pure native completed-candle market facts and factual observation provenance."""
from dataclasses import dataclass
from decimal import Decimal
import re
from typing import ClassVar

COMPLETED_CANDLE_CONTRACT_VERSION = "completed-candle-v1"
MAX_SAFE_INTEGER = 9_007_199_254_740_991


def _timestamp(value):
    if type(value) is not int or not 0 <= value <= MAX_SAFE_INTEGER:
        raise ValueError("timestamp must be a nonnegative safe integer millisecond")


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("provenance identity/endpoint must be nonempty")


@dataclass(frozen=True)
class CompletedCandleSeriesIdentity:
    provider: str
    exchange: str
    market_type: str
    contract_type: str
    instrument_id: str
    symbol: str
    native_symbol: str
    price_type: str
    series_basis: str
    timeframe_minutes: int

    def __post_init__(self):
        if ((self.provider, self.exchange, self.market_type, self.contract_type,
             self.price_type, self.series_basis) !=
                ("binance-usdm", "binance", "futures", "perpetual", "trade", "native-kline")
                or type(self.timeframe_minutes) is not int or self.timeframe_minutes != 1
                or not isinstance(self.native_symbol, str)
                or re.fullmatch(r"[A-Z0-9]+USDT", self.native_symbol) is None
                or self.symbol != self.native_symbol
                or self.instrument_id != f"binance-usdm:{self.native_symbol}"):
            raise ValueError("unsupported completed-candle series identity")


@dataclass(frozen=True)
class CompletedCandle:
    identity: CompletedCandleSeriesIdentity
    open_time_ms: int
    close_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    base_volume: Decimal
    quote_volume: Decimal

    def __post_init__(self):
        if not isinstance(self.identity, CompletedCandleSeriesIdentity):
            raise ValueError("invalid completed-candle identity")
        _timestamp(self.open_time_ms)
        _timestamp(self.close_time_ms)
        _timestamp(self.end_time_exclusive_ms)
        if self.open_time_ms % 60_000 or self.close_time_ms != self.open_time_ms + 59_999:
            raise ValueError("invalid native one-minute interval")
        for value in (self.open, self.high, self.low, self.close):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError("OHLC must be finite positive Decimal values")
        for value in (self.base_volume, self.quote_volume):
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError("volume must be finite nonnegative Decimal values")
        if not self.low <= self.open <= self.high or not self.low <= self.close <= self.high:
            raise ValueError("inconsistent OHLC range")

    @property
    def end_time_exclusive_ms(self):
        return self.close_time_ms + 1


@dataclass(frozen=True)
class WebSocketCandleProvenance:
    endpoint: str
    source_event_time_ms: int
    received_at_ms: int
    source_kind: ClassVar[str] = "websocket"

    def __post_init__(self):
        _text(self.endpoint)
        _timestamp(self.source_event_time_ms)
        _timestamp(self.received_at_ms)


@dataclass(frozen=True)
class RestCandleProvenance:
    endpoint: str
    retrieved_at_ms: int
    source_kind: ClassVar[str] = "rest"

    def __post_init__(self):
        _text(self.endpoint)
        _timestamp(self.retrieved_at_ms)


@dataclass(frozen=True)
class ArchiveCandleProvenance:
    dataset_id: str
    dataset_version: str
    dataset_content_sha256: str
    source_kind: ClassVar[str] = "archive"

    def __post_init__(self):
        _text(self.dataset_id)
        _text(self.dataset_version)
        if not isinstance(self.dataset_content_sha256, str) or re.fullmatch(
                r"[0-9a-f]{64}", self.dataset_content_sha256) is None:
            raise ValueError("invalid archive dataset SHA")


@dataclass(frozen=True)
class CompletedCandleObservation:
    candle: CompletedCandle
    provenance: WebSocketCandleProvenance | RestCandleProvenance | ArchiveCandleProvenance

    def __post_init__(self):
        if not isinstance(self.candle, CompletedCandle) or not isinstance(self.provenance,
                (WebSocketCandleProvenance, RestCandleProvenance, ArchiveCandleProvenance)):
            raise ValueError("invalid completed-candle observation")


@dataclass(frozen=True)
class CompletedCandleSeries:
    contract_version: str
    identity: CompletedCandleSeriesIdentity
    observations: tuple[CompletedCandleObservation, ...]

    def __post_init__(self):
        if self.contract_version != COMPLETED_CANDLE_CONTRACT_VERSION or not isinstance(
                self.identity, CompletedCandleSeriesIdentity):
            raise ValueError("unsupported completed-candle contract")
        object.__setattr__(self, "observations", tuple(self.observations))
        for observation in self.observations:
            if not isinstance(observation, CompletedCandleObservation) or observation.candle.identity != self.identity:
                raise ValueError("observation series identity mismatch")
        self.market_candles  # Reject conflicting facts at admission.

    @property
    def market_candles(self):
        candles = {}
        for observation in self.observations:
            candle = observation.candle
            previous = candles.setdefault(candle.open_time_ms, candle)
            if previous != candle:
                raise ValueError("conflicting completed candle at the same open time")
        return tuple(candles[key] for key in sorted(candles))

    @property
    def missing_open_times_ms(self):
        candles = self.market_candles
        return tuple(missing for first, second in zip(candles, candles[1:])
                     for missing in range(first.open_time_ms + 60_000, second.open_time_ms, 60_000))
