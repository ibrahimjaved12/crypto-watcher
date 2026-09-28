"""Deterministic and causal fixtures for EXP-75-08."""

from dataclasses import replace
from decimal import Decimal
import unittest

from market_analysis.experiments.market_state_common import MarketStateExperimentPoint
from market_analysis.experiments.market_state_correlation_clusters import (
    CLUSTER_DISTANCE_THRESHOLD, CORRELATION_ALGORITHM_VERSION,
    CORRELATION_CONFIG_60M, CORRELATION_CONFIGURATIONS, CORRELATION_EDGE_THRESHOLD,
    CORRELATION_MODEL_UNAVAILABLE, CORRELATION_NOT_SCHEDULED, CORRELATION_READY,
    CORRELATION_WARMING, CORRELATION_ZERO_VARIANCE,
    CorrelationAlignedReturnRow, CorrelationClusterState, _cluster_pairs,
    _clusters, _correlation_model_inputs, _edge_pairs, _jaccard, _network,
    advance_correlation_cluster_diagnostics, run_market_state_correlation_cluster_experiment,
)
from market_analysis.movement_classifier import SymbolSourceTimeEvidence
from market_analysis.movement_metrics import (
    ALGORITHM_VERSION, DEFAULT_CONFIG_VERSION, ExcludedSymbol, MarketMovementConfig,
    MarketMovementEvaluation, MarketMovementWindowResult, Metric, SymbolMovementResult,
    WINDOWS, _aggregates, _breadth, _outliers,
)


SYMBOLS = ("S1", "S2", "S3", "S4")
V1_CONFIG = MarketMovementConfig()


def _row(index, values, symbols=SYMBOLS):
    return CorrelationAlignedReturnRow(index * 60_000, tuple(zip(symbols, values)))


def _state(config=CORRELATION_CONFIG_60M, *, symbols=SYMBOLS, values=None, count=None):
    count = config.lookback_rows if count is None else count
    values = values or (lambda index: (float((-1) ** index),) * len(symbols))
    rows = tuple(_row(index, values(index), symbols) for index in range(count))
    return CorrelationClusterState(
        CORRELATION_ALGORITHM_VERSION, config.version, config.lookback_rows, 60_000,
        ALGORITHM_VERSION, DEFAULT_CONFIG_VERSION, "u1", "v1", symbols,
        "binance-usdm", "binance", "trade", count * 60_000 - 5_000, rows,
    )


def _symbol(name, boundary, minute, raw, excluded=False):
    z = 0.6745 * raw / 0.02
    direction = "FLAT" if raw == 0 or abs(z) < V1_CONFIG.flat_z else "RISING" if raw > 0 else "FALLING"
    return SymbolMovementResult(
        symbol=name, instrument_id=name, provider="binance-usdm", exchange="binance",
        price_type="trade", window_minutes=minute, evaluation_boundary_time_ms=boundary,
        included=not excluded, exclusion_reasons=("TEST_EXCLUDED",) if excluded else (),
        current_return=Metric.missing("TEST_EXCLUDED") if excluded else Metric.present(raw),
        previous_return=Metric.present(0.01), velocity=Metric.present(raw / (minute * 60)),
        previous_velocity=Metric.present(0.001), acceleration=Metric.present(0.001),
        historical_median=Metric.present(0.0), historical_mad=Metric.present(0.02),
        normalized_z=Metric.missing("TEST_EXCLUDED") if excluded else Metric.present(z),
        direction=Metric.missing("TEST_EXCLUDED") if excluded else Metric.present(direction),
        material_rising=not excluded and raw > 0 and abs(z) >= V1_CONFIG.material_z,
        material_falling=not excluded and raw < 0 and abs(z) >= V1_CONFIG.material_z,
        current_notional_volume=Metric.present(Decimal("10")), rvol=Metric.present(1.0),
        cross_sectional_z=Metric.missing("CROSS_SECTIONAL_MAD_UNAVAILABLE"),
        outlier_candidate=False,
    )


def _evaluation(boundary, values=None, *, symbols=SYMBOLS, excluded_1m=(),
                five_minute_values=None, identity=None, force_eligible=False):
    identity = identity or {}
    values = values if values is not None else (0.06,) * len(symbols)
    windows = {}
    for minute in WINDOWS:
        raw_values = five_minute_values if minute == 5 and five_minute_values is not None else values
        excluded = set(excluded_1m if minute == 1 else ())
        results = tuple(_symbol(symbol, boundary, minute, raw, symbol in excluded)
                        for symbol, raw in zip(symbols, raw_values))
        included = tuple(item for item in results if item.included)
        eligible = force_eligible or (len(included) >= 5 and len(included) / len(symbols) >= 0.6)
        if eligible:
            results = _outliers(results, V1_CONFIG)
            included = tuple(item for item in results if item.included)
        windows[minute] = MarketMovementWindowResult(
            algorithm_version=identity.get("movement_algorithm_version", ALGORITHM_VERSION),
            config_version=identity.get("movement_config_version", DEFAULT_CONFIG_VERSION),
            universe_id=identity.get("universe_id", "u1"),
            universe_version=identity.get("universe_version", "v1"),
            configured_universe=symbols, included_symbols=tuple(item.symbol for item in included),
            excluded_symbols=tuple(ExcludedSymbol(symbol, ("TEST_EXCLUDED",))
                                   for symbol in symbols if symbol in excluded),
            window_minutes=minute, provider=identity.get("provider", "binance-usdm"),
            exchange=identity.get("exchange", "binance"),
            price_type=identity.get("price_type", "trade"),
            evaluation_boundary_time_ms=boundary, historical_lookback_ms=100,
            minimum_historical_coverage_ms=50, market_wide_eligible=eligible,
            eligible_count=len(included), eligible_fraction=len(included) / len(symbols),
            symbols=results, breadth=_breadth(included, eligible),
            aggregates=_aggregates(included, eligible, V1_CONFIG),
        )
    return MarketMovementEvaluation(
        algorithm_version=identity.get("movement_algorithm_version", ALGORITHM_VERSION),
        config_version=identity.get("movement_config_version", DEFAULT_CONFIG_VERSION),
        universe_id=identity.get("universe_id", "u1"),
        universe_version=identity.get("universe_version", "v1"),
        configured_universe=symbols, provider=identity.get("provider", "binance-usdm"),
        exchange=identity.get("exchange", "binance"),
        price_type=identity.get("price_type", "trade"),
        evaluation_boundary_time_ms=boundary, historical_lookback_ms=100,
        minimum_historical_coverage_ms=50, windows=windows,
    )


def _point(boundary, partition="development", **kwargs):
    evaluation = _evaluation(boundary, **kwargs)
    source = tuple(SymbolSourceTimeEvidence(symbol, None, None, None)
                   for symbol in evaluation.configured_universe)
    return MarketStateExperimentPoint(evaluation, source, partition)


class CorrelationClusterExperimentTests(unittest.TestCase):
    def test_fixed_config_and_population_pearson(self):
        self.assertEqual(tuple(config.lookback_rows for config in CORRELATION_CONFIGURATIONS),
                         (60, 120, 240))
        self.assertEqual(CORRELATION_EDGE_THRESHOLD, 0.70)
        self.assertEqual(CLUSTER_DISTANCE_THRESHOLD, 0.30)
        with self.assertRaises(ValueError):
            replace(CORRELATION_CONFIG_60M, edge_threshold=0.69)
        rows = tuple(_row(i, (float((1, 1, -1, -1)[i % 4]),) * 4) for i in range(60))
        (means, stds, matrix), reason = _correlation_model_inputs(rows)
        self.assertIsNone(reason)
        self.assertEqual(means, (0.0,) * 4)
        self.assertEqual(stds, (1.0,) * 4)
        self.assertEqual(matrix, ((1.0,) * 4,) * 4)

    def test_complete_network_and_two_independent_groups(self):
        complete = _state()
        evidence, _ = advance_correlation_cluster_diagnostics(_evaluation(60 * 60_000), CORRELATION_CONFIG_60M, complete)
        self.assertEqual(evidence.status, CORRELATION_READY)
        self.assertEqual(evidence.network_edge_count, 6)
        self.assertEqual(evidence.network_edge_density, 1.0)
        self.assertEqual(evidence.connected_components, (SYMBOLS,))
        self.assertEqual(evidence.clusters, (SYMBOLS,))
        self.assertEqual(evidence.largest_connected_component_fraction, 1.0)
        self.assertEqual(evidence.largest_cluster_fraction, 1.0)
        self.assertEqual(evidence.mean_within_cluster_pairwise_correlation, 1.0)
        x, y = (1.0, 1.0, -1.0, -1.0), (1.0, -1.0, 1.0, -1.0)
        state = _state(values=lambda i: (x[i % 4], x[i % 4], y[i % 4], y[i % 4]))
        evidence, _ = advance_correlation_cluster_diagnostics(_evaluation(60 * 60_000), CORRELATION_CONFIG_60M, state)
        self.assertEqual(evidence.correlation_matrix[0][1], 1.0)
        self.assertEqual(evidence.correlation_matrix[0][2], 0.0)
        self.assertEqual(evidence.network_edge_count, 2)
        self.assertEqual(evidence.network_edge_density, 1 / 3)
        self.assertEqual(evidence.connected_components, (("S1", "S2"), ("S3", "S4")))
        self.assertEqual(evidence.clusters, evidence.connected_components)
        self.assertEqual(evidence.largest_cluster_fraction, 0.5)

    def test_anticorrelation_never_becomes_a_positive_edge(self):
        x = (1.0, 1.0, -1.0, -1.0)
        state = _state(values=lambda i: (x[i % 4], x[i % 4], -x[i % 4], -x[i % 4]))
        evidence, _ = advance_correlation_cluster_diagnostics(_evaluation(60 * 60_000), CORRELATION_CONFIG_60M, state)
        self.assertEqual(evidence.negative_pair_count, 4)
        self.assertEqual(evidence.minimum_pairwise_correlation, -1.0)
        self.assertEqual(evidence.network_edge_count, 2)
        self.assertEqual(evidence.clusters, (("S1", "S2"), ("S3", "S4")))

    def test_network_chain_differs_from_average_linkage_and_ties_are_stable(self):
        names = ("A", "B", "C")
        matrix = ((1.0, 0.7, 0.0), (0.7, 1.0, 0.7), (0.0, 0.7, 1.0))
        edges, degrees, components = _network(matrix, names)
        self.assertEqual(_edge_pairs(edges), {("A", "B"), ("B", "C")})
        self.assertEqual(degrees, (("A", 1), ("B", 2), ("C", 1)))
        self.assertEqual(components, (names,))
        self.assertEqual(_clusters(matrix, names), (("A", "B"), ("C",)))
        self.assertEqual(_clusters(matrix, names), _clusters(matrix, names))
        boundary = ((1.0, 0.7, 0.699), (0.7, 1.0, 0.0), (0.699, 0.0, 1.0))
        self.assertEqual(_edge_pairs(_network(boundary, names)[0]), {("A", "B")})

    def test_warming_zero_variance_and_missing_row(self):
        for config in CORRELATION_CONFIGURATIONS:
            state = _state(config, count=config.lookback_rows - 1)
            at = (config.lookback_rows - 1) * 60_000
            evidence, _ = advance_correlation_cluster_diagnostics(_evaluation(at), config, state)
            self.assertEqual(evidence.status, CORRELATION_WARMING)
            self.assertEqual(evidence.prior_row_count, config.lookback_rows - 1)
            ready_state = _state(config)
            evidence, _ = advance_correlation_cluster_diagnostics(_evaluation(config.lookback_rows * 60_000), config, ready_state)
            self.assertEqual(evidence.status, CORRELATION_READY)
        constant = _state(values=lambda i: (0.0, float(i % 4), float(i % 3), float(i % 5)))
        evidence, next_state = advance_correlation_cluster_diagnostics(_evaluation(60 * 60_000), CORRELATION_CONFIG_60M, constant)
        self.assertEqual((evidence.status, evidence.status_reason),
                         (CORRELATION_MODEL_UNAVAILABLE, CORRELATION_ZERO_VARIANCE))
        self.assertEqual(len(next_state.rows), 60)
        complete = _state()
        evidence, next_state = advance_correlation_cluster_diagnostics(
            _evaluation(60 * 60_000, excluded_1m=("S4",)), CORRELATION_CONFIG_60M, complete)
        self.assertEqual(evidence.status, CORRELATION_READY)
        self.assertFalse(evidence.current_row_complete)
        self.assertFalse(evidence.current_row_appended_after_evaluation)
        self.assertTrue(evidence.history_reset_after_evaluation)
        self.assertEqual(next_state.rows, ())
        restarted = replace(next_state, last_evaluation_boundary_time_ms=61 * 60_000 - 5_000)
        warming, _ = advance_correlation_cluster_diagnostics(
            _evaluation(61 * 60_000), CORRELATION_CONFIG_60M, restarted)
        self.assertEqual(warming.status, CORRELATION_WARMING)

    def test_strict_prior_shock_and_non_scheduled_point(self):
        prior = _state()
        ordinary, ordinary_state = advance_correlation_cluster_diagnostics(
            _evaluation(60 * 60_000, values=(0.01,) * 4), CORRELATION_CONFIG_60M, prior)
        shock, shock_state = advance_correlation_cluster_diagnostics(
            _evaluation(60 * 60_000, values=(100.0, -100.0, 200.0, -200.0)),
            CORRELATION_CONFIG_60M, prior)
        for name in ("prior_means", "prior_population_stds", "correlation_matrix",
                     "mean_pairwise_correlation", "median_pairwise_correlation",
                     "network_edges", "connected_components", "clusters"):
            self.assertEqual(getattr(ordinary, name), getattr(shock, name))
        self.assertNotEqual(ordinary_state.rows[-1], shock_state.rows[-1])
        unscheduled, unchanged = advance_correlation_cluster_diagnostics(
            _evaluation(60 * 60_000 + 5_000), CORRELATION_CONFIG_60M, ordinary_state)
        self.assertEqual(unscheduled.status, CORRELATION_NOT_SCHEDULED)
        self.assertEqual(unchanged.rows, ordinary_state.rows)

    def test_stability_and_mover_concentration(self):
        self.assertEqual(_jaccard(set(), set()), 1.0)
        self.assertEqual(_jaccard({1, 2}, {2, 3}), 1 / 3)
        self.assertEqual(_jaccard(_cluster_pairs((("A",), ("B",))),
                                  _cluster_pairs((("A",), ("B",)))), 1.0)
        self.assertEqual(_jaccard(_cluster_pairs((("A", "B"), ("C",))),
                                  _cluster_pairs((("A", "C"), ("B",)))), 0.0)
        complete = _state()
        first, next_state = advance_correlation_cluster_diagnostics(
            _evaluation(60 * 60_000), CORRELATION_CONFIG_60M, complete)
        next_state = replace(next_state, last_evaluation_boundary_time_ms=61 * 60_000 - 5_000)
        second, _ = advance_correlation_cluster_diagnostics(
            _evaluation(61 * 60_000), CORRELATION_CONFIG_60M, next_state, first)
        self.assertEqual(second.network_edge_jaccard, 1.0)
        self.assertEqual(second.cluster_pair_jaccard, 1.0)
        x, y = (1.0, 1.0, -1.0, -1.0), (1.0, -1.0, 1.0, -1.0)
        symbols = ("S1", "S2", "S3", "S4", "S5")
        state = _state(symbols=symbols, values=lambda i: (x[i % 4], x[i % 4], y[i % 4], y[i % 4], y[i % 4]))
        for movers, expected in (((0.06, 0.06, 0.0, 0.0, 0.0), 1.0),
                                 ((0.06, 0.0, 0.06, 0.0, 0.0), 0.5)):
            evidence, _ = advance_correlation_cluster_diagnostics(
                _evaluation(60 * 60_000, symbols=symbols, five_minute_values=movers),
                CORRELATION_CONFIG_60M, state)
            self.assertEqual(evidence.max_network_component_mover_share, expected)
            self.assertEqual(evidence.max_cluster_mover_share, expected)
        tie, _ = advance_correlation_cluster_diagnostics(
            _evaluation(60 * 60_000, symbols=symbols,
                        five_minute_values=(0.06, -0.06, 0.0, 0.0, 0.0)),
            CORRELATION_CONFIG_60M, state)
        self.assertEqual(tie.dominant_material_side, "NONE")
        self.assertIsNone(tie.max_cluster_mover_share)

    def test_scope_reset_and_replay_prefix_partitions(self):
        state = _state()
        base = _evaluation(60 * 60_000)
        for identity in ({"universe_id": "u2"}, {"universe_version": "v2"},
                         {"provider": "other"}, {"exchange": "other"},
                         {"price_type": "other"}):
            evidence, next_state = advance_correlation_cluster_diagnostics(
                _evaluation(60 * 60_000, identity=identity), CORRELATION_CONFIG_60M, state)
            self.assertTrue(evidence.scope_reset_before_evaluation)
            self.assertEqual(evidence.status, CORRELATION_WARMING)
            self.assertEqual(len(next_state.rows), 1)
        reordered = tuple(reversed(SYMBOLS))
        evidence, _ = advance_correlation_cluster_diagnostics(
            _evaluation(60 * 60_000, symbols=reordered), CORRELATION_CONFIG_60M, state)
        self.assertTrue(evidence.scope_reset_before_evaluation)
        alternate_config = CORRELATION_CONFIGURATIONS[1]
        evidence, _ = advance_correlation_cluster_diagnostics(
            base, alternate_config, state)
        self.assertTrue(evidence.scope_reset_before_evaluation)
        evidence, _ = advance_correlation_cluster_diagnostics(
            _evaluation(60 * 60_000 + 60_000), CORRELATION_CONFIG_60M, state)
        self.assertTrue(evidence.scope_reset_before_evaluation)
        with self.assertRaises(ValueError):
            advance_correlation_cluster_diagnostics(
                _evaluation(60 * 60_000, identity={"movement_config_version": "other"}),
                CORRELATION_CONFIG_60M, state)
        self.assertEqual(base.algorithm_version, ALGORITHM_VERSION)
        symbols = ("S1", "S2", "S3", "S4", "S5")
        points = tuple(_point(i * 5_000,
                              partition="development" if i < 744 else "validation" if i < 756 else "test",
                              symbols=symbols,
                              values=((0.06 if (i // 12) % 2 == 0 else -0.06),) * 5)
                       for i in range(768))
        prefix = run_market_state_correlation_cluster_experiment(points[:756], CORRELATION_CONFIG_60M)
        full = run_market_state_correlation_cluster_experiment(points, CORRELATION_CONFIG_60M)
        self.assertEqual(prefix.paired_points, full.paired_points[:756])
        self.assertEqual(prefix.summaries["development"], full.summaries["development"])
        self.assertEqual(prefix.summaries["validation"], full.summaries["validation"])
        self.assertEqual(full.summaries["validation"].model_ready_count, 1)
        self.assertEqual(full.summaries["validation"].network_stability_comparison_count, 1)
        self.assertEqual(full.summaries["test"].evaluation_count, 12)


if __name__ == "__main__":
    unittest.main()
