from dataclasses import replace
from decimal import Decimal
import unittest

from market_analysis.experiments.market_state_cusum import (
    CUSUM_AMBIGUOUS,
    CUSUM_CONFIG_K010_H075,
    CUSUM_CONFIG_K010_H150,
    CUSUM_CONFIG_K025_H150,
    CUSUM_DOWN_SHIFT,
    CUSUM_NONE,
    CUSUM_UNAVAILABLE,
    CUSUM_UP_SHIFT,
    CUSUMConfig,
    detection_regions,
    run_market_state_cusum_experiment,
    transform_market_movement_with_cusum,
)
from market_analysis.experiments.market_state_common import MarketStateExperimentPoint
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
                movement_config_version="market-movement-config-v1",
                provider="binance-usdm", exchange="binance", price_type="trade"):
    windows = {
        minute: _window(boundary, minute, state, normalized, raw, universe,
                        universe_id, universe_version, movement_config_version,
                        provider, exchange, price_type)
        for minute in (1, 5, 15)
    }
    return MarketMovementEvaluation(
        algorithm_version="market-movement-v1",
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
           movement_config_version="market-movement-config-v1",
           provider="binance-usdm", exchange="binance", price_type="trade",
           partition="development"):
    return MarketStateExperimentPoint(
        _evaluation(boundary, state, normalized, raw, universe, universe_id,
                    universe_version, movement_config_version,
                    provider, exchange, price_type),
        _evidence(universe), partition,
    )


class MarketStateCUSUMExperimentTests(unittest.TestCase):
    def test_zero_input_keeps_both_accumulators_zero(self):
        observation, state = transform_market_movement_with_cusum(
            _evaluation(0, normalized=0.0), CUSUM_CONFIG_K010_H075)
        self.assertEqual(observation.direction_state, CUSUM_NONE)
        self.assertEqual(observation.positive_accumulator, 0.0)
        self.assertEqual(observation.negative_accumulator, 0.0)
        self.assertEqual(state.positive_accumulator, 0.0)
        self.assertEqual(state.negative_accumulator, 0.0)

    def test_positive_accumulation_and_inclusive_threshold(self):
        state = None
        observation = None
        for index in range(5):
            observation, state = transform_market_movement_with_cusum(
                _evaluation(index * 5_000, normalized=0.25),
                CUSUM_CONFIG_K010_H075,
                state,
            )
        self.assertAlmostEqual(observation.positive_accumulator, 0.75)
        self.assertEqual(observation.negative_accumulator, 0.0)
        self.assertEqual(observation.direction_state, CUSUM_UP_SHIFT)

    def test_negative_accumulation_is_symmetric(self):
        state = None
        observation = None
        for index in range(5):
            observation, state = transform_market_movement_with_cusum(
                _evaluation(index * 5_000, normalized=-0.25),
                CUSUM_CONFIG_K010_H075,
                state,
            )
        self.assertAlmostEqual(observation.negative_accumulator, 0.75)
        self.assertEqual(observation.positive_accumulator, 0.0)
        self.assertEqual(observation.direction_state, CUSUM_DOWN_SHIFT)

    def test_natural_decay_floors_at_zero(self):
        state = None
        for index in range(5):
            _, state = transform_market_movement_with_cusum(
                _evaluation(index * 5_000, normalized=0.25),
                CUSUM_CONFIG_K010_H075,
                state,
            )
        decayed, state = transform_market_movement_with_cusum(
            _evaluation(25_000, normalized=0.0), CUSUM_CONFIG_K010_H075, state)
        cleared, _ = transform_market_movement_with_cusum(
            _evaluation(30_000, normalized=-1.0), CUSUM_CONFIG_K010_H075, state)
        self.assertAlmostEqual(decayed.positive_accumulator, 0.65)
        self.assertEqual(cleared.positive_accumulator, 0.0)

    def test_both_thresholds_are_ambiguous(self):
        _, state = transform_market_movement_with_cusum(
            _evaluation(0, normalized=0.25), CUSUM_CONFIG_K010_H075)
        state = replace(state, positive_accumulator=1.0,
                        negative_accumulator=1.0,
                        direction_state=CUSUM_UP_SHIFT)
        observation, _ = transform_market_movement_with_cusum(
            _evaluation(5_000, normalized=0.0), CUSUM_CONFIG_K010_H075, state)
        self.assertEqual(observation.direction_state, CUSUM_AMBIGUOUS)
        self.assertFalse(observation.directional_onset)

    def test_preregistered_configurations_are_distinct_without_winner_selection(self):
        values = []
        for config in (CUSUM_CONFIG_K010_H075, CUSUM_CONFIG_K010_H150,
                       CUSUM_CONFIG_K025_H150):
            state = None
            observation = None
            for index in range(10):
                observation, state = transform_market_movement_with_cusum(
                    _evaluation(index * 5_000, normalized=0.25), config, state)
            values.append(observation.direction_state)
        self.assertEqual(values, [CUSUM_UP_SHIFT, CUSUM_UP_SHIFT, CUSUM_NONE])
        self.assertEqual(len({config.version for config in (
            CUSUM_CONFIG_K010_H075, CUSUM_CONFIG_K010_H150, CUSUM_CONFIG_K025_H150)}), 3)
        with self.assertRaises(ValueError):
            CUSUMConfig("custom", 0.0, 0.05, 0.5)

    def test_unavailable_input_resets_and_next_value_restarts_from_zero(self):
        _, state = transform_market_movement_with_cusum(
            _evaluation(0, normalized=0.25), CUSUM_CONFIG_K010_H075)
        missing_evaluation = _evaluation(5_000, normalized=0.0)
        missing_evaluation = replace(
            missing_evaluation,
            windows={
                **missing_evaluation.windows,
                5: replace(
                    missing_evaluation.windows[5],
                    aggregates=replace(
                        missing_evaluation.windows[5].aggregates,
                        median_normalized_movement=Metric.missing("missing"),
                    ),
                ),
            },
        )
        missing, reset = transform_market_movement_with_cusum(
            missing_evaluation, CUSUM_CONFIG_K010_H075, state)
        restarted, _ = transform_market_movement_with_cusum(
            _evaluation(10_000, normalized=0.25), CUSUM_CONFIG_K010_H075, reset)
        self.assertEqual(missing.direction_state, CUSUM_UNAVAILABLE)
        self.assertIsNone(reset)
        self.assertAlmostEqual(restarted.positive_accumulator, 0.15)

    def test_identity_and_nonconsecutive_boundary_changes_reset_state(self):
        _, state = transform_market_movement_with_cusum(
            _evaluation(0, normalized=0.25), CUSUM_CONFIG_K010_H075)
        gap, state = transform_market_movement_with_cusum(
            _evaluation(10_000, normalized=0.25), CUSUM_CONFIG_K010_H075, state)
        self.assertAlmostEqual(gap.positive_accumulator, 0.15)
        changed, state = transform_market_movement_with_cusum(
            _evaluation(15_000, normalized=0.25, universe_id="u2"),
            CUSUM_CONFIG_K010_H075, state)
        self.assertAlmostEqual(changed.positive_accumulator, 0.15)
        changed, state = transform_market_movement_with_cusum(
            _evaluation(20_000, normalized=0.25, universe_version="v2",
                        provider="other-provider", exchange="other-exchange",
                        price_type="mark", movement_config_version="other-config"),
            CUSUM_CONFIG_K010_H075, state)
        self.assertAlmostEqual(changed.positive_accumulator, 0.15)
        changed, _ = transform_market_movement_with_cusum(
            _evaluation(25_000, normalized=0.25), CUSUM_CONFIG_K010_H150, state)
        self.assertAlmostEqual(changed.positive_accumulator, 0.15)

    def test_transform_does_not_mutate_canonical_evaluation(self):
        evaluation = _evaluation(0, normalized=0.25)
        original = evaluation
        transform_market_movement_with_cusum(evaluation, CUSUM_CONFIG_K010_H075)
        self.assertEqual(evaluation, original)

    def test_one_continuous_region_has_one_onset_and_opposite_replaces_it(self):
        points = tuple(
            _point(index * 5_000, normalized=(0.25 if index < 5 else -0.25))
            for index in range(10)
        )
        result = run_market_state_cusum_experiment(points, CUSUM_CONFIG_K010_H075)
        regions = detection_regions(result.points)
        self.assertEqual(len(regions), 2)
        self.assertEqual(regions[0].direction, CUSUM_UP_SHIFT)
        self.assertEqual(regions[1].direction, CUSUM_DOWN_SHIFT)
        self.assertEqual(result.summaries["all"].cusum_up_onset_count, 1)
        self.assertEqual(result.summaries["all"].cusum_down_onset_count, 1)

    def test_ambiguous_and_unavailable_close_regions(self):
        points = tuple(_point(index * 5_000, normalized=0.25) for index in range(5))
        result = run_market_state_cusum_experiment(points, CUSUM_CONFIG_K010_H075)
        ambiguous = replace(result.points[-1], cusum_direction_state=CUSUM_AMBIGUOUS)
        self.assertEqual(detection_regions(result.points[:-1] + (ambiguous,))[0].end_boundary_time_ms,
                         20_000)
        missing = replace(result.points[-1], cusum_direction_state=CUSUM_UNAVAILABLE,
                          cusum_available=False)
        self.assertEqual(detection_regions(result.points[:-1] + (missing,))[0].end_boundary_time_ms,
                         20_000)

    def test_final_active_region_is_censored_and_not_short_lived(self):
        result = run_market_state_cusum_experiment(
            tuple(_point(index * 5_000, normalized=0.25) for index in range(5)),
            CUSUM_CONFIG_K010_H075,
        )
        region = detection_regions(result.points)[0]
        self.assertIsNone(region.end_boundary_time_ms)
        self.assertEqual(result.summaries["all"].cusum_short_lived_closed_region_count, 0)

    def test_detection_before_baseline_onset_has_negative_delta(self):
        points = (
            _point(0, normalized=1.0, raw=0.0),
            _point(5_000, state="rise", normalized=0.6, raw=0.1),
            _point(10_000, state="rise", normalized=0.6, raw=0.1),
        )
        summary = run_market_state_cusum_experiment(
            points, CUSUM_CONFIG_K010_H075).summaries["all"]
        self.assertEqual(summary.matched_baseline_onset_count, 1)
        self.assertEqual(summary.unmatched_baseline_onset_count, 0)
        self.assertEqual(summary.median_signed_detection_delta_ms, -10_000.0)

    def test_detection_after_baseline_onset_has_positive_delta(self):
        points = tuple(_point(index * 5_000, state="rise", normalized=0.6, raw=0.1)
                       for index in range(4))
        summary = run_market_state_cusum_experiment(
            points, CUSUM_CONFIG_K010_H150).summaries["all"]
        self.assertEqual(summary.matched_baseline_onset_count, 1)
        self.assertEqual(summary.median_signed_detection_delta_ms, 5_000.0)

    def test_same_boundary_detection_has_zero_delta_and_overlap_denominators(self):
        points = tuple(_point(index * 5_000, state="rise", normalized=0.6, raw=0.1)
                       for index in range(3))
        summary = run_market_state_cusum_experiment(
            points, CUSUM_CONFIG_K010_H075).summaries["all"]
        self.assertEqual(summary.median_signed_detection_delta_ms, 0.0)
        self.assertEqual(summary.same_direction_overlap_fraction_of_cusum_active, 1.0)
        self.assertEqual(summary.same_direction_baseline_coverage_fraction, 1.0)

    def test_opposite_direction_region_is_unmatched(self):
        points = (
            _point(0, normalized=-1.0, raw=0.0),
            _point(5_000, state="rise", normalized=0.6, raw=0.1),
            _point(10_000, state="rise", normalized=0.6, raw=0.1),
        )
        summary = run_market_state_cusum_experiment(
            points, CUSUM_CONFIG_K010_H075).summaries["all"]
        self.assertEqual(summary.matched_baseline_onset_count, 0)
        self.assertEqual(summary.unmatched_cusum_detection_region_count, 1)

    def test_one_cusum_region_cannot_match_two_baseline_onsets(self):
        values = [
            ("neutral", 1.0, 0.0),
            ("rise", 0.6, 0.1), ("rise", 0.6, 0.1),
            ("neutral", 1.0, 0.0), ("neutral", 1.0, 0.0), ("neutral", 1.0, 0.0),
            ("rise", 0.6, 0.1), ("rise", 0.6, 0.1),
        ]
        points = tuple(_point(index * 5_000, state=state, normalized=normalized, raw=raw)
                       for index, (state, normalized, raw) in enumerate(values))
        summary = run_market_state_cusum_experiment(
            points, CUSUM_CONFIG_K010_H075).summaries["all"]
        self.assertEqual(summary.matched_baseline_onset_count, 1)
        self.assertEqual(summary.unmatched_baseline_onset_count, 1)

    def test_development_summary_cannot_use_validation_cusum_onset_or_future_close(self):
        points = (
            _point(0, state="rise", normalized=0.6, raw=0.1),
            _point(5_000, state="rise", normalized=0.6, raw=0.1),
            _point(10_000, state="rise", normalized=0.6, raw=0.1,
                   partition="validation"),
            _point(15_000, state="rise", normalized=0.6, raw=0.1,
                   partition="validation"),
        )
        result = run_market_state_cusum_experiment(points, CUSUM_CONFIG_K010_H150)
        development = result.summaries["development"]
        self.assertEqual(development.matched_baseline_onset_count, 0)
        self.assertEqual(development.unmatched_baseline_onset_count, 1)

    def test_future_baseline_close_cannot_make_development_region_short_lived(self):
        points = (
            _point(0, state="rise", normalized=0.6, raw=0.1),
            _point(5_000, state="rise", normalized=0.6, raw=0.1),
            _point(10_000, state="neutral", normalized=0.0, raw=0.0),
            _point(15_000, state="neutral", normalized=0.0, raw=0.0),
            _point(20_000, state="neutral", normalized=0.0, raw=0.0,
                   partition="validation"),
        )
        result = run_market_state_cusum_experiment(points, CUSUM_CONFIG_K010_H075)
        self.assertEqual(result.summaries["development"].baseline_short_lived_closed_episode_count,
                         0)
        self.assertEqual(result.summaries["all"].baseline_short_lived_closed_episode_count,
                         1)
