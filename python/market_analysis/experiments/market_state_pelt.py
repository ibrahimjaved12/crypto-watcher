"""EXP-75-04A offline PELT segmentation of canonical market movement."""

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
from ..movement_classifier import MarketClassificationEvaluation, MarketClassifierConfig
from ..movement_metrics import MarketMovementEvaluation, Metric
from .market_state_common import (
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


PELT_ALGORITHM_VERSION = "market-state-pelt-offline-mean-shift-v1"
PELT_CONFIG_VERSION_PREFIX = "market-state-pelt-offline-mean-shift-config-v1"
PELT_MIN_SEGMENT_POINTS = 6
PELT_PENALTIES = (1.0, 2.0, 4.0)
PELT_NUMERICAL_TOL = 1e-12
PELT_TRANSITION_MATCH_WINDOW_MS = 60_000
PELT_EVALUATION_INTERVAL_MS = 5_000


def _config_version(beta: float, min_segment_points: int) -> str:
    return (f"{PELT_CONFIG_VERSION_PREFIX}:beta-{beta:.1f}:"
            f"min-segment-{min_segment_points}")


def _finite(value) -> bool:
    return (not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value))


@dataclass(frozen=True)
class PELTConfig:
    version: str
    beta: float
    min_segment_points: int

    def __post_init__(self):
        if not isinstance(self.version, str) or not self.version:
            raise ValueError("PELT config version is required")
        if not _finite(self.beta) or self.beta not in PELT_PENALTIES:
            raise ValueError("PELT beta must be one of the preregistered penalties")
        if type(self.min_segment_points) is not int or self.min_segment_points != PELT_MIN_SEGMENT_POINTS:
            raise ValueError("PELT minimum segment length is fixed at six points")
        if self.version != _config_version(self.beta, self.min_segment_points):
            raise ValueError("PELT config version must identify beta and minimum segment length")


PELT_CONFIG_BETA_1 = PELTConfig(_config_version(1.0, 6), 1.0, 6)
PELT_CONFIG_BETA_2 = PELTConfig(_config_version(2.0, 6), 2.0, 6)
PELT_CONFIG_BETA_4 = PELTConfig(_config_version(4.0, 6), 4.0, 6)
PELT_CONFIGURATIONS = (PELT_CONFIG_BETA_1, PELT_CONFIG_BETA_2, PELT_CONFIG_BETA_4)


@dataclass(frozen=True)
class PELTScopeIdentity:
    movement_algorithm_version: str
    movement_config_version: str
    universe_id: str
    universe_version: str
    provider: str
    exchange: str
    price_type: str


@dataclass(frozen=True)
class PELTBaselinePoint:
    evaluation_boundary_time_ms: int
    partition: ExperimentPartition
    raw_primary_median_normalized_movement: Metric[float]
    scope_identity: PELTScopeIdentity
    baseline_classification: MarketClassificationEvaluation
    baseline_lifecycle_state: MarketEpisodeLifecycleState
    baseline_transitions: tuple[MarketEpisodeTransition, ...]


@dataclass(frozen=True)
class PELTSegment:
    block_index: int
    start_index: int
    end_index: int
    scope_identity: PELTScopeIdentity
    start_boundary_time_ms: int
    end_boundary_time_ms: int
    observation_count: int
    mean: float
    sse: float


@dataclass(frozen=True)
class PELTChangePoint:
    block_index: int
    split_index: int
    boundary_time_ms: int
    scope_identity: PELTScopeIdentity
    left_mean: float
    left_observation_count: int
    right_mean: float
    right_observation_count: int
    mean_delta: float
    candidate_algorithm_version: str
    candidate_config_version: str


@dataclass(frozen=True)
class PELTBlockSummary:
    block_index: int
    scope_identity: PELTScopeIdentity
    start_boundary_time_ms: int
    end_boundary_time_ms: int
    observation_count: int
    status: str
    changepoint_indices: tuple[int, ...]
    penalized_objective: float | None


@dataclass(frozen=True)
class PELTSegmentationView:
    partition: str
    observed_through_boundary_time_ms: int | None
    usable_point_count: int
    unavailable_point_count: int
    compatible_block_count: int
    insufficient_block_count: int
    insufficient_block_point_count: int
    blocks: tuple[PELTBlockSummary, ...]
    segments: tuple[PELTSegment, ...]
    change_points: tuple[PELTChangePoint, ...]
    total_penalized_objective: float | None


@dataclass(frozen=True)
class PELTComparisonSummary:
    partition: str
    evaluation_count: int
    pelt_usable_point_count: int
    pelt_unavailable_point_count: int
    pelt_compatible_block_count: int
    pelt_insufficient_block_count: int
    pelt_insufficient_block_point_count: int
    pelt_segment_count: int
    pelt_change_point_count: int
    baseline_transition_counts: tuple[tuple[str, int], ...]
    baseline_directional_onset_count: int
    baseline_episode_count: int
    baseline_short_lived_closed_episode_count: int
    matched_baseline_onset_count: int
    unmatched_baseline_onset_count: int
    unmatched_pelt_change_point_count: int
    median_signed_pelt_minus_v1_onset_ms: float | None
    median_absolute_pelt_v1_offset_ms: float | None
    median_absolute_mean_delta: float | None


@dataclass(frozen=True)
class MarketStatePELTExperimentResult:
    pelt_config: PELTConfig
    baseline_points: tuple[PELTBaselinePoint, ...]
    segmentations: Mapping[str, PELTSegmentationView]
    summaries: Mapping[str, PELTComparisonSummary]

    def __post_init__(self):
        object.__setattr__(self, "baseline_points", tuple(self.baseline_points))
        object.__setattr__(self, "segmentations", MappingProxyType(dict(self.segmentations)))
        object.__setattr__(self, "summaries", MappingProxyType(dict(self.summaries)))


def _usable_value(metric: Metric) -> float | None:
    if not isinstance(metric, Metric) or not metric.available or not _finite(metric.value):
        return None
    return float(metric.value)


def _scope_identity(evaluation: MarketMovementEvaluation) -> PELTScopeIdentity:
    return PELTScopeIdentity(
        evaluation.algorithm_version,
        evaluation.config_version,
        evaluation.universe_id,
        evaluation.universe_version,
        evaluation.provider,
        evaluation.exchange,
        evaluation.price_type,
    )


def _prefix_sums(values: tuple[float, ...]) -> tuple[tuple[float, ...], tuple[float, ...]]:
    sums = [0.0]
    squares = [0.0]
    for value in values:
        sums.append(sums[-1] + value)
        squares.append(squares[-1] + value * value)
        if not _finite(sums[-1]) or not _finite(squares[-1]):
            raise ValueError("PELT prefix sums must remain finite")
    return tuple(sums), tuple(squares)


def _segment_cost(prefix_sum, prefix_sq, start: int, end: int) -> float:
    count = end - start
    if count <= 0:
        raise ValueError("PELT segment must contain observations")
    total = prefix_sum[end] - prefix_sum[start]
    total_sq = prefix_sq[end] - prefix_sq[start]
    cost = total_sq - total * total / count
    if not _finite(cost):
        raise ValueError("PELT segment cost must be finite")
    if cost < 0:
        if cost >= -PELT_NUMERICAL_TOL:
            return 0.0
        raise ValueError("PELT segment SSE is materially negative")
    return cost


def _choose_candidate(best, candidate):
    if best is None:
        return candidate
    objective, path = candidate
    best_objective, best_path = best
    if objective < best_objective - PELT_NUMERICAL_TOL:
        return candidate
    if (abs(objective - best_objective) <= PELT_NUMERICAL_TOL
            and path < best_path):
        return candidate
    return best


def _pelt_optimal_partition(
    values: tuple[float, ...], beta: float, min_segment_points: int,
) -> tuple[float, tuple[int, ...]] | None:
    """Exact PELT recursion with K=0 pruning and deterministic path ties."""
    n = len(values)
    if n < min_segment_points:
        return None
    prefix_sum, prefix_sq = _prefix_sums(values)
    objective: list[float | None] = [None] * (n + 1)
    predecessors: list[int | None] = [None] * (n + 1)
    objective[0] = -beta
    predecessors[0] = 0

    def path_for(start):
        path = []
        while start:
            path.append(start)
            start = predecessors[start]
        return tuple(reversed(path))
    admissible = [0]
    scheduled_removal: dict[int, int] = {}

    for end in range(min_segment_points, n + 1):
        expired = tuple(
            start for start, removal_end in scheduled_removal.items()
            if removal_end <= end
        )
        if expired:
            expired_set = set(expired)
            admissible = [start for start in admissible if start not in expired_set]
            for start in expired:
                del scheduled_removal[start]

        newly_eligible = end - min_segment_points
        if (predecessors[newly_eligible] is not None
                and newly_eligible not in admissible):
            admissible.append(newly_eligible)
        admissible.sort()
        candidates = tuple(
            start for start in admissible
            if end - start >= min_segment_points and objective[start] is not None
        )
        costs = {start: _segment_cost(prefix_sum, prefix_sq, start, end)
                 for start in candidates}
        best_objective = best_start = None
        for start in candidates:
            candidate_objective = objective[start] + costs[start] + beta
            if (best_start is None
                    or candidate_objective < best_objective - PELT_NUMERICAL_TOL
                    or (abs(candidate_objective - best_objective) <= PELT_NUMERICAL_TOL
                        and path_for(start) < path_for(best_start))):
                best_objective, best_start = candidate_objective, start
        if best_start is None:
            continue
        objective[end], predecessors[end] = best_objective, best_start

        # A dominated candidate stays active until this endpoint can itself
        # serve as a legal previous changepoint after a minimum-length segment.
        # The K=0 inequality remains the specified least-squares pruning rule.
        removal_end = end + min_segment_points
        for start in candidates:
            if (objective[start]
                    + costs[start]
                    > objective[end] + PELT_NUMERICAL_TOL):
                scheduled_removal[start] = min(
                    scheduled_removal.get(start, removal_end), removal_end
                )

    if objective[n] is None or predecessors[n] is None:
        return None
    return objective[n], path_for(predecessors[n])


def _segment_mean(prefix_sum, start: int, end: int) -> float:
    return (prefix_sum[end] - prefix_sum[start]) / (end - start)


def _segment_block(
    block_index: int,
    points: tuple[PELTBaselinePoint, ...],
    values: tuple[float, ...],
    scope: PELTScopeIdentity,
    config: PELTConfig,
) -> tuple[PELTBlockSummary, tuple[PELTSegment, ...], tuple[PELTChangePoint, ...]]:
    first_boundary = points[0].evaluation_boundary_time_ms
    last_boundary = points[-1].evaluation_boundary_time_ms
    if len(values) < config.min_segment_points:
        return (
            PELTBlockSummary(
                block_index, scope, first_boundary, last_boundary,
                len(values), "INSUFFICIENT_BLOCK", (), None,
            ),
            (),
            (),
        )

    prefix_sum, prefix_sq = _prefix_sums(values)
    optimum = _pelt_optimal_partition(values, config.beta, config.min_segment_points)
    if optimum is None:
        raise ValueError("PELT failed to find a valid segmentation for a sufficient block")
    objective, changepoint_indices = optimum
    edges = (0, *changepoint_indices, len(values))
    segments = []
    for start, end in zip(edges, edges[1:]):
        segments.append(PELTSegment(
            block_index=block_index,
            start_index=start,
            end_index=end,
            scope_identity=scope,
            start_boundary_time_ms=points[start].evaluation_boundary_time_ms,
            end_boundary_time_ms=points[end - 1].evaluation_boundary_time_ms,
            observation_count=end - start,
            mean=_segment_mean(prefix_sum, start, end),
            sse=_segment_cost(prefix_sum, prefix_sq, start, end),
        ))

    change_points = []
    for segment_index, split in enumerate(changepoint_indices):
        left, right = segments[segment_index], segments[segment_index + 1]
        change_points.append(PELTChangePoint(
            block_index=block_index,
            split_index=split,
            boundary_time_ms=points[split].evaluation_boundary_time_ms,
            scope_identity=scope,
            left_mean=left.mean,
            left_observation_count=left.observation_count,
            right_mean=right.mean,
            right_observation_count=right.observation_count,
            mean_delta=right.mean - left.mean,
            candidate_algorithm_version=PELT_ALGORITHM_VERSION,
            candidate_config_version=config.version,
        ))
    return (
        PELTBlockSummary(
            block_index, scope, first_boundary, last_boundary,
            len(values), "SEGMENTED", changepoint_indices, objective,
        ),
        tuple(segments),
        tuple(change_points),
    )


def _make_segmentation_view(
    partition: str,
    observed: tuple[PELTBaselinePoint, ...],
    config: PELTConfig,
) -> PELTSegmentationView:
    usable_count = sum(
        _usable_value(point.raw_primary_median_normalized_movement) is not None
        for point in observed
    )
    unavailable_count = len(observed) - usable_count
    blocks: list[PELTBlockSummary] = []
    segments: list[PELTSegment] = []
    change_points: list[PELTChangePoint] = []
    current_points: list[PELTBaselinePoint] = []
    current_values: list[float] = []
    current_scope = None

    def finish_block():
        nonlocal current_points, current_values, current_scope
        if not current_points:
            return
        block, block_segments, block_change_points = _segment_block(
            len(blocks), tuple(current_points), tuple(current_values),
            current_scope, config,
        )
        blocks.append(block)
        segments.extend(block_segments)
        change_points.extend(block_change_points)
        current_points = []
        current_values = []
        current_scope = None

    for point in observed:
        value = _usable_value(point.raw_primary_median_normalized_movement)
        if value is None:
            finish_block()
            continue
        if current_points and point.scope_identity != current_scope:
            finish_block()
        if not current_points:
            current_scope = point.scope_identity
        current_points.append(point)
        current_values.append(value)
    finish_block()

    sufficient_blocks = tuple(block for block in blocks if block.penalized_objective is not None)
    objective = (sum(block.penalized_objective for block in sufficient_blocks)
                 if sufficient_blocks else None)
    return PELTSegmentationView(
        partition=partition,
        observed_through_boundary_time_ms=(
            observed[-1].evaluation_boundary_time_ms if observed else None
        ),
        usable_point_count=usable_count,
        unavailable_point_count=unavailable_count,
        compatible_block_count=len(blocks),
        insufficient_block_count=sum(block.status == "INSUFFICIENT_BLOCK" for block in blocks),
        insufficient_block_point_count=sum(
            block.observation_count for block in blocks
            if block.status == "INSUFFICIENT_BLOCK"
        ),
        blocks=tuple(blocks),
        segments=tuple(segments),
        change_points=tuple(change_points),
        total_penalized_objective=objective,
    )


def _partition_of_boundary(points: tuple[PELTBaselinePoint, ...]) -> dict[int, str]:
    return {point.evaluation_boundary_time_ms: point.partition for point in points}


def _selected_baseline_onsets(points: tuple[PELTBaselinePoint, ...]):
    return tuple(
        (point, transition)
        for point in points
        for transition in point.baseline_transitions
        if transition.transition in ("STARTED", "REVERSED")
    )


def _match_pelt_to_baseline_onsets(
    selected_points: tuple[PELTBaselinePoint, ...],
    pelt_change_points: tuple[PELTChangePoint, ...],
) -> tuple[int, int, float | None, float | None, frozenset[int]]:
    used: set[int] = set()
    deltas = []
    unmatched_baseline = 0
    ordered_changes = tuple(sorted(
        pelt_change_points, key=lambda item: item.boundary_time_ms))
    for point, transition in _selected_baseline_onsets(selected_points):
        onset = transition.evaluation_boundary_time_ms
        matches = []
        for index, change_point in enumerate(ordered_changes):
            if index in used:
                continue
            delta = change_point.boundary_time_ms - onset
            if abs(delta) <= PELT_TRANSITION_MATCH_WINDOW_MS:
                matches.append((abs(delta), change_point.boundary_time_ms, index, delta))
        if not matches:
            unmatched_baseline += 1
            continue
        _, _, index, delta = min(matches)
        used.add(index)
        deltas.append(float(delta))
    return (
        len(deltas),
        unmatched_baseline,
        float(median(deltas)) if deltas else None,
        float(median(tuple(abs(delta) for delta in deltas))) if deltas else None,
        frozenset(used),
    )


def _summary(
    partition: str,
    all_points: tuple[PELTBaselinePoint, ...],
    selected: tuple[PELTBaselinePoint, ...],
    observed: tuple[PELTBaselinePoint, ...],
    segmentation: PELTSegmentationView,
) -> PELTComparisonSummary:
    spans = episode_spans(observed, "baseline")
    matched, unmatched_baseline, median_signed, median_absolute, used = (
        _match_pelt_to_baseline_onsets(selected, segmentation.change_points)
    )
    boundary_partitions = _partition_of_boundary(all_points)
    attributable_indices = tuple(
        index for index, change_point in enumerate(
            sorted(segmentation.change_points, key=lambda item: item.boundary_time_ms))
        if partition == "all"
        or boundary_partitions.get(change_point.boundary_time_ms) == partition
    )
    sorted_change_points = tuple(sorted(
        segmentation.change_points, key=lambda item: item.boundary_time_ms))
    unmatched_pelt = sum(index not in used for index in attributable_indices)
    attributed_deltas = tuple(
        abs(sorted_change_points[index].mean_delta) for index in attributable_indices
    )
    return PELTComparisonSummary(
        partition=partition,
        evaluation_count=len(selected),
        pelt_usable_point_count=segmentation.usable_point_count,
        pelt_unavailable_point_count=segmentation.unavailable_point_count,
        pelt_compatible_block_count=segmentation.compatible_block_count,
        pelt_insufficient_block_count=segmentation.insufficient_block_count,
        pelt_insufficient_block_point_count=segmentation.insufficient_block_point_count,
        pelt_segment_count=len(segmentation.segments),
        pelt_change_point_count=len(segmentation.change_points),
        baseline_transition_counts=transition_counts(selected, "baseline"),
        baseline_directional_onset_count=directional_onset_count(selected, "baseline"),
        baseline_episode_count=episode_count(spans, selected, partition),
        baseline_short_lived_closed_episode_count=short_lived_episode_count(
            spans, selected, partition),
        matched_baseline_onset_count=matched,
        unmatched_baseline_onset_count=unmatched_baseline,
        unmatched_pelt_change_point_count=unmatched_pelt,
        median_signed_pelt_minus_v1_onset_ms=median_signed,
        median_absolute_pelt_v1_offset_ms=median_absolute,
        median_absolute_mean_delta=(float(median(attributed_deltas))
                                    if attributed_deltas else None),
    )


def run_market_state_pelt_experiment(
    points: Iterable[MarketStateExperimentPoint],
    config: PELTConfig,
    *,
    classifier_config: MarketClassifierConfig | None = None,
    lifecycle_config: MarketEpisodeLifecycleConfig | None = None,
    canonical_branch_by_boundary: Mapping[int, tuple] | None = None,
) -> MarketStatePELTExperimentResult:
    """Run one canonical V1 branch and four independent offline PELT views."""
    if not isinstance(config, PELTConfig):
        raise ValueError("config must be PELTConfig")
    classifier_config = MarketClassifierConfig() if classifier_config is None else classifier_config
    lifecycle_config = MarketEpisodeLifecycleConfig() if lifecycle_config is None else lifecycle_config
    if not isinstance(classifier_config, MarketClassifierConfig):
        raise ValueError("classifier_config must be MarketClassifierConfig")
    if not isinstance(lifecycle_config, MarketEpisodeLifecycleConfig):
        raise ValueError("lifecycle_config must be MarketEpisodeLifecycleConfig")
    points = tuple(points)
    validate_experiment_points(points)
    baseline_state: MarketEpisodeLifecycleState | None = None
    baseline_points = []
    for point in points:
        evaluation = point.movement_evaluation
        classification, lifecycle = canonical_branch_for_point(
            evaluation,
            point.source_time_evidence,
            baseline_state,
            classifier_config,
            lifecycle_config,
            canonical_branch_by_boundary,
        )
        baseline_points.append(PELTBaselinePoint(
            evaluation_boundary_time_ms=evaluation.evaluation_boundary_time_ms,
            partition=point.partition,
            raw_primary_median_normalized_movement=(
                evaluation.windows[5].aggregates.median_normalized_movement
            ),
            scope_identity=_scope_identity(evaluation),
            baseline_classification=classification,
            baseline_lifecycle_state=lifecycle.next_state,
            baseline_transitions=lifecycle.transitions,
        ))
        baseline_state = lifecycle.next_state
    baseline_points = tuple(baseline_points)

    selected_by_partition = {
        partition: selected_points_for_partition(baseline_points, partition)
        for partition in ("all", "development", "validation", "test")
    }
    observed_by_partition = {
        partition: observed_points_for_partition(baseline_points, partition)
        for partition in ("all", "development", "validation", "test")
    }
    segmentations = {}
    views_by_cutoff = {}
    for partition in ("all", "development", "validation", "test"):
        observed = observed_by_partition[partition]
        cutoff = observed[-1].evaluation_boundary_time_ms if observed else None
        if cutoff not in views_by_cutoff:
            views_by_cutoff[cutoff] = _make_segmentation_view(partition, observed, config)
        segmentations[partition] = replace(views_by_cutoff[cutoff], partition=partition)

    summaries = {
        partition: _summary(
            partition,
            baseline_points,
            selected_by_partition[partition],
            observed_by_partition[partition],
            segmentations[partition],
        )
        for partition in ("all", "development", "validation", "test")
    }
    return MarketStatePELTExperimentResult(
        config, baseline_points, segmentations, summaries)
