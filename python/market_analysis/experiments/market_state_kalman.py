"""Independent EXP-75-03 local-linear-trend Kalman experiment."""

from __future__ import annotations

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
from ..movement_classifier import (
    MarketClassificationEvaluation,
    MarketClassifierConfig,
    SymbolSourceTimeEvidence,
)
from ..movement_metrics import MarketMovementEvaluation, Metric, WINDOWS
from .market_state_common import (
    ExperimentPartition,
    MarketStateExperimentPoint,
    advance_canonical_branch,
    directional_onset_count,
    episode_count,
    episode_spans,
    match_directional_onsets,
    observed_points_for_partition,
    selected_points_for_partition,
    short_lived_episode_count,
    transition_counts,
    validate_experiment_points,
)


KALMAN_ALGORITHM_VERSION = "market-movement-kalman-local-linear-trend-v1"
KALMAN_CONFIG_VERSION_PREFIX = (
    "market-movement-kalman-local-linear-trend-config-v1"
)
KALMAN_EVALUATION_INTERVAL_MS = 5_000
KALMAN_MEASUREMENT_VARIANCE = 0.25
KALMAN_PROCESS_NOISE_VARIANCES = (0.0025, 0.0100, 0.0400)
_KALMAN_COVARIANCE_TOL = 1e-12


def _config_version(q: float, r: float) -> str:
    return f"{KALMAN_CONFIG_VERSION_PREFIX}:q-{q:.4f}:r-{r:.2f}"


def _finite(value: float) -> bool:
    return (not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value))


def _validate_covariance(p00: float, p01: float, p11: float) -> None:
    if not all(_finite(value) for value in (p00, p01, p11)):
        raise ValueError("Kalman covariance must be finite")
    determinant = p00 * p11 - p01 * p01
    if p00 < -_KALMAN_COVARIANCE_TOL or p11 < -_KALMAN_COVARIANCE_TOL:
        raise ValueError("Kalman covariance is not positive semidefinite")
    if determinant < -_KALMAN_COVARIANCE_TOL:
        raise ValueError("Kalman covariance is not positive semidefinite")


@dataclass(frozen=True)
class KalmanConfig:
    """One fixed preregistered process-noise assumption."""

    version: str
    q: float
    r: float

    def __post_init__(self):
        if not isinstance(self.version, str) or not self.version:
            raise ValueError("Kalman config version is required")
        if not _finite(self.q) or not _finite(self.r) or self.q <= 0 or self.r <= 0:
            raise ValueError("Kalman q and r must be finite and positive")
        if (self.q, self.r) not in tuple(
                (q, KALMAN_MEASUREMENT_VARIANCE)
                for q in KALMAN_PROCESS_NOISE_VARIANCES):
            raise ValueError("Kalman configuration is not preregistered")
        if self.version != _config_version(self.q, self.r):
            raise ValueError("Kalman config version must identify q and r")


KALMAN_CONFIG_Q0025 = KalmanConfig(
    _config_version(0.0025, KALMAN_MEASUREMENT_VARIANCE),
    0.0025,
    KALMAN_MEASUREMENT_VARIANCE,
)
KALMAN_CONFIG_Q0100 = KalmanConfig(
    _config_version(0.0100, KALMAN_MEASUREMENT_VARIANCE),
    0.0100,
    KALMAN_MEASUREMENT_VARIANCE,
)
KALMAN_CONFIG_Q0400 = KalmanConfig(
    _config_version(0.0400, KALMAN_MEASUREMENT_VARIANCE),
    0.0400,
    KALMAN_MEASUREMENT_VARIANCE,
)
KALMAN_CONFIGURATIONS = (
    KALMAN_CONFIG_Q0025,
    KALMAN_CONFIG_Q0100,
    KALMAN_CONFIG_Q0400,
)


@dataclass(frozen=True)
class KalmanCandidateState:
    """Immutable two-state filter state at one canonical boundary."""

    candidate_algorithm_version: str
    candidate_config_version: str
    q: float
    r: float
    baseline_movement_algorithm_version: str
    baseline_movement_config_version: str
    universe_id: str
    universe_version: str
    provider: str
    exchange: str
    price_type: str
    last_evaluation_boundary_time_ms: int
    level: float
    trend: float
    p00: float
    p01: float
    p11: float

    def __post_init__(self):
        if self.candidate_algorithm_version != KALMAN_ALGORITHM_VERSION:
            raise ValueError("Kalman candidate algorithm version is invalid")
        config = KalmanConfig(self.candidate_config_version, self.q, self.r)
        if config.version != self.candidate_config_version:
            raise ValueError("Kalman candidate config identity is invalid")
        for name in (
            "baseline_movement_algorithm_version", "baseline_movement_config_version",
            "universe_id", "universe_version", "provider", "exchange", "price_type",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} is required")
        if (type(self.last_evaluation_boundary_time_ms) is not int
                or self.last_evaluation_boundary_time_ms < 0
                or self.last_evaluation_boundary_time_ms % KALMAN_EVALUATION_INTERVAL_MS):
            raise ValueError("Kalman state boundary is invalid")
        if not _finite(self.level) or not _finite(self.trend):
            raise ValueError("Kalman level and trend must be finite")
        _validate_covariance(self.p00, self.p01, self.p11)


@dataclass(frozen=True)
class KalmanObservation:
    """Explicit filter evidence for one boundary."""

    evaluation_boundary_time_ms: int
    raw_primary_median_normalized_movement: Metric[float]
    filtered_level: Metric[float]
    level: float | None
    trend: float | None
    innovation: float | None
    innovation_variance: float | None
    level_gain: float | None
    trend_gain: float | None
    candidate_algorithm_version: str
    candidate_config_version: str

    @property
    def available(self) -> bool:
        return self.filtered_level.available


@dataclass(frozen=True)
class PairedMarketStateKalmanExperimentPoint:
    """Lossless baseline/candidate evidence at one evaluation boundary."""

    evaluation_boundary_time_ms: int
    partition: ExperimentPartition
    baseline_movement_evaluation: MarketMovementEvaluation
    candidate_movement_evaluation: MarketMovementEvaluation
    raw_primary_median_normalized_movement: Metric[float]
    kalman_filtered_primary_median_normalized_movement: Metric[float]
    kalman_level: float | None
    kalman_trend: float | None
    kalman_innovation: float | None
    kalman_innovation_variance: float | None
    kalman_level_gain: float | None
    kalman_trend_gain: float | None
    candidate_algorithm_version: str
    candidate_config_version: str
    baseline_classification: MarketClassificationEvaluation
    candidate_classification: MarketClassificationEvaluation
    baseline_lifecycle_state: MarketEpisodeLifecycleState
    candidate_lifecycle_state: MarketEpisodeLifecycleState
    baseline_transitions: tuple[MarketEpisodeTransition, ...]
    candidate_transitions: tuple[MarketEpisodeTransition, ...]

    @property
    def baseline_primary_direction_state(self) -> str:
        return self.baseline_classification.windows[5].direction_state

    @property
    def candidate_primary_direction_state(self) -> str:
        return self.candidate_classification.windows[5].direction_state

    @property
    def baseline_primary_pace(self) -> Metric[str]:
        return self.baseline_classification.windows[5].pace

    @property
    def candidate_primary_pace(self) -> Metric[str]:
        return self.candidate_classification.windows[5].pace


@dataclass(frozen=True)
class KalmanComparisonSummary:
    """Descriptive comparison and filter diagnostics for one partition."""

    partition: str
    evaluation_count: int
    kalman_usable_count: int
    kalman_unavailable_count: int
    baseline_transition_counts: tuple[tuple[str, int], ...]
    candidate_transition_counts: tuple[tuple[str, int], ...]
    baseline_directional_onset_count: int
    candidate_directional_onset_count: int
    direction_state_disagreement_count: int
    direction_state_disagreement_fraction: float
    baseline_episode_count: int
    candidate_episode_count: int
    baseline_short_lived_closed_episode_count: int
    candidate_short_lived_closed_episode_count: int
    both_active_episode_boundary_count: int
    same_active_direction_boundary_count: int
    same_active_direction_overlap_fraction: float
    matched_onset_count: int
    unmatched_baseline_onset_count: int
    median_signed_candidate_onset_delta_ms: float | None
    median_absolute_innovation: float | None
    median_absolute_filter_gap: float | None
    median_absolute_trend_per_step: float | None


@dataclass(frozen=True)
class MarketStateKalmanExperimentResult:
    kalman_config: KalmanConfig
    points: tuple[PairedMarketStateKalmanExperimentPoint, ...]
    summaries: Mapping[str, KalmanComparisonSummary]

    def __post_init__(self):
        object.__setattr__(self, "points", tuple(self.points))
        object.__setattr__(self, "summaries", MappingProxyType(dict(self.summaries)))


def _usable_metric(metric: Metric) -> float | None:
    if not isinstance(metric, Metric) or not metric.available:
        return None
    value = metric.value
    if not _finite(value):
        return None
    return float(value)


def _state_matches(state, evaluation, config):
    return state is not None and (
        state.candidate_algorithm_version == KALMAN_ALGORITHM_VERSION
        and state.candidate_config_version == config.version
        and state.q == config.q
        and state.r == config.r
        and state.baseline_movement_algorithm_version == evaluation.algorithm_version
        and state.baseline_movement_config_version == evaluation.config_version
        and state.universe_id == evaluation.universe_id
        and state.universe_version == evaluation.universe_version
        and state.provider == evaluation.provider
        and state.exchange == evaluation.exchange
        and state.price_type == evaluation.price_type
    )


def _seed_state(evaluation, config, value):
    return KalmanCandidateState(
        KALMAN_ALGORITHM_VERSION,
        config.version,
        config.q,
        config.r,
        evaluation.algorithm_version,
        evaluation.config_version,
        evaluation.universe_id,
        evaluation.universe_version,
        evaluation.provider,
        evaluation.exchange,
        evaluation.price_type,
        evaluation.evaluation_boundary_time_ms,
        value,
        0.0,
        config.r,
        0.0,
        config.r,
    )


def _unavailable_observation(evaluation, config, raw):
    return KalmanObservation(
        evaluation.evaluation_boundary_time_ms,
        raw,
        Metric.missing("KALMAN_INPUT_UNAVAILABLE"),
        None,
        None,
        None,
        None,
        None,
        KALMAN_ALGORITHM_VERSION,
        config.version,
    )


def _candidate_evaluation(evaluation, config, filtered_metric):
    windows = {}
    for minute in WINDOWS:
        snapshot = replace(
            evaluation.windows[minute],
            algorithm_version=KALMAN_ALGORITHM_VERSION,
            config_version=config.version,
        )
        if minute == 5:
            snapshot = replace(
                snapshot,
                aggregates=replace(
                    snapshot.aggregates,
                    median_normalized_movement=filtered_metric,
                ),
            )
        windows[minute] = snapshot
    return replace(
        evaluation,
        algorithm_version=KALMAN_ALGORITHM_VERSION,
        config_version=config.version,
        windows=windows,
    )


def transform_market_movement_with_kalman(
    evaluation: MarketMovementEvaluation,
    config: KalmanConfig,
    state: KalmanCandidateState | None = None,
) -> tuple[MarketMovementEvaluation, KalmanObservation, KalmanCandidateState | None]:
    """Filter only the raw canonical #71 primary 5m median aggregate."""
    if not isinstance(evaluation, MarketMovementEvaluation):
        raise ValueError("evaluation must be MarketMovementEvaluation")
    if not isinstance(config, KalmanConfig):
        raise ValueError("config must be KalmanConfig")
    if state is not None and not isinstance(state, KalmanCandidateState):
        raise ValueError("state must be KalmanCandidateState or None")
    raw = evaluation.windows[5].aggregates.median_normalized_movement
    value = _usable_metric(raw)
    if value is None:
        observation = _unavailable_observation(evaluation, config, raw)
        return _candidate_evaluation(evaluation, config, observation.filtered_level), observation, None

    boundary = evaluation.evaluation_boundary_time_ms
    continuous = (
        _state_matches(state, evaluation, config)
        and boundary == state.last_evaluation_boundary_time_ms
        + KALMAN_EVALUATION_INTERVAL_MS
    )
    if not continuous:
        next_state = _seed_state(evaluation, config, value)
        observation = KalmanObservation(
            boundary,
            raw,
            Metric.present(value),
            value,
            0.0,
            None,
            None,
            None,
            None,
            KALMAN_ALGORITHM_VERSION,
            config.version,
        )
        return _candidate_evaluation(evaluation, config, observation.filtered_level), observation, next_state

    previous = state
    a = previous.p00 + 2.0 * previous.p01 + previous.p11 + config.q / 4.0
    b = previous.p01 + previous.p11 + config.q / 2.0
    c = previous.p11 + config.q
    if not all(_finite(item) for item in (a, b, c)):
        raise ValueError("Kalman predicted covariance must be finite")
    _validate_covariance(a, b, c)
    innovation = value - (previous.level + previous.trend)
    innovation_variance = a + config.r
    if not _finite(innovation) or not _finite(innovation_variance) or innovation_variance <= 0:
        raise ValueError("Kalman innovation variance must be finite and positive")
    level_gain = a / innovation_variance
    trend_gain = b / innovation_variance
    if not all(_finite(item) for item in (level_gain, trend_gain)):
        raise ValueError("Kalman gain must be finite")
    level = previous.level + previous.trend + level_gain * innovation
    trend = previous.trend + trend_gain * innovation

    alpha = 1.0 - level_gain
    beta = -trend_gain
    upper_p01 = alpha * (beta * a + b)
    lower_p10 = (beta * a + b) * alpha
    p00 = alpha * alpha * a + level_gain * config.r * level_gain
    p01 = (upper_p01 + lower_p10) / 2.0 + level_gain * config.r * trend_gain
    p11 = beta * beta * a + 2.0 * beta * b + c + trend_gain * config.r * trend_gain
    _validate_covariance(p00, p01, p11)
    next_state = KalmanCandidateState(
        KALMAN_ALGORITHM_VERSION,
        config.version,
        config.q,
        config.r,
        evaluation.algorithm_version,
        evaluation.config_version,
        evaluation.universe_id,
        evaluation.universe_version,
        evaluation.provider,
        evaluation.exchange,
        evaluation.price_type,
        boundary,
        level,
        trend,
        p00,
        p01,
        p11,
    )
    observation = KalmanObservation(
        boundary,
        raw,
        Metric.present(level),
        level,
        trend,
        innovation,
        innovation_variance,
        level_gain,
        trend_gain,
        KALMAN_ALGORITHM_VERSION,
        config.version,
    )
    return _candidate_evaluation(evaluation, config, observation.filtered_level), observation, next_state


def _summary(points, partition):
    selected = selected_points_for_partition(points, partition)
    observed = observed_points_for_partition(points, partition)
    baseline_spans = episode_spans(observed, "baseline")
    candidate_spans = episode_spans(observed, "candidate")
    both_active = 0
    same_active = 0
    for point in selected:
        baseline_episode = point.baseline_lifecycle_state.active_episode
        candidate_episode = point.candidate_lifecycle_state.active_episode
        if baseline_episode is not None and candidate_episode is not None:
            both_active += 1
            if baseline_episode.direction == candidate_episode.direction:
                same_active += 1
    disagreement = sum(
        point.baseline_primary_direction_state != point.candidate_primary_direction_state
        for point in selected
    )
    matched, unmatched, median_delta = match_directional_onsets(
        observed, selected, baseline_spans, candidate_spans)
    diagnostic_points = tuple(
        point for point in selected
        if point.kalman_innovation is not None
        and point.kalman_filtered_primary_median_normalized_movement.available
        and point.raw_primary_median_normalized_movement.available
    )
    absolute_innovations = tuple(abs(point.kalman_innovation) for point in diagnostic_points)
    absolute_gaps = tuple(
        abs(point.kalman_filtered_primary_median_normalized_movement.value
            - point.raw_primary_median_normalized_movement.value)
        for point in diagnostic_points
    )
    absolute_trends = tuple(abs(point.kalman_trend) for point in diagnostic_points)
    evaluation_count = len(selected)
    return KalmanComparisonSummary(
        partition=partition,
        evaluation_count=evaluation_count,
        kalman_usable_count=sum(
            point.kalman_filtered_primary_median_normalized_movement.available
            for point in selected
        ),
        kalman_unavailable_count=sum(
            not point.kalman_filtered_primary_median_normalized_movement.available
            for point in selected
        ),
        baseline_transition_counts=transition_counts(selected, "baseline"),
        candidate_transition_counts=transition_counts(selected, "candidate"),
        baseline_directional_onset_count=directional_onset_count(selected, "baseline"),
        candidate_directional_onset_count=directional_onset_count(selected, "candidate"),
        direction_state_disagreement_count=disagreement,
        direction_state_disagreement_fraction=(
            disagreement / evaluation_count if evaluation_count else 0.0
        ),
        baseline_episode_count=episode_count(baseline_spans, selected, partition),
        candidate_episode_count=episode_count(candidate_spans, selected, partition),
        baseline_short_lived_closed_episode_count=short_lived_episode_count(
            baseline_spans, selected, partition),
        candidate_short_lived_closed_episode_count=short_lived_episode_count(
            candidate_spans, selected, partition),
        both_active_episode_boundary_count=both_active,
        same_active_direction_boundary_count=same_active,
        same_active_direction_overlap_fraction=(
            same_active / both_active if both_active else 0.0
        ),
        matched_onset_count=matched,
        unmatched_baseline_onset_count=unmatched,
        median_signed_candidate_onset_delta_ms=median_delta,
        median_absolute_innovation=(float(median(absolute_innovations))
                                    if absolute_innovations else None),
        median_absolute_filter_gap=(float(median(absolute_gaps))
                                    if absolute_gaps else None),
        median_absolute_trend_per_step=(float(median(absolute_trends))
                                       if absolute_trends else None),
    )


def run_market_state_kalman_experiment(
    points: Iterable[MarketStateExperimentPoint],
    config: KalmanConfig,
    *,
    classifier_config: MarketClassifierConfig | None = None,
    lifecycle_config: MarketEpisodeLifecycleConfig | None = None,
) -> MarketStateKalmanExperimentResult:
    """Run unchanged canonical baseline beside one Kalman candidate stream."""
    if not isinstance(config, KalmanConfig):
        raise ValueError("config must be KalmanConfig")
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
    kalman_state = None
    paired = []
    for point in points:
        evaluation = point.movement_evaluation
        baseline_classification, baseline_result = advance_canonical_branch(
            evaluation,
            point.source_time_evidence,
            baseline_state,
            classifier_config,
            lifecycle_config,
        )
        candidate_evaluation, observation, kalman_state = transform_market_movement_with_kalman(
            evaluation, config, kalman_state)
        candidate_classification, candidate_result = advance_canonical_branch(
            candidate_evaluation,
            point.source_time_evidence,
            candidate_lifecycle_state,
            classifier_config,
            lifecycle_config,
        )
        paired.append(PairedMarketStateKalmanExperimentPoint(
            evaluation_boundary_time_ms=evaluation.evaluation_boundary_time_ms,
            partition=point.partition,
            baseline_movement_evaluation=evaluation,
            candidate_movement_evaluation=candidate_evaluation,
            raw_primary_median_normalized_movement=(
                evaluation.windows[5].aggregates.median_normalized_movement
            ),
            kalman_filtered_primary_median_normalized_movement=observation.filtered_level,
            kalman_level=observation.level,
            kalman_trend=observation.trend,
            kalman_innovation=observation.innovation,
            kalman_innovation_variance=observation.innovation_variance,
            kalman_level_gain=observation.level_gain,
            kalman_trend_gain=observation.trend_gain,
            candidate_algorithm_version=observation.candidate_algorithm_version,
            candidate_config_version=observation.candidate_config_version,
            baseline_classification=baseline_classification,
            candidate_classification=candidate_classification,
            baseline_lifecycle_state=baseline_result.next_state,
            candidate_lifecycle_state=candidate_result.next_state,
            baseline_transitions=baseline_result.transitions,
            candidate_transitions=candidate_result.transitions,
        ))
        baseline_state = baseline_result.next_state
        candidate_lifecycle_state = candidate_result.next_state
    paired = tuple(paired)
    summaries = {"all": _summary(paired, "all")}
    for partition in ("development", "validation", "test"):
        summaries[partition] = _summary(paired, partition)
    return MarketStateKalmanExperimentResult(config, paired, summaries)
