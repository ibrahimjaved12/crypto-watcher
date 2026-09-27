from dataclasses import replace
from decimal import Decimal
import unittest

from market_analysis.experiments.market_state_common import MarketStateExperimentPoint
from market_analysis.experiments.market_state_kalman import (
    KALMAN_ALGORITHM_VERSION,
    KALMAN_CONFIG_Q0025,
    KALMAN_CONFIG_Q0100,
    KALMAN_CONFIG_Q0400,
    KALMAN_CONFIGURATIONS,
    KALMAN_MEASUREMENT_VARIANCE,
    KalmanConfig,
    run_market_state_kalman_experiment,
    transform_market_movement_with_kalman,
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


def _window(boundary, minute, state="neutral", normalized=0.0, raw=0.0,
            universe=SYMBOLS, universe_id="u1", universe_version="v1",
            movement_algorithm_version="market-movement-v1",
            movement_config_version="market-movement-config-v1",
            provider="binance-usdm", exchange="binance", price_type="trade"):
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
        liquidity_weights=Metric.present(tuple((symbol, 1 / denominator)
                                               for symbol in universe)),
        dispersion_mad_normalized_movement=Metric.present(0.1),
    )
    return MarketMovementWindowResult(
        algorithm_version=movement_algorithm_version,
        config_version=movement_config_version,
        universe_id=universe_id,
        universe_version=universe_version,
        configured_universe=tuple(universe),
        included_symbols=tuple(universe),
        excluded_symbols=(),
        window_minutes=minute,
        provider=provider,
        exchange=exchange,
        price_type=price_type,
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


def _evaluation(boundary, state="neutral", normalized=0.0, raw=0.0,
                universe=SYMBOLS, universe_id="u1", universe_version="v1",
                movement_algorithm_version="market-movement-v1",
                movement_config_version="market-movement-config-v1",
                provider="binance-usdm", exchange="binance", price_type="trade"):
    windows = {
        minute: _window(
            boundary, minute, state, normalized, raw, universe, universe_id,
            universe_version, movement_algorithm_version,
            movement_config_version, provider, exchange, price_type,
        )
        for minute in (1, 5, 15)
    }
    return MarketMovementEvaluation(
        algorithm_version=movement_algorithm_version,
        config_version=movement_config_version,
        universe_id=universe_id,
        universe_version=universe_version,
        configured_universe=tuple(universe),
        provider=provider,
        exchange=exchange,
        price_type=price_type,
        evaluation_boundary_time_ms=boundary,
        historical_lookback_ms=100,
        minimum_historical_coverage_ms=50,
        windows=windows,
    )


def _evidence(universe=SYMBOLS):
    return tuple(SymbolSourceTimeEvidence(symbol, 1, 2, 3) for symbol in universe)


def _point(boundary, state="neutral", normalized=0.0, raw=0.0,
           universe=SYMBOLS, universe_id="u1", universe_version="v1",
           movement_algorithm_version="market-movement-v1",
           movement_config_version="market-movement-config-v1",
           provider="binance-usdm", exchange="binance", price_type="trade",
           partition="development"):
    return MarketStateExperimentPoint(
        _evaluation(
            boundary, state, normalized, raw, universe, universe_id,
            universe_version, movement_algorithm_version,
            movement_config_version, provider, exchange, price_type,
        ),
        _evidence(universe),
        partition,
    )


class MarketStateKalmanExperimentTests(unittest.TestCase):
    def test_initialization_seeds_level_and_covariance_without_innovation(self):
        candidate, observation, state = transform_market_movement_with_kalman(
            _evaluation(0, normalized=0.6), KALMAN_CONFIG_Q0100)
        self.assertEqual(observation.filtered_level.value, 0.6)
        self.assertIsNone(observation.innovation)
        self.assertIsNone(observation.innovation_variance)
        self.assertIsNone(observation.level_gain)
        self.assertIsNone(observation.trend_gain)
        self.assertEqual(state.level, 0.6)
        self.assertEqual(state.trend, 0.0)
        self.assertEqual((state.p00, state.p01, state.p11), (0.25, 0.0, 0.25))
        self.assertEqual(
            candidate.windows[5].aggregates.median_normalized_movement.value, 0.6)

    def test_one_step_middle_configuration_matches_independent_reference(self):
        _, _, state = transform_market_movement_with_kalman(
            _evaluation(0, normalized=0.0), KALMAN_CONFIG_Q0100)
        _, observation, state = transform_market_movement_with_kalman(
            _evaluation(5_000, normalized=0.6), KALMAN_CONFIG_Q0100, state)
        self.assertAlmostEqual(observation.innovation, 0.6)
        self.assertAlmostEqual(observation.innovation_variance, 0.7525)
        self.assertAlmostEqual(observation.level_gain, 0.6677740863787375)
        self.assertAlmostEqual(observation.trend_gain, 0.33887043189368776)
        self.assertAlmostEqual(observation.level, 0.4006644518272425)
        self.assertAlmostEqual(observation.trend, 0.20332225913621266)
        self.assertAlmostEqual(state.level, observation.level)
        self.assertAlmostEqual(state.trend, observation.trend)

    def test_constant_input_keeps_level_and_zero_trend(self):
        state = None
        for index in range(8):
            _, observation, state = transform_market_movement_with_kalman(
                _evaluation(index * 5_000, normalized=0.3),
                KALMAN_CONFIG_Q0100,
                state,
            )
            self.assertAlmostEqual(observation.level, 0.3)
            self.assertAlmostEqual(observation.trend, 0.0)

    def test_process_noise_changes_level_gain_without_selecting_a_winner(self):
        gains = []
        for config in (KALMAN_CONFIG_Q0025, KALMAN_CONFIG_Q0100, KALMAN_CONFIG_Q0400):
            _, _, state = transform_market_movement_with_kalman(
                _evaluation(0, normalized=0.0), config)
            _, observation, _ = transform_market_movement_with_kalman(
                _evaluation(5_000, normalized=0.6), config, state)
            gains.append(observation.level_gain)
        self.assertGreater(gains[2], gains[1])
        self.assertGreater(gains[1], gains[0])
        self.assertEqual(tuple(config.r for config in KALMAN_CONFIGURATIONS),
                         (KALMAN_MEASUREMENT_VARIANCE,) * 3)
        with self.assertRaises(ValueError):
            KalmanConfig("custom", 0.0200, KALMAN_MEASUREMENT_VARIANCE)

    def test_canonical_branches_are_isolated_and_candidate_lags(self):
        points = (
            _point(0, normalized=0.0),
            _point(5_000, state="rise", normalized=0.6, raw=0.1),
            _point(10_000, state="rise", normalized=0.6, raw=0.1),
            _point(15_000, state="rise", normalized=0.6, raw=0.1),
        )
        result = run_market_state_kalman_experiment(points, KALMAN_CONFIG_Q0100)
        initial, first_rise, second_rise, confirmed_rise = result.points
        self.assertEqual(initial.baseline_primary_direction_state, "NEUTRAL")
        self.assertEqual(first_rise.baseline_primary_direction_state, "BROAD_RISE")
        self.assertEqual(first_rise.candidate_primary_direction_state, "NEUTRAL")
        self.assertIsNone(first_rise.baseline_lifecycle_state.active_episode)
        self.assertIsNone(first_rise.candidate_lifecycle_state.active_episode)
        self.assertEqual(second_rise.baseline_primary_direction_state, "BROAD_RISE")
        self.assertEqual(second_rise.candidate_primary_direction_state, "BROAD_RISE")
        self.assertIsNotNone(second_rise.baseline_lifecycle_state.active_episode)
        self.assertIsNone(second_rise.candidate_lifecycle_state.active_episode)
        self.assertEqual(confirmed_rise.candidate_primary_direction_state, "BROAD_RISE")
        self.assertIsNotNone(confirmed_rise.candidate_lifecycle_state.active_episode)
        self.assertAlmostEqual(first_rise.kalman_level, 0.4006644518272425)
        self.assertNotEqual(
            first_rise.candidate_movement_evaluation.algorithm_version,
            points[1].movement_evaluation.algorithm_version,
        )

    def test_candidate_changes_only_selected_primary_aggregate(self):
        first = _evaluation(0, normalized=0.0)
        _, _, state = transform_market_movement_with_kalman(
            first, KALMAN_CONFIG_Q0100)
        baseline = _evaluation(5_000, state="rise", normalized=0.6, raw=0.1)
        candidate, _, _ = transform_market_movement_with_kalman(
            baseline, KALMAN_CONFIG_Q0100, state)
        self.assertEqual(
            baseline.windows[5].aggregates.median_normalized_movement.value, 0.6)
        self.assertEqual(candidate.windows[1].aggregates, baseline.windows[1].aggregates)
        self.assertEqual(candidate.windows[15].aggregates, baseline.windows[15].aggregates)
        self.assertEqual(candidate.windows[5].breadth, baseline.windows[5].breadth)
        self.assertEqual(candidate.windows[5].symbols, baseline.windows[5].symbols)
        self.assertEqual(
            candidate.windows[5].aggregates.median_raw_return,
            baseline.windows[5].aggregates.median_raw_return,
        )
        self.assertEqual(
            candidate.windows[5].symbols[0].acceleration,
            baseline.windows[5].symbols[0].acceleration,
        )
        self.assertNotEqual(
            candidate.windows[5].aggregates.median_normalized_movement,
            baseline.windows[5].aggregates.median_normalized_movement,
        )
        self.assertEqual(candidate.algorithm_version, KALMAN_ALGORITHM_VERSION)

    def test_unavailable_input_clears_state_and_next_value_seeds(self):
        _, _, state = transform_market_movement_with_kalman(
            _evaluation(0, normalized=0.4), KALMAN_CONFIG_Q0100)
        missing = _evaluation(5_000, normalized=0.0)
        missing = replace(
            missing,
            windows={
                **missing.windows,
                5: replace(
                    missing.windows[5],
                    aggregates=replace(
                        missing.windows[5].aggregates,
                        median_normalized_movement=Metric.missing("missing"),
                    ),
                ),
            },
        )
        candidate, observation, reset = transform_market_movement_with_kalman(
            missing, KALMAN_CONFIG_Q0100, state)
        self.assertFalse(observation.available)
        self.assertFalse(candidate.windows[5].aggregates.median_normalized_movement.available)
        self.assertIsNone(observation.level)
        self.assertIsNone(observation.trend)
        self.assertIsNone(observation.innovation)
        self.assertIsNone(observation.innovation_variance)
        self.assertIsNone(observation.level_gain)
        self.assertIsNone(observation.trend_gain)
        self.assertEqual(observation.candidate_algorithm_version, KALMAN_ALGORITHM_VERSION)
        self.assertEqual(observation.candidate_config_version, KALMAN_CONFIG_Q0100.version)
        self.assertIsNone(reset)
        _, restarted, reset = transform_market_movement_with_kalman(
            _evaluation(10_000, normalized=0.7), KALMAN_CONFIG_Q0100, reset)
        self.assertEqual(restarted.level, 0.7)
        self.assertEqual(restarted.trend, 0.0)
        self.assertEqual((reset.p00, reset.p01, reset.p11), (0.25, 0.0, 0.25))

    def test_scope_and_nonconsecutive_changes_fresh_seed(self):
        _, _, original_state = transform_market_movement_with_kalman(
            _evaluation(0, normalized=0.2), KALMAN_CONFIG_Q0100)
        changes = (
            ({"universe_id": "u2"}, KALMAN_CONFIG_Q0100),
            ({"universe_version": "v2"}, KALMAN_CONFIG_Q0100),
            ({"movement_algorithm_version": "other-algorithm"}, KALMAN_CONFIG_Q0100),
            ({"movement_config_version": "other-config"}, KALMAN_CONFIG_Q0100),
            ({"provider": "other-provider"}, KALMAN_CONFIG_Q0100),
            ({"exchange": "other-exchange"}, KALMAN_CONFIG_Q0100),
            ({"price_type": "mark"}, KALMAN_CONFIG_Q0100),
            ({}, KALMAN_CONFIG_Q0400),
            ({}, KALMAN_CONFIG_Q0100),
        )
        for kwargs, config in changes:
            _, observation, state = transform_market_movement_with_kalman(
                _evaluation(5_000 if kwargs else 10_000, normalized=0.7, **kwargs),
                config,
                original_state,
            )
            self.assertIsNone(observation.innovation)
            self.assertEqual(state.level, 0.7)
            self.assertEqual(state.trend, 0.0)
            self.assertEqual((state.p00, state.p01, state.p11), (0.25, 0.0, 0.25))

    def test_covariance_stays_valid_and_invalid_state_is_rejected(self):
        state = None
        for index, value in enumerate((1.0, -1.0) * 12):
            _, _, state = transform_market_movement_with_kalman(
                _evaluation(index * 5_000, normalized=value),
                KALMAN_CONFIG_Q0100,
                state,
            )
            self.assertGreaterEqual(state.p00, -1e-12)
            self.assertGreaterEqual(state.p11, -1e-12)
            self.assertGreaterEqual(state.p00 * state.p11 - state.p01 * state.p01, -1e-12)
        with self.assertRaisesRegex(ValueError, "positive semidefinite"):
            replace(state, p00=1.0, p01=2.0, p11=1.0)

    def test_runner_rejects_bad_chronology_and_preserves_prefix(self):
        points = (
            _point(0, normalized=0.0, partition="development"),
            _point(5_000, state="rise", normalized=0.6, raw=0.1,
                   partition="development"),
            _point(10_000, state="rise", normalized=0.6, raw=0.1,
                   partition="development"),
            _point(15_000, state="rise", normalized=0.6, raw=0.1,
                   partition="validation"),
        )
        full = run_market_state_kalman_experiment(points, KALMAN_CONFIG_Q0100)
        prefix = run_market_state_kalman_experiment(points[:3], KALMAN_CONFIG_Q0100)
        self.assertEqual(prefix.points, full.points[:3])
        self.assertEqual(set(full.summaries), {"all", "development", "validation", "test"})
        self.assertEqual(full.summaries["development"].unmatched_baseline_onset_count, 1)
        self.assertEqual(full.summaries["all"].matched_onset_count, 1)
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            run_market_state_kalman_experiment((_point(0), _point(0)), KALMAN_CONFIG_Q0100)
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            run_market_state_kalman_experiment((_point(5_000), _point(0)), KALMAN_CONFIG_Q0100)
        with self.assertRaisesRegex(ValueError, "exactly 5000"):
            run_market_state_kalman_experiment((_point(0), _point(10_000)), KALMAN_CONFIG_Q0100)
        with self.assertRaisesRegex(ValueError, "development, validation"):
            run_market_state_kalman_experiment(
                (_point(0, partition="validation"), _point(5_000)),
                KALMAN_CONFIG_Q0100,
            )

    def test_identical_inputs_are_deterministic(self):
        points = (
            _point(0, normalized=0.0),
            _point(5_000, state="rise", normalized=0.6, raw=0.1),
            _point(10_000, state="rise", normalized=0.6, raw=0.1),
        )
        first = run_market_state_kalman_experiment(points, KALMAN_CONFIG_Q0400)
        second = run_market_state_kalman_experiment(points, KALMAN_CONFIG_Q0400)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
