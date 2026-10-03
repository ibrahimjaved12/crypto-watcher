"""Causal availability, canonical parity, and stable #35 replay fixtures."""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from market_analysis import historical_replay as replay_module
from market_analysis.experiments.market_state_common import validate_experiment_points
from market_analysis.historical_replay import (
    HISTORICAL_REPLAY_ALGORITHM_VERSION, HISTORICAL_REPLAY_POLICY_VERSION,
    HistoricalReplayCheckpoint, HistoricalReplayConfig,
    HistoricalReplayDatasetManifest, HistoricalReplayInstrument,
    HistoricalReplayMovementCandle, HistoricalReplayRequest,
    HistoricalReplaySourceInterval, HistoricalReplayTrade, ReplayPartitionPlan,
    run_historical_market_replay, to_market_state_experiment_points,
    run_bounded_historical_market_replay,
)
from market_analysis.historical_replay_runtime import ReplayCheckpointStore
from market_analysis import historical_replay_runtime as runtime_module
from market_analysis.movement import (
    BUCKET_INTERVAL_MS, MAX_LAST_TRADE_AGE_MS, WINDOW_BUCKETS,
    MarketObservation, MovementBucketEngine,
)
from market_analysis.movement_history import (
    CompletedMovementCandle, build_historical_window_inputs,
)
from market_analysis.movement_metrics import (
    MarketMovementConfig, MarketMovementInput, MarketMovementSymbolInput,
    MarketUniverseInput, WINDOWS, calculate_market_movement,
)


BASE = 1_800_000_000_000
OUTPUT = BASE + 31 * 60_000
WARMUP_MS = (WINDOW_BUCKETS[15] - 1) * BUCKET_INTERVAL_MS + MAX_LAST_TRADE_AGE_MS
ENGINE_START = OUTPUT - WARMUP_MS
SYMBOL = "BTCUSDT"
DATASET = HistoricalReplayDatasetManifest("fixture", "v1", "a" * 64)


def _trade(trade_time, *, symbol=SYMBOL, first_seen=None, event_time=None,
           trade_id=None, price="100", quantity="1"):
    return HistoricalReplayTrade(
        symbol, f"binance-usdm:{symbol}", Decimal(price), Decimal(quantity),
        trade_time if event_time is None else event_time, trade_time,
        trade_time if trade_id is None else trade_id,
        trade_time if first_seen is None else first_seen,
    )


def _candle(open_time, close, *, first_seen=None, symbol=SYMBOL):
    return HistoricalReplayMovementCandle(
        symbol, open_time, Decimal(close), Decimal("2"), Decimal("200"),
        open_time + 60_000 if first_seen is None else first_seen,
    )


def _request(*, symbols=(SYMBOL,), trades=None, candles=(), intervals=None,
             grace=2_000, dataset=DATASET, movement_config=None,
             end=OUTPUT + 15_000, compatible=True):
    universe = MarketUniverseInput("u1", "v1", symbols)
    instruments = tuple(HistoricalReplayInstrument(
        symbol, f"binance-usdm:{symbol}", compatible) for symbol in symbols)
    if trades is None:
        trades = tuple(_trade(ENGINE_START, symbol=symbol, trade_id=index + 1)
                       for index, symbol in enumerate(symbols))
    if intervals is None:
        intervals = tuple(HistoricalReplaySourceInterval(
            symbol, ENGINE_START, end, "LIVE") for symbol in symbols)
    return HistoricalReplayRequest(
        dataset, universe, instruments, tuple(trades), tuple(candles),
        tuple(intervals), HistoricalReplayConfig(
            OUTPUT, end, grace, movement_config or MarketMovementConfig()),
    )


class HistoricalReplayTests(unittest.TestCase):
    def test_movement_engine_snapshot_restores_pending_carry_and_late_state(self):
        engine = MovementBucketEngine(f"binance-usdm:{SYMBOL}")
        first = MarketObservation("binance-usdm", f"binance-usdm:{SYMBOL}",
                                  "trade", Decimal("100"), Decimal("1"),
                                  ENGINE_START, ENGINE_START, 1, ENGINE_START)
        engine.observe((first,))
        engine.advance(ENGINE_START, "LIVE")
        engine.advance(ENGINE_START + 5_000, "LIVE")
        engine.observe((first,))
        future = replace(first, event_time_ms=ENGINE_START + 10_000,
                         trade_time_ms=ENGINE_START + 10_000,
                         aggregate_trade_id=2, received_at_ms=ENGINE_START + 10_000)
        engine.observe((future,))
        restored = MovementBucketEngine.from_state(engine.snapshot_state())
        self.assertEqual(restored.snapshot_state(), engine.snapshot_state())
        for boundary, state in ((ENGINE_START + 10_000, "LIVE"),
                                (ENGINE_START + 15_000, "UNAVAILABLE"),
                                (ENGINE_START + 20_000, "LIVE")):
            self.assertEqual(restored.advance(boundary, state),
                             engine.advance(boundary, state))
            self.assertEqual(restored.snapshot_state(), engine.snapshot_state())

    def test_durable_final_checkpoint_roundtrip_and_revision_identity(self):
        request = _request(trades=(_trade(ENGINE_START, trade_id=1),
                                   _trade(OUTPUT + 5_000, trade_id=2)))
        full = run_historical_market_replay(request)
        trades, _, _, _ = replay_module._canonical_inputs(request)
        class Index:
            def iter_replay_trades(self, symbols, after=None):
                return (row for row in trades
                        if after is None or
                        (row.first_seen_at_ms, symbols.index(row.symbol),
                         row.trade_time_ms, row.aggregate_trade_id) > after)
        bounded = SimpleNamespace(
            dataset=request.dataset, universe=request.universe,
            instruments=request.instruments, candles=request.candles,
            source_intervals=request.source_intervals, config=request.config,
            trade_stream_manifest=SimpleNamespace(duplicate_row_count=0),
            _index=Index())
        identity = {
            "run_fingerprint": full.manifest.run_fingerprint,
            "scientific_producer_revision": "c277011a3c1d3e0db2c5224e49386a375a6afc59",
            "runtime_implementation_revision": "different-runtime-commit",
        }
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = ReplayCheckpointStore(root, identity, OUTPUT,
                                          request.config.output_end_boundary_time_ms)
            bounded_run = run_bounded_historical_market_replay(
                bounded, boundary_callback=store.add_point)
            self.assertEqual(bounded_run, full)
            del bounded_run
            del store
            resumed_store = ReplayCheckpointStore(root, identity, OUTPUT,
                                                  request.config.output_end_boundary_time_ms)
            state = resumed_store.load_latest()
            self.assertEqual(state.completed_boundary_time_ms,
                             request.config.output_end_boundary_time_ms)
            self.assertEqual(tuple(resumed_store.points), full.points)
            self.assertEqual(
                run_bounded_historical_market_replay(bounded, runtime_state=state).diagnostics,
                full.diagnostics)
            changed = {**identity, "runtime_implementation_revision": "another-commit"}
            with self.assertRaisesRegex(ValueError, "identity"):
                ReplayCheckpointStore(root, changed, OUTPUT,
                                      request.config.output_end_boundary_time_ms).load_latest()
            chunk = next((root / "chunks").glob("*.jsonl"))
            chunk_bytes = chunk.read_bytes()
            chunk.write_bytes(chunk_bytes[:-1])
            with self.assertRaisesRegex(ValueError, "chunk SHA"):
                ReplayCheckpointStore(root, identity, OUTPUT,
                                      request.config.output_end_boundary_time_ms).load_latest()
            chunk.write_bytes(chunk_bytes)
            checkpoint = next(root.glob("checkpoint-*.json"))
            checkpoint.write_text(checkpoint.read_text()[:-2], encoding="utf-8")
            with self.assertRaises(ValueError):
                ReplayCheckpointStore(root, identity, OUTPUT,
                                      request.config.output_end_boundary_time_ms).load_latest()

    def test_checkpoint_metadata_publication_is_last(self):
        request = _request()
        full = run_historical_market_replay(request)
        identity = {"run_fingerprint": full.manifest.run_fingerprint}
        with tempfile.TemporaryDirectory() as folder:
            store = ReplayCheckpointStore(Path(folder), identity, OUTPUT,
                                          request.config.output_end_boundary_time_ms)
            with patch.object(runtime_module.os, "replace", side_effect=OSError("crash")):
                with self.assertRaisesRegex(OSError, "crash"):
                    store.add_point(full.points[0], None)
                    for point in full.points[1:-1]:
                        store.add_point(point, None)
                    # The state object is captured by the shared replay core.
                    trades, _, _, _ = replay_module._canonical_inputs(request)
                    class Index:
                        def iter_replay_trades(self, symbols, after=None):
                            return iter(trades)
                    bounded = SimpleNamespace(
                        dataset=request.dataset, universe=request.universe,
                        instruments=request.instruments, candles=request.candles,
                        source_intervals=request.source_intervals, config=request.config,
                        trade_stream_manifest=SimpleNamespace(duplicate_row_count=0),
                        _index=Index())
                    run_bounded_historical_market_replay(
                        bounded, boundary_callback=lambda point, state:
                        store.add_point(point, state) if state is not None else None)
            self.assertIsNone(ReplayCheckpointStore(
                Path(folder), identity, OUTPUT,
                request.config.output_end_boundary_time_ms).load_latest())

    def test_hourly_resume_at_two_boundaries_matches_uninterrupted(self):
        end = OUTPUT + 2 * 3_600_000 + 5_000
        request = _request(end=end, trades=(
            _trade(ENGINE_START, trade_id=1),
            _trade(OUTPUT + 3_600_000, trade_id=2),
            _trade(OUTPUT + 2 * 3_600_000, trade_id=3)))
        full = run_historical_market_replay(request)
        trades, _, _, _ = replay_module._canonical_inputs(request)
        class Index:
            def iter_replay_trades(self, symbols, after=None):
                return (row for row in trades if after is None or
                        (row.first_seen_at_ms, symbols.index(row.symbol),
                         row.trade_time_ms, row.aggregate_trade_id) > after)
        bounded = SimpleNamespace(
            dataset=request.dataset, universe=request.universe,
            instruments=request.instruments, candles=request.candles,
            source_intervals=request.source_intervals, config=request.config,
            trade_stream_manifest=SimpleNamespace(duplicate_row_count=0),
            _index=Index())
        class Interrupted(Exception):
            pass
        with tempfile.TemporaryDirectory() as folder:
            for hour in (1, 2):
                root = Path(folder) / str(hour)
                identity = {"run_fingerprint": full.manifest.run_fingerprint,
                            "scientific_producer_revision": "frozen",
                            "runtime_implementation_revision": "runtime"}
                def stop(event, details):
                    if (event == "REPLAY_CHECKPOINT_WRITTEN"
                            and details["completed_boundary"] == OUTPUT + hour * 3_600_000):
                        raise Interrupted()
                store = ReplayCheckpointStore(root, identity, OUTPUT, end,
                                              progress=stop)
                with self.assertRaises(Interrupted):
                    run_bounded_historical_market_replay(
                        bounded, boundary_callback=store.add_point)
                del store
                restored = ReplayCheckpointStore(root, identity, OUTPUT, end)
                state = restored.load_latest()
                self.assertEqual(state.completed_boundary_time_ms,
                                 OUTPUT + hour * 3_600_000)
                remainder = run_bounded_historical_market_replay(
                    bounded, runtime_state=state, boundary_callback=restored.add_point)
                self.assertEqual(tuple(restored.points), full.points)
                self.assertEqual(remainder.manifest, full.manifest)
                self.assertEqual(remainder.diagnostics, full.diagnostics)

    def test_historical_cache_strict_prior_and_lookback_expiry(self):
        minute = 60_000
        config = MarketMovementConfig(
            historical_lookback_ms=65_000,
            minimum_historical_coverage_ms=minute)
        candles = [CompletedMovementCandle(
            OUTPUT - offset * minute, Decimal(str(110 - offset)),
            Decimal("2"), Decimal("200")) for offset in (3, 2, 1)]
        boundaries = (OUTPUT, OUTPUT + 5_000, OUTPUT + 10_000,
                      OUTPUT + 15_000)
        expected = tuple(build_historical_window_inputs(candles, boundary, config)
                         for boundary in boundaries)
        cache = replay_module._ReplayHistoricalInputCache()
        with patch.object(replay_module, "build_historical_window_inputs",
                          wraps=build_historical_window_inputs) as builder:
            actual = tuple(cache.get(SYMBOL, candles, boundary, config, 0)
                           for boundary in boundaries)
            other_symbol = cache.get("ETHUSDT", candles, boundaries[-1], config, 0)
        self.assertEqual(actual, expected)
        self.assertEqual(builder.call_count, 4)
        self.assertIs(actual[2], actual[3])
        self.assertEqual(other_symbol, actual[-1])
        self.assertIsNot(other_symbol, actual[-1])
        self.assertNotEqual(actual[0], actual[1])  # end == OUTPUT enters at +5s
        self.assertNotEqual(actual[1], actual[2])  # older sample expires at +10s

    def test_historical_cache_visible_generation_and_gap_restoration(self):
        minute = 60_000
        config = MarketMovementConfig(
            historical_lookback_ms=10 * minute,
            minimum_historical_coverage_ms=minute)
        def candle(offset, close):
            return CompletedMovementCandle(
                OUTPUT - offset * minute, Decimal(str(close)),
                Decimal("2"), Decimal("200"))
        visible = [candle(5, 100), candle(4, 101),
                   candle(2, 103), candle(1, 104)]
        boundary = OUTPUT + 5_000
        cache = replay_module._ReplayHistoricalInputCache()
        with patch.object(replay_module, "build_historical_window_inputs",
                          wraps=build_historical_window_inputs) as builder:
            gapped = cache.get(SYMBOL, visible, boundary, config, 4)
            self.assertIs(gapped, cache.get(SYMBOL, visible, boundary + 5_000,
                                            config, 4))
            visible.append(candle(3, 102))  # older minute arrives after newer ones
            restored = cache.get(SYMBOL, visible, boundary + 10_000, config, 5)
            self.assertEqual(builder.call_count, 2)
            self.assertEqual(restored, build_historical_window_inputs(
                visible, boundary + 10_000, config))
            self.assertNotEqual(gapped, restored)
            next_minute = OUTPUT + minute + 5_000
            before_new = cache.get(SYMBOL, visible, next_minute, config, 5)
            visible.append(CompletedMovementCandle(
                OUTPUT, Decimal("106"), Decimal("2"), Decimal("200")))
            extended = cache.get(SYMBOL, visible, next_minute, config, 6)
            self.assertEqual(builder.call_count, 4)
            self.assertEqual(extended, build_historical_window_inputs(
                visible, next_minute, config))
            self.assertNotEqual(before_new, extended)

    def test_cached_replay_matches_direct_builder_with_late_candles(self):
        minute = 60_000
        end = OUTPUT + minute + 10_000
        config = MarketMovementConfig(
            historical_lookback_ms=10 * minute,
            minimum_historical_coverage_ms=minute)
        warm_trades = tuple(_trade(
            boundary, trade_id=index + 1, price=str(100 + index % 4))
            for index, boundary in enumerate(range(ENGINE_START, end + 1, 15_000)))
        candles = tuple(_candle(OUTPUT - offset * minute, str(110 - offset))
                        for offset in (5, 4, 2, 1)) + (
            _candle(OUTPUT - 3 * minute, "107", first_seen=OUTPUT + 7_000),
            _candle(OUTPUT, "112", first_seen=OUTPUT + minute),
        )
        request = _request(trades=warm_trades, candles=candles,
                           movement_config=config, end=end)
        progress = []
        with patch.object(replay_module, "build_historical_window_inputs",
                          wraps=build_historical_window_inputs) as builder:
            optimized = run_historical_market_replay(
                request, progress_callback=lambda completed, total:
                progress.append((completed, total)))
            optimized_builder_calls = builder.call_count

        class DirectBuilder:
            def get(self, _symbol, visible, boundary, movement_config, _generation):
                return build_historical_window_inputs(
                    visible, boundary, movement_config)

        with patch.object(replay_module, "_ReplayHistoricalInputCache", DirectBuilder):
            direct = run_historical_market_replay(request)
        self.assertEqual(optimized, direct)
        self.assertEqual(tuple(point.movement_evaluation for point in optimized.points),
                         tuple(point.movement_evaluation for point in direct.points))
        self.assertLess(optimized_builder_calls, len(optimized.points))
        self.assertEqual(progress[-1], (len(optimized.points), len(optimized.points)))
        self.assertLessEqual(len(progress), 20)

        changed_history = replace(request, candles=candles[:-2] + candles[-1:])
        changed_config = replace(request, config=replace(
            request.config, movement_config=replace(
                config, historical_lookback_ms=9 * minute)))
        for changed in (changed_history, changed_config):
            optimized_changed = run_historical_market_replay(changed)
            with patch.object(replay_module, "_ReplayHistoricalInputCache", DirectBuilder):
                direct_changed = run_historical_market_replay(changed)
            self.assertEqual(optimized_changed, direct_changed)

    def test_exact_grace_right_closed_future_and_late_trade(self):
        early = _trade(OUTPUT, first_seen=OUTPUT + 1_999, trade_id=101,
                       price="101")
        late = _trade(OUTPUT, first_seen=OUTPUT + 2_001, trade_id=102,
                      price="999")
        future = _trade(OUTPUT + 1, first_seen=OUTPUT + 1, trade_id=103,
                        price="102")
        result = run_historical_market_replay(_request(trades=(
            _trade(ENGINE_START), early, late, future)))
        point = result.points[0]
        bucket = point.endpoint_buckets[0][1]
        self.assertEqual(point.replay_clock_time_ms, OUTPUT + 2_000)
        self.assertEqual(point.evaluation_boundary_time_ms, OUTPUT)
        self.assertEqual(bucket.trade_count, 1)
        self.assertEqual(bucket.price, Decimal("101"))
        self.assertEqual(bucket.last_real_trade_time_ms, OUTPUT)
        self.assertEqual(bucket.last_received_at_ms, OUTPUT + 1_999)
        self.assertEqual(result.points[1].endpoint_buckets[0][1].trade_count, 1)
        self.assertEqual(result.diagnostics.late_trade_count, 1)
        self.assertEqual(point.endpoint_buckets[0][1].price, Decimal("101"))
        with self.assertRaises(ValueError):
            _trade(OUTPUT + 1, first_seen=OUTPUT)

    def test_live_quiet_carry_expiry_and_explicit_outage(self):
        trades = (_trade(OUTPUT, trade_id=1),)
        end = OUTPUT + MAX_LAST_TRADE_AGE_MS + BUCKET_INTERVAL_MS
        carried = run_historical_market_replay(_request(trades=trades, end=end))
        self.assertTrue(carried.points[1].endpoint_buckets[0][1].carried_forward)
        self.assertEqual(carried.points[1].endpoint_buckets[0][1].trade_count, 0)
        self.assertEqual(carried.points[3].endpoint_buckets[0][1].price, Decimal("100"))
        self.assertIsNone(carried.points[4].endpoint_buckets[0][1].price)
        self.assertFalse(carried.points[4].endpoint_buckets[0][1].carried_forward)
        intervals = (
            HistoricalReplaySourceInterval(SYMBOL, ENGINE_START, OUTPUT - 5_000, "LIVE"),
            HistoricalReplaySourceInterval(SYMBOL, OUTPUT, OUTPUT, "UNAVAILABLE"),
            HistoricalReplaySourceInterval(SYMBOL, OUTPUT + 5_000, end, "LIVE"),
        )
        outage = run_historical_market_replay(_request(trades=trades,
                                                        intervals=intervals, end=end))
        self.assertEqual(outage.points[0].source_states, ((SYMBOL, "UNAVAILABLE"),))
        self.assertIsNone(outage.points[0].endpoint_buckets[0][1].price)
        self.assertIn("SOURCE_UNAVAILABLE",
                      outage.points[0].movement_evaluation.windows[1].symbols[0].exclusion_reasons)
        self.assertEqual(outage.points[1].source_states, ((SYMBOL, "LIVE"),))
        self.assertEqual(outage.points[1].endpoint_buckets[0][1].trade_count, 0)
        self.assertIsNone(outage.points[1].endpoint_buckets[0][1].price)

    def test_explicit_stale_source_excludes_as_source_stale(self):
        intervals = (
            HistoricalReplaySourceInterval(SYMBOL, ENGINE_START, OUTPUT - 5_000, "LIVE"),
            HistoricalReplaySourceInterval(SYMBOL, OUTPUT, OUTPUT, "STALE"),
            HistoricalReplaySourceInterval(SYMBOL, OUTPUT + 5_000,
                                           OUTPUT + 15_000, "LIVE"),
        )
        stale = run_historical_market_replay(_request(intervals=intervals))
        self.assertEqual(stale.points[0].source_states, ((SYMBOL, "STALE"),))
        self.assertIn("SOURCE_STALE",
                      stale.points[0].movement_evaluation.windows[15].symbols[0].exclusion_reasons)

    def test_candle_completion_strict_prior_and_first_seen(self):
        config = MarketMovementConfig(
            version="replay-fixture-config-v1", historical_lookback_ms=10 * 60_000,
            minimum_historical_coverage_ms=60_000, rvol_comparison_windows=1)
        earlier = tuple(_candle(OUTPUT - offset * 60_000, price)
                        for offset, price in ((5, "100"), (4, "101"),
                                              (3, "102"), (2, "200")))
        current = _candle(OUTPUT - 60_000, "300")
        later = _candle(OUTPUT, "400")
        original = _request(candles=earlier, movement_config=config)
        baseline = run_historical_market_replay(original)
        with_future = run_historical_market_replay(replace(
            original, candles=earlier + (current, later)))
        self.assertEqual(baseline.points[0].movement_evaluation,
                         with_future.points[0].movement_evaluation)
        delayed = replace(earlier[-1], first_seen_at_ms=OUTPUT + 2_001)
        unavailable = run_historical_market_replay(replace(
            original, candles=earlier[:-1] + (delayed,)))
        self.assertNotEqual(
            baseline.points[0].movement_evaluation.windows[1].symbols[0].historical_median,
            unavailable.points[0].movement_evaluation.windows[1].symbols[0].historical_median)
        with self.assertRaises(ValueError):
            _candle(OUTPUT - 60_000, "100", first_seen=OUTPUT - 1)

    def test_duplicates_input_order_and_stable_identity(self):
        first = _trade(ENGINE_START, trade_id=1)
        second = _trade(OUTPUT, trade_id=2, price="101")
        candles = (_candle(OUTPUT - 3 * 60_000, "100"),
                   _candle(OUTPUT - 2 * 60_000, "101"))
        intervals = (
            HistoricalReplaySourceInterval(SYMBOL, ENGINE_START, OUTPUT - 5_000, "LIVE"),
            HistoricalReplaySourceInterval(SYMBOL, OUTPUT, OUTPUT + 15_000, "LIVE"),
        )
        request = _request(trades=(first, second, first), candles=candles,
                           intervals=intervals)
        a = run_historical_market_replay(request)
        b = run_historical_market_replay(replace(
            request, trades=(first, first, second), candles=tuple(reversed(candles)),
            source_intervals=tuple(reversed(intervals))))
        c = run_historical_market_replay(request)
        self.assertEqual(a, b)
        self.assertEqual(a, c)
        self.assertEqual(a.diagnostics.deduplicated_trade_count, 1)
        self.assertEqual(len({point.point_id for point in a.points}), len(a.points))
        self.assertEqual(a.diagnostics.market_wide_ineligible_point_counts,
                         ((1, 4), (5, 4), (15, 4)))
        self.assertEqual(a.manifest.algorithm_version, HISTORICAL_REPLAY_ALGORITHM_VERSION)
        self.assertEqual(a.manifest.policy_version, HISTORICAL_REPLAY_POLICY_VERSION)
        with self.assertRaises(ValueError):
            run_historical_market_replay(replace(request, trades=(first, replace(first, price=Decimal("99")))))

    def test_warmup_insufficient_data_and_direct_canonical_parity(self):
        warm_trades = tuple(_trade(boundary, trade_id=index + 1,
                                   price=str(100 + index % 3))
                            for index, boundary in enumerate(
                                range(ENGINE_START, OUTPUT + 1, MAX_LAST_TRADE_AGE_MS)))
        warmed = run_historical_market_replay(_request(trades=warm_trades))
        reasons = warmed.points[0].movement_evaluation.windows[15].symbols[0].exclusion_reasons
        self.assertNotIn("WARMING_INSUFFICIENT_LIVE_HISTORY", reasons)
        self.assertIn("INSUFFICIENT_NORMALIZATION_HISTORY", reasons)
        sparse = run_historical_market_replay(_request())
        self.assertIn("STALE_LAST_TRADE",
                      sparse.points[0].movement_evaluation.windows[15].symbols[0].exclusion_reasons)
        self.assertIn("INSUFFICIENT_NORMALIZATION_HISTORY",
                      sparse.points[0].movement_evaluation.windows[15].symbols[0].exclusion_reasons)
        direct_trade = _trade(OUTPUT, trade_id=999, price="105")
        replay = run_historical_market_replay(_request(trades=(
            _trade(ENGINE_START, trade_id=1), direct_trade)))
        engine = MovementBucketEngine(f"binance-usdm:{SYMBOL}")
        initial = MarketObservation("binance-usdm", f"binance-usdm:{SYMBOL}", "trade",
                                    Decimal("100"), Decimal("1"), ENGINE_START,
                                    ENGINE_START, 1, ENGINE_START)
        latest = MarketObservation("binance-usdm", f"binance-usdm:{SYMBOL}", "trade",
                                   Decimal("105"), Decimal("1"), OUTPUT,
                                   OUTPUT, 999, OUTPUT)
        engine.observe((initial,))
        for boundary in range(ENGINE_START, OUTPUT + 1, BUCKET_INTERVAL_MS):
            if boundary == OUTPUT:
                engine.observe((latest,))
            engine.advance(boundary, "LIVE")
        readiness = {window: engine.readiness(OUTPUT, window, "LIVE")
                     for window in WINDOWS}
        historical = build_historical_window_inputs((), OUTPUT, MarketMovementConfig())
        direct = calculate_market_movement(MarketMovementInput(
            OUTPUT, MarketUniverseInput("u1", "v1", (SYMBOL,)),
            {SYMBOL: MarketMovementSymbolInput(
                SYMBOL, f"binance-usdm:{SYMBOL}", True, readiness, historical)},
            MarketMovementConfig(),
        ))
        self.assertEqual(replay.points[0].movement_evaluation, direct)

    def test_source_evidence_order_and_partition_independence(self):
        symbols = ("ETHUSDT", SYMBOL)
        request = _request(symbols=symbols)
        result = run_historical_market_replay(request)
        point = result.points[0]
        self.assertEqual(tuple(item.symbol for item in point.source_time_evidence), symbols)
        for (_, bucket), evidence in zip(point.endpoint_buckets, point.source_time_evidence):
            self.assertEqual(evidence.last_real_trade_time_ms, bucket.last_real_trade_time_ms)
            self.assertEqual(evidence.last_real_event_time_ms, bucket.last_real_event_time_ms)
            self.assertEqual(evidence.last_received_at_ms, bucket.last_received_at_ms)
        first = ReplayPartitionPlan(OUTPUT, OUTPUT + 5_000)
        second = ReplayPartitionPlan(OUTPUT + 5_000, OUTPUT + 10_000)
        a = to_market_state_experiment_points(result, first)
        b = to_market_state_experiment_points(result, second)
        validate_experiment_points(a)
        validate_experiment_points(b)
        self.assertEqual(tuple(item.partition for item in a),
                         ("development", "validation", "test", "test"))
        self.assertEqual(tuple(item.partition for item in b),
                         ("development", "development", "validation", "test"))
        self.assertEqual(tuple(item.movement_evaluation for item in a),
                         tuple(item.movement_evaluation for item in b))
        self.assertEqual(tuple(item.source_time_evidence for item in a),
                         tuple(item.source_time_evidence for item in b))
        self.assertEqual(tuple(item.movement_evaluation for item in a),
                         tuple(point.movement_evaluation for point in result.points))

    def test_checkpoint_rebuild_and_identity_mismatch(self):
        request = _request(trades=(_trade(ENGINE_START, trade_id=1),
                                   _trade(OUTPUT, trade_id=2)))
        full = run_historical_market_replay(request)
        interior = HistoricalReplayCheckpoint(
            full.manifest.run_fingerprint,
            full.points[1].evaluation_boundary_time_ms,
            full.points[1].point_id)
        resumed = run_historical_market_replay(request, interior)
        self.assertEqual(resumed.points, full.points[2:])
        self.assertEqual(resumed.final_checkpoint, full.final_checkpoint)
        changed = (
            replace(request, dataset=replace(DATASET, content_sha256="b" * 64)),
            replace(request, universe=MarketUniverseInput("u2", "v1", (SYMBOL,))),
            replace(request, config=replace(request.config, finalization_grace_ms=2_001)),
            replace(request, config=replace(request.config,
                                            movement_config=replace(request.config.movement_config,
                                                                    historical_lookback_ms=8 * 24 * 60 * 60 * 1000))),
        )
        for incompatible in changed:
            with self.subTest(incompatible=incompatible), self.assertRaises(ValueError):
                run_historical_market_replay(incompatible, interior)
        with self.assertRaises(ValueError):
            run_historical_market_replay(request, replace(interior, last_point_id="0" * 64))

    def test_structural_input_validation_and_grace_identity(self):
        request = _request()
        changed_grace = run_historical_market_replay(replace(
            request, config=replace(request.config, finalization_grace_ms=2_001)))
        original = run_historical_market_replay(request)
        self.assertNotEqual(original.manifest.run_fingerprint,
                            changed_grace.manifest.run_fingerprint)
        self.assertNotEqual(original.points[0].point_id, changed_grace.points[0].point_id)
        with self.assertRaises(ValueError):
            HistoricalReplayConfig(OUTPUT, OUTPUT, 0)
        with self.assertRaises(ValueError):
            HistoricalReplayConfig(WARMUP_MS - BUCKET_INTERVAL_MS, OUTPUT)
        with self.assertRaises(ValueError):
            _request(trades=())
        with self.assertRaises(ValueError):
            run_historical_market_replay(replace(request, source_intervals=()))
        with self.assertRaises(ValueError):
            run_historical_market_replay(replace(request, source_intervals=(
                HistoricalReplaySourceInterval(SYMBOL, ENGINE_START,
                                               OUTPUT - 5_000, "LIVE"),)))
        with self.assertRaises(ValueError):
            run_historical_market_replay(replace(request, source_intervals=(
                HistoricalReplaySourceInterval(SYMBOL, ENGINE_START, OUTPUT, "LIVE"),
                HistoricalReplaySourceInterval(SYMBOL, OUTPUT + 10_000,
                                               OUTPUT + 15_000, "LIVE"))))
        incompatible = run_historical_market_replay(_request(compatible=False))
        self.assertIn("UNSUPPORTED_INSTRUMENT",
                      incompatible.points[0].movement_evaluation.windows[1].symbols[0].exclusion_reasons)


if __name__ == "__main__":
    unittest.main()
