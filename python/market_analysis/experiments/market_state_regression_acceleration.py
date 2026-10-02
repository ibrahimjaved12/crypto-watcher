"""EXP-75-05: causal OLS acceleration from canonical 5m symbol velocity."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import math
from statistics import median
from types import MappingProxyType
from typing import Iterable, Mapping

from ..market_episode_lifecycle import (
    MarketEpisodeLifecycleConfig,
    MarketEpisodeLifecycleState,
    MarketEpisodeTransition,
)
from ..movement_classifier import MarketClassificationEvaluation, MarketClassifierConfig
from ..movement_metrics import MarketMovementEvaluation, Metric, WINDOWS
from .market_state_common import (
    EXPERIMENT_EVALUATION_INTERVAL_MS,
    ExperimentPartition,
    MarketStateExperimentPoint,
    advance_canonical_branch,
    canonical_branch_for_point,
    directional_onset_count,
    episode_count,
    episode_spans,
    observed_points_for_partition,
    selected_points_for_partition,
    short_lived_episode_count,
    transition_counts,
    validate_experiment_points,
)


REGRESSION_ACCELERATION_ALGORITHM_VERSION = "market-movement-regression-acceleration-v1"
REGRESSION_CONFIG_VERSION_PREFIX = "market-movement-regression-acceleration-config-v1"
REGRESSION_WINDOW_POINTS = (12, 36, 60)
REGRESSION_EVALUATION_INTERVAL_MS = EXPERIMENT_EVALUATION_INTERVAL_MS
REGRESSION_INPUT_SERIES = "canonical-5m-velocity"
REGRESSION_ESTIMATOR = "ols"
REGRESSION_WEIGHTING = "uniform"
REGRESSION_NUMERICAL_TOL = 1e-12  # Comparisons in tests/invariants only.
REGRESSION_PACE_MATCH_WINDOW_MS = 300_000
REGRESSION_ACCELERATION_WARMING = "REGRESSION_ACCELERATION_WARMING"


def _config_version(window_points: int) -> str:
    return (f"{REGRESSION_CONFIG_VERSION_PREFIX}:input-{REGRESSION_INPUT_SERIES}"
            f":estimator-{REGRESSION_ESTIMATOR}:weighting-{REGRESSION_WEIGHTING}"
            f":spacing-{REGRESSION_EVALUATION_INTERVAL_MS}ms:points-{window_points}")


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except (OverflowError, ValueError):
        return None
    return value if math.isfinite(value) else None


@dataclass(frozen=True)
class RegressionAccelerationConfig:
    version: str
    window_points: int
    input_series: str = REGRESSION_INPUT_SERIES
    estimator: str = REGRESSION_ESTIMATOR
    weighting: str = REGRESSION_WEIGHTING
    spacing_ms: int = REGRESSION_EVALUATION_INTERVAL_MS

    def __post_init__(self):
        if (type(self.window_points) is not int
                or self.window_points not in REGRESSION_WINDOW_POINTS
                or self.input_series != REGRESSION_INPUT_SERIES
                or self.estimator != REGRESSION_ESTIMATOR
                or self.weighting != REGRESSION_WEIGHTING
                or type(self.spacing_ms) is not int
                or self.spacing_ms != REGRESSION_EVALUATION_INTERVAL_MS
                or self.version != _config_version(self.window_points)):
            raise ValueError("regression config must identify a preregistered uniform OLS window")


REGRESSION_CONFIG_60S = RegressionAccelerationConfig(_config_version(12), 12)
REGRESSION_CONFIG_180S = RegressionAccelerationConfig(_config_version(36), 36)
REGRESSION_CONFIG_300S = RegressionAccelerationConfig(_config_version(60), 60)
REGRESSION_CONFIGURATIONS = (
    REGRESSION_CONFIG_60S, REGRESSION_CONFIG_180S, REGRESSION_CONFIG_300S,
)


@dataclass(frozen=True)
class RegressionVelocitySample:
    evaluation_boundary_time_ms: int
    velocity: float

    def __post_init__(self):
        boundary = self.evaluation_boundary_time_ms
        if (type(boundary) is not int or boundary < 0
                or boundary % REGRESSION_EVALUATION_INTERVAL_MS):
            raise ValueError("regression sample boundary must be aligned")
        if _finite_number(self.velocity) is None:
            raise ValueError("regression sample velocity must be finite")


@dataclass(frozen=True)
class RegressionSymbolHistory:
    symbol: str
    samples: tuple[RegressionVelocitySample, ...]

    def __post_init__(self):
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("regression history symbol is required")
        samples = tuple(self.samples)
        if any(not isinstance(sample, RegressionVelocitySample) for sample in samples):
            raise ValueError("regression history contains an invalid sample")
        if any(right.evaluation_boundary_time_ms !=
               left.evaluation_boundary_time_ms + REGRESSION_EVALUATION_INTERVAL_MS
               for left, right in zip(samples, samples[1:])):
            raise ValueError("regression history samples must be consecutive")
        object.__setattr__(self, "samples", samples)


@dataclass(frozen=True)
class RegressionAccelerationCandidateState:
    candidate_algorithm_version: str
    candidate_config_version: str
    window_points: int
    baseline_movement_algorithm_version: str
    baseline_movement_config_version: str
    universe_id: str
    universe_version: str
    configured_universe: tuple[str, ...]
    provider: str
    exchange: str
    price_type: str
    last_evaluation_boundary_time_ms: int
    symbol_histories: tuple[RegressionSymbolHistory, ...]

    def __post_init__(self):
        if (self.candidate_algorithm_version != REGRESSION_ACCELERATION_ALGORITHM_VERSION
                or self.candidate_config_version != _config_version(self.window_points)
                or type(self.window_points) is not int
                or self.window_points not in REGRESSION_WINDOW_POINTS):
            raise ValueError("regression state config identity is invalid")
        for name in (
            "baseline_movement_algorithm_version", "baseline_movement_config_version",
            "universe_id", "universe_version", "provider", "exchange", "price_type",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} is required")
        boundary = self.last_evaluation_boundary_time_ms
        if (type(boundary) is not int or boundary < 0
                or boundary % REGRESSION_EVALUATION_INTERVAL_MS):
            raise ValueError("regression state boundary must be aligned")
        universe = tuple(self.configured_universe)
        histories = tuple(self.symbol_histories)
        if (any(not isinstance(symbol, str) or not symbol for symbol in universe)
                or len(set(universe)) != len(universe)
                or any(not isinstance(history, RegressionSymbolHistory) for history in histories)
                or tuple(history.symbol for history in histories) != universe):
            raise ValueError("regression histories must match the configured universe in order")
        for history in histories:
            if len(history.samples) > self.window_points:
                raise ValueError("regression history exceeds the configured window")
            if (history.samples and history.samples[-1].evaluation_boundary_time_ms
                    != boundary):
                raise ValueError("nonempty regression history must end at state boundary")
        object.__setattr__(self, "configured_universe", universe)
        object.__setattr__(self, "symbol_histories", histories)


def _same_scope(state, evaluation, config):
    return state is not None and (
        state.candidate_algorithm_version == REGRESSION_ACCELERATION_ALGORITHM_VERSION
        and state.candidate_config_version == config.version
        and state.window_points == config.window_points
        and state.baseline_movement_algorithm_version == evaluation.algorithm_version
        and state.baseline_movement_config_version == evaluation.config_version
        and state.universe_id == evaluation.universe_id
        and state.universe_version == evaluation.universe_version
        and state.configured_universe == evaluation.configured_universe
        and state.provider == evaluation.provider
        and state.exchange == evaluation.exchange
        and state.price_type == evaluation.price_type
        and evaluation.evaluation_boundary_time_ms ==
        state.last_evaluation_boundary_time_ms + REGRESSION_EVALUATION_INTERVAL_MS
    )


def _ols_velocity_slope(samples: tuple[RegressionVelocitySample, ...]) -> float:
    current_boundary = samples[-1].evaluation_boundary_time_ms
    x = tuple((sample.evaluation_boundary_time_ms - current_boundary) / 1000.0
              for sample in samples)
    y = tuple(float(sample.velocity) for sample in samples)
    x_bar = sum(x) / len(x)
    y_bar = sum(y) / len(y)
    denominator = sum((item - x_bar) ** 2 for item in x)
    if not math.isfinite(denominator) or denominator <= 0:
        raise ValueError("regression OLS denominator must be positive and finite")
    numerator = sum((xi - x_bar) * (yi - y_bar) for xi, yi in zip(x, y))
    slope = numerator / denominator
    if not math.isfinite(slope):
        raise ValueError("regression OLS slope must be finite")
    return slope


def transform_market_movement_with_regression_acceleration(
    evaluation: MarketMovementEvaluation,
    config: RegressionAccelerationConfig,
    state: RegressionAccelerationCandidateState | None = None,
) -> tuple[MarketMovementEvaluation, RegressionAccelerationCandidateState]:
    """Replace only included 5m acceleration using causal velocity histories."""
    if not isinstance(evaluation, MarketMovementEvaluation):
        raise ValueError("evaluation must be MarketMovementEvaluation")
    if not isinstance(config, RegressionAccelerationConfig):
        raise ValueError("config must be RegressionAccelerationConfig")
    if state is not None and not isinstance(state, RegressionAccelerationCandidateState):
        raise ValueError("state must be RegressionAccelerationCandidateState or None")
    boundary = evaluation.evaluation_boundary_time_ms
    if (type(boundary) is not int or boundary < 0
            or boundary % REGRESSION_EVALUATION_INTERVAL_MS):
        raise ValueError("evaluation boundary must be aligned")
    if set(evaluation.windows) != set(WINDOWS):
        raise ValueError("regression transform requires 1m, 5m, and 15m windows")
    primary = evaluation.windows[5]
    symbols = {item.symbol: item for item in primary.symbols}
    if len(symbols) != len(primary.symbols):
        raise ValueError("5m symbol results must be unique")
    prior = ({history.symbol: history.samples for history in state.symbol_histories}
             if _same_scope(state, evaluation, config) else {})
    histories = []
    candidate_acceleration = {}
    for symbol in evaluation.configured_universe:
        item = symbols.get(symbol)
        velocity = (_finite_number(item.velocity.value)
                    if item is not None and item.included and item.velocity.available
                    else None)
        if velocity is None:
            samples = ()
        else:
            samples = (*prior.get(symbol, ()),
                       RegressionVelocitySample(boundary, velocity))[-config.window_points:]
        histories.append(RegressionSymbolHistory(symbol, samples))
        if item is not None and item.included:
            candidate_acceleration[symbol] = (
                Metric.present(_ols_velocity_slope(samples))
                if len(samples) == config.window_points else
                Metric.missing(REGRESSION_ACCELERATION_WARMING)
            )
    next_state = RegressionAccelerationCandidateState(
        REGRESSION_ACCELERATION_ALGORITHM_VERSION, config.version, config.window_points,
        evaluation.algorithm_version, evaluation.config_version,
        evaluation.universe_id, evaluation.universe_version,
        evaluation.configured_universe, evaluation.provider, evaluation.exchange,
        evaluation.price_type, boundary, tuple(histories),
    )
    windows = {}
    for minute in WINDOWS:
        snapshot = evaluation.windows[minute]
        if minute == 5:
            snapshot = replace(snapshot, symbols=tuple(
                replace(item, acceleration=candidate_acceleration[item.symbol])
                if item.included and item.symbol in candidate_acceleration else item
                for item in snapshot.symbols
            ))
        windows[minute] = replace(
            snapshot, algorithm_version=REGRESSION_ACCELERATION_ALGORITHM_VERSION,
            config_version=config.version,
        )
    candidate = replace(
        evaluation, algorithm_version=REGRESSION_ACCELERATION_ALGORITHM_VERSION,
        config_version=config.version, windows=windows,
    )
    return candidate, next_state


@dataclass(frozen=True)
class RegressionAccelerationObservation:
    symbol: str
    baseline_acceleration: Metric[float]
    candidate_acceleration: Metric[float]
    velocity: Metric[float]
    history_count: int
    regression_available: bool


@dataclass(frozen=True)
class PairedMarketStateRegressionAccelerationPoint:
    evaluation_boundary_time_ms: int
    partition: ExperimentPartition
    baseline_evaluation: MarketMovementEvaluation
    candidate_evaluation: MarketMovementEvaluation
    baseline_classification: MarketClassificationEvaluation
    candidate_classification: MarketClassificationEvaluation
    baseline_lifecycle_state: MarketEpisodeLifecycleState
    candidate_lifecycle_state: MarketEpisodeLifecycleState
    baseline_transitions: tuple[MarketEpisodeTransition, ...]
    candidate_transitions: tuple[MarketEpisodeTransition, ...]
    regression_state: RegressionAccelerationCandidateState
    regression_observations: tuple[RegressionAccelerationObservation, ...]

    @property
    def baseline_primary(self):
        return self.baseline_classification.windows[5]

    @property
    def candidate_primary(self):
        return self.candidate_classification.windows[5]


@dataclass(frozen=True)
class PaceChangeEvent:
    evaluation_boundary_time_ms: int
    target_pace: str
    partition: str


@dataclass(frozen=True)
class PaceChangeMatch:
    baseline_event: PaceChangeEvent
    candidate_event: PaceChangeEvent
    signed_delta_ms: int


@dataclass(frozen=True)
class PaceChangeMatchResult:
    matches: tuple[PaceChangeMatch, ...]
    unmatched_baseline_count: int
    unmatched_candidate_count: int
    median_signed_delta_ms: float | None
    median_absolute_delta_ms: float | None


def _pace_change_events(points: tuple, branch: str) -> tuple[PaceChangeEvent, ...]:
    prior = None
    events = []
    for point in points:
        pace = getattr(point, f"{branch}_classification").windows[5].pace
        if not pace.available or pace.value not in ("ACCELERATING", "DECELERATING", "MIXED"):
            prior = None
            continue
        if prior is not None and pace.value != prior:
            events.append(PaceChangeEvent(
                point.evaluation_boundary_time_ms, pace.value, point.partition,
            ))
        prior = pace.value
    return tuple(events)


def _match_pace_change_events(
    selected_baseline: tuple[PaceChangeEvent, ...],
    observed_candidate: tuple[PaceChangeEvent, ...],
    selected_candidate: tuple[PaceChangeEvent, ...],
) -> PaceChangeMatchResult:
    used = set()
    matches = []
    for baseline in selected_baseline:
        options = (
            (abs(candidate.evaluation_boundary_time_ms - baseline.evaluation_boundary_time_ms),
             candidate.evaluation_boundary_time_ms, index, candidate)
            for index, candidate in enumerate(observed_candidate)
            if index not in used and candidate.target_pace == baseline.target_pace
            and abs(candidate.evaluation_boundary_time_ms - baseline.evaluation_boundary_time_ms)
            <= REGRESSION_PACE_MATCH_WINDOW_MS
        )
        best = min(options, default=None)
        if best is not None:
            used.add(best[2])
            matches.append(PaceChangeMatch(
                baseline, best[3],
                best[3].evaluation_boundary_time_ms - baseline.evaluation_boundary_time_ms,
            ))
    matched_candidate = {observed_candidate[index] for index in used}
    signed = tuple(match.signed_delta_ms for match in matches)
    return PaceChangeMatchResult(
        tuple(matches), len(selected_baseline) - len(matches),
        sum(candidate not in matched_candidate for candidate in selected_candidate),
        float(median(signed)) if signed else None,
        float(median(tuple(abs(delta) for delta in signed))) if signed else None,
    )


def _pace_count(points, branch):
    counts = Counter(
        (pace.value if pace.available else "UNAVAILABLE")
        for point in points
        for pace in (getattr(point, f"{branch}_primary").pace,)
    )
    return tuple(sorted(counts.items()))


def _metric_difference(left: Metric, right: Metric, *, breadth: bool = False):
    if not left.available or not right.available:
        return None
    left_value = left.value.fraction if breadth else left.value
    right_value = right.value.fraction if breadth else right.value
    return abs(right_value - left_value)


def _median_or_none(values):
    return float(median(values)) if values else None


def _sign(value: float) -> int:
    return (value > 0) - (value < 0)


@dataclass(frozen=True)
class RegressionAccelerationComparisonSummary:
    partition: str
    evaluation_count: int
    candidate_regression_ready_count: int
    candidate_regression_warming_count: int
    candidate_acceleration_unavailable_count: int
    baseline_pace_counts: tuple[tuple[str, int], ...]
    candidate_pace_counts: tuple[tuple[str, int], ...]
    both_pace_available_count: int
    pace_disagreement_count: int
    pace_disagreement_fraction: float
    baseline_pace_available_candidate_unavailable_count: int
    candidate_pace_available_baseline_unavailable_count: int
    symbol_acceleration_sign_comparison_count: int
    symbol_acceleration_sign_disagreement_count: int
    symbol_acceleration_sign_disagreement_fraction: float
    median_absolute_median_acceleration_difference: float | None
    median_absolute_positive_acceleration_breadth_difference: float | None
    median_absolute_negative_acceleration_breadth_difference: float | None
    baseline_transition_counts: tuple[tuple[str, int], ...]
    candidate_transition_counts: tuple[tuple[str, int], ...]
    baseline_episode_count: int
    candidate_episode_count: int
    baseline_directional_onset_count: int
    candidate_directional_onset_count: int
    baseline_short_lived_closed_episode_count: int
    candidate_short_lived_closed_episode_count: int
    baseline_strengthened_count: int
    candidate_strengthened_count: int
    baseline_weakened_count: int
    candidate_weakened_count: int
    both_active_episode_boundary_count: int
    same_active_direction_boundary_count: int
    same_active_direction_overlap_fraction: float
    baseline_pace_change_event_count: int
    candidate_pace_change_event_count: int
    matched_baseline_pace_change_count: int
    unmatched_baseline_pace_change_count: int
    unmatched_candidate_pace_change_count: int
    median_signed_pace_change_delta_ms: float | None
    median_absolute_pace_change_delta_ms: float | None
    direction_state_disagreement_count: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class MarketStateRegressionAccelerationExperimentResult:
    regression_config: RegressionAccelerationConfig
    paired_points: tuple[PairedMarketStateRegressionAccelerationPoint, ...]
    summaries: Mapping[str, RegressionAccelerationComparisonSummary]

    def __post_init__(self):
        object.__setattr__(self, "paired_points", tuple(self.paired_points))
        object.__setattr__(self, "summaries", MappingProxyType(dict(self.summaries)))


def _summary(points: tuple, partition: str) -> RegressionAccelerationComparisonSummary:
    selected = selected_points_for_partition(points, partition)
    observed = observed_points_for_partition(points, partition)
    baseline_spans = episode_spans(observed, "baseline")
    candidate_spans = episode_spans(observed, "candidate")
    baseline_events = _pace_change_events(observed, "baseline")
    candidate_events = _pace_change_events(observed, "candidate")
    selected_boundaries = {point.evaluation_boundary_time_ms for point in selected}
    selected_baseline_events = tuple(event for event in baseline_events
                                     if event.evaluation_boundary_time_ms in selected_boundaries)
    selected_candidate_events = tuple(event for event in candidate_events
                                      if event.evaluation_boundary_time_ms in selected_boundaries)
    pace_matches = _match_pace_change_events(
        selected_baseline_events, candidate_events, selected_candidate_events)
    both_pace = tuple(point for point in selected
                      if point.baseline_primary.pace.available
                      and point.candidate_primary.pace.available)
    pace_disagree = sum(point.baseline_primary.pace.value != point.candidate_primary.pace.value
                        for point in both_pace)
    comparable_symbols = tuple(
        item for point in selected for item in point.regression_observations
        if item.regression_available and item.baseline_acceleration.available
    )
    sign_disagree = sum(_sign(item.baseline_acceleration.value) !=
                        _sign(item.candidate_acceleration.value)
                        for item in comparable_symbols)
    def differences(field, breadth=False):
        values = tuple(
            value for point in selected
            for value in (_metric_difference(
                getattr(point.baseline_primary, field),
                getattr(point.candidate_primary, field), breadth=breadth,
            ),) if value is not None
        )
        return _median_or_none(values)
    both_active = tuple(point for point in selected
                        if point.baseline_lifecycle_state.active_episode is not None
                        and point.candidate_lifecycle_state.active_episode is not None)
    same_active = sum(
        point.baseline_lifecycle_state.active_episode.direction ==
        point.candidate_lifecycle_state.active_episode.direction
        for point in both_active
    )
    baseline_counts = transition_counts(selected, "baseline")
    candidate_counts = transition_counts(selected, "candidate")
    candidate_unavailable = sum(not point.candidate_primary.median_acceleration.available
                                for point in selected)
    warming = sum(
        not point.candidate_primary.median_acceleration.available
        and any(item.candidate_acceleration.reason == REGRESSION_ACCELERATION_WARMING
                for item in point.regression_observations)
        and all(item.candidate_acceleration.reason != REGRESSION_ACCELERATION_WARMING
                or _finite_number(item.velocity.value) is not None
                for item in point.regression_observations)
        for point in selected
    )
    return RegressionAccelerationComparisonSummary(
        partition=partition, evaluation_count=len(selected),
        candidate_regression_ready_count=sum(point.candidate_primary.median_acceleration.available
                                             for point in selected),
        candidate_regression_warming_count=warming,
        candidate_acceleration_unavailable_count=candidate_unavailable,
        baseline_pace_counts=_pace_count(selected, "baseline"),
        candidate_pace_counts=_pace_count(selected, "candidate"),
        both_pace_available_count=len(both_pace),
        pace_disagreement_count=pace_disagree,
        pace_disagreement_fraction=pace_disagree / len(both_pace) if both_pace else 0.0,
        baseline_pace_available_candidate_unavailable_count=sum(
            point.baseline_primary.pace.available and not point.candidate_primary.pace.available
            for point in selected),
        candidate_pace_available_baseline_unavailable_count=sum(
            point.candidate_primary.pace.available and not point.baseline_primary.pace.available
            for point in selected),
        symbol_acceleration_sign_comparison_count=len(comparable_symbols),
        symbol_acceleration_sign_disagreement_count=sign_disagree,
        symbol_acceleration_sign_disagreement_fraction=(
            sign_disagree / len(comparable_symbols) if comparable_symbols else 0.0),
        median_absolute_median_acceleration_difference=differences("median_acceleration"),
        median_absolute_positive_acceleration_breadth_difference=differences(
            "positive_acceleration_breadth", True),
        median_absolute_negative_acceleration_breadth_difference=differences(
            "negative_acceleration_breadth", True),
        baseline_transition_counts=baseline_counts,
        candidate_transition_counts=candidate_counts,
        baseline_episode_count=episode_count(baseline_spans, selected, partition),
        candidate_episode_count=episode_count(candidate_spans, selected, partition),
        baseline_directional_onset_count=directional_onset_count(selected, "baseline"),
        candidate_directional_onset_count=directional_onset_count(selected, "candidate"),
        baseline_short_lived_closed_episode_count=short_lived_episode_count(
            baseline_spans, selected, partition),
        candidate_short_lived_closed_episode_count=short_lived_episode_count(
            candidate_spans, selected, partition),
        baseline_strengthened_count=dict(baseline_counts).get("STRENGTHENED", 0),
        candidate_strengthened_count=dict(candidate_counts).get("STRENGTHENED", 0),
        baseline_weakened_count=dict(baseline_counts).get("WEAKENED", 0),
        candidate_weakened_count=dict(candidate_counts).get("WEAKENED", 0),
        both_active_episode_boundary_count=len(both_active),
        same_active_direction_boundary_count=same_active,
        same_active_direction_overlap_fraction=same_active / len(both_active) if both_active else 0.0,
        baseline_pace_change_event_count=len(selected_baseline_events),
        candidate_pace_change_event_count=len(selected_candidate_events),
        matched_baseline_pace_change_count=len(pace_matches.matches),
        unmatched_baseline_pace_change_count=pace_matches.unmatched_baseline_count,
        unmatched_candidate_pace_change_count=pace_matches.unmatched_candidate_count,
        median_signed_pace_change_delta_ms=pace_matches.median_signed_delta_ms,
        median_absolute_pace_change_delta_ms=pace_matches.median_absolute_delta_ms,
        direction_state_disagreement_count=tuple(
            (minute, sum(point.baseline_classification.windows[minute].direction_state !=
                         point.candidate_classification.windows[minute].direction_state
                         for point in selected))
            for minute in WINDOWS
        ),
    )


def run_market_state_regression_acceleration_experiment(
    points: Iterable[MarketStateExperimentPoint],
    config: RegressionAccelerationConfig,
    *,
    classifier_config: MarketClassifierConfig | None = None,
    lifecycle_config: MarketEpisodeLifecycleConfig | None = None,
    canonical_branch_by_boundary: Mapping[int, tuple] | None = None,
) -> MarketStateRegressionAccelerationExperimentResult:
    """Run independent canonical #72/#73 branches over explicit replay points."""
    if not isinstance(config, RegressionAccelerationConfig):
        raise ValueError("config must be RegressionAccelerationConfig")
    classifier_config = MarketClassifierConfig() if classifier_config is None else classifier_config
    lifecycle_config = MarketEpisodeLifecycleConfig() if lifecycle_config is None else lifecycle_config
    if not isinstance(classifier_config, MarketClassifierConfig):
        raise ValueError("classifier_config must be MarketClassifierConfig")
    if not isinstance(lifecycle_config, MarketEpisodeLifecycleConfig):
        raise ValueError("lifecycle_config must be MarketEpisodeLifecycleConfig")
    points = tuple(points)
    validate_experiment_points(points)
    baseline_state = None
    candidate_lifecycle_state = None
    regression_state = None
    paired = []
    for point in points:
        evaluation = point.movement_evaluation
        candidate, regression_state = transform_market_movement_with_regression_acceleration(
            evaluation, config, regression_state)
        baseline_classification, baseline_result = canonical_branch_for_point(
            evaluation, point.source_time_evidence, baseline_state,
            classifier_config, lifecycle_config, canonical_branch_by_boundary)
        candidate_classification, candidate_result = advance_canonical_branch(
            candidate, point.source_time_evidence, candidate_lifecycle_state,
            classifier_config, lifecycle_config)
        if any(baseline_classification.windows[minute].direction_state !=
               candidate_classification.windows[minute].direction_state
               for minute in WINDOWS):
            raise ValueError("regression acceleration changed canonical direction state")
        baseline_symbols = {item.symbol: item for item in evaluation.windows[5].symbols}
        candidate_symbols = {item.symbol: item for item in candidate.windows[5].symbols}
        observations = tuple(
            RegressionAccelerationObservation(
                symbol=history.symbol,
                baseline_acceleration=(baseline_symbols[history.symbol].acceleration
                                       if history.symbol in baseline_symbols else
                                       Metric.missing("SYMBOL_RESULT_UNAVAILABLE")),
                candidate_acceleration=(candidate_symbols[history.symbol].acceleration
                                        if history.symbol in candidate_symbols else
                                        Metric.missing("SYMBOL_RESULT_UNAVAILABLE")),
                velocity=(baseline_symbols[history.symbol].velocity
                          if history.symbol in baseline_symbols else
                          Metric.missing("SYMBOL_RESULT_UNAVAILABLE")),
                history_count=len(history.samples),
                regression_available=(history.symbol in candidate_symbols
                                      and candidate_symbols[history.symbol].included
                                      and candidate_symbols[history.symbol].acceleration.available),
            ) for history in regression_state.symbol_histories
        )
        paired.append(PairedMarketStateRegressionAccelerationPoint(
            evaluation.evaluation_boundary_time_ms, point.partition, evaluation, candidate,
            baseline_classification, candidate_classification,
            baseline_result.next_state, candidate_result.next_state,
            baseline_result.transitions, candidate_result.transitions,
            regression_state, observations,
        ))
        baseline_state = baseline_result.next_state
        candidate_lifecycle_state = candidate_result.next_state
    paired = tuple(paired)
    summaries = {partition: _summary(paired, partition)
                 for partition in ("all", "development", "validation", "test")}
    return MarketStateRegressionAccelerationExperimentResult(config, paired, summaries)
