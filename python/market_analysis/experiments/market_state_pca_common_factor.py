"""EXP-75-07: causal diagnostic-only correlation PCA of aligned 1m returns."""

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
    MarketMovementEvaluation,
    WINDOWS,
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
    observed_points_for_partition,
    selected_points_for_partition,
    short_lived_episode_count,
    transition_counts,
    validate_experiment_points,
)


PCA_ALGORITHM_VERSION = "market-state-pca-common-factor-v1"
PCA_CONFIG_VERSION_PREFIX = "market-state-pca-common-factor-config-v1"
PCA_LOOKBACK_ROWS = (60, 120, 240)
PCA_SAMPLE_INTERVAL_MS = 60_000
PCA_EVALUATION_INTERVAL_MS = EXPERIMENT_EVALUATION_INTERVAL_MS
PCA_NUMERICAL_TOL = 1e-12
PCA_MAX_JACOBI_ROTATIONS_FACTOR = 100
PCA_NOT_SCHEDULED = "PCA_NOT_SCHEDULED"
PCA_WARMING = "PCA_WARMING"
PCA_READY = "PCA_READY"
PCA_MODEL_UNAVAILABLE = "PCA_MODEL_UNAVAILABLE"
PCA_ZERO_VARIANCE = "PCA_ZERO_VARIANCE"
PCA_NUMERIC_UNAVAILABLE = "PCA_NUMERIC_UNAVAILABLE"
PCA_EIGEN_DECOMPOSITION_UNAVAILABLE = "PCA_EIGEN_DECOMPOSITION_UNAVAILABLE"
PCA_INCOMPLETE_CURRENT_ROW = "PCA_INCOMPLETE_CURRENT_ROW"
PCA_UNIDENTIFIED_LOADINGS = "PCA_UNIDENTIFIED_LOADINGS"
PCA_ZERO_CURRENT_ENERGY = "PCA_ZERO_CURRENT_ENERGY"


def _config_version(lookback_rows: int) -> str:
    return (f"{PCA_CONFIG_VERSION_PREFIX}:input-aligned-canonical-1m-returns"
            f":matrix-correlation:standardization-rolling-prior-mean-population-std"
            f":strict-prior-true:sample-60000ms:rows-{lookback_rows}"
            f":eigensolver-deterministic-jacobi")


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class PCACommonFactorConfig:
    version: str
    lookback_rows: int
    sample_interval_ms: int = PCA_SAMPLE_INTERVAL_MS
    input_series: str = "aligned-canonical-1m-returns"
    matrix: str = "correlation"
    standardization: str = "rolling-prior-mean-population-std"
    strict_prior: bool = True
    eigensolver: str = "deterministic-jacobi"

    def __post_init__(self):
        if (type(self.lookback_rows) is not int
                or self.lookback_rows not in PCA_LOOKBACK_ROWS
                or type(self.sample_interval_ms) is not int
                or self.sample_interval_ms != PCA_SAMPLE_INTERVAL_MS
                or self.input_series != "aligned-canonical-1m-returns"
                or self.matrix != "correlation"
                or self.standardization != "rolling-prior-mean-population-std"
                or self.strict_prior is not True
                or self.eigensolver != "deterministic-jacobi"
                or self.version != _config_version(self.lookback_rows)):
            raise ValueError("PCA config must identify a preregistered correlation model")


PCA_CONFIG_60M = PCACommonFactorConfig(_config_version(60), 60)
PCA_CONFIG_120M = PCACommonFactorConfig(_config_version(120), 120)
PCA_CONFIG_240M = PCACommonFactorConfig(_config_version(240), 240)
PCA_CONFIGURATIONS = (PCA_CONFIG_60M, PCA_CONFIG_120M, PCA_CONFIG_240M)


@dataclass(frozen=True)
class PCAAlignedReturnRow:
    evaluation_boundary_time_ms: int
    returns_by_symbol: tuple[tuple[str, float], ...]

    def __post_init__(self):
        boundary = self.evaluation_boundary_time_ms
        if (type(boundary) is not int or boundary < 0
                or boundary % PCA_SAMPLE_INTERVAL_MS):
            raise ValueError("PCA row boundary must be minute aligned")
        returns = tuple(self.returns_by_symbol)
        if (not returns or any(not isinstance(item, tuple) or len(item) != 2
                               or not isinstance(item[0], str) or not item[0]
                               or _finite(item[1]) is None for item in returns)
                or len({symbol for symbol, _ in returns}) != len(returns)):
            raise ValueError("PCA row must contain unique finite named returns")
        object.__setattr__(self, "returns_by_symbol", returns)


@dataclass(frozen=True)
class PCACommonFactorState:
    algorithm_version: str
    config_version: str
    lookback_rows: int
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
    rows: tuple[PCAAlignedReturnRow, ...]

    def __post_init__(self):
        if (self.algorithm_version != PCA_ALGORITHM_VERSION
                or type(self.lookback_rows) is not int
                or self.lookback_rows not in PCA_LOOKBACK_ROWS
                or self.config_version != _config_version(self.lookback_rows)
                or type(self.sample_interval_ms) is not int
                or self.sample_interval_ms != PCA_SAMPLE_INTERVAL_MS):
            raise ValueError("PCA state model identity is invalid")
        for name in (
            "baseline_movement_algorithm_version", "baseline_movement_config_version",
            "universe_id", "universe_version", "provider", "exchange", "price_type",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} is required")
        universe = tuple(self.configured_universe)
        if (not universe or any(not isinstance(symbol, str) or not symbol
                                for symbol in universe)
                or len(set(universe)) != len(universe)):
            raise ValueError("PCA configured universe must be ordered and unique")
        boundary = self.last_evaluation_boundary_time_ms
        if (type(boundary) is not int or boundary < 0
                or boundary % PCA_EVALUATION_INTERVAL_MS):
            raise ValueError("PCA state boundary must be five-second aligned")
        rows = tuple(self.rows)
        if len(rows) > self.lookback_rows:
            raise ValueError("PCA history exceeds configured lookback")
        for index, row in enumerate(rows):
            if (not isinstance(row, PCAAlignedReturnRow)
                    or tuple(symbol for symbol, _ in row.returns_by_symbol) != universe):
                raise ValueError("PCA row symbols must match configured universe order")
            if (index and row.evaluation_boundary_time_ms !=
                    rows[index - 1].evaluation_boundary_time_ms + PCA_SAMPLE_INTERVAL_MS):
                raise ValueError("PCA rows must be consecutive completed minutes")
        if rows:
            age = boundary - rows[-1].evaluation_boundary_time_ms
            if age < 0 or age >= PCA_SAMPLE_INTERVAL_MS:
                raise ValueError("PCA last row must be the latest aligned minute")
        object.__setattr__(self, "configured_universe", universe)
        object.__setattr__(self, "rows", rows)


def _validate_baseline(evaluation: MarketMovementEvaluation) -> None:
    if not isinstance(evaluation, MarketMovementEvaluation):
        raise ValueError("evaluation must be MarketMovementEvaluation")
    if (evaluation.algorithm_version != BASELINE_MOVEMENT_ALGORITHM_VERSION
            or evaluation.config_version != BASELINE_MOVEMENT_CONFIG_VERSION
            or set(evaluation.windows) != set(WINDOWS)):
        raise ValueError("PCA experiment requires canonical V1 movement evaluation")
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
            raise ValueError("PCA movement window scope disagrees with evaluation")


def _same_scope(state, evaluation, config) -> bool:
    return state is not None and (
        state.algorithm_version == PCA_ALGORITHM_VERSION
        and state.config_version == config.version
        and state.lookback_rows == config.lookback_rows
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
        state.last_evaluation_boundary_time_ms + PCA_EVALUATION_INTERVAL_MS
    )


def _current_row(evaluation: MarketMovementEvaluation) -> PCAAlignedReturnRow | None:
    by_symbol = {item.symbol: item for item in evaluation.windows[1].symbols}
    if (len(by_symbol) != len(evaluation.windows[1].symbols)
            or set(by_symbol) != set(evaluation.configured_universe)):
        return None
    values = []
    for symbol in evaluation.configured_universe:
        item = by_symbol.get(symbol)
        value = (_finite(item.current_return.value)
                 if item is not None and item.included and item.current_return.available
                 else None)
        if value is None:
            return None
        values.append((symbol, value))
    return PCAAlignedReturnRow(evaluation.evaluation_boundary_time_ms, tuple(values))


def _correlation_model_inputs(rows: tuple[PCAAlignedReturnRow, ...]):
    """Return prior population means/stds and symmetric correlation matrix."""
    n = len(rows)
    p = len(rows[0].returns_by_symbol)
    columns = tuple(tuple(row.returns_by_symbol[j][1] for row in rows)
                    for j in range(p))
    try:
        means = tuple(math.fsum(column) / n for column in columns)
        variances = tuple(math.fsum((value - means[j]) ** 2 for value in column) / n
                          for j, column in enumerate(columns))
        stds = tuple(math.sqrt(value) for value in variances)
    except (OverflowError, ValueError):
        return None, PCA_NUMERIC_UNAVAILABLE
    if any(not math.isfinite(value) for value in (*means, *variances, *stds)):
        return None, PCA_NUMERIC_UNAVAILABLE
    if any(value <= 0 for value in stds):
        return None, PCA_ZERO_VARIANCE
    standardized = tuple(tuple((value - means[j]) / stds[j] for value in column)
                         for j, column in enumerate(columns))
    if any(not math.isfinite(value) for column in standardized for value in column):
        return None, PCA_NUMERIC_UNAVAILABLE
    matrix = [[0.0] * p for _ in range(p)]
    for j in range(p):
        matrix[j][j] = 1.0
        for k in range(j + 1, p):
            try:
                corr = math.fsum(a * b for a, b in zip(standardized[j], standardized[k])) / n
            except (OverflowError, ValueError):
                return None, PCA_NUMERIC_UNAVAILABLE
            if not math.isfinite(corr) or abs(corr) > 1 + PCA_NUMERICAL_TOL:
                return None, PCA_NUMERIC_UNAVAILABLE
            if abs(corr) > 1:
                corr = math.copysign(1.0, corr)
            matrix[j][k] = matrix[k][j] = corr
    return (means, stds, tuple(tuple(row) for row in matrix)), None


def _jacobi_eigenpairs(matrix: tuple[tuple[float, ...], ...]):
    """Deterministic lexicographic-pivot symmetric Jacobi eigendecomposition."""
    p = len(matrix)
    if (p < 2 or any(len(row) != p for row in matrix)
            or any(not math.isfinite(value) for row in matrix for value in row)
            or any(abs(matrix[j][k] - matrix[k][j]) > PCA_NUMERICAL_TOL
                   for j in range(p) for k in range(j + 1, p))):
        return None
    a = [list(row) for row in matrix]
    vectors = [[float(j == k) for k in range(p)] for j in range(p)]
    converged = False
    for _ in range(PCA_MAX_JACOBI_ROTATIONS_FACTOR * p * p):
        pivot = None
        largest = 0.0
        for j in range(p):
            for k in range(j + 1, p):
                magnitude = abs(a[j][k])
                if magnitude > largest + PCA_NUMERICAL_TOL:
                    largest = magnitude
                    pivot = (j, k)
        if largest <= PCA_NUMERICAL_TOL:
            converged = True
            break
        j, k = pivot
        cross = a[j][k]
        tau = (a[k][k] - a[j][j]) / (2 * cross)
        root = math.hypot(1.0, tau)
        t = (1.0 / (tau + root) if tau >= 0
             else -1.0 / (-tau + root))
        c = 1.0 / math.sqrt(1.0 + t * t)
        s = t * c
        old_jj, old_kk = a[j][j], a[k][k]
        for index in range(p):
            if index != j and index != k:
                old_ij, old_ik = a[index][j], a[index][k]
                a[index][j] = a[j][index] = c * old_ij - s * old_ik
                a[index][k] = a[k][index] = s * old_ij + c * old_ik
            old_vj, old_vk = vectors[index][j], vectors[index][k]
            vectors[index][j] = c * old_vj - s * old_vk
            vectors[index][k] = s * old_vj + c * old_vk
        a[j][j] = old_jj - t * cross
        a[k][k] = old_kk + t * cross
        a[j][k] = a[k][j] = 0.0
    if not converged:
        return None
    trace = math.fsum(matrix[index][index] for index in range(p))
    eigenvalues = [a[index][index] for index in range(p)]
    if any(not math.isfinite(value) or value < -PCA_NUMERICAL_TOL
           for value in eigenvalues):
        return None
    eigenvalues = [max(0.0, value) for value in eigenvalues]
    if (abs(math.fsum(eigenvalues) - trace) >
            PCA_NUMERICAL_TOL * 100 * max(1.0, abs(trace))):
        return None
    order = sorted(range(p), key=lambda index: (-eigenvalues[index], index))
    return (tuple(eigenvalues[index] for index in order),
            tuple(tuple(vectors[row][index] for row in range(p)) for index in order))


def _orient_loading(loading: tuple[float, ...]) -> tuple[float, ...]:
    largest = max(abs(value) for value in loading)
    anchor = next(index for index, value in enumerate(loading)
                  if abs(value) >= largest - PCA_NUMERICAL_TOL)
    sign = 1.0 if loading[anchor] >= 0 else -1.0
    return tuple(sign * value for value in loading)


def _loading_similarity(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    value = abs(math.fsum(a * b for a, b in zip(left, right)))
    if value > 1 + PCA_NUMERICAL_TOL:
        raise ValueError("PCA unit loadings have invalid similarity")
    return min(1.0, value)


@dataclass(frozen=True)
class PCACommonFactorModel:
    prior_means: tuple[float, ...]
    prior_population_stds: tuple[float, ...]
    correlation_matrix: tuple[tuple[float, ...], ...]
    eigenvalues: tuple[float, ...]
    leading_eigenvalue: float
    second_eigenvalue: float
    eigen_gap: float
    explained_variance_ratio: float
    loadings_identified: bool
    pc1_loadings: tuple[tuple[str, float], ...] | None
    loading_coherence_fraction: float | None


def _fit_model(rows: tuple[PCAAlignedReturnRow, ...], universe: tuple[str, ...]):
    if len(universe) < 2:
        return None, PCA_NUMERIC_UNAVAILABLE
    inputs, reason = _correlation_model_inputs(rows)
    if inputs is None:
        return None, reason
    means, stds, matrix = inputs
    eigenpairs = _jacobi_eigenpairs(matrix)
    if eigenpairs is None:
        return None, PCA_EIGEN_DECOMPOSITION_UNAVAILABLE
    eigenvalues, vectors = eigenpairs
    total = math.fsum(eigenvalues)
    if total <= 0 or not math.isfinite(total):
        return None, PCA_NUMERIC_UNAVAILABLE
    gap = eigenvalues[0] - eigenvalues[1]
    evr = eigenvalues[0] / total
    if not math.isfinite(evr) or evr < 1 / len(universe) - PCA_NUMERICAL_TOL or evr > 1 + PCA_NUMERICAL_TOL:
        return None, PCA_NUMERIC_UNAVAILABLE
    identified = gap > PCA_NUMERICAL_TOL
    loadings = None
    coherence = None
    if identified:
        norm = math.sqrt(math.fsum(value * value for value in vectors[0]))
        if norm <= 0 or not math.isfinite(norm):
            return None, PCA_EIGEN_DECOMPOSITION_UNAVAILABLE
        oriented = _orient_loading(tuple(value / norm for value in vectors[0]))
        positive = sum(value > PCA_NUMERICAL_TOL for value in oriented)
        negative = sum(value < -PCA_NUMERICAL_TOL for value in oriented)
        coherence = max(positive, negative) / len(universe)
        loadings = tuple(zip(universe, oriented))
    return PCACommonFactorModel(
        means, stds, matrix, eigenvalues, eigenvalues[0], eigenvalues[1],
        gap, evr, identified, loadings, coherence,
    ), None


@dataclass(frozen=True)
class PCACommonFactorEvidence:
    evaluation_boundary_time_ms: int
    status: str
    status_reason: str | None
    prior_row_count: int
    universe_size: int
    model_available: bool
    model: PCACommonFactorModel | None
    leading_eigenvalue: float | None
    second_eigenvalue: float | None
    eigen_gap: float | None
    explained_variance_ratio: float | None
    loadings_identified: bool
    pc1_loadings: tuple[tuple[str, float], ...] | None
    loading_coherence_fraction: float | None
    loading_similarity: float | None
    current_projection_available: bool
    projection_reason: str | None
    factor_score: float | None
    current_pc1_energy_fraction: float | None
    current_row_complete: bool | None
    current_row_appended_after_evaluation: bool
    history_reset_after_evaluation: bool
    scope_reset_before_evaluation: bool
    algorithm_version: str
    config_version: str


def advance_pca_common_factor(
    evaluation: MarketMovementEvaluation,
    config: PCACommonFactorConfig,
    state: PCACommonFactorState | None = None,
    previous_scheduled_evidence: PCACommonFactorEvidence | None = None,
) -> tuple[PCACommonFactorEvidence, PCACommonFactorState]:
    """Fit from prior rows, project current aligned row, then update history."""
    _validate_baseline(evaluation)
    if not isinstance(config, PCACommonFactorConfig):
        raise ValueError("config must be PCACommonFactorConfig")
    if state is not None and not isinstance(state, PCACommonFactorState):
        raise ValueError("state must be PCACommonFactorState or None")
    if (previous_scheduled_evidence is not None
            and not isinstance(previous_scheduled_evidence, PCACommonFactorEvidence)):
        raise ValueError("previous scheduled evidence type is invalid")
    boundary = evaluation.evaluation_boundary_time_ms
    if (type(boundary) is not int or boundary < 0
            or boundary % PCA_EVALUATION_INTERVAL_MS):
        raise ValueError("PCA boundary must be five-second aligned")
    compatible = _same_scope(state, evaluation, config)
    prior_rows = state.rows if compatible else ()
    scheduled = boundary % PCA_SAMPLE_INTERVAL_MS == 0
    status = PCA_NOT_SCHEDULED
    reason = None
    model = None
    current = None
    projection_available = False
    projection_reason = None
    score = energy_fraction = similarity = None
    if scheduled:
        current = _current_row(evaluation)
        if len(prior_rows) < config.lookback_rows:
            status = PCA_WARMING
        else:
            model, reason = _fit_model(prior_rows, evaluation.configured_universe)
            status = PCA_READY if model is not None else PCA_MODEL_UNAVAILABLE
        if model is not None:
            if (model.loadings_identified and compatible
                    and previous_scheduled_evidence is not None
                    and previous_scheduled_evidence.status == PCA_READY
                    and previous_scheduled_evidence.loadings_identified
                    and previous_scheduled_evidence.pc1_loadings is not None
                    and tuple(symbol for symbol, _ in
                              previous_scheduled_evidence.pc1_loadings)
                    == evaluation.configured_universe
                    and not previous_scheduled_evidence.history_reset_after_evaluation
                    and previous_scheduled_evidence.config_version == config.version
                    and previous_scheduled_evidence.evaluation_boundary_time_ms ==
                    boundary - PCA_SAMPLE_INTERVAL_MS):
                similarity = _loading_similarity(
                    tuple(value for _, value in previous_scheduled_evidence.pc1_loadings),
                    tuple(value for _, value in model.pc1_loadings))
            if current is None:
                projection_reason = PCA_INCOMPLETE_CURRENT_ROW
            elif not model.loadings_identified:
                projection_reason = PCA_UNIDENTIFIED_LOADINGS
            else:
                z = tuple((value - mean) / std
                          for (_, value), mean, std in zip(
                              current.returns_by_symbol,
                              model.prior_means, model.prior_population_stds))
                if any(not math.isfinite(value) for value in z):
                    projection_reason = PCA_NUMERIC_UNAVAILABLE
                else:
                    score = math.fsum(loading * value
                                      for (_, loading), value in zip(model.pc1_loadings, z))
                    total_energy = math.fsum(value * value for value in z)
                    if not math.isfinite(score) or not math.isfinite(total_energy):
                        score = None
                        projection_reason = PCA_NUMERIC_UNAVAILABLE
                    else:
                        projection_available = True
                        if total_energy <= PCA_NUMERICAL_TOL:
                            projection_reason = PCA_ZERO_CURRENT_ENERGY
                        else:
                            energy_fraction = score * score / total_energy
                            if (not math.isfinite(energy_fraction)
                                    or energy_fraction > 1 + PCA_NUMERICAL_TOL):
                                score = None
                                projection_available = False
                                projection_reason = PCA_NUMERIC_UNAVAILABLE
                                energy_fraction = None
                            else:
                                energy_fraction = min(1.0, energy_fraction)
    appended = scheduled and current is not None
    cleared = scheduled and current is None
    if appended:
        next_rows = (*prior_rows, current)[-config.lookback_rows:]
    elif cleared:
        next_rows = ()
    else:
        next_rows = prior_rows
    next_state = PCACommonFactorState(
        PCA_ALGORITHM_VERSION, config.version, config.lookback_rows,
        config.sample_interval_ms, evaluation.algorithm_version,
        evaluation.config_version, evaluation.universe_id, evaluation.universe_version,
        evaluation.configured_universe, evaluation.provider, evaluation.exchange,
        evaluation.price_type, boundary, next_rows,
    )
    evidence = PCACommonFactorEvidence(
        boundary, status, reason, len(prior_rows), len(evaluation.configured_universe),
        model is not None, model,
        model.leading_eigenvalue if model else None,
        model.second_eigenvalue if model else None,
        model.eigen_gap if model else None,
        model.explained_variance_ratio if model else None,
        model.loadings_identified if model else False,
        model.pc1_loadings if model else None,
        model.loading_coherence_fraction if model else None,
        similarity, projection_available, projection_reason, score, energy_fraction,
        current is not None if scheduled else None,
        appended, cleared, state is not None and not compatible,
        PCA_ALGORITHM_VERSION, config.version,
    )
    return evidence, next_state


@dataclass(frozen=True)
class PairedMarketStatePCACommonFactorPoint:
    evaluation_boundary_time_ms: int
    partition: ExperimentPartition
    baseline_evaluation: MarketMovementEvaluation
    baseline_classification: MarketClassificationEvaluation
    baseline_lifecycle_state: MarketEpisodeLifecycleState
    baseline_transitions: tuple[MarketEpisodeTransition, ...]
    pca_evidence: PCACommonFactorEvidence
    pca_state: PCACommonFactorState


@dataclass(frozen=True)
class PCAFactorLoadingDiagnostic:
    symbol: str
    comparable_count: int
    median_absolute_loading: float | None


@dataclass(frozen=True)
class PCAGroupedDiagnostic:
    group: str
    model_ready_count: int
    median_explained_variance_ratio: float | None
    median_current_pc1_energy_fraction: float | None


@dataclass(frozen=True)
class PCACommonFactorComparisonSummary:
    partition: str
    evaluation_count: int
    pca_scheduled_count: int
    pca_not_scheduled_count: int
    pca_warming_count: int
    pca_model_ready_count: int
    pca_model_unavailable_count: int
    pca_projection_available_count: int
    pca_projection_unavailable_count: int
    median_explained_variance_ratio: float | None
    median_leading_eigenvalue: float | None
    median_eigen_gap: float | None
    median_loading_coherence_fraction: float | None
    loading_similarity_comparison_count: int
    median_loading_similarity: float | None
    minimum_loading_similarity: float | None
    median_current_pc1_energy_fraction: float | None
    median_absolute_factor_score: float | None
    breadth_comparison_count: int
    pearson_explained_variance_vs_directional_breadth: float | None
    energy_breadth_comparison_count: int
    pearson_pc1_energy_vs_directional_breadth: float | None
    baseline_state_pca_diagnostics: tuple[PCAGroupedDiagnostic, ...]
    baseline_active_episode_pca_diagnostics: PCAGroupedDiagnostic
    baseline_inactive_pca_diagnostics: PCAGroupedDiagnostic
    factor_loading_diagnostics: tuple[PCAFactorLoadingDiagnostic, ...]
    baseline_transition_counts: tuple[tuple[str, int], ...]
    baseline_episode_count: int
    baseline_short_lived_closed_episode_count: int
    baseline_directional_onset_count: int


@dataclass(frozen=True)
class MarketStatePCACommonFactorExperimentResult:
    pca_config: PCACommonFactorConfig
    paired_points: tuple[PairedMarketStatePCACommonFactorPoint, ...]
    summaries: Mapping[str, PCACommonFactorComparisonSummary]

    def __post_init__(self):
        object.__setattr__(self, "paired_points", tuple(self.paired_points))
        object.__setattr__(self, "summaries", MappingProxyType(dict(self.summaries)))


def _median_or_none(values):
    return float(median(values)) if values else None


def _pearson(pairs: tuple[tuple[float, float], ...]) -> float | None:
    if len(pairs) < 2:
        return None
    x_bar = math.fsum(x for x, _ in pairs) / len(pairs)
    y_bar = math.fsum(y for _, y in pairs) / len(pairs)
    numerator = math.fsum((x - x_bar) * (y - y_bar) for x, y in pairs)
    x2 = math.fsum((x - x_bar) ** 2 for x, _ in pairs)
    y2 = math.fsum((y - y_bar) ** 2 for _, y in pairs)
    denominator = math.sqrt(x2 * y2)
    return numerator / denominator if denominator > 0 and math.isfinite(denominator) else None


def _dominant_breadth(point) -> float | None:
    breadth = point.baseline_classification.windows[5].breadth
    if breadth.available and breadth.rising.available and breadth.falling.available:
        return max(breadth.rising.value.fraction, breadth.falling.value.fraction)
    return None


def _group(group, points):
    ready = tuple(point.pca_evidence for point in points
                  if point.pca_evidence.status == PCA_READY)
    return PCAGroupedDiagnostic(
        group, len(ready),
        _median_or_none(tuple(item.explained_variance_ratio for item in ready)),
        _median_or_none(tuple(item.current_pc1_energy_fraction for item in ready
                              if item.current_pc1_energy_fraction is not None)),
    )


def _loading_diagnostic(symbol, points):
    values = tuple(abs(dict(point.pca_evidence.pc1_loadings)[symbol])
                   for point in points
                   if point.pca_evidence.pc1_loadings is not None
                   and symbol in dict(point.pca_evidence.pc1_loadings))
    return PCAFactorLoadingDiagnostic(symbol, len(values), _median_or_none(values))


def _summary(points: tuple, partition: str) -> PCACommonFactorComparisonSummary:
    selected = selected_points_for_partition(points, partition)
    observed = observed_points_for_partition(points, partition)
    spans = episode_spans(observed, "baseline")
    scheduled = tuple(point for point in selected
                      if point.pca_evidence.status != PCA_NOT_SCHEDULED)
    ready = tuple(point.pca_evidence for point in scheduled
                  if point.pca_evidence.status == PCA_READY)
    projected = tuple(item for item in ready if item.current_projection_available)
    similarities = tuple(point.pca_evidence.loading_similarity for point in scheduled
                         if point.pca_evidence.loading_similarity is not None)
    breadth_pairs = tuple((point.pca_evidence.explained_variance_ratio, breadth)
                          for point in scheduled
                          for breadth in (_dominant_breadth(point),)
                          if point.pca_evidence.status == PCA_READY and breadth is not None)
    energy_pairs = tuple((point.pca_evidence.current_pc1_energy_fraction, breadth)
                         for point in scheduled
                         for breadth in (_dominant_breadth(point),)
                         if point.pca_evidence.current_pc1_energy_fraction is not None
                         and breadth is not None)
    universe_order = tuple(dict.fromkeys(symbol for point in selected
                                        for symbol in point.baseline_evaluation.configured_universe))
    loading_diagnostics = tuple(_loading_diagnostic(symbol, scheduled)
                                for symbol in universe_order)
    state_groups = tuple(_group(state, tuple(point for point in scheduled
                                             if point.baseline_classification.windows[5].direction_state
                                             == state))
                         for state in ("BROAD_RISE", "BROAD_DROP", "NEUTRAL"))
    active = tuple(point for point in scheduled
                   if point.baseline_lifecycle_state.active_episode is not None)
    inactive = tuple(point for point in scheduled
                     if point.baseline_lifecycle_state.active_episode is None)
    return PCACommonFactorComparisonSummary(
        partition=partition, evaluation_count=len(selected),
        pca_scheduled_count=len(scheduled),
        pca_not_scheduled_count=len(selected) - len(scheduled),
        pca_warming_count=sum(point.pca_evidence.status == PCA_WARMING
                              for point in scheduled),
        pca_model_ready_count=len(ready),
        pca_model_unavailable_count=sum(point.pca_evidence.status == PCA_MODEL_UNAVAILABLE
                                        for point in scheduled),
        pca_projection_available_count=len(projected),
        pca_projection_unavailable_count=len(scheduled) - len(projected),
        median_explained_variance_ratio=_median_or_none(tuple(
            item.explained_variance_ratio for item in ready)),
        median_leading_eigenvalue=_median_or_none(tuple(
            item.leading_eigenvalue for item in ready)),
        median_eigen_gap=_median_or_none(tuple(item.eigen_gap for item in ready)),
        median_loading_coherence_fraction=_median_or_none(tuple(
            item.loading_coherence_fraction for item in ready
            if item.loading_coherence_fraction is not None)),
        loading_similarity_comparison_count=len(similarities),
        median_loading_similarity=_median_or_none(similarities),
        minimum_loading_similarity=min(similarities) if similarities else None,
        median_current_pc1_energy_fraction=_median_or_none(tuple(
            item.current_pc1_energy_fraction for item in projected
            if item.current_pc1_energy_fraction is not None)),
        median_absolute_factor_score=_median_or_none(tuple(
            abs(item.factor_score) for item in projected if item.factor_score is not None)),
        breadth_comparison_count=len(breadth_pairs),
        pearson_explained_variance_vs_directional_breadth=_pearson(breadth_pairs),
        energy_breadth_comparison_count=len(energy_pairs),
        pearson_pc1_energy_vs_directional_breadth=_pearson(energy_pairs),
        baseline_state_pca_diagnostics=state_groups,
        baseline_active_episode_pca_diagnostics=_group("active", active),
        baseline_inactive_pca_diagnostics=_group("inactive", inactive),
        factor_loading_diagnostics=loading_diagnostics,
        baseline_transition_counts=transition_counts(selected, "baseline"),
        baseline_episode_count=episode_count(spans, selected, partition),
        baseline_short_lived_closed_episode_count=short_lived_episode_count(
            spans, selected, partition),
        baseline_directional_onset_count=directional_onset_count(selected, "baseline"),
    )


def run_market_state_pca_common_factor_experiment(
    points: Iterable[MarketStateExperimentPoint],
    config: PCACommonFactorConfig,
    *,
    classifier_config: MarketClassifierConfig | None = None,
    lifecycle_config: MarketEpisodeLifecycleConfig | None = None,
    canonical_branch_by_boundary: Mapping[int, tuple] | None = None,
) -> MarketStatePCACommonFactorExperimentResult:
    """Run one canonical V1 branch while collecting separate PCA diagnostics."""
    if not isinstance(config, PCACommonFactorConfig):
        raise ValueError("config must be PCACommonFactorConfig")
    classifier_config = MarketClassifierConfig() if classifier_config is None else classifier_config
    lifecycle_config = MarketEpisodeLifecycleConfig() if lifecycle_config is None else lifecycle_config
    if not isinstance(classifier_config, MarketClassifierConfig):
        raise ValueError("classifier_config must be MarketClassifierConfig")
    if not isinstance(lifecycle_config, MarketEpisodeLifecycleConfig):
        raise ValueError("lifecycle_config must be MarketEpisodeLifecycleConfig")
    points = tuple(points)
    validate_experiment_points(points)
    baseline_state = None
    pca_state = None
    previous_scheduled = None
    paired = []
    for point in points:
        evaluation = point.movement_evaluation
        evidence, pca_state = advance_pca_common_factor(
            evaluation, config, pca_state, previous_scheduled)
        if evidence.status != PCA_NOT_SCHEDULED:
            previous_scheduled = evidence
        classification, lifecycle = canonical_branch_for_point(
            evaluation, point.source_time_evidence, baseline_state,
            classifier_config, lifecycle_config, canonical_branch_by_boundary)
        paired.append(PairedMarketStatePCACommonFactorPoint(
            evaluation.evaluation_boundary_time_ms, point.partition, evaluation,
            classification, lifecycle.next_state, lifecycle.transitions,
            evidence, pca_state,
        ))
        baseline_state = lifecycle.next_state
    paired = tuple(paired)
    summaries = {partition: _summary(paired, partition)
                 for partition in ("all", "development", "validation", "test")}
    return MarketStatePCACommonFactorExperimentResult(config, paired, summaries)
