"""Issue #35 Part 1: causal historical #70/#71 replay over explicit inputs.

MarketObservation.received_at_ms represents dataset first-seen availability here,
not a claimed original live WebSocket socket-receive timestamp.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import json

from .movement import (
    BINANCE_USDM, BUCKET_INTERVAL_MS, COLLECTOR_STATES,
    MAX_LAST_TRADE_AGE_MS, TRADE_PRICE, WINDOW_BUCKETS,
    MarketObservation, MovementBucket, MovementBucketEngine,
)
from .movement_history import (
    MINUTE_MS, CompletedMovementCandle, build_historical_window_inputs,
)
from .movement_metrics import (
    ALGORITHM_VERSION as MOVEMENT_ALGORITHM_VERSION,
    EXCHANGE, MarketMovementConfig, MarketMovementEvaluation, MarketMovementInput,
    MarketMovementSymbolInput, MarketUniverseInput, WINDOWS,
    calculate_market_movement,
)
from .movement_classifier import SymbolSourceTimeEvidence
from .experiments.market_state_common import (
    MarketStateExperimentPoint, validate_experiment_points,
)


HISTORICAL_REPLAY_ALGORITHM_VERSION = "historical-market-replay-v1"
HISTORICAL_REPLAY_POLICY_VERSION = (
    "historical-market-replay-policy-v1:cadence-5000ms"
    ":availability-explicit-first-seen:history-strict-completed-1m"
    ":source-state-explicit:finalization-grace-positive"
    ":equal-time-order-trade-time-aggregate-id"
)
DEFAULT_FINALIZATION_GRACE_MS = 2_000
_MAX_SAFE_TIMESTAMP = 9_007_199_254_740_991


def _timestamp(value, name):
    if type(value) is not int or not 0 <= value <= _MAX_SAFE_TIMESTAMP:
        raise ValueError(f"{name} must be a nonnegative safe integer timestamp")
    return value


def _decimal(value, name, *, positive):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not number.is_finite() or (number <= 0 if positive else number < 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return number


def _digest(value):
    return (isinstance(value, str) and len(value) == 64
            and all(character in "0123456789abcdef" for character in value))


@dataclass(frozen=True)
class HistoricalReplayConfig:
    output_start_boundary_time_ms: int
    output_end_boundary_time_ms: int
    finalization_grace_ms: int = DEFAULT_FINALIZATION_GRACE_MS
    movement_config: MarketMovementConfig = field(default_factory=MarketMovementConfig)

    def __post_init__(self):
        start = _timestamp(self.output_start_boundary_time_ms, "output start")
        end = _timestamp(self.output_end_boundary_time_ms, "output end")
        if start % BUCKET_INTERVAL_MS or end % BUCKET_INTERVAL_MS or end < start:
            raise ValueError("replay output interval must be ordered and five-second aligned")
        if (type(self.finalization_grace_ms) is not int
                or self.finalization_grace_ms <= 0
                or end + self.finalization_grace_ms > _MAX_SAFE_TIMESTAMP):
            raise ValueError("finalization grace must be a positive safe integer")
        if not isinstance(self.movement_config, MarketMovementConfig):
            raise ValueError("movement_config must be MarketMovementConfig")
        if self.engine_start_boundary_time_ms < 0:
            raise ValueError("output start is too early for canonical #70 warm-up")

    @property
    def engine_start_boundary_time_ms(self):
        return (self.output_start_boundary_time_ms
                - (WINDOW_BUCKETS[15] - 1) * BUCKET_INTERVAL_MS
                - MAX_LAST_TRADE_AGE_MS)


@dataclass(frozen=True)
class HistoricalReplayDatasetManifest:
    dataset_id: str
    dataset_version: str
    content_sha256: str
    provider: str = BINANCE_USDM
    exchange: str = EXCHANGE
    price_type: str = TRADE_PRICE

    def __post_init__(self):
        if (not isinstance(self.dataset_id, str) or not self.dataset_id
                or not isinstance(self.dataset_version, str) or not self.dataset_version
                or not _digest(self.content_sha256)):
            raise ValueError("dataset identity and lowercase SHA-256 are required")
        if (self.provider, self.exchange, self.price_type) != (BINANCE_USDM, EXCHANGE, TRADE_PRICE):
            raise ValueError("V1 replay requires Binance USD-M trade data")


@dataclass(frozen=True)
class HistoricalReplayInstrument:
    symbol: str
    instrument_id: str
    instrument_compatible: bool | None

    def __post_init__(self):
        if (not isinstance(self.symbol, str) or not self.symbol
                or self.instrument_id != f"{BINANCE_USDM}:{self.symbol}"
                or self.instrument_compatible is not None
                and type(self.instrument_compatible) is not bool):
            raise ValueError("invalid historical replay instrument")


@dataclass(frozen=True)
class HistoricalReplayTrade:
    symbol: str
    instrument_id: str
    price: Decimal
    quantity: Decimal
    event_time_ms: int
    trade_time_ms: int
    aggregate_trade_id: int
    first_seen_at_ms: int

    def __post_init__(self):
        if (not isinstance(self.symbol, str) or not self.symbol
                or self.instrument_id != f"{BINANCE_USDM}:{self.symbol}"):
            raise ValueError("trade symbol/instrument is invalid")
        object.__setattr__(self, "price", _decimal(self.price, "price", positive=True))
        object.__setattr__(self, "quantity", _decimal(self.quantity, "quantity", positive=True))
        _timestamp(self.event_time_ms, "event_time_ms")
        _timestamp(self.trade_time_ms, "trade_time_ms")
        _timestamp(self.first_seen_at_ms, "first_seen_at_ms")
        if type(self.aggregate_trade_id) is not int or self.aggregate_trade_id < 0:
            raise ValueError("aggregate_trade_id must be nonnegative")
        if self.first_seen_at_ms < max(self.event_time_ms, self.trade_time_ms):
            raise ValueError("first-seen availability cannot precede trade/event time")


@dataclass(frozen=True)
class HistoricalReplayMovementCandle:
    symbol: str
    open_time_ms: int
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    first_seen_at_ms: int

    def __post_init__(self):
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("candle symbol is required")
        opening = _timestamp(self.open_time_ms, "candle open")
        first_seen = _timestamp(self.first_seen_at_ms, "candle first seen")
        if opening % MINUTE_MS or first_seen < opening + MINUTE_MS:
            raise ValueError("historical candle must be completed before first seen")
        object.__setattr__(self, "close", _decimal(self.close, "close", positive=True))
        object.__setattr__(self, "volume", _decimal(self.volume, "volume", positive=False))
        object.__setattr__(self, "quote_volume", _decimal(self.quote_volume, "quote volume", positive=False))

    def canonical(self):
        return CompletedMovementCandle(
            self.open_time_ms, self.close, self.volume, self.quote_volume)


@dataclass(frozen=True)
class HistoricalReplaySourceInterval:
    symbol: str
    start_boundary_time_ms: int
    end_boundary_time_ms: int
    source_state: str

    def __post_init__(self):
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("source interval symbol is required")
        start = _timestamp(self.start_boundary_time_ms, "source interval start")
        end = _timestamp(self.end_boundary_time_ms, "source interval end")
        if (start % BUCKET_INTERVAL_MS or end % BUCKET_INTERVAL_MS
                or end < start or self.source_state not in COLLECTOR_STATES):
            raise ValueError("invalid aligned source-state interval")


@dataclass(frozen=True)
class HistoricalReplayRequest:
    dataset: HistoricalReplayDatasetManifest
    universe: MarketUniverseInput
    instruments: tuple[HistoricalReplayInstrument, ...]
    trades: tuple[HistoricalReplayTrade, ...]
    candles: tuple[HistoricalReplayMovementCandle, ...]
    source_intervals: tuple[HistoricalReplaySourceInterval, ...]
    config: HistoricalReplayConfig

    def __post_init__(self):
        if (not isinstance(self.dataset, HistoricalReplayDatasetManifest)
                or not isinstance(self.universe, MarketUniverseInput)
                or not isinstance(self.config, HistoricalReplayConfig)
                or not self.universe.symbols):
            raise ValueError("replay requires dataset, nonempty universe, and config")
        for name, kind in (("instruments", HistoricalReplayInstrument),
                           ("trades", HistoricalReplayTrade),
                           ("candles", HistoricalReplayMovementCandle),
                           ("source_intervals", HistoricalReplaySourceInterval)):
            values = tuple(getattr(self, name))
            if any(not isinstance(value, kind) for value in values):
                raise ValueError(f"{name} must contain {kind.__name__}")
            object.__setattr__(self, name, values)
        if tuple(item.symbol for item in self.instruments) != self.universe.symbols:
            raise ValueError("instruments must match configured universe order")
        if not self.trades:
            raise ValueError("dataset lacks raw replayable trade evidence")


@dataclass(frozen=True)
class HistoricalReplayRunManifest:
    algorithm_version: str
    policy_version: str
    dataset_id: str
    dataset_version: str
    dataset_content_sha256: str
    provider: str
    exchange: str
    price_type: str
    universe_id: str
    universe_version: str
    configured_universe: tuple[str, ...]
    instrument_contract: tuple[tuple[str, str, bool | None], ...]
    movement_algorithm_version: str
    movement_config_version: str
    movement_config_parameters: tuple[tuple[str, str], ...]
    output_start_boundary_time_ms: int
    output_end_boundary_time_ms: int
    finalization_grace_ms: int
    run_fingerprint: str


@dataclass(frozen=True)
class HistoricalMarketReplayPoint:
    point_id: str
    evaluation_boundary_time_ms: int
    replay_clock_time_ms: int
    movement_evaluation: MarketMovementEvaluation
    endpoint_buckets: tuple[tuple[str, MovementBucket], ...]
    source_time_evidence: tuple[SymbolSourceTimeEvidence, ...]
    source_states: tuple[tuple[str, str], ...]

    def __post_init__(self):
        object.__setattr__(self, "endpoint_buckets", tuple(self.endpoint_buckets))
        object.__setattr__(self, "source_time_evidence", tuple(self.source_time_evidence))
        object.__setattr__(self, "source_states", tuple(self.source_states))


@dataclass(frozen=True)
class HistoricalReplayCheckpoint:
    run_fingerprint: str
    last_emitted_boundary_time_ms: int
    last_point_id: str


@dataclass(frozen=True)
class HistoricalReplayDiagnostics:
    engine_start_boundary_time_ms: int
    output_start_boundary_time_ms: int
    output_end_boundary_time_ms: int
    processed_trade_count: int
    deduplicated_trade_count: int
    late_trade_count: int
    emitted_point_count: int
    source_state_symbol_boundary_counts: tuple[tuple[str, int], ...]
    market_wide_eligible_point_counts: tuple[tuple[int, int], ...]
    market_wide_ineligible_point_counts: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class HistoricalMarketReplayResult:
    manifest: HistoricalReplayRunManifest
    points: tuple[HistoricalMarketReplayPoint, ...]
    diagnostics: HistoricalReplayDiagnostics
    final_checkpoint: HistoricalReplayCheckpoint | None

    def __post_init__(self):
        object.__setattr__(self, "points", tuple(self.points))


@dataclass(frozen=True)
class ReplayPartitionPlan:
    development_end_boundary_time_ms: int
    validation_end_boundary_time_ms: int

    def __post_init__(self):
        for name in ("development_end_boundary_time_ms", "validation_end_boundary_time_ms"):
            value = _timestamp(getattr(self, name), name)
            if value % BUCKET_INTERVAL_MS:
                raise ValueError("partition cutoffs must be five-second aligned")
        if self.validation_end_boundary_time_ms <= self.development_end_boundary_time_ms:
            raise ValueError("validation cutoff must follow development cutoff")


def _movement_config_identity(config):
    return tuple((name, value.hex() if type(value) is float else str(value))
                 for name, value in asdict(config).items())


def _run_manifest(request):
    dataset, universe, config = request.dataset, request.universe, request.config
    fields = dict(
        algorithm_version=HISTORICAL_REPLAY_ALGORITHM_VERSION,
        policy_version=HISTORICAL_REPLAY_POLICY_VERSION,
        dataset_id=dataset.dataset_id, dataset_version=dataset.dataset_version,
        dataset_content_sha256=dataset.content_sha256,
        provider=dataset.provider, exchange=dataset.exchange,
        price_type=dataset.price_type, universe_id=universe.id,
        universe_version=universe.version, configured_universe=universe.symbols,
        instrument_contract=tuple((item.symbol, item.instrument_id,
                                   item.instrument_compatible) for item in request.instruments),
        movement_algorithm_version=MOVEMENT_ALGORITHM_VERSION,
        movement_config_version=config.movement_config.version,
        movement_config_parameters=_movement_config_identity(config.movement_config),
        output_start_boundary_time_ms=config.output_start_boundary_time_ms,
        output_end_boundary_time_ms=config.output_end_boundary_time_ms,
        finalization_grace_ms=config.finalization_grace_ms,
    )
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return HistoricalReplayRunManifest(**fields, run_fingerprint=digest)


def _point_id(run_fingerprint, boundary):
    return hashlib.sha256(f"{run_fingerprint}|movement|{boundary}".encode("ascii")).hexdigest()


def _canonical_inputs(request):
    """Reject conflicting identities and order records independently of row order."""
    symbols = request.universe.symbols
    symbol_index = {symbol: index for index, symbol in enumerate(symbols)}
    instruments = {item.symbol: item for item in request.instruments}
    by_trade_id = {}
    for trade in request.trades:
        if trade.symbol not in instruments or trade.instrument_id != instruments[trade.symbol].instrument_id:
            raise ValueError("trade does not match configured replay instrument")
        key = (trade.symbol, trade.aggregate_trade_id)
        prior = by_trade_id.get(key)
        if prior is not None and prior != trade:
            raise ValueError("conflicting aggregate trade identity")
        by_trade_id[key] = trade
    deduplicated = len(request.trades) - len(by_trade_id)
    engine_start = request.config.engine_start_boundary_time_ms
    trades = tuple(sorted((trade for trade in by_trade_id.values()
                           if MovementBucketEngine._bucket_boundary(trade.trade_time_ms) >= engine_start),
                          key=lambda trade: (trade.first_seen_at_ms, symbol_index[trade.symbol],
                                             trade.trade_time_ms, trade.aggregate_trade_id)))
    by_candle_open = {}
    for candle in request.candles:
        if candle.symbol not in instruments:
            raise ValueError("candle symbol is outside configured universe")
        key = (candle.symbol, candle.open_time_ms)
        prior = by_candle_open.get(key)
        if prior is not None and prior != candle:
            raise ValueError("conflicting historical candle identity")
        by_candle_open[key] = candle
    candles = tuple(sorted(by_candle_open.values(),
                           key=lambda candle: (candle.first_seen_at_ms,
                                               symbol_index[candle.symbol], candle.open_time_ms)))
    intervals = {symbol: [] for symbol in symbols}
    for interval in request.source_intervals:
        if interval.symbol not in intervals:
            raise ValueError("source interval symbol is outside configured universe")
        intervals[interval.symbol].append(interval)
    for symbol in symbols:
        sequence = tuple(sorted(intervals[symbol], key=lambda item: item.start_boundary_time_ms))
        if (not sequence or sequence[0].start_boundary_time_ms > engine_start
                or sequence[0].end_boundary_time_ms < engine_start
                or sequence[-1].end_boundary_time_ms < request.config.output_end_boundary_time_ms):
            raise ValueError("source-state coverage is incomplete")
        if any(right.start_boundary_time_ms != left.end_boundary_time_ms + BUCKET_INTERVAL_MS
               for left, right in zip(sequence, sequence[1:])):
            raise ValueError("source-state intervals must be consecutive and nonoverlapping")
        intervals[symbol] = sequence
    return trades, candles, intervals, deduplicated


def _validate_checkpoint(checkpoint, manifest):
    if checkpoint is None:
        return
    if not isinstance(checkpoint, HistoricalReplayCheckpoint):
        raise ValueError("checkpoint must be HistoricalReplayCheckpoint")
    boundary = checkpoint.last_emitted_boundary_time_ms
    if (checkpoint.run_fingerprint != manifest.run_fingerprint
            or type(boundary) is not int
            or not manifest.output_start_boundary_time_ms <= boundary <= manifest.output_end_boundary_time_ms
            or boundary % BUCKET_INTERVAL_MS
            or checkpoint.last_point_id != _point_id(manifest.run_fingerprint, boundary)):
        raise ValueError("checkpoint does not belong to this replay run")


def run_historical_market_replay(
    request: HistoricalReplayRequest,
    checkpoint: HistoricalReplayCheckpoint | None = None,
) -> HistoricalMarketReplayResult:
    """Rebuild from warm-up; skip checkpointed outputs without private engine snapshots."""
    if not isinstance(request, HistoricalReplayRequest):
        raise ValueError("request must be HistoricalReplayRequest")
    manifest = _run_manifest(request)
    _validate_checkpoint(checkpoint, manifest)
    trades, candles, source_intervals, duplicate_count = _canonical_inputs(request)
    symbols = request.universe.symbols
    config = request.config
    engines = {instrument.symbol: MovementBucketEngine(instrument.instrument_id)
               for instrument in request.instruments}
    instruments = {instrument.symbol: instrument for instrument in request.instruments}
    source_indexes = {symbol: 0 for symbol in symbols}
    visible_candles = {symbol: [] for symbol in symbols}
    pending_trades = {symbol: [] for symbol in symbols}
    trade_index = candle_index = processed_trades = 0
    source_counts = Counter()
    eligible_counts = Counter()
    ineligible_counts = Counter()
    points = []
    for boundary in range(config.engine_start_boundary_time_ms,
                          config.output_end_boundary_time_ms + 1, BUCKET_INTERVAL_MS):
        replay_clock = boundary + config.finalization_grace_ms
        while trade_index < len(trades) and trades[trade_index].first_seen_at_ms <= replay_clock:
            trade = trades[trade_index]
            pending_trades[trade.symbol].append(trade)
            trade_index += 1
        while candle_index < len(candles) and candles[candle_index].first_seen_at_ms <= replay_clock:
            candle = candles[candle_index]
            visible_candles[candle.symbol].append(candle.canonical())
            candle_index += 1
        endpoint_buckets = {}
        current_states = {}
        for symbol in symbols:
            pending = pending_trades[symbol]
            eligible = [trade for trade in pending if trade.trade_time_ms <= boundary]
            pending_trades[symbol] = [trade for trade in pending if trade.trade_time_ms > boundary]
            eligible.sort(key=lambda trade: (trade.trade_time_ms, trade.aggregate_trade_id))
            if eligible:
                observations = tuple(MarketObservation(
                    provider=BINANCE_USDM, instrument_id=trade.instrument_id,
                    price_type=TRADE_PRICE, price=trade.price, quantity=trade.quantity,
                    event_time_ms=trade.event_time_ms, trade_time_ms=trade.trade_time_ms,
                    aggregate_trade_id=trade.aggregate_trade_id,
                    received_at_ms=trade.first_seen_at_ms,
                ) for trade in eligible)
                engines[symbol].observe(observations)
                processed_trades += len(observations)
            sequence = source_intervals[symbol]
            index = source_indexes[symbol]
            while boundary > sequence[index].end_boundary_time_ms:
                index += 1
            source_indexes[symbol] = index
            source_state = sequence[index].source_state
            current_states[symbol] = source_state
            source_counts[source_state] += 1
            endpoint_buckets[symbol] = engines[symbol].advance(boundary, source_state)
        if boundary < config.output_start_boundary_time_ms:
            continue
        movement_symbols = {}
        source_evidence = []
        for symbol in symbols:
            engine = engines[symbol]
            source_state = current_states[symbol]
            readiness = {window: engine.readiness(boundary, window, source_state)
                         for window in WINDOWS}
            historical = build_historical_window_inputs(
                visible_candles[symbol], boundary, config.movement_config)
            instrument = instruments[symbol]
            movement_symbols[symbol] = MarketMovementSymbolInput(
                symbol=symbol, instrument_id=instrument.instrument_id,
                instrument_compatible=instrument.instrument_compatible,
                readiness=readiness, historical=historical,
            )
            bucket = endpoint_buckets[symbol]
            source_evidence.append(SymbolSourceTimeEvidence(
                symbol, bucket.last_real_trade_time_ms,
                bucket.last_real_event_time_ms, bucket.last_received_at_ms,
            ))
        evaluation = calculate_market_movement(MarketMovementInput(
            evaluation_boundary_time_ms=boundary, universe=request.universe,
            symbols=movement_symbols, config=config.movement_config,
        ))
        if checkpoint is not None and boundary <= checkpoint.last_emitted_boundary_time_ms:
            continue
        for window in WINDOWS:
            if evaluation.windows[window].market_wide_eligible:
                eligible_counts[window] += 1
            else:
                ineligible_counts[window] += 1
        points.append(HistoricalMarketReplayPoint(
            point_id=_point_id(manifest.run_fingerprint, boundary),
            evaluation_boundary_time_ms=boundary,
            replay_clock_time_ms=replay_clock,
            movement_evaluation=evaluation,
            endpoint_buckets=tuple((symbol, endpoint_buckets[symbol]) for symbol in symbols),
            source_time_evidence=tuple(source_evidence),
            source_states=tuple((symbol, current_states[symbol]) for symbol in symbols),
        ))
    diagnostics = HistoricalReplayDiagnostics(
        engine_start_boundary_time_ms=config.engine_start_boundary_time_ms,
        output_start_boundary_time_ms=config.output_start_boundary_time_ms,
        output_end_boundary_time_ms=config.output_end_boundary_time_ms,
        processed_trade_count=processed_trades,
        deduplicated_trade_count=duplicate_count,
        late_trade_count=sum(engine.rejected_late_observations for engine in engines.values()),
        emitted_point_count=len(points),
        source_state_symbol_boundary_counts=tuple((state, source_counts[state])
                                                   for state in sorted(COLLECTOR_STATES)),
        market_wide_eligible_point_counts=tuple((window, eligible_counts[window])
                                                for window in WINDOWS),
        market_wide_ineligible_point_counts=tuple((window, ineligible_counts[window])
                                                  for window in WINDOWS),
    )
    final_checkpoint = (HistoricalReplayCheckpoint(
        manifest.run_fingerprint, points[-1].evaluation_boundary_time_ms,
        points[-1].point_id) if points else checkpoint)
    return HistoricalMarketReplayResult(manifest, tuple(points), diagnostics, final_checkpoint)


def to_market_state_experiment_points(
    result: HistoricalMarketReplayResult,
    plan: ReplayPartitionPlan,
) -> tuple[MarketStateExperimentPoint, ...]:
    """Label replay points for #75 without recalculating canonical movement."""
    if not isinstance(result, HistoricalMarketReplayResult) or not isinstance(plan, ReplayPartitionPlan):
        raise ValueError("result and plan must use replay contract types")
    manifest = result.manifest
    start = manifest.output_start_boundary_time_ms
    end = manifest.output_end_boundary_time_ms
    if not (start <= plan.development_end_boundary_time_ms
            < plan.validation_end_boundary_time_ms < end):
        raise ValueError("partition plan must divide the run into three ordered periods")
    points = tuple(MarketStateExperimentPoint(
        point.movement_evaluation, point.source_time_evidence,
        "development" if point.evaluation_boundary_time_ms <= plan.development_end_boundary_time_ms
        else "validation" if point.evaluation_boundary_time_ms <= plan.validation_end_boundary_time_ms
        else "test",
    ) for point in result.points)
    validate_experiment_points(points)
    return points
