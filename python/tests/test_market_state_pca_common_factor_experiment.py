"""Explicit EXP-75-07 PCA mathematics and causal replay fixtures."""

from dataclasses import replace
from decimal import Decimal
import math
import unittest

from market_analysis.experiments.market_state_common import (
    MarketStateExperimentPoint,
    advance_canonical_branch,
)
from market_analysis.experiments.market_state_pca_common_factor import (
    PCA_ALGORITHM_VERSION,
    PCA_CONFIG_60M,
    PCA_CONFIG_120M,
    PCA_CONFIGURATIONS,
    PCA_INCOMPLETE_CURRENT_ROW,
    PCA_MODEL_UNAVAILABLE,
    PCA_NOT_SCHEDULED,
    PCA_READY,
    PCA_WARMING,
    PCA_ZERO_CURRENT_ENERGY,
    PCA_ZERO_VARIANCE,
    PCAAlignedReturnRow,
    PCACommonFactorConfig,
    PCACommonFactorState,
    PairedMarketStatePCACommonFactorPoint,
    _fit_model,
    _jacobi_eigenpairs,
    _loading_similarity,
    _orient_loading,
    _pearson,
    _summary,
    advance_pca_common_factor,
    run_market_state_pca_common_factor_experiment,
)
from market_analysis.market_episode_lifecycle import MarketEpisodeLifecycleConfig
from market_analysis.movement_classifier import MarketClassifierConfig, SymbolSourceTimeEvidence
from market_analysis.movement_metrics import (
    ALGORITHM_VERSION,
    DEFAULT_CONFIG_VERSION,
    ExcludedSymbol,
    MarketMovementConfig,
    MarketMovementEvaluation,
    MarketMovementWindowResult,
    Metric,
    SymbolMovementResult,
    WINDOWS,
    _aggregates,
    _breadth,
    _outliers,
)


SYMBOLS = ("S1", "S2", "S3", "S4", "S5")
V1_CONFIG = MarketMovementConfig()
TOL = 1e-10


def _row(index, values, symbols=SYMBOLS):
    return PCAAlignedReturnRow(index * 60_000, tuple(zip(symbols, values)))


def _pattern_rows(config, symbols=SYMBOLS, values=None):
    return tuple(_row(index,
                      values(index) if values is not None else
                      (float((-1) ** index),) * len(symbols), symbols)
                 for index in range(config.lookback_rows))


def _state(config=PCA_CONFIG_60M, *, symbols=SYMBOLS, values=None):
    rows = _pattern_rows(config, symbols, values)
    boundary = config.lookback_rows * 60_000
    return PCACommonFactorState(
        PCA_ALGORITHM_VERSION, config.version, config.lookback_rows, 60_000,
        ALGORITHM_VERSION, DEFAULT_CONFIG_VERSION, "u1", "v1", symbols,
        "binance-usdm", "binance", "trade", boundary - 5_000, rows,
    )


def _symbol(symbol, boundary, minute, raw, excluded=False):
    z = 0.6745 * raw / 0.02
    direction = ("FLAT" if raw == 0 or abs(z) < V1_CONFIG.flat_z
                 else "RISING" if raw > 0 else "FALLING")
    return SymbolMovementResult(
        symbol=symbol, instrument_id=symbol,
        provider="binance-usdm", exchange="binance", price_type="trade",
        window_minutes=minute, evaluation_boundary_time_ms=boundary,
        included=not excluded,
        exclusion_reasons=("TEST_EXCLUDED",) if excluded else (),
        current_return=Metric.present(raw), previous_return=Metric.present(0.01),
        velocity=Metric.present(raw / (minute * 60)),
        previous_velocity=Metric.present(0.001),
        acceleration=Metric.present(0.001),
        historical_median=Metric.present(0.0), historical_mad=Metric.present(0.02),
        normalized_z=Metric.missing("TEST_EXCLUDED") if excluded else Metric.present(z),
        direction=Metric.missing("TEST_EXCLUDED") if excluded else Metric.present(direction),
        material_rising=not excluded and raw > 0 and abs(z) >= V1_CONFIG.material_z,
        material_falling=not excluded and raw < 0 and abs(z) >= V1_CONFIG.material_z,
        current_notional_volume=Metric.present(Decimal("10")),
        rvol=Metric.present(1.0),
        cross_sectional_z=Metric.missing("CROSS_SECTIONAL_MAD_UNAVAILABLE"),
        outlier_candidate=False,
    )


def _evaluation(boundary, values=None, *, symbols=SYMBOLS, excluded_1m=(), identity=None):
    identity = identity or {}
    values = values if values is not None else (0.06,) * len(symbols)
    windows = {}
    for minute in WINDOWS:
        excluded = set(excluded_1m if minute == 1 else ())
        results = tuple(_symbol(symbol, boundary, minute, raw, symbol in excluded)
                        for symbol, raw in zip(symbols, values))
        included = tuple(item for item in results if item.included)
        eligible = len(included) >= 5 and len(included) / len(symbols) >= 0.6
        if eligible:
            results = _outliers(results, V1_CONFIG)
            included = tuple(item for item in results if item.included)
        else:
            results = tuple(replace(
                item, cross_sectional_z=Metric.missing("MARKET_UNIVERSE_INELIGIBLE"),
                outlier_candidate=False,
            ) for item in results)
        windows[minute] = MarketMovementWindowResult(
            algorithm_version=identity.get("movement_algorithm_version", ALGORITHM_VERSION),
            config_version=identity.get("movement_config_version", DEFAULT_CONFIG_VERSION),
            universe_id=identity.get("universe_id", "u1"),
            universe_version=identity.get("universe_version", "v1"),
            configured_universe=symbols,
            included_symbols=tuple(item.symbol for item in included),
            excluded_symbols=tuple(ExcludedSymbol(symbol, ("TEST_EXCLUDED",))
                                   for symbol in symbols if symbol in excluded),
            window_minutes=minute,
            provider=identity.get("provider", "binance-usdm"),
            exchange=identity.get("exchange", "binance"),
            price_type=identity.get("price_type", "trade"),
            evaluation_boundary_time_ms=boundary,
            historical_lookback_ms=100, minimum_historical_coverage_ms=50,
            market_wide_eligible=eligible,
            eligible_count=len(included), eligible_fraction=len(included) / len(symbols),
            symbols=results, breadth=_breadth(included, eligible),
            aggregates=_aggregates(included, eligible, V1_CONFIG),
        )
    return MarketMovementEvaluation(
        algorithm_version=identity.get("movement_algorithm_version", ALGORITHM_VERSION),
        config_version=identity.get("movement_config_version", DEFAULT_CONFIG_VERSION),
        universe_id=identity.get("universe_id", "u1"),
        universe_version=identity.get("universe_version", "v1"),
        configured_universe=symbols,
        provider=identity.get("provider", "binance-usdm"),
        exchange=identity.get("exchange", "binance"),
        price_type=identity.get("price_type", "trade"),
        evaluation_boundary_time_ms=boundary,
        historical_lookback_ms=100, minimum_historical_coverage_ms=50,
        windows=windows,
    )


def _point(boundary, partition="development", **kwargs):
    evaluation = _evaluation(boundary, **kwargs)
    source = tuple(SymbolSourceTimeEvidence(symbol, None, None, None)
                   for symbol in evaluation.configured_universe)
    return MarketStateExperimentPoint(evaluation, source, partition)


class PCACommonFactorExperimentTests(unittest.TestCase):
    def test_fixed_config_identity_rejects_other_models(self):
        self.assertEqual(tuple(config.lookback_rows for config in PCA_CONFIGURATIONS),
                         (60, 120, 240))
        for config in PCA_CONFIGURATIONS:
            for token in ("aligned-canonical-1m-returns", "matrix-correlation",
                          "population-std", "strict-prior-true", "sample-60000ms",
                          f"rows-{config.lookback_rows}", "deterministic-jacobi"):
                self.assertIn(token, config.version)
        for changes in (
            {"version": "custom", "lookback_rows": 60},
            {"version": PCA_CONFIG_60M.version, "lookback_rows": 90},
            {"version": PCA_CONFIG_60M.version, "lookback_rows": 60,
             "matrix": "covariance"},
            {"version": PCA_CONFIG_60M.version, "lookback_rows": 60,
             "strict_prior": False},
            {"version": PCA_CONFIG_60M.version, "lookback_rows": 60,
             "sample_interval_ms": 5_000},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                PCACommonFactorConfig(**changes)

    def test_jacobi_identity_ones_and_nontrivial_psd_spectrum(self):
        matrices_and_expected = (
            (((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
             (1.0, 1.0, 1.0)),
            (((1.0, 1.0, 1.0), (1.0, 1.0, 1.0), (1.0, 1.0, 1.0)),
             (3.0, 0.0, 0.0)),
            (((2.0, 1.0), (1.0, 2.0)), (3.0, 1.0)),
        )
        for matrix, expected in matrices_and_expected:
            with self.subTest(matrix=matrix):
                eigenvalues, eigenvectors = _jacobi_eigenpairs(matrix)
                self.assertEqual(len(eigenvalues), len(expected))
                for actual, reference in zip(eigenvalues, expected):
                    self.assertAlmostEqual(actual, reference, delta=TOL)
                self.assertEqual(tuple(eigenvalues), tuple(sorted(eigenvalues, reverse=True)))
                self.assertAlmostEqual(sum(eigenvalues),
                                       sum(matrix[i][i] for i in range(len(matrix))), delta=TOL)
                for index, vector in enumerate(eigenvectors):
                    self.assertAlmostEqual(sum(value * value for value in vector), 1.0,
                                           delta=TOL)
                    for row in range(len(matrix)):
                        left = sum(matrix[row][col] * vector[col]
                                   for col in range(len(matrix)))
                        self.assertAlmostEqual(left, eigenvalues[index] * vector[row],
                                               delta=TOL)
                    for other in eigenvectors[:index]:
                        self.assertAlmostEqual(sum(a * b for a, b in zip(vector, other)),
                                               0.0, delta=TOL)

    def test_perfect_common_factor_loadings_and_projection(self):
        state = _state()
        boundary = 60 * 60_000
        model, reason = _fit_model(state.rows, SYMBOLS)
        self.assertIsNone(reason)
        self.assertEqual(model.correlation_matrix,
                         tuple((1.0,) * len(SYMBOLS) for _ in SYMBOLS))
        self.assertAlmostEqual(model.leading_eigenvalue, 5.0, delta=TOL)
        self.assertTrue(all(abs(value) < TOL for value in model.eigenvalues[1:]))
        self.assertAlmostEqual(model.explained_variance_ratio, 1.0, delta=TOL)
        self.assertAlmostEqual(model.loading_coherence_fraction, 1.0, delta=TOL)
        for _, value in model.pc1_loadings:
            self.assertAlmostEqual(abs(value), 1 / math.sqrt(5), delta=TOL)
        evidence, after = advance_pca_common_factor(
            _evaluation(boundary, values=(2.0,) * 5), PCA_CONFIG_60M, state)
        self.assertEqual(evidence.status, PCA_READY)
        self.assertTrue(evidence.current_projection_available)
        self.assertAlmostEqual(evidence.factor_score, 2 * math.sqrt(5), delta=TOL)
        self.assertAlmostEqual(evidence.current_pc1_energy_fraction, 1.0, delta=TOL)
        self.assertEqual(after.rows[-1].returns_by_symbol, tuple((symbol, 2.0)
                                                               for symbol in SYMBOLS))

    def test_opposite_groups_have_rank_one_but_split_loading_signs(self):
        symbols = ("S1", "S2", "S3", "S4")
        rows = _pattern_rows(PCA_CONFIG_60M, symbols,
                             lambda index: ((-1.0) ** index,) * 2 +
                             (-((-1.0) ** index),) * 2)
        model, reason = _fit_model(rows, symbols)
        self.assertIsNone(reason)
        self.assertAlmostEqual(model.explained_variance_ratio, 1.0, delta=TOL)
        self.assertAlmostEqual(model.loading_coherence_fraction, 0.5, delta=TOL)
        loadings = tuple(value for _, value in model.pc1_loadings)
        self.assertEqual(sum(value > 0 for value in loadings), 2)
        self.assertEqual(sum(value < 0 for value in loadings), 2)

    def test_identity_correlation_has_unidentified_pc1(self):
        symbols = ("S1", "S2", "S3")
        pattern = ((1.0, 1.0, 1.0), (1.0, -1.0, -1.0),
                   (-1.0, 1.0, -1.0), (-1.0, -1.0, 1.0))
        rows = _pattern_rows(PCA_CONFIG_60M, symbols,
                             lambda index: pattern[index % len(pattern)])
        model, reason = _fit_model(rows, symbols)
        self.assertIsNone(reason)
        self.assertEqual(model.correlation_matrix,
                         ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))
        self.assertEqual(model.eigenvalues, (1.0, 1.0, 1.0))
        self.assertEqual(model.eigen_gap, 0.0)
        self.assertAlmostEqual(model.explained_variance_ratio, 1 / 3, delta=TOL)
        self.assertFalse(model.loadings_identified)
        self.assertIsNone(model.pc1_loadings)
        state = _state(PCA_CONFIG_60M, symbols=symbols,
                       values=lambda index: pattern[index % len(pattern)])
        evidence, _ = advance_pca_common_factor(
            _evaluation(60 * 60_000, symbols=symbols, values=pattern[0]),
            PCA_CONFIG_60M, state)
        self.assertTrue(evidence.model_available)
        self.assertFalse(evidence.current_projection_available)
        self.assertIsNone(evidence.factor_score)

    def test_orthogonal_current_vector_has_zero_pc1_energy(self):
        state = _state()
        boundary = 60 * 60_000
        evidence, _ = advance_pca_common_factor(
            _evaluation(boundary, values=(2.0, -2.0, 0.0, 0.0, 0.0)),
            PCA_CONFIG_60M, state)
        self.assertAlmostEqual(evidence.factor_score, 0.0, delta=TOL)
        self.assertAlmostEqual(evidence.current_pc1_energy_fraction, 0.0, delta=TOL)

    def test_zero_current_energy_does_not_fabricate_energy_fraction(self):
        evidence, _ = advance_pca_common_factor(
            _evaluation(60 * 60_000, values=(0.0,) * 5), PCA_CONFIG_60M, _state())
        self.assertEqual(evidence.status, PCA_READY)
        self.assertTrue(evidence.current_projection_available)
        self.assertEqual(evidence.factor_score, 0.0)
        self.assertIsNone(evidence.current_pc1_energy_fraction)
        self.assertEqual(evidence.projection_reason, PCA_ZERO_CURRENT_ENERGY)

    def test_current_shock_changes_projection_but_not_prior_model(self):
        state = _state()
        boundary = 60 * 60_000
        ordinary, after_ordinary = advance_pca_common_factor(
            _evaluation(boundary, values=(2.0,) * 5), PCA_CONFIG_60M, state)
        shock, after_shock = advance_pca_common_factor(
            _evaluation(boundary, values=(1000.0, -1000.0, 0.0, 0.0, 0.0)),
            PCA_CONFIG_60M, state)
        self.assertEqual(ordinary.model.prior_means, shock.model.prior_means)
        self.assertEqual(ordinary.model.prior_population_stds,
                         shock.model.prior_population_stds)
        self.assertEqual(ordinary.model.correlation_matrix,
                         shock.model.correlation_matrix)
        self.assertEqual(ordinary.model.eigenvalues, shock.model.eigenvalues)
        self.assertEqual(ordinary.model.pc1_loadings, shock.model.pc1_loadings)
        self.assertEqual(ordinary.explained_variance_ratio,
                         shock.explained_variance_ratio)
        self.assertNotEqual(ordinary.current_pc1_energy_fraction,
                            shock.current_pc1_energy_fraction)
        self.assertEqual(after_ordinary.rows[-1].returns_by_symbol[0][1], 2.0)
        self.assertEqual(after_shock.rows[-1].returns_by_symbol[0][1], 1000.0)

    def test_zero_variance_is_model_unavailable_without_dropping_symbol(self):
        state = _state(values=lambda index: (1.0, float((-1) ** index),
                                             float((-1) ** index),
                                             float((-1) ** index),
                                             float((-1) ** index)))
        evidence, after = advance_pca_common_factor(
            _evaluation(60 * 60_000), PCA_CONFIG_60M, state)
        self.assertEqual(evidence.status, PCA_MODEL_UNAVAILABLE)
        self.assertEqual(evidence.status_reason, PCA_ZERO_VARIANCE)
        self.assertIsNone(evidence.explained_variance_ratio)
        self.assertEqual(len(after.rows), 60)

    def test_orient_sign_anchor_ties_and_similarity(self):
        self.assertEqual(_orient_loading((0.5, -0.5, 0.25)),
                         _orient_loading((-0.5, 0.5, -0.25)))
        self.assertEqual(_orient_loading((-0.5, 0.5, 0.25)),
                         (0.5, -0.5, -0.25))
        self.assertAlmostEqual(_loading_similarity((1.0, 0.0), (1.0, 0.0)),
                               1.0, delta=TOL)
        self.assertAlmostEqual(_loading_similarity((1.0, 0.0), (0.0, 1.0)),
                               0.0, delta=TOL)

    def test_minute_sampling_and_non_aligned_status(self):
        state = None
        scheduled = []
        for boundary in range(0, 125_000, 5_000):
            evidence, state = advance_pca_common_factor(
                _evaluation(boundary), PCA_CONFIG_60M, state)
            if boundary % 60_000:
                self.assertEqual(evidence.status, PCA_NOT_SCHEDULED)
                self.assertFalse(evidence.current_row_appended_after_evaluation)
            else:
                scheduled.append(boundary)
                self.assertEqual(evidence.status, PCA_WARMING)
        self.assertEqual(tuple(scheduled), (0, 60_000, 120_000))
        self.assertEqual(tuple(row.evaluation_boundary_time_ms for row in state.rows),
                         (0, 60_000, 120_000))

    def test_strict_prior_warming_for_all_configurations(self):
        for config in PCA_CONFIGURATIONS:
            with self.subTest(lookback=config.lookback_rows):
                current = config.lookback_rows * 60_000
                full = _state(config)
                short = replace(full, rows=full.rows[1:])
                warming, state = advance_pca_common_factor(
                    _evaluation(current), config, short)
                self.assertEqual(warming.status, PCA_WARMING)
                self.assertEqual(warming.prior_row_count, config.lookback_rows - 1)
                self.assertEqual(len(state.rows), config.lookback_rows)
                for boundary in range(current + 5_000, current + 60_000, 5_000):
                    skipped, state = advance_pca_common_factor(
                        _evaluation(boundary), config, state)
                    self.assertEqual(skipped.status, PCA_NOT_SCHEDULED)
                ready, _ = advance_pca_common_factor(
                    _evaluation(current + 60_000), config, state)
                self.assertEqual(ready.status, PCA_READY)

    def test_missing_aligned_row_keeps_current_model_then_clears_all_history(self):
        state = _state()
        boundary = 60 * 60_000
        evidence, after = advance_pca_common_factor(
            _evaluation(boundary, excluded_1m=("S1",)), PCA_CONFIG_60M, state)
        self.assertEqual(evidence.status, PCA_READY)
        self.assertTrue(evidence.model_available)
        self.assertFalse(evidence.current_row_complete)
        self.assertFalse(evidence.current_projection_available)
        self.assertEqual(evidence.projection_reason, PCA_INCOMPLETE_CURRENT_ROW)
        self.assertFalse(evidence.current_row_appended_after_evaluation)
        self.assertTrue(evidence.history_reset_after_evaluation)
        self.assertEqual(after.rows, ())
        after = replace(after, last_evaluation_boundary_time_ms=boundary + 55_000)
        next_evidence, next_state = advance_pca_common_factor(
            _evaluation(boundary + 60_000), PCA_CONFIG_60M, after, evidence)
        self.assertEqual(next_evidence.status, PCA_WARMING)
        self.assertIsNone(next_evidence.loading_similarity)
        self.assertEqual(len(next_state.rows), 1)

    def test_scope_and_gap_resets_do_not_use_old_rows(self):
        state = _state()
        boundary = 60 * 60_000
        changes = (
            {"baseline_movement_algorithm_version": "old-movement"},
            {"baseline_movement_config_version": "old-config"},
            {"universe_id": "old-universe"},
            {"universe_version": "old-version"},
            {"provider": "old-provider"},
            {"exchange": "old-exchange"},
            {"price_type": "old-price"},
        )
        for change in changes:
            with self.subTest(change=change):
                evidence, after = advance_pca_common_factor(
                    _evaluation(boundary), PCA_CONFIG_60M, replace(state, **change))
                self.assertEqual(evidence.status, PCA_WARMING)
                self.assertTrue(evidence.scope_reset_before_evaluation)
                self.assertEqual(len(after.rows), 1)
        reversed_symbols = tuple(reversed(SYMBOLS))
        reversed_rows = tuple(PCAAlignedReturnRow(
            row.evaluation_boundary_time_ms,
            tuple(reversed(row.returns_by_symbol))) for row in state.rows)
        swapped = replace(state, configured_universe=reversed_symbols, rows=reversed_rows)
        evidence, _ = advance_pca_common_factor(_evaluation(boundary),
                                                PCA_CONFIG_60M, swapped)
        self.assertEqual(evidence.status, PCA_WARMING)
        for config, current in ((PCA_CONFIG_120M, boundary),
                                (PCA_CONFIG_60M, boundary + 10_000)):
            evidence, after = advance_pca_common_factor(
                _evaluation(current), config, state)
            self.assertEqual(evidence.prior_row_count, 0)
            self.assertEqual(evidence.status,
                             PCA_WARMING if current % 60_000 == 0 else PCA_NOT_SCHEDULED)
            self.assertEqual(len(after.rows), 1 if current % 60_000 == 0 else 0)
        for identity in ({"movement_algorithm_version": "unknown"},
                         {"movement_config_version": "unknown"}):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                advance_pca_common_factor(_evaluation(boundary, identity=identity),
                                          PCA_CONFIG_60M, state)

    def test_state_rejects_malformed_rows_order_and_history(self):
        with self.assertRaises(ValueError):
            PCAAlignedReturnRow(5_000, (("S1", 1.0),))
        with self.assertRaises(ValueError):
            PCAAlignedReturnRow(0, (("S1", float("nan")),))
        state = _state()
        with self.assertRaises(ValueError):
            replace(state, rows=tuple(reversed(state.rows)))
        with self.assertRaises(ValueError):
            replace(state, rows=(PCAAlignedReturnRow(0, (("S1", 1.0),)),))
        with self.assertRaises(ValueError):
            replace(state, rows=state.rows[:-1])

    def test_loading_similarity_across_scheduled_ready_points_only(self):
        state = _state()
        current = 60 * 60_000
        first, state = advance_pca_common_factor(_evaluation(current), PCA_CONFIG_60M, state)
        for boundary in range(current + 5_000, current + 60_000, 5_000):
            _, state = advance_pca_common_factor(_evaluation(boundary), PCA_CONFIG_60M, state,
                                                 first)
        second, _ = advance_pca_common_factor(
            _evaluation(current + 60_000), PCA_CONFIG_60M, state, first)
        self.assertEqual(second.status, PCA_READY)
        self.assertAlmostEqual(second.loading_similarity, 1.0, delta=TOL)

    def test_pearson_is_descriptive_and_unavailable_with_zero_variance(self):
        self.assertAlmostEqual(_pearson(((1.0, 2.0), (2.0, 4.0), (3.0, 6.0))),
                               1.0, delta=TOL)
        self.assertAlmostEqual(_pearson(((1.0, 6.0), (2.0, 4.0), (3.0, 2.0))),
                               -1.0, delta=TOL)
        self.assertIsNone(_pearson(((1.0, 2.0),)))
        self.assertIsNone(_pearson(((1.0, 2.0), (1.0, 3.0))))

    def test_summary_groups_loading_and_breadth_without_candidate_branch(self):
        state = _state()
        boundary = 60 * 60_000
        point = _point(boundary)
        evidence, after = advance_pca_common_factor(
            point.movement_evaluation, PCA_CONFIG_60M, state)
        classification, lifecycle = advance_canonical_branch(
            point.movement_evaluation, point.source_time_evidence, None,
            MarketClassifierConfig(), MarketEpisodeLifecycleConfig())
        paired = PairedMarketStatePCACommonFactorPoint(
            boundary, "development", point.movement_evaluation,
            classification, lifecycle.next_state, lifecycle.transitions,
            evidence, after)
        summary = _summary((paired,), "all")
        self.assertEqual(summary.pca_model_ready_count, 1)
        self.assertEqual(summary.pca_projection_available_count, 1)
        self.assertEqual(summary.breadth_comparison_count, 1)
        self.assertIsNone(summary.pearson_explained_variance_vs_directional_breadth)
        self.assertEqual(summary.baseline_state_pca_diagnostics[0].group, "BROAD_RISE")
        self.assertEqual(summary.baseline_state_pca_diagnostics[0].model_ready_count, 1)
        self.assertEqual(summary.baseline_inactive_pca_diagnostics.model_ready_count, 1)
        self.assertEqual(tuple(item.symbol for item in summary.factor_loading_diagnostics),
                         SYMBOLS)
        self.assertTrue(all(item.comparable_count == 1
                            for item in summary.factor_loading_diagnostics))
        self.assertFalse(hasattr(paired, "candidate_classification"))

    def test_runner_prefix_and_partition_summaries_are_causal(self):
        points = tuple(_point(
            index * 5_000,
            "development" if index < 12 else "validation" if index < 24 else "test",
        ) for index in range(30))
        full = run_market_state_pca_common_factor_experiment(points, PCA_CONFIG_60M)
        repeat = run_market_state_pca_common_factor_experiment(points, PCA_CONFIG_60M)
        dev = run_market_state_pca_common_factor_experiment(points[:12], PCA_CONFIG_60M)
        validation = run_market_state_pca_common_factor_experiment(points[:24],
                                                                   PCA_CONFIG_60M)
        self.assertEqual(full.paired_points, repeat.paired_points)
        self.assertEqual(dict(full.summaries), dict(repeat.summaries))
        self.assertEqual(full.paired_points[:12], dev.paired_points)
        self.assertEqual(full.paired_points[:24], validation.paired_points)
        self.assertEqual(full.summaries["development"], dev.summaries["development"])
        self.assertEqual(full.summaries["validation"], validation.summaries["validation"])
        self.assertEqual(full.summaries["test"].evaluation_count, 6)
        self.assertIs(full.paired_points[0].baseline_evaluation, points[0].movement_evaluation)
        self.assertEqual(full.summaries["all"].pca_scheduled_count, 3)
        self.assertEqual(full.summaries["all"].pca_not_scheduled_count, 27)
        self.assertEqual(full.summaries["all"].pca_model_ready_count, 0)
        for bad in ((points[0], points[0]), (points[1], points[0]),
                    (points[0], points[2]), (points[24], points[0])):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                run_market_state_pca_common_factor_experiment(bad, PCA_CONFIG_60M)


if __name__ == "__main__":
    unittest.main()
