"""Fixed Gaussian HMM math, training leakage, and causal replay fixtures."""

from dataclasses import replace
from decimal import Decimal
import math
from types import SimpleNamespace
import unittest

from market_analysis.experiments.market_state_common import MarketStateExperimentPoint
from market_analysis.experiments.market_state_hmm_regimes import (
    HMM_CONFIG_V1, HMM_EM_ITERATIONS, HMM_FEATURE_UNAVAILABLE,
    HMM_INSUFFICIENT_TRAINING_ROWS, HMM_INSUFFICIENT_TRAINING_TRANSITIONS,
    HMM_NOT_SCHEDULED, HMM_READY, HMM_STATE_NAMES, HMM_TRAINING_PARTITION,
    HMM_TRAINING_ZERO_VARIANCE, HMM_VARIANCE_FLOOR,
    HMMDevelopmentTrainingBlock, HMMFeatureRow, HMMFilterState, HMMRegimeConfig, HMMTrainingDiagnostics,
    _canonicalize, _development_blocks, _extract_feature_row, _filter_observation,
    _gaussian_log_density, _logsumexp, _occupancy_tv, _standardize_blocks,
    _summary, _train_from_blocks, _training_fingerprint,
    advance_hmm_regime_filter, run_market_state_hmm_experiment,
    train_hmm_regime_model, train_hmm_regime_model_from_blocks,
    train_hmm_regime_model_from_feature_blocks,
)
from market_analysis.movement_classifier import SymbolSourceTimeEvidence
from market_analysis.movement_metrics import (
    ALGORITHM_VERSION, DEFAULT_CONFIG_VERSION, MarketMovementConfig,
    MarketMovementEvaluation, MarketMovementWindowResult, Metric, SymbolMovementResult,
    WINDOWS, _aggregates, _breadth,
)


SYMBOLS = ("S1", "S2", "S3", "S4", "S5")
V1_CONFIG = MarketMovementConfig()
SCOPE = (ALGORITHM_VERSION, DEFAULT_CONFIG_VERSION, "u1", "v1", SYMBOLS,
         "binance-usdm", "binance", "trade")


def _synthetic_blocks():
    blocks = []
    for group, center in enumerate((-2.0, 0.0, 2.0)):
        block = []
        for index in range(80):
            phase = index % 7
            values = (center + 0.04 * (phase - 3),
                      center / 3 + 0.02 * ((index * 3) % 11),
                      0.5 + 0.03 * ((index + group) % 5),
                      1.0 + 0.02 * ((index * 2 + group) % 9))
            block.append(HMMFeatureRow((group * 81 + index) * 60_000, values))
        blocks.append(tuple(block))
    return tuple(blocks)


def _symbol(symbol, boundary, minute, z, rvol=1.0, missing_rvol=False):
    raw = z * 0.01
    direction = "FLAT" if abs(z) < V1_CONFIG.flat_z else "RISING" if z > 0 else "FALLING"
    return SymbolMovementResult(
        symbol=symbol, instrument_id=symbol, provider="binance-usdm", exchange="binance",
        price_type="trade", window_minutes=minute, evaluation_boundary_time_ms=boundary,
        included=True, exclusion_reasons=(), current_return=Metric.present(raw),
        previous_return=Metric.present(0.0), velocity=Metric.present(raw / (minute * 60)),
        previous_velocity=Metric.present(0.0), acceleration=Metric.present(0.0),
        historical_median=Metric.present(0.0), historical_mad=Metric.present(0.02),
        normalized_z=Metric.present(z), direction=Metric.present(direction),
        material_rising=z >= V1_CONFIG.material_z,
        material_falling=z <= -V1_CONFIG.material_z,
        current_notional_volume=Metric.present(Decimal("10")),
        rvol=Metric.missing("TEST_MISSING_RVOL") if missing_rvol else Metric.present(rvol),
        cross_sectional_z=Metric.missing("CROSS_SECTIONAL_MAD_UNAVAILABLE"),
        outlier_candidate=False,
    )


def _evaluation(boundary, *, minute_index=None, missing_rvol=False, shift=0.0,
                identity=None):
    identity = identity or {}
    minute_index = boundary // 60_000 if minute_index is None else minute_index
    group = (minute_index // 80) % 3
    center = (-2.0, 0.0, 2.0)[group] + 0.025 * ((minute_index * 3) % 7 - 3) + shift
    spread = 0.3 + 0.04 * (minute_index % 5)
    zs = tuple(center + spread * offset for offset in (-2, -1, 0, 1, 2))
    windows = {}
    for minute in WINDOWS:
        symbols = tuple(_symbol(name, boundary, minute, z,
                                rvol=1.0 + 0.03 * (minute_index % 9) + 0.01 * position,
                                missing_rvol=missing_rvol and position == 2)
                        for position, (name, z) in enumerate(zip(SYMBOLS, zs)))
        windows[minute] = MarketMovementWindowResult(
            algorithm_version=identity.get("algorithm", ALGORITHM_VERSION),
            config_version=identity.get("config", DEFAULT_CONFIG_VERSION),
            universe_id=identity.get("universe_id", "u1"),
            universe_version=identity.get("universe_version", "v1"),
            configured_universe=SYMBOLS, included_symbols=SYMBOLS,
            excluded_symbols=(), window_minutes=minute,
            provider=identity.get("provider", "binance-usdm"),
            exchange=identity.get("exchange", "binance"),
            price_type=identity.get("price_type", "trade"),
            evaluation_boundary_time_ms=boundary, historical_lookback_ms=100,
            minimum_historical_coverage_ms=50, market_wide_eligible=True,
            eligible_count=5, eligible_fraction=1.0, symbols=symbols,
            breadth=_breadth(symbols, True), aggregates=_aggregates(symbols, True, V1_CONFIG),
        )
    return MarketMovementEvaluation(
        algorithm_version=identity.get("algorithm", ALGORITHM_VERSION),
        config_version=identity.get("config", DEFAULT_CONFIG_VERSION),
        universe_id=identity.get("universe_id", "u1"),
        universe_version=identity.get("universe_version", "v1"),
        configured_universe=SYMBOLS, provider=identity.get("provider", "binance-usdm"),
        exchange=identity.get("exchange", "binance"),
        price_type=identity.get("price_type", "trade"),
        evaluation_boundary_time_ms=boundary, historical_lookback_ms=100,
        minimum_historical_coverage_ms=50, windows=windows,
    )


def _point(boundary, partition, **kwargs):
    evaluation = _evaluation(boundary, **kwargs)
    source = tuple(SymbolSourceTimeEvidence(symbol, None, None, None) for symbol in SYMBOLS)
    return MarketStateExperimentPoint(evaluation, source, partition)


def _replay_points(*, validation_shift=0.0, test_shift=0.0,
                   missing_validation_minute=None, missing_test_minute=None):
    points = []
    for tick in range(12 * 244):
        minute = tick // 12
        partition = "development" if minute < 240 else "validation" if minute < 242 else "test"
        shift = 0.0 if partition == "development" else validation_shift if partition == "validation" else test_shift
        missing = (tick % 12 == 0 and
                   ((partition == "validation" and minute == missing_validation_minute)
                    or (partition == "test" and minute == missing_test_minute)))
        points.append(_point(tick * 5_000, partition, shift=shift, missing_rvol=missing))
    return tuple(points)


class GaussianHMMMathTests(unittest.TestCase):
    def test_exact_primary_five_minute_feature_schema(self):
        row = _extract_feature_row(_evaluation(0))
        self.assertIsNotNone(row)
        for actual, expected in zip(row.values, (-2.075, -1.0, 0.3, 1.02)):
            self.assertAlmostEqual(actual, expected)
        self.assertIsNone(_extract_feature_row(_evaluation(0, missing_rvol=True)))
        self.assertIsNone(_extract_feature_row(_evaluation(5_000)))

    def test_fixed_config_and_exact_population_standardization(self):
        with self.assertRaises(ValueError):
            HMMRegimeConfig(state_count=4)
        rows = ((HMMFeatureRow(0, (1, 2, 3, 4)),
                 HMMFeatureRow(60_000, (3, 4, 5, 6))),)
        (means, stds, standardized), reason = _standardize_blocks(rows)
        self.assertIsNone(reason)
        self.assertEqual(means, (2, 3, 4, 5))
        self.assertEqual(stds, (1, 1, 1, 1))
        self.assertEqual(standardized, (((-1, -1, -1, -1), (1, 1, 1, 1)),))

    def test_gaussian_density_and_stable_logsumexp(self):
        expected = -2 * math.log(2 * math.pi)
        self.assertAlmostEqual(_gaussian_log_density((0, 0, 0, 0), (0, 0, 0, 0),
                                                     (1, 1, 1, 1)), expected)
        self.assertAlmostEqual(_logsumexp((-1000.0, -1000.0)), -1000 + math.log(2))
        self.assertAlmostEqual(_logsumexp((math.log(2), math.log(3))), math.log(5))

    def test_forward_filter_equal_emissions_and_reset(self):
        transition = ((0.8, 0.1, 0.1), (0.2, 0.7, 0.1), (0.1, 0.2, 0.7))
        model = SimpleNamespace(
            feature_means=(0.0,) * 4, feature_population_stds=(1.0,) * 4,
            pi=(0.2, 0.3, 0.5), transition_matrix=transition,
            emission_means=((0.0,) * 4,) * 3, emission_variances=((1.0,) * 4,) * 3,
        )
        previous = HMMFilterState(0, (0.6, 0.3, 0.1))
        row = HMMFeatureRow(60_000, (0, 0, 0, 0))
        standardized, reset, predicted, posterior, hard, confidence, entropy, likelihood = _filter_observation(row, model, previous)
        expected = (0.55, 0.29, 0.16)
        self.assertFalse(reset)
        self.assertEqual(standardized, (0, 0, 0, 0))
        for actual, target in zip(predicted, expected):
            self.assertAlmostEqual(actual, target)
        for actual, target in zip(posterior, expected):
            self.assertAlmostEqual(actual, target)
        self.assertEqual(hard, HMM_STATE_NAMES[0])
        self.assertAlmostEqual(confidence, 0.55)
        self.assertAlmostEqual(likelihood, -2 * math.log(2 * math.pi))
        self.assertGreater(entropy, 0)
        self.assertTrue(_filter_observation(HMMFeatureRow(120_000, (0, 0, 0, 0)), model, None)[1])

    def test_canonical_state_permutation(self):
        pi = (0.2, 0.5, 0.3)
        transition = ((0.7, 0.2, 0.1), (0.1, 0.8, 0.1), (0.2, 0.3, 0.5))
        means = ((2.0, 0.0, 0.0, 0.0), (-2.0, 0.0, 0.0, 0.0),
                 (0.0, 0.0, 0.0, 0.0))
        variances = ((2.0,) * 4, (3.0,) * 4, (4.0,) * 4)
        new_pi, new_transition, new_means, new_variances = _canonicalize(
            pi, transition, means, variances)
        self.assertEqual(new_pi, (0.5, 0.3, 0.2))
        self.assertEqual(tuple(row[0] for row in new_means), (-2.0, 0.0, 2.0))
        self.assertEqual(new_variances, (variances[1], variances[2], variances[0]))
        self.assertEqual(new_transition[0], (0.8, 0.1, 0.1))

    def test_training_blocks_thresholds_and_fingerprint(self):
        rows = [HMMFeatureRow(i * 60_000, (float(i), 1, 2, 3)) for i in (0, 1, 3, 4)]
        points = tuple(SimpleNamespace(partition="development",
                                       movement_evaluation=SimpleNamespace(evaluation_boundary_time_ms=i * 60_000))
                       for i in range(5))
        blocks, unavailable = _development_blocks(points, (rows[0], rows[1], None, rows[2], rows[3]))
        self.assertEqual((len(blocks), unavailable), (2, 1))
        self.assertEqual(sum(len(block) - 1 for block in blocks), 2)
        self.assertEqual(_train_from_blocks((rows[:2],), 0, SCOPE, HMM_CONFIG_V1)[0].reason,
                         HMM_INSUFFICIENT_TRAINING_ROWS)
        singleton_blocks = tuple((HMMFeatureRow(i * 120_000, (float(i), 1, 2, 3)),)
                                 for i in range(240))
        self.assertEqual(_train_from_blocks(singleton_blocks, 0, SCOPE, HMM_CONFIG_V1)[0].reason,
                         HMM_INSUFFICIENT_TRAINING_TRANSITIONS)
        constant = (tuple(HMMFeatureRow(i * 60_000, (1, 2, 3, 4)) for i in range(240)),)
        self.assertEqual(_train_from_blocks(constant, 0, SCOPE, HMM_CONFIG_V1)[0].reason,
                         HMM_TRAINING_ZERO_VARIANCE)
        first_hash = _training_fingerprint(blocks, SCOPE, HMM_CONFIG_V1)
        changed = ((replace(blocks[0][0], values=(99.0, 1.0, 2.0, 3.0)), blocks[0][1]), blocks[1])
        self.assertNotEqual(first_hash, _training_fingerprint(changed, SCOPE, HMM_CONFIG_V1))
        self.assertEqual(first_hash, _training_fingerprint(blocks, SCOPE, HMM_CONFIG_V1))

    def test_deterministic_three_regime_training_and_artifact_validation(self):
        blocks = _synthetic_blocks()
        first_diagnostics, first = _train_from_blocks(blocks, 0, SCOPE, HMM_CONFIG_V1)
        second_diagnostics, second = _train_from_blocks(blocks, 0, SCOPE, HMM_CONFIG_V1)
        self.assertEqual(first_diagnostics, second_diagnostics)
        self.assertEqual(first, second)
        self.assertIsNotNone(first)
        self.assertEqual(first.em_iteration_count, HMM_EM_ITERATIONS)
        self.assertLessEqual(first.emission_means[0][0], first.emission_means[1][0])
        self.assertLessEqual(first.emission_means[1][0], first.emission_means[2][0])
        self.assertTrue(all(math.isfinite(value) and value >= HMM_VARIANCE_FLOOR
                            for row in first.emission_variances for value in row))
        self.assertAlmostEqual(sum(first.pi), 1.0)
        for row in first.transition_matrix:
            self.assertAlmostEqual(sum(row), 1.0)
        self.assertGreaterEqual(first.final_development_log_likelihood,
                                first.initial_development_log_likelihood - 1e-6)
        with self.assertRaises(ValueError):
            replace(first, emission_variances=((0.0,) * 4,) + first.emission_variances[1:])
        with self.assertRaises(ValueError):
            replace(first, pi=(0.0, 0.0, 1.0))

    def test_multiblock_training_reuses_math_and_never_bridges_days(self):
        study_points = _replay_points()
        legacy_diagnostics, legacy_model = train_hmm_regime_model(study_points)
        feature_rows = tuple(_extract_feature_row(point.movement_evaluation)
                             for point in study_points)
        one_day_features, unavailable_count = _development_blocks(
            study_points, feature_rows)
        one_day = HMMDevelopmentTrainingBlock(
            0, "2024-01-01", 0, 86_400_000, SCOPE,
            one_day_features, unavailable_count)
        block_diagnostics, block_model = train_hmm_regime_model_from_blocks((one_day,))
        self.assertEqual((block_diagnostics, block_model),
                         (legacy_diagnostics, legacy_model))

        feature_blocks = _synthetic_blocks()
        expected_diagnostics, expected_model = _train_from_blocks(
            feature_blocks, 0, SCOPE, HMM_CONFIG_V1)
        actual_diagnostics, actual_model = train_hmm_regime_model_from_feature_blocks(
            feature_blocks, SCOPE, 0, HMM_CONFIG_V1)
        self.assertEqual((actual_diagnostics, actual_model),
                         (expected_diagnostics, expected_model))

        day = 86_400_000
        first = HMMDevelopmentTrainingBlock(
            0, "1970-01-01", 0, day, SCOPE, (feature_blocks[0],), 0)
        second_start = 3 * day
        second_rows = tuple(HMMFeatureRow(row.evaluation_boundary_time_ms + second_start,
                                          row.values)
                            for row in feature_blocks[1])
        second = HMMDevelopmentTrainingBlock(
            1, "1970-01-04", second_start, 4 * day, SCOPE, (second_rows,), 0)
        third_start = 6 * day
        third_rows = tuple(HMMFeatureRow(row.evaluation_boundary_time_ms + third_start,
                                         row.values)
                           for row in feature_blocks[2])
        third = HMMDevelopmentTrainingBlock(
            2, "1970-01-07", third_start, 7 * day, SCOPE, (third_rows,), 0)
        diagnostics, model = train_hmm_regime_model_from_blocks(
            (first, second, third), HMM_CONFIG_V1)
        self.assertEqual(diagnostics.block_count, 3)
        self.assertEqual(diagnostics.transition_count,
                         sum(len(block) - 1 for block in feature_blocks))
        self.assertIsNotNone(model)
        with self.assertRaises(ValueError):
            train_hmm_regime_model_from_blocks((second, first, third), HMM_CONFIG_V1)


class GaussianHMMReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.points = _replay_points()
        cls.training, cls.model = train_hmm_regime_model(cls.points)
        cls.full_run = run_market_state_hmm_experiment(cls.points)

    def test_development_only_fingerprint_and_test_hidden_prefix(self):
        development = self.points[:240 * 12]
        altered_validation = development + tuple(
            _point(point.movement_evaluation.evaluation_boundary_time_ms, point.partition,
                   shift=0.7 if point.partition == "validation" else -0.5)
            for point in self.points[240 * 12:])
        training_b, model_b = train_hmm_regime_model(altered_validation)
        self.assertEqual(self.training, training_b)
        self.assertEqual(self.model, model_b)
        self.assertEqual(self.model.training_data_sha256, model_b.training_data_sha256)
        changed_development = list(self.points)
        changed_development[0] = _point(0, "development", shift=0.4)
        _, changed_model = train_hmm_regime_model(changed_development)
        self.assertNotEqual(self.model.training_data_sha256, changed_model.training_data_sha256)
        prefix = run_market_state_hmm_experiment(self.points[:242 * 12])
        full = self.full_run
        self.assertEqual(prefix.model_artifact, full.model_artifact)
        self.assertEqual(prefix.paired_points, full.paired_points[:242 * 12])
        self.assertEqual(prefix.summaries["validation"], full.summaries["validation"])
        changed_test = self.points[:242 * 12] + tuple(
            _point(point.movement_evaluation.evaluation_boundary_time_ms, "test", shift=1.3)
            for point in self.points[242 * 12:])
        hidden = run_market_state_hmm_experiment(changed_test)
        self.assertEqual(full.model_artifact, hidden.model_artifact)
        self.assertEqual(full.paired_points[:242 * 12], hidden.paired_points[:242 * 12])
        self.assertEqual(full.summaries["validation"], hidden.summaries["validation"])
        self.assertEqual(full.summaries["development"].inference_ready_count, 0)
        self.assertEqual(full.paired_points[0].hmm_evidence.status, HMM_TRAINING_PARTITION)
        self.assertEqual(full.summaries["all"].inference_ready_count, 4)
        self.assertEqual(len(development), 240 * 12)

    def test_filter_boundary_continuity_missing_and_non_scheduled(self):
        model = self.model
        first, state = advance_hmm_regime_filter(_evaluation(240 * 60_000), "validation", model)
        self.assertEqual(first.status, HMM_READY)
        self.assertTrue(first.filter_reset_before_observation)
        self.assertEqual(first.predicted_state_probabilities, model.pi)
        off_minute, same_state = advance_hmm_regime_filter(
            _evaluation(240 * 60_000 + 5_000), "validation", model, state)
        self.assertEqual(off_minute.status, HMM_NOT_SCHEDULED)
        self.assertEqual(same_state, state)
        second, state = advance_hmm_regime_filter(
            _evaluation(241 * 60_000), "validation", model, same_state)
        self.assertFalse(second.filter_reset_before_observation)
        first_test, _ = advance_hmm_regime_filter(_evaluation(242 * 60_000), "test", model, state)
        self.assertFalse(first_test.filter_reset_before_observation)
        missing, cleared = advance_hmm_regime_filter(
            _evaluation(242 * 60_000, missing_rvol=True), "test", model, state)
        self.assertEqual(missing.status, HMM_FEATURE_UNAVAILABLE)
        self.assertIsNone(missing.posterior_probabilities)
        self.assertIsNone(missing.hard_state)
        self.assertIsNone(cleared)
        restarted, _ = advance_hmm_regime_filter(_evaluation(243 * 60_000), "test", model, cleared)
        self.assertTrue(restarted.filter_reset_before_observation)
        self.assertEqual(restarted.predicted_state_probabilities, model.pi)

    def test_fixed_scope_and_v1_training_label_separation(self):
        changed = list(self.points)
        changed[-1] = _point(changed[-1].movement_evaluation.evaluation_boundary_time_ms,
                             "test", identity={"universe_id": "other"})
        with self.assertRaises(ValueError):
            train_hmm_regime_model(changed)
        changed[-1] = _point(changed[-1].movement_evaluation.evaluation_boundary_time_ms,
                             "test", identity={"config": "other"})
        with self.assertRaises(ValueError):
            train_hmm_regime_model(changed)
        self.assertEqual(tuple(state for state in HMM_STATE_NAMES),
                         ("LOW_MOVEMENT", "MID_MOVEMENT", "HIGH_MOVEMENT"))

    def test_contingency_switches_and_occupancy_tv(self):
        run = self.full_run
        ready = tuple(point for point in run.paired_points if point.hmm_evidence.status == HMM_READY)
        summary = run.summaries["all"]
        self.assertEqual(sum(count for _, row in summary.hmm_v1_contingency
                             for _, count in row), len(ready))
        self.assertEqual(sum(item.observation_count for item in summary.state_diagnostics), len(ready))
        self.assertEqual(summary.hard_state_transition_comparison_count, 3)
        self.assertEqual(summary.v1_direction_transition_comparison_count, 3)
        self.assertEqual(summary.sum_predictive_log_likelihood,
                         math.fsum(point.hmm_evidence.predictive_log_likelihood for point in ready))
        validation = run.paired_points[240 * 12:242 * 12]
        test = run.paired_points[242 * 12:]
        expected_tv = 0.5 * sum(abs(
            sum(point.hmm_evidence.hard_state == state for point in validation if point.hmm_evidence.status == HMM_READY) / 2
            - sum(point.hmm_evidence.hard_state == state for point in test if point.hmm_evidence.status == HMM_READY) / 2)
            for state in HMM_STATE_NAMES)
        self.assertEqual(_occupancy_tv(validation, test), expected_tv)
        self.assertEqual(summary.validation_test_occupancy_total_variation, expected_tv)
        self.assertIsNone(run.summaries["validation"].validation_test_occupancy_total_variation)

    def test_known_contingency_switch_counts_and_occupancy_tv(self):
        training = HMMTrainingDiagnostics("HMM_TRAINING_READY", None, 240, 0, 1,
                                          239, 0, 239 * 60_000, "0" * 64)
        def point(index, state, direction, partition="validation", status=HMM_READY,
                  reset=False):
            evidence = SimpleNamespace(
                status=status, hard_state=state, filter_reset_before_observation=reset,
                predictive_log_likelihood=-1.0, posterior_confidence=0.8,
                posterior_entropy=0.4, raw_feature_vector=(1.0, 0.0, 0.5, 1.0),
            )
            classification = SimpleNamespace(windows={5: SimpleNamespace(direction_state=direction)})
            return SimpleNamespace(
                evaluation_boundary_time_ms=index * 60_000, partition=partition,
                hmm_evidence=evidence, baseline_classification=classification,
                baseline_lifecycle_state=SimpleNamespace(active_episode=None),
                baseline_transitions=(),
            )
        known = (
            point(0, "LOW_MOVEMENT", "NEUTRAL", reset=True),
            point(1, "LOW_MOVEMENT", "NEUTRAL"),
            point(2, "MID_MOVEMENT", "BROAD_RISE"),
            point(3, "MID_MOVEMENT", "BROAD_RISE"),
            point(4, "HIGH_MOVEMENT", "BROAD_DROP"),
            point(5, None, "NEUTRAL", status=HMM_FEATURE_UNAVAILABLE),
            point(6, "HIGH_MOVEMENT", "BROAD_DROP", partition="test", reset=True),
        )
        summary = _summary(known, "all", training)
        self.assertEqual(summary.hard_state_transition_comparison_count, 4)
        self.assertEqual(summary.hard_state_switch_count, 2)
        self.assertEqual(summary.hard_state_switch_fraction, 0.5)
        self.assertEqual(summary.v1_direction_switch_count, 2)
        self.assertEqual(summary.simultaneous_hmm_and_v1_switch_count, 2)
        self.assertEqual(summary.hmm_v1_contingency[0][1],
                         (("BROAD_RISE", 0), ("BROAD_DROP", 0), ("NEUTRAL", 2)))
        validation = (
            point(0, "LOW_MOVEMENT", "NEUTRAL"),
            point(1, "MID_MOVEMENT", "NEUTRAL"),
        )
        test = (
            point(2, "MID_MOVEMENT", "NEUTRAL", partition="test"),
            point(3, "HIGH_MOVEMENT", "NEUTRAL", partition="test"),
        )
        self.assertEqual(_occupancy_tv(validation, test), 0.5)


if __name__ == "__main__":
    unittest.main()
