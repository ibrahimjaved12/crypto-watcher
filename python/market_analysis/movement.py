"""Pure, replayable five-second Binance USD-M movement buckets.

The caller owns observation ordering and supplies every finalization boundary.
This module has no clock, network, persistence, or scheduler access.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable

BUCKET_INTERVAL_MS = 5_000
MAX_LAST_TRADE_AGE_MS = 15_000
DEFAULT_HISTORY_BUCKETS = 420
WINDOW_BUCKETS = {1: 25, 5: 121, 15: 361}
BINANCE_USDM = "binance-usdm"
TRADE_PRICE = "trade"
COLLECTOR_STATES = {"LIVE", "RECOVERING", "STALE", "UNAVAILABLE"}


def _timestamp(value, name):
    if type(value) is not int or value < 0 or value > 9_007_199_254_740_991:
        raise ValueError(f"{name} must be a nonnegative safe integer timestamp")
    return value


def _aggregate_id(value):
    if type(value) is not int or value < 0:
        raise ValueError("aggregate_trade_id must be a nonnegative integer")
    return value


def _positive_decimal(value, name):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be finite and positive") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return number


@dataclass(frozen=True)
class MarketObservation:
    """One validated futures trade, preserving each exchange/receive timestamp."""

    provider: str
    instrument_id: str
    price_type: str
    price: Decimal
    quantity: Decimal
    event_time_ms: int
    trade_time_ms: int
    aggregate_trade_id: int
    received_at_ms: int

    def __post_init__(self):
        if not isinstance(self.provider, str) or not self.provider:
            raise ValueError("provider is required")
        if not isinstance(self.instrument_id, str) or not self.instrument_id:
            raise ValueError("instrument_id is required")
        if not isinstance(self.price_type, str) or not self.price_type:
            raise ValueError("price_type is required")
        object.__setattr__(self, "price", _positive_decimal(self.price, "price"))
        object.__setattr__(self, "quantity", _positive_decimal(self.quantity, "quantity"))
        _timestamp(self.event_time_ms, "event_time_ms")
        _timestamp(self.trade_time_ms, "trade_time_ms")
        _aggregate_id(self.aggregate_trade_id)
        _timestamp(self.received_at_ms, "received_at_ms")


@dataclass(frozen=True)
class MovementBucket:
    boundary_time_ms: int
    source_state: str
    price: Decimal | None
    last_real_price: Decimal | None
    base_volume: Decimal
    quote_volume: Decimal
    trade_count: int
    last_real_trade_time_ms: int | None
    last_real_event_time_ms: int | None
    last_received_at_ms: int | None
    carried_forward: bool
    provider: str
    instrument_id: str
    price_type: str


@dataclass(frozen=True)
class MovementReadiness:
    state: str
    reason: str | None
    boundary_time_ms: int
    window_minutes: int
    collector_state: str
    required_bucket_count: int
    history: tuple[MovementBucket, ...]
    endpoint: MovementBucket | None
    last_real_trade_age_ms: int | None


@dataclass
class _PendingBucket:
    base_volume: Decimal = Decimal(0)
    quote_volume: Decimal = Decimal(0)
    trade_count: int = 0
    latest: MarketObservation | None = None

    def add(self, observation):
        self.base_volume += observation.quantity
        self.quote_volume += observation.price * observation.quantity
        self.trade_count += 1
        if (self.latest is None or
            (observation.trade_time_ms, observation.aggregate_trade_id) >
            (self.latest.trade_time_ms, self.latest.aggregate_trade_id)):
            self.latest = observation


@dataclass(frozen=True)
class MovementPendingBucketState:
    boundary_time_ms: int
    base_volume: Decimal
    quote_volume: Decimal
    trade_count: int
    latest: MarketObservation | None


@dataclass(frozen=True)
class MovementBucketEngineState:
    provider: str
    instrument_id: str
    price_type: str
    capacity: int
    buckets: tuple[MovementBucket, ...]
    pending: tuple[MovementPendingBucketState, ...]
    last_finalized_boundary_ms: int | None
    last_real_observation: MarketObservation | None
    carry_observation: MarketObservation | None
    last_accepted_order_key: tuple[int, int] | None
    rejected_late_observations: int


class MovementBucketEngine:
    """Per-instrument bounded state advanced only by explicit aligned boundaries."""

    def __init__(
        self,
        instrument_id,
        provider=BINANCE_USDM,
        price_type=TRADE_PRICE,
        capacity=DEFAULT_HISTORY_BUCKETS,
    ):
        if provider != BINANCE_USDM:
            raise ValueError("movement buckets require the Binance USD-M provider")
        if (not isinstance(instrument_id, str)
                or not instrument_id.startswith(f"{provider}:")
                or not instrument_id[len(provider) + 1:]):
            raise ValueError("instrument_id must identify a Binance USD-M instrument")
        if price_type != TRADE_PRICE:
            raise ValueError("movement buckets require trade prices")
        if type(capacity) is not int or capacity < DEFAULT_HISTORY_BUCKETS:
            raise ValueError("capacity must retain at least 420 buckets")
        self.provider = provider
        self.instrument_id = instrument_id
        self.price_type = price_type
        self.capacity = capacity
        self._buckets = deque(maxlen=capacity)
        self._pending = {}
        self._last_finalized_boundary_ms = None
        self._last_real_observation = None
        self._carry_observation = None
        self._last_accepted_order_key = None
        self._rejected_late_observations = 0

    @property
    def history(self):
        """Immutable view of the finalized compact buckets currently in memory."""
        return tuple(self._buckets)

    @property
    def rejected_late_observations(self):
        return self._rejected_late_observations

    def snapshot_state(self) -> MovementBucketEngineState:
        return MovementBucketEngineState(
            self.provider, self.instrument_id, self.price_type, self.capacity,
            tuple(self._buckets),
            tuple(MovementPendingBucketState(boundary, value.base_volume,
                                             value.quote_volume, value.trade_count,
                                             value.latest)
                  for boundary, value in sorted(self._pending.items())),
            self._last_finalized_boundary_ms, self._last_real_observation,
            self._carry_observation, self._last_accepted_order_key,
            self._rejected_late_observations)

    @classmethod
    def from_state(cls, state: MovementBucketEngineState) -> MovementBucketEngine:
        if not isinstance(state, MovementBucketEngineState):
            raise ValueError("invalid movement engine state")
        engine = cls(state.instrument_id, state.provider, state.price_type,
                     state.capacity)
        finalized = state.last_finalized_boundary_ms
        if finalized is not None:
            _timestamp(finalized, "last finalized boundary")
            if finalized % BUCKET_INTERVAL_MS:
                raise ValueError("unaligned finalized movement boundary")
        if len(state.buckets) > state.capacity or len(state.pending) > state.capacity:
            raise ValueError("movement state exceeds capacity")
        bucket_times = tuple(item.boundary_time_ms for item in state.buckets)
        if (any(not isinstance(item, MovementBucket)
                or (item.provider, item.instrument_id, item.price_type)
                != (state.provider, state.instrument_id, state.price_type)
                or item.source_state not in COLLECTOR_STATES
                for item in state.buckets)
                or any(right != left + BUCKET_INTERVAL_MS
                       for left, right in zip(bucket_times, bucket_times[1:]))
                or (bucket_times and bucket_times[-1] != finalized)
                or (finalized is None and bucket_times)):
            raise ValueError("invalid finalized movement bucket sequence")
        pending_times = tuple(item.boundary_time_ms for item in state.pending)
        if (pending_times != tuple(sorted(set(pending_times)))
                or any(value % BUCKET_INTERVAL_MS or value < 0
                       or (finalized is not None and value <= finalized)
                       for value in pending_times)):
            raise ValueError("invalid pending movement boundaries")
        def observation(value):
            if value is None:
                return
            if (not isinstance(value, MarketObservation)
                    or (value.provider, value.instrument_id, value.price_type)
                    != (state.provider, state.instrument_id, state.price_type)):
                raise ValueError("movement observation provenance mismatch")
        for value in (state.last_real_observation, state.carry_observation):
            observation(value)
        if state.carry_observation is not None and state.carry_observation != state.last_real_observation:
            raise ValueError("invalid movement carry state")
        for item in state.pending:
            if (not isinstance(item, MovementPendingBucketState)
                    or type(item.trade_count) is not int or item.trade_count <= 0
                    or item.base_volume <= 0 or item.quote_volume <= 0):
                raise ValueError("invalid pending movement aggregate")
            observation(item.latest)
            if (item.latest is None
                    or cls._bucket_boundary(item.latest.trade_time_ms) != item.boundary_time_ms):
                raise ValueError("pending movement latest observation mismatch")
        if (state.last_accepted_order_key is not None
                and (type(state.last_accepted_order_key) is not tuple
                     or len(state.last_accepted_order_key) != 2
                     or tuple(map(type, state.last_accepted_order_key)) != (int, int)
                     or min(state.last_accepted_order_key) < 0)):
            raise ValueError("invalid movement accepted order key")
        latest_keys = [(item.latest.trade_time_ms, item.latest.aggregate_trade_id)
                       for item in state.pending]
        if (state.last_accepted_order_key is None) != (state.last_real_observation is None and not latest_keys):
            raise ValueError("inconsistent movement accepted order key")
        if latest_keys and max(latest_keys) > state.last_accepted_order_key:
            raise ValueError("movement accepted key precedes pending observation")
        if (type(state.rejected_late_observations) is not int
                or state.rejected_late_observations < 0):
            raise ValueError("invalid movement late rejection count")
        engine._buckets.extend(state.buckets)
        engine._pending = {
            item.boundary_time_ms: _PendingBucket(item.base_volume,
                                                  item.quote_volume,
                                                  item.trade_count, item.latest)
            for item in state.pending}
        engine._last_finalized_boundary_ms = finalized
        engine._last_real_observation = state.last_real_observation
        engine._carry_observation = state.carry_observation
        engine._last_accepted_order_key = state.last_accepted_order_key
        engine._rejected_late_observations = state.rejected_late_observations
        return engine

    @staticmethod
    def _bucket_boundary(trade_time_ms):
        # Right-closed intervals make a trade exactly at t part of the bucket
        # ending at t, matching the endpoint rule (trade_time <= t).
        return ((trade_time_ms + BUCKET_INTERVAL_MS - 1) // BUCKET_INTERVAL_MS
                * BUCKET_INTERVAL_MS)

    def observe(self, observations: Iterable[MarketObservation]):
        """Aggregate an ordered batch without retaining raw observations."""
        batch = tuple(observations)
        previous_order_key = self._last_accepted_order_key
        for observation in batch:
            if not isinstance(observation, MarketObservation):
                raise ValueError("observations must be MarketObservation values")
            if (observation.provider != self.provider
                    or observation.instrument_id != self.instrument_id
                    or observation.price_type != self.price_type):
                raise ValueError("observation provenance does not match this instrument")
            order_key = (observation.trade_time_ms, observation.aggregate_trade_id)
            finalized_late = (
                self._last_finalized_boundary_ms is not None
                and self._bucket_boundary(observation.trade_time_ms)
                <= self._last_finalized_boundary_ms
            )
            if finalized_late:
                continue
            if previous_order_key is not None and order_key <= previous_order_key:
                raise ValueError("observations must be strictly ordered by trade time and aggregate ID")
            previous_order_key = order_key

        pending_boundaries = {
            self._bucket_boundary(observation.trade_time_ms)
            for observation in batch
            if (self._last_finalized_boundary_ms is None
                    or self._bucket_boundary(observation.trade_time_ms)
                    > self._last_finalized_boundary_ms)
        }
        if len(self._pending) + len(pending_boundaries.difference(self._pending)) > self.capacity:
            raise ValueError("too many unfinalized movement buckets")

        accepted = 0
        for observation in batch:
            boundary = self._bucket_boundary(observation.trade_time_ms)
            if (self._last_finalized_boundary_ms is not None
                    and boundary <= self._last_finalized_boundary_ms):
                self._rejected_late_observations += 1
                continue
            self._pending.setdefault(boundary, _PendingBucket()).add(observation)
            self._last_accepted_order_key = (
                observation.trade_time_ms,
                observation.aggregate_trade_id,
            )
            accepted += 1
        return accepted

    def advance(self, boundary_time_ms, source_state):
        """Finalize one boundary with its explicit source state; skipped boundaries are errors."""
        boundary_time_ms = _timestamp(boundary_time_ms, "boundary_time_ms")
        if source_state not in COLLECTOR_STATES:
            raise ValueError("invalid source state")
        if boundary_time_ms % BUCKET_INTERVAL_MS:
            raise ValueError("boundary_time_ms must align to the five-second grid")
        previous = self._last_finalized_boundary_ms
        if previous is not None and boundary_time_ms != previous + BUCKET_INTERVAL_MS:
            raise ValueError("every five-second boundary must be finalized in order")
        if previous is None and any(pending < boundary_time_ms for pending in self._pending):
            raise ValueError("finalize earlier observed bucket boundaries first")

        pending = self._pending.pop(boundary_time_ms, _PendingBucket())
        if pending.latest is not None:
            self._last_real_observation = pending.latest
        if source_state != "LIVE":
            self._carry_observation = None
        elif pending.latest is not None:
            self._carry_observation = pending.latest
        factual = self._last_real_observation
        carry = self._carry_observation
        carry_age_ms = None if carry is None else boundary_time_ms - carry.trade_time_ms
        fresh = (
            source_state == "LIVE"
            and carry is not None
            and carry_age_ms <= MAX_LAST_TRADE_AGE_MS
        )
        bucket = MovementBucket(
            boundary_time_ms=boundary_time_ms,
            source_state=source_state,
            price=carry.price if fresh else None,
            last_real_price=None if factual is None else factual.price,
            base_volume=pending.base_volume,
            quote_volume=pending.quote_volume,
            trade_count=pending.trade_count,
            last_real_trade_time_ms=None if factual is None else factual.trade_time_ms,
            last_real_event_time_ms=None if factual is None else factual.event_time_ms,
            last_received_at_ms=None if factual is None else factual.received_at_ms,
            carried_forward=pending.trade_count == 0 and fresh,
            provider=self.provider,
            instrument_id=self.instrument_id,
            price_type=self.price_type,
        )
        self._buckets.append(bucket)
        self._last_finalized_boundary_ms = boundary_time_ms
        return bucket

    def readiness(self, boundary_time_ms, window_minutes, collector_state):
        """Describe endpoint eligibility and exact live-history availability."""
        boundary_time_ms = _timestamp(boundary_time_ms, "boundary_time_ms")
        if boundary_time_ms % BUCKET_INTERVAL_MS:
            raise ValueError("boundary_time_ms must align to the five-second grid")
        if window_minutes not in WINDOW_BUCKETS:
            raise ValueError("window_minutes must be 1, 5, or 15")
        if collector_state not in COLLECTOR_STATES:
            raise ValueError("invalid collector state")

        required = WINDOW_BUCKETS[window_minutes]
        by_boundary = {bucket.boundary_time_ms: bucket for bucket in self._buckets}
        endpoint = by_boundary.get(boundary_time_ms)
        history_start = boundary_time_ms - (required - 1) * BUCKET_INTERVAL_MS
        expected = tuple(range(history_start, boundary_time_ms + 1, BUCKET_INTERVAL_MS))
        history = tuple(by_boundary[value] for value in expected if value in by_boundary)
        last_trade_time = None if endpoint is None else endpoint.last_real_trade_time_ms
        age_ms = None if last_trade_time is None else boundary_time_ms - last_trade_time

        def result(state, reason):
            return MovementReadiness(
                state=state,
                reason=reason,
                boundary_time_ms=boundary_time_ms,
                window_minutes=window_minutes,
                collector_state=collector_state,
                required_bucket_count=required,
                history=history,
                endpoint=endpoint,
                last_real_trade_age_ms=age_ms,
            )

        if collector_state in {"RECOVERING", "UNAVAILABLE"}:
            return result("unavailable", f"collector_{collector_state.lower()}")
        if collector_state == "STALE":
            return result("stale", "collector_stale")
        if endpoint is None:
            return result("missing_history", "boundary_not_retained_or_finalized")
        if age_ms is not None and age_ms > MAX_LAST_TRADE_AGE_MS:
            return result("stale", "last_real_trade_expired")
        if len(history) < required:
            return result("warming", "insufficient_exact_live_history")
        if len(history) != required or tuple(
            bucket.boundary_time_ms for bucket in history
        ) != expected:
            return result("missing_history", "noncontiguous_live_history")
        if any(bucket.price is None for bucket in history):
            if any(bucket.source_state != "LIVE" for bucket in history):
                return result("unavailable", "source_unavailable_in_required_history")
            if any(bucket.last_real_trade_time_ms is None for bucket in history):
                return result("unavailable", "no_real_trade_history")
            return result("stale", "unusable_price_history")
        if last_trade_time is None:
            return result("unavailable", "no_real_trade_endpoint")
        return result("ready", None)
