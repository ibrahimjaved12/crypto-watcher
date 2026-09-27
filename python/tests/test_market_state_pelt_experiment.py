from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
import unittest

from market_analysis.experiments.market_state_common import MarketStateExperimentPoint
from market_analysis.experiments.market_state_pelt import (
    PELT_ALGORITHM_VERSION,
    PELT_CONFIG_BETA_1,
    PELT_CONFIG_BETA_2,
    PELT_CONFIG_BETA_4,
    PELT_CONFIGURATIONS,
    PELT_TRANSITION_MATCH_WINDOW_MS,
    PELTChangePoint,
    PELTConfig,
    PELTScopeIdentity,
    _match_pelt_to_baseline_onsets,
    run_market_state_pelt_experiment,
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
NUMERICAL_TOL = 1e-12


def _symbol(symbol, boundary, minute, direction, acceleration,
            provider="binance-usdm", exchange="binance", price_type="trade"):
    return SymbolMovementResult(
        symbol=symbol,
        instrument_id=symbol,
        provider=provider,
        exchange=exchange,
        price_type=price_type,
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
            universe=SYMBOLS, identity=None):
    identity = identity or {}
    denominator = len(universe)
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
    symbols = tuple(_symbol(
        symbol, boundary, minute, direction, acceleration,
        identity.get("provider", "binance-usdm"),
        identity.get("exchange", "binance"),
        identity.get("price_type", "trade"),
    ) for symbol in universe)
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
        algorithm_version=identity.get("movement_algorithm_version", "market-movement-v1"),
        config_version=identity.get("movement_config_version", "market-movement-config-v1"),
        universe_id=identity.get("universe_id", "u1"),
        universe_version=identity.get("universe_version", "v1"),
        configured_universe=tuple(universe),
        included_symbols=tuple(universe),
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


def _evaluation(boundary, state="neutral", normalized=0.0, raw=0.0,
                universe=SYMBOLS, identity=None):
    identity = identity or {}
    windows = {
        minute: _window(boundary, minute, state, normalized, raw, universe, identity)
        for minute in (1, 5, 15)
    }
    return MarketMovementEvaluation(
        algorithm_version=identity.get("movement_algorithm_version", "market-movement-v1"),
        config_version=identity.get("movement_config_version", "market-movement-config-v1"),
        universe_id=identity.get("universe_id", "u1"),
        universe_version=identity.get("universe_version", "v1"),
        configured_universe=tuple(universe),
        provider=identity.get("provider", "binance-usdm"),
        exchange=identity.get("exchange", "binance"),
        price_type=identity.get("price_type", "trade"),
        evaluation_boundary_time_ms=boundary,
        historical_lookback_ms=100,
        minimum_historical_coverage_ms=50,
        windows=windows,
    )


def _point(boundary, normalized=0.0, state="neutral", raw=0.0,
           partition="development", identity=None):
    identity = identity or {}
    universe = identity.get("universe", SYMBOLS)
    evaluation = _evaluation(boundary, state, normalized, raw, universe, identity)
    evidence = tuple(SymbolSourceTimeEvidence(symbol, 1, 2, 3) for symbol in universe)
    return MarketStateExperimentPoint(evaluation, evidence, partition)


def _run_values(values, config, *, states=None, raw_returns=None,
                partitions=None, identities=None, unavailable_indices=()):
    states = states or ("neutral",) * len(values)
    raw_returns = raw_returns or (0.0,) * len(values)
    partitions = partitions or ("development",) * len(values)
    identities = identities or ({},) * len(values)
    points = []
    for index, value in enumerate(values):
        point = _point(
            index * 5_000,
            0.0 if value is None else value,
            states[index],
            raw_returns[index],
            partitions[index],
            identities[index],
        )
        if index in unavailable_indices:
            evaluation = point.movement_evaluation
            window = evaluation.windows[5]
            evaluation = replace(
                evaluation,
                windows={
                    **evaluation.windows,
                    5: replace(
                        window,
                        aggregates=replace(
                            window.aggregates,
                            median_normalized_movement=Metric.missing("missing"),
                        ),
                    ),
                },
            )
            point = replace(point, movement_evaluation=evaluation)
        points.append(point)
    return tuple(points), run_market_state_pelt_experiment(tuple(points), config)


def _unpruned_oracle(values, beta, min_segment_points=6):
    """Independent all-start optimal partitioning reference for small fixtures."""
    prefix = [0.0]
    prefix_sq = [0.0]
    for value in values:
        prefix.append(prefix[-1] + value)
        prefix_sq.append(prefix_sq[-1] + value * value)

    def cost(start, end):
        count = end - start
        total = prefix[end] - prefix[start]
        total_sq = prefix_sq[end] - prefix_sq[start]
        result = total_sq - total * total / count
        if result < 0 and result >= -NUMERICAL_TOL:
            return 0.0
        if result < -NUMERICAL_TOL:
            raise AssertionError("oracle encountered materially negative SSE")
        return result

    objectives = [None] * (len(values) + 1)
    paths = [None] * (len(values) + 1)
    objectives[0] = -beta
    paths[0] = ()
    for end in range(min_segment_points, len(values) + 1):
        candidates = []
        for start in range(end - min_segment_points + 1):
            if objectives[start] is None:
                continue
            path = paths[start] + ((start,) if start else ())
            candidates.append((objectives[start] + cost(start, end) + beta, path))
        best_objective, best_path = candidates[0]
        for objective, path in candidates[1:]:
            if objective < best_objective - NUMERICAL_TOL:
                best_objective, best_path = objective, path
            elif (abs(objective - best_objective) <= NUMERICAL_TOL
                  and path < best_path):
                best_objective, best_path = objective, path
        objectives[end], paths[end] = best_objective, best_path
    if objectives[-1] is None:
        return None
    return objectives[-1], paths[-1]


class MarketStatePELTExperimentTests(unittest.TestCase):
    def test_exact_piecewise_constant_shift_boundary_and_segments(self):
        values = (0.0,) * 6 + (1.0,) * 6
        _, result = _run_values(values, PELT_CONFIG_BETA_1)
        view = result.segmentations["all"]
        self.assertEqual(view.change_points[0].split_index, 6)
        self.assertEqual(view.change_points[0].boundary_time_ms, 30_000)
        self.assertEqual(view.change_points[0].mean_delta, 1.0)
        self.assertEqual(tuple(segment.observation_count for segment in view.segments), (6, 6))
        self.assertEqual(tuple(segment.sse for segment in view.segments), (0.0, 0.0))
        self.assertAlmostEqual(view.total_penalized_objective, 1.0)
        self.assertEqual(view.segments[0].end_boundary_time_ms, 25_000)
        self.assertEqual(view.segments[1].start_boundary_time_ms, 30_000)
        self.assertEqual(result.summaries["all"].unmatched_pelt_change_point_count, 1)
        self.assertNotIn("direction_state", PELTChangePoint.__dataclass_fields__)
        self.assertNotIn("classification", PELTChangePoint.__dataclass_fields__)
        self.assertNotIn("trade_direction", PELTChangePoint.__dataclass_fields__)

    def test_penalty_contrast_has_fixed_distinct_semantics(self):
        values = (0.0,) * 6 + (0.6,) * 6
        _, low = _run_values(values, PELT_CONFIG_BETA_1)
        _, middle = _run_values(values, PELT_CONFIG_BETA_2)
        _, high = _run_values(values, PELT_CONFIG_BETA_4)
        self.assertEqual(len(low.segmentations["all"].change_points), 1)
        self.assertEqual(middle.segmentations["all"].change_points, ())
        self.assertEqual(high.segmentations["all"].change_points, ())
        self.assertEqual(tuple(config.beta for config in PELT_CONFIGURATIONS), (1.0, 2.0, 4.0))
        with self.assertRaises(ValueError):
            PELTConfig("custom", 3.0, 6)

    def test_minimum_segment_length_and_insufficient_blocks(self):
        _, short = _run_values((0.0,) * 5, PELT_CONFIG_BETA_1)
        short_view = short.segmentations["all"]
        self.assertEqual(short_view.insufficient_block_count, 1)
        self.assertEqual(short_view.insufficient_block_point_count, 5)
        self.assertEqual(short_view.blocks[0].status, "INSUFFICIENT_BLOCK")
        self.assertEqual(short_view.segments, ())
        for count in range(6, 12):
            _, result = _run_values((0.0,) * 6 + (1.0,) * (count - 6),
                                    PELT_CONFIG_BETA_1)
            view = result.segmentations["all"]
            self.assertEqual(len(view.segments), 1)
            self.assertEqual(view.change_points, ())
        _, constrained = _run_values((0.0,) * 6 + (1.0,) * 5, PELT_CONFIG_BETA_1)
        self.assertEqual(constrained.segmentations["all"].change_points, ())

    def test_independent_unpruned_oracle_matches_pelt(self):
        series = (
            (0.0,) * 18,
            (0.0,) * 6 + (0.9,) * 6,
            (0.0,) * 6 + (0.9,) * 6 + (-0.4,) * 6,
            tuple((index % 5 - 2) * 0.13 for index in range(24)),
            tuple((index % 3) * 0.2 for index in range(11)),
        )
        for values in series:
            for config in PELT_CONFIGURATIONS:
                _, result = _run_values(values, config)
                block = result.segmentations["all"].blocks[0]
                oracle = _unpruned_oracle(values, config.beta, config.min_segment_points)
                if oracle is None:
                    self.assertIsNone(block.penalized_objective)
                    self.assertEqual(block.changepoint_indices, ())
                else:
                    oracle_objective, oracle_points = oracle
                    self.assertAlmostEqual(block.penalized_objective, oracle_objective,
                                           delta=NUMERICAL_TOL)
                    self.assertEqual(block.changepoint_indices, oracle_points)

    def test_unavailable_gap_forms_independent_blocks_without_gap_change_point(self):
        values = (0.0,) * 6 + (None,) + (1.0,) * 6
        points, result = _run_values(
            values, PELT_CONFIG_BETA_1, unavailable_indices=(6,))
        view = result.segmentations["all"]
        self.assertEqual(view.unavailable_point_count, 1)
        self.assertEqual(view.usable_point_count, 12)
        self.assertEqual(view.compatible_block_count, 2)
        self.assertEqual(tuple(block.observation_count for block in view.blocks), (6, 6))
        self.assertEqual(view.change_points, ())
        self.assertEqual(view.segments[0].end_boundary_time_ms, 25_000)
        self.assertEqual(view.segments[1].start_boundary_time_ms, 35_000)
        self.assertEqual(points[6].evaluation_boundary_time_ms, 30_000)

    def test_each_scope_identity_change_starts_new_block(self):
        identity_fields = (
            ("movement_algorithm_version", "other-algorithm"),
            ("movement_config_version", "other-config"),
            ("universe_id", "u2"),
            ("universe_version", "v2"),
            ("provider", "other-provider"),
            ("exchange", "other-exchange"),
            ("price_type", "mark"),
        )
        for field, changed_value in identity_fields:
            first_identity = {}
            second_identity = {field: changed_value}
            values = (0.0,) * 6 + (1.0,) * 6
            _, result = _run_values(
                values,
                PELT_CONFIG_BETA_1,
                identities=(first_identity,) * 6 + (second_identity,) * 6,
            )
            view = result.segmentations["all"]
            self.assertEqual(view.compatible_block_count, 2, field)
            self.assertEqual(view.change_points, (), field)

    def test_development_and_validation_views_ignore_future_partitions(self):
        values = (0.0,) * 6 + (0.8,) * 6 + (-0.2,) * 6 + (0.5,) * 6
        partitions = ("development",) * 12 + ("validation",) * 6 + ("test",) * 6
        _, dev_only = _run_values(values[:12], PELT_CONFIG_BETA_1)
        all_points, complete = _run_values(
            values, PELT_CONFIG_BETA_1, partitions=partitions)
        self.assertEqual(dev_only.segmentations["development"],
                         complete.segmentations["development"])
        self.assertEqual(dev_only.summaries["development"],
                         complete.summaries["development"])
        self.assertEqual(dev_only.segmentations["all"],
                         complete.segmentations["development"])
        val_prefix = run_market_state_pelt_experiment(all_points[:18], PELT_CONFIG_BETA_1)
        self.assertEqual(val_prefix.segmentations["validation"],
                         complete.segmentations["validation"])
        self.assertEqual(val_prefix.summaries["validation"],
                         complete.summaries["validation"])

    def test_hindsight_segmentation_can_revise_earlier_boundary(self):
        prefix = (0.0,) * 6 + (0.6,) * 6
        _, prefix_result = _run_values(prefix, PELT_CONFIG_BETA_1)
        extended = prefix + (0.0,) * 24
        _, extended_result = _run_values(extended, PELT_CONFIG_BETA_1)
        self.assertEqual(tuple(cp.split_index for cp in
                               prefix_result.segmentations["all"].change_points), (6,))
        self.assertNotEqual(
            prefix_result.segmentations["all"].change_points,
            extended_result.segmentations["all"].change_points,
        )

    def test_temporal_matching_uses_window_and_reports_signed_offset(self):
        values = (0.0,) * 6 + (0.6,) * 6
        states = ("neutral",) * 6 + ("rise",) * 6
        raw = (0.0,) * 6 + (0.1,) * 6
        _, result = _run_values(
            values, PELT_CONFIG_BETA_1, states=states, raw_returns=raw)
        summary = result.summaries["all"]
        self.assertEqual(summary.matched_baseline_onset_count, 1)
        self.assertEqual(summary.unmatched_baseline_onset_count, 0)
        self.assertEqual(summary.median_signed_pelt_minus_v1_onset_ms, -5_000.0)
        self.assertEqual(summary.median_absolute_pelt_v1_offset_ms, 5_000.0)
        self.assertEqual(PELT_TRANSITION_MATCH_WINDOW_MS, 60_000)

        late_values = (0.0,) * 6 + (0.6,) * 20
        late_states = ("neutral",) * 24 + ("rise",) * 2
        late_raw = (0.0,) * 24 + (0.1,) * 2
        _, late = _run_values(
            late_values,
            PELT_CONFIG_BETA_1,
            states=late_states,
            raw_returns=late_raw,
        )
        late_summary = late.summaries["all"]
        self.assertEqual(late_summary.matched_baseline_onset_count, 0)
        self.assertEqual(late_summary.unmatched_baseline_onset_count, 1)

    def test_one_change_point_matches_at_most_one_onset_and_ties_choose_earlier(self):
        transition_a = SimpleNamespace(
            transition="STARTED", evaluation_boundary_time_ms=60_000)
        transition_b = SimpleNamespace(
            transition="REVERSED", evaluation_boundary_time_ms=80_000)
        selected = (
            SimpleNamespace(evaluation_boundary_time_ms=60_000,
                            baseline_transitions=(transition_a,)),
            SimpleNamespace(evaluation_boundary_time_ms=80_000,
                            baseline_transitions=(transition_b,)),
        )
        scope = PELTScopeIdentity("a", "c", "u", "v", "p", "e", "t")

        def change(boundary):
            return PELTChangePoint(
                0, 1, boundary, scope, 0.0, 6, 1.0, 6, 1.0,
                PELT_ALGORITHM_VERSION, PELT_CONFIG_BETA_1.version,
            )

        matched, unmatched, _, _, used = _match_pelt_to_baseline_onsets(
            selected, (change(70_000),))
        self.assertEqual((matched, unmatched), (1, 1))
        self.assertEqual(used, frozenset((0,)))
        matched, unmatched, signed, absolute, used = _match_pelt_to_baseline_onsets(
            (SimpleNamespace(
                evaluation_boundary_time_ms=60_000,
                baseline_transitions=(transition_a,),
            ),),
            (change(70_000), change(50_000)),
        )
        self.assertEqual((matched, unmatched), (1, 0))
        self.assertEqual(signed, -10_000.0)
        self.assertEqual(absolute, 10_000.0)
        self.assertEqual(used, frozenset((0,)))
        matched, unmatched, signed, _, _ = _match_pelt_to_baseline_onsets(
            (SimpleNamespace(
                evaluation_boundary_time_ms=60_000,
                baseline_transitions=(transition_a,),
            ),),
            (change(120_000),),
        )
        self.assertEqual((matched, unmatched, signed), (1, 0, 60_000.0))

    def test_partition_attribution_and_deterministic_result(self):
        values = (0.0,) * 6 + (0.6,) * 12
        partitions = ("development",) * 7 + ("validation",) * 11
        _, first = _run_values(values, PELT_CONFIG_BETA_1, partitions=partitions)
        _, second = _run_values(values, PELT_CONFIG_BETA_1, partitions=partitions)
        self.assertEqual(first, second)
        validation_summary = first.summaries["validation"]
        self.assertEqual(validation_summary.pelt_change_point_count,
                         len(first.segmentations["validation"].change_points))
        self.assertIsNone(validation_summary.median_absolute_mean_delta)
        self.assertEqual(validation_summary.unmatched_pelt_change_point_count, 0)


if __name__ == "__main__":
    unittest.main()
