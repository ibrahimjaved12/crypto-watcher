"""EXP-75-04B exact, research-only Bayesian online change-point detector.

This is an untruncated reference implementation. Its run-length state grows
with uninterrupted history, so it is not wired into indefinite live retention.
"""

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
    directional_onset_count,
    episode_count,
    episode_spans,
    observed_points_for_partition,
    selected_points_for_partition,
    short_lived_episode_count,
    transition_counts,
    validate_experiment_points,
)


BOCPD_ALGORITHM_VERSION = "market-state-bocpd-gaussian-mean-v1"
BOCPD_CONFIG_VERSION_PREFIX = "market-state-bocpd-gaussian-mean-config-v1"
BOCPD_EVALUATION_INTERVAL_MS = 5_000
BOCPD_PRIOR_MEAN = 0.0
BOCPD_PRIOR_MEAN_VARIANCE = 4.0
BOCPD_OBSERVATION_VARIANCE = 1.0
BOCPD_RECENT_RUN_MAX_STEPS = 2
BOCPD_ALARM_THRESHOLD = 0.50
BOCPD_MIN_HISTORY_POINTS = 6
BOCPD_TRANSITION_MATCH_WINDOW_MS = 60_000
BOCPD_NUMERICAL_TOL = 1e-12
BOCPD_EXPECTED_RUN_LENGTHS = (30, 60, 120)

BOCPD_WARMING = "WARMING"
BOCPD_NONE = "NONE"
BOCPD_CHANGE = "CHANGE"
BOCPD_UNAVAILABLE = "UNAVAILABLE"
_BOCPD_STATES = frozenset((BOCPD_WARMING, BOCPD_NONE, BOCPD_CHANGE, BOCPD_UNAVAILABLE))


def _finite(value) -> bool:
    return (not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value))


def _logsumexp(log_values: Iterable[float]) -> float:
    values = tuple(log_values)
    if not values:
        raise ValueError("logsumexp requires at least one value")
    if any(not _finite(value) for value in values):
        raise ValueError("logsumexp values must be finite")
    maximum = max(values)
    result = maximum + math.log(sum(math.exp(value - maximum) for value in values))
    if not math.isfinite(result):
        raise ValueError("logsumexp result must be finite")
    return result


def _config_version(expected_run_length_points: int) -> str:
    return (
        f"{BOCPD_CONFIG_VERSION_PREFIX}:erl-{expected_run_length_points}:"
        f"mu0-{BOCPD_PRIOR_MEAN:.1f}:tau0var-{BOCPD_PRIOR_MEAN_VARIANCE:.1f}:"
        f"sigma2-{BOCPD_OBSERVATION_VARIANCE:.1f}:"
        f"recent-run-steps-le-{BOCPD_RECENT_RUN_MAX_STEPS}:"
        f"alarm-{BOCPD_ALARM_THRESHOLD:.2f}:min-history-{BOCPD_MIN_HISTORY_POINTS}"
    )


@dataclass(frozen=True)
class BOCPDConfig:
    """One of the three fixed, preregistered constant-hazard configurations."""

    version: str
    expected_run_length_points: int
    prior_mean: float = BOCPD_PRIOR_MEAN
    prior_mean_variance: float = BOCPD_PRIOR_MEAN_VARIANCE
    observation_variance: float = BOCPD_OBSERVATION_VARIANCE
    recent_run_max_steps: int = BOCPD_RECENT_RUN_MAX_STEPS
    alarm_probability_threshold: float = BOCPD_ALARM_THRESHOLD
    minimum_history_points: int = BOCPD_MIN_HISTORY_POINTS

    def __post_init__(self):
        if not isinstance(self.version, str) or not self.version:
            raise ValueError("BOCPD config version is required")
        if type(self.expected_run_length_points) is not int or (
                self.expected_run_length_points not in BOCPD_EXPECTED_RUN_LENGTHS):
            raise ValueError("BOCPD expected run length must be preregistered")
        if self.version != _config_version(self.expected_run_length_points):
            raise ValueError("BOCPD config version must encode all fixed model settings")
        numeric_values = (
            self.prior_mean,
            self.prior_mean_variance,
            self.observation_variance,
            self.alarm_probability_threshold,
        )
        if any(not _finite(value) for value in numeric_values):
            raise ValueError("BOCPD numeric settings must be finite numbers")
        if (type(self.recent_run_max_steps) is not int
                or type(self.minimum_history_points) is not int):
            raise ValueError("BOCPD run and history limits must be integers")
        if (self.prior_mean != BOCPD_PRIOR_MEAN
                or self.prior_mean_variance != BOCPD_PRIOR_MEAN_VARIANCE
                or self.observation_variance != BOCPD_OBSERVATION_VARIANCE
                or self.recent_run_max_steps != BOCPD_RECENT_RUN_MAX_STEPS
                or self.alarm_probability_threshold != BOCPD_ALARM_THRESHOLD
                or self.minimum_history_points != BOCPD_MIN_HISTORY_POINTS):
            raise ValueError("BOCPD prior, observation, alarm, and warm-up settings are fixed")

    @property
    def hazard(self) -> float:
        return 1.0 / self.expected_run_length_points


BOCPD_CONFIG_ERL_30 = BOCPDConfig(_config_version(30), 30)
BOCPD_CONFIG_ERL_60 = BOCPDConfig(_config_version(60), 60)
BOCPD_CONFIG_ERL_120 = BOCPDConfig(_config_version(120), 120)
BOCPD_CONFIGURATIONS = (
    BOCPD_CONFIG_ERL_30,
    BOCPD_CONFIG_ERL_60,
    BOCPD_CONFIG_ERL_120,
)


@dataclass(frozen=True)
class BOCPDRunLengthHypothesis:
    run_length_steps: int
    log_probability: float
    posterior_mean: float
    posterior_mean_variance: float

    def __post_init__(self):
        if type(self.run_length_steps) is not int or self.run_length_steps < 0:
            raise ValueError("BOCPD run length must be a nonnegative integer")
        if not _finite(self.log_probability):
            raise ValueError("BOCPD log probability must be finite")
        if not _finite(self.posterior_mean):
            raise ValueError("BOCPD posterior mean must be finite")
        if (not _finite(self.posterior_mean_variance)
                or self.posterior_mean_variance <= 0):
            raise ValueError("BOCPD posterior mean variance must be finite and positive")


@dataclass(frozen=True)
class BOCPDCandidateState:
    """Immutable exact posterior and scope identity for deterministic replay."""

    candidate_algorithm_version: str
    candidate_config_version: str
    expected_run_length_points: int
    hazard: float
    prior_mean: float
    prior_mean_variance: float
    observation_variance: float
    baseline_movement_algorithm_version: str
    baseline_movement_config_version: str
    universe_id: str
    universe_version: str
    provider: str
    exchange: str
    price_type: str
    last_evaluation_boundary_time_ms: int
    observations_since_reset: int
    hypotheses: tuple[BOCPDRunLengthHypothesis, ...]

    def __post_init__(self):
        if self.candidate_algorithm_version != BOCPD_ALGORITHM_VERSION:
            raise ValueError("BOCPD candidate algorithm version is invalid")
        config = BOCPDConfig(
            self.candidate_config_version,
            self.expected_run_length_points,
            self.prior_mean,
            self.prior_mean_variance,
            self.observation_variance,
        )
        if self.hazard != config.hazard:
            raise ValueError("BOCPD state hazard disagrees with its config")
        for name in (
            "baseline_movement_algorithm_version", "baseline_movement_config_version",
            "universe_id", "universe_version", "provider", "exchange", "price_type",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} is required")
        boundary = self.last_evaluation_boundary_time_ms
        if (type(boundary) is not int or boundary < 0
                or boundary % BOCPD_EVALUATION_INTERVAL_MS):
            raise ValueError("BOCPD state boundary is invalid")
        if type(self.observations_since_reset) is not int or self.observations_since_reset < 1:
            raise ValueError("BOCPD observations_since_reset must be positive")
        hypotheses = tuple(self.hypotheses)
        if not hypotheses or any(not isinstance(item, BOCPDRunLengthHypothesis)
                                 for item in hypotheses):
            raise ValueError("BOCPD hypotheses must be a nonempty tuple of run-length states")
        expected_run_lengths = tuple(range(self.observations_since_reset + 1))
        actual_run_lengths = tuple(item.run_length_steps for item in hypotheses)
        if actual_run_lengths != expected_run_lengths:
            raise ValueError("BOCPD run lengths must be contiguous from zero through history length")
        if abs(_logsumexp(item.log_probability for item in hypotheses)) > BOCPD_NUMERICAL_TOL:
            raise ValueError("BOCPD log posterior must already be normalized")
        object.__setattr__(self, "hypotheses", hypotheses)


@dataclass(frozen=True)
class BOCPDObservation:
    evaluation_boundary_time_ms: int
    raw_primary_median_normalized_movement: Metric[float]
    available: bool
    detector_state: str
    run_length_zero_probability: float | None
    recent_change_probability: float | None
    map_run_length_steps: int | None
    expected_run_length_steps: float | None
    hypothesis_count: int
    observations_since_reset: int
    candidate_algorithm_version: str
    candidate_config_version: str

    def __post_init__(self):
        if type(self.evaluation_boundary_time_ms) is not int or (
                self.evaluation_boundary_time_ms < 0
                or self.evaluation_boundary_time_ms % BOCPD_EVALUATION_INTERVAL_MS):
            raise ValueError("BOCPD observation boundary is invalid")
        if self.detector_state not in _BOCPD_STATES:
            raise ValueError("BOCPD detector state is invalid")
        if type(self.available) is not bool:
            raise ValueError("BOCPD available flag must be boolean")
        if self.detector_state == BOCPD_UNAVAILABLE:
            if (self.available or self.run_length_zero_probability is not None
                    or self.recent_change_probability is not None
                    or self.map_run_length_steps is not None
                    or self.expected_run_length_steps is not None
                    or self.hypothesis_count != 0 or self.observations_since_reset != 0):
                raise ValueError("unavailable BOCPD observations cannot carry posterior state")
        else:
            if not self.available or self.hypothesis_count < 1 or self.observations_since_reset < 1:
                raise ValueError("usable BOCPD observations require posterior evidence")
            if self.hypothesis_count != self.observations_since_reset + 1:
                raise ValueError("BOCPD hypothesis count must match untruncated run-length support")
            for value in (self.run_length_zero_probability,
                          self.recent_change_probability,
                          self.expected_run_length_steps):
                if value is None or not _finite(value):
                    raise ValueError("usable BOCPD diagnostics must be finite")
            if (self.run_length_zero_probability < 0
                    or self.run_length_zero_probability > 1
                    or self.recent_change_probability < 0
                    or self.recent_change_probability > 1
                    or self.expected_run_length_steps < 0):
                raise ValueError("BOCPD probabilities and expected run length are out of range")
            if (type(self.map_run_length_steps) is not int
                    or self.map_run_length_steps < 0
                    or self.map_run_length_steps > self.observations_since_reset):
                raise ValueError("usable BOCPD MAP run length is required")


@dataclass(frozen=True)
class BOCPDExperimentPoint:
    """BOCPD evidence paired with exactly one unchanged V1 branch evaluation."""

    movement_evaluation: MarketMovementEvaluation
    source_time_evidence: tuple[SymbolSourceTimeEvidence, ...]
    evaluation_boundary_time_ms: int
    partition: ExperimentPartition
    bocpd_observation: BOCPDObservation
    baseline_classification: MarketClassificationEvaluation
    baseline_lifecycle_state: MarketEpisodeLifecycleState
    baseline_transitions: tuple[MarketEpisodeTransition, ...]

    @property
    def raw_primary_median_normalized_movement(self) -> Metric[float]:
        return self.bocpd_observation.raw_primary_median_normalized_movement

    @property
    def detector_state(self) -> str:
        return self.bocpd_observation.detector_state


@dataclass(frozen=True)
class BOCPDDetectionRegion:
    start_boundary_time_ms: int
    end_boundary_time_ms: int | None
    observed_through_boundary_time_ms: int

    def __post_init__(self):
        if (type(self.start_boundary_time_ms) is not int
                or self.start_boundary_time_ms < 0
                or self.start_boundary_time_ms % BOCPD_EVALUATION_INTERVAL_MS):
            raise ValueError("BOCPD region start boundary is invalid")
        if self.end_boundary_time_ms is not None and (
                type(self.end_boundary_time_ms) is not int
                or self.end_boundary_time_ms < self.start_boundary_time_ms
                or self.end_boundary_time_ms % BOCPD_EVALUATION_INTERVAL_MS):
            raise ValueError("BOCPD region end boundary is invalid")
        if (type(self.observed_through_boundary_time_ms) is not int
                or self.observed_through_boundary_time_ms < self.start_boundary_time_ms
                or self.observed_through_boundary_time_ms % BOCPD_EVALUATION_INTERVAL_MS):
            raise ValueError("BOCPD region observed-through boundary is invalid")
        if (self.end_boundary_time_ms is not None
                and self.end_boundary_time_ms > self.observed_through_boundary_time_ms):
            raise ValueError("BOCPD region cannot end after its observed cutoff")


@dataclass(frozen=True)
class BOCPDComparisonSummary:
    partition: str
    evaluation_count: int
    bocpd_usable_count: int
    bocpd_unavailable_count: int
    bocpd_warming_count: int
    bocpd_change_boundary_count: int
    bocpd_detection_region_count: int
    bocpd_short_lived_closed_region_count: int
    baseline_transition_counts: tuple[tuple[str, int], ...]
    baseline_directional_onset_count: int
    baseline_episode_count: int
    baseline_short_lived_closed_episode_count: int
    matched_baseline_onset_count: int
    unmatched_baseline_onset_count: int
    unmatched_bocpd_detection_region_count: int
    median_signed_bocpd_minus_v1_onset_ms: float | None
    median_absolute_bocpd_v1_offset_ms: float | None
    median_recent_change_probability: float | None
    maximum_recent_change_probability: float | None
    median_expected_run_length_steps: float | None
    maximum_hypothesis_count: int | None


@dataclass(frozen=True)
class MarketStateBOCPDExperimentResult:
    bocpd_config: BOCPDConfig
    points: tuple[BOCPDExperimentPoint, ...]
    detection_regions_by_partition: Mapping[str, tuple[BOCPDDetectionRegion, ...]]
    summaries: Mapping[str, BOCPDComparisonSummary]

    def __post_init__(self):
        object.__setattr__(self, "points", tuple(self.points))
        object.__setattr__(
            self, "detection_regions_by_partition",
            MappingProxyType({key: tuple(value)
                              for key, value in self.detection_regions_by_partition.items()}),
        )
        object.__setattr__(self, "summaries", MappingProxyType(dict(self.summaries)))


def _usable_metric(metric: Metric) -> float | None:
    if not isinstance(metric, Metric) or not metric.available:
        return None
    value = metric.value
    if not _finite(value):
        return None
    return float(value)


def _log_normal_density(value: float, mean: float, variance: float) -> float:
    if not _finite(variance) or variance <= 0:
        raise ValueError("BOCPD predictive variance must be finite and positive")
    residual = value - mean
    result = -0.5 * (
        math.log(2.0 * math.pi * variance) + residual * residual / variance
    )
    if not math.isfinite(result):
        raise ValueError("BOCPD predictive log density must be finite")
    return result


def _assimilate(mean: float, variance: float, observation: float,
                observation_variance: float) -> tuple[float, float]:
    posterior_variance = 1.0 / (1.0 / variance + 1.0 / observation_variance)
    posterior_mean = posterior_variance * (
        mean / variance + observation / observation_variance
    )
    if not _finite(posterior_mean) or not _finite(posterior_variance) or posterior_variance <= 0:
        raise ValueError("BOCPD posterior sufficient statistics must be finite and valid")
    return posterior_mean, posterior_variance


def _state_matches(state: BOCPDCandidateState | None,
                   evaluation: MarketMovementEvaluation,
                   config: BOCPDConfig) -> bool:
    return state is not None and (
        state.candidate_algorithm_version == BOCPD_ALGORITHM_VERSION
        and state.candidate_config_version == config.version
        and state.expected_run_length_points == config.expected_run_length_points
        and state.hazard == config.hazard
        and state.prior_mean == config.prior_mean
        and state.prior_mean_variance == config.prior_mean_variance
        and state.observation_variance == config.observation_variance
        and state.baseline_movement_algorithm_version == evaluation.algorithm_version
        and state.baseline_movement_config_version == evaluation.config_version
        and state.universe_id == evaluation.universe_id
        and state.universe_version == evaluation.universe_version
        and state.provider == evaluation.provider
        and state.exchange == evaluation.exchange
        and state.price_type == evaluation.price_type
    )


def _posterior_diagnostics(
    hypotheses: tuple[BOCPDRunLengthHypothesis, ...],
) -> tuple[float, float, int, float]:
    raw_probabilities = tuple(math.exp(item.log_probability) for item in hypotheses)
    total_probability = sum(raw_probabilities)
    if not math.isfinite(total_probability) or total_probability <= 0:
        raise ValueError("BOCPD linear diagnostic probabilities must have finite positive mass")
    probabilities = tuple(
        probability / total_probability for probability in raw_probabilities
    )
    zero_probability = probabilities[0]
    recent_probability = sum(
        probability for item, probability in zip(hypotheses, probabilities)
        if item.run_length_steps <= BOCPD_RECENT_RUN_MAX_STEPS
    )
    expected_run_length = sum(
        item.run_length_steps * probability
        for item, probability in zip(hypotheses, probabilities)
    )
    maximum_probability = max(probabilities)
    map_run_length = next(
        item.run_length_steps
        for item, probability in zip(hypotheses, probabilities)
        if maximum_probability - probability <= BOCPD_NUMERICAL_TOL
    )
    return zero_probability, recent_probability, map_run_length, expected_run_length


def _alarm_state(observations_since_reset: int, recent_probability: float) -> str:
    if observations_since_reset < BOCPD_MIN_HISTORY_POINTS:
        return BOCPD_WARMING
    if (recent_probability >= BOCPD_ALARM_THRESHOLD
            or math.isclose(recent_probability, BOCPD_ALARM_THRESHOLD,
                            rel_tol=0.0, abs_tol=BOCPD_NUMERICAL_TOL)):
        return BOCPD_CHANGE
    return BOCPD_NONE


def transform_market_movement_with_bocpd(
    evaluation: MarketMovementEvaluation,
    config: BOCPDConfig,
    state: BOCPDCandidateState | None = None,
) -> tuple[BOCPDObservation, BOCPDCandidateState | None]:
    """Process one raw canonical #71 5m median movement observation causally."""
    if not isinstance(evaluation, MarketMovementEvaluation):
        raise ValueError("evaluation must be MarketMovementEvaluation")
    if not isinstance(config, BOCPDConfig):
        raise ValueError("config must be BOCPDConfig")
    if state is not None and not isinstance(state, BOCPDCandidateState):
        raise ValueError("state must be BOCPDCandidateState or None")

    boundary = evaluation.evaluation_boundary_time_ms
    raw = evaluation.windows[5].aggregates.median_normalized_movement
    value = _usable_metric(raw)
    if value is None:
        return BOCPDObservation(
            boundary, raw, False, BOCPD_UNAVAILABLE, None, None, None, None, 0, 0,
            BOCPD_ALGORITHM_VERSION, config.version,
        ), None

    continuous = (
        _state_matches(state, evaluation, config)
        and boundary == state.last_evaluation_boundary_time_ms
        + BOCPD_EVALUATION_INTERVAL_MS
    )
    if continuous:
        previous_hypotheses = state.hypotheses
        previous_count = state.observations_since_reset
    else:
        previous_hypotheses = (BOCPDRunLengthHypothesis(
            0, 0.0, config.prior_mean, config.prior_mean_variance,
        ),)
        previous_count = 0

    hazard = config.hazard
    log_hazard = math.log(hazard)
    log_survival = math.log1p(-hazard)
    log_predictives = tuple(
        _log_normal_density(
            value,
            hypothesis.posterior_mean,
            config.observation_variance + hypothesis.posterior_mean_variance,
        )
        for hypothesis in previous_hypotheses
    )
    log_cp = _logsumexp(
        hypothesis.log_probability + log_hazard + log_predictive
        for hypothesis, log_predictive in zip(previous_hypotheses, log_predictives)
    )
    growth_log_probabilities = tuple(
        hypothesis.log_probability + log_survival + log_predictive
        for hypothesis, log_predictive in zip(previous_hypotheses, log_predictives)
    )
    log_normalizer = _logsumexp((log_cp, *growth_log_probabilities))
    next_hypotheses = [BOCPDRunLengthHypothesis(
        0,
        log_cp - log_normalizer,
        config.prior_mean,
        config.prior_mean_variance,
    )]
    for hypothesis, log_growth in zip(previous_hypotheses, growth_log_probabilities):
        posterior_mean, posterior_variance = _assimilate(
            hypothesis.posterior_mean,
            hypothesis.posterior_mean_variance,
            value,
            config.observation_variance,
        )
        next_hypotheses.append(BOCPDRunLengthHypothesis(
            hypothesis.run_length_steps + 1,
            log_growth - log_normalizer,
            posterior_mean,
            posterior_variance,
        ))
    hypotheses = tuple(next_hypotheses)
    observations_since_reset = previous_count + 1
    next_state = BOCPDCandidateState(
        BOCPD_ALGORITHM_VERSION,
        config.version,
        config.expected_run_length_points,
        config.hazard,
        config.prior_mean,
        config.prior_mean_variance,
        config.observation_variance,
        evaluation.algorithm_version,
        evaluation.config_version,
        evaluation.universe_id,
        evaluation.universe_version,
        evaluation.provider,
        evaluation.exchange,
        evaluation.price_type,
        boundary,
        observations_since_reset,
        hypotheses,
    )
    zero_probability, recent_probability, map_run_length, expected_run_length = (
        _posterior_diagnostics(hypotheses)
    )
    observation = BOCPDObservation(
        boundary,
        raw,
        True,
        _alarm_state(observations_since_reset, recent_probability),
        zero_probability,
        recent_probability,
        map_run_length,
        expected_run_length,
        len(hypotheses),
        observations_since_reset,
        BOCPD_ALGORITHM_VERSION,
        config.version,
    )
    return observation, next_state


def detection_regions(
    points: tuple[BOCPDExperimentPoint, ...],
) -> tuple[BOCPDDetectionRegion, ...]:
    """Build causal CHANGE regions, leaving a final active region censored."""
    if not points:
        return ()
    observed_through = points[-1].evaluation_boundary_time_ms
    active_start = None
    regions = []
    for point in points:
        boundary = point.evaluation_boundary_time_ms
        is_change = point.bocpd_observation.detector_state == BOCPD_CHANGE
        if active_start is not None and not is_change:
            regions.append(BOCPDDetectionRegion(
                active_start, boundary, observed_through,
            ))
            active_start = None
        if is_change and active_start is None:
            active_start = boundary
    if active_start is not None:
        regions.append(BOCPDDetectionRegion(
            active_start, None, observed_through,
        ))
    return tuple(regions)


def _region_is_attributable(
    region: BOCPDDetectionRegion,
    selected_points: tuple[BOCPDExperimentPoint, ...],
    partition: str,
) -> bool:
    return partition == "all" or region.start_boundary_time_ms in {
        point.evaluation_boundary_time_ms for point in selected_points
    }


def _match_v1_onsets(
    selected_points: tuple[BOCPDExperimentPoint, ...],
    regions: tuple[BOCPDDetectionRegion, ...],
) -> tuple[int, int, float | None, float | None, frozenset[int]]:
    """One-to-one onset matching; nearest detector onset wins, earlier breaks ties."""
    used: set[int] = set()
    signed_deltas = []
    unmatched = 0
    onsets = tuple(
        transition
        for point in selected_points
        for transition in point.baseline_transitions
        if transition.transition in ("STARTED", "REVERSED")
    )
    for transition in onsets:
        onset = transition.evaluation_boundary_time_ms
        candidates = []
        for index, region in enumerate(regions):
            if index in used:
                continue
            delta = region.start_boundary_time_ms - onset
            if abs(delta) <= BOCPD_TRANSITION_MATCH_WINDOW_MS:
                candidates.append((abs(delta), region.start_boundary_time_ms, index, delta))
        if not candidates:
            unmatched += 1
            continue
        _, _, index, delta = min(candidates)
        used.add(index)
        signed_deltas.append(float(delta))
    return (
        len(signed_deltas),
        unmatched,
        float(median(signed_deltas)) if signed_deltas else None,
        float(median(tuple(abs(delta) for delta in signed_deltas)))
        if signed_deltas else None,
        frozenset(used),
    )


def _summary(points: tuple[BOCPDExperimentPoint, ...], partition: str) -> BOCPDComparisonSummary:
    selected = selected_points_for_partition(points, partition)
    observed = observed_points_for_partition(points, partition)
    observed_regions = detection_regions(observed)
    attributable_indices = tuple(
        index for index, region in enumerate(observed_regions)
        if _region_is_attributable(region, selected, partition)
    )
    attributable_regions = tuple(observed_regions[index] for index in attributable_indices)
    spans = episode_spans(observed, "baseline")
    matched, unmatched_baseline, median_signed, median_absolute, used = _match_v1_onsets(
        selected, observed_regions,
    )
    eligible = tuple(
        point.bocpd_observation for point in selected
        if point.bocpd_observation.available
        and point.bocpd_observation.detector_state in (BOCPD_NONE, BOCPD_CHANGE)
    )
    recent_values = tuple(item.recent_change_probability for item in eligible)
    expected_values = tuple(item.expected_run_length_steps for item in eligible)
    hypothesis_counts = tuple(item.hypothesis_count for item in eligible)
    return BOCPDComparisonSummary(
        partition=partition,
        evaluation_count=len(selected),
        bocpd_usable_count=sum(point.bocpd_observation.available for point in selected),
        bocpd_unavailable_count=sum(
            not point.bocpd_observation.available for point in selected),
        bocpd_warming_count=sum(
            point.bocpd_observation.detector_state == BOCPD_WARMING
            for point in selected),
        bocpd_change_boundary_count=sum(
            point.bocpd_observation.detector_state == BOCPD_CHANGE
            for point in selected),
        bocpd_detection_region_count=len(attributable_regions),
        bocpd_short_lived_closed_region_count=sum(
            region.end_boundary_time_ms is not None
            and region.end_boundary_time_ms - region.start_boundary_time_ms < 30_000
            for region in attributable_regions
        ),
        baseline_transition_counts=transition_counts(selected, "baseline"),
        baseline_directional_onset_count=directional_onset_count(selected, "baseline"),
        baseline_episode_count=episode_count(spans, selected, partition),
        baseline_short_lived_closed_episode_count=short_lived_episode_count(
            spans, selected, partition),
        matched_baseline_onset_count=matched,
        unmatched_baseline_onset_count=unmatched_baseline,
        unmatched_bocpd_detection_region_count=sum(
            index not in used for index in attributable_indices),
        median_signed_bocpd_minus_v1_onset_ms=median_signed,
        median_absolute_bocpd_v1_offset_ms=median_absolute,
        median_recent_change_probability=(
            float(median(recent_values)) if recent_values else None),
        maximum_recent_change_probability=max(recent_values) if recent_values else None,
        median_expected_run_length_steps=(
            float(median(expected_values)) if expected_values else None),
        maximum_hypothesis_count=max(hypothesis_counts) if hypothesis_counts else None,
    )


def run_market_state_bocpd_experiment(
    points: Iterable[MarketStateExperimentPoint],
    config: BOCPDConfig,
    *,
    classifier_config: MarketClassifierConfig | None = None,
    lifecycle_config: MarketEpisodeLifecycleConfig | None = None,
) -> MarketStateBOCPDExperimentResult:
    """Run one exact BOCPD stream beside one unchanged canonical V1 branch."""
    if not isinstance(config, BOCPDConfig):
        raise ValueError("config must be BOCPDConfig")
    classifier_config = MarketClassifierConfig() if classifier_config is None else classifier_config
    lifecycle_config = MarketEpisodeLifecycleConfig() if lifecycle_config is None else lifecycle_config
    if not isinstance(classifier_config, MarketClassifierConfig):
        raise ValueError("classifier_config must be MarketClassifierConfig")
    if not isinstance(lifecycle_config, MarketEpisodeLifecycleConfig):
        raise ValueError("lifecycle_config must be MarketEpisodeLifecycleConfig")
    points = tuple(points)
    validate_experiment_points(points)

    baseline_state: MarketEpisodeLifecycleState | None = None
    bocpd_state: BOCPDCandidateState | None = None
    results = []
    for point in points:
        evaluation = point.movement_evaluation
        classification, lifecycle = advance_canonical_branch(
            evaluation,
            point.source_time_evidence,
            baseline_state,
            classifier_config,
            lifecycle_config,
        )
        observation, bocpd_state = transform_market_movement_with_bocpd(
            evaluation, config, bocpd_state,
        )
        results.append(BOCPDExperimentPoint(
            movement_evaluation=evaluation,
            source_time_evidence=point.source_time_evidence,
            evaluation_boundary_time_ms=evaluation.evaluation_boundary_time_ms,
            partition=point.partition,
            bocpd_observation=observation,
            baseline_classification=classification,
            baseline_lifecycle_state=lifecycle.next_state,
            baseline_transitions=lifecycle.transitions,
        ))
        baseline_state = lifecycle.next_state

    result_points = tuple(results)
    partitions = ("all", "development", "validation", "test")
    regions_by_partition = {
        partition: detection_regions(observed_points_for_partition(result_points, partition))
        for partition in partitions
    }
    summaries = {
        partition: _summary(result_points, partition)
        for partition in partitions
    }
    return MarketStateBOCPDExperimentResult(
        config, result_points, regions_by_partition, summaries,
    )
