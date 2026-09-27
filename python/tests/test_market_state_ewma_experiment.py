from dataclasses import replace
from decimal import Decimal
import unittest

from market_analysis.experiments.market_state_ewma import (
    EWMA_CONFIG_10S,
    EWMA_CONFIG_30S,
    EWMA_CONFIG_60S,
    MarketStateExperimentPoint,
    run_market_state_ewma_experiment,
    transform_market_movement_with_ewma,
)
from market_analysis.movement_classifier import SymbolSourceTimeEvidence
from market_analysis.movement_metrics import (
    BreadthSide,
    MarketMovementEvaluation,
    MarketMovementWindowResult,
    Metric,
    SymbolMovementResult,
    WindowAggregates,
    WindowBreadth,
)


SYMBOLS = ("S1", "S2", "S3", "S4", "S5")


def _symbol(symbol, boundary, minute, direction, acceleration):
    return SymbolMovementResult(
        symbol=symbol,
        instrument_id=symbol,
        provider="binance-usdm",
        exchange="binance",
        price_type="trade",
        window_minutes=minute,
        evaluation_boundary_time_ms=boundary,
        included=True,
        exclusion_reasons=(),
        current_return=Metric.present(0.1 if direction == "RISING" else -0.1),
        previous_return=Metric.present(0.05),
        velocity=Metric.present(0.01),
        previous_velocity=Metric.present(0.005),
        acceleration=Metric.present(acceleration),
        historical_median=Metric.present(0.0),
        historical_mad=Metric.present(0.1),
        normalized_z=Metric.present(1.0),
        direction=Metric.present(direction),
        material_rising=direction == "RISING",
        material_falling=direction == "FALLING",
        current_notional_volume=Metric.present(Decimal("1")),
        rvol=Metric.present(1.0),
        cross_sectional_z=Metric.present(0.0),
        outlier_candidate=False,
    )


def _side(count, denominator):
    return Metric.present(BreadthSide(count, count / denominator))


def _window(boundary, minute, state="rise", normalized=0.6, raw=0.1,
            universe=SYMBOLS, universe_id="u1", universe_version="v1",
            movement_config_version="market-movement-config-v1"):
    denominator = len(universe)
    if state == "rise":
        direction = "RISING"
        acceleration = 1.0
        rising, falling = denominator, 0
        material_rising, material_falling = denominator, 0
    elif state == "drop":
        direction = "FALLING"
        acceleration = -1.0
        rising, falling = 0, denominator
        material_rising, material_falling = 0, denominator
    else:
        direction = "FLAT"
        acceleration = 0.0
        rising = falling = material_rising = material_falling = 0
    symbols = tuple(_symbol(symbol, boundary, minute, direction, acceleration)
                    for symbol in universe)
    breadth = WindowBreadth(
        available=True,
        reason=None,
        denominator=denominator,
        flat=_side(denominator if state == "neutral" else 0, denominator),
        rising=_side(rising, denominator),
        falling=_side(falling, denominator),
        material_rising=_side(material_rising, denominator),
        material_falling=_side(material_falling, denominator),
    )
    aggregates = WindowAggregates(
        median_normalized_movement=Metric.present(normalized),
        median_raw_return=Metric.present(raw),
        trimmed_mean_normalized_movement=Metric.present(normalized),
        liquidity_weighted_normalized_movement=Metric.present(normalized),
        liquidity_weights=Metric.present(tuple((symbol, 1 / denominator) for symbol in universe)),
        dispersion_mad_normalized_movement=Metric.present(0.1),
    )
    return MarketMovementWindowResult(
        algorithm_version="market-movement-v1",
        config_version=movement_config_version,
        universe_id=universe_id,
        universe_version=universe_version,
        configured_universe=tuple(universe),
        included_symbols=tuple(universe),
        excluded_symbols=(),
        window_minutes=minute,
        provider="binance-usdm",
        exchange="binance",
        price_type="trade",
        evaluation_boundary_time_ms=boundary,
        historical_lookback_ms=100,
        minimum_historical_coverage_ms=50,
        market_wide_eligible=True,
        eligible_count=denominator,
        eligible_fraction=1.0,
        symbols=symbols,
        breadth=breadth,
        aggregates=aggregates,
    )


def _evaluation(boundary, state="rise", normalized=0.6, raw=0.1,
                universe=SYMBOLS, universe_id="u1", universe_version="v1",
                movement_config_version="market-movement-config-v1"):
    windows = {
        minute: _window(boundary, minute, state, normalized, raw, universe,
                        universe_id, universe_version, movement_config_version)
        for minute in (1, 5, 15)
    }
    return MarketMovementEvaluation(
        algorithm_version="market-movement-v1",
        config_version=movement_config_version,
        universe_id=universe_id,
        universe_version=universe_version,
        configured_universe=tuple(universe),
        provider="binance-usdm",
        exchange="binance",
        price_type="trade",
        evaluation_boundary_time_ms=boundary,
        historical_lookback_ms=100,
        minimum_historical_coverage_ms=50,
        windows=windows,
    )


def _evidence(universe=SYMBOLS):
    return tuple(SymbolSourceTimeEvidence(symbol, 1, 2, 3) for symbol in universe)


def _point(boundary, state="rise", normalized=0.6, raw=0.1,
           universe=SYMBOLS, universe_id="u1", universe_version="v1",
           movement_config_version="market-movement-config-v1", partition="development"):
    return MarketStateExperimentPoint(
        _evaluation(boundary, state, normalized, raw, universe, universe_id,
                    universe_version, movement_config_version),
        _evidence(universe), partition,
    )


class MarketStateEWMAExperimentTests(unittest.TestCase):
    def test_ewma_uses_half_life_formula_and_seeds_first_point(self):
        first, state = transform_market_movement_with_ewma(
            _evaluation(0, normalized=1.0), EWMA_CONFIG_10S)
        second, next_state = transform_market_movement_with_ewma(
            _evaluation(5_000, normalized=0.0), EWMA_CONFIG_10S, state)
        alpha = 1 - 2 ** (-5_000 / 10_000)
        self.assertEqual(first.windows[5].aggregates.median_normalized_movement.value, 1.0)
        self.assertAlmostEqual(
            second.windows[5].aggregates.median_normalized_movement.value,
            1 - alpha,
        )
        self.assertAlmostEqual(next_state.current_ewma, 1 - alpha)

    def test_fixed_half_lives_are_distinct_and_deterministic(self):
        first = _evaluation(0, normalized=1.0)
        second = _evaluation(5_000, normalized=0.0)
        values = []
        for config in (EWMA_CONFIG_10S, EWMA_CONFIG_30S, EWMA_CONFIG_60S):
            _, state = transform_market_movement_with_ewma(first, config)
            candidate, _ = transform_market_movement_with_ewma(second, config, state)
            values.append(candidate.windows[5].aggregates.median_normalized_movement.value)
        self.assertEqual(len(set(values)), 3)
        self.assertLess(values[0], values[1])
        self.assertLess(values[1], values[2])

    def test_unavailable_metric_resets_without_bridging_or_future_access(self):
        first, state = transform_market_movement_with_ewma(
            _evaluation(0, normalized=1.0), EWMA_CONFIG_10S)
        unavailable_evaluation = _evaluation(5_000, normalized=0.0)
        unavailable = replace(
            unavailable_evaluation,
            windows={
                **unavailable_evaluation.windows,
                5: replace(
                    unavailable_evaluation.windows[5],
                    aggregates=replace(
                        unavailable_evaluation.windows[5].aggregates,
                        median_normalized_movement=Metric.missing("missing"),
                    ),
                ),
            },
        )
        candidate, reset = transform_market_movement_with_ewma(
            unavailable, EWMA_CONFIG_10S, state)
        self.assertEqual(first.windows[5].aggregates.median_normalized_movement.value, 1.0)
        self.assertIsNone(reset)
        self.assertFalse(candidate.windows[5].aggregates.median_normalized_movement.available)
        seeded, seeded_state = transform_market_movement_with_ewma(
            _evaluation(10_000, normalized=0.25), EWMA_CONFIG_10S, reset)
        self.assertEqual(seeded.windows[5].aggregates.median_normalized_movement.value, 0.25)
        self.assertEqual(seeded_state.current_ewma, 0.25)

    def test_gap_universe_and_baseline_identity_reset_state(self):
        _, state = transform_market_movement_with_ewma(
            _evaluation(0, normalized=1.0), EWMA_CONFIG_10S)
        gap, gap_state = transform_market_movement_with_ewma(
            _evaluation(10_000, normalized=0.25), EWMA_CONFIG_10S, state)
        self.assertEqual(gap.windows[5].aggregates.median_normalized_movement.value, 0.25)
        self.assertEqual(gap_state.current_ewma, 0.25)
        changed, changed_state = transform_market_movement_with_ewma(
            _evaluation(15_000, normalized=0.75, universe_id="u2"),
            EWMA_CONFIG_10S,
            gap_state,
        )
        self.assertEqual(changed_state.current_ewma, 0.75)
        changed_config, changed_config_state = transform_market_movement_with_ewma(
            _evaluation(20_000, normalized=0.5, movement_config_version="other-config"),
            EWMA_CONFIG_10S,
            changed_state,
        )
        self.assertEqual(changed_config_state.current_ewma, 0.5)
        self.assertEqual(
            changed_config.windows[5].aggregates.median_normalized_movement.value,
            0.5,
        )

    def test_candidate_is_immutable_identity_scoped_and_changes_only_primary_metric(self):
        baseline = _evaluation(0, normalized=0.75)
        candidate, _ = transform_market_movement_with_ewma(baseline, EWMA_CONFIG_30S)
        self.assertEqual(baseline.windows[5].aggregates.median_normalized_movement.value, 0.75)
        self.assertNotEqual(candidate.algorithm_version, baseline.algorithm_version)
        self.assertEqual(candidate.config_version, EWMA_CONFIG_30S.version)
        self.assertEqual(candidate.windows[1].aggregates, baseline.windows[1].aggregates)
        self.assertEqual(candidate.windows[15].aggregates, baseline.windows[15].aggregates)
        self.assertEqual(candidate.windows[5].aggregates.median_normalized_movement.value, 0.75)
        self.assertEqual(candidate.windows[5].breadth, baseline.windows[5].breadth)
        self.assertEqual(candidate.windows[5].symbols, baseline.windows[5].symbols)

    def test_runner_reuses_canonical_branches_and_keeps_lifecycle_states_isolated(self):
        points = (
            _point(0, normalized=0.6),
            _point(5_000, normalized=0.0),
        )
        result = run_market_state_ewma_experiment(points, EWMA_CONFIG_10S)
        first, second = result.points
        self.assertIs(
            first.baseline_classification.windows[5].movement_snapshot,
            points[0].movement_evaluation.windows[5],
        )
        self.assertEqual(first.baseline_primary_direction_state, "BROAD_RISE")
        self.assertEqual(second.baseline_primary_direction_state, "BROAD_RISE")
        self.assertEqual(second.candidate_primary_direction_state, "NEUTRAL")
        self.assertIsNotNone(second.baseline_lifecycle_state.active_episode)
        self.assertIsNone(second.candidate_lifecycle_state.active_episode)
        self.assertNotEqual(
            first.candidate_classification.windows[5].movement_algorithm_version,
            "market-movement-v1",
        )

    def test_partition_and_boundary_validation_is_strict(self):
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            run_market_state_ewma_experiment((_point(0), _point(0)), EWMA_CONFIG_10S)
        with self.assertRaisesRegex(ValueError, "exactly 5000"):
            run_market_state_ewma_experiment((_point(0), _point(10_000)), EWMA_CONFIG_10S)
        with self.assertRaisesRegex(ValueError, "development, validation"):
            run_market_state_ewma_experiment((
                _point(0, partition="validation"),
                _point(5_000, partition="development"),
            ), EWMA_CONFIG_10S)

    def test_experiment_point_requires_exact_ordered_source_evidence(self):
        evaluation = _evaluation(0)
        with self.assertRaisesRegex(ValueError, "configured universe"):
            MarketStateExperimentPoint(evaluation, _evidence(SYMBOLS[:-1]), "development")
        with self.assertRaisesRegex(ValueError, "configured universe"):
            MarketStateExperimentPoint(evaluation, _evidence(SYMBOLS + ("S6",)), "development")
        with self.assertRaisesRegex(ValueError, "configured universe"):
            MarketStateExperimentPoint(
                evaluation,
                _evidence(("S2", "S1", "S3", "S4", "S5")),
                "development",
            )

    def test_development_summary_does_not_match_future_candidate_onset(self):
        points = (
            _point(0, state="neutral", normalized=0.0, raw=0.0),
            _point(5_000, state="neutral", normalized=0.0, raw=0.0),
            _point(10_000, normalized=1.0, raw=0.1),
            _point(15_000, normalized=1.0, raw=0.1),
            _point(20_000, normalized=1.0, raw=0.1, partition="validation"),
            _point(25_000, normalized=1.0, raw=0.1, partition="validation"),
        )
        result = run_market_state_ewma_experiment(points, EWMA_CONFIG_10S)
        development = result.summaries["development"]
        self.assertEqual(development.matched_onset_count, 0)
        self.assertEqual(development.unmatched_baseline_onset_count, 1)
        self.assertGreaterEqual(result.summaries["all"].matched_onset_count, 1)

    def test_future_close_does_not_make_development_episode_short_lived(self):
        points = (
            _point(0, normalized=0.6, partition="development"),
            _point(5_000, normalized=0.6, partition="development"),
            _point(10_000, state="neutral", normalized=0.0, raw=0.0,
                   partition="development"),
            _point(15_000, state="neutral", normalized=0.0, raw=0.0,
                   partition="development"),
            _point(20_000, state="neutral", normalized=0.0, raw=0.0,
                   partition="validation"),
        )
        result = run_market_state_ewma_experiment(points, EWMA_CONFIG_10S)
        self.assertEqual(result.summaries["development"].baseline_short_lived_episode_count, 0)
        self.assertEqual(result.summaries["all"].baseline_short_lived_episode_count, 1)

    def test_active_at_end_episode_is_censored_not_short_lived(self):
        result = run_market_state_ewma_experiment((
            _point(0, normalized=0.6),
            _point(5_000, normalized=0.6),
        ), EWMA_CONFIG_10S)
        self.assertEqual(result.summaries["all"].baseline_short_lived_episode_count, 0)
        self.assertEqual(result.summaries["all"].candidate_short_lived_episode_count, 0)

    def test_summary_reports_short_lived_episodes_and_onset_lead_lag(self):
        points = (
            _point(0, normalized=0.6),
            _point(5_000, normalized=0.6),
            _point(10_000, state="neutral", normalized=0.0, raw=0.0),
            _point(15_000, state="neutral", normalized=0.0, raw=0.0),
            _point(20_000, state="neutral", normalized=0.0, raw=0.0),
        )
        result = run_market_state_ewma_experiment(points, EWMA_CONFIG_10S)
        summary = result.summaries["all"]
        self.assertEqual(summary.baseline_short_lived_episode_count, 1)
        self.assertEqual(summary.candidate_short_lived_episode_count, 1)
        self.assertEqual(summary.matched_onset_count, 1)
        self.assertEqual(summary.unmatched_baseline_onset_count, 0)
        self.assertEqual(summary.median_signed_onset_delta_ms, 0.0)
        self.assertEqual(summary.baseline_transition_counts, (("ENDED", 1), ("STARTED", 1)))

    def test_identical_explicit_inputs_are_deterministic_and_partition_summaries_exist(self):
        points = (
            _point(0, partition="development"),
            _point(5_000, partition="validation"),
            _point(10_000, partition="test"),
        )
        first = run_market_state_ewma_experiment(points, EWMA_CONFIG_60S)
        second = run_market_state_ewma_experiment(points, EWMA_CONFIG_60S)
        self.assertEqual(first, second)
        self.assertEqual(set(first.summaries), {"all", "development", "validation", "test"})
        prefix = run_market_state_ewma_experiment(points[:2], EWMA_CONFIG_60S)
        self.assertEqual(prefix.points, first.points[:2])
