"""Focused #73 fixtures with explicit canonical #72 evidence. Do not run upstream math."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_analysis.market_episode_lifecycle import (
    ALGORITHM_VERSION, DEFAULT_CONFIG_VERSION, STATE_SERIALIZATION_VERSION,
    _episode_id,
    MarketEpisodeLifecycleConfig, deserialize_market_episode_lifecycle_state,
    interrupt_market_episode_state_on_restart, process_market_episode_lifecycle,
    serialize_market_episode_lifecycle_state,
)
from market_analysis.movement_classifier import (
    IsolatedOutlier, MarketClassificationEvaluation, MarketClassifierConfig,
    MarketWindowClassification, SymbolSourceTimeEvidence, SymbolVolumeContext,
)
from market_analysis.movement_metrics import (
    BreadthSide, ExcludedSymbol, MarketMovementWindowResult, Metric,
    SymbolMovementResult, WindowAggregates, WindowBreadth,
)


BASE = 1_800_000
SYMBOLS = tuple(f"S{index:03d}" for index in range(101))
MISSING = Metric.missing("MARKET_UNIVERSE_INELIGIBLE")


def side(count):
    return Metric.present(BreadthSide(count, count / 100))


def symbol(name, direction, minute, boundary, *, material=False, outlier=False,
           provider="binance-usdm", exchange="binance", price_type="trade"):
    raw = 0.01 if direction == "RISING" else -0.01 if direction == "FALLING" else 0.0
    sign = -1 if direction == "FALLING" else 1
    return SymbolMovementResult(
        symbol=name, instrument_id=f"{provider}:{name}", provider=provider,
        exchange=exchange, price_type=price_type, window_minutes=minute,
        evaluation_boundary_time_ms=boundary, included=True, exclusion_reasons=(),
        current_return=Metric.present(raw), previous_return=Metric.present(0.0),
        velocity=Metric.present(raw / 60), previous_velocity=Metric.present(0.0),
        acceleration=Metric.present(0.1 * sign), historical_median=Metric.present(0.0),
        historical_mad=Metric.present(0.01), normalized_z=Metric.present(2.0 * sign),
        direction=Metric.present(direction), material_rising=material and direction == "RISING",
        material_falling=material and direction == "FALLING",
        current_notional_volume=Metric.present(Decimal("100")), rvol=Metric.present(1.25),
        cross_sectional_z=Metric.present(4.0 * sign if outlier else 0.0),
        outlier_candidate=outlier,
    )


def excluded_symbol(name, minute, boundary, reason="STALE_LAST_TRADE", *,
                    provider="binance-usdm", exchange="binance", price_type="trade"):
    return SymbolMovementResult(
        symbol=name, instrument_id=f"{provider}:{name}", provider=provider,
        exchange=exchange, price_type=price_type, window_minutes=minute,
        evaluation_boundary_time_ms=boundary, included=False, exclusion_reasons=(reason,),
        current_return=MISSING, previous_return=MISSING, velocity=MISSING,
        previous_velocity=MISSING, acceleration=MISSING, historical_median=MISSING,
        historical_mad=MISSING, normalized_z=MISSING, direction=MISSING,
        material_rising=False, material_falling=False, current_notional_volume=MISSING,
        rvol=MISSING, cross_sectional_z=MISSING, outlier_candidate=False,
    )


def classification(boundary, *, direction="BROAD_RISE", same_count=None,
                   material_count=None, pace="MIXED", universe_id="watched",
                   universe_version="v1", classifier_algorithm_version="market-state-classifier-v1",
                   classifier_config_version="market-state-classifier-config-v1",
                   movement_algorithm_version="market-movement-v1",
                   movement_config_version="market-movement-config-v1",
                   provider="binance-usdm", exchange="binance", price_type="trade",
                   raw=None, outlier=False, breadth_direction=None):
    """Supply complete #72 snapshots directly; no classifier decision is made here."""
    if breadth_direction is not None:
        active_direction = breadth_direction
    elif direction == "BROAD_DROP":
        active_direction = "FALLING"
    else:
        active_direction = "RISING"
    if same_count is None:
        same_count = 60 if direction == "NEUTRAL" else 80
    if material_count is None:
        material_count = 40 if direction == "NEUTRAL" else 60
    unavailable = direction in ("WARMING", "UNAVAILABLE")
    other_direction = "FALLING" if active_direction == "RISING" else "RISING"
    other_count = 100 - same_count
    if raw is None:
        raw = -0.01 if active_direction == "FALLING" else 0.01
    pace_metric = Metric.missing("NO_BROAD_DIRECTION") if direction not in (
        "BROAD_RISE", "BROAD_DROP") else Metric.present(pace)
    acceleration = (-0.1 if active_direction == "FALLING" else 0.1)
    if pace == "DECELERATING":
        acceleration = -acceleration
    if pace == "MIXED":
        acceleration = 0.0
    windows = {}
    for minute, role in ((1, "RAPID"), (5, "PRIMARY"), (15, "PERSISTENCE")):
        if unavailable:
            included = ()
            excluded = tuple(excluded_symbol(name, minute, boundary,
                         "WARMING_INSUFFICIENT_LIVE_HISTORY" if direction == "WARMING"
                         else "SOURCE_UNAVAILABLE", provider=provider, exchange=exchange,
                         price_type=price_type) for name in SYMBOLS)
            breadth = WindowBreadth(False, "MARKET_UNIVERSE_INELIGIBLE", 0,
                                    MISSING, MISSING, MISSING, MISSING, MISSING)
            aggregates = WindowAggregates(*(MISSING for _ in range(6)))
            volume = ()
            isolated = ()
        else:
            included = tuple(symbol(
                name, active_direction if index < same_count else other_direction,
                minute, boundary, material=index < material_count,
                outlier=outlier and index == same_count,
                provider=provider, exchange=exchange, price_type=price_type,
            ) for index, name in enumerate(SYMBOLS[:100]))
            excluded = (excluded_symbol(SYMBOLS[100], minute, boundary,
                        provider=provider, exchange=exchange, price_type=price_type),)
            rising = same_count if active_direction == "RISING" else other_count
            falling = same_count if active_direction == "FALLING" else other_count
            breadth = WindowBreadth(
                True, None, 100, side(0), side(rising), side(falling),
                side(material_count if active_direction == "RISING" else 0),
                side(material_count if active_direction == "FALLING" else 0),
            )
            aggregates = WindowAggregates(
                Metric.present(0.8 if raw > 0 else -0.8), Metric.present(raw),
                Metric.present(0.4), Metric.present(0.45),
                Metric.present(tuple((name, 0.01) for name in SYMBOLS[:100])),
                Metric.present(0.2),
            )
            volume = tuple(SymbolVolumeContext(item.symbol, item.current_notional_volume,
                                               item.rvol) for item in included)
            isolated = (IsolatedOutlier(SYMBOLS[same_count], other_direction,
                        -0.01 if other_direction == "FALLING" else 0.01,
                        -2.0 if other_direction == "FALLING" else 2.0,
                        -4.0 if other_direction == "FALLING" else 4.0,
                        other_count, other_count / 100, 100),) if outlier else ()
        snapshot = MarketMovementWindowResult(
            movement_algorithm_version, movement_config_version, universe_id, universe_version,
            SYMBOLS, tuple(item.symbol for item in included),
            tuple(ExcludedSymbol(item.symbol, item.exclusion_reasons) for item in excluded),
            minute, provider, exchange, price_type, boundary, 604_800_000, 259_200_000,
            not unavailable, len(included), len(included) / len(SYMBOLS),
            (*included, *excluded), breadth, aggregates,
        )
        provenance = tuple(SymbolSourceTimeEvidence(name, boundary - 5_000,
                           boundary - 4_000, boundary - 3_000) for name in SYMBOLS)
        windows[minute] = MarketWindowClassification(
            window_minutes=minute, horizon_role=role, is_primary=minute == 5,
            direction_state=direction, pace=pace_metric,
            prior_confirmed_episode_direction=None, reversal_candidate=None,
            isolated_outliers=isolated, breadth=breadth,
            median_raw_return=aggregates.median_raw_return,
            median_normalized_movement=aggregates.median_normalized_movement,
            median_acceleration=Metric.present(acceleration) if not unavailable else MISSING,
            positive_acceleration_breadth=side(70 if acceleration > 0 else 30)
            if not unavailable else MISSING,
            negative_acceleration_breadth=side(70 if acceleration < 0 else 30)
            if not unavailable else MISSING,
            dispersion_mad_normalized_movement=aggregates.dispersion_mad_normalized_movement,
            trimmed_mean_normalized_movement=aggregates.trimmed_mean_normalized_movement,
            liquidity_weighted_normalized_movement=aggregates.liquidity_weighted_normalized_movement,
            liquidity_weights=aggregates.liquidity_weights,
            volume_context=volume, market_wide_eligible=not unavailable,
            eligible_count=len(included), eligible_fraction=len(included) / len(SYMBOLS),
            configured_universe=SYMBOLS, included_symbols=tuple(item.symbol for item in included),
            excluded_symbols=snapshot.excluded_symbols,
            availability_reasons=("WARMING_INSUFFICIENT_LIVE_HISTORY",) if direction == "WARMING"
            else ("SOURCE_UNAVAILABLE",) if direction == "UNAVAILABLE" else (),
            classifier_algorithm_version=classifier_algorithm_version,
            classifier_config_version=classifier_config_version,
            movement_algorithm_version=movement_algorithm_version,
            movement_config_version=movement_config_version,
            universe_id=universe_id, universe_version=universe_version,
            provider=provider, exchange=exchange, price_type=price_type,
            evaluation_boundary_time_ms=boundary, source_time_evidence=provenance,
            movement_snapshot=snapshot,
        )
    return MarketClassificationEvaluation(
        classifier_algorithm_version, classifier_config_version,
        MarketClassifierConfig(version=classifier_config_version),
        movement_algorithm_version, movement_config_version,
        universe_id, universe_version, provider, exchange, price_type,
        boundary, 5, windows,
    )


def advance(state, boundary, **kwargs):
    return process_market_episode_lifecycle(classification(boundary, **kwargs), state)


def start(**kwargs):
    first = advance(None, BASE, **kwargs)
    second = advance(first.next_state, BASE + 5_000, **kwargs)
    assert [event.transition for event in second.transitions] == ["STARTED"]
    return second.next_state, second.transitions[0]


class MarketEpisodeLifecycleTests(unittest.TestCase):
    def test_v1_config_is_versioned_and_immutable(self):
        config = MarketEpisodeLifecycleConfig()
        self.assertEqual((config.version, config.evaluation_cadence_ms,
                          config.start_confirmation_count, config.end_confirmation_count,
                          config.reversal_confirmation_count, config.strengthen_confirmation_count,
                          config.weaken_confirmation_count, config.resume_confirmation_count,
                          config.continuation_breadth, config.material_strengthen_breadth,
                          config.material_weaken_breadth),
                         (DEFAULT_CONFIG_VERSION, 5_000, 2, 3, 2, 2, 2, 2, 0.55, 0.70, 0.50))
        changed = {
            "evaluation_cadence_ms": 10_000,
            "start_confirmation_count": 3, "end_confirmation_count": 4,
            "reversal_confirmation_count": 3, "strengthen_confirmation_count": 3,
            "weaken_confirmation_count": 3, "resume_confirmation_count": 3,
            "continuation_breadth": 0.60, "material_strengthen_breadth": 0.75,
            "material_weaken_breadth": 0.45,
        }
        for name, value in changed.items():
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    MarketEpisodeLifecycleConfig(version="custom-v2", **{name: value})
        alternate_version = MarketEpisodeLifecycleConfig(version="custom-v2")
        self.assertEqual(alternate_version.version, "custom-v2")
        self.assertEqual(alternate_version.continuation_breadth, 0.55)
        with self.assertRaises(FrozenInstanceError):
            config.start_confirmation_count = 3

    def test_start_requires_two_consecutive_strict_entries_and_never_repeats(self):
        first = advance(None, BASE)
        self.assertEqual(first.transitions, ())
        self.assertEqual((first.next_state.pending_start.direction,
                          first.next_state.pending_start.count,
                          first.next_state.pending_start.start_boundary_time_ms),
                         ("BROAD_RISE", 1, BASE))
        second = advance(first.next_state, BASE + 5_000)
        self.assertEqual([event.transition for event in second.transitions], ["STARTED"])
        self.assertEqual(second.transitions[0].episode_start_boundary_time_ms, BASE)
        self.assertEqual(second.transitions[0].evaluation_boundary_time_ms, BASE + 5_000)
        state = second.next_state
        for offset in (10_000, 15_000, 20_000, 25_000):
            result = advance(state, BASE + offset)
            self.assertEqual(result.transitions, ())
            self.assertEqual(result.next_state.active_episode.episode_id,
                             second.transitions[0].episode_id)
            state = result.next_state

    def test_pending_start_cancels_or_replaces_direction(self):
        first = advance(None, BASE)
        neutral = advance(first.next_state, BASE + 5_000, direction="NEUTRAL")
        self.assertIsNone(neutral.next_state.pending_start)
        opposite = advance(first.next_state, BASE + 5_000, direction="BROAD_DROP")
        self.assertEqual((opposite.next_state.pending_start.direction,
                          opposite.next_state.pending_start.count), ("BROAD_DROP", 1))
        confirmed = advance(opposite.next_state, BASE + 10_000, direction="BROAD_DROP")
        self.assertEqual(confirmed.transitions[0].transition, "STARTED")
        self.assertEqual(confirmed.transitions[0].episode_start_boundary_time_ms, BASE + 5_000)

    def test_continuation_hysteresis_exact_55_and_three_failures(self):
        state, _ = start()
        for offset, count in ((10_000, 60), (15_000, 55)):
            result = advance(state, BASE + offset, direction="NEUTRAL",
                             same_count=count, material_count=50, raw=0.01)
            self.assertEqual(result.transitions, ())
            self.assertEqual(result.next_state.continuation_failure_count, 0)
            state = result.next_state
        for offset, expected in ((20_000, 1), (25_000, 2)):
            result = advance(state, BASE + offset, direction="NEUTRAL",
                             same_count=54, material_count=40, raw=0.01)
            self.assertEqual(result.transitions, ())
            self.assertEqual(result.next_state.continuation_failure_count, expected)
            state = result.next_state
        ended = advance(state, BASE + 30_000, direction="NEUTRAL",
                        same_count=55, material_count=40, raw=-0.01)
        self.assertEqual([event.transition for event in ended.transitions], ["ENDED"])
        self.assertEqual(ended.transitions[0].transition_reason, "continuation_failed")
        self.assertIsNone(ended.next_state.active_episode)

    def test_passing_continuation_resets_failure_count(self):
        state, _ = start()
        failed = advance(state, BASE + 10_000, direction="NEUTRAL", same_count=54)
        passed = advance(failed.next_state, BASE + 15_000, direction="NEUTRAL", same_count=55)
        self.assertEqual(passed.next_state.continuation_failure_count, 0)
        self.assertEqual(passed.transitions, ())

    def test_drop_continuation_uses_falling_breadth_and_negative_raw_sign(self):
        state, _ = start(direction="BROAD_DROP")
        passing = advance(state, BASE + 10_000, direction="NEUTRAL",
                          same_count=55, material_count=40, raw=-0.01,
                          breadth_direction="FALLING")
        self.assertEqual(passing.next_state.continuation_failure_count, 0)
        failing = advance(passing.next_state, BASE + 15_000, direction="NEUTRAL",
                          same_count=55, material_count=40, raw=0.01,
                          breadth_direction="FALLING")
        self.assertEqual(failing.next_state.continuation_failure_count, 1)

    def test_clean_opposite_two_tick_reversal_precedes_exit_and_has_no_cooldown(self):
        state, started = start()
        first = advance(state, BASE + 10_000, direction="BROAD_DROP")
        self.assertEqual(first.transitions, ())
        self.assertEqual(first.next_state.pending_reversal.count, 1)
        self.assertEqual(first.next_state.continuation_failure_count, 1)
        confirmed = advance(first.next_state, BASE + 15_000, direction="BROAD_DROP")
        self.assertEqual([event.transition for event in confirmed.transitions], ["REVERSED"])
        reversal = confirmed.transitions[0]
        self.assertEqual((reversal.from_direction, reversal.to_direction),
                         ("BROAD_RISE", "BROAD_DROP"))
        self.assertEqual(reversal.previous_episode_id, started.episode_id)
        self.assertNotEqual(reversal.episode_id, started.episode_id)
        self.assertEqual(reversal.episode_start_boundary_time_ms, BASE + 10_000)
        again = advance(confirmed.next_state, BASE + 20_000, direction="BROAD_RISE")
        final = advance(again.next_state, BASE + 25_000, direction="BROAD_RISE")
        self.assertEqual([event.transition for event in final.transitions], ["REVERSED"])

    def test_confirmed_reversal_wins_when_end_threshold_matures_together(self):
        state, _ = start()
        first_failure = advance(state, BASE + 10_000, direction="NEUTRAL", same_count=40)
        self.assertEqual(first_failure.next_state.continuation_failure_count, 1)
        first_opposite = advance(first_failure.next_state, BASE + 15_000,
                                 direction="BROAD_DROP")
        self.assertEqual(first_opposite.transitions, ())
        self.assertEqual(first_opposite.next_state.pending_reversal.count, 1)
        self.assertEqual(first_opposite.next_state.continuation_failure_count, 2)

        confirmed = advance(first_opposite.next_state, BASE + 20_000,
                            direction="BROAD_DROP")
        self.assertEqual([event.transition for event in confirmed.transitions], ["REVERSED"])
        self.assertEqual(confirmed.transitions[0].transition_reason,
                         "confirmed_opposite_broad_entry")
        self.assertEqual(confirmed.next_state.continuation_failure_count, 0)

    def test_end_before_reversal_confirmation_starts_new_direction_candidate(self):
        state, _ = start()
        for offset in (10_000, 15_000):
            state = advance(state, BASE + offset, direction="NEUTRAL",
                            same_count=40).next_state
        self.assertEqual(state.continuation_failure_count, 2)

        ended = advance(state, BASE + 20_000, direction="BROAD_DROP")
        self.assertEqual([event.transition for event in ended.transitions], ["ENDED"])
        self.assertEqual(ended.transitions[0].transition_reason, "continuation_failed")
        self.assertIsNone(ended.next_state.active_episode)
        self.assertIsNone(ended.next_state.pending_reversal)
        self.assertEqual((ended.next_state.pending_start.direction,
                          ended.next_state.pending_start.count,
                          ended.next_state.pending_start.start_boundary_time_ms),
                         ("BROAD_DROP", 1, BASE + 20_000))

        started = advance(ended.next_state, BASE + 25_000, direction="BROAD_DROP")
        self.assertEqual([event.transition for event in started.transitions], ["STARTED"])
        self.assertEqual(started.transitions[0].episode_start_boundary_time_ms, BASE + 20_000)

    def test_pace_strengthening_and_weakening_are_crossings(self):
        state, _ = start(pace="MIXED")
        first = advance(state, BASE + 10_000, pace="ACCELERATING")
        self.assertEqual(first.transitions, ())
        confirmed = advance(first.next_state, BASE + 15_000, pace="ACCELERATING")
        self.assertEqual([event.transition for event in confirmed.transitions], ["STRENGTHENED"])
        self.assertIn("pace_accelerating", confirmed.transitions[0].transition_reason)
        held = advance(confirmed.next_state, BASE + 20_000, pace="ACCELERATING")
        self.assertEqual(held.transitions, ())
        lower = advance(held.next_state, BASE + 25_000, pace="DECELERATING")
        weakened = advance(lower.next_state, BASE + 30_000, pace="DECELERATING")
        self.assertEqual([event.transition for event in weakened.transitions], ["WEAKENED"])
        self.assertIn("pace_decelerating", weakened.transitions[0].transition_reason)
        self.assertEqual(advance(weakened.next_state, BASE + 35_000,
                                 pace="DECELERATING").transitions, ())

    def test_material_crossings_exact_70_below_50_and_no_repeat(self):
        state, _ = start(material_count=69)
        first = advance(state, BASE + 10_000, material_count=70)
        self.assertEqual(first.transitions, ())
        confirmed = advance(first.next_state, BASE + 15_000, material_count=72)
        self.assertEqual([event.transition for event in confirmed.transitions], ["STRENGTHENED"])
        self.assertEqual(confirmed.transitions[0].transition_reason, "material_strengthening")
        state = advance(confirmed.next_state, BASE + 20_000,
                        material_count=72).next_state
        state = advance(state, BASE + 25_000, material_count=51).next_state
        exact = advance(state, BASE + 30_000, material_count=50)
        self.assertEqual(exact.transitions, ())
        first_low = advance(exact.next_state, BASE + 35_000, direction="NEUTRAL",
                            same_count=60, material_count=49)
        self.assertEqual(first_low.transitions, ())
        weak = advance(first_low.next_state, BASE + 40_000, direction="NEUTRAL",
                       same_count=60, material_count=48)
        self.assertEqual([event.transition for event in weak.transitions], ["WEAKENED"])
        self.assertEqual(weak.transitions[0].transition_reason, "material_weakening")
        held = advance(weak.next_state, BASE + 45_000, direction="NEUTRAL",
                       same_count=60, material_count=47)
        self.assertEqual(held.transitions, ())

    def test_continuation_failure_cancels_pending_material_weakening(self):
        state, _ = start(material_count=51)
        first = advance(state, BASE + 10_000, direction="NEUTRAL",
                        same_count=60, material_count=49)
        self.assertEqual(first.next_state.pending_weaken[0].reason, "material_weakening")
        failed = advance(first.next_state, BASE + 15_000, direction="NEUTRAL",
                         same_count=54, material_count=48)
        self.assertEqual(failed.transitions, ())
        self.assertEqual(failed.next_state.pending_weaken, ())
        self.assertEqual(failed.next_state.continuation_failure_count, 1)

    def test_pace_and_material_confirmation_combine_into_one_transition(self):
        state, _ = start(pace="MIXED", material_count=69)
        first = advance(state, BASE + 10_000, pace="ACCELERATING", material_count=70)
        confirmed = advance(first.next_state, BASE + 15_000,
                            pace="ACCELERATING", material_count=71)
        self.assertEqual(len(confirmed.transitions), 1)
        self.assertEqual(confirmed.transitions[0].transition, "STRENGTHENED")
        self.assertEqual(confirmed.transitions[0].transition_reason,
                         "pace_accelerating+material_strengthening")

    def test_episode_creation_sets_crossing_baseline(self):
        state, _ = start(pace="ACCELERATING", material_count=80)
        same = advance(state, BASE + 10_000, pace="ACCELERATING", material_count=80)
        self.assertEqual(same.transitions, ())
        state, _ = start(pace="DECELERATING", material_count=50)
        same = advance(state, BASE + 10_000, pace="DECELERATING",
                       direction="NEUTRAL", same_count=60, material_count=50)
        self.assertEqual(same.transitions, ())

    def test_warming_and_unavailable_interrupt_without_ordinary_exit(self):
        first = advance(None, BASE)
        self.assertIsNone(advance(first.next_state, BASE + 5_000,
                                  direction="WARMING").next_state.pending_start)
        for unavailable_direction in ("WARMING", "UNAVAILABLE"):
            state, started = start()
            for offset in (10_000, 15_000, 20_000, 25_000):
                result = advance(state, BASE + offset, direction=unavailable_direction)
                self.assertEqual(result.transitions, ())
                self.assertTrue(result.next_state.interrupted)
                self.assertEqual(result.next_state.continuation_failure_count, 0)
                self.assertEqual(result.next_state.current_direction_state,
                                 unavailable_direction)
                self.assertEqual(result.next_state.active_episode.episode_id,
                                 started.episode_id)
                state = result.next_state

    def test_universe_change_ends_old_scope_then_starts_new_candidate(self):
        state, started = start()
        changed_input = classification(BASE + 10_000, direction="BROAD_DROP",
                                       universe_version="v2")
        result = process_market_episode_lifecycle(changed_input, state)
        self.assertEqual([event.transition for event in result.transitions], ["ENDED"])
        event = result.transitions[0]
        self.assertEqual(event.transition_reason, "universe_changed")
        self.assertEqual(event.episode_scope.universe_version, "v1")
        self.assertEqual(event.evaluation_scope.universe_version, "v2")
        self.assertEqual(event.episode_lifecycle_config.version, DEFAULT_CONFIG_VERSION)
        self.assertEqual(event.evaluation_lifecycle_config.version, DEFAULT_CONFIG_VERSION)
        self.assertEqual(event.windows_context[1], changed_input.windows[5])
        self.assertEqual(event.episode_id, started.episode_id)
        self.assertIsNone(result.next_state.active_episode)
        self.assertEqual((result.next_state.pending_start.direction,
                          result.next_state.pending_start.count), ("BROAD_DROP", 1))
        self.assertIsNone(result.next_state.pending_reversal)
        pending_only = advance(None, BASE)
        changed = advance(pending_only.next_state, BASE + 5_000,
                          universe_version="v2")
        self.assertEqual(changed.transitions, ())
        self.assertEqual(changed.next_state.pending_start.count, 1)

    def test_scope_versions_and_provider_changes_end_active_episode(self):
        changes = {
            "universe_id": "other", "classifier_algorithm_version": "classifier-v2",
            "classifier_config_version": "custom-classifier-v2",
            "movement_algorithm_version": "movement-v2",
            "movement_config_version": "movement-config-v2",
            "provider": "other-provider", "exchange": "other-exchange",
            "price_type": "mid",
        }
        expected = {
            "universe_id": "universe_changed",
            "classifier_algorithm_version": "classifier_algorithm_changed",
            "classifier_config_version": "classifier_config_changed",
            "movement_algorithm_version": "movement_algorithm_changed",
            "movement_config_version": "movement_config_changed",
            "provider": "provider_changed", "exchange": "exchange_changed",
            "price_type": "price_type_changed",
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                state, started = start()
                result = advance(state, BASE + 10_000, **{field: value})
                self.assertEqual([event.transition for event in result.transitions], ["ENDED"])
                self.assertEqual(result.transitions[0].transition_reason, expected[field])
                self.assertEqual(result.transitions[0].episode_id, started.episode_id)
                self.assertIsNone(result.next_state.active_episode)
        state, _ = start()
        changed_config = MarketEpisodeLifecycleConfig(version="custom-v2")
        result = process_market_episode_lifecycle(classification(BASE + 10_000),
                                                  state, changed_config)
        self.assertEqual(result.transitions[0].transition_reason,
                         "lifecycle_config_changed")
        self.assertEqual(result.transitions[0].episode_lifecycle_config, state.lifecycle_config)
        self.assertEqual(result.transitions[0].evaluation_lifecycle_config, changed_config)
        for scope_field, changed_value, reason in (
            ("lifecycle_algorithm_version", "previous-lifecycle-v0",
             "lifecycle_algorithm_changed"),
            ("primary_window_minutes", 1, "primary_window_changed"),
        ):
            with self.subTest(scope_field=scope_field):
                state, _ = start()
                old_scope = replace(state.scope, **{scope_field: changed_value})
                old_id = (_episode_id(old_scope, state.active_episode.direction,
                          state.active_episode.start_boundary_time_ms)
                          if old_scope.lifecycle_algorithm_version == ALGORITHM_VERSION
                          else "previous-scope-episode")
                old_episode = replace(state.active_episode, scope=old_scope,
                                      episode_id=old_id)
                old_state = replace(state, scope=old_scope, active_episode=old_episode,
                                    lifecycle_algorithm_version=old_scope.lifecycle_algorithm_version)
                self.assertEqual(deserialize_market_episode_lifecycle_state(
                    serialize_market_episode_lifecycle_state(old_state)), old_state)
                result = process_market_episode_lifecycle(
                    classification(BASE + 10_000), old_state)
                self.assertEqual(result.transitions[0].transition_reason, reason)
                self.assertEqual(result.transitions[0].episode_scope, old_scope)
                self.assertEqual(result.transitions[0].episode_id, old_episode.episode_id)

    def test_duplicate_backward_gap_and_replay_identities(self):
        first_input = classification(BASE)
        first = process_market_episode_lifecycle(first_input)
        duplicate = process_market_episode_lifecycle(first_input, first.next_state)
        self.assertIs(duplicate.next_state, first.next_state)
        self.assertEqual(duplicate.transitions, ())
        with self.assertRaises(ValueError):
            process_market_episode_lifecycle(classification(BASE, material_count=61),
                                             first.next_state)
        with self.assertRaises(ValueError):
            process_market_episode_lifecycle(classification(BASE - 5_000),
                                             advance(first.next_state, BASE + 5_000).next_state)
        with self.assertRaises(ValueError):
            process_market_episode_lifecycle(classification(BASE + 1))
        gap = advance(first.next_state, BASE + 10_000)
        self.assertEqual(gap.transitions, ())
        self.assertEqual(gap.next_state.pending_start.count, 1)
        state, _ = start()
        gap = advance(state, BASE + 15_000)
        self.assertTrue(gap.next_state.interrupted)
        self.assertEqual(gap.next_state.pending_resume.count, 1)
        self.assertEqual(gap.next_state.continuation_failure_count, 0)
        crossing = advance(state, BASE + 10_000, pace="ACCELERATING")
        self.assertTrue(crossing.next_state.pending_strengthen)
        interrupted_gap = advance(crossing.next_state, BASE + 20_000)
        self.assertTrue(interrupted_gap.next_state.interrupted)
        self.assertEqual(interrupted_gap.next_state.pending_strengthen, ())
        self.assertEqual(interrupted_gap.next_state.pending_resume.count, 1)
        opposite = advance(state, BASE + 10_000, direction="BROAD_DROP")
        interrupted_opposite = advance(opposite.next_state, BASE + 20_000,
                                       direction="BROAD_DROP")
        self.assertEqual(interrupted_opposite.transitions, ())
        self.assertTrue(interrupted_opposite.next_state.interrupted)
        self.assertEqual(interrupted_opposite.next_state.pending_reversal.count, 1)
        self.assertEqual(interrupted_opposite.next_state.continuation_failure_count, 0)

        def replay():
            prior = None
            emitted = []
            for boundary, direction in ((BASE, "BROAD_RISE"), (BASE + 5_000, "BROAD_RISE"),
                                        (BASE + 10_000, "BROAD_DROP"),
                                        (BASE + 15_000, "BROAD_DROP")):
                result = advance(prior, boundary, direction=direction)
                prior = result.next_state
                emitted.extend(result.transitions)
            return prior, tuple(emitted)
        self.assertEqual(replay(), replay())
        _, events = replay()
        self.assertEqual([event.transition for event in events], ["STARTED", "REVERSED"])
        self.assertNotEqual(events[0].event_id, events[1].event_id)

    def test_restart_serialization_warming_resume_and_opposite_reversal(self):
        state, started = start()
        serialized = serialize_market_episode_lifecycle_state(state)
        self.assertEqual(serialized["serialization_version"], STATE_SERIALIZATION_VERSION)
        restored = deserialize_market_episode_lifecycle_state(deepcopy(serialized))
        self.assertEqual(restored, state)
        interrupted = interrupt_market_episode_state_on_restart(restored)
        self.assertTrue(interrupted.interrupted)
        self.assertEqual(interrupted.active_episode.episode_id, started.episode_id)
        warm = advance(interrupted, BASE + 10_000, direction="WARMING")
        self.assertIsNone(warm.next_state.pending_resume)
        self.assertEqual(warm.transitions, ())
        one = advance(warm.next_state, BASE + 15_000)
        self.assertTrue(one.next_state.interrupted)
        self.assertEqual(one.next_state.pending_resume.count, 1)
        two = advance(one.next_state, BASE + 20_000)
        self.assertFalse(two.next_state.interrupted)
        self.assertEqual(two.transitions, ())
        self.assertEqual(two.next_state.active_episode.episode_id, started.episode_id)
        self.assertEqual(deserialize_market_episode_lifecycle_state(
            serialize_market_episode_lifecycle_state(two.next_state)), two.next_state)

        interrupted = interrupt_market_episode_state_on_restart(state)
        opposite = advance(interrupted, BASE + 10_000, direction="BROAD_DROP")
        confirmed = advance(opposite.next_state, BASE + 15_000, direction="BROAD_DROP")
        self.assertEqual([event.transition for event in confirmed.transitions], ["REVERSED"])

    def test_serialization_rejects_malformed_state(self):
        state, _ = start()
        good = serialize_market_episode_lifecycle_state(state)
        changes = (
            lambda item: item.update(serialization_version="wrong"),
            lambda item: item.pop("last_classification_fingerprint"),
            lambda item: item.update(continuation_failure_count=-1),
            lambda item: item.update(last_evaluation_boundary_time_ms=BASE + 1),
            lambda item: item["scope"].update(universe_version="other"),
            lambda item: item["active_episode"].update(direction="NEUTRAL"),
            lambda item: item["active_episode"].update(episode_id="random"),
            lambda item: item.update(lifecycle_algorithm_version="other"),
            lambda item: item.update(pending_strengthen=[{"reason": "unknown", "count": 1}]),
        )
        for mutate in changes:
            invalid = deepcopy(good)
            mutate(invalid)
            with self.subTest(payload=invalid):
                with self.assertRaises(ValueError):
                    deserialize_market_episode_lifecycle_state(invalid)

    def test_serialization_round_trips_pending_confirmation_state(self):
        pending_start = advance(None, BASE).next_state
        state, _ = start(pace="MIXED", material_count=69)
        pending_strengthen = advance(state, BASE + 10_000,
                                     pace="ACCELERATING", material_count=70).next_state
        pending_reversal = advance(state, BASE + 10_000,
                                   direction="BROAD_DROP").next_state
        interrupted = interrupt_market_episode_state_on_restart(state)
        pending_resume = advance(interrupted, BASE + 10_000).next_state
        failed = advance(state, BASE + 10_000, direction="NEUTRAL",
                         same_count=54).next_state
        for candidate in (pending_start, pending_strengthen, pending_reversal,
                          pending_resume, failed):
            with self.subTest(candidate=candidate.current_direction_state):
                self.assertEqual(deserialize_market_episode_lifecycle_state(
                    serialize_market_episode_lifecycle_state(candidate)), candidate)

    def test_event_preserves_canonical_windows_times_metrics_and_contracts(self):
        initial = classification(BASE, outlier=True)
        first = process_market_episode_lifecycle(initial)
        confirm_input = classification(BASE + 5_000, outlier=True)
        second = process_market_episode_lifecycle(confirm_input, first.next_state)
        event = second.transitions[0]
        primary = confirm_input.windows[5]
        self.assertEqual(event.event_family, "BROAD_MOVE")
        self.assertEqual(event.windows_context,
                         tuple(confirm_input.windows[minute] for minute in (1, 5, 15)))
        self.assertEqual(event.classification, confirm_input)
        self.assertEqual(event.source_time_evidence, primary.source_time_evidence)
        self.assertEqual(event.directional_breadth, primary.breadth.rising)
        self.assertEqual(event.material_breadth, primary.breadth.material_rising)
        self.assertEqual(event.median_raw_return, primary.median_raw_return)
        self.assertEqual(event.median_normalized_movement,
                         primary.median_normalized_movement)
        self.assertEqual(event.median_acceleration, primary.median_acceleration)
        self.assertEqual(event.acceleration_breadth,
                         primary.positive_acceleration_breadth)
        self.assertEqual(event.pace, primary.pace)
        self.assertEqual(event.dispersion_mad_normalized_movement,
                         primary.dispersion_mad_normalized_movement)
        self.assertEqual(event.volume_context, primary.volume_context)
        self.assertEqual(event.isolated_outliers, primary.isolated_outliers)
        self.assertEqual(event.configured_universe, primary.configured_universe)
        self.assertEqual(event.included_symbols, primary.included_symbols)
        self.assertEqual(event.excluded_symbols, primary.excluded_symbols)
        self.assertEqual(event.episode_scope.classifier_config_version,
                         primary.classifier_config_version)
        self.assertEqual(event.evaluation_scope.movement_config_version,
                         primary.movement_config_version)
        self.assertEqual(event.episode_scope.lifecycle_algorithm_version, ALGORITHM_VERSION)
        self.assertEqual(event.episode_lifecycle_config, MarketEpisodeLifecycleConfig())
        self.assertEqual(event.evaluation_lifecycle_config, MarketEpisodeLifecycleConfig())
        self.assertEqual([item.symbol for item in event.supporting_contracts],
                         list(SYMBOLS[:80]))
        self.assertEqual([item.symbol for item in event.conflicting_contracts],
                         list(SYMBOLS[80:100]))
        self.assertEqual(event.source_time_evidence[0].last_real_event_time_ms,
                         BASE + 5_000 - 4_000)

        _, drop_event = start(direction="BROAD_DROP", pace="ACCELERATING")
        self.assertEqual(drop_event.directional_breadth,
                         drop_event.classification.windows[5].breadth.falling)
        self.assertEqual(drop_event.material_breadth,
                         drop_event.classification.windows[5].breadth.material_falling)
        self.assertEqual(drop_event.acceleration_breadth,
                         drop_event.classification.windows[5].negative_acceleration_breadth)

    def test_episode_id_changes_with_true_identity(self):
        _, baseline = start()
        variants = (
            (BASE + 10_000, {}),
            (BASE, {"direction": "BROAD_DROP"}),
            (BASE, {"universe_version": "v2"}),
            (BASE, {"classifier_config_version": "classifier-config-v2"}),
        )
        for start_boundary, kwargs in variants:
            first = advance(None, start_boundary, **kwargs)
            second = advance(first.next_state, start_boundary + 5_000, **kwargs)
            self.assertNotEqual(second.transitions[0].episode_id, baseline.episode_id)


if __name__ == "__main__":
    unittest.main()
