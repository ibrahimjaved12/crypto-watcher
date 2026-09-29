"""Focused EXP-75-06B timing, range, provenance, and identity fixtures."""

from dataclasses import replace
from decimal import Decimal, localcontext
import math
from types import SimpleNamespace
import unittest

from market_analysis.experiments.market_state_atr_normalization import (
    ATR_CONFIG_30M, ATR_INSUFFICIENT_HISTORY, ATR_KAPPA,
    ATR_LATEST_NOT_YET_AVAILABLE, ATR_MISSING_ADJACENT_MINUTE,
    ATR_MISSING_LATEST_MINUTE, ATR_PREVIOUS_CLOSE_NOT_YET_AVAILABLE,
    ATR_ZERO_SCALE, ATR_NUMERATOR_UNAVAILABLE, _ATRVisibilityCursor,
    _atr_metrics, _candidate_symbol, _scale,
    _validate_inputs,
)
from market_analysis.experiments.market_state_common import MarketStateExperimentPoint
from market_analysis.historical_atr_extension import _manifest
from market_analysis.historical_experiment_batch import (
    EXPERIMENT_SUITE_V1, experiment_stream_sha256,
)
from market_analysis.historical_ohlc_evidence import (
    BinanceTradeOHLCEvidence, CompletedTradeOHLCCandle,
)
from market_analysis.historical_replay import (
    HistoricalReplayRunManifest, ReplayPartitionPlan,
)
from market_analysis.market_episode_lifecycle import MarketEpisodeLifecycleConfig
from market_analysis.movement_classifier import (
    MarketClassifierConfig, SymbolSourceTimeEvidence,
)
from market_analysis.movement_metrics import (
    ALGORITHM_VERSION, DEFAULT_CONFIG_VERSION, MarketMovementEvaluation,
    Metric, SymbolMovementResult, WINDOWS,
)


MINUTE = 60_000
SYMBOL = "BTCUSDT"
DATASET_SHA = "a" * 64


def _candle(index, *, high="101", low="99", close="100",
            opening="100", first_seen=None):
    start = index * MINUTE
    return CompletedTradeOHLCCandle(
        SYMBOL, f"binance-usdm:{SYMBOL}", start, start + MINUTE - 1,
        Decimal(opening), Decimal(high), Decimal(low), Decimal(close),
        start + MINUTE if first_seen is None else first_seen,
    )


def _evidence(candles, *, dataset_sha=DATASET_SHA, symbols=(SYMBOL,)):
    return BinanceTradeOHLCEvidence(
        "fixture-dataset", "v1", dataset_sha, symbols, tuple(candles))


def _thirty_range_candles():
    return (_candle(0, high="100", low="100"), *(
        _candle(index) for index in range(1, 30)
    ), _candle(30, high="110", low="100", close="101"))


def _manifest_fixture(*, sha=DATASET_SHA, symbols=(SYMBOL,),
                      price_type="trade", boundary=31 * MINUTE + 5_000):
    return HistoricalReplayRunManifest(
        algorithm_version="historical-replay-v1",
        policy_version="historical-replay-policy-v1",
        dataset_id="fixture-dataset",
        dataset_version="v1",
        dataset_content_sha256=sha,
        provider="binance-usdm",
        exchange="binance",
        price_type=price_type,
        universe_id="u1",
        universe_version="v1",
        configured_universe=symbols,
        instrument_contract=(),
        movement_algorithm_version=ALGORITHM_VERSION,
        movement_config_version=DEFAULT_CONFIG_VERSION,
        movement_config_parameters=(),
        output_start_boundary_time_ms=boundary,
        output_end_boundary_time_ms=boundary,
        finalization_grace_ms=2_000,
        run_fingerprint="f" * 64,
    )


def _point(boundary=31 * MINUTE + 5_000):
    windows = {
        minute: SimpleNamespace(evaluation_boundary_time_ms=boundary)
        for minute in WINDOWS
    }
    evaluation = MarketMovementEvaluation(
        ALGORITHM_VERSION, DEFAULT_CONFIG_VERSION, "u1", "v1", (SYMBOL,),
        "binance-usdm", "binance", "trade", boundary, MINUTE, MINUTE,
        windows,
    )
    return MarketStateExperimentPoint(
        evaluation, (SymbolSourceTimeEvidence(SYMBOL, None, None, None),),
        "development",
    )


class ATRRangeEvidenceTests(unittest.TestCase):
    def test_true_range_uses_previous_close_across_price_gap(self):
        candles = (
            _candle(0, high="100", low="100"),
            _candle(1, opening="110", high="112", low="109", close="111"),
        )
        history = _ATRVisibilityCursor(_evidence(candles)).histories_at(
            2 * MINUTE + 5_000)[SYMBOL]
        self.assertEqual(history.true_ranges_newest_first, (Decimal("12"),))

    def test_hand_calculated_true_ranges_and_calibrated_sma_scale(self):
        evidence = _evidence(_thirty_range_candles())
        history = _ATRVisibilityCursor(evidence).histories_at(
            31 * MINUTE + 5_000)[SYMBOL]
        self.assertEqual(history.true_ranges_newest_first,
                         (Decimal("10"),) + (Decimal("2"),) * 29)
        raw, relative = _atr_metrics(history, ATR_CONFIG_30M.lookback_minutes)
        self.assertTrue(raw.available)
        with localcontext() as context:
            context.prec = 50
            expected_raw = Decimal(68) / Decimal(30)
            expected_relative = expected_raw / Decimal(101)
        self.assertEqual(raw.value, expected_raw)
        self.assertEqual(relative.value, expected_relative)
        self.assertAlmostEqual(_scale(relative, 5).value,
                               ATR_KAPPA * float(expected_relative) * math.sqrt(5))

    def test_candidate_score_uses_v1_numerator_without_mad_multiplier(self):
        item = SymbolMovementResult(
            symbol=SYMBOL, instrument_id=f"binance-usdm:{SYMBOL}",
            provider="binance-usdm", exchange="binance", price_type="trade",
            window_minutes=5, evaluation_boundary_time_ms=31 * MINUTE + 5_000,
            included=True, exclusion_reasons=(),
            current_return=Metric.present(0.06),
            previous_return=Metric.present(0.01),
            velocity=Metric.present(0.001),
            previous_velocity=Metric.present(0.001),
            acceleration=Metric.present(0.0),
            historical_median=Metric.present(0.01),
            historical_mad=Metric.present(0.02),
            normalized_z=Metric.present(0.6745 * (0.06 - 0.01) / 0.02),
            direction=Metric.present("RISING"),
            material_rising=True, material_falling=False,
            current_notional_volume=Metric.present(Decimal("10")),
            rvol=Metric.present(1.0),
            cross_sectional_z=Metric.missing("UNAVAILABLE"),
            outlier_candidate=False,
        )
        candidate = _candidate_symbol(item, Metric.present(0.025))
        self.assertAlmostEqual(candidate.normalized_z.value, 2.0)
        self.assertEqual(candidate.current_return, item.current_return)
        self.assertEqual(candidate.historical_median, item.historical_median)
        self.assertEqual(candidate.historical_mad, item.historical_mad)
        self.assertEqual(_candidate_symbol(
            replace(item, historical_median=Metric.missing("NO_CENTER")),
            Metric.present(0.025)).normalized_z.reason, ATR_NUMERATOR_UNAVAILABLE)

    def test_exact_boundary_excludes_just_completed_minute(self):
        cursor = _ATRVisibilityCursor(_evidence(_thirty_range_candles()))
        exact = cursor.histories_at(31 * MINUTE)[SYMBOL]
        self.assertEqual(exact.latest_expected_end_time_ms, 30 * MINUTE)
        self.assertEqual(len(exact.true_ranges_newest_first), 29)
        self.assertEqual(_atr_metrics(exact, 30)[0].reason,
                         ATR_INSUFFICIENT_HISTORY)
        five_seconds_later = cursor.histories_at(31 * MINUTE + 5_000)[SYMBOL]
        self.assertEqual(five_seconds_later.latest_expected_end_time_ms,
                         31 * MINUTE)
        self.assertTrue(_atr_metrics(five_seconds_later, 30)[0].available)

    def test_previous_close_seen_exactly_at_t_is_still_ineligible(self):
        boundary = 31 * MINUTE + 5_000
        candles = list(_thirty_range_candles())
        candles[29] = replace(candles[29], first_seen_at_ms=boundary)
        evidence = _evidence(candles)
        # The #111 inclusive query exposes the preceding close at t.
        inclusive = evidence.as_of(SYMBOL, "1m", boundary, limit=1)
        self.assertIsNotNone(inclusive.candles[-1].previous_close)
        cursor = _ATRVisibilityCursor(evidence)
        exact = cursor.histories_at(boundary)[SYMBOL]
        self.assertEqual(exact.stopped_reason, ATR_PREVIOUS_CLOSE_NOT_YET_AVAILABLE)
        self.assertFalse(_atr_metrics(exact, 30)[0].available)
        later = cursor.histories_at(boundary + 5_000)[SYMBOL]
        self.assertEqual(later.latest_expected_end_time_ms,
                         exact.latest_expected_end_time_ms)
        self.assertIsNot(later, exact)
        self.assertTrue(_atr_metrics(later, 30)[0].available)
        self.assertIs(
            cursor.histories_at(boundary + 10_000)[SYMBOL], later)

    def test_latest_missing_and_late_latest_have_distinct_reasons(self):
        candles = _thirty_range_candles()
        boundary = 31 * MINUTE + 5_000
        missing = _ATRVisibilityCursor(_evidence(candles[:-1]))
        self.assertEqual(missing.histories_at(boundary)[SYMBOL].stopped_reason,
                         ATR_MISSING_LATEST_MINUTE)
        late_candles = (*candles[:-1], replace(
            candles[-1], first_seen_at_ms=boundary + 5_000))
        late = _ATRVisibilityCursor(_evidence(late_candles))
        self.assertEqual(late.histories_at(boundary)[SYMBOL].stopped_reason,
                         ATR_LATEST_NOT_YET_AVAILABLE)

    def test_gap_recovers_only_after_it_leaves_the_lookback(self):
        candles = tuple(_candle(index) for index in range(47) if index != 15)
        cursor = _ATRVisibilityCursor(_evidence(candles))
        blocked = cursor.histories_at(31 * MINUTE + 5_000)[SYMBOL]
        self.assertEqual(blocked.stopped_reason, ATR_MISSING_ADJACENT_MINUTE)
        self.assertFalse(_atr_metrics(blocked, 30)[0].available)
        recovered = cursor.histories_at(47 * MINUTE + 5_000)[SYMBOL]
        self.assertTrue(_atr_metrics(recovered, 30)[0].available)

    def test_late_older_candle_invalidates_same_minute_snapshot(self):
        boundary = 31 * MINUTE + 5_000
        candles = list(_thirty_range_candles())
        candles[15] = replace(candles[15],
                              first_seen_at_ms=boundary + 5_000)
        cursor = _ATRVisibilityCursor(_evidence(candles))
        before = cursor.histories_at(boundary)[SYMBOL]
        self.assertEqual(before.stopped_reason,
                         ATR_PREVIOUS_CLOSE_NOT_YET_AVAILABLE)
        self.assertIs(cursor.histories_at(boundary + 5_000)[SYMBOL], before)
        after = cursor.histories_at(boundary + 10_000)[SYMBOL]
        self.assertEqual(after.latest_expected_end_time_ms,
                         before.latest_expected_end_time_ms)
        self.assertIsNot(after, before)
        self.assertTrue(_atr_metrics(after, 30)[0].available)

    def test_zero_true_range_is_valid_but_all_zero_scale_is_not(self):
        flat = tuple(_candle(index, high="100", low="100")
                     for index in range(31))
        history = _ATRVisibilityCursor(_evidence(flat)).histories_at(
            31 * MINUTE + 5_000)[SYMBOL]
        self.assertEqual(history.true_ranges_newest_first, (Decimal(0),) * 30)
        raw, relative = _atr_metrics(history, 30)
        self.assertTrue(raw.available)
        self.assertEqual(raw.value, Decimal(0))
        self.assertEqual(_scale(relative, 1).reason, ATR_ZERO_SCALE)
        changed = (*flat[:-1], _candle(
            30, high="101", low="99", close="100"))
        mixed = _ATRVisibilityCursor(_evidence(changed)).histories_at(
            31 * MINUTE + 5_000)[SYMBOL]
        self.assertEqual(mixed.true_ranges_newest_first[1:], (Decimal(0),) * 29)
        self.assertTrue(_scale(_atr_metrics(mixed, 30)[1], 1).available)

    def test_dataset_symbols_and_trade_provenance_rejected(self):
        point = _point()
        manifest = _manifest_fixture()
        with self.assertRaisesRegex(ValueError, "dataset identity"):
            _validate_inputs((point,), _evidence((), dataset_sha="b" * 64),
                             manifest)
        with self.assertRaisesRegex(ValueError, "symbols or trade-price provenance"):
            _validate_inputs((point,), _evidence(
                (), symbols=("ETHUSDT", SYMBOL)), manifest)
        with self.assertRaisesRegex(ValueError, "symbols or trade-price provenance"):
            _validate_inputs((point,), _evidence(()),
                             replace(manifest, price_type="mark"))

    def test_extension_identity_tracks_ohlc_without_changing_old_stream(self):
        self.assertEqual(len(EXPERIMENT_SUITE_V1), 28)
        self.assertNotIn("EXP-75-06B",
                         tuple(item.experiment_id for item in EXPERIMENT_SUITE_V1))
        boundary = 31 * MINUTE + 5_000
        manifest = _manifest_fixture(boundary=boundary)
        point = _point(boundary)
        replay = SimpleNamespace(
            manifest=manifest,
            points=(SimpleNamespace(
                point_id="fixed-replay-point",
                evaluation_boundary_time_ms=boundary),),
        )
        plan = ReplayPartitionPlan(boundary + 5_000, boundary + 10_000)
        stream_sha = experiment_stream_sha256(replay, (point,), plan)
        first = _evidence(())
        second = _evidence((_candle(0),))
        self.assertNotEqual(first.evidence_sha256, second.evidence_sha256)

        def extension_manifest(evidence, output_sha="c" * 64):
            prepared = SimpleNamespace(
                archive_dataset=SimpleNamespace(ohlc_evidence=evidence),
                replay_result=replay, experiment_points=(point,),
                partition_plan=plan,
            )
            return _manifest(
                prepared, MarketClassifierConfig(),
                MarketEpisodeLifecycleConfig(), output_sha,
            )

        before, after = extension_manifest(first), extension_manifest(second)
        self.assertEqual(manifest.run_fingerprint, replay.manifest.run_fingerprint)
        self.assertEqual(stream_sha, before.experiment_stream_sha256)
        self.assertEqual(stream_sha, after.experiment_stream_sha256)
        self.assertNotEqual(before.extension_run_fingerprint,
                            after.extension_run_fingerprint)
        changed_output = extension_manifest(first, "d" * 64)
        self.assertEqual(before.extension_run_fingerprint,
                         changed_output.extension_run_fingerprint)
        self.assertNotEqual(before.candidate_output_evidence_sha256,
                            changed_output.candidate_output_evidence_sha256)


if __name__ == "__main__":
    unittest.main()
