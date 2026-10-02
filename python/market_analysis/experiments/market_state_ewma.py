"""Independent EXP-75-01 EWMA market-state experiment."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from types import MappingProxyType
from typing import Iterable, Mapping

from ..market_episode_lifecycle import (
    MarketEpisodeLifecycleConfig,
    MarketEpisodeLifecycleState,
    MarketEpisodeTransition,
    process_market_episode_lifecycle,
)
from ..movement_classifier import (
    MarketClassificationContext,
    MarketClassificationEvaluation,
    MarketClassifierConfig,
    MarketWindowClassificationContext,
    SymbolSourceTimeEvidence,
    classify_market_movement,
)
from ..movement_metrics import MarketMovementEvaluation, Metric, WINDOWS
from .market_state_common import (
    ExperimentPartition,
    MarketStateExperimentPoint,
    advance_canonical_branch,
    canonical_branch_for_point,
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


EWMA_ALGORITHM_VERSION = "market-movement-ewma-primary-median-v1"
EWMA_CONFIG_VERSION_PREFIX = "market-movement-ewma-primary-median-config-v1"
EWMA_HALF_LIVES_MS = (10_000, 30_000, 60_000)
EWMA_EVALUATION_INTERVAL_MS = 5_000


def _config_version(half_life_ms: int) -> str:
    return f"{EWMA_CONFIG_VERSION_PREFIX}:half-life-{half_life_ms}ms"


@dataclass(frozen=True)
class EWMAConfig:
    """One preregistered EWMA configuration; no configuration is optimized."""

    version: str
    half_life_ms: int

    def __post_init__(self):
        if not isinstance(self.version, str) or not self.version:
            raise ValueError("EWMA config version is required")
        if type(self.half_life_ms) is not int or self.half_life_ms not in EWMA_HALF_LIVES_MS:
            raise ValueError("EWMA half_life_ms must be one of the preregistered values")
        if self.version != _config_version(self.half_life_ms):
            raise ValueError("EWMA config version must identify its preregistered half-life")


EWMA_CONFIG_10S = EWMAConfig(_config_version(10_000), 10_000)
EWMA_CONFIG_30S = EWMAConfig(_config_version(30_000), 30_000)
EWMA_CONFIG_60S = EWMAConfig(_config_version(60_000), 60_000)
EWMA_CONFIGURATIONS = (EWMA_CONFIG_10S, EWMA_CONFIG_30S, EWMA_CONFIG_60S)


@dataclass(frozen=True)
class EWMACandidateState:
    """Serializable state for one candidate stream between replay points."""

    candidate_algorithm_version: str
    candidate_config_version: str
    half_life_ms: int
    baseline_movement_algorithm_version: str
    baseline_movement_config_version: str
    universe_id: str
    universe_version: str
    provider: str
    exchange: str
    price_type: str
    last_evaluation_boundary_time_ms: int
    current_ewma: float

    def __post_init__(self):
        if self.candidate_algorithm_version != EWMA_ALGORITHM_VERSION:
            raise ValueError("EWMA candidate algorithm version is invalid")
        if self.candidate_config_version != _config_version(self.half_life_ms):
            raise ValueError("EWMA candidate config identity is invalid")
        if type(self.half_life_ms) is not int or self.half_life_ms not in EWMA_HALF_LIVES_MS:
            raise ValueError("EWMA state half-life is invalid")
        for name in (
            "baseline_movement_algorithm_version", "baseline_movement_config_version",
            "universe_id", "universe_version", "provider", "exchange", "price_type",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} is required")
        if (type(self.last_evaluation_boundary_time_ms) is not int
                or self.last_evaluation_boundary_time_ms < 0
                or self.last_evaluation_boundary_time_ms % EWMA_EVALUATION_INTERVAL_MS):
            raise ValueError("EWMA state boundary is invalid")
        if (isinstance(self.current_ewma, bool) or not isinstance(self.current_ewma, (int, float))
                or not math.isfinite(self.current_ewma)):
            raise ValueError("EWMA state value must be finite")


@dataclass(frozen=True)
class PairedMarketStateExperimentPoint:
    """Lossless per-boundary evidence for the baseline and candidate branches."""

    evaluation_boundary_time_ms: int
    partition: ExperimentPartition
    raw_primary_median_normalized_movement: Metric[float]
    ewma_primary_median_normalized_movement: Metric[float]
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
class ExperimentComparisonSummary:
    """Descriptive comparison measurements for one run or partition."""

    partition: str
    evaluation_count: int
    usable_candidate_count: int
    unavailable_candidate_count: int
    baseline_transition_counts: tuple[tuple[str, int], ...]
    candidate_transition_counts: tuple[tuple[str, int], ...]
    baseline_directional_onset_count: int
    candidate_directional_onset_count: int
    direction_state_disagreement_count: int
    direction_state_disagreement_fraction: float
    both_active_episode_boundary_count: int
    same_active_direction_boundary_count: int
    same_active_direction_overlap_fraction: float
    baseline_episode_count: int
    candidate_episode_count: int
    baseline_short_lived_episode_count: int
    candidate_short_lived_episode_count: int
    matched_onset_count: int
    unmatched_baseline_onset_count: int
    median_signed_onset_delta_ms: float | None


@dataclass(frozen=True)
class MarketStateEWMAExperimentResult:
    """Immutable research output; no promotion or persistence decision is made."""

    ewma_config: EWMAConfig
    points: tuple[PairedMarketStateExperimentPoint, ...]
    summaries: Mapping[str, ExperimentComparisonSummary]

    def __post_init__(self):
        object.__setattr__(self, "points", tuple(self.points))
        object.__setattr__(self, "summaries", MappingProxyType(dict(self.summaries)))


def _usable_metric(metric: Metric) -> float | None:
    if not isinstance(metric, Metric) or not metric.available:
        return None
    value = metric.value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _candidate_state_matches(state, evaluation, config):
    return state is not None and (
        state.candidate_algorithm_version == EWMA_ALGORITHM_VERSION
        and state.candidate_config_version == config.version
        and state.half_life_ms == config.half_life_ms
        and state.baseline_movement_algorithm_version == evaluation.algorithm_version
        and state.baseline_movement_config_version == evaluation.config_version
        and state.universe_id == evaluation.universe_id
        and state.universe_version == evaluation.universe_version
        and state.provider == evaluation.provider
        and state.exchange == evaluation.exchange
        and state.price_type == evaluation.price_type
    )


def _candidate_metric_and_state(evaluation, config, state):
    """Return one EWMA metric and reset/continued state without interpolation."""
    boundary = evaluation.evaluation_boundary_time_ms
    usable = _usable_metric(evaluation.windows[5].aggregates.median_normalized_movement)
    prior = None
    if (_candidate_state_matches(state, evaluation, config)
            and boundary == state.last_evaluation_boundary_time_ms + EWMA_EVALUATION_INTERVAL_MS):
        prior = state.current_ewma
    if usable is None:
        return Metric.missing("EWMA_INPUT_UNAVAILABLE"), None
    alpha = 1.0 - 2.0 ** (-EWMA_EVALUATION_INTERVAL_MS / config.half_life_ms)
    current = usable if prior is None else alpha * usable + (1.0 - alpha) * prior
    next_state = EWMACandidateState(
        EWMA_ALGORITHM_VERSION, config.version, config.half_life_ms,
        evaluation.algorithm_version, evaluation.config_version,
        evaluation.universe_id, evaluation.universe_version,
        evaluation.provider, evaluation.exchange, evaluation.price_type,
        boundary, current,
    )
    return Metric.present(current), next_state


def transform_market_movement_with_ewma(
    evaluation: MarketMovementEvaluation,
    config: EWMAConfig,
    state: EWMACandidateState | None = None,
) -> tuple[MarketMovementEvaluation, EWMACandidateState | None]:
    """Transform only the primary 5m median aggregate and return new immutable state."""
    if not isinstance(evaluation, MarketMovementEvaluation):
        raise ValueError("evaluation must be MarketMovementEvaluation")
    if not isinstance(config, EWMAConfig):
        raise ValueError("config must be EWMAConfig")
    if state is not None and not isinstance(state, EWMACandidateState):
        raise ValueError("state must be EWMACandidateState or None")
    candidate_metric, next_state = _candidate_metric_and_state(evaluation, config, state)
    windows = {}
    for minute in WINDOWS:
        snapshot = evaluation.windows[minute]
        snapshot = replace(snapshot, algorithm_version=EWMA_ALGORITHM_VERSION,
                           config_version=config.version)
        if minute == 5:
            snapshot = replace(
                snapshot,
                aggregates=replace(snapshot.aggregates,
                                   median_normalized_movement=candidate_metric),
            )
        windows[minute] = snapshot
    return replace(evaluation, algorithm_version=EWMA_ALGORITHM_VERSION,
                   config_version=config.version, windows=windows), next_state


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
    evaluation_count = len(selected)
    return ExperimentComparisonSummary(
        partition=partition,
        evaluation_count=evaluation_count,
        usable_candidate_count=sum(
            point.ewma_primary_median_normalized_movement.available for point in selected
        ),
        unavailable_candidate_count=sum(
            not point.ewma_primary_median_normalized_movement.available for point in selected
        ),
        baseline_transition_counts=transition_counts(selected, "baseline"),
        candidate_transition_counts=transition_counts(selected, "candidate"),
        baseline_directional_onset_count=directional_onset_count(selected, "baseline"),
        candidate_directional_onset_count=directional_onset_count(selected, "candidate"),
        direction_state_disagreement_count=disagreement,
        direction_state_disagreement_fraction=(disagreement / evaluation_count
                                               if evaluation_count else 0.0),
        both_active_episode_boundary_count=both_active,
        same_active_direction_boundary_count=same_active,
        same_active_direction_overlap_fraction=(same_active / both_active if both_active else 0.0),
        baseline_episode_count=episode_count(baseline_spans, selected, partition),
        candidate_episode_count=episode_count(candidate_spans, selected, partition),
        baseline_short_lived_episode_count=short_lived_episode_count(
            baseline_spans, selected, partition),
        candidate_short_lived_episode_count=short_lived_episode_count(
            candidate_spans, selected, partition),
        matched_onset_count=matched,
        unmatched_baseline_onset_count=unmatched,
        median_signed_onset_delta_ms=median_delta,
    )


def run_market_state_ewma_experiment(
    points: Iterable[MarketStateExperimentPoint],
    config: EWMAConfig,
    *,
    classifier_config: MarketClassifierConfig | None = None,
    lifecycle_config: MarketEpisodeLifecycleConfig | None = None,
    canonical_branch_by_boundary: Mapping[int, tuple] | None = None,
) -> MarketStateEWMAExperimentResult:
    """Run paired canonical baseline/candidate streams over explicit replay points."""
    if not isinstance(config, EWMAConfig):
        raise ValueError("config must be EWMAConfig")
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
    ewma_state = None
    paired = []
    for point in points:
        evaluation = point.movement_evaluation
        candidate_evaluation, ewma_state = transform_market_movement_with_ewma(
            evaluation, config, ewma_state)
        baseline_classification, baseline_result = canonical_branch_for_point(
            evaluation, point.source_time_evidence, baseline_state,
            classifier_config, lifecycle_config, canonical_branch_by_boundary)
        candidate_classification, candidate_result = advance_canonical_branch(
            candidate_evaluation, point.source_time_evidence, candidate_lifecycle_state,
            classifier_config, lifecycle_config)
        paired.append(PairedMarketStateExperimentPoint(
            evaluation.evaluation_boundary_time_ms, point.partition,
            evaluation.windows[5].aggregates.median_normalized_movement,
            candidate_evaluation.windows[5].aggregates.median_normalized_movement,
            EWMA_ALGORITHM_VERSION, config.version,
            baseline_classification, candidate_classification,
            baseline_result.next_state, candidate_result.next_state,
            baseline_result.transitions, candidate_result.transitions,
        ))
        baseline_state = baseline_result.next_state
        candidate_lifecycle_state = candidate_result.next_state
    paired = tuple(paired)
    summaries = {"all": _summary(paired, "all")}
    for partition in ("development", "validation", "test"):
        summaries[partition] = _summary(paired, partition)
    return MarketStateEWMAExperimentResult(config, paired, summaries)
