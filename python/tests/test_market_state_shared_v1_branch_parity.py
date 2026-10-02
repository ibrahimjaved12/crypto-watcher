"""Generated parity fixtures for the shared canonical V1 branch optimization."""

from dataclasses import replace
from decimal import Decimal
import unittest

from market_analysis.experiments.market_state_common import (
    MarketStateExperimentPoint, advance_canonical_branch,
    canonical_branch_for_point,
)
from market_analysis.historical_experiment_batch import (
    EXPERIMENT_SUITE_V1, report_json_safe,
)
from market_analysis.market_episode_lifecycle import MarketEpisodeLifecycleConfig
from market_analysis.movement_classifier import (
    MarketClassifierConfig, SymbolSourceTimeEvidence,
)
from market_analysis.movement_metrics import (
    ALGORITHM_VERSION, DEFAULT_CONFIG_VERSION, MarketMovementConfig,
    MarketMovementEvaluation, MarketMovementWindowResult, Metric,
    SymbolMovementResult, WINDOWS, _aggregates, _breadth,
)


SYMBOLS = ("S1", "S2", "S3", "S4", "S5")
MOVEMENT_CONFIG = MarketMovementConfig()
RUNNER_FAMILIES = (
    "EXP-75-01", "EXP-75-02", "EXP-75-03", "EXP-75-04A", "EXP-75-04B",
    "EXP-75-05", "EXP-75-06A", "EXP-75-07", "EXP-75-08",
)


def _evaluation(boundary):
    minute_index = boundary // 60_000
    group = (minute_index // 80) % 3
    center = (-2.0, 0.0, 2.0)[group] + 0.025 * ((minute_index * 3) % 7 - 3)
    spread = 0.3 + 0.04 * (minute_index % 5)
    z_values = tuple(center + spread * offset for offset in (-2, -1, 0, 1, 2))
    windows = {}
    for minute in WINDOWS:
        rows = tuple(SymbolMovementResult(
            symbol=symbol, instrument_id=symbol, provider="binance-usdm",
            exchange="binance", price_type="trade", window_minutes=minute,
            evaluation_boundary_time_ms=boundary, included=True, exclusion_reasons=(),
            current_return=Metric.present(z * 0.01),
            previous_return=Metric.present(0.0),
            velocity=Metric.present(z * 0.01 / (minute * 60)),
            previous_velocity=Metric.present(0.0), acceleration=Metric.present(0.0),
            historical_median=Metric.present(0.0), historical_mad=Metric.present(0.02),
            normalized_z=Metric.present(z),
            direction=Metric.present("FLAT" if abs(z) < MOVEMENT_CONFIG.flat_z
                                     else "RISING" if z > 0 else "FALLING"),
            material_rising=z >= MOVEMENT_CONFIG.material_z,
            material_falling=z <= -MOVEMENT_CONFIG.material_z,
            current_notional_volume=Metric.present(Decimal("10")),
            rvol=Metric.present(1.0 + 0.03 * (minute_index % 9) + 0.01 * index),
            cross_sectional_z=Metric.missing("CROSS_SECTIONAL_MAD_UNAVAILABLE"),
            outlier_candidate=False,
        ) for index, (symbol, z) in enumerate(zip(SYMBOLS, z_values)))
        windows[minute] = MarketMovementWindowResult(
            algorithm_version=ALGORITHM_VERSION,
            config_version=DEFAULT_CONFIG_VERSION,
            universe_id="generated-five-symbol", universe_version="v1",
            configured_universe=SYMBOLS, included_symbols=SYMBOLS,
            excluded_symbols=(), window_minutes=minute, provider="binance-usdm",
            exchange="binance", price_type="trade",
            evaluation_boundary_time_ms=boundary,
            historical_lookback_ms=100, minimum_historical_coverage_ms=50,
            market_wide_eligible=True, eligible_count=len(SYMBOLS),
            eligible_fraction=1.0, symbols=rows, breadth=_breadth(rows, True),
            aggregates=_aggregates(rows, True, MOVEMENT_CONFIG),
        )
    return MarketMovementEvaluation(
        algorithm_version=ALGORITHM_VERSION,
        config_version=DEFAULT_CONFIG_VERSION,
        universe_id="generated-five-symbol", universe_version="v1",
        configured_universe=SYMBOLS, provider="binance-usdm", exchange="binance",
        price_type="trade", evaluation_boundary_time_ms=boundary,
        historical_lookback_ms=100, minimum_historical_coverage_ms=50,
        windows=windows,
    )


def _points(count=220):
    return tuple(MarketStateExperimentPoint(
        _evaluation(tick * 5_000),
        tuple(SymbolSourceTimeEvidence(symbol, None, None, None)
              for symbol in SYMBOLS),
        "development",
    ) for tick in range(count))


def _canonical_branch(points, classifier_config, lifecycle_config):
    result = {}
    previous_state = None
    for point in points:
        branch = advance_canonical_branch(
            point.movement_evaluation, point.source_time_evidence,
            previous_state, classifier_config, lifecycle_config)
        result[point.movement_evaluation.evaluation_boundary_time_ms] = branch
        previous_state = branch[1].next_state
    return result


class SharedV1BranchParityTests(unittest.TestCase):
    def test_all_modified_suite_runner_families_preserve_candidate_output(self):
        points = _points()
        classifier_config = MarketClassifierConfig()
        lifecycle_config = MarketEpisodeLifecycleConfig()
        shared = _canonical_branch(points, classifier_config, lifecycle_config)
        first_by_family = {}
        for descriptor in EXPERIMENT_SUITE_V1:
            if descriptor.experiment_id != "EXP-75-09":
                first_by_family.setdefault(descriptor.experiment_id, descriptor)
        self.assertEqual(tuple(first_by_family), RUNNER_FAMILIES)

        for family in RUNNER_FAMILIES:
            descriptor = first_by_family[family]
            with self.subTest(family=family, config=descriptor.config_version):
                original_path = descriptor.runner(
                    points, descriptor.config, classifier_config=classifier_config,
                    lifecycle_config=lifecycle_config)
                shared_path = descriptor.runner(
                    points, descriptor.config, classifier_config=classifier_config,
                    lifecycle_config=lifecycle_config,
                    canonical_branch_by_boundary=shared)
                self.assertEqual(report_json_safe(original_path),
                                 report_json_safe(shared_path))

    def test_cached_branch_rejects_same_boundary_scope_and_evidence_mismatches(self):
        points = _points(3)
        point = points[0]
        classifier_config = MarketClassifierConfig()
        lifecycle_config = MarketEpisodeLifecycleConfig()
        shared = _canonical_branch(points, classifier_config, lifecycle_config)
        altered_evidence = (replace(point.source_time_evidence[0],
                                    last_received_at_ms=1),
                            *point.source_time_evidence[1:])
        cases = (
            (replace(point.movement_evaluation, price_type="mark"),
             point.source_time_evidence, classifier_config, lifecycle_config),
            (replace(point.movement_evaluation, config_version="movement-v2"),
             point.source_time_evidence, classifier_config, lifecycle_config),
            (replace(point.movement_evaluation,
                     configured_universe=tuple(reversed(SYMBOLS))),
             point.source_time_evidence, classifier_config, lifecycle_config),
            (point.movement_evaluation, altered_evidence,
             classifier_config, lifecycle_config),
            (point.movement_evaluation, point.source_time_evidence,
             replace(classifier_config, version="generated-classifier-v2"),
             lifecycle_config),
            (point.movement_evaluation, point.source_time_evidence,
             classifier_config,
             replace(lifecycle_config, version="generated-lifecycle-v2")),
        )
        for evaluation, evidence, classifier, lifecycle in cases:
            with self.subTest(classifier=classifier.version, lifecycle=lifecycle.version):
                with self.assertRaises(ValueError):
                    canonical_branch_for_point(
                        evaluation, evidence, None, classifier, lifecycle, shared)
