from dataclasses import replace
from decimal import Decimal
import math
from types import SimpleNamespace
import unittest

from market_analysis.experiments.market_state_bocpd import (
    BOCPD_ALGORITHM_VERSION,
    BOCPD_ALARM_THRESHOLD,
    BOCPD_CHANGE,
    BOCPD_CONFIG_ERL_30,
    BOCPD_CONFIG_ERL_60,
    BOCPD_CONFIG_ERL_120,
    BOCPD_CONFIGURATIONS,
    BOCPD_MIN_HISTORY_POINTS,
    BOCPD_NONE,
    BOCPD_OBSERVATION_VARIANCE,
    BOCPD_PRIOR_MEAN,
    BOCPD_PRIOR_MEAN_VARIANCE,
    BOCPD_RECENT_RUN_MAX_STEPS,
    BOCPD_UNAVAILABLE,
    BOCPD_WARMING,
    BOCPDDetectionRegion,
    BOCPDRunLengthHypothesis,
    _alarm_state,
    _match_v1_onsets,
    _posterior_diagnostics,
    _summary,
    detection_regions,
    run_market_state_bocpd_experiment,
    transform_market_movement_with_bocpd,
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
NUMERICAL_TOL = 1e-12


def _symbol(symbol, boundary, minute, direction, acceleration, identity):
    return SymbolMovementResult(
        symbol=symbol,
        instrument_id=symbol,
        provider=identity.get("provider", "binance-usdm"),
        exchange=identity.get("exchange", "binance"),
        price_type=identity.get("price_type", "trade"),
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


def _window(boundary, minute, normalized, state, identity):
    denominator = len(SYMBOLS)
    if state == "rise":
        direction, acceleration = "RISING", 1.0
        rising, falling = denominator, 0
        material_rising, material_falling = denominator, 0
    elif state == "drop":
        direction, acceleration = "FALLING", -1.0
        rising, falling = 0, denominator
        material_rising, material_falling = 0, denominator
    else:
        direction, acceleration = "FLAT", 0.0
        rising = falling = material_rising = material_falling = 0
    symbols = tuple(
        _symbol(symbol, boundary, minute, direction, acceleration, identity)
        for symbol in SYMBOLS
    )
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
    raw_return = 0.1 if state == "rise" else -0.1 if state == "drop" else 0.0
    aggregates = WindowAggregates(
        median_normalized_movement=Metric.present(normalized),
        median_raw_return=Metric.present(raw_return),
        trimmed_mean_normalized_movement=Metric.present(normalized),
        liquidity_weighted_normalized_movement=Metric.present(normalized),
        liquidity_weights=Metric.present(tuple((symbol, 1 / denominator)
                                               for symbol in SYMBOLS)),
        dispersion_mad_normalized_movement=Metric.present(0.1),
    )
    return MarketMovementWindowResult(
        algorithm_version=identity.get("movement_algorithm_version", "market-movement-v1"),
        config_version=identity.get("movement_config_version", "market-movement-config-v1"),
        universe_id=identity.get("universe_id", "u1"),
        universe_version=identity.get("universe_version", "v1"),
        configured_universe=SYMBOLS,
        included_symbols=SYMBOLS,
        excluded_symbols=(),
        window_minutes=minute,
        provider=identity.get("provider", "binance-usdm"),
        exchange=identity.get("exchange", "binance"),
        price_type=identity.get("price_type", "trade"),
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


def _evaluation(boundary, normalized=0.0, state="neutral", identity=None):
    identity = identity or {}
    windows = {
        minute: _window(boundary, minute, normalized, state, identity)
        for minute in (1, 5, 15)
    }
    return MarketMovementEvaluation(
        algorithm_version=identity.get("movement_algorithm_version", "market-movement-v1"),
        config_version=identity.get("movement_config_version", "market-movement-config-v1"),
        universe_id=identity.get("universe_id", "u1"),
        universe_version=identity.get("universe_version", "v1"),
        configured_universe=SYMBOLS,
        provider=identity.get("provider", "binance-usdm"),
        exchange=identity.get("exchange", "binance"),
        price_type=identity.get("price_type", "trade"),
        evaluation_boundary_time_ms=boundary,
        historical_lookback_ms=100,
        minimum_historical_coverage_ms=50,
        windows=windows,
    )


def _point(boundary, value=0.0, state="neutral", partition="development", identity=None):
    evaluation = _evaluation(boundary, 0.0 if value is None else value, state, identity)
    if value is None:
        evaluation = _with_raw_metric(evaluation, Metric.missing("fixture_unavailable"))
    evidence = tuple(SymbolSourceTimeEvidence(symbol, 1, 2, 3) for symbol in SYMBOLS)
    return MarketStateExperimentPoint(evaluation, evidence, partition)


def _with_raw_metric(evaluation, metric):
    window = evaluation.windows[5]
    return replace(
        evaluation,
        windows={
            **evaluation.windows,
            5: replace(
                window,
                aggregates=replace(window.aggregates,
                                   median_normalized_movement=metric),
            ),
        },
    )


def _run(values, config, *, states=None, partitions=None, identities=None):
    states = states or ("neutral",) * len(values)
    partitions = partitions or ("development",) * len(values)
    identities = identities or ({},) * len(values)
    points = tuple(
        _point(index * 5_000, value, states[index], partitions[index], identities[index])
        for index, value in enumerate(values)
    )
    return points, run_market_state_bocpd_experiment(points, config)


def _fake_detector_point(boundary, state, partition="development", transitions=()):
    available = state != BOCPD_UNAVAILABLE
    observation = SimpleNamespace(
        available=available,
        detector_state=state,
        recent_change_probability=0.7 if state == BOCPD_CHANGE else 0.1,
        expected_run_length_steps=3.0,
        hypothesis_count=4,
    )
    return SimpleNamespace(
        evaluation_boundary_time_ms=boundary,
        partition=partition,
        bocpd_observation=observation,
        baseline_transitions=transitions,
        baseline_lifecycle_state=SimpleNamespace(active_episode=None),
    )


def _started(boundary, episode_id):
    return SimpleNamespace(
        transition="STARTED",
        episode_id=episode_id,
        previous_episode_id=None,
        episode_direction="BROAD_RISE",
        episode_start_boundary_time_ms=boundary,
        evaluation_boundary_time_ms=boundary,
    )


class MarketStateBOCPDExperimentTests(unittest.TestCase):
    def test_preregistered_configs_encode_fixed_model_and_hazards(self):
        self.assertEqual(
            tuple(config.expected_run_length_points for config in BOCPD_CONFIGURATIONS),
            (30, 60, 120),
        )
        self.assertEqual(
            tuple(config.hazard for config in BOCPD_CONFIGURATIONS),
            (1 / 30, 1 / 60, 1 / 120),
        )
        self.assertEqual(BOCPD_CONFIG_ERL_30.hazard, 1 / 30)
        self.assertEqual(BOCPD_CONFIG_ERL_60.hazard, 1 / 60)
        self.assertEqual(BOCPD_CONFIG_ERL_120.hazard, 1 / 120)
        self.assertEqual(BOCPD_PRIOR_MEAN, 0.0)
        self.assertEqual(BOCPD_PRIOR_MEAN_VARIANCE, 4.0)
        self.assertEqual(BOCPD_OBSERVATION_VARIANCE, 1.0)
        self.assertEqual(BOCPD_RECENT_RUN_MAX_STEPS, 2)
        self.assertEqual(BOCPD_ALARM_THRESHOLD, 0.50)
        self.assertEqual(BOCPD_MIN_HISTORY_POINTS, 6)
        with self.assertRaises(ValueError):
            replace(BOCPD_CONFIG_ERL_60, expected_run_length_points=61)
        with self.assertRaises(ValueError):
            replace(BOCPD_CONFIG_ERL_60, prior_mean_variance=3.0)
        with self.assertRaises(ValueError):
            replace(BOCPD_CONFIG_ERL_60, alarm_probability_threshold=0.49)
        with self.assertRaises(ValueError):
            replace(BOCPD_CONFIG_ERL_60, minimum_history_points=5)
        with self.assertRaises(ValueError):
            replace(BOCPD_CONFIG_ERL_60, version="unregistered")

    def test_first_observation_and_exact_two_step_posterior_reference(self):
        first, state = transform_market_movement_with_bocpd(
            _evaluation(0, 0.0), BOCPD_CONFIG_ERL_60,
        )
        self.assertEqual(first.detector_state, BOCPD_WARMING)
        self.assertEqual(first.observations_since_reset, 1)
        self.assertEqual(first.hypothesis_count, 2)
        self.assertEqual(tuple(item.run_length_steps for item in state.hypotheses), (0, 1))
        self.assertAlmostEqual(first.run_length_zero_probability, 1 / 60, delta=NUMERICAL_TOL)
        self.assertAlmostEqual(state.hypotheses[1].log_probability, math.log(59 / 60),
                               delta=NUMERICAL_TOL)
        self.assertEqual(state.hypotheses[0].posterior_mean, 0.0)
        self.assertEqual(state.hypotheses[0].posterior_mean_variance, 4.0)
        self.assertEqual(state.hypotheses[1].posterior_mean, 0.0)
        self.assertAlmostEqual(state.hypotheses[1].posterior_mean_variance, 0.8)

        second, state = transform_market_movement_with_bocpd(
            _evaluation(5_000, 3.0), BOCPD_CONFIG_ERL_60, state,
        )
        probabilities = tuple(math.exp(item.log_probability) for item in state.hypotheses)
        expected_probabilities = (
            0.016666666666666666,
            0.047155128832907886,
            0.9361782045004254,
        )
        for actual, expected in zip(probabilities, expected_probabilities):
            self.assertAlmostEqual(actual, expected, delta=NUMERICAL_TOL)
        expected_statistics = ((0.0, 4.0), (2.4, 0.8),
                               (1.3333333333333333, 0.4444444444444444))
        for hypothesis, (mean, variance) in zip(state.hypotheses, expected_statistics):
            self.assertAlmostEqual(hypothesis.posterior_mean, mean, delta=NUMERICAL_TOL)
            self.assertAlmostEqual(
                hypothesis.posterior_mean_variance, variance, delta=NUMERICAL_TOL,
            )
        self.assertEqual(second.observations_since_reset, 2)
        self.assertEqual(second.hypothesis_count, 3)
        self.assertEqual(second.detector_state, BOCPD_WARMING)

    def test_map_tie_and_alarm_threshold_use_fixed_deterministic_tolerance(self):
        equal_log_probability = math.log(1 / 3)
        hypotheses = tuple(
            BOCPDRunLengthHypothesis(run_length, equal_log_probability, 0.0, 1.0)
            for run_length in range(3)
        )
        self.assertEqual(_posterior_diagnostics(hypotheses)[2], 0)
        self.assertEqual(_alarm_state(6, BOCPD_ALARM_THRESHOLD), BOCPD_CHANGE)
        self.assertEqual(
            _alarm_state(6, BOCPD_ALARM_THRESHOLD - 0.5e-12), BOCPD_CHANGE,
        )
        self.assertEqual(
            _alarm_state(6, BOCPD_ALARM_THRESHOLD - 2e-12), BOCPD_NONE,
        )

    def test_run_length_zero_probability_tracks_constant_hazard(self):
        for config in BOCPD_CONFIGURATIONS:
            points, result = _run((0.0, 0.2, -0.1, 0.4, 0.0, 0.1, -0.3), config)
            self.assertEqual(len(result.points), len(points))
            for point in result.points:
                self.assertAlmostEqual(
                    point.bocpd_observation.run_length_zero_probability,
                    config.hazard,
                    delta=NUMERICAL_TOL,
                )

    def test_six_point_warmup_and_stable_series_for_all_hazards(self):
        _, warming_only = _run((0.0,) * 5, BOCPD_CONFIG_ERL_60)
        self.assertIsNone(warming_only.summaries["all"].median_recent_change_probability)
        self.assertIsNone(warming_only.summaries["all"].maximum_recent_change_probability)
        self.assertIsNone(warming_only.summaries["all"].median_expected_run_length_steps)
        self.assertIsNone(warming_only.summaries["all"].maximum_hypothesis_count)
        for config in BOCPD_CONFIGURATIONS:
            _, short = _run((0.0,) * 6, config)
            states = tuple(point.detector_state for point in short.points)
            self.assertEqual(states[:5], (BOCPD_WARMING,) * 5)
            self.assertEqual(states[5], BOCPD_NONE)

            _, stable = _run((0.0,) * 36, config)
            self.assertTrue(all(point.detector_state == BOCPD_WARMING
                                for point in stable.points[:5]))
            self.assertTrue(all(point.detector_state == BOCPD_NONE
                                for point in stable.points[5:]))
            self.assertGreaterEqual(
                stable.points[-1].bocpd_observation.map_run_length_steps,
                stable.points[5].bocpd_observation.map_run_length_steps,
            )
            self.assertEqual(
                stable.summaries["all"].bocpd_change_boundary_count, 0,
            )

    def test_hazard_sensitivity_orders_recent_posterior_mass(self):
        values = (0.0,) * 12 + (3.5,)
        recent = []
        for config in BOCPD_CONFIGURATIONS:
            _, result = _run(values, config)
            recent.append(result.points[-1].bocpd_observation.recent_change_probability)
        self.assertGreater(recent[0], recent[1])
        self.assertGreater(recent[1], recent[2])

    def test_primary_five_minute_median_is_the_only_bocpd_observation(self):
        first = _evaluation(0, 0.25)
        windows = dict(first.windows)
        windows[1] = replace(
            windows[1],
            aggregates=replace(windows[1].aggregates,
                               median_normalized_movement=Metric.present(80.0)),
        )
        windows[15] = replace(
            windows[15],
            aggregates=replace(windows[15].aggregates,
                               median_normalized_movement=Metric.present(-80.0)),
        )
        changed_other_windows = replace(first, windows=windows)
        observation_a, state_a = transform_market_movement_with_bocpd(
            first, BOCPD_CONFIG_ERL_60,
        )
        observation_b, state_b = transform_market_movement_with_bocpd(
            changed_other_windows, BOCPD_CONFIG_ERL_60,
        )
        self.assertEqual(observation_a, observation_b)
        self.assertEqual(state_a, state_b)

    def test_unavailable_non_numeric_and_non_finite_values_reset_state(self):
        _, previous = transform_market_movement_with_bocpd(
            _evaluation(0), BOCPD_CONFIG_ERL_60,
        )
        for metric in (Metric.missing("unavailable"),
                       Metric.present("not-a-number"),
                       Metric.present(float("nan")),
                       Metric.present(float("inf"))):
            observation, next_state = transform_market_movement_with_bocpd(
                _with_raw_metric(_evaluation(5_000), metric),
                BOCPD_CONFIG_ERL_60,
                previous,
            )
            self.assertEqual(observation.detector_state, BOCPD_UNAVAILABLE)
            self.assertIsNone(observation.run_length_zero_probability)
            self.assertIsNone(observation.recent_change_probability)
            self.assertIsNone(observation.map_run_length_steps)
            self.assertIsNone(observation.expected_run_length_steps)
            self.assertIsNone(next_state)

        unavailable, reset_state = transform_market_movement_with_bocpd(
            _with_raw_metric(_evaluation(5_000), Metric.missing("unavailable")),
            BOCPD_CONFIG_ERL_60,
            previous,
        )
        self.assertEqual(unavailable.detector_state, BOCPD_UNAVAILABLE)
        self.assertIsNone(reset_state)
        after_gap, restarted = transform_market_movement_with_bocpd(
            _evaluation(10_000), BOCPD_CONFIG_ERL_60, reset_state,
        )
        self.assertEqual(after_gap.observations_since_reset, 1)
        self.assertEqual(after_gap.detector_state, BOCPD_WARMING)
        self.assertEqual(restarted.observations_since_reset, 1)

    def test_nonconsecutive_scope_and_candidate_config_changes_reset(self):
        base_eval = _evaluation(0)
        _, initial = transform_market_movement_with_bocpd(
            base_eval, BOCPD_CONFIG_ERL_60,
        )
        gap_obs, _ = transform_market_movement_with_bocpd(
            _evaluation(10_000), BOCPD_CONFIG_ERL_60, initial,
        )
        self.assertEqual(gap_obs.observations_since_reset, 1)

        changed_scope = (
            ("universe_id", "u2"),
            ("universe_version", "v2"),
            ("movement_algorithm_version", "movement-v2"),
            ("movement_config_version", "movement-config-v2"),
            ("provider", "other-provider"),
            ("exchange", "other-exchange"),
            ("price_type", "mark"),
        )
        for field, value in changed_scope:
            with self.subTest(field=field):
                observation, state = transform_market_movement_with_bocpd(
                    _evaluation(5_000, identity={field: value}),
                    BOCPD_CONFIG_ERL_60,
                    initial,
                )
                self.assertEqual(observation.observations_since_reset, 1)
                self.assertEqual(observation.detector_state, BOCPD_WARMING)
                self.assertEqual(state.observations_since_reset, 1)

        observation, state = transform_market_movement_with_bocpd(
            _evaluation(5_000), BOCPD_CONFIG_ERL_30, initial,
        )
        self.assertEqual(observation.observations_since_reset, 1)
        self.assertEqual(state.candidate_config_version, BOCPD_CONFIG_ERL_30.version)

    def test_state_invariants_reject_corruption_without_repair(self):
        _, state = transform_market_movement_with_bocpd(
            _evaluation(0), BOCPD_CONFIG_ERL_60,
        )
        with self.assertRaises(ValueError):
            replace(state, candidate_algorithm_version="wrong-algorithm")
        with self.assertRaises(ValueError):
            replace(state, candidate_config_version="wrong-config")
        with self.assertRaises(ValueError):
            replace(state, last_evaluation_boundary_time_ms=1)
        with self.assertRaises(ValueError):
            replace(state, observations_since_reset=2)
        with self.assertRaises(ValueError):
            replace(state, hypotheses=(state.hypotheses[0], state.hypotheses[0]))
        with self.assertRaises(ValueError):
            replace(state, hypotheses=(
                state.hypotheses[0],
                replace(state.hypotheses[1], run_length_steps=2),
            ))
        with self.assertRaises(ValueError):
            replace(state, hypotheses=(
                replace(state.hypotheses[0], log_probability=0.2),
                state.hypotheses[1],
            ))
        with self.assertRaises(ValueError):
            replace(state.hypotheses[0], log_probability=float("nan"))
        with self.assertRaises(ValueError):
            replace(state.hypotheses[1], posterior_mean=float("inf"))
        with self.assertRaises(ValueError):
            replace(state.hypotheses[1], posterior_mean_variance=0.0)

    def test_causal_prefix_invariance_and_complete_determinism(self):
        values = (0.0,) * 8 + (0.8, 0.9, 1.0, 0.7, -0.2, 0.1, 0.0, 0.3)
        partitions = ("development",) * 10 + ("validation",) * 3 + ("test",) * 3
        config = BOCPD_CONFIG_ERL_30
        all_points, full = _run(values, config, partitions=partitions)
        prefix_points, prefix = _run(values[:10], config)
        self.assertEqual(full.points[:10], prefix.points)
        self.assertEqual(
            replace(full.summaries["development"], partition="all"),
            prefix.summaries["all"],
        )
        self.assertEqual(full.points, _run(values, config, partitions=partitions)[1].points)
        self.assertEqual(full.summaries, _run(values, config, partitions=partitions)[1].summaries)
        self.assertEqual(all_points[9].partition, "development")
        self.assertEqual(prefix.summaries["all"].evaluation_count, 10)

    def test_detection_regions_group_changes_close_on_reset_and_censor_open(self):
        points = tuple(
            _fake_detector_point(boundary, state)
            for boundary, state in (
                (10_000, BOCPD_WARMING),
                (15_000, BOCPD_CHANGE),
                (20_000, BOCPD_CHANGE),
                (25_000, BOCPD_NONE),
                (30_000, BOCPD_CHANGE),
                (35_000, BOCPD_UNAVAILABLE),
                (40_000, BOCPD_CHANGE),
            )
        )
        regions = detection_regions(points)
        self.assertEqual(len(regions), 3)
        self.assertEqual(
            (regions[0].start_boundary_time_ms, regions[0].end_boundary_time_ms),
            (15_000, 25_000),
        )
        self.assertEqual(
            (regions[1].start_boundary_time_ms, regions[1].end_boundary_time_ms),
            (30_000, 35_000),
        )
        self.assertEqual(regions[2].start_boundary_time_ms, 40_000)
        self.assertIsNone(regions[2].end_boundary_time_ms)
        self.assertEqual(regions[2].observed_through_boundary_time_ms, 40_000)

    def test_causal_abrupt_change_creates_one_region_and_scope_reset_closes_it(self):
        values = (0.0,) * 12 + (3.5, 0.0)
        identities = ({},) * 13 + ({"universe_version": "v2"},)
        _, result = _run(
            values,
            BOCPD_CONFIG_ERL_30,
            identities=identities,
        )
        self.assertEqual(result.points[12].detector_state, BOCPD_CHANGE)
        self.assertGreaterEqual(
            result.points[12].bocpd_observation.recent_change_probability,
            BOCPD_ALARM_THRESHOLD,
        )
        self.assertEqual(result.points[13].detector_state, BOCPD_WARMING)
        regions = result.detection_regions_by_partition["all"]
        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0].start_boundary_time_ms, 60_000)
        self.assertEqual(regions[0].end_boundary_time_ms, 65_000)
        self.assertEqual(regions[0].observed_through_boundary_time_ms, 65_000)

    def test_matching_window_tie_break_and_one_to_one_use(self):
        first = _fake_detector_point(100_000, BOCPD_NONE, transitions=(
            _started(100_000, "episode-1"),
        ))
        regions = (
            BOCPDDetectionRegion(90_000, None, 100_000),
            BOCPDDetectionRegion(110_000, None, 110_000),
        )
        matched, unmatched, signed, absolute, used = _match_v1_onsets((first,), regions)
        self.assertEqual((matched, unmatched), (1, 0))
        self.assertEqual(signed, -10_000.0)
        self.assertEqual(absolute, 10_000.0)
        self.assertEqual(used, frozenset((0,)))

        near_a = _started(200_000, "episode-a")
        near_b = _started(210_000, "episode-b")
        two_onsets = _fake_detector_point(210_000, BOCPD_NONE,
                                          transitions=(near_a, near_b))
        one_region = (BOCPDDetectionRegion(205_000, None, 210_000),)
        matched, unmatched, _, _, used = _match_v1_onsets((two_onsets,), one_region)
        self.assertEqual((matched, unmatched), (1, 1))
        self.assertEqual(used, frozenset((0,)))

        far_onset = _fake_detector_point(300_000, BOCPD_NONE, transitions=(
            _started(300_000, "episode-far"),
        ))
        matched, unmatched, signed, absolute, used = _match_v1_onsets(
            (far_onset,), one_region,
        )
        self.assertEqual((matched, unmatched), (0, 1))
        self.assertIsNone(signed)
        self.assertIsNone(absolute)
        self.assertEqual(used, frozenset())

    def test_partition_cutoffs_prevent_future_region_leakage_and_censor(self):
        dev_onset = _fake_detector_point(
            5_000, BOCPD_WARMING, transitions=(_started(5_000, "episode-dev"),),
        )
        dev_first = _fake_detector_point(0, BOCPD_WARMING)
        validation_region = _fake_detector_point(10_000, BOCPD_CHANGE, "validation")
        points = (dev_first, dev_onset, validation_region)
        development = _summary(points, "development")
        full = _summary(points, "all")
        self.assertEqual(development.matched_baseline_onset_count, 0)
        self.assertEqual(development.unmatched_baseline_onset_count, 1)
        self.assertEqual(development.unmatched_bocpd_detection_region_count, 0)
        self.assertEqual(full.matched_baseline_onset_count, 1)
        self.assertEqual(full.median_signed_bocpd_minus_v1_onset_ms, 5_000.0)

        begins_in_dev = _fake_detector_point(5_000, BOCPD_CHANGE)
        closes_in_validation = _fake_detector_point(10_000, BOCPD_NONE, "validation")
        split_points = (dev_first, begins_in_dev, closes_in_validation)
        dev_observed = tuple(point for point in split_points
                             if point.evaluation_boundary_time_ms <= 5_000)
        dev_region = detection_regions(dev_observed)[0]
        self.assertIsNone(dev_region.end_boundary_time_ms)
        self.assertEqual(dev_region.observed_through_boundary_time_ms, 5_000)
        self.assertEqual(_summary(split_points, "development")
                         .bocpd_short_lived_closed_region_count, 0)

    def test_closed_short_region_count_excludes_open_regions(self):
        points = tuple(
            _fake_detector_point(boundary, state)
            for boundary, state in (
                (0, BOCPD_WARMING),
                (5_000, BOCPD_CHANGE),
                (10_000, BOCPD_NONE),
                (15_000, BOCPD_CHANGE),
                (50_000, BOCPD_NONE),
                (55_000, BOCPD_CHANGE),
            )
        )
        summary = _summary(points, "all")
        self.assertEqual(summary.bocpd_detection_region_count, 3)
        self.assertEqual(summary.bocpd_short_lived_closed_region_count, 1)

    def test_experiment_result_is_deterministic_and_keeps_v1_only_baseline(self):
        values = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.6, 0.6, 0.6, 0.6)
        states = ("neutral",) * 6 + ("rise",) * 4
        points, result = _run(values, BOCPD_CONFIG_ERL_60, states=states)
        second = run_market_state_bocpd_experiment(points, BOCPD_CONFIG_ERL_60)
        self.assertEqual(result, second)
        self.assertEqual(result.points[0].baseline_classification.windows[5].direction_state,
                         "NEUTRAL")
        self.assertEqual(result.points[-1].bocpd_observation.candidate_algorithm_version,
                         BOCPD_ALGORITHM_VERSION)
        self.assertIs(result.points[-1].movement_evaluation, points[-1].movement_evaluation)
        self.assertTrue(all(point.bocpd_observation.candidate_algorithm_version
                            == BOCPD_ALGORITHM_VERSION for point in result.points))

if __name__ == "__main__":
    unittest.main()
