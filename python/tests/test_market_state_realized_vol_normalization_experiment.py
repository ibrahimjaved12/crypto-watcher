"""Explicit canonical-#71 fixtures for EXP-75-06A; run by GitHub CI."""

from dataclasses import replace
from decimal import Decimal
import math
import unittest

from market_analysis.experiments.market_state_common import (
    MarketStateExperimentPoint,
    advance_canonical_branch,
)
from market_analysis.experiments.market_state_realized_vol_normalization import (
    REALIZED_VOLATILITY_ALGORITHM_VERSION,
    REALIZED_VOL_CONFIG_30M,
    REALIZED_VOL_CONFIG_60M,
    REALIZED_VOL_CONFIGURATIONS,
    REALIZED_VOL_NORMALIZATION_UNAVAILABLE,
    REALIZED_VOL_WARMING,
    REALIZED_VOL_ZERO_SCALE,
    REALIZED_VOL_SCALE_UNAVAILABLE,
    RealizedVolatilityCandidateState,
    RealizedVolatilityNormalizationConfig,
    RealizedVolatilitySample,
    RealizedVolatilitySymbolHistory,
    PairedMarketStateRealizedVolatilityPoint,
    _candidate_z,
    _horizon_sigma,
    _realized_sigma_1m,
    _summary,
    run_market_state_realized_vol_normalization_experiment,
    transform_market_movement_with_realized_vol_normalization,
)
from market_analysis.market_episode_lifecycle import MarketEpisodeLifecycleConfig
from market_analysis.movement_classifier import (
    MarketClassifierConfig,
    SymbolSourceTimeEvidence,
)
from market_analysis.movement_metrics import (
    ALGORITHM_VERSION,
    DEFAULT_CONFIG_VERSION,
    ExcludedSymbol,
    MarketMovementConfig,
    MarketMovementEvaluation,
    MarketMovementWindowResult,
    Metric,
    SymbolMovementResult,
    WINDOWS,
    _aggregates,
    _breadth,
    _outliers,
)


SYMBOLS = ("S1", "S2", "S3", "S4", "S5")
V1_CONFIG = MarketMovementConfig()
TOL = 1e-12


def _symbol(symbol, boundary, minute, raw, mad, center, *, excluded=False,
            notional=Decimal("10")):
    current = Metric.present(raw)
    z = 0.6745 * (raw - center) / mad
    if excluded:
        normalized = Metric.missing("TEST_EXCLUDED")
        direction = Metric.missing("TEST_EXCLUDED")
    else:
        normalized = Metric.present(z)
        direction = Metric.present(
            "FLAT" if raw == 0 or abs(z) < V1_CONFIG.flat_z
            else "RISING" if raw > 0 else "FALLING")
    return SymbolMovementResult(
        symbol=symbol, instrument_id=symbol,
        provider="binance-usdm", exchange="binance", price_type="trade",
        window_minutes=minute, evaluation_boundary_time_ms=boundary,
        included=not excluded,
        exclusion_reasons=("TEST_EXCLUDED",) if excluded else (),
        current_return=current, previous_return=Metric.present(0.01),
        velocity=Metric.present(raw / (minute * 60)),
        previous_velocity=Metric.present(0.001),
        acceleration=Metric.present(0.001),
        historical_median=Metric.present(center), historical_mad=Metric.present(mad),
        normalized_z=normalized, direction=direction,
        material_rising=not excluded and raw > 0 and abs(z) >= V1_CONFIG.material_z,
        material_falling=not excluded and raw < 0 and abs(z) >= V1_CONFIG.material_z,
        current_notional_volume=Metric.present(notional), rvol=Metric.present(1.0),
        cross_sectional_z=Metric.missing("CROSS_SECTIONAL_MAD_UNAVAILABLE"),
        outlier_candidate=False,
    )


def _evaluation(boundary, *, symbols=SYMBOLS, returns=0.06, mads=0.02,
                centers=0.0, excluded_by_window=None, identity=None):
    identity = identity or {}
    excluded_by_window = excluded_by_window or {}
    raw = returns if isinstance(returns, dict) else {symbol: returns for symbol in symbols}
    mad = mads if isinstance(mads, dict) else {symbol: mads for symbol in symbols}
    center = centers if isinstance(centers, dict) else {symbol: centers for symbol in symbols}
    windows = {}
    for minute in WINDOWS:
        excluded = set(excluded_by_window.get(minute, ()))
        results = tuple(_symbol(symbol, boundary, minute, raw[symbol], mad[symbol],
                                center[symbol], excluded=symbol in excluded)
                        for symbol in symbols)
        included = tuple(item for item in results if item.included)
        eligible = len(included) >= 5 and len(included) / len(symbols) >= 0.6
        if eligible:
            results = _outliers(results, V1_CONFIG)
            included = tuple(item for item in results if item.included)
        else:
            results = tuple(replace(
                item,
                cross_sectional_z=Metric.missing("MARKET_UNIVERSE_INELIGIBLE"),
                outlier_candidate=False,
            ) for item in results)
        windows[minute] = MarketMovementWindowResult(
            algorithm_version=identity.get("movement_algorithm_version", ALGORITHM_VERSION),
            config_version=identity.get("movement_config_version", DEFAULT_CONFIG_VERSION),
            universe_id=identity.get("universe_id", "u1"),
            universe_version=identity.get("universe_version", "v1"),
            configured_universe=symbols,
            included_symbols=tuple(item.symbol for item in included),
            excluded_symbols=tuple(ExcludedSymbol(symbol, ("TEST_EXCLUDED",))
                                   for symbol in symbols if symbol in excluded),
            window_minutes=minute, provider=identity.get("provider", "binance-usdm"),
            exchange=identity.get("exchange", "binance"),
            price_type=identity.get("price_type", "trade"),
            evaluation_boundary_time_ms=boundary,
            historical_lookback_ms=100, minimum_historical_coverage_ms=50,
            market_wide_eligible=eligible, eligible_count=len(included),
            eligible_fraction=len(included) / len(symbols), symbols=results,
            breadth=_breadth(included, eligible),
            aggregates=_aggregates(included, eligible, V1_CONFIG),
        )
    return MarketMovementEvaluation(
        algorithm_version=identity.get("movement_algorithm_version", ALGORITHM_VERSION),
        config_version=identity.get("movement_config_version", DEFAULT_CONFIG_VERSION),
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


def _point(boundary, partition="development", **kwargs):
    evaluation = _evaluation(boundary, **kwargs)
    source = tuple(SymbolSourceTimeEvidence(symbol, None, None, None)
                   for symbol in evaluation.configured_universe)
    return MarketStateExperimentPoint(evaluation, source, partition)


def _ready_state(current_boundary, config=REALIZED_VOL_CONFIG_30M, *,
                 symbols=SYMBOLS, prior_returns=0.02):
    values = (prior_returns if isinstance(prior_returns, dict)
              else {symbol: prior_returns for symbol in symbols})
    histories = tuple(RealizedVolatilitySymbolHistory(
        symbol, tuple(RealizedVolatilitySample(
            current_boundary - (config.lookback_points - index) * 60_000,
            values[symbol],
        ) for index in range(config.lookback_points)),
    ) for symbol in symbols)
    return RealizedVolatilityCandidateState(
        REALIZED_VOLATILITY_ALGORITHM_VERSION, config.version,
        config.lookback_points, 60_000, ALGORITHM_VERSION, DEFAULT_CONFIG_VERSION,
        "u1", "v1", symbols, "binance-usdm", "binance", "trade",
        current_boundary - 5_000, histories,
    )


def _symbol_result(evaluation, minute=5, symbol="S1"):
    return next(item for item in evaluation.windows[minute].symbols if item.symbol == symbol)


def _single_point_summary(baseline, candidate, candidate_state):
    source = tuple(SymbolSourceTimeEvidence(symbol, None, None, None)
                   for symbol in baseline.configured_universe)
    baseline_classification, baseline_lifecycle = advance_canonical_branch(
        baseline, source, None, MarketClassifierConfig(), MarketEpisodeLifecycleConfig())
    candidate_classification, candidate_lifecycle = advance_canonical_branch(
        candidate, source, None, MarketClassifierConfig(), MarketEpisodeLifecycleConfig())
    paired = PairedMarketStateRealizedVolatilityPoint(
        baseline.evaluation_boundary_time_ms, "development", baseline, candidate,
        baseline_classification, candidate_classification,
        baseline_lifecycle.next_state, candidate_lifecycle.next_state,
        baseline_lifecycle.transitions, candidate_lifecycle.transitions,
        candidate_state, (),
    )
    return _summary((paired,), "all")


class RealizedVolatilityExperimentTests(unittest.TestCase):
    def test_fixed_versions_and_reject_arbitrary_configurations(self):
        self.assertEqual(tuple(config.lookback_points for config in REALIZED_VOL_CONFIGURATIONS),
                         (30, 60, 120))
        for config in REALIZED_VOL_CONFIGURATIONS:
            for part in ("canonical-aligned-1m-return", "sample-60000ms", "strict-prior-true",
                         "estimator-rms", "canonical-historical-median", "sqrt-minutes",
                         f"points-{config.lookback_points}"):
                self.assertIn(part, config.version)
        for changed in (
            {"version": "custom", "lookback_points": 30},
            {"version": REALIZED_VOL_CONFIG_30M.version, "lookback_points": 45},
            {"version": REALIZED_VOL_CONFIG_30M.version, "lookback_points": 30,
             "sample_interval_ms": 5_000},
            {"version": REALIZED_VOL_CONFIG_30M.version, "lookback_points": 30,
             "strict_prior": False},
            {"version": REALIZED_VOL_CONFIG_30M.version, "lookback_points": 30,
             "estimator": "sample-stddev"},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                RealizedVolatilityNormalizationConfig(**changed)

    def test_exact_rms_horizon_and_centered_normalization_formulas(self):
        samples = (RealizedVolatilitySample(0, 3.0),
                   RealizedVolatilitySample(60_000, 4.0))
        sigma = _realized_sigma_1m(samples, 2)
        self.assertTrue(sigma.available)
        self.assertAlmostEqual(sigma.value, math.sqrt(12.5), delta=TOL)
        for minute in WINDOWS:
            scale = _horizon_sigma(Metric.present(2.0), minute)
            self.assertAlmostEqual(scale.value, 2 * math.sqrt(minute), delta=TOL)
        z = _candidate_z(Metric.present(0.06), Metric.present(0.01),
                         _horizon_sigma(Metric.present(0.02), 5))
        self.assertAlmostEqual(z.value, (0.06 - 0.01) / (0.02 * math.sqrt(5)), delta=TOL)

    def test_strict_prior_extreme_sample_does_not_normalize_itself(self):
        boundary = 30 * 60_000
        state = _ready_state(boundary)
        current = _evaluation(boundary, returns=1.0, mads=0.02)
        candidate, after = transform_market_movement_with_realized_vol_normalization(
            current, REALIZED_VOL_CONFIG_30M, state)
        self.assertAlmostEqual(_symbol_result(candidate, 1).normalized_z.value, 1.0 / 0.02,
                               delta=TOL)
        self.assertEqual(after.symbol_histories[0].samples[-1].one_minute_return, 1.0)
        self.assertEqual(after.symbol_histories[0].samples[-1].evaluation_boundary_time_ms,
                         boundary)
        next_candidate, _ = transform_market_movement_with_realized_vol_normalization(
            _evaluation(boundary + 5_000, returns=1.0), REALIZED_VOL_CONFIG_30M, after)
        expected_next_sigma = math.sqrt((29 * 0.02 ** 2 + 1.0 ** 2) / 30)
        self.assertAlmostEqual(_symbol_result(next_candidate, 1).normalized_z.value,
                               1.0 / expected_next_sigma, delta=TOL)

    def test_only_minute_boundaries_append_nonoverlapping_returns(self):
        state = None
        sample_counts = []
        for boundary in range(0, 125_000, 5_000):
            _, state = transform_market_movement_with_realized_vol_normalization(
                _evaluation(boundary), REALIZED_VOL_CONFIG_30M, state)
            sample_counts.append(len(state.symbol_histories[0].samples))
        self.assertEqual(tuple(index * 5_000 for index, count in enumerate(sample_counts)
                               if index == 0 or count != sample_counts[index - 1]),
                         (0, 60_000, 120_000))
        self.assertEqual(tuple(sample.evaluation_boundary_time_ms
                               for sample in state.symbol_histories[0].samples),
                         (0, 60_000, 120_000))

    def test_strict_prior_warmup_for_all_three_lookbacks(self):
        for config in REALIZED_VOL_CONFIGURATIONS:
            with self.subTest(lookback=config.lookback_points):
                boundary = config.lookback_points * 60_000
                state = _ready_state(boundary, config)
                short = replace(state, symbol_histories=tuple(
                    replace(history, samples=history.samples[1:])
                    for history in state.symbol_histories))
                candidate, short_after = transform_market_movement_with_realized_vol_normalization(
                    _evaluation(boundary), config, short)
                self.assertEqual(_symbol_result(candidate).normalized_z.reason,
                                 REALIZED_VOL_WARMING)
                self.assertEqual(len(short_after.symbol_histories[0].samples),
                                 config.lookback_points)
                ready, _ = transform_market_movement_with_realized_vol_normalization(
                    _evaluation(boundary + 5_000), config, short_after)
                self.assertTrue(_symbol_result(ready).normalized_z.available)

    def test_zero_scale_is_explicit_and_never_falls_back_to_mad(self):
        boundary = 30 * 60_000
        state = _ready_state(boundary, prior_returns=0.0)
        candidate, _ = transform_market_movement_with_realized_vol_normalization(
            _evaluation(boundary), REALIZED_VOL_CONFIG_30M, state)
        self.assertEqual(_realized_sigma_1m(state.symbol_histories[0].samples, 30).reason,
                         REALIZED_VOL_ZERO_SCALE)
        self.assertEqual(_symbol_result(candidate).normalized_z.reason, REALIZED_VOL_ZERO_SCALE)
        self.assertTrue(_symbol_result(_evaluation(boundary)).historical_mad.available)

    def test_nonfinite_realized_variance_is_explicitly_unavailable(self):
        samples = tuple(RealizedVolatilitySample(index * 60_000, 1e308)
                        for index in range(30))
        self.assertEqual(_realized_sigma_1m(samples, 30).reason,
                         REALIZED_VOL_SCALE_UNAVAILABLE)

    def test_one_symbol_invalid_aligned_sample_clears_after_current_evaluation(self):
        symbols = (*SYMBOLS, "S6")
        boundary = 30 * 60_000
        state = _ready_state(boundary, symbols=symbols)
        baseline = _evaluation(boundary, symbols=symbols,
                               excluded_by_window={1: ("S1",)})
        current, after = transform_market_movement_with_realized_vol_normalization(
            baseline, REALIZED_VOL_CONFIG_30M, state)
        self.assertTrue(_symbol_result(current, 5, "S1").normalized_z.available)
        self.assertEqual(after.symbol_histories[0].samples, ())
        self.assertEqual(len(after.symbol_histories[1].samples), 30)
        next_candidate, next_state = transform_market_movement_with_realized_vol_normalization(
            _evaluation(boundary + 5_000, symbols=symbols), REALIZED_VOL_CONFIG_30M, after)
        self.assertEqual(_symbol_result(next_candidate, 5, "S1").normalized_z.reason,
                         REALIZED_VOL_WARMING)
        self.assertTrue(_symbol_result(next_candidate, 5, "S2").normalized_z.available)
        self.assertEqual(next_state.symbol_histories[0].samples, ())

    def test_scope_resets_all_histories_and_rejects_noncanonical_baselines(self):
        boundary = 30 * 60_000
        state = _ready_state(boundary)
        changes = (
            {"baseline_movement_algorithm_version": "older-movement"},
            {"baseline_movement_config_version": "older-config"},
            {"universe_id": "other-u"}, {"universe_version": "other-v"},
            {"provider": "other-provider"}, {"exchange": "other-exchange"},
            {"price_type": "mark"},
            {"configured_universe": tuple(reversed(SYMBOLS)),
             "symbol_histories": tuple(reversed(state.symbol_histories))},
        )
        for change in changes:
            with self.subTest(change=change):
                candidate, reset = transform_market_movement_with_realized_vol_normalization(
                    _evaluation(boundary), REALIZED_VOL_CONFIG_30M,
                    replace(state, **change))
                self.assertEqual(_symbol_result(candidate).normalized_z.reason,
                                 REALIZED_VOL_WARMING)
                self.assertEqual(tuple(len(item.samples) for item in reset.symbol_histories),
                                 (1,) * len(SYMBOLS))
        for changed_config, changed_boundary in (
            (REALIZED_VOL_CONFIG_60M, boundary),
            (REALIZED_VOL_CONFIG_30M, boundary + 10_000),
        ):
            candidate, reset = transform_market_movement_with_realized_vol_normalization(
                _evaluation(changed_boundary), changed_config, state)
            self.assertEqual(_symbol_result(candidate).normalized_z.reason,
                             REALIZED_VOL_WARMING)
            self.assertEqual(len(reset.symbol_histories[0].samples),
                             1 if changed_boundary % 60_000 == 0 else 0)
        for identity in ({"movement_algorithm_version": "unknown"},
                         {"movement_config_version": "unknown"}):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                transform_market_movement_with_realized_vol_normalization(
                    _evaluation(boundary, identity=identity), REALIZED_VOL_CONFIG_30M, state)

    def test_state_validation_rejects_malformed_samples_and_order(self):
        with self.assertRaises(ValueError):
            RealizedVolatilitySample(5_000, 0.01)
        with self.assertRaises(ValueError):
            RealizedVolatilitySample(60_000, float("nan"))
        with self.assertRaises(ValueError):
            RealizedVolatilitySymbolHistory("S1", (
                RealizedVolatilitySample(0, 0.01),
                RealizedVolatilitySample(120_000, 0.02)))
        state = _ready_state(30 * 60_000)
        with self.assertRaises(ValueError):
            replace(state, symbol_histories=tuple(reversed(state.symbol_histories)))
        with self.assertRaises(ValueError):
            replace(state, symbol_histories=(replace(
                state.symbol_histories[0],
                samples=state.symbol_histories[0].samples[:-1]),
                *state.symbol_histories[1:]))

    def test_transformation_preserves_canonical_inputs_and_cross_sectional_z(self):
        boundary = 30 * 60_000
        raw = {symbol: 0.01 + index * 0.002 for index, symbol in enumerate(SYMBOLS)}
        baseline = _evaluation(boundary, returns=raw)
        candidate, _ = transform_market_movement_with_realized_vol_normalization(
            baseline, REALIZED_VOL_CONFIG_30M, _ready_state(boundary))
        self.assertEqual(baseline.algorithm_version, ALGORITHM_VERSION)
        self.assertEqual(candidate.algorithm_version, REALIZED_VOLATILITY_ALGORITHM_VERSION)
        for minute in WINDOWS:
            before, after = baseline.windows[minute], candidate.windows[minute]
            self.assertEqual(after.algorithm_version, REALIZED_VOLATILITY_ALGORITHM_VERSION)
            self.assertEqual(after.config_version, REALIZED_VOL_CONFIG_30M.version)
            for name in ("configured_universe", "included_symbols", "excluded_symbols",
                         "eligible_count", "eligible_fraction", "market_wide_eligible",
                         "provider", "exchange", "price_type",
                         "evaluation_boundary_time_ms", "historical_lookback_ms",
                         "minimum_historical_coverage_ms"):
                self.assertEqual(getattr(after, name), getattr(before, name))
            self.assertEqual(after.aggregates.median_raw_return,
                             before.aggregates.median_raw_return)
            for old_item, new_item in zip(before.symbols, after.symbols):
                for name in ("current_return", "previous_return", "velocity",
                             "previous_velocity", "acceleration", "historical_median",
                             "historical_mad", "current_notional_volume", "rvol",
                             "included", "exclusion_reasons", "cross_sectional_z"):
                    self.assertEqual(getattr(new_item, name), getattr(old_item, name))
                if old_item.normalized_z.value and new_item.normalized_z.value:
                    self.assertEqual(math.copysign(1, old_item.normalized_z.value),
                                     math.copysign(1, new_item.normalized_z.value))

    def test_warming_preserves_universe_but_marks_derived_evidence_unavailable(self):
        baseline = _evaluation(0)
        candidate, _ = transform_market_movement_with_realized_vol_normalization(
            baseline, REALIZED_VOL_CONFIG_30M)
        for minute in WINDOWS:
            before, after = baseline.windows[minute], candidate.windows[minute]
            self.assertEqual(after.included_symbols, before.included_symbols)
            self.assertEqual(after.excluded_symbols, before.excluded_symbols)
            self.assertEqual(after.eligible_count, before.eligible_count)
            self.assertEqual(after.eligible_fraction, before.eligible_fraction)
            self.assertEqual(after.market_wide_eligible, before.market_wide_eligible)
            self.assertFalse(after.breadth.available)
            self.assertEqual(after.breadth.reason, REALIZED_VOL_NORMALIZATION_UNAVAILABLE)
            self.assertFalse(after.aggregates.median_normalized_movement.available)
            self.assertEqual(after.aggregates.median_raw_return,
                             before.aggregates.median_raw_return)
        points = (_point(0), _point(5_000))
        run = run_market_state_realized_vol_normalization_experiment(
            points, REALIZED_VOL_CONFIG_30M)
        self.assertEqual(run.paired_points[0].candidate_classification.windows[5].direction_state,
                         "UNAVAILABLE")
        self.assertEqual(dict(run.summaries["all"].candidate_unavailable_count_by_window),
                         {1: 2, 5: 2, 15: 2})

    def test_candidate_never_rescues_baseline_ineligible_universe(self):
        symbols = SYMBOLS[:4]
        boundary = 30 * 60_000
        baseline = _evaluation(boundary, symbols=symbols)
        candidate, _ = transform_market_movement_with_realized_vol_normalization(
            baseline, REALIZED_VOL_CONFIG_30M,
            _ready_state(boundary, symbols=symbols))
        for minute in WINDOWS:
            self.assertFalse(baseline.windows[minute].market_wide_eligible)
            self.assertFalse(candidate.windows[minute].market_wide_eligible)
            self.assertEqual(candidate.windows[minute].eligible_count, 4)
            self.assertFalse(candidate.windows[minute].breadth.available)

    def test_scale_changes_material_breadth_and_canonical_broad_state(self):
        boundary = 30 * 60_000
        baseline = _evaluation(boundary, returns=0.06, mads=0.02)
        state = _ready_state(boundary, prior_returns=0.1)
        candidate, _ = transform_market_movement_with_realized_vol_normalization(
            baseline, REALIZED_VOL_CONFIG_30M, state)
        self.assertEqual(_symbol_result(candidate).current_return,
                         _symbol_result(baseline).current_return)
        self.assertEqual(baseline.windows[5].breadth.material_rising.value.fraction, 1.0)
        self.assertEqual(candidate.windows[5].breadth.material_rising.value.fraction, 0.0)
        self.assertNotEqual(candidate.windows[5].aggregates.median_normalized_movement,
                            baseline.windows[5].aggregates.median_normalized_movement)
        self.assertNotEqual(candidate.windows[5].aggregates.liquidity_weighted_normalized_movement,
                            baseline.windows[5].aggregates.liquidity_weighted_normalized_movement)
        # A direct ready-state classification is sufficient to prove reuse of #72;
        # the paired runner separately proves #72/#73 branch wiring below.
        from market_analysis.experiments.market_state_common import classification_context
        from market_analysis.movement_classifier import classify_market_movement
        source = tuple(SymbolSourceTimeEvidence(symbol, None, None, None) for symbol in SYMBOLS)
        baseline_classification = classify_market_movement(
            baseline, classification_context(source, None))
        candidate_classification = classify_market_movement(
            candidate, classification_context(source, None))
        self.assertEqual(baseline_classification.windows[5].direction_state, "BROAD_RISE")
        self.assertEqual(candidate_classification.windows[5].direction_state, "NEUTRAL")

    def test_smaller_scale_can_move_flat_to_rising_without_sign_change(self):
        boundary = 30 * 60_000
        baseline = _evaluation(boundary, returns=0.02, mads=0.1)
        candidate, _ = transform_market_movement_with_realized_vol_normalization(
            baseline, REALIZED_VOL_CONFIG_30M,
            _ready_state(boundary, prior_returns=0.005))
        self.assertEqual(_symbol_result(baseline).direction.value, "FLAT")
        self.assertEqual(_symbol_result(candidate).direction.value, "RISING")
        self.assertTrue(_symbol_result(candidate).material_rising)
        self.assertEqual(_symbol_result(candidate).current_return,
                         _symbol_result(baseline).current_return)

    def test_outlier_masking_and_reverse_preserve_raw_cross_sectional_z(self):
        boundary = 30 * 60_000
        symbols = tuple(f"S{index}" for index in range(1, 11))
        raw = {symbol: 0.01 + index * 0.001 for index, symbol in enumerate(symbols)}
        raw["S10"] = 0.2
        for mad, sigma, baseline_expected, candidate_expected in (
            (0.03, 0.2, True, False),
            (0.2, 0.005, False, True),
        ):
            with self.subTest(mad=mad, sigma=sigma):
                baseline = _evaluation(boundary, symbols=symbols, returns=raw, mads=mad)
                candidate, next_state = transform_market_movement_with_realized_vol_normalization(
                    baseline, REALIZED_VOL_CONFIG_30M,
                    _ready_state(boundary, symbols=symbols, prior_returns=sigma))
                before = _symbol_result(baseline, 5, "S10")
                after = _symbol_result(candidate, 5, "S10")
                self.assertTrue(before.cross_sectional_z.available)
                self.assertGreater(abs(before.cross_sectional_z.value), 3.5)
                self.assertEqual(after.cross_sectional_z, before.cross_sectional_z)
                self.assertEqual(before.outlier_candidate, baseline_expected)
                self.assertEqual(after.outlier_candidate, candidate_expected)
                summary = _single_point_summary(baseline, candidate, next_state)
                self.assertEqual(summary.outlier_comparable_count, len(symbols) * len(WINDOWS))
                self.assertEqual(summary.both_outlier_count, 0)
                self.assertEqual(summary.baseline_only_outlier_count,
                                 len(WINDOWS) if baseline_expected else 0)
                self.assertEqual(summary.candidate_only_outlier_count,
                                 len(WINDOWS) if candidate_expected else 0)
                self.assertEqual(summary.baseline_outlier_candidate_count,
                                 len(WINDOWS) if baseline_expected else 0)
                self.assertEqual(summary.candidate_outlier_candidate_count,
                                 len(WINDOWS) if candidate_expected else 0)

    def test_warming_baseline_outlier_is_not_a_comparable_outlier_result(self):
        boundary = 30 * 60_000
        symbols = tuple(f"S{index}" for index in range(1, 11))
        raw = {symbol: 0.01 + index * 0.001 for index, symbol in enumerate(symbols)}
        raw["S10"] = 0.2
        baseline = _evaluation(boundary, symbols=symbols, returns=raw, mads=0.03)
        candidate, state = transform_market_movement_with_realized_vol_normalization(
            baseline, REALIZED_VOL_CONFIG_30M)
        self.assertTrue(_symbol_result(baseline, 5, "S10").outlier_candidate)
        self.assertEqual(_symbol_result(candidate, 5, "S10").normalized_z.reason,
                         REALIZED_VOL_WARMING)
        self.assertFalse(_symbol_result(candidate, 5, "S10").outlier_candidate)
        summary = _single_point_summary(baseline, candidate, state)
        self.assertEqual(summary.outlier_comparable_count, 0)
        self.assertEqual(summary.baseline_outlier_candidate_count, 0)
        self.assertEqual(summary.candidate_outlier_candidate_count, 0)
        self.assertEqual(summary.both_outlier_count, 0)
        self.assertEqual(summary.baseline_only_outlier_count, 0)
        self.assertEqual(summary.candidate_only_outlier_count, 0)

    def test_ready_aggregate_uses_candidate_z_and_canonical_helpers(self):
        boundary = 30 * 60_000
        symbols = tuple(f"S{index}" for index in range(1, 11))
        raw = {symbol: 0.01 + index * 0.005 for index, symbol in enumerate(symbols)}
        baseline = _evaluation(boundary, symbols=symbols, returns=raw)
        candidate, _ = transform_market_movement_with_realized_vol_normalization(
            baseline, REALIZED_VOL_CONFIG_30M,
            _ready_state(boundary, symbols=symbols, prior_returns=0.1))
        window = candidate.windows[5]
        included = tuple(item for item in window.symbols if item.included)
        expected = _aggregates(included, True, V1_CONFIG)
        self.assertEqual(window.aggregates, expected)
        self.assertEqual(window.breadth, _breadth(included, True))
        self.assertNotEqual(window.aggregates.median_normalized_movement,
                            baseline.windows[5].aggregates.median_normalized_movement)
        self.assertNotEqual(window.aggregates.trimmed_mean_normalized_movement,
                            baseline.windows[5].aggregates.trimmed_mean_normalized_movement)
        self.assertNotEqual(window.aggregates.dispersion_mad_normalized_movement,
                            baseline.windows[5].aggregates.dispersion_mad_normalized_movement)

    def test_cross_asset_fairness_math_from_comparable_primary_values(self):
        boundary = 30 * 60_000
        raw = {symbol: 0.02 * (index + 1) for index, symbol in enumerate(SYMBOLS)}
        prior = {symbol: raw[symbol] / math.sqrt(5) for symbol in SYMBOLS}
        state = _ready_state(boundary, prior_returns=prior)
        point = _point(boundary, returns=raw)
        # The runner builds its own history causally, so compare its summary's
        # pure diagnostic calculation using a direct ready candidate paired with
        # canonical branch results and immutable per-boundary evidence.
        from market_analysis.experiments.market_state_realized_vol_normalization import _summary
        from market_analysis.experiments.market_state_common import advance_canonical_branch
        from market_analysis.market_episode_lifecycle import MarketEpisodeLifecycleConfig
        from market_analysis.movement_classifier import MarketClassifierConfig
        candidate, after = transform_market_movement_with_realized_vol_normalization(
            point.movement_evaluation, REALIZED_VOL_CONFIG_30M, state)
        baseline_classification, baseline_result = advance_canonical_branch(
            point.movement_evaluation, point.source_time_evidence, None,
            MarketClassifierConfig(), MarketEpisodeLifecycleConfig())
        candidate_classification, candidate_result = advance_canonical_branch(
            candidate, point.source_time_evidence, None,
            MarketClassifierConfig(), MarketEpisodeLifecycleConfig())
        from market_analysis.experiments.market_state_realized_vol_normalization import (
            PairedMarketStateRealizedVolatilityPoint,
        )
        paired = PairedMarketStateRealizedVolatilityPoint(
            boundary, "development", point.movement_evaluation, candidate,
            baseline_classification, candidate_classification,
            baseline_result.next_state, candidate_result.next_state,
            baseline_result.transitions, candidate_result.transitions, after, (),
        )
        summary = _summary((paired,), "all")
        diagnostics = summary.symbol_fairness_diagnostics
        self.assertEqual(tuple(item.symbol for item in diagnostics), SYMBOLS)
        for index, item in enumerate(diagnostics):
            self.assertEqual(item.comparable_primary_count, 1)
            self.assertAlmostEqual(item.median_abs_candidate_z, 1.0, delta=TOL)
            expected_baseline = abs(0.6745 * raw[item.symbol] / 0.02)
            self.assertAlmostEqual(item.median_abs_baseline_z, expected_baseline,
                                   delta=TOL)
        self.assertAlmostEqual(summary.candidate_cross_symbol_median_abs_z_mad, 0.0,
                               delta=TOL)
        self.assertAlmostEqual(summary.baseline_cross_symbol_median_abs_z_mad,
                               0.6745, delta=TOL)

    def test_runner_is_causal_and_partition_summaries_ignore_future(self):
        points = tuple(_point(
            index * 5_000,
            "development" if index < 24 else "validation" if index < 36 else "test",
            returns=0.02 if index < 30 else 0.06,
        ) for index in range(42))
        full = run_market_state_realized_vol_normalization_experiment(
            points, REALIZED_VOL_CONFIG_30M)
        repeat = run_market_state_realized_vol_normalization_experiment(
            points, REALIZED_VOL_CONFIG_30M)
        dev = run_market_state_realized_vol_normalization_experiment(
            points[:24], REALIZED_VOL_CONFIG_30M)
        validation = run_market_state_realized_vol_normalization_experiment(
            points[:36], REALIZED_VOL_CONFIG_30M)
        self.assertEqual(full.paired_points, repeat.paired_points)
        self.assertEqual(dict(full.summaries), dict(repeat.summaries))
        self.assertEqual(full.paired_points[:24], dev.paired_points)
        self.assertEqual(full.paired_points[:36], validation.paired_points)
        self.assertEqual(full.summaries["development"], dev.summaries["development"])
        self.assertEqual(full.summaries["validation"], validation.summaries["validation"])
        self.assertIs(full.paired_points[0].baseline_evaluation, points[0].movement_evaluation)
        self.assertEqual(full.paired_points[0].candidate_classification.windows[5].direction_state,
                         "UNAVAILABLE")
        self.assertEqual(full.summaries["all"].candidate_episode_count, 0)
        self.assertGreater(full.summaries["all"].baseline_episode_count, 0)
        self.assertEqual(full.summaries["test"].evaluation_count, 6)
        for bad in ((points[0], points[0]), (points[1], points[0]),
                    (points[0], points[2]), (points[30], points[0])):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                run_market_state_realized_vol_normalization_experiment(
                    bad, REALIZED_VOL_CONFIG_30M)

    def test_causal_runner_reaches_ready_candidate_and_changes_primary_regime(self):
        points = tuple(_point(boundary, returns=0.06, mads=0.02)
                       for boundary in range(0, 30 * 60_000 + 5_000, 5_000))
        result = run_market_state_realized_vol_normalization_experiment(
            points, REALIZED_VOL_CONFIG_30M)
        last = result.paired_points[-1]
        self.assertIs(last.baseline_evaluation, points[-1].movement_evaluation)
        self.assertEqual(last.symbol_evidence[0].prior_history_count, 30)
        self.assertAlmostEqual(last.symbol_evidence[0].sigma_1m.value, 0.06, delta=TOL)
        self.assertTrue(last.candidate_evaluation.windows[5].breadth.available)
        self.assertEqual(last.baseline_classification.windows[5].direction_state, "BROAD_RISE")
        self.assertEqual(last.candidate_classification.windows[5].direction_state, "NEUTRAL")
        self.assertIsNotNone(last.baseline_lifecycle_state.active_episode)
        self.assertIsNone(last.candidate_lifecycle_state.active_episode)
        self.assertEqual(dict(result.summaries["all"].candidate_ready_count_by_window),
                         {1: 12, 5: 12, 15: 12})
        self.assertEqual(dict(result.summaries["all"].direction_state_disagreement_count_by_window)[5],
                         12)

    def test_empty_summary_uses_none_for_descriptive_medians(self):
        result = run_market_state_realized_vol_normalization_experiment(
            (), REALIZED_VOL_CONFIG_30M)
        for summary in result.summaries.values():
            self.assertEqual(summary.evaluation_count, 0)
            self.assertEqual(dict(summary.comparable_z_count_by_window), {1: 0, 5: 0, 15: 0})
            self.assertEqual(dict(summary.median_absolute_z_difference_by_window),
                             {1: None, 5: None, 15: None})
            self.assertIsNone(summary.baseline_cross_symbol_median_abs_z_mad)
            self.assertIsNone(summary.median_signed_candidate_minus_baseline_onset_ms)


if __name__ == "__main__":
    unittest.main()
