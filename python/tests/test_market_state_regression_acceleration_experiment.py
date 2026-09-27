"""Focused explicit-#71 fixtures for EXP-75-05. GitHub Verify runs these."""

from dataclasses import replace
from decimal import Decimal
import math
from types import SimpleNamespace
import unittest

from market_analysis.experiments.market_state_common import MarketStateExperimentPoint
from market_analysis.experiments.market_state_regression_acceleration import (
    REGRESSION_ACCELERATION_ALGORITHM_VERSION,
    REGRESSION_ACCELERATION_WARMING,
    REGRESSION_CONFIG_60S,
    REGRESSION_CONFIG_180S,
    REGRESSION_CONFIG_300S,
    REGRESSION_CONFIGURATIONS,
    REGRESSION_NUMERICAL_TOL,
    PaceChangeEvent,
    RegressionAccelerationConfig,
    RegressionSymbolHistory,
    RegressionVelocitySample,
    _match_pace_change_events,
    _pace_change_events,
    run_market_state_regression_acceleration_experiment,
    transform_market_movement_with_regression_acceleration,
)
from market_analysis.movement_classifier import SymbolSourceTimeEvidence
from market_analysis.movement_metrics import (
    BreadthSide,
    ExcludedSymbol,
    MarketMovementEvaluation,
    MarketMovementWindowResult,
    Metric,
    SymbolMovementResult,
    WindowAggregates,
    WindowBreadth,
)


SYMBOLS = ("S1", "S2", "S3", "S4", "S5")


def _side(count, denominator):
    return Metric.present(BreadthSide(count, count / denominator))


def _symbol(symbol, boundary, minute, velocity, acceleration, identity, *, included=True):
    return SymbolMovementResult(
        symbol=symbol, instrument_id=symbol,
        provider=identity.get("provider", "binance-usdm"),
        exchange=identity.get("exchange", "binance"),
        price_type=identity.get("price_type", "trade"),
        window_minutes=minute, evaluation_boundary_time_ms=boundary,
        included=included,
        exclusion_reasons=() if included else ("TEST_EXCLUDED",),
        current_return=Metric.present(0.1),
        previous_return=Metric.present(0.05),
        velocity=velocity if isinstance(velocity, Metric) else Metric.present(velocity),
        previous_velocity=Metric.present(0.005),
        acceleration=Metric.present(acceleration),
        historical_median=Metric.present(0.0),
        historical_mad=Metric.present(0.1),
        normalized_z=Metric.present(1.0),
        direction=Metric.present("RISING") if included else Metric.missing("TEST_EXCLUDED"),
        material_rising=included, material_falling=False,
        current_notional_volume=Metric.present(Decimal("10")),
        rvol=Metric.present(1.0),
        cross_sectional_z=Metric.present(0.0),
        outlier_candidate=False,
    )


def _window(boundary, minute, symbols, velocities, acceleration, identity, excluded):
    included = tuple(symbol for symbol in symbols if symbol not in excluded)
    count = len(included)
    results = tuple(
        _symbol(symbol, boundary, minute, velocities.get(symbol, 0.0),
                acceleration, identity, included=symbol in included)
        for symbol in symbols
    )
    breadth = WindowBreadth(
        available=bool(count), reason=None if count else "TEST_EMPTY",
        denominator=count, flat=_side(0, count) if count else Metric.missing("TEST_EMPTY"),
        rising=_side(count, count) if count else Metric.missing("TEST_EMPTY"),
        falling=_side(0, count) if count else Metric.missing("TEST_EMPTY"),
        material_rising=_side(count, count) if count else Metric.missing("TEST_EMPTY"),
        material_falling=_side(0, count) if count else Metric.missing("TEST_EMPTY"),
    )
    aggregates = WindowAggregates(
        median_normalized_movement=Metric.present(1.0),
        median_raw_return=Metric.present(0.1),
        trimmed_mean_normalized_movement=Metric.present(1.0),
        liquidity_weighted_normalized_movement=Metric.present(1.0),
        liquidity_weights=Metric.present(tuple((symbol, 1 / count) for symbol in included)),
        dispersion_mad_normalized_movement=Metric.present(0.0),
    )
    return MarketMovementWindowResult(
        algorithm_version=identity.get("movement_algorithm_version", "market-movement-v1"),
        config_version=identity.get("movement_config_version", "market-movement-config-v1"),
        universe_id=identity.get("universe_id", "u1"),
        universe_version=identity.get("universe_version", "v1"),
        configured_universe=symbols, included_symbols=included,
        excluded_symbols=tuple(ExcludedSymbol(symbol, ("TEST_EXCLUDED",))
                               for symbol in symbols if symbol in excluded),
        window_minutes=minute,
        provider=identity.get("provider", "binance-usdm"),
        exchange=identity.get("exchange", "binance"),
        price_type=identity.get("price_type", "trade"),
        evaluation_boundary_time_ms=boundary,
        historical_lookback_ms=100, minimum_historical_coverage_ms=50,
        market_wide_eligible=count >= 5,
        eligible_count=count, eligible_fraction=count / len(symbols),
        symbols=results, breadth=breadth, aggregates=aggregates,
    )


def _evaluation(boundary, velocity=0.0, acceleration=-1.0, *, symbols=SYMBOLS,
                identity=None, excluded=(), velocity_overrides=None):
    identity = identity or {}
    velocities = {symbol: velocity for symbol in symbols}
    velocities.update(velocity_overrides or {})
    windows = {minute: _window(boundary, minute, symbols, velocities,
                               acceleration, identity, excluded)
               for minute in (1, 5, 15)}
    return MarketMovementEvaluation(
        algorithm_version=identity.get("movement_algorithm_version", "market-movement-v1"),
        config_version=identity.get("movement_config_version", "market-movement-config-v1"),
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


def _point(index, velocity=0.0, acceleration=-1.0, partition="development",
           *, symbols=SYMBOLS, identity=None, excluded=(), velocity_overrides=None):
    evaluation = _evaluation(index * 5_000, velocity, acceleration, symbols=symbols,
                             identity=identity, excluded=excluded,
                             velocity_overrides=velocity_overrides)
    source = tuple(SymbolSourceTimeEvidence(symbol, None, None, None) for symbol in symbols)
    return MarketStateExperimentPoint(evaluation, source, partition)


def _transform_series(velocities, config, *, acceleration=-1.0):
    state = None
    outputs = []
    for index, velocity in enumerate(velocities):
        result, state = transform_market_movement_with_regression_acceleration(
            _evaluation(index * 5_000, velocity, acceleration), config, state)
        outputs.append((result, state))
    return tuple(outputs)


def _candidate_acceleration(evaluation, symbol="S1"):
    return next(item.acceleration for item in evaluation.windows[5].symbols
                if item.symbol == symbol)


def _event(boundary, target="ACCELERATING", partition="development"):
    return PaceChangeEvent(boundary, target, partition)


def _direction_point(index, direction):
    point = _point(index, float(index))
    if direction == "rise":
        return point
    original = point.movement_evaluation
    count = len(SYMBOLS)
    windows = {}
    for minute, window in original.windows.items():
        state = "FALLING" if direction == "drop" else "FLAT"
        symbols = tuple(replace(
            item,
            direction=Metric.present(state),
            current_return=Metric.present(-0.1 if direction == "drop" else 0.0),
            material_rising=False,
            material_falling=direction == "drop",
        ) for item in window.symbols)
        breadth = replace(
            window.breadth,
            rising=_side(0, count),
            falling=_side(count if direction == "drop" else 0, count),
            flat=_side(count if direction == "neutral" else 0, count),
            material_rising=_side(0, count),
            material_falling=_side(count if direction == "drop" else 0, count),
        )
        aggregates = replace(
            window.aggregates,
            median_raw_return=Metric.present(-0.1 if direction == "drop" else 0.0),
            median_normalized_movement=Metric.present(-1.0 if direction == "drop" else 0.0),
        )
        windows[minute] = replace(window, symbols=symbols,
                                  breadth=breadth, aggregates=aggregates)
    return replace(point, movement_evaluation=replace(original, windows=windows))


class RegressionAccelerationExperimentTests(unittest.TestCase):
    def test_fixed_config_identity_and_reject_arbitrary_models(self):
        self.assertEqual(tuple(config.window_points for config in REGRESSION_CONFIGURATIONS),
                         (12, 36, 60))
        for config in REGRESSION_CONFIGURATIONS:
            self.assertIn("canonical-5m-velocity", config.version)
            self.assertIn("estimator-ols", config.version)
            self.assertIn("weighting-uniform", config.version)
            self.assertIn("spacing-5000ms", config.version)
            self.assertIn(f"points-{config.window_points}", config.version)
        for changed in (
            {"version": "custom", "window_points": 12},
            {"version": REGRESSION_CONFIG_60S.version, "window_points": 13},
            {"version": REGRESSION_CONFIG_60S.version, "window_points": 12,
             "weighting": "exponential"},
            {"version": REGRESSION_CONFIG_60S.version, "window_points": 12,
             "estimator": "huber"},
            {"version": REGRESSION_CONFIG_60S.version, "window_points": 12,
             "input_series": "raw-price"},
            {"version": REGRESSION_CONFIG_60S.version, "window_points": 12,
             "spacing_ms": 1_000},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                RegressionAccelerationConfig(**changed)

    def test_exact_ols_positive_negative_constant_and_warming(self):
        for velocities, expected in (
            (tuple(range(12)), 0.2),
            (tuple(range(11, -1, -1)), -0.2),
            ((0.25,) * 12, 0.0),
        ):
            with self.subTest(velocities=velocities):
                outputs = _transform_series(velocities, REGRESSION_CONFIG_60S)
                for candidate, state in outputs[:-1]:
                    self.assertEqual(_candidate_acceleration(candidate).reason,
                                     REGRESSION_ACCELERATION_WARMING)
                    self.assertLess(len(state.symbol_histories[0].samples), 12)
                metric = _candidate_acceleration(outputs[-1][0])
                self.assertTrue(metric.available)
                self.assertTrue(math.isclose(metric.value, expected, abs_tol=REGRESSION_NUMERICAL_TOL))

    def test_each_window_warms_until_nth_and_linear_slope_matches(self):
        # v(t) = 0.125 * elapsed_seconds + 2; independent expected slope 0.125.
        velocities = tuple(2 + 0.125 * 5 * index for index in range(60))
        for config in REGRESSION_CONFIGURATIONS:
            with self.subTest(config=config.window_points):
                outputs = _transform_series(velocities, config)
                self.assertFalse(_candidate_acceleration(outputs[config.window_points - 2][0]).available)
                for candidate, state in outputs[config.window_points - 1:]:
                    self.assertAlmostEqual(_candidate_acceleration(candidate).value, 0.125,
                                           delta=REGRESSION_NUMERICAL_TOL)
                    self.assertEqual(len(state.symbol_histories[0].samples), config.window_points)

    def test_constant_velocity_is_zero_for_all_preregistered_windows(self):
        for config in REGRESSION_CONFIGURATIONS:
            with self.subTest(window_points=config.window_points):
                candidate = _transform_series((0.25,) * config.window_points, config)[-1][0]
                self.assertAlmostEqual(_candidate_acceleration(candidate).value, 0.0,
                                       delta=REGRESSION_NUMERICAL_TOL)

    def test_shorter_window_reacts_more_to_recent_ramp(self):
        velocities = (0.0,) * 48 + tuple(float(index) for index in range(12))
        slopes = tuple(_candidate_acceleration(_transform_series(velocities, config)[-1][0]).value
                       for config in REGRESSION_CONFIGURATIONS)
        self.assertGreater(abs(slopes[0]), abs(slopes[1]))
        self.assertGreater(abs(slopes[1]), abs(slopes[2]))

    def test_symbol_missing_clears_only_its_history(self):
        state = _transform_series(range(12), REGRESSION_CONFIG_60S)[-1][1]
        invalid = _evaluation(12 * 5_000, 12.0,
                              velocity_overrides={"S1": Metric.missing("VELOCITY_UNAVAILABLE")})
        candidate, state = transform_market_movement_with_regression_acceleration(
            invalid, REGRESSION_CONFIG_60S, state)
        self.assertEqual(len(state.symbol_histories[0].samples), 0)
        self.assertEqual(len(state.symbol_histories[1].samples), 12)
        self.assertEqual(_candidate_acceleration(candidate, "S1").reason,
                         REGRESSION_ACCELERATION_WARMING)
        self.assertTrue(_candidate_acceleration(candidate, "S2").available)
        candidate, state = transform_market_movement_with_regression_acceleration(
            _evaluation(13 * 5_000, 13.0), REGRESSION_CONFIG_60S, state)
        self.assertEqual(len(state.symbol_histories[0].samples), 1)
        self.assertEqual(len(state.symbol_histories[1].samples), 12)
        self.assertFalse(_candidate_acceleration(candidate, "S1").available)
        self.assertTrue(_candidate_acceleration(candidate, "S2").available)
        excluded, state = transform_market_movement_with_regression_acceleration(
            _evaluation(14 * 5_000, 14.0, excluded=("S1",)), REGRESSION_CONFIG_60S, state)
        self.assertEqual(state.symbol_histories[0].samples, ())
        self.assertEqual(_candidate_acceleration(excluded, "S1"),
                         _evaluation(14 * 5_000, 14.0, excluded=("S1",)).windows[5].symbols[0].acceleration)
        self.assertEqual(len(state.symbol_histories[1].samples), 12)

    def test_nonfinite_and_nonnumeric_velocity_clear_only_affected_symbol(self):
        warm_state = _transform_series(range(12), REGRESSION_CONFIG_60S)[-1][1]
        for invalid in (float("nan"), float("inf"), "not-a-number", 10 ** 1_000):
            with self.subTest(invalid=str(invalid)[:40]):
                evaluation = _evaluation(12 * 5_000, 12.0,
                                         velocity_overrides={"S1": Metric.present(invalid)})
                candidate, next_state = transform_market_movement_with_regression_acceleration(
                    evaluation, REGRESSION_CONFIG_60S, warm_state)
                self.assertEqual(next_state.symbol_histories[0].samples, ())
                self.assertEqual(len(next_state.symbol_histories[1].samples), 12)
                self.assertEqual(_candidate_acceleration(candidate).reason,
                                 REGRESSION_ACCELERATION_WARMING)

    def test_scope_changes_and_standalone_gap_reset_with_current_sample(self):
        state = _transform_series(range(12), REGRESSION_CONFIG_60S)[-1][1]
        changes = (
            {"movement_algorithm_version": "another-movement"},
            {"movement_config_version": "another-config"},
            {"universe_id": "u2"},
            {"universe_version": "v2"},
            {"provider": "other-provider"},
            {"exchange": "other-exchange"},
            {"price_type": "mark"},
        )
        for identity in changes:
            with self.subTest(identity=identity):
                _, reset = transform_market_movement_with_regression_acceleration(
                    _evaluation(12 * 5_000, 12.0, identity=identity),
                    REGRESSION_CONFIG_60S, state)
                self.assertEqual(tuple(len(item.samples) for item in reset.symbol_histories), (1,) * 5)
        _, reset = transform_market_movement_with_regression_acceleration(
            _evaluation(12 * 5_000, 12.0, symbols=tuple(reversed(SYMBOLS))),
            REGRESSION_CONFIG_60S, state)
        self.assertEqual(reset.configured_universe, tuple(reversed(SYMBOLS)))
        self.assertEqual(tuple(len(item.samples) for item in reset.symbol_histories), (1,) * 5)
        _, reset = transform_market_movement_with_regression_acceleration(
            _evaluation(12 * 5_000, 12.0), REGRESSION_CONFIG_180S, state)
        self.assertEqual(tuple(len(item.samples) for item in reset.symbol_histories), (1,) * 5)
        _, reset = transform_market_movement_with_regression_acceleration(
            _evaluation(14 * 5_000, 14.0), REGRESSION_CONFIG_60S, state)
        self.assertEqual(tuple(len(item.samples) for item in reset.symbol_histories), (1,) * 5)

    def test_malformed_external_history_is_rejected(self):
        sample = RegressionVelocitySample(5_000, 1.0)
        with self.assertRaises(ValueError):
            RegressionSymbolHistory("S1", (sample, RegressionVelocitySample(15_000, 2.0)))
        with self.assertRaises(ValueError):
            RegressionVelocitySample(5_000, float("nan"))
        state = _transform_series(range(12), REGRESSION_CONFIG_60S)[-1][1]
        for change in (
            {"symbol_histories": tuple(reversed(state.symbol_histories))},
            {"symbol_histories": (RegressionSymbolHistory("S1", (sample,)),
                                  *state.symbol_histories[1:])},
            {"symbol_histories": (RegressionSymbolHistory(
                "S1", tuple(RegressionVelocitySample(i * 5_000, float(i))
                            for i in range(13))), *state.symbol_histories[1:])},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(state, **change)

    def test_candidate_changes_only_included_5m_acceleration_and_identity(self):
        original = _evaluation(0, 0.0)
        candidate, state = transform_market_movement_with_regression_acceleration(
            original, REGRESSION_CONFIG_60S)
        self.assertEqual(original.algorithm_version, "market-movement-v1")
        self.assertEqual(candidate.algorithm_version, REGRESSION_ACCELERATION_ALGORITHM_VERSION)
        self.assertEqual(candidate.config_version, REGRESSION_CONFIG_60S.version)
        for minute in (1, 15):
            self.assertEqual(candidate.windows[minute].symbols, original.windows[minute].symbols)
            self.assertEqual(candidate.windows[minute].breadth, original.windows[minute].breadth)
            self.assertEqual(candidate.windows[minute].aggregates, original.windows[minute].aggregates)
        baseline_window, candidate_window = original.windows[5], candidate.windows[5]
        self.assertEqual(candidate_window.included_symbols, baseline_window.included_symbols)
        self.assertEqual(candidate_window.excluded_symbols, baseline_window.excluded_symbols)
        self.assertEqual(candidate_window.market_wide_eligible, baseline_window.market_wide_eligible)
        self.assertEqual(candidate_window.eligible_count, baseline_window.eligible_count)
        self.assertEqual(candidate_window.breadth, baseline_window.breadth)
        self.assertEqual(candidate_window.aggregates, baseline_window.aggregates)
        for before, after in zip(baseline_window.symbols, candidate_window.symbols):
            self.assertEqual(replace(after, acceleration=before.acceleration), before)
            self.assertTrue(after.included)
            self.assertEqual(after.acceleration.reason, REGRESSION_ACCELERATION_WARMING)
        self.assertEqual(tuple(len(history.samples) for history in state.symbol_histories), (1,) * 5)

    def test_warming_preserves_direction_and_isolates_pace(self):
        points = tuple(_point(index, float(index), acceleration=-1.0)
                       for index in range(12))
        result = run_market_state_regression_acceleration_experiment(points,
                                                                     REGRESSION_CONFIG_60S)
        first, last = result.paired_points[0], result.paired_points[-1]
        self.assertIs(first.baseline_evaluation, points[0].movement_evaluation)
        self.assertEqual(first.baseline_primary.direction_state, "BROAD_RISE")
        self.assertEqual(first.candidate_primary.direction_state, "BROAD_RISE")
        self.assertEqual(first.baseline_primary.pace.value, "DECELERATING")
        self.assertEqual(first.candidate_primary.pace.reason, "ACCELERATION_UNAVAILABLE")
        self.assertEqual(last.baseline_primary.pace.value, "DECELERATING")
        self.assertEqual(last.candidate_primary.pace.value, "ACCELERATING")
        self.assertEqual(last.regression_observations[0].history_count, 12)
        self.assertTrue(last.regression_observations[0].regression_available)
        self.assertEqual(dict(result.summaries["all"].direction_state_disagreement_count),
                         {1: 0, 5: 0, 15: 0})
        self.assertEqual(result.summaries["all"].candidate_regression_warming_count, 11)
        self.assertEqual(result.summaries["all"].candidate_regression_ready_count, 1)
        self.assertEqual(result.summaries["all"].pace_disagreement_count, 1)
        self.assertEqual(result.summaries["all"].symbol_acceleration_sign_disagreement_count, 5)

    def test_lifecycle_strength_can_differ_without_directional_episode_shift(self):
        points = tuple(_point(index, float(index),
                              acceleration=1.0 if index >= 14 else -1.0)
                       for index in range(17))
        result = run_market_state_regression_acceleration_experiment(points,
                                                                     REGRESSION_CONFIG_60S)
        summary = result.summaries["all"]
        self.assertEqual(summary.baseline_directional_onset_count, 1)
        self.assertEqual(summary.candidate_directional_onset_count, 1)
        self.assertEqual(summary.baseline_episode_count, summary.candidate_episode_count)
        self.assertEqual(summary.baseline_strengthened_count, 1)
        self.assertEqual(summary.candidate_strengthened_count, 0)
        baseline_directional = tuple((point.evaluation_boundary_time_ms,
                                      transition.transition)
                                     for point in result.paired_points
                                     for transition in point.baseline_transitions
                                     if transition.transition in ("STARTED", "REVERSED", "ENDED"))
        candidate_directional = tuple((point.evaluation_boundary_time_ms,
                                       transition.transition)
                                      for point in result.paired_points
                                      for transition in point.candidate_transitions
                                      if transition.transition in ("STARTED", "REVERSED", "ENDED"))
        self.assertEqual(baseline_directional, candidate_directional)

    def test_started_reversed_and_ended_boundaries_remain_identical(self):
        states = ("rise", "rise", "drop", "drop", "neutral", "neutral", "neutral")
        result = run_market_state_regression_acceleration_experiment(
            tuple(_direction_point(index, state) for index, state in enumerate(states)),
            REGRESSION_CONFIG_60S,
        )
        baseline = tuple((point.evaluation_boundary_time_ms, event.transition)
                         for point in result.paired_points
                         for event in point.baseline_transitions
                         if event.transition in ("STARTED", "REVERSED", "ENDED"))
        candidate = tuple((point.evaluation_boundary_time_ms, event.transition)
                          for point in result.paired_points
                          for event in point.candidate_transitions
                          if event.transition in ("STARTED", "REVERSED", "ENDED"))
        self.assertEqual(baseline, candidate)
        self.assertEqual(tuple(name for _, name in baseline),
                         ("STARTED", "REVERSED", "ENDED"))

    def test_pace_events_reset_after_unavailable_and_match_signed_deltas(self):
        def fake(boundary, pace):
            metric = Metric.present(pace) if pace else Metric.missing("UNAVAILABLE")
            classification = SimpleNamespace(windows={5: SimpleNamespace(pace=metric)})
            return SimpleNamespace(evaluation_boundary_time_ms=boundary,
                                   partition="development", baseline_classification=classification)
        events = _pace_change_events(tuple(fake(i * 5_000, pace) for i, pace in enumerate(
            ("MIXED", "ACCELERATING", None, "DECELERATING", "MIXED"))), "baseline")
        self.assertEqual(tuple(event.evaluation_boundary_time_ms for event in events),
                         (5_000, 20_000))
        for delta in (-5_000, 0, 5_000):
            with self.subTest(delta=delta):
                baseline = (_event(100_000),)
                candidate = (_event(100_000 + delta),)
                match = _match_pace_change_events(baseline, candidate, candidate)
                self.assertEqual(match.matches[0].signed_delta_ms, delta)
                self.assertEqual(match.median_signed_delta_ms, delta)
        baseline = (_event(400_000),)
        candidate = (_event(95_000), _event(400_000, "DECELERATING"))
        match = _match_pace_change_events(baseline, candidate, candidate)
        self.assertEqual(match.unmatched_baseline_count, 1)
        self.assertEqual(match.unmatched_candidate_count, 2)
        baseline = (_event(100_000), _event(105_000))
        candidate = (_event(102_500),)
        match = _match_pace_change_events(baseline, candidate, candidate)
        self.assertEqual(len(match.matches), 1)
        self.assertEqual(match.unmatched_baseline_count, 1)
        tied = (_event(95_000), _event(105_000))
        match = _match_pace_change_events((_event(100_000),), tied, tied)
        self.assertEqual(match.matches[0].signed_delta_ms, -5_000)

    def test_cross_partition_matching_is_causal_and_attributed(self):
        dev_candidate = _event(100_000, partition="development")
        val_baseline = _event(105_000, partition="validation")
        match = _match_pace_change_events((val_baseline,), (dev_candidate,), ())
        self.assertEqual(match.matches[0].signed_delta_ms, -5_000)
        self.assertEqual(match.unmatched_candidate_count, 0)
        dev_baseline = _event(100_000, partition="development")
        match = _match_pace_change_events((dev_baseline,), (), ())
        self.assertEqual(match.unmatched_baseline_count, 1)
        points = tuple(_point(
            index,
            -100.0 if index == 11 else 1_000.0 if index == 12 else float(index),
            acceleration=1.0 if index >= 10 else -1.0,
            partition="development" if index < 12 else "validation",
        ) for index in range(13))
        result = run_market_state_regression_acceleration_experiment(
            points, REGRESSION_CONFIG_60S)
        self.assertEqual(result.summaries["development"].baseline_pace_change_event_count, 1)
        self.assertEqual(result.summaries["development"].candidate_pace_change_event_count, 0)
        self.assertEqual(result.summaries["development"].matched_baseline_pace_change_count, 0)
        self.assertEqual(result.summaries["validation"].candidate_pace_change_event_count, 1)

    def test_prefix_and_partition_summaries_are_causal_and_deterministic(self):
        points = tuple(_point(index, float(index), partition=(
            "development" if index < 12 else "validation" if index < 15 else "test"))
            for index in range(18))
        full = run_market_state_regression_acceleration_experiment(points,
                                                                    REGRESSION_CONFIG_60S)
        again = run_market_state_regression_acceleration_experiment(points,
                                                                     REGRESSION_CONFIG_60S)
        dev = run_market_state_regression_acceleration_experiment(points[:12],
                                                                   REGRESSION_CONFIG_60S)
        validation = run_market_state_regression_acceleration_experiment(points[:15],
                                                                          REGRESSION_CONFIG_60S)
        self.assertEqual(full.paired_points, again.paired_points)
        self.assertEqual(dict(full.summaries), dict(again.summaries))
        self.assertEqual(full.paired_points[:12], dev.paired_points)
        self.assertEqual(full.paired_points[:15], validation.paired_points)
        self.assertEqual(full.summaries["development"], dev.summaries["development"])
        self.assertEqual(full.summaries["validation"], validation.summaries["validation"])
        self.assertEqual(full.summaries["test"].evaluation_count, 3)
        for bad in (
            (points[0], points[0]),
            (points[1], points[0]),
            (points[0], points[2]),
            (points[12], points[0]),
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                run_market_state_regression_acceleration_experiment(
                    bad, REGRESSION_CONFIG_60S)

    def test_empty_summary_has_no_fabricated_descriptive_medians(self):
        result = run_market_state_regression_acceleration_experiment(
            (), REGRESSION_CONFIG_60S)
        for summary in result.summaries.values():
            self.assertEqual(summary.evaluation_count, 0)
            self.assertEqual(summary.pace_disagreement_fraction, 0.0)
            self.assertIsNone(summary.median_absolute_median_acceleration_difference)
            self.assertIsNone(summary.median_signed_pace_change_delta_ms)
            self.assertIsNone(summary.median_absolute_pace_change_delta_ms)


if __name__ == "__main__":
    unittest.main()
