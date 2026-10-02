"""EXP-75-08: causal, descriptive correlation network and clustering evidence."""

from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import median
from types import MappingProxyType
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
    EXPERIMENT_EVALUATION_INTERVAL_MS, ExperimentPartition, MarketStateExperimentPoint,
    advance_canonical_branch, directional_onset_count, episode_count, episode_spans,
    canonical_branch_for_point,
    observed_points_for_partition, selected_points_for_partition,
    short_lived_episode_count, transition_counts, validate_experiment_points,
)


CORRELATION_ALGORITHM_VERSION = "market-state-correlation-cluster-network-v1"
CORRELATION_CONFIG_VERSION_PREFIX = "market-state-correlation-cluster-network-config-v1"
CORRELATION_LOOKBACK_ROWS = (60, 120, 240)
CORRELATION_SAMPLE_INTERVAL_MS = 60_000
CORRELATION_EDGE_THRESHOLD = 0.70
CLUSTER_DISTANCE_THRESHOLD = 0.30
CORRELATION_NUMERICAL_TOL = 1e-12
CORRELATION_NOT_SCHEDULED = "CORRELATION_NOT_SCHEDULED"
CORRELATION_WARMING = "CORRELATION_WARMING"
CORRELATION_READY = "CORRELATION_READY"
CORRELATION_MODEL_UNAVAILABLE = "CORRELATION_MODEL_UNAVAILABLE"
CORRELATION_ZERO_VARIANCE = "CORRELATION_ZERO_VARIANCE"
CORRELATION_NUMERIC_UNAVAILABLE = "CORRELATION_NUMERIC_UNAVAILABLE"


def _version(rows: int) -> str:
    return (f"{CORRELATION_CONFIG_VERSION_PREFIX}:input-aligned-canonical-1m-returns"
            f":strict-prior-true:sample-60000ms:estimator-pearson-population"
            f":rows-{rows}:edge-0.70:distance-1-rho"
            f":clustering-agglomerative-average-linkage:cut-0.30")


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class CorrelationClusterConfig:
    version: str
    lookback_rows: int
    sample_interval_ms: int = CORRELATION_SAMPLE_INTERVAL_MS
    input_series: str = "aligned-canonical-1m-returns"
    strict_prior: bool = True
    estimator: str = "pearson-population"
    edge_threshold: float = CORRELATION_EDGE_THRESHOLD
    distance: str = "1-rho"
    clustering: str = "agglomerative-average-linkage"
    cluster_cut: float = CLUSTER_DISTANCE_THRESHOLD

    def __post_init__(self):
        if (type(self.lookback_rows) is not int
                or self.lookback_rows not in CORRELATION_LOOKBACK_ROWS
                or self.version != _version(self.lookback_rows)
                or type(self.sample_interval_ms) is not int
                or self.sample_interval_ms != CORRELATION_SAMPLE_INTERVAL_MS
                or self.input_series != "aligned-canonical-1m-returns"
                or self.strict_prior is not True
                or self.estimator != "pearson-population"
                or type(self.edge_threshold) is not float
                or self.edge_threshold != CORRELATION_EDGE_THRESHOLD
                or self.distance != "1-rho"
                or self.clustering != "agglomerative-average-linkage"
                or type(self.cluster_cut) is not float
                or self.cluster_cut != CLUSTER_DISTANCE_THRESHOLD):
            raise ValueError("correlation config must be one of the preregistered models")


CORRELATION_CONFIG_60M = CorrelationClusterConfig(_version(60), 60)
CORRELATION_CONFIG_120M = CorrelationClusterConfig(_version(120), 120)
CORRELATION_CONFIG_240M = CorrelationClusterConfig(_version(240), 240)
CORRELATION_CONFIGURATIONS = (
    CORRELATION_CONFIG_60M, CORRELATION_CONFIG_120M, CORRELATION_CONFIG_240M,
)


@dataclass(frozen=True)
class CorrelationAlignedReturnRow:
    evaluation_boundary_time_ms: int
    returns_by_symbol: tuple[tuple[str, float], ...]

    def __post_init__(self):
        boundary = self.evaluation_boundary_time_ms
        if (type(boundary) is not int or boundary < 0
                or boundary % CORRELATION_SAMPLE_INTERVAL_MS):
            raise ValueError("correlation row must be minute aligned")
        values = tuple(self.returns_by_symbol)
        if (not values or any(not isinstance(pair, tuple) or len(pair) != 2
                              or not isinstance(pair[0], str) or not pair[0]
                              or _finite(pair[1]) is None for pair in values)
                or len({name for name, _ in values}) != len(values)):
            raise ValueError("correlation row requires unique finite named returns")
        object.__setattr__(self, "returns_by_symbol", values)


@dataclass(frozen=True)
class CorrelationClusterState:
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
    rows: tuple[CorrelationAlignedReturnRow, ...]

    def __post_init__(self):
        if (self.algorithm_version != CORRELATION_ALGORITHM_VERSION
                or type(self.lookback_rows) is not int
                or self.lookback_rows not in CORRELATION_LOOKBACK_ROWS
                or self.config_version != _version(self.lookback_rows)
                or type(self.sample_interval_ms) is not int
                or self.sample_interval_ms != CORRELATION_SAMPLE_INTERVAL_MS):
            raise ValueError("correlation state identity is invalid")
        for name in ("baseline_movement_algorithm_version", "baseline_movement_config_version",
                     "universe_id", "universe_version", "provider", "exchange", "price_type"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} is required")
        universe = tuple(self.configured_universe)
        if (len(universe) < 2 or any(not isinstance(name, str) or not name for name in universe)
                or len(set(universe)) != len(universe)):
            raise ValueError("correlation universe requires at least two unique symbols")
        boundary = self.last_evaluation_boundary_time_ms
        if (type(boundary) is not int or boundary < 0
                or boundary % EXPERIMENT_EVALUATION_INTERVAL_MS):
            raise ValueError("correlation state boundary must be five-second aligned")
        rows = tuple(self.rows)
        if len(rows) > self.lookback_rows:
            raise ValueError("correlation history exceeds lookback")
        for index, row in enumerate(rows):
            if (not isinstance(row, CorrelationAlignedReturnRow)
                    or tuple(name for name, _ in row.returns_by_symbol) != universe):
                raise ValueError("correlation row must match universe order")
            if index and row.evaluation_boundary_time_ms != rows[index - 1].evaluation_boundary_time_ms + CORRELATION_SAMPLE_INTERVAL_MS:
                raise ValueError("correlation rows must be consecutive minutes")
        if rows and not 0 <= boundary - rows[-1].evaluation_boundary_time_ms < CORRELATION_SAMPLE_INTERVAL_MS:
            raise ValueError("last row must be the most recent aligned minute")
        object.__setattr__(self, "configured_universe", universe)
        object.__setattr__(self, "rows", rows)


def _validate_baseline(evaluation: MarketMovementEvaluation) -> None:
    if not isinstance(evaluation, MarketMovementEvaluation):
        raise ValueError("evaluation must be MarketMovementEvaluation")
    if (evaluation.algorithm_version != BASELINE_MOVEMENT_ALGORITHM_VERSION
            or evaluation.config_version != BASELINE_MOVEMENT_CONFIG_VERSION
            or set(evaluation.windows) != set(WINDOWS)
            or len(evaluation.configured_universe) < 2):
        raise ValueError("correlation experiment requires canonical V1 movement")
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
            raise ValueError("correlation movement window scope disagrees")


def _same_scope(state, evaluation, config) -> bool:
    return state is not None and (
        state.algorithm_version == CORRELATION_ALGORITHM_VERSION
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
        and evaluation.evaluation_boundary_time_ms == state.last_evaluation_boundary_time_ms + EXPERIMENT_EVALUATION_INTERVAL_MS
    )


def _current_row(evaluation: MarketMovementEvaluation) -> CorrelationAlignedReturnRow | None:
    items = evaluation.windows[1].symbols
    by_symbol = {item.symbol: item for item in items}
    if len(by_symbol) != len(items) or set(by_symbol) != set(evaluation.configured_universe):
        return None
    values = []
    for symbol in evaluation.configured_universe:
        item = by_symbol[symbol]
        value = _finite(item.current_return.value) if item.included and item.current_return.available else None
        if value is None:
            return None
        values.append((symbol, value))
    return CorrelationAlignedReturnRow(evaluation.evaluation_boundary_time_ms, tuple(values))


def _correlation_model_inputs(rows: tuple[CorrelationAlignedReturnRow, ...]):
    """Population Pearson estimates from exactly the supplied prior rows."""
    n, p = len(rows), len(rows[0].returns_by_symbol)
    columns = tuple(tuple(row.returns_by_symbol[j][1] for row in rows) for j in range(p))
    try:
        means = tuple(math.fsum(column) / n for column in columns)
        variances = tuple(math.fsum((value - means[j]) ** 2 for value in column) / n
                          for j, column in enumerate(columns))
        stds = tuple(math.sqrt(value) for value in variances)
    except (OverflowError, ValueError):
        return None, CORRELATION_NUMERIC_UNAVAILABLE
    if any(not math.isfinite(value) for value in (*means, *variances, *stds)):
        return None, CORRELATION_NUMERIC_UNAVAILABLE
    if any(value <= 0 for value in stds):
        return None, CORRELATION_ZERO_VARIANCE
    matrix = [[0.0] * p for _ in range(p)]
    for j in range(p):
        matrix[j][j] = 1.0
        for k in range(j + 1, p):
            try:
                covariance = math.fsum((a - means[j]) * (b - means[k])
                                       for a, b in zip(columns[j], columns[k])) / n
                rho = covariance / (stds[j] * stds[k])
            except (OverflowError, ValueError, ZeroDivisionError):
                return None, CORRELATION_NUMERIC_UNAVAILABLE
            if not math.isfinite(rho) or abs(rho) > 1 + CORRELATION_NUMERICAL_TOL:
                return None, CORRELATION_NUMERIC_UNAVAILABLE
            if abs(rho) > 1:
                rho = math.copysign(1.0, rho)
            matrix[j][k] = matrix[k][j] = rho
    return (means, stds, tuple(tuple(row) for row in matrix)), None


def _network(matrix: tuple[tuple[float, ...], ...], universe: tuple[str, ...]):
    p = len(universe)
    neighbors = [[] for _ in universe]
    edges = []
    for i in range(p):
        for j in range(i + 1, p):
            rho = matrix[i][j]
            if rho >= CORRELATION_EDGE_THRESHOLD - CORRELATION_NUMERICAL_TOL:
                edges.append((universe[i], universe[j], rho))
                neighbors[i].append(j)
                neighbors[j].append(i)
    seen = set()
    components = []
    for start in range(p):
        if start in seen:
            continue
        pending = [start]
        seen.add(start)
        members = []
        while pending:
            current = pending.pop(0)
            members.append(current)
            for neighbor in neighbors[current]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    pending.append(neighbor)
        components.append(tuple(sorted(members)))
    components.sort(key=lambda item: (item[0], item))
    return (tuple(edges), tuple((universe[i], len(neighbors[i])) for i in range(p)),
            tuple(tuple(universe[i] for i in group) for group in components))


def _clusters(matrix: tuple[tuple[float, ...], ...], universe: tuple[str, ...]):
    groups = [(i,) for i in range(len(universe))]
    while len(groups) > 1:
        best_distance = math.inf
        best_pair = None
        for a in range(len(groups)):
            for b in range(a + 1, len(groups)):
                distance = math.fsum(1 - matrix[i][j] for i in groups[a] for j in groups[b]) / (len(groups[a]) * len(groups[b]))
                key = (groups[a], groups[b])
                if (distance < best_distance - CORRELATION_NUMERICAL_TOL
                        or (abs(distance - best_distance) <= CORRELATION_NUMERICAL_TOL
                            and (best_pair is None or key < best_pair))):
                    best_distance, best_pair = distance, key
        if best_distance > CLUSTER_DISTANCE_THRESHOLD + CORRELATION_NUMERICAL_TOL:
            break
        left, right = best_pair
        groups.remove(left)
        groups.remove(right)
        groups.append(tuple(sorted((*left, *right))))
        groups.sort(key=lambda item: (item[0], item))
    return tuple(tuple(universe[i] for i in group) for group in groups)


def _jaccard(left: set, right: set) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def _edge_pairs(edges) -> set[tuple[str, str]]:
    return {(a, b) for a, b, _ in edges}


def _cluster_pairs(clusters) -> set[frozenset[str]]:
    return {frozenset((group[i], group[j])) for group in clusters
            for i in range(len(group)) for j in range(i + 1, len(group))}


@dataclass(frozen=True)
class CorrelationClusterEvidence:
    evaluation_boundary_time_ms: int
    status: str
    status_reason: str | None
    prior_row_count: int
    universe_size: int
    model_available: bool
    prior_means: tuple[tuple[str, float], ...] | None
    prior_population_stds: tuple[tuple[str, float], ...] | None
    correlation_matrix: tuple[tuple[float, ...], ...] | None
    pair_count: int | None
    mean_pairwise_correlation: float | None
    median_pairwise_correlation: float | None
    median_absolute_pairwise_correlation: float | None
    minimum_pairwise_correlation: float | None
    maximum_pairwise_correlation: float | None
    negative_pair_count: int | None
    negative_pair_fraction: float | None
    network_edge_threshold: float
    network_edges: tuple[tuple[str, str, float], ...] | None
    network_edge_count: int | None
    possible_edge_count: int
    network_edge_density: float | None
    degree_by_symbol: tuple[tuple[str, int], ...] | None
    connected_components: tuple[tuple[str, ...], ...] | None
    connected_component_count: int | None
    largest_connected_component_size: int | None
    largest_connected_component_fraction: float | None
    clusters: tuple[tuple[str, ...], ...] | None
    cluster_count: int | None
    singleton_cluster_count: int | None
    largest_cluster_size: int | None
    largest_cluster_fraction: float | None
    mean_within_cluster_pairwise_correlation: float | None
    network_edge_jaccard: float | None
    cluster_pair_jaccard: float | None
    dominant_material_side: str | None
    dominant_material_mover_count: int | None
    max_network_component_mover_share: float | None
    network_components_with_movers: int | None
    network_component_mover_coverage_fraction: float | None
    max_cluster_mover_share: float | None
    clusters_with_movers: int | None
    cluster_mover_coverage_fraction: float | None
    current_row_complete: bool | None
    current_row_appended_after_evaluation: bool
    history_reset_after_evaluation: bool
    scope_reset_before_evaluation: bool
    algorithm_version: str
    config_version: str
    baseline_movement_algorithm_version: str
    baseline_movement_config_version: str
    universe_id: str
    universe_version: str
    configured_universe: tuple[str, ...]
    provider: str
    exchange: str
    price_type: str


def _mover_concentration(groups, movers):
    counts = tuple(sum(name in movers for name in group) for group in groups)
    occupied = sum(count > 0 for count in counts)
    return max(counts) / len(movers), occupied, occupied / len(groups)


def _material_movers(evaluation):
    window = evaluation.windows[5]
    included = tuple(item for item in window.symbols if item.included)
    rising = {item.symbol for item in included if item.material_rising}
    falling = {item.symbol for item in included if item.material_falling}
    if len(rising) > len(falling):
        return "RISING", rising
    if len(falling) > len(rising):
        return "FALLING", falling
    return "NONE", set()


def advance_correlation_cluster_diagnostics(
    evaluation: MarketMovementEvaluation,
    config: CorrelationClusterConfig,
    state: CorrelationClusterState | None = None,
    previous_scheduled_evidence: CorrelationClusterEvidence | None = None,
) -> tuple[CorrelationClusterEvidence, CorrelationClusterState]:
    """Report a strict-prior model, then append or clear the current aligned row."""
    _validate_baseline(evaluation)
    if not isinstance(config, CorrelationClusterConfig):
        raise ValueError("config must be CorrelationClusterConfig")
    if state is not None and not isinstance(state, CorrelationClusterState):
        raise ValueError("state must be CorrelationClusterState or None")
    if previous_scheduled_evidence is not None and not isinstance(previous_scheduled_evidence, CorrelationClusterEvidence):
        raise ValueError("previous scheduled evidence type is invalid")
    boundary = evaluation.evaluation_boundary_time_ms
    if type(boundary) is not int or boundary < 0 or boundary % EXPERIMENT_EVALUATION_INTERVAL_MS:
        raise ValueError("correlation boundary must be five-second aligned")
    compatible = _same_scope(state, evaluation, config)
    prior_rows = state.rows if compatible else ()
    scheduled = boundary % CORRELATION_SAMPLE_INTERVAL_MS == 0
    current = _current_row(evaluation) if scheduled else None
    status = CORRELATION_NOT_SCHEDULED
    reason = None
    inputs = None
    if scheduled:
        if len(prior_rows) < config.lookback_rows:
            status = CORRELATION_WARMING
        else:
            inputs, reason = _correlation_model_inputs(prior_rows)
            status = CORRELATION_READY if inputs is not None else CORRELATION_MODEL_UNAVAILABLE
    values = {}
    if inputs is not None:
        means, stds, matrix = inputs
        universe = evaluation.configured_universe
        p = len(universe)
        pairs = tuple(matrix[i][j] for i in range(p) for j in range(i + 1, p))
        edges, degree, components = _network(matrix, universe)
        clusters = _clusters(matrix, universe)
        within = tuple(matrix[universe.index(group[i])][universe.index(group[j])]
                       for group in clusters for i in range(len(group)) for j in range(i + 1, len(group)))
        values = dict(
            prior_means=tuple(zip(universe, means)), prior_population_stds=tuple(zip(universe, stds)),
            correlation_matrix=matrix, pair_count=len(pairs),
            mean_pairwise_correlation=math.fsum(pairs) / len(pairs),
            median_pairwise_correlation=float(median(pairs)),
            median_absolute_pairwise_correlation=float(median(abs(value) for value in pairs)),
            minimum_pairwise_correlation=min(pairs), maximum_pairwise_correlation=max(pairs),
            negative_pair_count=sum(value < 0 for value in pairs),
            negative_pair_fraction=sum(value < 0 for value in pairs) / len(pairs),
            network_edges=edges, network_edge_count=len(edges),
            network_edge_density=len(edges) / len(pairs), degree_by_symbol=degree,
            connected_components=components, connected_component_count=len(components),
            largest_connected_component_size=max(map(len, components)),
            largest_connected_component_fraction=max(map(len, components)) / p,
            clusters=clusters, cluster_count=len(clusters),
            singleton_cluster_count=sum(len(group) == 1 for group in clusters),
            largest_cluster_size=max(map(len, clusters)),
            largest_cluster_fraction=max(map(len, clusters)) / p,
            mean_within_cluster_pairwise_correlation=(math.fsum(within) / len(within) if within else None),
        )
        prior = previous_scheduled_evidence
        if (compatible and prior is not None and prior.status == CORRELATION_READY
                and prior.evaluation_boundary_time_ms == boundary - CORRELATION_SAMPLE_INTERVAL_MS
                and prior.config_version == config.version
                and prior.algorithm_version == CORRELATION_ALGORITHM_VERSION
                and prior.baseline_movement_algorithm_version == evaluation.algorithm_version
                and prior.baseline_movement_config_version == evaluation.config_version
                and prior.universe_id == evaluation.universe_id
                and prior.universe_version == evaluation.universe_version
                and prior.configured_universe == universe
                and prior.provider == evaluation.provider
                and prior.exchange == evaluation.exchange
                and prior.price_type == evaluation.price_type
                and prior.prior_means is not None
                and tuple(name for name, _ in prior.prior_means) == universe
                and not prior.history_reset_after_evaluation):
            values["network_edge_jaccard"] = _jaccard(_edge_pairs(prior.network_edges), _edge_pairs(edges))
            values["cluster_pair_jaccard"] = _jaccard(_cluster_pairs(prior.clusters), _cluster_pairs(clusters))
        if evaluation.windows[5].market_wide_eligible:
            side, movers = _material_movers(evaluation)
            values["dominant_material_side"] = side
            values["dominant_material_mover_count"] = len(movers)
            if movers:
                (values["max_network_component_mover_share"],
                 values["network_components_with_movers"],
                 values["network_component_mover_coverage_fraction"]) = _mover_concentration(components, movers)
                (values["max_cluster_mover_share"], values["clusters_with_movers"],
                 values["cluster_mover_coverage_fraction"]) = _mover_concentration(clusters, movers)
    appended = scheduled and current is not None
    cleared = scheduled and current is None
    next_rows = ((*prior_rows, current)[-config.lookback_rows:] if appended
                 else () if cleared else prior_rows)
    next_state = CorrelationClusterState(
        CORRELATION_ALGORITHM_VERSION, config.version, config.lookback_rows,
        config.sample_interval_ms, evaluation.algorithm_version, evaluation.config_version,
        evaluation.universe_id, evaluation.universe_version, evaluation.configured_universe,
        evaluation.provider, evaluation.exchange, evaluation.price_type, boundary, next_rows,
    )
    defaults = {name: None for name in CorrelationClusterEvidence.__dataclass_fields__}
    defaults.update(values)
    defaults.update(
        evaluation_boundary_time_ms=boundary, status=status, status_reason=reason,
        prior_row_count=len(prior_rows), universe_size=len(evaluation.configured_universe),
        model_available=inputs is not None, network_edge_threshold=CORRELATION_EDGE_THRESHOLD,
        possible_edge_count=len(evaluation.configured_universe) * (len(evaluation.configured_universe) - 1) // 2,
        current_row_complete=current is not None if scheduled else None,
        current_row_appended_after_evaluation=appended, history_reset_after_evaluation=cleared,
        scope_reset_before_evaluation=state is not None and not compatible,
        algorithm_version=CORRELATION_ALGORITHM_VERSION, config_version=config.version,
        baseline_movement_algorithm_version=evaluation.algorithm_version,
        baseline_movement_config_version=evaluation.config_version,
        universe_id=evaluation.universe_id, universe_version=evaluation.universe_version,
        configured_universe=evaluation.configured_universe, provider=evaluation.provider,
        exchange=evaluation.exchange, price_type=evaluation.price_type,
    )
    return CorrelationClusterEvidence(**defaults), next_state


@dataclass(frozen=True)
class PairedMarketStateCorrelationClusterPoint:
    evaluation_boundary_time_ms: int
    partition: ExperimentPartition
    baseline_evaluation: MarketMovementEvaluation
    baseline_classification: MarketClassificationEvaluation
    baseline_lifecycle_state: MarketEpisodeLifecycleState
    baseline_transitions: tuple[MarketEpisodeTransition, ...]
    correlation_evidence: CorrelationClusterEvidence
    correlation_state: CorrelationClusterState


@dataclass(frozen=True)
class CorrelationGroupedDiagnostic:
    group: str
    count: int
    median_pairwise_correlation: float | None
    median_absolute_pairwise_correlation: float | None
    median_network_edge_density: float | None
    median_largest_connected_component_fraction: float | None
    median_cluster_count: float | None
    median_largest_cluster_fraction: float | None
    median_max_network_component_mover_share: float | None
    median_max_cluster_mover_share: float | None


@dataclass(frozen=True)
class CorrelationNetworkSymbolDiagnostic:
    symbol: str
    comparable_count: int
    median_degree: float | None
    median_degree_fraction: float | None


@dataclass(frozen=True)
class CorrelationClusterComparisonSummary:
    partition: str
    evaluation_count: int
    scheduled_count: int
    not_scheduled_count: int
    warming_count: int
    model_ready_count: int
    model_unavailable_count: int
    median_pairwise_correlation: float | None
    median_absolute_pairwise_correlation: float | None
    median_negative_pair_fraction: float | None
    median_network_edge_density: float | None
    median_connected_component_count: float | None
    median_largest_component_fraction: float | None
    median_cluster_count: float | None
    median_singleton_cluster_count: float | None
    median_largest_cluster_fraction: float | None
    median_within_cluster_correlation: float | None
    network_stability_comparison_count: int
    median_network_edge_jaccard: float | None
    minimum_network_edge_jaccard: float | None
    cluster_stability_comparison_count: int
    median_cluster_pair_jaccard: float | None
    minimum_cluster_pair_jaccard: float | None
    material_mover_comparable_count: int
    median_max_network_component_mover_share: float | None
    median_network_component_mover_coverage_fraction: float | None
    median_max_cluster_mover_share: float | None
    median_cluster_mover_coverage_fraction: float | None
    directional_breadth_comparison_count: int
    pearson_network_edge_density_vs_directional_breadth: float | None
    pearson_largest_component_fraction_vs_directional_breadth: float | None
    pearson_largest_cluster_fraction_vs_directional_breadth: float | None
    material_breadth_comparison_count: int
    pearson_network_mover_share_vs_material_breadth: float | None
    pearson_cluster_mover_share_vs_material_breadth: float | None
    baseline_state_grouped_diagnostics: tuple[CorrelationGroupedDiagnostic, ...]
    baseline_active_diagnostic: CorrelationGroupedDiagnostic
    baseline_inactive_diagnostic: CorrelationGroupedDiagnostic
    per_symbol_network_diagnostics: tuple[CorrelationNetworkSymbolDiagnostic, ...]
    baseline_transition_counts: tuple[tuple[str, int], ...]
    baseline_episode_count: int
    baseline_short_lived_closed_episode_count: int
    baseline_directional_onset_count: int


@dataclass(frozen=True)
class MarketStateCorrelationClusterExperimentResult:
    correlation_config: CorrelationClusterConfig
    paired_points: tuple[PairedMarketStateCorrelationClusterPoint, ...]
    summaries: Mapping[str, CorrelationClusterComparisonSummary]

    def __post_init__(self):
        object.__setattr__(self, "paired_points", tuple(self.paired_points))
        object.__setattr__(self, "summaries", MappingProxyType(dict(self.summaries)))


def _median_or_none(values) -> float | None:
    return float(median(values)) if values else None


def _pearson(pairs: tuple[tuple[float, float], ...]) -> float | None:
    if len(pairs) < 2:
        return None
    x_mean = math.fsum(x for x, _ in pairs) / len(pairs)
    y_mean = math.fsum(y for _, y in pairs) / len(pairs)
    numerator = math.fsum((x - x_mean) * (y - y_mean) for x, y in pairs)
    x_var = math.fsum((x - x_mean) ** 2 for x, _ in pairs)
    y_var = math.fsum((y - y_mean) ** 2 for _, y in pairs)
    denominator = math.sqrt(x_var * y_var)
    return numerator / denominator if denominator > 0 and math.isfinite(denominator) else None


def _breadth(point, material=False):
    breadth = point.baseline_classification.windows[5].breadth
    rising = breadth.material_rising if material else breadth.rising
    falling = breadth.material_falling if material else breadth.falling
    if breadth.available and rising.available and falling.available:
        return max(rising.value.fraction, falling.value.fraction)
    return None


def _group(group, points):
    ready = tuple(point.correlation_evidence for point in points
                  if point.correlation_evidence.status == CORRELATION_READY)
    def med(name):
        return _median_or_none(tuple(value for item in ready
                                     if (value := getattr(item, name)) is not None))
    return CorrelationGroupedDiagnostic(
        group, len(ready), med("median_pairwise_correlation"),
        med("median_absolute_pairwise_correlation"), med("network_edge_density"),
        med("largest_connected_component_fraction"), med("cluster_count"),
        med("largest_cluster_fraction"), med("max_network_component_mover_share"),
        med("max_cluster_mover_share"),
    )


def _summary(points: tuple[PairedMarketStateCorrelationClusterPoint, ...], partition: str):
    selected = selected_points_for_partition(points, partition)
    observed = observed_points_for_partition(points, partition)
    spans = episode_spans(observed, "baseline")
    scheduled = tuple(point for point in selected
                      if point.correlation_evidence.status != CORRELATION_NOT_SCHEDULED)
    ready_points = tuple(point for point in scheduled
                         if point.correlation_evidence.status == CORRELATION_READY)
    ready = tuple(point.correlation_evidence for point in ready_points)
    def med(name):
        return _median_or_none(tuple(value for item in ready
                                     if (value := getattr(item, name)) is not None))
    network_stability = tuple(item.network_edge_jaccard for item in ready
                              if item.network_edge_jaccard is not None)
    cluster_stability = tuple(item.cluster_pair_jaccard for item in ready
                              if item.cluster_pair_jaccard is not None)
    directional = tuple((point.correlation_evidence, value) for point in ready_points
                        if (value := _breadth(point)) is not None)
    material = tuple((point.correlation_evidence, value) for point in ready_points
                     if point.correlation_evidence.max_network_component_mover_share is not None
                     and (value := _breadth(point, material=True)) is not None)
    def association(items, name):
        return _pearson(tuple((getattr(evidence, name), breadth)
                              for evidence, breadth in items))
    universe_order = tuple(dict.fromkeys(symbol for point in selected
                                         for symbol in point.baseline_evaluation.configured_universe))
    symbol_diagnostics = []
    for symbol in universe_order:
        degrees = tuple(dict(item.degree_by_symbol)[symbol] for item in ready
                        if item.degree_by_symbol is not None and symbol in dict(item.degree_by_symbol))
        fractions = tuple(dict(item.degree_by_symbol)[symbol] / (item.universe_size - 1)
                          for item in ready if item.degree_by_symbol is not None
                          and symbol in dict(item.degree_by_symbol))
        symbol_diagnostics.append(CorrelationNetworkSymbolDiagnostic(
            symbol, len(degrees), _median_or_none(degrees), _median_or_none(fractions)))
    groups = tuple(_group(state, tuple(point for point in scheduled
                                       if point.baseline_classification.windows[5].direction_state == state))
                   for state in ("BROAD_RISE", "BROAD_DROP", "NEUTRAL"))
    active = tuple(point for point in scheduled
                   if point.baseline_lifecycle_state.active_episode is not None)
    inactive = tuple(point for point in scheduled
                     if point.baseline_lifecycle_state.active_episode is None)
    return CorrelationClusterComparisonSummary(
        partition=partition, evaluation_count=len(selected), scheduled_count=len(scheduled),
        not_scheduled_count=len(selected) - len(scheduled),
        warming_count=sum(point.correlation_evidence.status == CORRELATION_WARMING for point in scheduled),
        model_ready_count=len(ready),
        model_unavailable_count=sum(point.correlation_evidence.status == CORRELATION_MODEL_UNAVAILABLE for point in scheduled),
        median_pairwise_correlation=med("median_pairwise_correlation"),
        median_absolute_pairwise_correlation=med("median_absolute_pairwise_correlation"),
        median_negative_pair_fraction=med("negative_pair_fraction"),
        median_network_edge_density=med("network_edge_density"),
        median_connected_component_count=med("connected_component_count"),
        median_largest_component_fraction=med("largest_connected_component_fraction"),
        median_cluster_count=med("cluster_count"),
        median_singleton_cluster_count=med("singleton_cluster_count"),
        median_largest_cluster_fraction=med("largest_cluster_fraction"),
        median_within_cluster_correlation=med("mean_within_cluster_pairwise_correlation"),
        network_stability_comparison_count=len(network_stability),
        median_network_edge_jaccard=_median_or_none(network_stability),
        minimum_network_edge_jaccard=min(network_stability) if network_stability else None,
        cluster_stability_comparison_count=len(cluster_stability),
        median_cluster_pair_jaccard=_median_or_none(cluster_stability),
        minimum_cluster_pair_jaccard=min(cluster_stability) if cluster_stability else None,
        material_mover_comparable_count=sum(item.max_network_component_mover_share is not None for item in ready),
        median_max_network_component_mover_share=med("max_network_component_mover_share"),
        median_network_component_mover_coverage_fraction=med("network_component_mover_coverage_fraction"),
        median_max_cluster_mover_share=med("max_cluster_mover_share"),
        median_cluster_mover_coverage_fraction=med("cluster_mover_coverage_fraction"),
        directional_breadth_comparison_count=len(directional),
        pearson_network_edge_density_vs_directional_breadth=association(directional, "network_edge_density"),
        pearson_largest_component_fraction_vs_directional_breadth=association(directional, "largest_connected_component_fraction"),
        pearson_largest_cluster_fraction_vs_directional_breadth=association(directional, "largest_cluster_fraction"),
        material_breadth_comparison_count=len(material),
        pearson_network_mover_share_vs_material_breadth=association(material, "max_network_component_mover_share"),
        pearson_cluster_mover_share_vs_material_breadth=association(material, "max_cluster_mover_share"),
        baseline_state_grouped_diagnostics=groups,
        baseline_active_diagnostic=_group("active", active),
        baseline_inactive_diagnostic=_group("inactive", inactive),
        per_symbol_network_diagnostics=tuple(symbol_diagnostics),
        baseline_transition_counts=transition_counts(selected, "baseline"),
        baseline_episode_count=episode_count(spans, selected, partition),
        baseline_short_lived_closed_episode_count=short_lived_episode_count(spans, selected, partition),
        baseline_directional_onset_count=directional_onset_count(selected, "baseline"),
    )


def run_market_state_correlation_cluster_experiment(
    points: Iterable[MarketStateExperimentPoint],
    config: CorrelationClusterConfig,
    *,
    classifier_config: MarketClassifierConfig | None = None,
    lifecycle_config: MarketEpisodeLifecycleConfig | None = None,
    canonical_branch_by_boundary: Mapping[int, tuple] | None = None,
) -> MarketStateCorrelationClusterExperimentResult:
    """Advance one canonical #72/#73 branch and independent correlation evidence."""
    if not isinstance(config, CorrelationClusterConfig):
        raise ValueError("config must be CorrelationClusterConfig")
    classifier_config = MarketClassifierConfig() if classifier_config is None else classifier_config
    lifecycle_config = MarketEpisodeLifecycleConfig() if lifecycle_config is None else lifecycle_config
    if not isinstance(classifier_config, MarketClassifierConfig):
        raise ValueError("classifier_config must be MarketClassifierConfig")
    if not isinstance(lifecycle_config, MarketEpisodeLifecycleConfig):
        raise ValueError("lifecycle_config must be MarketEpisodeLifecycleConfig")
    points = tuple(points)
    validate_experiment_points(points)
    baseline_state = None
    correlation_state = None
    previous_scheduled = None
    paired = []
    for point in points:
        evaluation = point.movement_evaluation
        evidence, correlation_state = advance_correlation_cluster_diagnostics(
            evaluation, config, correlation_state, previous_scheduled)
        if evidence.status != CORRELATION_NOT_SCHEDULED:
            previous_scheduled = evidence
        classification, lifecycle = canonical_branch_for_point(
            evaluation, point.source_time_evidence, baseline_state,
            classifier_config, lifecycle_config, canonical_branch_by_boundary)
        paired.append(PairedMarketStateCorrelationClusterPoint(
            evaluation.evaluation_boundary_time_ms, point.partition, evaluation,
            classification, lifecycle.next_state, lifecycle.transitions,
            evidence, correlation_state))
        baseline_state = lifecycle.next_state
    paired = tuple(paired)
    summaries = {partition: _summary(paired, partition)
                 for partition in ("all", "development", "validation", "test")}
    return MarketStateCorrelationClusterExperimentResult(config, paired, summaries)
