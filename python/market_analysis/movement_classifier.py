"""Pure, deterministic #72 classification of canonical #71 movement evidence.

The caller supplies episode context and source provenance. This module neither
acquires history nor confirms or changes episode lifecycle state.
"""

from dataclasses import dataclass
import math
from statistics import median
from types import MappingProxyType
from typing import Mapping

from .movement_metrics import (
    BreadthSide, ExcludedSymbol, MarketMovementEvaluation,
    MarketMovementWindowResult, Metric, WindowBreadth, WINDOWS,
)


ALGORITHM_VERSION = "market-state-classifier-v1"
DEFAULT_CONFIG_VERSION = "market-state-classifier-config-v1"
HORIZON_ROLES = MappingProxyType({1: "RAPID", 5: "PRIMARY", 15: "PERSISTENCE"})
_BROAD_DIRECTIONS = frozenset(("BROAD_RISE", "BROAD_DROP"))
_WARMING_REASONS = frozenset(("WARMING_INSUFFICIENT_LIVE_HISTORY",))


@dataclass(frozen=True)
class MarketClassifierConfig:
    version: str = DEFAULT_CONFIG_VERSION
    directional_breadth: float = 0.70
    material_breadth: float = 0.50
    normalized_movement: float = 0.50
    acceleration_breadth: float = 0.60
    isolated_outlier_breadth_disagreement: float = 0.50

    def __post_init__(self):
        if not isinstance(self.version, str) or not self.version:
            raise ValueError("classifier config version is required")
        for name in ("directional_breadth", "material_breadth",
                     "acceleration_breadth", "isolated_outlier_breadth_disagreement"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 0 < value <= 1):
                raise ValueError(f"{name} must be finite and in (0, 1]")
        value = self.normalized_movement
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0):
            raise ValueError("normalized_movement must be finite and positive")


@dataclass(frozen=True)
class MarketWindowClassificationContext:
    # None means the caller has no trustworthy source/event timestamp.
    source_timestamp_ms: int | None = None
    event_timestamp_ms: int | None = None
    prior_confirmed_episode_direction: str | None = None

    def __post_init__(self):
        for name in ("source_timestamp_ms", "event_timestamp_ms"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a nonnegative integer or None")
        if (self.prior_confirmed_episode_direction is not None
                and self.prior_confirmed_episode_direction not in _BROAD_DIRECTIONS):
            raise ValueError("prior episode direction must be BROAD_RISE, BROAD_DROP, or None")


@dataclass(frozen=True)
class MarketClassificationContext:
    windows: Mapping[int, MarketWindowClassificationContext]

    def __post_init__(self):
        if set(self.windows) != set(WINDOWS):
            raise ValueError("classification context must contain the 1m, 5m, and 15m windows")
        if any(not isinstance(value, MarketWindowClassificationContext)
               for value in self.windows.values()):
            raise ValueError("window context has the wrong type")
        object.__setattr__(self, "windows", MappingProxyType(dict(self.windows)))


@dataclass(frozen=True)
class SymbolVolumeContext:
    symbol: str
    current_notional_volume: Metric
    rvol: Metric


@dataclass(frozen=True)
class IsolatedOutlier:
    symbol: str
    direction: str
    raw_return: float
    historical_z: float
    cross_sectional_z: float
    same_direction_breadth_count: int
    same_direction_breadth_fraction: float
    same_direction_breadth_denominator: int


@dataclass(frozen=True)
class ReversalCandidate:
    prior_confirmed_episode_direction: str
    current_direction: str
    directional_breadth: BreadthSide
    material_breadth: BreadthSide
    median_raw_return: float
    median_normalized_movement: float


@dataclass(frozen=True)
class MarketWindowClassification:
    window_minutes: int
    horizon_role: str
    is_primary: bool
    direction_state: str
    pace: str
    prior_confirmed_episode_direction: str | None
    reversal_candidate: ReversalCandidate | None
    isolated_outliers: tuple[IsolatedOutlier, ...]
    breadth: WindowBreadth
    median_raw_return: Metric[float]
    median_normalized_movement: Metric[float]
    median_acceleration: Metric[float]
    positive_acceleration_breadth: Metric[BreadthSide]
    negative_acceleration_breadth: Metric[BreadthSide]
    dispersion_mad_normalized_movement: Metric[float]
    trimmed_mean_normalized_movement: Metric[float]
    liquidity_weighted_normalized_movement: Metric[float]
    liquidity_weights: Metric[tuple[tuple[str, float], ...]]
    volume_context: tuple[SymbolVolumeContext, ...]
    market_wide_eligible: bool
    eligible_count: int
    eligible_fraction: float
    configured_universe: tuple[str, ...]
    included_symbols: tuple[str, ...]
    excluded_symbols: tuple[ExcludedSymbol, ...]
    availability_reasons: tuple[str, ...]
    classifier_algorithm_version: str
    classifier_config_version: str
    movement_algorithm_version: str
    movement_config_version: str
    universe_id: str
    universe_version: str
    provider: str
    exchange: str
    price_type: str
    evaluation_boundary_time_ms: int
    source_timestamp_ms: int | None
    event_timestamp_ms: int | None
    movement_snapshot: MarketMovementWindowResult


@dataclass(frozen=True)
class MarketClassificationEvaluation:
    classifier_algorithm_version: str
    classifier_config_version: str
    movement_algorithm_version: str
    movement_config_version: str
    universe_id: str
    universe_version: str
    provider: str
    exchange: str
    price_type: str
    evaluation_boundary_time_ms: int
    primary_window_minutes: int
    windows: Mapping[int, MarketWindowClassification]

    def __post_init__(self):
        object.__setattr__(self, "windows", MappingProxyType(dict(self.windows)))


def _availability_reasons(window):
    reasons = tuple(dict.fromkeys(reason for excluded in window.excluded_symbols
                                  for reason in excluded.reasons))
    if not reasons and not window.market_wide_eligible:
        return (window.breadth.reason or "MARKET_UNIVERSE_INELIGIBLE",)
    return reasons


def _ineligible_state(window):
    # If there are fewer than five configured symbols, warm-up alone cannot
    # satisfy the issue's minimum universe size.
    if (len(window.configured_universe) >= 5 and window.excluded_symbols
            and all(excluded.reasons and set(excluded.reasons) <= _WARMING_REASONS
                    for excluded in window.excluded_symbols)):
        return "WARMING"
    return "UNAVAILABLE"


def _direction(window, config):
    if not window.market_wide_eligible:
        return _ineligible_state(window)
    breadth = window.breadth
    aggregates = window.aggregates
    needed = (breadth.rising, breadth.falling, breadth.material_rising,
              breadth.material_falling, aggregates.median_raw_return,
              aggregates.median_normalized_movement)
    if not breadth.available or not all(metric.available for metric in needed):
        return "UNAVAILABLE"
    raw = aggregates.median_raw_return.value
    normalized = aggregates.median_normalized_movement.value
    if (breadth.rising.value.fraction >= config.directional_breadth
            and breadth.material_rising.value.fraction >= config.material_breadth
            and raw > 0 and normalized >= config.normalized_movement):
        return "BROAD_RISE"
    if (breadth.falling.value.fraction >= config.directional_breadth
            and breadth.material_falling.value.fraction >= config.material_breadth
            and raw < 0 and normalized <= -config.normalized_movement):
        return "BROAD_DROP"
    return "NEUTRAL"


def _acceleration_evidence(window):
    included = tuple(item for item in window.symbols if item.included)
    if (not included or len(included) != window.eligible_count
            or any(not item.acceleration.available for item in included)):
        missing = Metric.missing("ACCELERATION_UNAVAILABLE")
        return missing, missing, missing
    values = tuple(item.acceleration.value for item in included)
    denominator = window.eligible_count
    positive = sum(value > 0 for value in values)
    negative = sum(value < 0 for value in values)
    return (Metric.present(median(values)),
            Metric.present(BreadthSide(positive, positive / denominator)),
            Metric.present(BreadthSide(negative, negative / denominator)))


def _pace(direction, median_acceleration, positive, negative, config):
    if direction not in _BROAD_DIRECTIONS:
        return "NOT_APPLICABLE"
    if not all(metric.available for metric in (median_acceleration, positive, negative)):
        return "UNAVAILABLE"
    accelerating = positive if direction == "BROAD_RISE" else negative
    decelerating = negative if direction == "BROAD_RISE" else positive
    sign = 1 if direction == "BROAD_RISE" else -1
    if (accelerating.value.fraction >= config.acceleration_breadth
            and sign * median_acceleration.value > 0):
        return "ACCELERATING"
    if (decelerating.value.fraction >= config.acceleration_breadth
            and sign * median_acceleration.value < 0):
        return "DECELERATING"
    return "MIXED"


def _isolated_outliers(window, config):
    if not window.breadth.available:
        return ()
    found = []
    for item in window.symbols:
        if not item.included or not item.outlier_candidate or not item.direction.available:
            continue
        if item.direction.value == "RISING":
            side = window.breadth.rising
        elif item.direction.value == "FALLING":
            side = window.breadth.falling
        else:
            continue
        if not side.available or side.value.fraction >= config.isolated_outlier_breadth_disagreement:
            continue
        if not all(metric.available for metric in
                   (item.current_return, item.normalized_z, item.cross_sectional_z)):
            continue
        found.append(IsolatedOutlier(
            item.symbol, item.direction.value, item.current_return.value,
            item.normalized_z.value, item.cross_sectional_z.value,
            side.value.count, side.value.fraction, window.breadth.denominator,
        ))
    return tuple(found)


def _reversal(direction, prior, window):
    if ((direction == "BROAD_RISE" and prior != "BROAD_DROP")
            or (direction == "BROAD_DROP" and prior != "BROAD_RISE")
            or direction not in _BROAD_DIRECTIONS):
        return None
    side = window.breadth.rising if direction == "BROAD_RISE" else window.breadth.falling
    material = (window.breadth.material_rising if direction == "BROAD_RISE"
                else window.breadth.material_falling)
    return ReversalCandidate(
        prior, direction, side.value, material.value,
        window.aggregates.median_raw_return.value,
        window.aggregates.median_normalized_movement.value,
    )


def classify_market_movement(
    evaluation: MarketMovementEvaluation,
    context: MarketClassificationContext,
    config: MarketClassifierConfig | None = None,
) -> MarketClassificationEvaluation:
    """Classify #71's exact snapshots using only explicit deterministic context."""
    if not isinstance(evaluation, MarketMovementEvaluation):
        raise ValueError("evaluation must be MarketMovementEvaluation")
    if not isinstance(context, MarketClassificationContext):
        raise ValueError("context must be MarketClassificationContext")
    config = MarketClassifierConfig() if config is None else config
    if not isinstance(config, MarketClassifierConfig):
        raise ValueError("config must be MarketClassifierConfig")
    if set(evaluation.windows) != set(WINDOWS):
        raise ValueError("movement evaluation must contain the 1m, 5m, and 15m windows")
    results = {}
    for window_minutes in WINDOWS:
        snapshot = evaluation.windows[window_minutes]
        if snapshot.window_minutes != window_minutes:
            raise ValueError("movement window key and window_minutes disagree")
        supplied = context.windows[window_minutes]
        direction = _direction(snapshot, config)
        middle, positive, negative = _acceleration_evidence(snapshot)
        aggregates = snapshot.aggregates
        results[window_minutes] = MarketWindowClassification(
            window_minutes=window_minutes, horizon_role=HORIZON_ROLES[window_minutes],
            is_primary=window_minutes == 5, direction_state=direction,
            pace=_pace(direction, middle, positive, negative, config),
            prior_confirmed_episode_direction=supplied.prior_confirmed_episode_direction,
            reversal_candidate=_reversal(direction, supplied.prior_confirmed_episode_direction, snapshot),
            isolated_outliers=_isolated_outliers(snapshot, config),
            breadth=snapshot.breadth,
            median_raw_return=aggregates.median_raw_return,
            median_normalized_movement=aggregates.median_normalized_movement,
            median_acceleration=middle,
            positive_acceleration_breadth=positive,
            negative_acceleration_breadth=negative,
            dispersion_mad_normalized_movement=aggregates.dispersion_mad_normalized_movement,
            trimmed_mean_normalized_movement=aggregates.trimmed_mean_normalized_movement,
            liquidity_weighted_normalized_movement=aggregates.liquidity_weighted_normalized_movement,
            liquidity_weights=aggregates.liquidity_weights,
            volume_context=tuple(SymbolVolumeContext(item.symbol, item.current_notional_volume, item.rvol)
                                 for item in snapshot.symbols if item.included),
            market_wide_eligible=snapshot.market_wide_eligible,
            eligible_count=snapshot.eligible_count, eligible_fraction=snapshot.eligible_fraction,
            configured_universe=snapshot.configured_universe,
            included_symbols=snapshot.included_symbols,
            excluded_symbols=snapshot.excluded_symbols,
            availability_reasons=_availability_reasons(snapshot),
            classifier_algorithm_version=ALGORITHM_VERSION,
            classifier_config_version=config.version,
            movement_algorithm_version=snapshot.algorithm_version,
            movement_config_version=snapshot.config_version,
            universe_id=snapshot.universe_id, universe_version=snapshot.universe_version,
            provider=snapshot.provider, exchange=snapshot.exchange, price_type=snapshot.price_type,
            evaluation_boundary_time_ms=snapshot.evaluation_boundary_time_ms,
            source_timestamp_ms=supplied.source_timestamp_ms,
            event_timestamp_ms=supplied.event_timestamp_ms,
            movement_snapshot=snapshot,
        )
    return MarketClassificationEvaluation(
        ALGORITHM_VERSION, config.version, evaluation.algorithm_version,
        evaluation.config_version, evaluation.universe_id, evaluation.universe_version,
        evaluation.provider, evaluation.exchange, evaluation.price_type,
        evaluation.evaluation_boundary_time_ms, 5, results,
    )
