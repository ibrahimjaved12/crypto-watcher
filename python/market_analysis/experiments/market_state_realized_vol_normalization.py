"""EXP-75-06A: strict-prior realized-volatility normalization research."""

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
from ..movement_metrics import (
    ALGORITHM_VERSION as BASELINE_MOVEMENT_ALGORITHM_VERSION,
    DEFAULT_CONFIG_VERSION as BASELINE_MOVEMENT_CONFIG_VERSION,
    MarketMovementConfig,
    MarketMovementEvaluation,
    Metric,
    WindowAggregates,
    WindowBreadth,
    WINDOWS,
    _aggregates,
    _breadth,
    _outliers,
)
from .market_state_common import (
    EXPERIMENT_EVALUATION_INTERVAL_MS,
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


REALIZED_VOLATILITY_ALGORITHM_VERSION = "market-movement-realized-vol-normalization-v1"
REALIZED_VOL_CONFIG_VERSION_PREFIX = "market-movement-realized-vol-normalization-config-v1"
REALIZED_VOL_LOOKBACK_POINTS = (30, 60, 120)
REALIZED_VOL_SAMPLE_INTERVAL_MS = 60_000
REALIZED_VOL_EVALUATION_INTERVAL_MS = EXPERIMENT_EVALUATION_INTERVAL_MS
REALIZED_VOL_WARMING = "REALIZED_VOLATILITY_WARMING"
REALIZED_VOL_ZERO_SCALE = "REALIZED_VOLATILITY_ZERO_SCALE"
REALIZED_VOL_SCALE_UNAVAILABLE = "REALIZED_VOLATILITY_SCALE_UNAVAILABLE"
REALIZED_VOL_NORMALIZATION_UNAVAILABLE = "REALIZED_VOLATILITY_NORMALIZATION_UNAVAILABLE"
_BASELINE_CONFIG = MarketMovementConfig()


def _config_version(lookback_points: int) -> str:
    return (f"{REALIZED_VOL_CONFIG_VERSION_PREFIX}:input-canonical-aligned-1m-return"
            f":sample-60000ms:strict-prior-true:estimator-rms"
            f":center-canonical-historical-median:scaling-sqrt-minutes"
            f":points-{lookback_points}")


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class RealizedVolatilityNormalizationConfig:
    version: str
    lookback_points: int
    sample_interval_ms: int = REALIZED_VOL_SAMPLE_INTERVAL_MS
    strict_prior: bool = True
    estimator: str = "rms"
    center: str = "canonical-historical-median"
    horizon_scaling: str = "sqrt-minutes"
    input_series: str = "canonical-aligned-1m-return"

    def __post_init__(self):
        if (type(self.lookback_points) is not int
                or self.lookback_points not in REALIZED_VOL_LOOKBACK_POINTS
                or type(self.sample_interval_ms) is not int
                or self.sample_interval_ms != REALIZED_VOL_SAMPLE_INTERVAL_MS
                or self.strict_prior is not True or self.estimator != "rms"
                or self.center != "canonical-historical-median"
                or self.horizon_scaling != "sqrt-minutes"
                or self.input_series != "canonical-aligned-1m-return"
                or self.version != _config_version(self.lookback_points)):
            raise ValueError("realized-volatility config must identify a preregistered model")


REALIZED_VOL_CONFIG_30M = RealizedVolatilityNormalizationConfig(_config_version(30), 30)
REALIZED_VOL_CONFIG_60M = RealizedVolatilityNormalizationConfig(_config_version(60), 60)
REALIZED_VOL_CONFIG_120M = RealizedVolatilityNormalizationConfig(_config_version(120), 120)
REALIZED_VOL_CONFIGURATIONS = (
    REALIZED_VOL_CONFIG_30M, REALIZED_VOL_CONFIG_60M, REALIZED_VOL_CONFIG_120M,
)


@dataclass(frozen=True)
class RealizedVolatilitySample:
    evaluation_boundary_time_ms: int
    one_minute_return: float

    def __post_init__(self):
        boundary = self.evaluation_boundary_time_ms
        if (type(boundary) is not int or boundary < 0
                or boundary % REALIZED_VOL_SAMPLE_INTERVAL_MS):
            raise ValueError("RV sample boundary must be minute aligned")
        if _finite(self.one_minute_return) is None:
            raise ValueError("RV sample return must be finite")


@dataclass(frozen=True)
class RealizedVolatilitySymbolHistory:
    symbol: str
    samples: tuple[RealizedVolatilitySample, ...]

    def __post_init__(self):
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("RV history symbol is required")
        samples = tuple(self.samples)
        if any(not isinstance(sample, RealizedVolatilitySample) for sample in samples):
            raise ValueError("RV history sample type is invalid")
        if any(right.evaluation_boundary_time_ms !=
               left.evaluation_boundary_time_ms + REALIZED_VOL_SAMPLE_INTERVAL_MS
               for left, right in zip(samples, samples[1:])):
            raise ValueError("RV history samples must be consecutive completed minutes")
        object.__setattr__(self, "samples", samples)


@dataclass(frozen=True)
class RealizedVolatilityCandidateState:
    candidate_algorithm_version: str
    candidate_config_version: str
    lookback_points: int
    sample_interval_ms: int
    baseline_movement_algorithm_version: str
    baseline_movement_config_version: str
    universe_id: str
    universe_version: str
    configured_universe: tuple[str, ...]
    provider: str
    exchange: str
    price_type: str
    last_evaluation_boundary_time_ms: int
    symbol_histories: tuple[RealizedVolatilitySymbolHistory, ...]

    def __post_init__(self):
        if (self.candidate_algorithm_version != REALIZED_VOLATILITY_ALGORITHM_VERSION
                or type(self.lookback_points) is not int
                or self.lookback_points not in REALIZED_VOL_LOOKBACK_POINTS
                or self.candidate_config_version != _config_version(self.lookback_points)
                or type(self.sample_interval_ms) is not int
                or self.sample_interval_ms != REALIZED_VOL_SAMPLE_INTERVAL_MS):
            raise ValueError("RV state algorithm/config identity is invalid")
        for name in ("baseline_movement_algorithm_version",
                     "baseline_movement_config_version", "universe_id", "universe_version",
                     "provider", "exchange", "price_type"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} is required")
        boundary = self.last_evaluation_boundary_time_ms
        if (type(boundary) is not int or boundary < 0
                or boundary % REALIZED_VOL_EVALUATION_INTERVAL_MS):
            raise ValueError("RV state boundary must be 5-second aligned")
        universe = tuple(self.configured_universe)
        histories = tuple(self.symbol_histories)
        if (any(not isinstance(symbol, str) or not symbol for symbol in universe)
                or len(set(universe)) != len(universe)
                or any(not isinstance(item, RealizedVolatilitySymbolHistory) for item in histories)
                or tuple(item.symbol for item in histories) != universe):
            raise ValueError("RV state histories must match configured universe order")
        for history in histories:
            if len(history.samples) > self.lookback_points:
                raise ValueError("RV history exceeds configured lookback")
            if history.samples:
                age = boundary - history.samples[-1].evaluation_boundary_time_ms
                if age < 0 or age >= REALIZED_VOL_SAMPLE_INTERVAL_MS:
                    raise ValueError("RV history must end at the latest usable minute")
        object.__setattr__(self, "configured_universe", universe)
        object.__setattr__(self, "symbol_histories", histories)


def _validate_baseline(evaluation: MarketMovementEvaluation) -> None:
    if not isinstance(evaluation, MarketMovementEvaluation):
        raise ValueError("evaluation must be MarketMovementEvaluation")
    if (evaluation.algorithm_version != BASELINE_MOVEMENT_ALGORITHM_VERSION
            or evaluation.config_version != BASELINE_MOVEMENT_CONFIG_VERSION
            or set(evaluation.windows) != set(WINDOWS)):
        raise ValueError("RV experiment requires canonical V1 #71 movement evaluation")
    for minute in WINDOWS:
        window = evaluation.windows[minute]
        if (window.window_minutes != minute
                or window.algorithm_version != BASELINE_MOVEMENT_ALGORITHM_VERSION
                or window.config_version != BASELINE_MOVEMENT_CONFIG_VERSION
                or window.configured_universe != evaluation.configured_universe
                or window.universe_id != evaluation.universe_id
                or window.universe_version != evaluation.universe_version
                or window.provider != evaluation.provider
                or window.exchange != evaluation.exchange
                or window.price_type != evaluation.price_type
                or window.evaluation_boundary_time_ms != evaluation.evaluation_boundary_time_ms):
            raise ValueError("RV experiment window does not match canonical V1 scope")


def _same_scope(state, evaluation, config) -> bool:
    return state is not None and (
        state.candidate_algorithm_version == REALIZED_VOLATILITY_ALGORITHM_VERSION
        and state.candidate_config_version == config.version
        and state.lookback_points == config.lookback_points
        and state.sample_interval_ms == config.sample_interval_ms
        and state.baseline_movement_algorithm_version == evaluation.algorithm_version
        and state.baseline_movement_config_version == evaluation.config_version
        and state.universe_id == evaluation.universe_id
        and state.universe_version == evaluation.universe_version
        and state.configured_universe == evaluation.configured_universe
        and state.provider == evaluation.provider
        and state.exchange == evaluation.exchange
        and state.price_type == evaluation.price_type
        and evaluation.evaluation_boundary_time_ms ==
        state.last_evaluation_boundary_time_ms + REALIZED_VOL_EVALUATION_INTERVAL_MS
    )


def _prior_histories(state, evaluation, config):
    return ({item.symbol: item.samples for item in state.symbol_histories}
            if _same_scope(state, evaluation, config) else {})


def _realized_sigma_1m(samples: tuple[RealizedVolatilitySample, ...],
                       lookback_points: int) -> Metric[float]:
    """RMS of exactly N prior, consecutive, non-overlapping one-minute returns."""
    if len(samples) < lookback_points:
        return Metric.missing(REALIZED_VOL_WARMING)
    if len(samples) != lookback_points:
        raise ValueError("RV estimator received an invalid history length")
    try:
        variance = math.fsum(sample.one_minute_return ** 2 for sample in samples) / lookback_points
        sigma = math.sqrt(variance)
    except (OverflowError, ValueError):
        return Metric.missing(REALIZED_VOL_SCALE_UNAVAILABLE)
    if not math.isfinite(variance) or not math.isfinite(sigma):
        return Metric.missing(REALIZED_VOL_SCALE_UNAVAILABLE)
    if sigma <= 0:
        return Metric.missing(REALIZED_VOL_ZERO_SCALE)
    return Metric.present(sigma)


def _horizon_sigma(sigma_1m: Metric[float], window_minutes: int) -> Metric[float]:
    if not sigma_1m.available:
        return Metric.missing(sigma_1m.reason)
    scale = sigma_1m.value * math.sqrt(window_minutes)
    return (Metric.present(scale) if math.isfinite(scale) and scale > 0
            else Metric.missing(REALIZED_VOL_SCALE_UNAVAILABLE))


def _candidate_z(current_return: Metric[float], historical_median: Metric[float],
                 sigma_w: Metric[float]) -> Metric[float]:
    if not sigma_w.available:
        return Metric.missing(sigma_w.reason)
    if sigma_w.value <= 0:
        return Metric.missing(REALIZED_VOL_ZERO_SCALE)
    current = _finite(current_return.value) if current_return.available else None
    center = _finite(historical_median.value) if historical_median.available else None
    if current is None or center is None:
        return Metric.missing(REALIZED_VOL_NORMALIZATION_UNAVAILABLE)
    z = (current - center) / sigma_w.value
    return (Metric.present(z) if math.isfinite(z)
            else Metric.missing(REALIZED_VOL_NORMALIZATION_UNAVAILABLE))


def _missing_window_evidence(window, reason):
    missing = Metric.missing(reason)
    breadth = WindowBreadth(False, reason, window.eligible_count,
                            missing, missing, missing, missing, missing)
    aggregates = WindowAggregates(
        missing, window.aggregates.median_raw_return,
        missing, missing, missing, missing,
    )
    return breadth, aggregates


def _candidate_symbol(item, sigma_w, config):
    if not item.included:
        return item
    z = _candidate_z(item.current_return, item.historical_median, sigma_w)
    if not z.available:
        return replace(item, normalized_z=z, direction=Metric.missing(z.reason),
                       material_rising=False, material_falling=False,
                       outlier_candidate=False)
    raw = item.current_return.value
    if item.normalized_z.available:
        baseline_z = _finite(item.normalized_z.value)
        if (baseline_z is not None and baseline_z != 0 and z.value != 0
                and (baseline_z > 0) != (z.value > 0)):
            raise ValueError("RV normalization changed the centered-return sign")
    direction = ("FLAT" if raw == 0 or abs(z.value) < config.flat_z
                 else "RISING" if raw > 0 else "FALLING")
    return replace(
        item, normalized_z=z, direction=Metric.present(direction),
        material_rising=raw > 0 and abs(z.value) >= config.material_z,
        material_falling=raw < 0 and abs(z.value) >= config.material_z,
        outlier_candidate=False,
    )


def transform_market_movement_with_realized_vol_normalization(
    evaluation: MarketMovementEvaluation,
    config: RealizedVolatilityNormalizationConfig,
    state: RealizedVolatilityCandidateState | None = None,
) -> tuple[MarketMovementEvaluation, RealizedVolatilityCandidateState]:
    """Use prior aligned 1m returns, then append the current aligned return."""
    _validate_baseline(evaluation)
    if not isinstance(config, RealizedVolatilityNormalizationConfig):
        raise ValueError("config must be RealizedVolatilityNormalizationConfig")
    if state is not None and not isinstance(state, RealizedVolatilityCandidateState):
        raise ValueError("state must be RealizedVolatilityCandidateState or None")
    boundary = evaluation.evaluation_boundary_time_ms
    if (type(boundary) is not int or boundary < 0
            or boundary % REALIZED_VOL_EVALUATION_INTERVAL_MS):
        raise ValueError("evaluation boundary must be 5-second aligned")
    prior = _prior_histories(state, evaluation, config)
    sigmas = {symbol: _realized_sigma_1m(prior.get(symbol, ()), config.lookback_points)
              for symbol in evaluation.configured_universe}
    windows = {}
    for minute in WINDOWS:
        baseline = evaluation.windows[minute]
        results = tuple(_candidate_symbol(item, _horizon_sigma(sigmas[item.symbol], minute),
                                          _BASELINE_CONFIG)
                        for item in baseline.symbols)
        included = tuple(item for item in results if item.included)
        ready = baseline.market_wide_eligible and included and all(
            item.normalized_z.available for item in included)
        if ready:
            results = _outliers(results, _BASELINE_CONFIG)
            included = tuple(item for item in results if item.included)
            breadth = _breadth(included, True)
            aggregates = _aggregates(included, True, _BASELINE_CONFIG)
        elif not baseline.market_wide_eligible:
            breadth, aggregates = _missing_window_evidence(
                baseline, "MARKET_UNIVERSE_INELIGIBLE")
        else:
            breadth, aggregates = _missing_window_evidence(
                baseline, REALIZED_VOL_NORMALIZATION_UNAVAILABLE)
        windows[minute] = replace(
            baseline, algorithm_version=REALIZED_VOLATILITY_ALGORITHM_VERSION,
            config_version=config.version, symbols=results,
            breadth=breadth, aggregates=aggregates,
        )
    candidate = replace(
        evaluation, algorithm_version=REALIZED_VOLATILITY_ALGORITHM_VERSION,
        config_version=config.version, windows=windows,
    )
    # Strict prior: this mutation of immutable state happens after candidate construction.
    one_minute_results = {item.symbol: item for item in evaluation.windows[1].symbols}
    histories = []
    aligned = boundary % REALIZED_VOL_SAMPLE_INTERVAL_MS == 0
    for symbol in evaluation.configured_universe:
        samples = prior.get(symbol, ())
        if aligned:
            item = one_minute_results.get(symbol)
            current = (_finite(item.current_return.value)
                       if item is not None and item.included and item.current_return.available
                       else None)
            if current is None:
                samples = ()
            else:
                samples = (*samples, RealizedVolatilitySample(boundary, current))[
                    -config.lookback_points:]
        histories.append(RealizedVolatilitySymbolHistory(symbol, samples))
    next_state = RealizedVolatilityCandidateState(
        REALIZED_VOLATILITY_ALGORITHM_VERSION, config.version,
        config.lookback_points, config.sample_interval_ms,
        evaluation.algorithm_version, evaluation.config_version,
        evaluation.universe_id, evaluation.universe_version,
        evaluation.configured_universe, evaluation.provider, evaluation.exchange,
        evaluation.price_type, boundary, tuple(histories),
    )
    return candidate, next_state


@dataclass(frozen=True)
class RealizedVolatilitySymbolEvidence:
    symbol: str
    prior_history_count: int
    sigma_1m: Metric[float]
    sigma_by_window: tuple[tuple[int, Metric[float]], ...]
    baseline_z_by_window: tuple[tuple[int, Metric[float]], ...]
    candidate_z_by_window: tuple[tuple[int, Metric[float]], ...]
    aligned_sample_appended_after_evaluation: bool


@dataclass(frozen=True)
class PairedMarketStateRealizedVolatilityPoint:
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
    realized_volatility_state: RealizedVolatilityCandidateState
    symbol_evidence: tuple[RealizedVolatilitySymbolEvidence, ...]


@dataclass(frozen=True)
class RealizedVolatilitySymbolNormalizationDiagnostic:
    symbol: str
    comparable_primary_count: int
    median_abs_baseline_z: float | None
    median_abs_candidate_z: float | None
    median_absolute_z_difference: float | None


@dataclass(frozen=True)
class RealizedVolatilityComparisonSummary:
    partition: str
    evaluation_count: int
    candidate_ready_count_by_window: tuple[tuple[int, int], ...]
    candidate_unavailable_count_by_window: tuple[tuple[int, int], ...]
    comparable_z_count_by_window: tuple[tuple[int, int], ...]
    median_absolute_z_difference_by_window: tuple[tuple[int, float | None], ...]
    median_abs_baseline_z_by_window: tuple[tuple[int, float | None], ...]
    median_abs_candidate_z_by_window: tuple[tuple[int, float | None], ...]
    symbol_fairness_diagnostics: tuple[RealizedVolatilitySymbolNormalizationDiagnostic, ...]
    baseline_cross_symbol_median_abs_z_mad: float | None
    candidate_cross_symbol_median_abs_z_mad: float | None
    median_abs_rising_breadth_difference_by_window: tuple[tuple[int, float | None], ...]
    median_abs_falling_breadth_difference_by_window: tuple[tuple[int, float | None], ...]
    median_abs_material_rising_breadth_difference_by_window: tuple[tuple[int, float | None], ...]
    median_abs_material_falling_breadth_difference_by_window: tuple[tuple[int, float | None], ...]
    baseline_outlier_candidate_count: int
    candidate_outlier_candidate_count: int
    outlier_comparable_count: int
    both_outlier_count: int
    baseline_only_outlier_count: int
    candidate_only_outlier_count: int
    both_available_direction_count_by_window: tuple[tuple[int, int], ...]
    direction_state_disagreement_count_by_window: tuple[tuple[int, int], ...]
    direction_state_disagreement_fraction_by_window: tuple[tuple[int, float], ...]
    baseline_transition_counts: tuple[tuple[str, int], ...]
    candidate_transition_counts: tuple[tuple[str, int], ...]
    baseline_episode_count: int
    candidate_episode_count: int
    baseline_short_lived_closed_episode_count: int
    candidate_short_lived_closed_episode_count: int
    baseline_directional_onset_count: int
    candidate_directional_onset_count: int
    matched_baseline_onset_count: int
    unmatched_baseline_onset_count: int
    median_signed_candidate_minus_baseline_onset_ms: float | None
    baseline_active_boundary_count: int
    candidate_active_boundary_count: int
    both_active_boundary_count: int
    same_direction_active_boundary_count: int
    same_direction_both_active_fraction: float


@dataclass(frozen=True)
class MarketStateRealizedVolatilityExperimentResult:
    realized_volatility_config: RealizedVolatilityNormalizationConfig
    paired_points: tuple[PairedMarketStateRealizedVolatilityPoint, ...]
    summaries: Mapping[str, RealizedVolatilityComparisonSummary]

    def __post_init__(self):
        object.__setattr__(self, "paired_points", tuple(self.paired_points))
        object.__setattr__(self, "summaries", MappingProxyType(dict(self.summaries)))


def _median_or_none(values) -> float | None:
    return float(median(values)) if values else None


def _mad_or_none(values) -> float | None:
    if not values:
        return None
    center = median(values)
    return float(median(abs(value - center) for value in values))


def _z_pairs(points, minute):
    for point in points:
        before = {item.symbol: item for item in point.baseline_evaluation.windows[minute].symbols}
        for after in point.candidate_evaluation.windows[minute].symbols:
            baseline = before.get(after.symbol)
            if (baseline is not None and baseline.included and after.included
                    and baseline.normalized_z.available and after.normalized_z.available):
                yield baseline, after


def _included_pairs(points, minute):
    for point in points:
        before = {item.symbol: item for item in point.baseline_evaluation.windows[minute].symbols}
        for after in point.candidate_evaluation.windows[minute].symbols:
            baseline = before.get(after.symbol)
            if baseline is not None and baseline.included and after.included:
                yield baseline, after


def _breadth_difference(points, minute, side):
    differences = []
    for point in points:
        baseline = getattr(point.baseline_evaluation.windows[minute].breadth, side)
        candidate = getattr(point.candidate_evaluation.windows[minute].breadth, side)
        if baseline.available and candidate.available:
            differences.append(abs(candidate.value.fraction - baseline.value.fraction))
    return _median_or_none(differences)


def _summary(points: tuple, partition: str) -> RealizedVolatilityComparisonSummary:
    selected = selected_points_for_partition(points, partition)
    observed = observed_points_for_partition(points, partition)
    baseline_spans = episode_spans(observed, "baseline")
    candidate_spans = episode_spans(observed, "candidate")
    matched, unmatched, onset_delta = match_directional_onsets(
        observed, selected, baseline_spans, candidate_spans)
    z_pairs = {minute: tuple(_z_pairs(selected, minute)) for minute in WINDOWS}
    order = tuple(dict.fromkeys(symbol for point in selected
                                for symbol in point.baseline_evaluation.configured_universe))
    primary_by_symbol = {symbol: tuple((before, after) for before, after in z_pairs[5]
                                       if before.symbol == symbol)
                         for symbol in order}
    fairness = tuple(RealizedVolatilitySymbolNormalizationDiagnostic(
        symbol, len(pairs),
        _median_or_none(tuple(abs(before.normalized_z.value) for before, _ in pairs)),
        _median_or_none(tuple(abs(after.normalized_z.value) for _, after in pairs)),
        _median_or_none(tuple(abs(after.normalized_z.value - before.normalized_z.value)
                              for before, after in pairs)),
    ) for symbol, pairs in primary_by_symbol.items())
    outlier_pairs = tuple(pair for minute in WINDOWS for pair in z_pairs[minute])
    both_outliers = sum(before.outlier_candidate and after.outlier_candidate
                        for before, after in outlier_pairs)
    baseline_only = sum(before.outlier_candidate and not after.outlier_candidate
                        for before, after in outlier_pairs)
    candidate_only = sum(after.outlier_candidate and not before.outlier_candidate
                         for before, after in outlier_pairs)
    direction_counts = {}
    direction_disagreements = {}
    for minute in WINDOWS:
        comparable = tuple(point for point in selected
                           if point.baseline_classification.windows[minute].direction_state
                           not in ("WARMING", "UNAVAILABLE")
                           and point.candidate_classification.windows[minute].direction_state
                           not in ("WARMING", "UNAVAILABLE"))
        direction_counts[minute] = len(comparable)
        direction_disagreements[minute] = sum(
            point.baseline_classification.windows[minute].direction_state !=
            point.candidate_classification.windows[minute].direction_state
            for point in comparable)
    baseline_active = tuple(point for point in selected
                            if point.baseline_lifecycle_state.active_episode is not None)
    candidate_active = tuple(point for point in selected
                             if point.candidate_lifecycle_state.active_episode is not None)
    both_active = tuple(point for point in selected
                        if point.baseline_lifecycle_state.active_episode is not None
                        and point.candidate_lifecycle_state.active_episode is not None)
    same_direction = sum(
        point.baseline_lifecycle_state.active_episode.direction ==
        point.candidate_lifecycle_state.active_episode.direction
        for point in both_active)
    return RealizedVolatilityComparisonSummary(
        partition=partition, evaluation_count=len(selected),
        candidate_ready_count_by_window=tuple(
            (minute, sum(point.candidate_evaluation.windows[minute].breadth.available
                         for point in selected)) for minute in WINDOWS),
        candidate_unavailable_count_by_window=tuple(
            (minute, sum(not point.candidate_evaluation.windows[minute].breadth.available
                         for point in selected)) for minute in WINDOWS),
        comparable_z_count_by_window=tuple((minute, len(z_pairs[minute])) for minute in WINDOWS),
        median_absolute_z_difference_by_window=tuple(
            (minute, _median_or_none(tuple(abs(after.normalized_z.value - before.normalized_z.value)
                                           for before, after in z_pairs[minute])))
            for minute in WINDOWS),
        median_abs_baseline_z_by_window=tuple(
            (minute, _median_or_none(tuple(abs(before.normalized_z.value)
                                           for before, _ in z_pairs[minute])))
            for minute in WINDOWS),
        median_abs_candidate_z_by_window=tuple(
            (minute, _median_or_none(tuple(abs(after.normalized_z.value)
                                           for _, after in z_pairs[minute])))
            for minute in WINDOWS),
        symbol_fairness_diagnostics=fairness,
        baseline_cross_symbol_median_abs_z_mad=_mad_or_none(tuple(
            item.median_abs_baseline_z for item in fairness
            if item.median_abs_baseline_z is not None)),
        candidate_cross_symbol_median_abs_z_mad=_mad_or_none(tuple(
            item.median_abs_candidate_z for item in fairness
            if item.median_abs_candidate_z is not None)),
        median_abs_rising_breadth_difference_by_window=tuple(
            (minute, _breadth_difference(selected, minute, "rising")) for minute in WINDOWS),
        median_abs_falling_breadth_difference_by_window=tuple(
            (minute, _breadth_difference(selected, minute, "falling")) for minute in WINDOWS),
        median_abs_material_rising_breadth_difference_by_window=tuple(
            (minute, _breadth_difference(selected, minute, "material_rising"))
            for minute in WINDOWS),
        median_abs_material_falling_breadth_difference_by_window=tuple(
            (minute, _breadth_difference(selected, minute, "material_falling"))
            for minute in WINDOWS),
        baseline_outlier_candidate_count=both_outliers + baseline_only,
        candidate_outlier_candidate_count=both_outliers + candidate_only,
        outlier_comparable_count=len(outlier_pairs),
        both_outlier_count=both_outliers,
        baseline_only_outlier_count=baseline_only,
        candidate_only_outlier_count=candidate_only,
        both_available_direction_count_by_window=tuple(direction_counts.items()),
        direction_state_disagreement_count_by_window=tuple(direction_disagreements.items()),
        direction_state_disagreement_fraction_by_window=tuple(
            (minute, direction_disagreements[minute] / direction_counts[minute]
             if direction_counts[minute] else 0.0) for minute in WINDOWS),
        baseline_transition_counts=transition_counts(selected, "baseline"),
        candidate_transition_counts=transition_counts(selected, "candidate"),
        baseline_episode_count=episode_count(baseline_spans, selected, partition),
        candidate_episode_count=episode_count(candidate_spans, selected, partition),
        baseline_short_lived_closed_episode_count=short_lived_episode_count(
            baseline_spans, selected, partition),
        candidate_short_lived_closed_episode_count=short_lived_episode_count(
            candidate_spans, selected, partition),
        baseline_directional_onset_count=directional_onset_count(selected, "baseline"),
        candidate_directional_onset_count=directional_onset_count(selected, "candidate"),
        matched_baseline_onset_count=matched,
        unmatched_baseline_onset_count=unmatched,
        median_signed_candidate_minus_baseline_onset_ms=onset_delta,
        baseline_active_boundary_count=len(baseline_active),
        candidate_active_boundary_count=len(candidate_active),
        both_active_boundary_count=len(both_active),
        same_direction_active_boundary_count=same_direction,
        same_direction_both_active_fraction=(same_direction / len(both_active)
                                             if both_active else 0.0),
    )


def run_market_state_realized_vol_normalization_experiment(
    points: Iterable[MarketStateExperimentPoint],
    config: RealizedVolatilityNormalizationConfig,
    *,
    classifier_config: MarketClassifierConfig | None = None,
    lifecycle_config: MarketEpisodeLifecycleConfig | None = None,
    canonical_branch_by_boundary: Mapping[int, tuple] | None = None,
) -> MarketStateRealizedVolatilityExperimentResult:
    """Compare V1 with an independent causal RV candidate through #72/#73."""
    if not isinstance(config, RealizedVolatilityNormalizationConfig):
        raise ValueError("config must be RealizedVolatilityNormalizationConfig")
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
    rv_state = None
    paired = []
    for point in points:
        evaluation = point.movement_evaluation
        _validate_baseline(evaluation)
        prior = _prior_histories(rv_state, evaluation, config)
        candidate, next_rv_state = transform_market_movement_with_realized_vol_normalization(
            evaluation, config, rv_state)
        baseline_classification, baseline_result = canonical_branch_for_point(
            evaluation, point.source_time_evidence, baseline_state,
            classifier_config, lifecycle_config, canonical_branch_by_boundary)
        candidate_classification, candidate_result = advance_canonical_branch(
            candidate, point.source_time_evidence, candidate_lifecycle_state,
            classifier_config, lifecycle_config)
        evidence = []
        for symbol in evaluation.configured_universe:
            prior_samples = prior.get(symbol, ())
            sigma = _realized_sigma_1m(prior_samples, config.lookback_points)
            after_history = next(item for item in next_rv_state.symbol_histories
                                 if item.symbol == symbol)
            evidence.append(RealizedVolatilitySymbolEvidence(
                symbol, len(prior_samples), sigma,
                tuple((minute, _horizon_sigma(sigma, minute)) for minute in WINDOWS),
                tuple((minute, next(item.normalized_z
                                    for item in evaluation.windows[minute].symbols
                                    if item.symbol == symbol)) for minute in WINDOWS),
                tuple((minute, next(item.normalized_z
                                    for item in candidate.windows[minute].symbols
                                    if item.symbol == symbol)) for minute in WINDOWS),
                (bool(after_history.samples)
                 and after_history.samples[-1].evaluation_boundary_time_ms ==
                 evaluation.evaluation_boundary_time_ms),
            ))
        paired.append(PairedMarketStateRealizedVolatilityPoint(
            evaluation.evaluation_boundary_time_ms, point.partition,
            evaluation, candidate, baseline_classification, candidate_classification,
            baseline_result.next_state, candidate_result.next_state,
            baseline_result.transitions, candidate_result.transitions,
            next_rv_state, tuple(evidence),
        ))
        baseline_state = baseline_result.next_state
        candidate_lifecycle_state = candidate_result.next_state
        rv_state = next_rv_state
    paired = tuple(paired)
    summaries = {partition: _summary(paired, partition)
                 for partition in ("all", "development", "validation", "test")}
    return MarketStateRealizedVolatilityExperimentResult(config, paired, summaries)
