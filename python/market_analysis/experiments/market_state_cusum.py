"""Independent EXP-75-02 causal CUSUM market-state experiment."""

from __future__ import annotations

from dataclasses import dataclass
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
from ..movement_metrics import MarketMovementEvaluation, Metric
from .market_state_common import (
    ExperimentPartition,
    MarketStateExperimentPoint,
    advance_canonical_branch,
    branch_transitions,
    directional_onset_count,
    episode_count,
    episode_spans,
    is_directional_onset,
    observed_points_for_partition,
    selected_points_for_partition,
    short_lived_episode_count,
    transition_counts,
    validate_experiment_points,
)


CUSUM_ALGORITHM_VERSION = "market-state-cusum-primary-median-v1"
CUSUM_CONFIG_VERSION_PREFIX = "market-state-cusum-primary-median-config-v1"
CUSUM_EVALUATION_INTERVAL_MS = 5_000
CUSUM_NONE = "NONE"
CUSUM_UP_SHIFT = "UP_SHIFT"
CUSUM_DOWN_SHIFT = "DOWN_SHIFT"
CUSUM_AMBIGUOUS = "AMBIGUOUS"
CUSUM_UNAVAILABLE = "UNAVAILABLE"
_CUSUM_DIRECTIONS = frozenset((CUSUM_UP_SHIFT, CUSUM_DOWN_SHIFT))
_CUSUM_STATES = _CUSUM_DIRECTIONS | frozenset((CUSUM_NONE, CUSUM_AMBIGUOUS, CUSUM_UNAVAILABLE))
_CUSUM_THRESHOLD_ABS_TOL = 1e-12
_PREREGISTERED_PARAMETERS = (
    (0.0, 0.10, 0.75),
    (0.0, 0.10, 1.50),
    (0.0, 0.25, 1.50),
)


def _at_or_above_threshold(value: float, threshold: float) -> bool:
    return value >= threshold or math.isclose(
        value, threshold, rel_tol=0.0, abs_tol=_CUSUM_THRESHOLD_ABS_TOL)


def _direction(positive: float, negative: float, h: float) -> str:
    positive_reached = _at_or_above_threshold(positive, h)
    negative_reached = _at_or_above_threshold(negative, h)
    if positive_reached and not negative_reached:
        return CUSUM_UP_SHIFT
    if negative_reached and not positive_reached:
        return CUSUM_DOWN_SHIFT
    if positive_reached and negative_reached:
        return CUSUM_AMBIGUOUS
    return CUSUM_NONE


def _config_version(k: float, h: float) -> str:
    return f"{CUSUM_CONFIG_VERSION_PREFIX}:k-{k:.2f}:h-{h:.2f}"


@dataclass(frozen=True)
class CUSUMConfig:
    """One of the three fixed preregistered CUSUM parameter sets."""

    version: str
    reference: float
    k: float
    h: float

    def __post_init__(self):
        values = (self.reference, self.k, self.h)
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) for value in values):
            raise ValueError("CUSUM parameters must be finite numbers")
        if values not in _PREREGISTERED_PARAMETERS:
            raise ValueError("CUSUM configuration is not preregistered")
        if self.version != _config_version(self.k, self.h):
            raise ValueError("CUSUM config version must encode its preregistered parameters")


CUSUM_CONFIG_K010_H075 = CUSUMConfig(_config_version(0.10, 0.75), 0.0, 0.10, 0.75)
CUSUM_CONFIG_K010_H150 = CUSUMConfig(_config_version(0.10, 1.50), 0.0, 0.10, 1.50)
CUSUM_CONFIG_K025_H150 = CUSUMConfig(_config_version(0.25, 1.50), 0.0, 0.25, 1.50)
CUSUM_CONFIGURATIONS = (
    CUSUM_CONFIG_K010_H075,
    CUSUM_CONFIG_K010_H150,
    CUSUM_CONFIG_K025_H150,
)


@dataclass(frozen=True)
class CUSUMCandidateState:
    """Immutable replay state for one CUSUM configuration."""

    candidate_algorithm_version: str
    candidate_config_version: str
    reference: float
    k: float
    h: float
    baseline_movement_algorithm_version: str
    baseline_movement_config_version: str
    universe_id: str
    universe_version: str
    provider: str
    exchange: str
    price_type: str
    last_evaluation_boundary_time_ms: int
    positive_accumulator: float
    negative_accumulator: float
    direction_state: str

    def __post_init__(self):
        if self.candidate_algorithm_version != CUSUM_ALGORITHM_VERSION:
            raise ValueError("CUSUM candidate algorithm version is invalid")
        config = CUSUMConfig(
            self.candidate_config_version, self.reference, self.k, self.h)
        if config.version != self.candidate_config_version:
            raise ValueError("CUSUM candidate config identity is invalid")
        for name in (
            "baseline_movement_algorithm_version", "baseline_movement_config_version",
            "universe_id", "universe_version", "provider", "exchange", "price_type",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} is required")
        if (type(self.last_evaluation_boundary_time_ms) is not int
                or self.last_evaluation_boundary_time_ms < 0
                or self.last_evaluation_boundary_time_ms % CUSUM_EVALUATION_INTERVAL_MS):
            raise ValueError("CUSUM state boundary is invalid")
        for name in ("positive_accumulator", "negative_accumulator"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} must be finite and nonnegative")
        if self.direction_state not in _CUSUM_STATES - {CUSUM_UNAVAILABLE}:
            raise ValueError("CUSUM state direction is invalid")
        if self.direction_state != _direction(
                self.positive_accumulator, self.negative_accumulator, self.h):
            raise ValueError("CUSUM state direction is inconsistent with accumulators")


@dataclass(frozen=True)
class CUSUMObservation:
    evaluation_boundary_time_ms: int
    raw_primary_median_normalized_movement: Metric[float]
    positive_accumulator: float | None
    negative_accumulator: float | None
    direction_state: str
    directional_onset: bool
    candidate_algorithm_version: str
    candidate_config_version: str

    @property
    def available(self) -> bool:
        return self.direction_state != CUSUM_UNAVAILABLE


@dataclass(frozen=True)
class CUSUMExperimentPoint:
    """Per-boundary detector evidence paired with the unchanged V1 branch."""

    movement_evaluation: MarketMovementEvaluation
    source_time_evidence: tuple[SymbolSourceTimeEvidence, ...]
    evaluation_boundary_time_ms: int
    partition: ExperimentPartition
    raw_primary_median_normalized_movement: Metric[float]
    cusum_available: bool
    positive_accumulator: float | None
    negative_accumulator: float | None
    cusum_direction_state: str
    cusum_directional_onset: bool
    candidate_algorithm_version: str
    candidate_config_version: str
    baseline_classification: MarketClassificationEvaluation
    baseline_lifecycle_state: MarketEpisodeLifecycleState
    baseline_transitions: tuple[MarketEpisodeTransition, ...]
    cusum_state: CUSUMCandidateState | None

    @property
    def baseline_primary_direction_state(self) -> str:
        return self.baseline_classification.windows[5].direction_state


@dataclass(frozen=True)
class CUSUMDetectionRegion:
    direction: str
    onset_boundary_time_ms: int
    end_boundary_time_ms: int | None
    observed_through_boundary_time_ms: int

    @property
    def observed_end_boundary_time_ms(self) -> int:
        return (self.end_boundary_time_ms
                if self.end_boundary_time_ms is not None
                else self.observed_through_boundary_time_ms)


@dataclass(frozen=True)
class CUSUMComparisonSummary:
    partition: str
    evaluation_count: int
    cusum_usable_count: int
    cusum_unavailable_count: int
    cusum_ambiguous_count: int
    cusum_onset_count: int
    cusum_up_onset_count: int
    cusum_down_onset_count: int
    cusum_detection_region_count: int
    cusum_short_lived_closed_region_count: int
    baseline_transition_counts: tuple[tuple[str, int], ...]
    baseline_directional_onset_count: int
    baseline_episode_count: int
    baseline_short_lived_closed_episode_count: int
    cusum_directional_active_boundary_count: int
    baseline_active_episode_boundary_count: int
    same_direction_active_overlap_boundary_count: int
    same_direction_overlap_fraction_of_cusum_active: float
    same_direction_baseline_coverage_fraction: float
    matched_baseline_onset_count: int
    unmatched_baseline_onset_count: int
    median_signed_detection_delta_ms: float | None
    unmatched_cusum_detection_region_count: int


@dataclass(frozen=True)
class MarketStateCUSUMExperimentResult:
    cusum_config: CUSUMConfig
    points: tuple[CUSUMExperimentPoint, ...]
    summaries: Mapping[str, CUSUMComparisonSummary]

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


def _state_matches(state, evaluation, config):
    return state is not None and (
        state.candidate_algorithm_version == CUSUM_ALGORITHM_VERSION
        and state.candidate_config_version == config.version
        and state.reference == config.reference
        and state.k == config.k
        and state.h == config.h
        and state.baseline_movement_algorithm_version == evaluation.algorithm_version
        and state.baseline_movement_config_version == evaluation.config_version
        and state.universe_id == evaluation.universe_id
        and state.universe_version == evaluation.universe_version
        and state.provider == evaluation.provider
        and state.exchange == evaluation.exchange
        and state.price_type == evaluation.price_type
    )


def transform_market_movement_with_cusum(
    evaluation: MarketMovementEvaluation,
    config: CUSUMConfig,
    state: CUSUMCandidateState | None = None,
) -> tuple[CUSUMObservation, CUSUMCandidateState | None]:
    """Process one raw canonical #71 5m median observation causally."""
    if not isinstance(evaluation, MarketMovementEvaluation):
        raise ValueError("evaluation must be MarketMovementEvaluation")
    if not isinstance(config, CUSUMConfig):
        raise ValueError("config must be CUSUMConfig")
    if state is not None and not isinstance(state, CUSUMCandidateState):
        raise ValueError("state must be CUSUMCandidateState or None")
    boundary = evaluation.evaluation_boundary_time_ms
    raw = evaluation.windows[5].aggregates.median_normalized_movement
    value = _usable_metric(raw)
    if value is None:
        return CUSUMObservation(
            boundary, raw, None, None, CUSUM_UNAVAILABLE, False,
            CUSUM_ALGORITHM_VERSION, config.version,
        ), None
    continuous = (_state_matches(state, evaluation, config)
                  and boundary == state.last_evaluation_boundary_time_ms
                  + CUSUM_EVALUATION_INTERVAL_MS)
    previous_positive = state.positive_accumulator if continuous else 0.0
    previous_negative = state.negative_accumulator if continuous else 0.0
    previous_direction = state.direction_state if continuous else CUSUM_NONE
    delta = value - config.reference
    positive = max(0.0, previous_positive + delta - config.k)
    negative = max(0.0, previous_negative - delta - config.k)
    direction = _direction(positive, negative, config.h)
    onset = direction in _CUSUM_DIRECTIONS and direction != previous_direction
    next_state = CUSUMCandidateState(
        CUSUM_ALGORITHM_VERSION, config.version, config.reference, config.k, config.h,
        evaluation.algorithm_version, evaluation.config_version,
        evaluation.universe_id, evaluation.universe_version,
        evaluation.provider, evaluation.exchange, evaluation.price_type,
        boundary, positive, negative, direction,
    )
    return CUSUMObservation(
        boundary, raw, positive, negative, direction, onset,
        CUSUM_ALGORITHM_VERSION, config.version,
    ), next_state


def detection_regions(points: tuple[CUSUMExperimentPoint, ...]) -> tuple[CUSUMDetectionRegion, ...]:
    """Construct closed and open/censored directional CUSUM regions."""
    if not points:
        return ()
    observed_through = points[-1].evaluation_boundary_time_ms
    active = None
    regions = []
    for point in points:
        direction = point.cusum_direction_state
        boundary = point.evaluation_boundary_time_ms
        if active is not None and direction != active.direction:
            regions.append(CUSUMDetectionRegion(
                active.direction, active.onset_boundary_time_ms,
                boundary, observed_through,
            ))
            active = None
        if direction in _CUSUM_DIRECTIONS and active is None:
            active = CUSUMDetectionRegion(
                direction, boundary, None, observed_through)
    if active is not None:
        regions.append(active)
    return tuple(regions)


def _direction_matches(cusum_direction, baseline_direction):
    return ((cusum_direction == CUSUM_UP_SHIFT and baseline_direction == "BROAD_RISE")
            or (cusum_direction == CUSUM_DOWN_SHIFT and baseline_direction == "BROAD_DROP"))


def _region_is_attributable(region, selected_points, partition):
    if partition == "all":
        return True
    return region.onset_boundary_time_ms in {
        point.evaluation_boundary_time_ms for point in selected_points
    }


def _match_baseline_onsets(observed_points, selected_points, baseline_spans, regions):
    baseline_by_id = {span.episode_id: span for span in baseline_spans}
    onset_regions = tuple(region for region in regions)
    used = set()
    deltas = []
    unmatched = 0
    for point in selected_points:
        for transition in branch_transitions(point, "baseline"):
            if not is_directional_onset(transition):
                continue
            baseline_span = baseline_by_id.get(transition.episode_id)
            if baseline_span is None:
                unmatched += 1
                continue
            matches = []
            for index, region in enumerate(onset_regions):
                if index in used or not _direction_matches(
                        region.direction, transition.episode_direction):
                    continue
                if (region.onset_boundary_time_ms <= baseline_span.observed_end_boundary_time_ms
                        and baseline_span.start_boundary_time_ms <= region.observed_end_boundary_time_ms):
                    matches.append((
                        abs(region.onset_boundary_time_ms
                            - transition.evaluation_boundary_time_ms),
                        index, region,
                    ))
            if not matches:
                unmatched += 1
                continue
            _, index, region = min(matches, key=lambda item: item[0])
            used.add(index)
            deltas.append(float(region.onset_boundary_time_ms
                               - transition.evaluation_boundary_time_ms))
    return len(deltas), unmatched, (float(median(deltas)) if deltas else None), used


def _unmatched_regions(regions, baseline_spans, selected_points, partition):
    attributable = tuple(region for region in regions
                         if _region_is_attributable(region, selected_points, partition))
    unmatched = 0
    for region in attributable:
        matched = any(
            _direction_matches(region.direction, span.direction)
            and region.onset_boundary_time_ms <= span.observed_end_boundary_time_ms
            and span.start_boundary_time_ms <= region.observed_end_boundary_time_ms
            for span in baseline_spans
        )
        if not matched:
            unmatched += 1
    return unmatched


def _summary(points, partition):
    selected = selected_points_for_partition(points, partition)
    observed = observed_points_for_partition(points, partition)
    regions = detection_regions(observed)
    baseline_spans = episode_spans(observed, "baseline")
    baseline_active = sum(
        point.baseline_lifecycle_state.active_episode is not None
        for point in selected
    )
    cusum_active = sum(point.cusum_direction_state in _CUSUM_DIRECTIONS
                       for point in selected)
    same_overlap = 0
    for point in selected:
        episode = point.baseline_lifecycle_state.active_episode
        if (episode is not None
                and _direction_matches(point.cusum_direction_state, episode.direction)):
            same_overlap += 1
    onset_count = sum(point.cusum_directional_onset for point in selected)
    up_onsets = sum(point.cusum_directional_onset
                    and point.cusum_direction_state == CUSUM_UP_SHIFT
                    for point in selected)
    down_onsets = sum(point.cusum_directional_onset
                      and point.cusum_direction_state == CUSUM_DOWN_SHIFT
                      for point in selected)
    matched, unmatched, median_delta, _ = _match_baseline_onsets(
        observed, selected, baseline_spans, regions)
    attributable_regions = tuple(region for region in regions
                                 if _region_is_attributable(region, selected, partition))
    return CUSUMComparisonSummary(
        partition=partition,
        evaluation_count=len(selected),
        cusum_usable_count=sum(point.cusum_available for point in selected),
        cusum_unavailable_count=sum(not point.cusum_available for point in selected),
        cusum_ambiguous_count=sum(
            point.cusum_direction_state == CUSUM_AMBIGUOUS for point in selected),
        cusum_onset_count=onset_count,
        cusum_up_onset_count=up_onsets,
        cusum_down_onset_count=down_onsets,
        cusum_detection_region_count=len(attributable_regions),
        cusum_short_lived_closed_region_count=sum(
            region.end_boundary_time_ms is not None
            and region.end_boundary_time_ms - region.onset_boundary_time_ms < 30_000
            for region in attributable_regions
        ),
        baseline_transition_counts=transition_counts(selected, "baseline"),
        baseline_directional_onset_count=directional_onset_count(selected, "baseline"),
        baseline_episode_count=episode_count(baseline_spans, selected, partition),
        baseline_short_lived_closed_episode_count=short_lived_episode_count(
            baseline_spans, selected, partition),
        cusum_directional_active_boundary_count=cusum_active,
        baseline_active_episode_boundary_count=baseline_active,
        same_direction_active_overlap_boundary_count=same_overlap,
        same_direction_overlap_fraction_of_cusum_active=(
            same_overlap / cusum_active if cusum_active else 0.0),
        same_direction_baseline_coverage_fraction=(
            same_overlap / baseline_active if baseline_active else 0.0),
        matched_baseline_onset_count=matched,
        unmatched_baseline_onset_count=unmatched,
        median_signed_detection_delta_ms=median_delta,
        unmatched_cusum_detection_region_count=_unmatched_regions(
            regions, baseline_spans, selected, partition),
    )

def run_market_state_cusum_experiment(
    points: Iterable[MarketStateExperimentPoint],
    config: CUSUMConfig,
    *,
    classifier_config: MarketClassifierConfig | None = None,
    lifecycle_config: MarketEpisodeLifecycleConfig | None = None,
) -> MarketStateCUSUMExperimentResult:
    """Run an unchanged canonical baseline beside one independent CUSUM detector."""
    if not isinstance(config, CUSUMConfig):
        raise ValueError("config must be CUSUMConfig")
    classifier_config = MarketClassifierConfig() if classifier_config is None else classifier_config
    lifecycle_config = MarketEpisodeLifecycleConfig() if lifecycle_config is None else lifecycle_config
    if not isinstance(classifier_config, MarketClassifierConfig):
        raise ValueError("classifier_config must be MarketClassifierConfig")
    if not isinstance(lifecycle_config, MarketEpisodeLifecycleConfig):
        raise ValueError("lifecycle_config must be MarketEpisodeLifecycleConfig")
    points = tuple(points)
    validate_experiment_points(points)
    baseline_state = None
    cusum_state = None
    results = []
    for point in points:
        evaluation = point.movement_evaluation
        baseline_classification, baseline_result = advance_canonical_branch(
            evaluation, point.source_time_evidence, baseline_state,
            classifier_config, lifecycle_config)
        observation, cusum_state = transform_market_movement_with_cusum(
            evaluation, config, cusum_state)
        results.append(CUSUMExperimentPoint(
            movement_evaluation=evaluation,
            source_time_evidence=point.source_time_evidence,
            evaluation_boundary_time_ms=evaluation.evaluation_boundary_time_ms,
            partition=point.partition,
            raw_primary_median_normalized_movement=observation.raw_primary_median_normalized_movement,
            cusum_available=observation.available,
            positive_accumulator=observation.positive_accumulator,
            negative_accumulator=observation.negative_accumulator,
            cusum_direction_state=observation.direction_state,
            cusum_directional_onset=observation.directional_onset,
            candidate_algorithm_version=observation.candidate_algorithm_version,
            candidate_config_version=observation.candidate_config_version,
            baseline_classification=baseline_classification,
            baseline_lifecycle_state=baseline_result.next_state,
            baseline_transitions=baseline_result.transitions,
            cusum_state=cusum_state,
        ))
        baseline_state = baseline_result.next_state
    results = tuple(results)
    summaries = {"all": _summary(results, "all")}
    for partition in ("development", "validation", "test"):
        summaries[partition] = _summary(results, partition)
    return MarketStateCUSUMExperimentResult(config, results, summaries)
