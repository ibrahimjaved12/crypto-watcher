"""Shared pure replay infrastructure for Issue #75 market-state experiments."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from statistics import median
from typing import Iterable, Literal

from ..market_episode_lifecycle import (
    MarketEpisodeLifecycleConfig,
    MarketEpisodeLifecycleResult,
    MarketEpisodeLifecycleState,
    MarketEpisodeTransition,
    process_market_episode_lifecycle,
)
from ..movement_classifier import (
    ALGORITHM_VERSION as CLASSIFIER_ALGORITHM_VERSION,
    MarketClassificationContext,
    MarketClassificationEvaluation,
    MarketClassifierConfig,
    MarketWindowClassificationContext,
    SymbolSourceTimeEvidence,
    classify_market_movement,
)
from ..movement_metrics import MarketMovementEvaluation, WINDOWS


ExperimentPartition = Literal["development", "validation", "test"]
EXPERIMENT_PARTITIONS = ("development", "validation", "test")
EXPERIMENT_SUMMARY_ALL = "all"
EXPERIMENT_EVALUATION_INTERVAL_MS = 5_000


@dataclass(frozen=True)
class MarketStateExperimentPoint:
    """One explicit chronological replay point shared by experiment candidates."""

    movement_evaluation: MarketMovementEvaluation
    source_time_evidence: tuple[SymbolSourceTimeEvidence, ...]
    partition: ExperimentPartition

    def __post_init__(self):
        if not isinstance(self.movement_evaluation, MarketMovementEvaluation):
            raise ValueError("movement_evaluation must be MarketMovementEvaluation")
        if self.partition not in EXPERIMENT_PARTITIONS:
            raise ValueError("experiment partition is invalid")
        evidence = tuple(self.source_time_evidence)
        if any(not isinstance(item, SymbolSourceTimeEvidence) for item in evidence):
            raise ValueError("source_time_evidence contains an invalid item")
        symbols = tuple(item.symbol for item in evidence)
        if symbols != self.movement_evaluation.configured_universe:
            raise ValueError("source_time_evidence must match the configured universe in order")
        boundary = self.movement_evaluation.evaluation_boundary_time_ms
        if (type(boundary) is not int or boundary < 0
                or boundary % EXPERIMENT_EVALUATION_INTERVAL_MS):
            raise ValueError("movement evaluation boundary must be aligned to 5000 ms")
        for minute in WINDOWS:
            window = self.movement_evaluation.windows.get(minute)
            if (window is None
                    or window.evaluation_boundary_time_ms != boundary):
                raise ValueError("movement evaluation windows must share the evaluation boundary")
        object.__setattr__(self, "source_time_evidence", evidence)


def _prior_confirmed_direction(state, evaluation, classifier_config):
    if state is None or state.active_episode is None:
        return None
    scope = state.active_episode.scope
    matches = (
        scope.universe_id == evaluation.universe_id,
        scope.universe_version == evaluation.universe_version,
        scope.movement_algorithm_version == evaluation.algorithm_version,
        scope.movement_config_version == evaluation.config_version,
        scope.classifier_algorithm_version == CLASSIFIER_ALGORITHM_VERSION,
        scope.classifier_config_version == classifier_config.version,
        scope.provider == evaluation.provider,
        scope.exchange == evaluation.exchange,
        scope.price_type == evaluation.price_type,
    )
    return state.active_episode.direction if all(matches) else None


def classification_context(
    source_time_evidence: tuple[SymbolSourceTimeEvidence, ...],
    prior_direction: str | None,
) -> MarketClassificationContext:
    """Build identical source evidence contexts with prior direction only on 5m."""
    return MarketClassificationContext({
        minute: MarketWindowClassificationContext(
            source_time_evidence,
            prior_direction if minute == 5 else None,
        )
        for minute in WINDOWS
    })


def advance_canonical_branch(
    evaluation: MarketMovementEvaluation,
    source_time_evidence: tuple[SymbolSourceTimeEvidence, ...],
    previous_state: MarketEpisodeLifecycleState | None,
    classifier_config: MarketClassifierConfig,
    lifecycle_config: MarketEpisodeLifecycleConfig,
) -> tuple[MarketClassificationEvaluation, MarketEpisodeLifecycleResult]:
    """Advance one independent canonical #72 → #73 branch."""
    prior_direction = _prior_confirmed_direction(
        previous_state, evaluation, classifier_config)
    classification = classify_market_movement(
        evaluation,
        classification_context(source_time_evidence, prior_direction),
        classifier_config,
    )
    lifecycle = process_market_episode_lifecycle(
        classification, previous_state, lifecycle_config)
    return classification, lifecycle


def validate_experiment_points(points: Iterable[MarketStateExperimentPoint]) -> None:
    """Require ordered development → validation → test 5-second points."""
    last_boundary = None
    last_partition_index = 0
    for index, point in enumerate(points):
        if not isinstance(point, MarketStateExperimentPoint):
            raise ValueError(f"experiment point {index} has the wrong type")
        boundary = point.movement_evaluation.evaluation_boundary_time_ms
        partition_index = EXPERIMENT_PARTITIONS.index(point.partition)
        if partition_index < last_partition_index:
            raise ValueError(
                "experiment partitions must be development, validation, then test")
        if last_boundary is not None:
            if boundary <= last_boundary:
                raise ValueError("experiment boundaries must be strictly increasing")
            if boundary != last_boundary + EXPERIMENT_EVALUATION_INTERVAL_MS:
                raise ValueError("experiment boundaries must advance exactly 5000 ms")
        last_boundary = boundary
        last_partition_index = partition_index


@dataclass(frozen=True)
class EpisodeSpan:
    """Observed episode interval; None end means open/censored at the cutoff."""

    episode_id: str
    direction: str
    start_boundary_time_ms: int
    end_boundary_time_ms: int | None
    observed_through_boundary_time_ms: int

    @property
    def observed_end_boundary_time_ms(self) -> int:
        return (self.end_boundary_time_ms
                if self.end_boundary_time_ms is not None
                else self.observed_through_boundary_time_ms)


def branch_transitions(point, branch: str) -> tuple[MarketEpisodeTransition, ...]:
    return (point.baseline_transitions if branch == "baseline"
            else point.candidate_transitions)


def episode_spans(points: tuple, branch: str) -> tuple[EpisodeSpan, ...]:
    """Build closed and open/censored spans from only the observed points."""
    if not points:
        return ()
    observed_through = points[-1].evaluation_boundary_time_ms
    starts = {}
    spans = []

    def close(episode_id, end_boundary, fallback=None):
        start = starts.pop(episode_id, None)
        if start is None:
            start = fallback
        if start is not None:
            spans.append(EpisodeSpan(
                episode_id, start[0], start[1], end_boundary, observed_through,
            ))

    for point in points:
        for transition in branch_transitions(point, branch):
            if transition.transition == "STARTED":
                starts[transition.episode_id] = (
                    transition.episode_direction,
                    transition.episode_start_boundary_time_ms,
                )
            elif transition.transition == "REVERSED":
                if transition.previous_episode_id is not None:
                    close(transition.previous_episode_id,
                          transition.evaluation_boundary_time_ms)
                starts[transition.episode_id] = (
                    transition.episode_direction,
                    transition.episode_start_boundary_time_ms,
                )
            elif transition.transition == "ENDED":
                close(
                    transition.episode_id,
                    transition.evaluation_boundary_time_ms,
                    (transition.episode_direction,
                     transition.episode_start_boundary_time_ms),
                )
    for episode_id, (direction, start) in tuple(starts.items()):
        spans.append(EpisodeSpan(
            episode_id, direction, start, None, observed_through,
        ))
    return tuple(sorted(spans,
                        key=lambda span: (span.start_boundary_time_ms,
                                          span.episode_id)))


def is_directional_onset(transition: MarketEpisodeTransition) -> bool:
    return transition.transition in ("STARTED", "REVERSED")


def transition_counts(points: tuple, branch: str) -> tuple[tuple[str, int], ...]:
    counts = Counter(
        transition.transition
        for point in points
        for transition in branch_transitions(point, branch)
    )
    return tuple(sorted(counts.items()))


def directional_onset_count(points: tuple, branch: str) -> int:
    return sum(
        is_directional_onset(transition)
        for point in points
        for transition in branch_transitions(point, branch)
    )


def partition_cutoff(points: tuple, partition: str) -> int | None:
    selected = tuple(point for point in points
                     if partition == EXPERIMENT_SUMMARY_ALL
                     or point.partition == partition)
    if partition == EXPERIMENT_SUMMARY_ALL and points:
        return points[-1].evaluation_boundary_time_ms
    return selected[-1].evaluation_boundary_time_ms if selected else None


def observed_points_for_partition(points: tuple, partition: str) -> tuple:
    cutoff = partition_cutoff(points, partition)
    if cutoff is None:
        return ()
    return tuple(point for point in points
                 if point.evaluation_boundary_time_ms <= cutoff)


def selected_points_for_partition(points: tuple, partition: str) -> tuple:
    return tuple(point for point in points
                 if partition == EXPERIMENT_SUMMARY_ALL
                 or point.partition == partition)


def episode_count(spans: tuple[EpisodeSpan, ...], selected_points: tuple,
                  partition: str) -> int:
    if partition == EXPERIMENT_SUMMARY_ALL:
        return len(spans)
    boundaries = {point.evaluation_boundary_time_ms for point in selected_points}
    return sum(span.start_boundary_time_ms in boundaries for span in spans)


def short_lived_episode_count(spans: tuple[EpisodeSpan, ...], selected_points: tuple,
                              partition: str) -> int:
    if partition == EXPERIMENT_SUMMARY_ALL:
        attributable = spans
    else:
        boundaries = {point.evaluation_boundary_time_ms for point in selected_points}
        attributable = tuple(span for span in spans
                             if span.start_boundary_time_ms in boundaries)
    return sum(
        span.end_boundary_time_ms is not None
        and span.end_boundary_time_ms - span.start_boundary_time_ms < 30_000
        for span in attributable
    )


def match_directional_onsets(
    observed_points: tuple,
    selected_points: tuple,
    baseline_spans: tuple[EpisodeSpan, ...],
    candidate_spans: tuple[EpisodeSpan, ...],
) -> tuple[int, int, float | None]:
    """One-to-one same-direction onset matching within observed overlap."""
    baseline_by_id = {span.episode_id: span for span in baseline_spans}
    candidate_by_id = {span.episode_id: span for span in candidate_spans}
    candidate_onsets = [
        transition
        for point in observed_points
        for transition in branch_transitions(point, "candidate")
        if is_directional_onset(transition)
        and transition.episode_id in candidate_by_id
    ]
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
            for candidate_transition in candidate_onsets:
                if candidate_transition.episode_id in used:
                    continue
                candidate_span = candidate_by_id[candidate_transition.episode_id]
                if (candidate_transition.episode_direction == transition.episode_direction
                        and candidate_span.start_boundary_time_ms <=
                        baseline_span.observed_end_boundary_time_ms
                        and baseline_span.start_boundary_time_ms <=
                        candidate_span.observed_end_boundary_time_ms):
                    matches.append((
                        abs(candidate_transition.evaluation_boundary_time_ms
                            - transition.evaluation_boundary_time_ms),
                        candidate_transition,
                    ))
            if not matches:
                unmatched += 1
                continue
            _, candidate_transition = min(matches, key=lambda item: item[0])
            used.add(candidate_transition.episode_id)
            deltas.append(float(
                candidate_transition.evaluation_boundary_time_ms
                - transition.evaluation_boundary_time_ms
            ))
    return len(deltas), unmatched, (float(median(deltas)) if deltas else None)
