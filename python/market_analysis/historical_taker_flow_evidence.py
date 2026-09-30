"""Immutable side-aware aggTrade evidence for EXP-75-12.

The archive's first-seen timestamp is an exchange-time surrogate. It cannot
reconstruct historical WebSocket delivery latency or collector health.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
from types import MappingProxyType
from typing import Mapping

from .movement import BUCKET_INTERVAL_MS, MovementBucketEngine


TAKER_FLOW_EVIDENCE_SCHEMA_VERSION = "historical-taker-flow-evidence-v1"
TAKER_FLOW_ALGORITHM_VERSION = "taker-buy-sell-imbalance-v1"
TAKER_FLOW_CONFIG_VERSION = "EXP-75-12-fixed-1m-5m-15m-decimal50-v1"
TAKER_FLOW_BUCKET_RULE = "movement-engine-right-closed-ceil-5s-v1"
TAKER_FLOW_SIDE_MAPPING = "buyer_is_maker=false:aggressive_buy;true:aggressive_sell"
TAKER_FLOW_AVAILABILITY_BASIS = "exchange-timestamp-surrogate-v1"
TAKER_FLOW_WINDOWS = (1, 5, 15)


def _decimal(value, name):
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite nonnegative Decimal") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{name} must be a finite nonnegative Decimal")
    return result


def _timestamp(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer timestamp")
    return value


def _sum_decimals_exact(values) -> Decimal:
    values = tuple(values)
    if not values:
        return Decimal(0)
    exponent = min(value.as_tuple().exponent for value in values)
    adjusted_values = [value.adjusted() for value in values if value]
    adjusted = max(adjusted_values, default=0)
    precision = max(1, adjusted - exponent + 1 + len(str(len(values))))
    with localcontext() as context:
        context.prec = precision
        return sum(values, Decimal(0))


@dataclass(frozen=True)
class HistoricalTakerFlowBucket:
    boundary_time_ms: int
    buy_quote_notional: Decimal
    sell_quote_notional: Decimal
    buy_aggtrade_count: int
    sell_aggtrade_count: int

    def __post_init__(self):
        boundary = _timestamp(self.boundary_time_ms, "bucket boundary")
        if boundary % BUCKET_INTERVAL_MS:
            raise ValueError("flow bucket boundary must align to five seconds")
        object.__setattr__(self, "buy_quote_notional",
                           _decimal(self.buy_quote_notional, "buy notional"))
        object.__setattr__(self, "sell_quote_notional",
                           _decimal(self.sell_quote_notional, "sell notional"))
        for name in ("buy_aggtrade_count", "sell_aggtrade_count"):
            count = getattr(self, name)
            if type(count) is not int or count < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if ((self.buy_aggtrade_count == 0) != (self.buy_quote_notional == 0)
                or (self.sell_aggtrade_count == 0) != (self.sell_quote_notional == 0)):
            raise ValueError("notional and aggTrade row count must agree")


@dataclass(frozen=True)
class TakerFlowWindowSums:
    buy_quote_notional: Decimal
    sell_quote_notional: Decimal
    buy_aggtrade_count: int
    sell_aggtrade_count: int

    @property
    def gross_quote_notional(self) -> Decimal:
        return _sum_decimals_exact((self.buy_quote_notional, self.sell_quote_notional))


@dataclass(frozen=True)
class HistoricalTakerFlowSymbolBuckets:
    symbol: str
    buckets: tuple[HistoricalTakerFlowBucket, ...]
    _boundaries: tuple[int, ...] = field(default=(), init=False, repr=False, compare=False)
    _buy_prefix: tuple[Decimal, ...] = field(default=(), init=False, repr=False, compare=False)
    _sell_prefix: tuple[Decimal, ...] = field(default=(), init=False, repr=False, compare=False)
    _buy_count_prefix: tuple[int, ...] = field(default=(), init=False, repr=False, compare=False)
    _sell_count_prefix: tuple[int, ...] = field(default=(), init=False, repr=False, compare=False)

    def __post_init__(self):
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("flow evidence symbol is required")
        buckets = tuple(self.buckets)
        if (any(not isinstance(item, HistoricalTakerFlowBucket) for item in buckets)
                or tuple(item.boundary_time_ms for item in buckets)
                != tuple(sorted({item.boundary_time_ms for item in buckets}))):
            raise ValueError("flow buckets must have unique increasing boundaries")
        boundaries = tuple(item.boundary_time_ms for item in buckets)
        buy, sell, buy_count, sell_count = [Decimal(0)], [Decimal(0)], [0], [0]
        for bucket in buckets:
            buy.append(_sum_decimals_exact((buy[-1], bucket.buy_quote_notional)))
            sell.append(_sum_decimals_exact((sell[-1], bucket.sell_quote_notional)))
            buy_count.append(buy_count[-1] + bucket.buy_aggtrade_count)
            sell_count.append(sell_count[-1] + bucket.sell_aggtrade_count)
        object.__setattr__(self, "buckets", buckets)
        object.__setattr__(self, "_boundaries", boundaries)
        object.__setattr__(self, "_buy_prefix", tuple(buy))
        object.__setattr__(self, "_sell_prefix", tuple(sell))
        object.__setattr__(self, "_buy_count_prefix", tuple(buy_count))
        object.__setattr__(self, "_sell_count_prefix", tuple(sell_count))

    def sum_boundaries(self, start_exclusive_ms: int,
                       end_inclusive_ms: int) -> TakerFlowWindowSums:
        """Sum sparse bucket values in (start, end] in O(log n)."""
        start = _timestamp(start_exclusive_ms, "window start")
        end = _timestamp(end_inclusive_ms, "window end")
        if (end < start or start % BUCKET_INTERVAL_MS
                or end % BUCKET_INTERVAL_MS):
            raise ValueError("flow query endpoints must be ordered five-second boundaries")
        left = bisect_right(self._boundaries, start)
        right = bisect_right(self._boundaries, end)
        buy = _sum_decimals_exact((self._buy_prefix[right], -self._buy_prefix[left]))
        sell = _sum_decimals_exact((self._sell_prefix[right], -self._sell_prefix[left]))
        return TakerFlowWindowSums(
            buy,
            sell,
            self._buy_count_prefix[right] - self._buy_count_prefix[left],
            self._sell_count_prefix[right] - self._sell_count_prefix[left],
        )


def _content_payload(*, schema_version, algorithm_version, dataset_id,
                     dataset_version, dataset_content_sha256,
                     configured_symbols, engine_start_boundary_time_ms,
                     output_end_boundary_time_ms, bucket_interval_ms, bucket_rule,
                     side_mapping, availability_basis, finalization_grace_ms,
                     symbol_buckets) -> dict:
    return {
        "schema_version": schema_version,
        "algorithm_version": algorithm_version,
        "dataset_id": dataset_id,
        "dataset_version": dataset_version,
        "dataset_content_sha256": dataset_content_sha256,
        "configured_symbols": tuple(configured_symbols),
        "engine_start_boundary_time_ms": engine_start_boundary_time_ms,
        "output_end_boundary_time_ms": output_end_boundary_time_ms,
        "bucket_interval_ms": bucket_interval_ms,
        "bucket_rule": bucket_rule,
        "side_mapping": side_mapping,
        "availability_basis": availability_basis,
        "finalization_grace_ms": finalization_grace_ms,
        "buckets": tuple(
            (symbol.symbol, bucket.boundary_time_ms,
             str(bucket.buy_quote_notional), str(bucket.sell_quote_notional),
             bucket.buy_aggtrade_count, bucket.sell_aggtrade_count)
            for symbol in symbol_buckets for bucket in symbol.buckets
        ),
    }


def _evidence_payload(evidence: "HistoricalTakerFlowEvidence") -> dict:
    return _content_payload(
        schema_version=evidence.schema_version,
        algorithm_version=evidence.algorithm_version,
        dataset_id=evidence.dataset_id,
        dataset_version=evidence.dataset_version,
        dataset_content_sha256=evidence.dataset_content_sha256,
        configured_symbols=evidence.configured_symbols,
        engine_start_boundary_time_ms=evidence.engine_start_boundary_time_ms,
        output_end_boundary_time_ms=evidence.output_end_boundary_time_ms,
        bucket_interval_ms=evidence.bucket_interval_ms,
        bucket_rule=evidence.bucket_rule,
        side_mapping=evidence.side_mapping,
        availability_basis=evidence.availability_basis,
        finalization_grace_ms=evidence.finalization_grace_ms,
        symbol_buckets=evidence.symbol_buckets,
    )


def _sha256(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class HistoricalTakerFlowEvidence:
    schema_version: str
    algorithm_version: str
    dataset_id: str
    dataset_version: str
    dataset_content_sha256: str
    configured_symbols: tuple[str, ...]
    engine_start_boundary_time_ms: int
    output_end_boundary_time_ms: int
    bucket_interval_ms: int
    bucket_rule: str
    side_mapping: str
    availability_basis: str
    finalization_grace_ms: int
    symbol_buckets: tuple[HistoricalTakerFlowSymbolBuckets, ...]
    evidence_sha256: str
    _by_symbol: Mapping[str, HistoricalTakerFlowSymbolBuckets] = field(
        default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self):
        symbols = tuple(self.configured_symbols)
        series = tuple(self.symbol_buckets)
        if (not self.dataset_id or not self.dataset_version
                or len(self.dataset_content_sha256) != 64
                or any(ch not in "0123456789abcdef" for ch in self.dataset_content_sha256)
                or not symbols or len(set(symbols)) != len(symbols)
                or tuple(item.symbol for item in series) != symbols):
            raise ValueError("flow evidence dataset identity and ordered symbols are required")
        start = _timestamp(self.engine_start_boundary_time_ms, "engine start")
        end = _timestamp(self.output_end_boundary_time_ms, "output end")
        if (start % BUCKET_INTERVAL_MS or end % BUCKET_INTERVAL_MS or end < start
                or self.bucket_interval_ms != BUCKET_INTERVAL_MS
                or type(self.finalization_grace_ms) is not int
                or self.finalization_grace_ms <= 0):
            raise ValueError("invalid flow evidence range, bucket interval, or grace")
        for name in ("schema_version", "algorithm_version", "bucket_rule",
                     "side_mapping", "availability_basis"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} is required")
        for item in series:
            if any(not start <= bucket.boundary_time_ms <= end
                   for bucket in item.buckets):
                raise ValueError("flow bucket is outside the captured replay range")
        expected = _sha256(_evidence_payload(self))
        if self.evidence_sha256 != expected:
            raise ValueError("flow evidence digest does not match its canonical content")
        object.__setattr__(self, "configured_symbols", symbols)
        object.__setattr__(self, "symbol_buckets", series)
        object.__setattr__(self, "_by_symbol", MappingProxyType(
            {item.symbol: item for item in series}))

    def query_window(self, symbol: str, boundary_time_ms: int,
                     window_minutes: int) -> tuple[TakerFlowWindowSums | None, str | None]:
        if type(window_minutes) is not int or window_minutes not in TAKER_FLOW_WINDOWS:
            raise ValueError("taker flow windows are fixed to 1m, 5m, and 15m")
        boundary = _timestamp(boundary_time_ms, "evaluation boundary")
        if boundary % BUCKET_INTERVAL_MS:
            raise ValueError("evaluation boundary must align to five seconds")
        series = self._by_symbol.get(symbol)
        if series is None:
            return None, "FLOW_EVIDENCE_UNAVAILABLE"
        start = boundary - window_minutes * 60_000
        if (start < self.engine_start_boundary_time_ms
                or boundary > self.output_end_boundary_time_ms):
            return None, "FLOW_EVIDENCE_UNAVAILABLE"
        return series.sum_boundaries(start, boundary), None

    def matches_dataset(self, dataset_id: str, dataset_version: str,
                        content_sha256: str, configured_symbols: tuple[str, ...]) -> bool:
        return ((self.dataset_id, self.dataset_version, self.dataset_content_sha256,
                 self.configured_symbols)
                == (dataset_id, dataset_version, content_sha256,
                    tuple(configured_symbols)))


class HistoricalTakerFlowEvidenceBuilder:
    """Compact bucket accumulator with replay-compatible admission and finalization."""

    def __init__(self, *, dataset_id: str, dataset_version: str,
                 dataset_content_sha256: str | None, configured_symbols: tuple[str, ...],
                 engine_start_boundary_time_ms: int, output_end_boundary_time_ms: int,
                 finalization_grace_ms: int):
        self.dataset_id = dataset_id
        self.dataset_version = dataset_version
        self.dataset_content_sha256 = dataset_content_sha256
        self.configured_symbols = tuple(configured_symbols)
        self.engine_start_boundary_time_ms = _timestamp(
            engine_start_boundary_time_ms, "engine start")
        self.output_end_boundary_time_ms = _timestamp(
            output_end_boundary_time_ms, "output end")
        self.finalization_grace_ms = finalization_grace_ms
        if (not self.configured_symbols or len(set(self.configured_symbols))
                != len(self.configured_symbols)
                or self.engine_start_boundary_time_ms % BUCKET_INTERVAL_MS
                or self.output_end_boundary_time_ms % BUCKET_INTERVAL_MS
                or self.output_end_boundary_time_ms < self.engine_start_boundary_time_ms
                or type(finalization_grace_ms) is not int or finalization_grace_ms <= 0):
            raise ValueError("invalid taker flow evidence builder configuration")
        self._buckets = {symbol: {} for symbol in self.configured_symbols}
        self._finalized_boundary = None
        self._finalized_outputs = []

    def add_trade(self, symbol: str, event_time_ms: int, first_seen_at_ms: int,
                  price: Decimal, quantity: Decimal, buyer_is_maker: bool) -> bool:
        """Add only rows admitted before their assigned bucket is finalized."""
        if symbol not in self._buckets:
            raise ValueError("flow row symbol is outside configured universe")
        event_time = _timestamp(event_time_ms, "event time")
        first_seen = _timestamp(first_seen_at_ms, "first seen")
        if first_seen < event_time:
            raise ValueError("first-seen availability cannot precede event time")
        price = _decimal(price, "price")
        quantity = _decimal(quantity, "quantity")
        if price <= 0 or quantity <= 0 or type(buyer_is_maker) is not bool:
            raise ValueError("flow row requires positive price/quantity and a boolean maker flag")
        bucket = MovementBucketEngine._bucket_boundary(event_time)
        if (event_time > bucket or bucket < self.engine_start_boundary_time_ms
                or bucket > self.output_end_boundary_time_ms
                or first_seen > bucket + self.finalization_grace_ms
                or self._finalized_boundary is not None
                and bucket <= self._finalized_boundary):
            return False
        row = self._buckets[symbol].setdefault(
            bucket, [Decimal(0), Decimal(0), 0, 0])
        product_precision = (len(price.as_tuple().digits)
                             + len(quantity.as_tuple().digits))
        with localcontext() as context:
            context.prec = max(1, product_precision)
            notional = price * quantity
        side = 1 if buyer_is_maker else 0
        row[side] = _sum_decimals_exact((row[side], notional))
        row[side + 2] += 1
        return True

    def finalize_bucket(self, boundary_time_ms: int) -> tuple[HistoricalTakerFlowBucket, ...]:
        boundary = _timestamp(boundary_time_ms, "finalization boundary")
        expected = (self.engine_start_boundary_time_ms if self._finalized_boundary is None
                    else self._finalized_boundary + BUCKET_INTERVAL_MS)
        if boundary != expected or boundary > self.output_end_boundary_time_ms:
            raise ValueError("flow buckets must finalize once in replay boundary order")
        result = tuple(
            HistoricalTakerFlowBucket(
                boundary,
                self._buckets[symbol].get(boundary, (Decimal(0), Decimal(0), 0, 0))[0],
                self._buckets[symbol].get(boundary, (Decimal(0), Decimal(0), 0, 0))[1],
                self._buckets[symbol].get(boundary, (Decimal(0), Decimal(0), 0, 0))[2],
                self._buckets[symbol].get(boundary, (Decimal(0), Decimal(0), 0, 0))[3],
            ) for symbol in self.configured_symbols
        )
        self._finalized_outputs.append((boundary, result))
        self._finalized_boundary = boundary
        return result

    def build(self, *, dataset_id: str | None = None,
              dataset_version: str | None = None,
              dataset_content_sha256: str | None = None) -> HistoricalTakerFlowEvidence:
        dataset_id = self.dataset_id if dataset_id is None else dataset_id
        dataset_version = self.dataset_version if dataset_version is None else dataset_version
        dataset_content_sha256 = (self.dataset_content_sha256
                                  if dataset_content_sha256 is None
                                  else dataset_content_sha256)
        if (not isinstance(dataset_id, str) or not dataset_id
                or not isinstance(dataset_version, str) or not dataset_version
                or not isinstance(dataset_content_sha256, str)):
            raise ValueError("flow evidence dataset identity is required")
        series = tuple(HistoricalTakerFlowSymbolBuckets(
            symbol, tuple(HistoricalTakerFlowBucket(boundary, *values)
                          for boundary, values in sorted(self._buckets[symbol].items())))
            for symbol in self.configured_symbols)
        fields = dict(
            schema_version=TAKER_FLOW_EVIDENCE_SCHEMA_VERSION,
            algorithm_version=TAKER_FLOW_ALGORITHM_VERSION,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            dataset_content_sha256=dataset_content_sha256,
            configured_symbols=self.configured_symbols,
            engine_start_boundary_time_ms=self.engine_start_boundary_time_ms,
            output_end_boundary_time_ms=self.output_end_boundary_time_ms,
            bucket_interval_ms=BUCKET_INTERVAL_MS,
            bucket_rule=TAKER_FLOW_BUCKET_RULE,
            side_mapping=TAKER_FLOW_SIDE_MAPPING,
            availability_basis=TAKER_FLOW_AVAILABILITY_BASIS,
            finalization_grace_ms=self.finalization_grace_ms,
            symbol_buckets=series,
        )
        return HistoricalTakerFlowEvidence(
            fields["schema_version"], fields["algorithm_version"],
            fields["dataset_id"], fields["dataset_version"],
            fields["dataset_content_sha256"], fields["configured_symbols"],
            fields["engine_start_boundary_time_ms"],
            fields["output_end_boundary_time_ms"], fields["bucket_interval_ms"],
            fields["bucket_rule"], fields["side_mapping"], fields["availability_basis"],
            fields["finalization_grace_ms"], fields["symbol_buckets"],
            _sha256(_content_payload(**fields)),
        )
