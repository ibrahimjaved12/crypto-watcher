"""EXP-75-09: development-trained Gaussian HMM, causal diagnostic replay only."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cmp_to_key
import hashlib
import json
import math
from statistics import median
from types import MappingProxyType, SimpleNamespace
from typing import Iterable, Mapping

from ..market_episode_lifecycle import (
    MarketEpisodeLifecycleConfig, MarketEpisodeLifecycleState, MarketEpisodeTransition,
)
from ..movement_classifier import MarketClassificationEvaluation, MarketClassifierConfig
from ..movement_metrics import (
    ALGORITHM_VERSION as BASELINE_MOVEMENT_ALGORITHM_VERSION,
    DEFAULT_CONFIG_VERSION as BASELINE_MOVEMENT_CONFIG_VERSION,
    MarketMovementEvaluation, WINDOWS,
)
from .market_state_common import (
    ExperimentPartition, MarketStateExperimentPoint, advance_canonical_branch,
    directional_onset_count, episode_count, episode_spans,
    observed_points_for_partition, selected_points_for_partition,
    short_lived_episode_count, transition_counts, validate_experiment_points,
)


HMM_ALGORITHM_VERSION = "market-state-gaussian-hmm-v1"
HMM_CONFIG_VERSION = (
    "market-state-gaussian-hmm-config-v1:states-3"
    ":feature-schema-market-state-hmm-features-v1:features-4:sample-60000ms"
    ":training-development-only:emissions-diagonal-gaussian:em-50"
    ":variance-floor-1e-4:probability-floor-1e-12"
    ":initial-pi-equal:initial-transition-090-005"
    ":initial-means-feature0-minus1-0-plus1:initial-variance-1"
    ":inference-causal-forward-filter"
)
HMM_FEATURE_SCHEMA = "market-state-hmm-features-v1"
HMM_FEATURE_NAMES = (
    "median_normalized_movement", "material_breadth_imbalance",
    "dispersion_mad_normalized_movement", "median_rvol",
)
HMM_STATE_NAMES = ("LOW_MOVEMENT", "MID_MOVEMENT", "HIGH_MOVEMENT")
HMM_STATE_COUNT = 3
HMM_FEATURE_COUNT = 4
HMM_SAMPLE_INTERVAL_MS = 60_000
HMM_MIN_TRAINING_ROWS = 240
HMM_MIN_TRAINING_TRANSITIONS = 120
HMM_EM_ITERATIONS = 50
HMM_VARIANCE_FLOOR = 1e-4
HMM_PROBABILITY_FLOOR = 1e-12
HMM_NUMERICAL_TOL = 1e-12
HMM_NOT_SCHEDULED = "HMM_NOT_SCHEDULED"
HMM_TRAINING_PARTITION = "HMM_TRAINING_PARTITION"
HMM_READY = "HMM_READY"
HMM_FEATURE_UNAVAILABLE = "HMM_FEATURE_UNAVAILABLE"
HMM_TRAINING_UNAVAILABLE = "HMM_TRAINING_UNAVAILABLE"
HMM_INSUFFICIENT_TRAINING_ROWS = "HMM_INSUFFICIENT_TRAINING_ROWS"
HMM_INSUFFICIENT_TRAINING_TRANSITIONS = "HMM_INSUFFICIENT_TRAINING_TRANSITIONS"
HMM_TRAINING_ZERO_VARIANCE = "HMM_TRAINING_ZERO_VARIANCE"
HMM_TRAINING_DEGENERATE = "HMM_TRAINING_DEGENERATE"
HMM_TRAINING_NUMERIC_UNAVAILABLE = "HMM_TRAINING_NUMERIC_UNAVAILABLE"
_LOG_TWO_PI = math.log(2 * math.pi)


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class HMMRegimeConfig:
    version: str = HMM_CONFIG_VERSION
    state_count: int = HMM_STATE_COUNT
    feature_schema: str = HMM_FEATURE_SCHEMA
    feature_count: int = HMM_FEATURE_COUNT
    sample_interval_ms: int = HMM_SAMPLE_INTERVAL_MS
    training_partition: str = "development"
    emission_family: str = "diagonal-gaussian"
    em_iterations: int = HMM_EM_ITERATIONS
    variance_floor: float = HMM_VARIANCE_FLOOR
    probability_floor: float = HMM_PROBABILITY_FLOOR
    initialization: str = "fixed"
    inference: str = "causal-forward-filter"

    def __post_init__(self):
        expected = HMMRegimeConfig.__dataclass_fields__
        fixed = {name: field.default for name, field in expected.items()}
        if any(type(getattr(self, name)) is not type(value) or getattr(self, name) != value
               for name, value in fixed.items()):
            raise ValueError("HMM config must be the preregistered V1 model")


HMM_CONFIG_V1 = HMMRegimeConfig()


@dataclass(frozen=True)
class HMMFeatureRow:
    evaluation_boundary_time_ms: int
    values: tuple[float, float, float, float]

    def __post_init__(self):
        if (type(self.evaluation_boundary_time_ms) is not int
                or self.evaluation_boundary_time_ms < 0
                or self.evaluation_boundary_time_ms % HMM_SAMPLE_INTERVAL_MS):
            raise ValueError("HMM feature row must be minute aligned")
        values = tuple(self.values)
        if len(values) != HMM_FEATURE_COUNT or any(_finite(value) is None for value in values):
            raise ValueError("HMM feature vector requires four finite values")
        object.__setattr__(self, "values", tuple(float(value) for value in values))


@dataclass(frozen=True)
class HMMDevelopmentTrainingBlock:
    """Outcome-blind HMM feature evidence for one frozen development day."""

    study_period_index: int
    utc_date: str
    start_boundary_time_ms: int
    end_boundary_time_ms: int
    movement_scope: tuple
    feature_blocks: tuple[tuple[HMMFeatureRow, ...], ...]
    unavailable_row_count: int
    block_sha256: str = ""

    def __post_init__(self):
        feature_blocks = tuple(tuple(block) for block in self.feature_blocks)
        if (type(self.study_period_index) is not int or self.study_period_index < 0
                or not isinstance(self.utc_date, str) or not self.utc_date
                or type(self.start_boundary_time_ms) is not int
                or type(self.end_boundary_time_ms) is not int
                or self.end_boundary_time_ms - self.start_boundary_time_ms != 86_400_000
                or self.start_boundary_time_ms % HMM_SAMPLE_INTERVAL_MS
                or self.end_boundary_time_ms % HMM_SAMPLE_INTERVAL_MS
                or not self.movement_scope
                or any(not block or any(not isinstance(row, HMMFeatureRow) for row in block)
                       for block in feature_blocks)
                or type(self.unavailable_row_count) is not int
                or self.unavailable_row_count < 0):
            raise ValueError("invalid frozen HMM development day block")
        previous = None
        for block in feature_blocks:
            if any(right.evaluation_boundary_time_ms != left.evaluation_boundary_time_ms
                   + HMM_SAMPLE_INTERVAL_MS for left, right in zip(block, block[1:])):
                raise ValueError("HMM feature sub-blocks must be contiguous")
            if previous is not None and block[0].evaluation_boundary_time_ms <= previous:
                raise ValueError("HMM feature sub-blocks must be chronological")
            previous = block[-1].evaluation_boundary_time_ms
        payload = {
            "study_period_index": self.study_period_index,
            "utc_date": self.utc_date,
            "start_boundary_time_ms": self.start_boundary_time_ms,
            "end_boundary_time_ms": self.end_boundary_time_ms,
            "movement_scope": self.movement_scope,
            "feature_blocks": tuple(tuple((row.evaluation_boundary_time_ms,
                                             tuple(value.hex() for value in row.values))
                                            for row in block)
                                     for block in feature_blocks),
            "unavailable_row_count": self.unavailable_row_count,
        }
        fingerprint = hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True).encode("ascii")).hexdigest()
        if self.block_sha256 and self.block_sha256 != fingerprint:
            raise ValueError("HMM development block SHA-256 mismatch")
        object.__setattr__(self, "feature_blocks", feature_blocks)
        object.__setattr__(self, "movement_scope", tuple(self.movement_scope))
        object.__setattr__(self, "block_sha256", fingerprint)


@dataclass(frozen=True)
class HMMTrainingDiagnostics:
    status: str
    reason: str | None
    usable_row_count: int
    unavailable_row_count: int
    block_count: int
    transition_count: int
    first_usable_boundary_time_ms: int | None
    last_usable_boundary_time_ms: int | None
    training_data_sha256: str


def _probabilities(values, *, floor=0.0):
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ArithmeticError("invalid probability mass")
    total = math.fsum(values)
    if total <= HMM_NUMERICAL_TOL or not math.isfinite(total):
        raise ArithmeticError("probability mass is degenerate")
    normalized = tuple(value / total for value in values)
    floored = tuple(max(value, floor) for value in normalized)
    floored_total = math.fsum(floored)
    return tuple(value / floored_total for value in floored)


@dataclass(frozen=True)
class GaussianHMMModelArtifact:
    algorithm_version: str
    config_version: str
    feature_schema_version: str
    state_names: tuple[str, str, str]
    training_partition: str
    training_usable_row_count: int
    training_unavailable_row_count: int
    training_block_count: int
    training_transition_count: int
    training_first_usable_boundary_time_ms: int
    training_last_usable_boundary_time_ms: int
    feature_means: tuple[float, float, float, float]
    feature_population_stds: tuple[float, float, float, float]
    pi: tuple[float, float, float]
    transition_matrix: tuple[tuple[float, float, float], ...]
    emission_means: tuple[tuple[float, float, float, float], ...]
    emission_variances: tuple[tuple[float, float, float, float], ...]
    em_iteration_count: int
    initial_development_log_likelihood: float
    final_development_log_likelihood: float
    final_development_log_likelihood_per_row: float
    training_data_sha256: str
    model_sha256: str

    def __post_init__(self):
        vectors = (self.state_names, self.feature_means, self.feature_population_stds,
                   self.pi, self.transition_matrix, self.emission_means,
                   self.emission_variances)
        if (any(type(vector) is not tuple for vector in vectors)
                or any(type(row) is not tuple for matrix in (
                    self.transition_matrix, self.emission_means, self.emission_variances)
                    for row in matrix)):
            raise ValueError("HMM artifact parameters must be immutable tuples")
        if (self.algorithm_version != HMM_ALGORITHM_VERSION
                or self.config_version != HMM_CONFIG_VERSION
                or self.feature_schema_version != HMM_FEATURE_SCHEMA
                or self.state_names != HMM_STATE_NAMES
                or self.training_partition != "development"
                or self.em_iteration_count != HMM_EM_ITERATIONS
                or type(self.training_usable_row_count) is not int
                or type(self.training_unavailable_row_count) is not int
                or type(self.training_block_count) is not int
                or type(self.training_transition_count) is not int
                or type(self.training_first_usable_boundary_time_ms) is not int
                or type(self.training_last_usable_boundary_time_ms) is not int
                or self.training_usable_row_count < HMM_MIN_TRAINING_ROWS
                or self.training_unavailable_row_count < 0
                or self.training_transition_count < HMM_MIN_TRAINING_TRANSITIONS
                or self.training_block_count < 1
                or self.training_transition_count != self.training_usable_row_count - self.training_block_count
                or self.training_first_usable_boundary_time_ms < 0
                or self.training_first_usable_boundary_time_ms % HMM_SAMPLE_INTERVAL_MS
                or self.training_last_usable_boundary_time_ms % HMM_SAMPLE_INTERVAL_MS
                or self.training_first_usable_boundary_time_ms > self.training_last_usable_boundary_time_ms):
            raise ValueError("invalid HMM artifact identity or training counts")
        if (len(self.feature_means) != HMM_FEATURE_COUNT
                or len(self.feature_population_stds) != HMM_FEATURE_COUNT
                or any(not math.isfinite(value) for value in self.feature_means)
                or any(not math.isfinite(value) or value <= HMM_NUMERICAL_TOL
                       for value in self.feature_population_stds)):
            raise ValueError("invalid HMM feature standardization")
        if (len(self.pi) != HMM_STATE_COUNT
                or len(self.transition_matrix) != HMM_STATE_COUNT
                or len(self.emission_means) != HMM_STATE_COUNT
                or len(self.emission_variances) != HMM_STATE_COUNT):
            raise ValueError("invalid HMM state dimensions")
        for vector in (self.pi, *self.transition_matrix):
            if (len(vector) != HMM_STATE_COUNT
                    or any(not math.isfinite(value) or value <
                           HMM_PROBABILITY_FLOOR / (1 + HMM_STATE_COUNT * HMM_PROBABILITY_FLOOR) * (1 - HMM_NUMERICAL_TOL)
                           for value in vector)
                    or abs(math.fsum(vector) - 1) > HMM_NUMERICAL_TOL):
                raise ValueError("invalid HMM probability distribution")
        for means, variances in zip(self.emission_means, self.emission_variances):
            if (len(means) != HMM_FEATURE_COUNT or len(variances) != HMM_FEATURE_COUNT
                    or any(not math.isfinite(value) for value in means)
                    or any(not math.isfinite(value) or value < HMM_VARIANCE_FLOOR for value in variances)):
                raise ValueError("invalid HMM Gaussian emissions")
        if _state_order(self.emission_means) != tuple(range(HMM_STATE_COUNT)):
            raise ValueError("HMM states are not canonically ordered")
        likelihoods = (self.initial_development_log_likelihood,
                       self.final_development_log_likelihood,
                       self.final_development_log_likelihood_per_row)
        if any(not math.isfinite(value) for value in likelihoods):
            raise ValueError("invalid HMM likelihood")
        for digest in (self.training_data_sha256, self.model_sha256):
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("invalid HMM SHA-256 fingerprint")
        if self.model_sha256 != _model_fingerprint(self):
            raise ValueError("HMM model fingerprint does not match parameters")


@dataclass(frozen=True)
class HMMFilterState:
    last_usable_boundary_time_ms: int
    posterior_probabilities: tuple[float, float, float]

    def __post_init__(self):
        if (type(self.last_usable_boundary_time_ms) is not int
                or self.last_usable_boundary_time_ms < 0
                or self.last_usable_boundary_time_ms % HMM_SAMPLE_INTERVAL_MS
                or len(self.posterior_probabilities) != HMM_STATE_COUNT
                or any(not math.isfinite(value) or value < 0 for value in self.posterior_probabilities)
                or abs(math.fsum(self.posterior_probabilities) - 1) > HMM_NUMERICAL_TOL):
            raise ValueError("invalid causal HMM filter state")


def _validate_baseline(evaluation: MarketMovementEvaluation):
    if not isinstance(evaluation, MarketMovementEvaluation):
        raise ValueError("evaluation must be MarketMovementEvaluation")
    if (evaluation.algorithm_version != BASELINE_MOVEMENT_ALGORITHM_VERSION
            or evaluation.config_version != BASELINE_MOVEMENT_CONFIG_VERSION
            or set(evaluation.windows) != set(WINDOWS)):
        raise ValueError("HMM experiment requires canonical V1 movement")
    for minute in WINDOWS:
        window = evaluation.windows[minute]
        if (window.window_minutes != minute
                or window.algorithm_version != evaluation.algorithm_version
                or window.config_version != evaluation.config_version
                or window.configured_universe != evaluation.configured_universe
                or window.universe_id != evaluation.universe_id
                or window.universe_version != evaluation.universe_version
                or window.provider != evaluation.provider
                or window.exchange != evaluation.exchange
                or window.price_type != evaluation.price_type
                or window.evaluation_boundary_time_ms != evaluation.evaluation_boundary_time_ms):
            raise ValueError("HMM movement window scope disagrees")


def _scope(evaluation):
    return (evaluation.algorithm_version, evaluation.config_version,
            evaluation.universe_id, evaluation.universe_version,
            evaluation.configured_universe, evaluation.provider,
            evaluation.exchange, evaluation.price_type)


def _extract_feature_row(evaluation: MarketMovementEvaluation) -> HMMFeatureRow | None:
    if evaluation.evaluation_boundary_time_ms % HMM_SAMPLE_INTERVAL_MS:
        return None
    window = evaluation.windows[5]
    breadth = window.breadth
    if (not window.market_wide_eligible or not breadth.available
            or not breadth.material_rising.available
            or not breadth.material_falling.available):
        return None
    median_movement = window.aggregates.median_normalized_movement
    dispersion = window.aggregates.dispersion_mad_normalized_movement
    if not median_movement.available or not dispersion.available:
        return None
    rising = _finite(breadth.material_rising.value.fraction) if breadth.material_rising.value is not None else None
    falling = _finite(breadth.material_falling.value.fraction) if breadth.material_falling.value is not None else None
    if rising is None or falling is None:
        return None
    included = tuple(item for item in window.symbols if item.included)
    if not included or tuple(item.symbol for item in included) != window.included_symbols:
        return None
    rvol = tuple(_finite(item.rvol.value) if item.rvol.available else None for item in included)
    if any(value is None for value in rvol):
        return None
    values = (
        _finite(median_movement.value),
        _finite(rising - falling),
        _finite(dispersion.value),
        _finite(median(rvol)),
    )
    if any(value is None for value in values) or not -1 - HMM_NUMERICAL_TOL <= values[1] <= 1 + HMM_NUMERICAL_TOL:
        return None
    return HMMFeatureRow(evaluation.evaluation_boundary_time_ms, values)


def _development_blocks(points, feature_rows):
    blocks = []
    active = []
    unavailable = 0
    previous_boundary = None
    for point, row in zip(points, feature_rows):
        if point.partition != "development" or point.movement_evaluation.evaluation_boundary_time_ms % HMM_SAMPLE_INTERVAL_MS:
            continue
        boundary = point.movement_evaluation.evaluation_boundary_time_ms
        if row is None:
            unavailable += 1
            if active:
                blocks.append(tuple(active))
                active = []
        else:
            if active and boundary != previous_boundary + HMM_SAMPLE_INTERVAL_MS:
                blocks.append(tuple(active))
                active = []
            active.append(row)
        previous_boundary = boundary
    if active:
        blocks.append(tuple(active))
    return tuple(blocks), unavailable


def _training_fingerprint(blocks, scope, config):
    parts = [HMM_ALGORITHM_VERSION, config.version, HMM_FEATURE_SCHEMA,
             *(str(value) if not isinstance(value, tuple) else ",".join(value) for value in scope)]
    for block in blocks:
        parts.append(f"block:{block[0].evaluation_boundary_time_ms}")
        for row in block:
            parts.append("row:" + str(row.evaluation_boundary_time_ms) + ":" + ":".join(value.hex() for value in row.values))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _model_fingerprint(model):
    parts = [model.algorithm_version, model.config_version, model.feature_schema_version,
             *model.state_names]
    for vector in (model.feature_means, model.feature_population_stds, model.pi,
                   *model.transition_matrix, *model.emission_means, *model.emission_variances):
        parts.append(":".join(value.hex() for value in vector))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _standardize_blocks(blocks):
    rows = tuple(row for block in blocks for row in block)
    n = len(rows)
    columns = tuple(tuple(row.values[d] for row in rows) for d in range(HMM_FEATURE_COUNT))
    try:
        means = tuple(math.fsum(column) / n for column in columns)
        variances = tuple(math.fsum((value - means[d]) ** 2 for value in column) / n
                          for d, column in enumerate(columns))
        stds = tuple(math.sqrt(value) for value in variances)
    except (OverflowError, ValueError, ZeroDivisionError):
        return None, HMM_TRAINING_NUMERIC_UNAVAILABLE
    if any(not math.isfinite(value) for value in (*means, *variances, *stds)):
        return None, HMM_TRAINING_NUMERIC_UNAVAILABLE
    if any(value <= HMM_NUMERICAL_TOL for value in stds):
        return None, HMM_TRAINING_ZERO_VARIANCE
    standardized = tuple(tuple(tuple((row.values[d] - means[d]) / stds[d]
                                       for d in range(HMM_FEATURE_COUNT)) for row in block)
                         for block in blocks)
    if any(not math.isfinite(value) for block in standardized for row in block for value in row):
        return None, HMM_TRAINING_NUMERIC_UNAVAILABLE
    return (means, stds, standardized), None


def _logsumexp(values):
    values = tuple(values)
    if not values:
        return -math.inf
    greatest = max(values)
    if greatest == -math.inf:
        return -math.inf
    if not math.isfinite(greatest):
        raise ArithmeticError("non-finite log weight")
    return greatest + math.log(math.fsum(math.exp(value - greatest) for value in values))


def _gaussian_log_density(row, means, variances):
    if len(row) != HMM_FEATURE_COUNT or len(means) != HMM_FEATURE_COUNT or len(variances) != HMM_FEATURE_COUNT:
        raise ValueError("Gaussian emission must have four dimensions")
    value = -0.5 * math.fsum(
        _LOG_TWO_PI + math.log(variance) + (x - mean) ** 2 / variance
        for x, mean, variance in zip(row, means, variances))
    if not math.isfinite(value):
        raise ArithmeticError("non-finite Gaussian log density")
    return value


def _state_order(means):
    def compare(a, b):
        primary = means[a][0] - means[b][0]
        if abs(primary) > HMM_NUMERICAL_TOL:
            return -1 if primary < 0 else 1
        if means[a] != means[b]:
            return -1 if means[a] < means[b] else 1
        return a - b
    return tuple(sorted(range(HMM_STATE_COUNT), key=cmp_to_key(compare)))


def _canonicalize(pi, transition, means, variances):
    order = _state_order(means)
    return (tuple(pi[i] for i in order),
            tuple(tuple(transition[i][j] for j in order) for i in order),
            tuple(means[i] for i in order), tuple(variances[i] for i in order))


def _forward_backward(block, pi, transition, means, variances):
    emissions = tuple(tuple(_gaussian_log_density(row, means[s], variances[s])
                            for s in range(HMM_STATE_COUNT)) for row in block)
    log_pi = tuple(math.log(value) for value in pi)
    log_transition = tuple(tuple(math.log(value) for value in row) for row in transition)
    alpha = [tuple(log_pi[s] + emissions[0][s] for s in range(HMM_STATE_COUNT))]
    for time in range(1, len(block)):
        alpha.append(tuple(
            emissions[time][j] + _logsumexp(alpha[time - 1][i] + log_transition[i][j]
                                           for i in range(HMM_STATE_COUNT))
            for j in range(HMM_STATE_COUNT)))
    likelihood = _logsumexp(alpha[-1])
    beta = [None] * len(block)
    beta[-1] = (0.0,) * HMM_STATE_COUNT
    for time in range(len(block) - 2, -1, -1):
        beta[time] = tuple(_logsumexp(
            log_transition[i][j] + emissions[time + 1][j] + beta[time + 1][j]
            for j in range(HMM_STATE_COUNT)) for i in range(HMM_STATE_COUNT))
    gamma = tuple(tuple(math.exp(alpha[time][s] + beta[time][s] - likelihood)
                        for s in range(HMM_STATE_COUNT)) for time in range(len(block)))
    xi = tuple(tuple(tuple(math.exp(
        alpha[time][i] + log_transition[i][j] + emissions[time + 1][j]
        + beta[time + 1][j] - likelihood) for j in range(HMM_STATE_COUNT))
        for i in range(HMM_STATE_COUNT)) for time in range(len(block) - 1))
    if (not math.isfinite(likelihood)
            or any(not math.isfinite(value) for row in gamma for value in row)
            or any(not math.isfinite(value) for matrix in xi for row in matrix for value in row)):
        raise ArithmeticError("non-finite HMM expectations")
    return likelihood, gamma, xi


def _total_likelihood(blocks, pi, transition, means, variances):
    return math.fsum(_forward_backward(block, pi, transition, means, variances)[0]
                     for block in blocks)


def _train_from_blocks(blocks, unavailable_count, scope, config):
    rows = tuple(row for block in blocks for row in block)
    transitions = sum(len(block) - 1 for block in blocks)
    fingerprint = _training_fingerprint(blocks, scope, config)
    first = rows[0].evaluation_boundary_time_ms if rows else None
    last = rows[-1].evaluation_boundary_time_ms if rows else None

    def unavailable(reason):
        return (HMMTrainingDiagnostics(
            HMM_TRAINING_UNAVAILABLE, reason, len(rows), unavailable_count,
            len(blocks), transitions, first, last, fingerprint), None)

    if len(rows) < HMM_MIN_TRAINING_ROWS:
        return unavailable(HMM_INSUFFICIENT_TRAINING_ROWS)
    if transitions < HMM_MIN_TRAINING_TRANSITIONS:
        return unavailable(HMM_INSUFFICIENT_TRAINING_TRANSITIONS)
    inputs, reason = _standardize_blocks(blocks)
    if inputs is None:
        return unavailable(reason)
    feature_means, feature_stds, standardized = inputs
    pi = (1 / 3,) * HMM_STATE_COUNT
    transition = tuple(tuple(0.90 if i == j else 0.05
                             for j in range(HMM_STATE_COUNT)) for i in range(HMM_STATE_COUNT))
    means = ((-1.0, 0.0, 0.0, 0.0),
             (0.0, 0.0, 0.0, 0.0),
             (1.0, 0.0, 0.0, 0.0))
    variances = ((1.0,) * HMM_FEATURE_COUNT,) * HMM_STATE_COUNT
    try:
        initial_likelihood = _total_likelihood(standardized, pi, transition, means, variances)
        for _ in range(HMM_EM_ITERATIONS):
            expectations = tuple(_forward_backward(block, pi, transition, means, variances)
                                 for block in standardized)
            initial_mass = tuple(math.fsum(gamma[0][s] for _, gamma, _ in expectations)
                                 for s in range(HMM_STATE_COUNT))
            state_mass = tuple(math.fsum(gamma[time][s]
                                         for (_, gamma, _), block in zip(expectations, standardized)
                                         for time in range(len(block)))
                               for s in range(HMM_STATE_COUNT))
            transition_mass = tuple(tuple(math.fsum(xi[time][i][j]
                                                       for (_, _, xi), block in zip(expectations, standardized)
                                                       for time in range(len(block) - 1))
                                          for j in range(HMM_STATE_COUNT))
                                    for i in range(HMM_STATE_COUNT))
            if (any(value <= HMM_NUMERICAL_TOL for value in state_mass)
                    or any(math.fsum(row) <= HMM_NUMERICAL_TOL for row in transition_mass)):
                return unavailable(HMM_TRAINING_DEGENERATE)
            pi = _probabilities(initial_mass, floor=HMM_PROBABILITY_FLOOR)
            transition = tuple(_probabilities(row, floor=HMM_PROBABILITY_FLOOR)
                               for row in transition_mass)
            new_means = tuple(tuple(
                math.fsum(gamma[time][s] * block[time][d]
                          for (_, gamma, _), block in zip(expectations, standardized)
                          for time in range(len(block))) / state_mass[s]
                for d in range(HMM_FEATURE_COUNT)) for s in range(HMM_STATE_COUNT))
            new_variances = tuple(tuple(max(
                math.fsum(gamma[time][s] * (block[time][d] - new_means[s][d]) ** 2
                          for (_, gamma, _), block in zip(expectations, standardized)
                          for time in range(len(block))) / state_mass[s],
                HMM_VARIANCE_FLOOR) for d in range(HMM_FEATURE_COUNT))
                for s in range(HMM_STATE_COUNT))
            if any(not math.isfinite(value) for row in (*new_means, *new_variances)
                   for value in row):
                return unavailable(HMM_TRAINING_NUMERIC_UNAVAILABLE)
            means, variances = new_means, new_variances
        pi, transition, means, variances = _canonicalize(pi, transition, means, variances)
        final_likelihood = _total_likelihood(standardized, pi, transition, means, variances)
    except (ArithmeticError, OverflowError, ValueError, ZeroDivisionError):
        return unavailable(HMM_TRAINING_NUMERIC_UNAVAILABLE)
    if not math.isfinite(initial_likelihood) or not math.isfinite(final_likelihood):
        return unavailable(HMM_TRAINING_NUMERIC_UNAVAILABLE)
    fields = dict(
        algorithm_version=HMM_ALGORITHM_VERSION, config_version=config.version,
        feature_schema_version=HMM_FEATURE_SCHEMA, state_names=HMM_STATE_NAMES,
        training_partition="development", training_usable_row_count=len(rows),
        training_unavailable_row_count=unavailable_count, training_block_count=len(blocks),
        training_transition_count=transitions,
        training_first_usable_boundary_time_ms=first,
        training_last_usable_boundary_time_ms=last,
        feature_means=feature_means, feature_population_stds=feature_stds,
        pi=pi, transition_matrix=transition, emission_means=means,
        emission_variances=variances, em_iteration_count=HMM_EM_ITERATIONS,
        initial_development_log_likelihood=initial_likelihood,
        final_development_log_likelihood=final_likelihood,
        final_development_log_likelihood_per_row=final_likelihood / len(rows),
        training_data_sha256=fingerprint,
    )
    fields["model_sha256"] = _model_fingerprint(SimpleNamespace(**fields))
    try:
        model = GaussianHMMModelArtifact(**fields)
    except ValueError:
        return unavailable(HMM_TRAINING_NUMERIC_UNAVAILABLE)
    return (HMMTrainingDiagnostics(
        "HMM_TRAINING_READY", None, len(rows), unavailable_count, len(blocks),
        transitions, first, last, fingerprint), model)


def train_hmm_regime_model(
    points: Iterable[MarketStateExperimentPoint],
    config: HMMRegimeConfig = HMM_CONFIG_V1,
) -> tuple[HMMTrainingDiagnostics, GaussianHMMModelArtifact | None]:
    """Fit once using development rows only; validation/test never enter the model."""
    if not isinstance(config, HMMRegimeConfig):
        raise ValueError("config must be HMMRegimeConfig")
    points = tuple(points)
    validate_experiment_points(points)
    scope = None
    feature_rows = []
    for point in points:
        evaluation = point.movement_evaluation
        _validate_baseline(evaluation)
        if scope is None:
            scope = _scope(evaluation)
        elif _scope(evaluation) != scope:
            raise ValueError("HMM experiment requires one fixed movement scope")
        feature_rows.append(_extract_feature_row(evaluation))
    blocks, unavailable_count = _development_blocks(points, feature_rows)
    return _train_from_blocks(blocks, unavailable_count, scope or (), config)


def extract_hmm_development_training_block(
    period, points: Iterable[MarketStateExperimentPoint],
) -> HMMDevelopmentTrainingBlock:
    """Extract one frozen development day's features without joining days."""
    if (getattr(period, "phase", None) != "development"
            or type(getattr(period, "study_period_index", None)) is not int
            or getattr(period, "utc_date", None) is None
            or type(getattr(period, "start_boundary_time_ms", None)) is not int
            or type(getattr(period, "end_boundary_time_ms", None)) is not int):
        raise ValueError("HMM development extraction requires a frozen development period")
    points = tuple(points)
    if (len(points) != (period.end_boundary_time_ms - period.start_boundary_time_ms) // 5_000 + 1
            or not points):
        raise ValueError("HMM development period must contain its full canonical 5-second stream")
    scope = None
    rows = []
    blocks = []
    unavailable_count = 0
    previous_boundary = None
    for point in points:
        if not isinstance(point, MarketStateExperimentPoint):
            raise ValueError("HMM development stream contains an invalid point")
        boundary = point.movement_evaluation.evaluation_boundary_time_ms
        if (point.partition != "development"
                or (previous_boundary is None and boundary != period.start_boundary_time_ms)
                or (previous_boundary is not None and boundary != previous_boundary + 5_000)):
            raise ValueError("HMM development stream must be one full uniform-phase day")
        previous_boundary = boundary
        _validate_baseline(point.movement_evaluation)
        current_scope = _scope(point.movement_evaluation)
        if scope is None:
            scope = current_scope
        elif current_scope != scope:
            raise ValueError("HMM development periods must use identical movement scope")
        if boundary >= period.end_boundary_time_ms or boundary % HMM_SAMPLE_INTERVAL_MS:
            continue
        feature = _extract_feature_row(point.movement_evaluation)
        if feature is None:
            unavailable_count += 1
            if rows:
                blocks.append(tuple(rows))
                rows = []
        else:
            if rows and boundary != rows[-1].evaluation_boundary_time_ms + HMM_SAMPLE_INTERVAL_MS:
                blocks.append(tuple(rows))
                rows = []
            rows.append(feature)
    if previous_boundary != period.end_boundary_time_ms:
        raise ValueError("HMM development stream does not reach the frozen period end")
    if rows:
        blocks.append(tuple(rows))
    return HMMDevelopmentTrainingBlock(
        period.study_period_index, period.utc_date.isoformat(),
        period.start_boundary_time_ms, period.end_boundary_time_ms,
        scope or (), tuple(blocks), unavailable_count)


def train_hmm_regime_model_from_feature_blocks(
    feature_blocks: Iterable[Iterable[HMMFeatureRow]],
    movement_scope: tuple,
    unavailable_row_count: int = 0,
    config: HMMRegimeConfig = HMM_CONFIG_V1,
) -> tuple[HMMTrainingDiagnostics, GaussianHMMModelArtifact | None]:
    """Fit the existing Gaussian HMM math over independent chronological blocks."""
    blocks = tuple(tuple(block) for block in feature_blocks)
    if (not isinstance(config, HMMRegimeConfig)
            or type(unavailable_row_count) is not int or unavailable_row_count < 0
            or not movement_scope or any(not block for block in blocks)
            or any(not isinstance(row, HMMFeatureRow) for block in blocks for row in block)):
        raise ValueError("invalid multi-block HMM training request")
    previous = None
    for block in blocks:
        if any(right.evaluation_boundary_time_ms != left.evaluation_boundary_time_ms
               + HMM_SAMPLE_INTERVAL_MS for left, right in zip(block, block[1:])):
            raise ValueError("HMM training blocks must be contiguous internally")
        if previous is not None and block[0].evaluation_boundary_time_ms <= previous:
            raise ValueError("HMM training blocks must be in chronological order")
        previous = block[-1].evaluation_boundary_time_ms
    return _train_from_blocks(blocks, unavailable_row_count,
                              tuple(movement_scope), config)


def train_hmm_regime_model_from_blocks(
    development_blocks: Iterable[HMMDevelopmentTrainingBlock],
    config: HMMRegimeConfig = HMM_CONFIG_V1,
) -> tuple[HMMTrainingDiagnostics, GaussianHMMModelArtifact | None]:
    """Train on separate frozen development days; never bridge calendar gaps."""
    blocks = tuple(development_blocks)
    if (not blocks or any(not isinstance(item, HMMDevelopmentTrainingBlock)
                          for item in blocks)
            or tuple(item.study_period_index for item in blocks)
            != tuple(sorted(item.study_period_index for item in blocks))
            or len({item.study_period_index for item in blocks}) != len(blocks)
            or tuple(item.utc_date for item in blocks)
            != tuple(sorted(item.utc_date for item in blocks))
            or len({item.utc_date for item in blocks}) != len(blocks)):
        raise ValueError("HMM input must be unique chronological development-period blocks")
    scope = blocks[0].movement_scope
    if any(item.movement_scope != scope for item in blocks):
        raise ValueError("HMM development periods must share one movement/universe scope")
    feature_blocks = tuple(feature_block for period in blocks
                           for feature_block in period.feature_blocks)
    return train_hmm_regime_model_from_feature_blocks(
        feature_blocks, scope,
        sum(item.unavailable_row_count for item in blocks), config)


@dataclass(frozen=True)
class HMMRegimeEvidence:
    evaluation_boundary_time_ms: int
    status: str
    status_reason: str | None
    raw_feature_vector: tuple[float, float, float, float] | None
    standardized_feature_vector: tuple[float, float, float, float] | None
    filter_reset_before_observation: bool | None
    predicted_state_probabilities: tuple[float, float, float] | None
    posterior_probabilities: tuple[float, float, float] | None
    hard_state: str | None
    posterior_confidence: float | None
    posterior_entropy: float | None
    predictive_log_likelihood: float | None
    algorithm_version: str
    config_version: str
    model_sha256: str | None


def _filter_observation(row, model, previous):
    standardized = tuple((row.values[d] - model.feature_means[d]) /
                         model.feature_population_stds[d] for d in range(HMM_FEATURE_COUNT))
    if any(not math.isfinite(value) for value in standardized):
        raise ArithmeticError("non-finite standardized inference feature")
    reset = previous is None or row.evaluation_boundary_time_ms != previous.last_usable_boundary_time_ms + HMM_SAMPLE_INTERVAL_MS
    predicted = (model.pi if reset else tuple(
        math.fsum(previous.posterior_probabilities[i] * model.transition_matrix[i][j]
                  for i in range(HMM_STATE_COUNT)) for j in range(HMM_STATE_COUNT)))
    log_weights = tuple(math.log(predicted[s]) + _gaussian_log_density(
        standardized, model.emission_means[s], model.emission_variances[s])
        for s in range(HMM_STATE_COUNT))
    predictive_ll = _logsumexp(log_weights)
    posterior = _probabilities(tuple(math.exp(value - predictive_ll) for value in log_weights))
    hard = HMM_STATE_NAMES[max(range(HMM_STATE_COUNT), key=lambda s: posterior[s])]
    confidence = max(posterior)
    entropy = -math.fsum(value * math.log(value) for value in posterior if value > 0)
    return (standardized, reset, predicted, posterior, hard, confidence,
            entropy, predictive_ll)


def advance_hmm_regime_filter(
    evaluation: MarketMovementEvaluation,
    partition: ExperimentPartition,
    model: GaussianHMMModelArtifact | None,
    state: HMMFilterState | None = None,
    *,
    training_reason: str | None = None,
    config: HMMRegimeConfig = HMM_CONFIG_V1,
) -> tuple[HMMRegimeEvidence, HMMFilterState | None]:
    """Forward filter a validation/test minute; never revise prior observations."""
    _validate_baseline(evaluation)
    if not isinstance(config, HMMRegimeConfig) or partition not in ("development", "validation", "test"):
        raise ValueError("invalid HMM config or partition")
    if model is not None and not isinstance(model, GaussianHMMModelArtifact):
        raise ValueError("model must be a validated HMM artifact")
    if state is not None and not isinstance(state, HMMFilterState):
        raise ValueError("state must be HMMFilterState or None")
    boundary = evaluation.evaluation_boundary_time_ms
    if type(boundary) is not int or boundary < 0 or boundary % 5_000:
        raise ValueError("HMM boundary must be five-second aligned")
    scheduled = boundary % HMM_SAMPLE_INTERVAL_MS == 0
    row = _extract_feature_row(evaluation) if scheduled else None
    status = (HMM_NOT_SCHEDULED if not scheduled else
              HMM_TRAINING_PARTITION if partition == "development" else
              HMM_TRAINING_UNAVAILABLE if model is None else
              HMM_FEATURE_UNAVAILABLE if row is None else HMM_READY)
    values = dict(
        evaluation_boundary_time_ms=boundary, status=status,
        status_reason=(training_reason or HMM_TRAINING_UNAVAILABLE)
        if status == HMM_TRAINING_UNAVAILABLE else None,
        raw_feature_vector=row.values if row is not None else None,
        standardized_feature_vector=None, filter_reset_before_observation=None,
        predicted_state_probabilities=None, posterior_probabilities=None,
        hard_state=None, posterior_confidence=None, posterior_entropy=None,
        predictive_log_likelihood=None, algorithm_version=HMM_ALGORITHM_VERSION,
        config_version=config.version, model_sha256=model.model_sha256 if model else None,
    )
    if partition == "development":
        return HMMRegimeEvidence(**values), None
    if not scheduled:
        return HMMRegimeEvidence(**values), state
    if model is None or row is None:
        return HMMRegimeEvidence(**values), None
    try:
        standardized, reset, predicted, posterior, hard, confidence, entropy, likelihood = _filter_observation(row, model, state)
    except (ArithmeticError, OverflowError, ValueError, ZeroDivisionError):
        values.update(status=HMM_FEATURE_UNAVAILABLE, status_reason="HMM_INFERENCE_NUMERIC_UNAVAILABLE")
        return HMMRegimeEvidence(**values), None
    values.update(
        standardized_feature_vector=standardized,
        filter_reset_before_observation=reset,
        predicted_state_probabilities=predicted, posterior_probabilities=posterior,
        hard_state=hard, posterior_confidence=confidence, posterior_entropy=entropy,
        predictive_log_likelihood=likelihood,
    )
    return HMMRegimeEvidence(**values), HMMFilterState(boundary, posterior)


@dataclass(frozen=True)
class PairedMarketStateHMMPoint:
    evaluation_boundary_time_ms: int
    partition: ExperimentPartition
    baseline_evaluation: MarketMovementEvaluation
    baseline_classification: MarketClassificationEvaluation
    baseline_lifecycle_state: MarketEpisodeLifecycleState
    baseline_transitions: tuple[MarketEpisodeTransition, ...]
    hmm_evidence: HMMRegimeEvidence
    hmm_filter_state: HMMFilterState | None


@dataclass(frozen=True)
class HMMStateDiagnostic:
    state: str
    observation_count: int
    occupancy_fraction: float | None
    broad_rise_count: int
    broad_rise_fraction: float | None
    broad_drop_count: int
    broad_drop_fraction: float | None
    neutral_count: int
    neutral_fraction: float | None
    baseline_active_episode_count: int
    baseline_active_episode_fraction: float | None
    median_posterior_confidence: float | None
    median_posterior_entropy: float | None
    median_raw_features: tuple[tuple[str, float | None], ...]


@dataclass(frozen=True)
class HMMRegimeComparisonSummary:
    partition: str
    evaluation_count: int
    scheduled_count: int
    not_scheduled_count: int
    training_usable_row_count: int
    training_unavailable_row_count: int
    training_block_count: int
    training_transition_count: int
    inference_ready_count: int
    inference_feature_unavailable_count: int
    inference_training_unavailable_count: int
    sum_predictive_log_likelihood: float | None
    mean_predictive_log_likelihood_per_ready_row: float | None
    median_posterior_confidence: float | None
    median_posterior_entropy: float | None
    hmm_v1_contingency: tuple[tuple[str, tuple[tuple[str, int], ...]], ...]
    state_diagnostics: tuple[HMMStateDiagnostic, ...]
    hard_state_transition_comparison_count: int
    hard_state_switch_count: int
    hard_state_switch_fraction: float | None
    v1_direction_transition_comparison_count: int
    v1_direction_switch_count: int
    v1_direction_switch_fraction: float | None
    simultaneous_hmm_and_v1_switch_count: int
    validation_test_occupancy_total_variation: float | None
    baseline_transition_counts: tuple[tuple[str, int], ...]
    baseline_episode_count: int
    baseline_short_lived_closed_episode_count: int
    baseline_directional_onset_count: int


@dataclass(frozen=True)
class MarketStateHMMExperimentResult:
    hmm_config: HMMRegimeConfig
    training_diagnostics: HMMTrainingDiagnostics
    model_artifact: GaussianHMMModelArtifact | None
    paired_points: tuple[PairedMarketStateHMMPoint, ...]
    summaries: Mapping[str, HMMRegimeComparisonSummary]

    def __post_init__(self):
        object.__setattr__(self, "paired_points", tuple(self.paired_points))
        object.__setattr__(self, "summaries", MappingProxyType(dict(self.summaries)))


_V1_STATES = ("BROAD_RISE", "BROAD_DROP", "NEUTRAL")


def _median_or_none(values):
    return float(median(values)) if values else None


def _state_diagnostic(state, ready_points):
    points = tuple(point for point in ready_points if point.hmm_evidence.hard_state == state)
    count = len(points)
    total = len(ready_points)
    direction_counts = tuple(sum(point.baseline_classification.windows[5].direction_state == direction
                                 for point in points) for direction in _V1_STATES)
    active = sum(point.baseline_lifecycle_state.active_episode is not None for point in points)
    raw = tuple((name, _median_or_none(tuple(point.hmm_evidence.raw_feature_vector[d]
                                        for point in points)))
                for d, name in enumerate(HMM_FEATURE_NAMES))
    return HMMStateDiagnostic(
        state=state, observation_count=count,
        occupancy_fraction=count / total if total else None,
        broad_rise_count=direction_counts[0],
        broad_rise_fraction=direction_counts[0] / count if count else None,
        broad_drop_count=direction_counts[1],
        broad_drop_fraction=direction_counts[1] / count if count else None,
        neutral_count=direction_counts[2],
        neutral_fraction=direction_counts[2] / count if count else None,
        baseline_active_episode_count=active,
        baseline_active_episode_fraction=active / count if count else None,
        median_posterior_confidence=_median_or_none(tuple(
            point.hmm_evidence.posterior_confidence for point in points)),
        median_posterior_entropy=_median_or_none(tuple(
            point.hmm_evidence.posterior_entropy for point in points)),
        median_raw_features=raw,
    )


def _occupancy_tv(validation, test):
    validation_ready = tuple(point for point in validation if point.hmm_evidence.status == HMM_READY)
    test_ready = tuple(point for point in test if point.hmm_evidence.status == HMM_READY)
    if not validation_ready or not test_ready:
        return None
    return 0.5 * math.fsum(abs(
        sum(point.hmm_evidence.hard_state == state for point in validation_ready) / len(validation_ready)
        - sum(point.hmm_evidence.hard_state == state for point in test_ready) / len(test_ready))
        for state in HMM_STATE_NAMES)


def _summary(points, partition, training):
    selected = selected_points_for_partition(points, partition)
    observed = observed_points_for_partition(points, partition)
    spans = episode_spans(observed, "baseline")
    scheduled = tuple(point for point in selected
                      if point.hmm_evidence.status != HMM_NOT_SCHEDULED)
    ready = tuple(point for point in selected if point.hmm_evidence.status == HMM_READY)
    likelihoods = tuple(point.hmm_evidence.predictive_log_likelihood for point in ready)
    contingency = tuple((state, tuple((direction, sum(
        point.hmm_evidence.hard_state == state
        and point.baseline_classification.windows[5].direction_state == direction
        for point in ready)) for direction in _V1_STATES)) for state in HMM_STATE_NAMES)
    diagnostics = tuple(_state_diagnostic(state, ready) for state in HMM_STATE_NAMES)
    earlier_ready = {point.evaluation_boundary_time_ms: point for point in observed
                     if point.hmm_evidence.status == HMM_READY}
    comparable = tuple((earlier_ready[point.evaluation_boundary_time_ms - HMM_SAMPLE_INTERVAL_MS], point)
                       for point in ready
                       if not point.hmm_evidence.filter_reset_before_observation
                       and point.evaluation_boundary_time_ms - HMM_SAMPLE_INTERVAL_MS in earlier_ready)
    hmm_switches = sum(prior.hmm_evidence.hard_state != current.hmm_evidence.hard_state
                       for prior, current in comparable)
    v1_switches = sum(prior.baseline_classification.windows[5].direction_state !=
                      current.baseline_classification.windows[5].direction_state
                      for prior, current in comparable)
    simultaneous = sum(
        prior.hmm_evidence.hard_state != current.hmm_evidence.hard_state
        and prior.baseline_classification.windows[5].direction_state !=
        current.baseline_classification.windows[5].direction_state
        for prior, current in comparable)
    training_context = partition in ("all", "development")
    occupancy_tv = (_occupancy_tv(
        tuple(point for point in selected if point.partition == "validation"),
        tuple(point for point in selected if point.partition == "test"))
        if partition == "all" else None)
    return HMMRegimeComparisonSummary(
        partition=partition, evaluation_count=len(selected), scheduled_count=len(scheduled),
        not_scheduled_count=len(selected) - len(scheduled),
        training_usable_row_count=training.usable_row_count if training_context else 0,
        training_unavailable_row_count=training.unavailable_row_count if training_context else 0,
        training_block_count=training.block_count if training_context else 0,
        training_transition_count=training.transition_count if training_context else 0,
        inference_ready_count=len(ready),
        inference_feature_unavailable_count=sum(point.hmm_evidence.status == HMM_FEATURE_UNAVAILABLE
                                                for point in scheduled),
        inference_training_unavailable_count=sum(point.hmm_evidence.status == HMM_TRAINING_UNAVAILABLE
                                                 for point in scheduled),
        sum_predictive_log_likelihood=math.fsum(likelihoods) if likelihoods else None,
        mean_predictive_log_likelihood_per_ready_row=(math.fsum(likelihoods) / len(likelihoods)
                                                     if likelihoods else None),
        median_posterior_confidence=_median_or_none(tuple(
            point.hmm_evidence.posterior_confidence for point in ready)),
        median_posterior_entropy=_median_or_none(tuple(
            point.hmm_evidence.posterior_entropy for point in ready)),
        hmm_v1_contingency=contingency, state_diagnostics=diagnostics,
        hard_state_transition_comparison_count=len(comparable),
        hard_state_switch_count=hmm_switches,
        hard_state_switch_fraction=hmm_switches / len(comparable) if comparable else None,
        v1_direction_transition_comparison_count=len(comparable),
        v1_direction_switch_count=v1_switches,
        v1_direction_switch_fraction=v1_switches / len(comparable) if comparable else None,
        simultaneous_hmm_and_v1_switch_count=simultaneous,
        validation_test_occupancy_total_variation=occupancy_tv,
        baseline_transition_counts=transition_counts(selected, "baseline"),
        baseline_episode_count=episode_count(spans, selected, partition),
        baseline_short_lived_closed_episode_count=short_lived_episode_count(spans, selected, partition),
        baseline_directional_onset_count=directional_onset_count(selected, "baseline"),
    )


def run_market_state_hmm_experiment(
    points: Iterable[MarketStateExperimentPoint],
    config: HMMRegimeConfig = HMM_CONFIG_V1,
    *,
    classifier_config: MarketClassifierConfig | None = None,
    lifecycle_config: MarketEpisodeLifecycleConfig | None = None,
) -> MarketStateHMMExperimentResult:
    """Fit development once; forward filter validation/test alongside unchanged V1."""
    if not isinstance(config, HMMRegimeConfig):
        raise ValueError("config must be HMMRegimeConfig")
    classifier_config = MarketClassifierConfig() if classifier_config is None else classifier_config
    lifecycle_config = MarketEpisodeLifecycleConfig() if lifecycle_config is None else lifecycle_config
    if not isinstance(classifier_config, MarketClassifierConfig):
        raise ValueError("classifier_config must be MarketClassifierConfig")
    if not isinstance(lifecycle_config, MarketEpisodeLifecycleConfig):
        raise ValueError("lifecycle_config must be MarketEpisodeLifecycleConfig")
    points = tuple(points)
    training, model = train_hmm_regime_model(points, config)
    baseline_state = None
    filter_state = None
    paired = []
    for point in points:
        evaluation = point.movement_evaluation
        classification, lifecycle = advance_canonical_branch(
            evaluation, point.source_time_evidence, baseline_state,
            classifier_config, lifecycle_config)
        evidence, filter_state = advance_hmm_regime_filter(
            evaluation, point.partition, model, filter_state,
            training_reason=training.reason, config=config)
        paired.append(PairedMarketStateHMMPoint(
            evaluation.evaluation_boundary_time_ms, point.partition, evaluation,
            classification, lifecycle.next_state, lifecycle.transitions,
            evidence, filter_state))
        baseline_state = lifecycle.next_state
    paired = tuple(paired)
    summaries = {part: _summary(paired, part, training)
                 for part in ("all", "development", "validation", "test")}
    return MarketStateHMMExperimentResult(config, training, model, paired, summaries)
