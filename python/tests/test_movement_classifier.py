"""Focused #72 fixtures built from explicit #71 result snapshots. Run with unittest."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_analysis.movement_classifier import (
    MarketClassificationContext, MarketWindowClassificationContext,
    classify_market_movement,
)
from market_analysis.movement_metrics import (
    BreadthSide, ExcludedSymbol, MarketMovementEvaluation,
    MarketMovementWindowResult, Metric, SymbolMovementResult,
    WindowAggregates, WindowBreadth,
)


BOUNDARY = 1_800_000
MISSING = Metric.missing("SYMBOL_EXCLUDED")


def symbol(name, direction, window, acceleration, *, material=False, outlier=False):
    raw = 0.01 if direction == "RISING" else -0.01 if direction == "FALLING" else 0.0
    z = 2.0 if outlier else 1.0 if direction == "RISING" else -1.0
    return SymbolMovementResult(
        symbol=name, instrument_id=f"binance-usdm:{name}",
        provider="binance-usdm", exchange="binance", price_type="trade",
        window_minutes=window, evaluation_boundary_time_ms=BOUNDARY,
        included=True, exclusion_reasons=(),
        current_return=Metric.present(raw), previous_return=Metric.present(0.0),
        velocity=Metric.present(raw / 60), previous_velocity=Metric.present(0.0),
        acceleration=Metric.present(acceleration),
        historical_median=Metric.present(0.0), historical_mad=Metric.present(0.01),
        normalized_z=Metric.present(z), direction=Metric.present(direction),
        material_rising=material and direction == "RISING",
        material_falling=material and direction == "FALLING",
        current_notional_volume=Metric.present(Decimal("100")),
        rvol=Metric.present(1.25), cross_sectional_z=Metric.present(4.0 if outlier else 0.0),
        outlier_candidate=outlier,
    )


def excluded_symbol(name, window, reasons):
    return SymbolMovementResult(
        symbol=name, instrument_id=f"binance-usdm:{name}",
        provider="binance-usdm", exchange="binance", price_type="trade",
        window_minutes=window, evaluation_boundary_time_ms=BOUNDARY,
        included=False, exclusion_reasons=reasons,
        current_return=MISSING, previous_return=MISSING, velocity=MISSING,
        previous_velocity=MISSING, acceleration=MISSING, historical_median=MISSING,
        historical_mad=MISSING, normalized_z=MISSING, direction=MISSING,
        material_rising=False, material_falling=False,
        current_notional_volume=MISSING, rvol=MISSING, cross_sectional_z=MISSING,
        outlier_candidate=False,
    )


def window_result(
    window=5, *, directions=("RISING",) * 7 + ("FALLING",) * 3,
    material_count=5, accelerations=None, median_raw=0.01,
    median_normalized=0.5, eligible=True, exclusion_reasons=(),
    outlier_index=None, configured_count=10,
):
    accelerations = accelerations if accelerations is not None else (1.0,) * len(directions)
    assert len(accelerations) == len(directions)
    leading_direction = directions[0] if directions else None
    material_left = material_count
    included = []
    for index, (direction, acceleration) in enumerate(zip(directions, accelerations)):
        material = direction == leading_direction and material_left > 0
        if material:
            material_left -= 1
        included.append(symbol(f"S{index}", direction, window, acceleration,
                               material=material, outlier=index == outlier_index))
    included = tuple(included)
    assert len(included) <= configured_count
    excluded = tuple(excluded_symbol(f"S{index}", window, tuple(exclusion_reasons))
                     for index in range(len(included), configured_count))
    all_symbols = included + excluded
    denominator = len(included)

    def side(count):
        return Metric.present(BreadthSide(count, count / denominator))

    if eligible:
        rising = sum(item.direction.value == "RISING" for item in included)
        falling = sum(item.direction.value == "FALLING" for item in included)
        breadth = WindowBreadth(
            True, None, denominator, side(denominator - rising - falling),
            side(rising), side(falling),
            side(sum(item.material_rising for item in included)),
            side(sum(item.material_falling for item in included)),
        )
        aggregates = WindowAggregates(
            Metric.present(median_normalized), Metric.present(median_raw),
            Metric.present(0.4), Metric.present(0.45),
            Metric.present(tuple((item.symbol, 1 / denominator) for item in included)),
            Metric.present(0.2),
        )
    else:
        unavailable = Metric.missing("MARKET_UNIVERSE_INELIGIBLE")
        breadth = WindowBreadth(False, "MARKET_UNIVERSE_INELIGIBLE", denominator,
                                unavailable, unavailable, unavailable, unavailable, unavailable)
        aggregates = WindowAggregates(*(unavailable for _ in range(6)))
    return MarketMovementWindowResult(
        algorithm_version="market-movement-v1", config_version="market-movement-config-v1",
        universe_id="watched", universe_version="v4",
        configured_universe=tuple(f"S{index}" for index in range(configured_count)),
        included_symbols=tuple(item.symbol for item in included),
        excluded_symbols=tuple(ExcludedSymbol(item.symbol, item.exclusion_reasons)
                               for item in excluded),
        window_minutes=window, provider="binance-usdm", exchange="binance",
        price_type="trade", evaluation_boundary_time_ms=BOUNDARY,
        historical_lookback_ms=604_800_000, minimum_historical_coverage_ms=259_200_000,
        market_wide_eligible=eligible, eligible_count=denominator,
        eligible_fraction=denominator / configured_count,
        symbols=all_symbols, breadth=breadth, aggregates=aggregates,
    )


def evaluation(primary):
    windows = {5: primary}
    for minutes in (1, 15):
        windows[minutes] = replace(
            primary, window_minutes=minutes,
            symbols=tuple(replace(item, window_minutes=minutes) for item in primary.symbols),
        )
    return MarketMovementEvaluation(
        "market-movement-v1", "market-movement-config-v1", "watched", "v4",
        primary.configured_universe, "binance-usdm", "binance", "trade", BOUNDARY,
        604_800_000, 259_200_000, windows,
    )


def classify(primary, *, prior=None, source=None, event=None):
    context = MarketClassificationContext({
        minutes: MarketWindowClassificationContext(source, event, prior)
        for minutes in (1, 5, 15)
    })
    return classify_market_movement(evaluation(primary), context)


class DirectionTests(unittest.TestCase):
    def test_broad_rise_at_exact_directional_material_and_normalized_thresholds(self):
        result = classify(window_result()).windows[5]
        self.assertEqual(result.direction_state, "BROAD_RISE")
        self.assertEqual(result.breadth.rising.value.fraction, 0.70)
        self.assertEqual(result.breadth.material_rising.value.fraction, 0.50)
        self.assertEqual(result.median_normalized_movement.value, 0.50)

    def test_broad_drop_at_exact_negative_normalized_threshold(self):
        result = classify(window_result(
            directions=("FALLING",) * 7 + ("RISING",) * 3,
            median_raw=-0.01, median_normalized=-0.5,
        )).windows[5]
        self.assertEqual(result.direction_state, "BROAD_DROP")

    def test_partial_conditions_are_neutral(self):
        cases = (
            dict(directions=("RISING",) * 6 + ("FALLING",) * 4),
            dict(material_count=4),
            dict(median_normalized=0.499),
            dict(median_raw=0.0),
            dict(median_raw=-0.01),
            dict(directions=("FALLING",) * 7 + ("RISING",) * 3,
                 median_raw=-0.01, median_normalized=-0.499),
            dict(directions=("FALLING",) * 7 + ("RISING",) * 3,
                 median_raw=0.0, median_normalized=-0.5),
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertEqual(classify(window_result(**changes)).windows[5].direction_state,
                                 "NEUTRAL")


class PaceTests(unittest.TestCase):
    def test_rise_accelerating_decelerating_and_mixed(self):
        cases = (
            ((1.0,) * 6 + (-1.0,) * 4, "ACCELERATING"),
            ((-1.0,) * 6 + (1.0,) * 4, "DECELERATING"),
            ((1.0,) * 5 + (-1.0,) * 5, "MIXED"),
        )
        for values, expected in cases:
            with self.subTest(expected=expected):
                result = classify(window_result(accelerations=values)).windows[5]
                self.assertEqual(result.pace, expected)
                self.assertEqual(result.positive_acceleration_breadth.value.count,
                                 sum(value > 0 for value in values))
                self.assertEqual(result.negative_acceleration_breadth.value.count,
                                 sum(value < 0 for value in values))
        self.assertEqual(classify(window_result(accelerations=cases[0][0])).windows[5]
                         .positive_acceleration_breadth.value.fraction, 0.60)

    def test_drop_accelerating_decelerating_and_mixed(self):
        drop = dict(directions=("FALLING",) * 7 + ("RISING",) * 3,
                    median_raw=-0.01, median_normalized=-0.5)
        cases = (
            ((-1.0,) * 6 + (1.0,) * 4, "ACCELERATING"),
            ((1.0,) * 6 + (-1.0,) * 4, "DECELERATING"),
            ((-1.0,) * 5 + (1.0,) * 5, "MIXED"),
        )
        for values, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(classify(window_result(**drop, accelerations=values))
                                 .windows[5].pace, expected)

    def test_non_broad_pace_is_not_applicable(self):
        self.assertEqual(classify(window_result(material_count=4)).windows[5].pace,
                         "NOT_APPLICABLE")


class OutlierAndReversalTests(unittest.TestCase):
    def test_isolated_outlier_requires_strictly_less_than_half_breadth(self):
        result = classify(window_result(
            directions=("RISING",) * 4 + ("FALLING",) * 6,
            material_count=4, median_raw=-0.01, median_normalized=-0.5,
            outlier_index=0,
        )).windows[5]
        self.assertEqual(len(result.isolated_outliers), 1)
        outlier = result.isolated_outliers[0]
        self.assertEqual((outlier.symbol, outlier.direction, outlier.raw_return),
                         ("S0", "RISING", 0.01))
        self.assertEqual((outlier.historical_z, outlier.cross_sectional_z), (2.0, 4.0))
        self.assertEqual((outlier.same_direction_breadth_count,
                          outlier.same_direction_breadth_fraction,
                          outlier.same_direction_breadth_denominator), (4, 0.4, 10))

    def test_exact_half_and_market_agreeing_outliers_are_not_isolated(self):
        for rising in (5, 7):
            with self.subTest(rising=rising):
                result = classify(window_result(
                    directions=("RISING",) * rising + ("FALLING",) * (10 - rising),
                    outlier_index=0,
                )).windows[5]
                self.assertEqual(result.isolated_outliers, ())

    def test_only_opposite_confirmed_broad_episode_is_a_reversal_candidate(self):
        primary = window_result()
        candidate = classify(primary, prior="BROAD_DROP").windows[5].reversal_candidate
        self.assertIsNotNone(candidate)
        self.assertEqual((candidate.prior_confirmed_episode_direction,
                          candidate.current_direction,
                          candidate.directional_breadth.fraction,
                          candidate.material_breadth.fraction),
                         ("BROAD_DROP", "BROAD_RISE", 0.7, 0.5))
        for prior in (None, "BROAD_RISE"):
            self.assertIsNone(classify(primary, prior=prior).windows[5].reversal_candidate)
        self.assertIsNone(classify(window_result(material_count=4), prior="BROAD_DROP")
                          .windows[5].reversal_candidate)
        drop = window_result(directions=("FALLING",) * 7 + ("RISING",) * 3,
                             median_raw=-0.01, median_normalized=-0.5)
        self.assertEqual(classify(drop, prior="BROAD_RISE").windows[5]
                         .reversal_candidate.current_direction, "BROAD_DROP")


class AvailabilityAndOutputTests(unittest.TestCase):
    def test_warming_preempts_neutral_and_broad_claims(self):
        primary = window_result(
            directions=("RISING",) * 4, material_count=4, eligible=False,
            exclusion_reasons=("WARMING_INSUFFICIENT_LIVE_HISTORY",),
        )
        result = classify(primary).windows[5]
        self.assertEqual(result.direction_state, "WARMING")
        self.assertEqual(result.pace, "NOT_APPLICABLE")
        self.assertEqual(result.availability_reasons, ("WARMING_INSUFFICIENT_LIVE_HISTORY",))

    def test_unavailable_and_mixed_warming_unavailable_preempt(self):
        for reasons in (("SOURCE_STALE",),
                        ("WARMING_INSUFFICIENT_LIVE_HISTORY", "SOURCE_UNAVAILABLE"),
                        ("INSUFFICIENT_NORMALIZATION_HISTORY",)):
            with self.subTest(reasons=reasons):
                primary = window_result(directions=("RISING",) * 4, material_count=4,
                                        eligible=False, exclusion_reasons=reasons)
                result = classify(primary).windows[5]
                self.assertEqual(result.direction_state, "UNAVAILABLE")
                self.assertEqual(result.pace, "NOT_APPLICABLE")
                self.assertEqual(result.excluded_symbols[0].reasons, reasons)
        small = window_result(directions=(), eligible=False, configured_count=4,
                              exclusion_reasons=("WARMING_INSUFFICIENT_LIVE_HISTORY",))
        self.assertEqual(classify(small).windows[5].direction_state, "UNAVAILABLE")

    def test_horizons_metadata_provenance_snapshot_and_determinism(self):
        primary = window_result()
        context = MarketClassificationContext({
            1: MarketWindowClassificationContext(None, None, None),
            5: MarketWindowClassificationContext(1_799_000, 1_798_000, "BROAD_DROP"),
            15: MarketWindowClassificationContext(None, None, None),
        })
        input_evaluation = evaluation(primary)
        first = classify_market_movement(input_evaluation, context)
        self.assertEqual(first, classify_market_movement(input_evaluation, context))
        self.assertEqual(first.primary_window_minutes, 5)
        for minutes, role in ((1, "RAPID"), (5, "PRIMARY"), (15, "PERSISTENCE")):
            result = first.windows[minutes]
            self.assertEqual((result.horizon_role, result.is_primary), (role, minutes == 5))
            self.assertIs(result.movement_snapshot, input_evaluation.windows[minutes])
            self.assertEqual(result.included_symbols, primary.included_symbols)
            self.assertEqual(result.configured_universe, primary.configured_universe)
            self.assertEqual(result.classifier_algorithm_version, "market-state-classifier-v1")
            self.assertEqual(result.classifier_config_version, "market-state-classifier-config-v1")
            self.assertEqual(result.movement_algorithm_version, "market-movement-v1")
            self.assertEqual(result.movement_config_version, "market-movement-config-v1")
            self.assertEqual((result.universe_id, result.universe_version,
                              result.provider, result.exchange, result.price_type),
                             ("watched", "v4", "binance-usdm", "binance", "trade"))
            self.assertEqual(result.evaluation_boundary_time_ms, BOUNDARY)
            self.assertEqual(result.dispersion_mad_normalized_movement.value, 0.2)
            self.assertEqual(result.trimmed_mean_normalized_movement.value, 0.4)
            self.assertEqual(result.liquidity_weighted_normalized_movement.value, 0.45)
            self.assertEqual(result.volume_context[0].current_notional_volume.value,
                             Decimal("100"))
            self.assertEqual(result.volume_context[0].rvol.value, 1.25)
        self.assertEqual((first.windows[5].source_timestamp_ms,
                          first.windows[5].event_timestamp_ms), (1_799_000, 1_798_000))
        self.assertIsNone(first.windows[1].source_timestamp_ms)
        self.assertIsNone(first.windows[1].event_timestamp_ms)
        with self.assertRaises(FrozenInstanceError):
            first.windows[5].direction_state = "NEUTRAL"
        with self.assertRaises(TypeError):
            first.windows[5] = first.windows[1]

    def test_excluded_reasons_are_preserved_without_reconstruction(self):
        primary = window_result(directions=("RISING",) * 6, material_count=5,
                                exclusion_reasons=("SOURCE_RECOVERING", "UNSUPPORTED_INSTRUMENT"))
        result = classify(primary).windows[5]
        self.assertEqual(result.direction_state, "BROAD_RISE")
        self.assertEqual(result.excluded_symbols, primary.excluded_symbols)
        self.assertEqual(result.availability_reasons,
                         ("SOURCE_RECOVERING", "UNSUPPORTED_INSTRUMENT"))
        self.assertEqual(result.included_symbols, primary.included_symbols)


if __name__ == "__main__":
    unittest.main()
