"""Runtime-only projection, scientific parity, restart and retention regressions.

The scaled regression measures live rich replay objects, not a fixed RSS number.
Real 24-hour acceptance remains a separate manual gate.
"""
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

from market_analysis import historical_replay as replay_module
from market_analysis import historical_replay_runtime as checkpoints
from market_analysis import historical_study_runtime as runtime
from market_analysis import historical_market_state_study_execution as execution
from market_analysis.binance_historical_archive import (
    BinanceUSDMArchiveRequest, daily_aggtrades_relative_path, daily_kline_relative_path,
    load_binance_usdm_bounded_historical_replay_dataset, required_aggtrade_dates,
    required_kline_dates,
)
from market_analysis.experiments.market_state_atr_normalization import (
    ATR_CONFIGURATIONS, run_market_state_atr_normalization_experiment,
    run_market_state_atr_normalization_suite,
)
from market_analysis.experiments.market_state_common import advance_canonical_branch
from market_analysis.experiments.market_state_hmm_regimes import (
    _scope, train_hmm_regime_model_from_feature_blocks,
)
from market_analysis.historical_experiment_batch import (
    EXPERIMENT_SUITE_V1, experiment_point_stream_sha256, experiment_stream_sha256,
)
from market_analysis.historical_market_state_candidate_evidence import adapt_candidate_result
from market_analysis.historical_market_state_forward_outcomes import TradePriceForwardEvidence
from market_analysis.historical_ohlc_evidence import BinanceTradeOHLCEvidence, CompletedTradeOHLCCandle
from market_analysis.historical_replay import (
    HistoricalReplayConfig, ReplayPartitionPlan,
    run_bounded_historical_market_replay, run_historical_market_replay,
)
from market_analysis.market_episode_lifecycle import MarketEpisodeLifecycleConfig
from market_analysis.movement_classifier import MarketClassifierConfig
from market_analysis.movement_metrics import MarketUniverseInput
from test_historical_replay import _request, OUTPUT
from test_historical_experiment_batch import _write_archive
from test_market_state_hmm_regimes_experiment import _point, _synthetic_blocks


PRODUCER = "c277011a3c1d3e0db2c5224e49386a375a6afc59"


def bounded_view(request):
    trades, _, _, _ = replay_module._canonical_inputs(request)
    class Index:
        def iter_replay_trades(self, symbols, after=None):
            return (item for item in trades if after is None or
                    (item.first_seen_at_ms, symbols.index(item.symbol),
                     item.trade_time_ms, item.aggregate_trade_id) > after)
    return SimpleNamespace(**{name: getattr(request, name) for name in (
        "dataset", "universe", "instruments", "candles", "source_intervals", "config")},
        trade_stream_manifest=SimpleNamespace(duplicate_row_count=0), _index=Index())


def store_for(root, request, manifest, phase="development"):
    return checkpoints.ReplayCheckpointStore(root, {
        "run_fingerprint": manifest.run_fingerprint, "phase": phase,
        "scientific_producer_revision": PRODUCER,
        "runtime_implementation_revision": runtime.current_runtime_implementation_revision(),
        "study_manifest_sha256": "a" * 64,
        "extension_coverage_manifest_sha256": "b" * 64,
        "archive_content_sha256": manifest.dataset_content_sha256,
        "study_period_index": 0, "utc_date": "2024-01-01",
        "configured_symbols": list(manifest.configured_universe),
        "finalization_grace_ms": request.config.finalization_grace_ms,
    }, request.config.output_start_boundary_time_ms, request.config.output_end_boundary_time_ms)


class RuntimeSourceRevisionTests(unittest.TestCase):
    def test_clean_package_returns_exact_full_revision(self):
        for revision in ("a" * 40, "b" * 64):
            with self.subTest(revision=revision), patch.object(
                    runtime.subprocess, "run", side_effect=[
                        SimpleNamespace(stdout=revision + "\n"),
                        SimpleNamespace(stdout="")]) as run:
                self.assertEqual(runtime.current_runtime_implementation_revision(), revision)
                root = str(Path(runtime.__file__).resolve().parents[2])
                self.assertEqual(run.call_args_list, [
                    unittest.mock.call(["git", "-C", root, "rev-parse", "HEAD"],
                                       check=True, capture_output=True, text=True),
                    unittest.mock.call(["git", "-C", root, "status", "--porcelain",
                                        "--untracked-files=all", "--", "python/market_analysis"],
                                       check=True, capture_output=True, text=True)])

    def test_tracked_staged_and_untracked_package_changes_fail_closed(self):
        for status in (" M python/market_analysis/module.py\n",
                       "M  python/market_analysis/module.py\n",
                       "?? python/market_analysis/new_module.py\n"):
            with self.subTest(status=status), patch.object(
                    runtime.subprocess, "run", side_effect=[
                        SimpleNamespace(stdout="a" * 40), SimpleNamespace(stdout=status)]):
                with self.assertRaisesRegex(ValueError, "source tree is not clean at HEAD"):
                    runtime.current_runtime_implementation_revision()

    def test_unrelated_dirty_files_do_not_block_package_identity(self):
        outside_status = " M docs/historical-replay.md\n?? python/tests/local.py\n?? outputs/result.json\n"
        def git_response(argv, **kwargs):
            if argv[3:] == ["rev-parse", "HEAD"]:
                return SimpleNamespace(stdout="a" * 40)
            # A whole-repository status would expose unrelated local changes.
            scoped = argv[3:] == ["status", "--porcelain", "--untracked-files=all",
                                  "--", "python/market_analysis"]
            return SimpleNamespace(stdout="" if scoped else outside_status)
        with patch.object(runtime.subprocess, "run", side_effect=git_response):
            self.assertEqual(runtime.current_runtime_implementation_revision(), "a" * 40)

    def test_git_head_and_status_failures_fail_closed(self):
        failures = ([OSError("git unavailable")],
                    [runtime.subprocess.CalledProcessError(1, "git rev-parse")],
                    [SimpleNamespace(stdout="a" * 40),
                     runtime.subprocess.CalledProcessError(1, "git status")],
                    [SimpleNamespace(stdout="invalid HEAD")])
        for responses in failures:
            with self.subTest(responses=responses), patch.object(
                    runtime.subprocess, "run", side_effect=responses):
                with self.assertRaises(ValueError):
                    runtime.current_runtime_implementation_revision()


class WorkerRuntimeRevisionTests(unittest.TestCase):
    def _job(self, root, runtime_revision, *, include_runtime_revision=True):
        request_raw = runtime._canonical_bytes(runtime.encode({"action": "fixture"}))
        request_path = root / "request.json"
        result_path = root / "result.json"
        job_path = root / "job.json"
        request_path.write_bytes(request_raw)
        identity = {
            "scientific_producer_revision": PRODUCER,
            "request_sha256": runtime._sha(request_raw),
        }
        if include_runtime_revision:
            identity["runtime_implementation_revision"] = runtime_revision
        job_path.write_bytes(runtime._canonical_bytes({
            "identity": identity,
            "request_path": str(request_path),
            "result_path": str(result_path),
        }))
        return job_path, result_path, identity

    def test_worker_runs_on_matching_runtime_revision_independent_of_producer(self):
        runtime_revision = "a" * 40
        self.assertNotEqual(runtime_revision, PRODUCER)
        with TemporaryDirectory() as folder:
            job_path, result_path, identity = self._job(Path(folder), runtime_revision)
            with patch.object(runtime, "current_runtime_implementation_revision",
                              return_value=runtime_revision), \
                 patch.object(runtime, "_execute_scientific_stage",
                              return_value={"fixture": "executed"}) as execute:
                runtime._worker(job_path)
            execute.assert_called_once_with({"action": "fixture"})
            result = checkpoints._read_json_bytes(result_path.read_bytes())
            self.assertEqual(result["identity"], identity)
            self.assertEqual(runtime.decode(result["result"]), {"fixture": "executed"})

    def test_worker_dirty_source_fails_before_request_execution_or_publication(self):
        with TemporaryDirectory() as folder:
            job_path, result_path, _ = self._job(Path(folder), "a" * 40)
            # A missing request also proves rejection precedes reading it.
            (Path(folder) / "request.json").unlink()
            with patch.object(runtime.subprocess, "run", side_effect=[
                    SimpleNamespace(stdout="a" * 40),
                    SimpleNamespace(stdout=" M python/market_analysis/module.py\n")]), \
                 patch.object(runtime, "_execute_scientific_stage") as execute:
                with self.assertRaisesRegex(ValueError, "worker runtime implementation revision") as caught:
                    runtime._worker(job_path)
            self.assertIn("source tree is not clean at HEAD", str(caught.exception.__cause__))
            execute.assert_not_called()
            self.assertFalse(result_path.exists())

    def test_worker_revision_mismatch_fails_before_execution_or_result_publication(self):
        with TemporaryDirectory() as folder:
            job_path, result_path, _ = self._job(Path(folder), "a" * 40)
            with patch.object(runtime, "current_runtime_implementation_revision",
                              return_value="b" * 40), \
                 patch.object(runtime, "_execute_scientific_stage",
                              side_effect=AssertionError("scientific stage must not run")) as execute:
                with self.assertRaisesRegex(ValueError, "runtime implementation revision mismatch"):
                    runtime._worker(job_path)
            execute.assert_not_called()
            self.assertFalse(result_path.exists())

    def test_worker_rejects_missing_malformed_or_unavailable_runtime_revision(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            cases = ((None, False), ("not-a-git-revision", True))
            for value, include_revision in cases:
                with self.subTest(runtime_revision=value):
                    job_path, result_path, _ = self._job(
                        root, value, include_runtime_revision=include_revision)
                    with patch.object(runtime, "current_runtime_implementation_revision",
                                      side_effect=AssertionError("invalid identity must fail first")), \
                         patch.object(runtime, "_execute_scientific_stage") as execute:
                        with self.assertRaisesRegex(ValueError, "valid runtime implementation revision"):
                            runtime._worker(job_path)
                    execute.assert_not_called()
                    self.assertFalse(result_path.exists())
            job_path, result_path, _ = self._job(root, "c" * 40)
            with patch.object(runtime, "current_runtime_implementation_revision",
                              side_effect=OSError("Git is unavailable")), \
                 patch.object(runtime, "_execute_scientific_stage") as execute:
                with self.assertRaisesRegex(ValueError, "could not determine worker runtime"):
                    runtime._worker(job_path)
            execute.assert_not_called()
            self.assertFalse(result_path.exists())


class ReplayProjectionTests(unittest.TestCase):
    def test_default_materialized_and_callback_only_points_are_exact(self):
        request = _request(end=OUTPUT + 60_000)
        expected = run_historical_market_replay(request)
        view = bounded_view(request)
        self.assertEqual(run_bounded_historical_market_replay(view), expected)
        points = []
        actual = run_bounded_historical_market_replay(view, retain_points=False,
                    boundary_callback=lambda point, state: points.append(point))
        self.assertEqual(tuple(points), expected.points)
        self.assertEqual(actual.points, ())
        self.assertEqual(actual.manifest, expected.manifest)
        self.assertEqual(actual.diagnostics, expected.diagnostics)
        self.assertEqual(actual.final_checkpoint, expected.final_checkpoint)

    def test_chain_spool_alignment_and_corruption_runtime_rejection(self):
        request = _request(end=OUTPUT + 60_000)
        expected = run_historical_market_replay(request)
        with TemporaryDirectory() as folder:
            store = store_for(Path(folder), request, expected.manifest)
            actual = run_bounded_historical_market_replay(bounded_view(request),
                         retain_points=False, boundary_callback=store.add_point)
            self.assertFalse(hasattr(store, "points"))
            self.assertEqual(store.chunk_points, [])
            self.assertEqual(store.point_count, len(expected.points))
            state = store.load_latest()
            self.assertEqual(state.emitted_point_count, len(expected.points))
            self.assertEqual(tuple(store.iter_points()), expected.points)
            resumed = run_bounded_historical_market_replay(bounded_view(request),
                        retain_points=False, runtime_state=state)
            self.assertEqual(resumed.final_checkpoint, actual.final_checkpoint)
            self.assertEqual(resumed.diagnostics, actual.diagnostics)
            stream = runtime.create_study_point_stream(store)
            for rich, compact in zip(expected.points, stream):
                self.assertEqual(compact.point_id, rich.point_id)
                self.assertEqual(compact.evaluation_boundary_time_ms, rich.evaluation_boundary_time_ms)
                self.assertEqual(compact.movement_evaluation, rich.movement_evaluation)
                self.assertEqual(compact.source_time_evidence, rich.source_time_evidence)
                self.assertEqual(compact.phase, "development")
                self.assertFalse(hasattr(compact, "endpoint_buckets"))
                self.assertFalse(hasattr(compact, "source_states"))
            self.assertEqual(stream.identity["final_replay_checkpoint_sha256"], store.previous_sha)
            self.assertEqual(tuple(runtime.create_study_point_stream(store)), tuple(stream))
            with self.assertRaises(ValueError):
                runtime.StudyPointStream(stream.root, {**stream.identity,
                    "runtime_implementation_revision": "d5023c6cb6dbec7b7a0451562631fd1d8e34834d"})
            old_store = store_for(Path(folder), request, expected.manifest)
            old_store.identity["runtime_implementation_revision"] = "old-runtime"
            with self.assertRaises(ValueError):
                old_store.load_latest()
            point_path = stream.root / "points.jsonl"
            original = point_path.read_bytes()
            point_path.write_bytes(original[:-1])
            with self.assertRaises(ValueError):
                runtime.StudyPointStream(stream.root, stream.identity)
            point_path.write_bytes(original)
            chunk = next((Path(folder) / "chunks").glob("*.jsonl"))
            chunk.write_bytes(chunk.read_bytes() + b"\n")
            with self.assertRaises(ValueError):
                store.load_latest()
            with self.assertRaises(ValueError):
                tuple(store.iter_points())

    def test_callback_only_replay_releases_emitted_points(self):
        references = []
        result = run_bounded_historical_market_replay(
            bounded_view(_request(end=OUTPUT + 10_000)), retain_points=False,
            boundary_callback=lambda point, state: references.append(weakref.ref(point)))
        self.assertEqual(result.points, ())
        self.assertEqual(len(references), 3)
        self.assertTrue(all(ref() is None for ref in references))

    def test_synthetic_hourly_chunks_release_and_stream_validated_points_once(self):
        request = _request(end=OUTPUT + 5_000)
        captured = []
        replay = run_bounded_historical_market_replay(bounded_view(request),
            boundary_callback=lambda point, state: captured.append((point, state))
            if state is not None else None)
        template, state_template = captured.pop()
        end = OUTPUT + 3_600_000 + 5_000
        with TemporaryDirectory() as folder:
            store = store_for(Path(folder), request, replay.manifest)
            store.end_boundary = end
            references = []
            for count, boundary in enumerate(range(OUTPUT, end + 1, 5_000), 1):
                point = replace(template,
                    point_id=replay_module.canonical_replay_point_id(replay.manifest.run_fingerprint, boundary),
                    evaluation_boundary_time_ms=boundary,
                    replay_clock_time_ms=boundary + request.config.finalization_grace_ms,
                    movement_evaluation=replace(template.movement_evaluation,
                        evaluation_boundary_time_ms=boundary),
                    endpoint_buckets=tuple((symbol, replace(bucket, boundary_time_ms=boundary))
                                           for symbol, bucket in template.endpoint_buckets))
                references.append(weakref.ref(point))
                state = replace(state_template, completed_boundary_time_ms=boundary,
                    replay_clock_time_ms=point.replay_clock_time_ms, emitted_point_count=count)
                store.add_point(point, state if boundary in store._checkpoint_boundaries() else None)
                del point, state
                if not store.chunk_points:
                    self.assertTrue(all(ref() is None for ref in references))
            self.assertEqual(len(list(store.root.glob("checkpoint-*.json"))), 2)
            decoded = []
            original_decode = checkpoints._decode
            def track(kind, value, substitutions=None):
                item = original_decode(kind, value, substitutions)
                if kind is replay_module.HistoricalMarketReplayPoint:
                    decoded.append(weakref.ref(item))
                return item
            with patch.object(checkpoints, "_decode", side_effect=track):
                latest = store.load_latest()
                self.assertEqual(len(decoded), count)
                self.assertTrue(all(ref() is None for ref in decoded))
                decoded.clear()
                for index, point in enumerate(store.iter_points()):
                    boundary = OUTPUT + index * 5_000
                    self.assertEqual(point.evaluation_boundary_time_ms, boundary)
                    self.assertEqual(point.point_id, replay_module.canonical_replay_point_id(
                        replay.manifest.run_fingerprint, boundary))
                    # At the second chunk, the completed first chunk is released.
                    if boundary == end:
                        self.assertTrue(all(ref() is None for ref in decoded[:-1]))
                    del point
                self.assertEqual(index + 1, count)
                self.assertEqual(len(decoded), count)
                self.assertTrue(all(ref() is None for ref in decoded))
                decoded.clear()
                runtime.create_study_point_stream(store)
                self.assertEqual(len(decoded), count)
                self.assertTrue(all(ref() is None for ref in decoded))
            self.assertEqual(latest.emitted_point_count, count)
            self.assertEqual(store.chunk_points, [])
            # An incomplete prefix must be rejected after generator consumption,
            # despite completion metadata left from an earlier validation.
            (store.root / f"checkpoint-{end}.json").unlink()
            with self.assertRaisesRegex(ValueError, "validated completed replay"):
                runtime.create_study_point_stream(store)


class ScientificStageParityTests(unittest.TestCase):
    def _fixed_parity(self, descriptors, ticks):
        points = tuple(_point(9_000_000 + tick * 5_000, "validation") for tick in range(ticks))
        period = SimpleNamespace(study_period_index=5, utc_date=date(2024, 1, 1),
            phase="validation", start_boundary_time_ms=9_000_000,
            end_boundary_time_ms=9_000_000 + (ticks - 1) * 5_000)
        classifier, lifecycle_config = MarketClassifierConfig(), MarketEpisodeLifecycleConfig()
        branch, state = {}, None
        for point in points:
            classification, lifecycle = advance_canonical_branch(point.movement_evaluation,
                point.source_time_evidence, state, classifier, lifecycle_config)
            state = lifecycle.next_state
            branch[point.movement_evaluation.evaluation_boundary_time_ms] = classification, lifecycle
        for descriptor in descriptors:
            with self.subTest(experiment=descriptor.experiment_id, config=descriptor.config_version):
                expected = descriptor.runner(points, descriptor.config,
                    canonical_branch_by_boundary=branch)
                actual = descriptor.runner(points, descriptor.config,
                    canonical_branch_by_boundary=None)
                self.assertEqual(execution._canonical(actual), execution._canonical(expected))
                bundle = adapt_candidate_result(period, descriptor, actual)
                self.assertEqual(bundle, adapt_candidate_result(period, descriptor, expected))
                self.assertEqual(descriptor.config.version, descriptor.config_version)

    def test_distinct_fixed_runner_families_full_usable_parity(self):
        representatives = {}
        for descriptor in EXPERIMENT_SUITE_V1:
            if descriptor.experiment_id != "EXP-75-09":
                representatives.setdefault(descriptor.runner, descriptor)
        self._fixed_parity(tuple(representatives.values()), 37)

    def test_every_registered_fixed_configuration_small_contract_parity(self):
        self._fixed_parity(tuple(descriptor for descriptor in EXPERIMENT_SUITE_V1
                                 if descriptor.experiment_id != "EXP-75-09"), 2)

    def _fixture(self, root):
        config = HistoricalReplayConfig(OUTPUT, OUTPUT + 60_000)
        symbols = execution.ORDERED_SYMBOLS
        universe = MarketUniverseInput("u1", "v1", symbols)
        day = date.fromtimestamp(OUTPUT // 1000)
        for symbol in symbols:
            for utc_day in required_aggtrade_dates(config):
                _write_archive(root, daily_aggtrades_relative_path(symbol, utc_day),
                    ((1, "100", "1", 1, 1, OUTPUT, "false"),) if utc_day == day else ())
            for utc_day in required_kline_dates(config):
                _write_archive(root, daily_kline_relative_path(symbol, utc_day), ())
        dataset = load_binance_usdm_bounded_historical_replay_dataset(
                    BinanceUSDMArchiveRequest(root, universe, config))
        replay = run_bounded_historical_market_replay(dataset)
        period = SimpleNamespace(study_period_index=5, utc_date=day, phase="validation",
            start_boundary_time_ms=OUTPUT, end_boundary_time_ms=OUTPUT + 60_000)
        points = execution._study_experiment_points(replay, period)
        v1, states, branch = execution._build_v1_evidence(period, replay.points)
        prepared = SimpleNamespace(period=period, archive_dataset=dataset,
            canonical_replay_result=replay, experiment_points=points,
            canonical_v1_branch_by_boundary=branch, study_manifest_sha256="a" * 64,
            core_eligibility_sha256="b" * 64, code_revision=PRODUCER)
        _, model = train_hmm_regime_model_from_feature_blocks(_synthetic_blocks(),
                    _scope(points[0].movement_evaluation))
        # Verified local loaders with missing packages exercise every extension's
        # explicit unavailable-evidence behavior; no provider/network is invoked.
        roots = {name: root / name for name in ("mark_trade", "open_interest", "funding", "liquidation")}
        supplementary = execution._load_source_evidence(period, roots)
        self.assertTrue(all(item["evidence"] is not None for item in supplementary.values()))
        store = store_for(root / "checkpoints", dataset, replay.manifest, "validation")
        run_bounded_historical_market_replay(dataset, retain_points=False, boundary_callback=store.add_point)
        stream = runtime.create_study_point_stream(store)
        compact = SimpleNamespace(**{**vars(prepared),
            "canonical_replay_result": replay_module.CompactStudyReplay(
                replay.manifest, stream, replay.diagnostics, replay.final_checkpoint),
            "experiment_points": None, "canonical_v1_branch_by_boundary": None})
        return prepared, compact, supplementary, model, v1, states

    def test_single_atr_configs_equal_multi_suite_native_evidence_and_sha(self):
        with TemporaryDirectory() as folder:
            prepared, compact, supplementary, model, v1, states = self._fixture(Path(folder))
            try:
                # Populate 121 completed candles so all three ATR windows work.
                from decimal import Decimal
                start = OUTPUT - 122 * 60_000
                candles = tuple(CompletedTradeOHLCCandle(symbol, f"binance-usdm:{symbol}",
                    opening, opening + 59_999, Decimal("100"), Decimal("102"),
                    Decimal("99"), Decimal("101"), opening + 60_000)
                    for symbol in execution.ORDERED_SYMBOLS
                    for opening in range(start, OUTPUT, 60_000))
                manifest = prepared.canonical_replay_result.manifest
                evidence = BinanceTradeOHLCEvidence(manifest.dataset_id, manifest.dataset_version,
                    manifest.dataset_content_sha256, manifest.configured_universe, candles)
                expected = run_market_state_atr_normalization_suite(prepared.experiment_points,
                    evidence, manifest, canonical_branch_by_boundary=prepared.canonical_v1_branch_by_boundary)
                for config, multi in zip(ATR_CONFIGURATIONS, expected):
                    single = run_market_state_atr_normalization_experiment(prepared.experiment_points,
                        evidence, manifest, config)
                    self.assertEqual(execution._canonical(single), execution._canonical(multi))
                    descriptor = SimpleNamespace(experiment_id="EXP-75-06B",
                        algorithm_version=execution.ATR_ALGORITHM_VERSION, config_version=config.version)
                    self.assertEqual(adapt_candidate_result(prepared.period, descriptor, single),
                                     adapt_candidate_result(prepared.period, descriptor, multi))
            finally:
                prepared.archive_dataset.close()

    def test_populated_extension_builders_share_exact_projected_inputs(self):
        from market_analysis.historical_mark_price_evidence import CompletedMarkPriceCandle
        from market_analysis.historical_open_interest_evidence import OpenInterestObservation
        from market_analysis.historical_funding_evidence import SettledFundingEvent
        from market_analysis.historical_liquidation_evidence import LiquidationSnapshot
        with TemporaryDirectory() as folder:
            prepared, compact, sources, model, v1, states = self._fixture(Path(folder))
            try:
                replay = compact.canonical_replay_result
                projected = tuple(replay.points)
                local = SimpleNamespace(**{**vars(compact),
                    "canonical_replay_result": replay_module.CompactStudyReplay(
                        replay.manifest, projected, replay.diagnostics, replay.final_checkpoint),
                    "experiment_points": tuple(point.experiment_point() for point in projected)})
                populated = {}
                sha = "e" * 64
                for name, source in sources.items():
                    evidence = source["evidence"]
                    if name == "mark_trade":
                        candles = tuple(CompletedMarkPriceCandle(symbol, opening, opening + 59_999,
                            Decimal("100"), Decimal("103"), Decimal("99"), Decimal("102"),
                            opening + 60_000) for symbol in execution.ORDERED_SYMBOLS
                            for opening in range(evidence.expected_open_time_start_ms,
                                evidence.expected_open_time_end_ms_exclusive, 60_000))
                        packages = tuple(replace(package, archive_present=True, checksum_present=True,
                            checksum_verified=True, archive_sha256=sha, status="VERIFIED",
                            statuses=("VERIFIED",)) for package in evidence.packages)
                        evidence = replace(evidence, packages=packages, candles=candles)
                    elif name in ("funding", "open_interest"):
                        packages = tuple(replace(package, archive_present=True, checksum_present=True,
                            checksum_verified=True, sha256=sha, status="VERIFIED")
                            for package in evidence.packages)
                        package_by_symbol = {package.symbol: package for package in packages}
                        rows = []
                        for symbol, package in package_by_symbol.items():
                            if name == "funding":
                                timestamp = (OUTPUT // 28_800_000) * 28_800_000
                                rows.append(SettledFundingEvent(symbol, timestamp, Decimal("0.0001"),
                                    8, timestamp + 1, package.relative_path, sha))
                            else:
                                latest = (OUTPUT // 300_000) * 300_000
                                for index in range(5):
                                    timestamp = latest - (4 - index) * 300_000
                                    rows.append(OpenInterestObservation(symbol, timestamp,
                                        Decimal(100 + index), Decimal(10_000 + index * 100),
                                        timestamp + 300_000, package.relative_path, sha))
                        evidence = replace(evidence, packages=packages, rows=tuple(rows))
                    else:
                        packages = tuple(replace(package, archive_present=True,
                            compressed_sha256=sha, status="ARCHIVE_DAY_AVAILABLE")
                            for package in evidence.packages)
                        package = next(package for package in packages if package.utc_day == prepared.period.utc_date)
                        rows = tuple(LiquidationSnapshot(symbol, (OUTPUT - 10_000) * 1000,
                            (OUTPUT - 5_000) * 1000, str(index), "buy" if index % 2 else "sell",
                            Decimal("100"), Decimal("2"), package.relative_path, sha, index)
                            for index, symbol in enumerate(execution.ORDERED_SYMBOLS))
                        evidence = replace(evidence, packages=packages, rows=rows)
                    populated[name] = {**source, "evidence": evidence,
                        "coverage": execution._source_coverage_summary(evidence, source_kind=name)}
                for selector in ("taker-flow", *populated):
                    with self.subTest(extension=selector):
                        expected = execution._candidate_execution(prepared, populated, model,
                                                                    stage_selector=selector)
                        actual = execution._candidate_execution(local, populated, model,
                                                                  stage_selector=selector)
                        self.assertEqual(execution._canonical(actual), execution._canonical(expected))
                        # Each extension's complete native point output also matches,
                        # not only the minute-grid evidence sampled by its adapter.
                        specs = {
                            "mark_trade": (execution.HistoricalMarkTradeExtensionPrepared,
                                "mark_evidence", "mark_archive_root", execution.build_historical_study_mark_trade_points),
                            "open_interest": (execution.HistoricalOpenInterestExtensionPrepared,
                                "oi_evidence", "oi_archive_root", execution.build_historical_study_open_interest_points),
                            "funding": (execution.HistoricalFundingExtensionPrepared,
                                "funding_evidence", "funding_archive_root", execution.build_historical_study_funding_points),
                            "liquidation": (execution.HistoricalLiquidationExtensionPrepared,
                                "liquidation_evidence", "liquidation_archive_root", execution.build_historical_study_liquidation_points),
                        }
                        if selector == "taker-flow":
                            expected_points = execution.build_historical_study_taker_flow_points(
                                prepared.archive_dataset, prepared.canonical_replay_result,
                                prepared.experiment_points, prepared.period.phase)
                            actual_points = execution.build_historical_study_taker_flow_points(
                                local.archive_dataset, local.canonical_replay_result,
                                local.experiment_points, local.period.phase)
                        else:
                            cls, evidence_field, root_field, builder = specs[selector]
                            def extension_input(item):
                                return cls(archive_dataset=item.archive_dataset,
                                    replay_result=item.canonical_replay_result,
                                    experiment_points=item.experiment_points, partition_plan=None,
                                    study_phase=item.period.phase, **{
                                        evidence_field: populated[selector]["evidence"],
                                        root_field: populated[selector]["root"]})
                            expected_points = builder(extension_input(prepared))
                            actual_points = builder(extension_input(local))
                        self.assertEqual(execution._canonical(actual_points), execution._canonical(expected_points))
            finally:
                prepared.archive_dataset.close()

    def test_staged_scientific_payload_resume_extension_and_event_context_parity(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            prepared, compact, supplementary, model, v1, states = self._fixture(root)
            try:
                stream = compact.canonical_replay_result.points
                actual_v1, actual_states, empty_branch = execution._build_v1_evidence(
                    compact.period, stream, retain_branch=False)
                self.assertEqual(actual_v1, v1)
                self.assertEqual(actual_states, states)
                self.assertEqual(empty_branch, {})
                expected = execution._candidate_execution(prepared, supplementary, model)
                selectors = ("hmm", *(f"fixed-{index:02d}" for index, descriptor
                    in enumerate(EXPERIMENT_SUITE_V1) if descriptor.experiment_id != "EXP-75-09"),
                    *(f"atr-{index}" for index in range(len(ATR_CONFIGURATIONS))),
                    "taker-flow", "mark_trade", "open_interest", "funding", "liquidation")
                cache, requests, calls, reused = {}, {}, [], []
                def transport(stream_arg, stage_id, request, **kwargs):
                    self.assertIs(stream_arg, stream)
                    calls.append(stage_id)
                    encoded = runtime._canonical_bytes(runtime.encode(request))
                    if stage_id in cache:
                        self.assertEqual(encoded, requests[stage_id])
                        reused.append(stage_id)
                    else:
                        # Mock only stage transport; execute real scientific functions.
                        cache[stage_id] = runtime._execute_scientific_stage(
                            runtime.decode(runtime.encode(request)))
                        requests[stage_id] = encoded
                    return cache[stage_id]
                def interrupted(*args, **kwargs):
                    if len(cache) == 2:
                        raise RuntimeError("synthetic interruption")
                    return transport(*args, **kwargs)
                with patch.object(execution, "run_stage", side_effect=interrupted):
                    with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
                        execution._staged_candidate_execution(compact, supplementary, model)
                self.assertEqual(calls, list(selectors[:2]))
                calls.clear()
                with patch.object(execution, "run_stage", side_effect=transport):
                    actual = execution._staged_candidate_execution(compact, supplementary, model)
                self.assertEqual(calls, list(selectors))
                self.assertEqual(reused, list(selectors[:2]))
                self.assertEqual(execution._canonical(actual), execution._canonical(expected))
                for selector in selectors:
                    request = runtime.decode(checkpoints._read_json_bytes(requests[selector]))
                    self.assertEqual(request["selector"], selector)
                    self.assertEqual(request["scientific_stage"], execution._stage_science_identity(selector))
                    self.assertEqual(set(request["supplementary"]),
                                     {selector} if selector in supplementary else set())
                cache["v1"] = (actual_v1, actual_states)
                dependencies = tuple({"stage_id": selector,
                    "stage_identity_sha256": runtime._sha(requests.get(selector, b"v1")),
                    "stage_result_sha256": runtime._sha(runtime._canonical_bytes(
                        runtime.encode(cache[selector])))} for selector in ("v1", *selectors))
                def bind_dependencies(stream_arg, stage_ids):
                    self.assertIs(stream_arg, stream)
                    self.assertEqual(tuple(stage_ids), ("v1", *selectors))
                    return dependencies
                records = (*actual[0], *v1)
                # Force a sparse exact event to cover context even on unavailable data.
                event = execution.HistoricalStudyCandidateEvidence(5, prepared.period.utc_date.isoformat(),
                    "validation", "EXP-75-02", "cusum-v1", "fixture", OUTPUT + 5_000,
                    "EVENT", "ONSET", {})
                self.assertEqual(execution._event_time_v1_context(prepared, (*records, event)),
                                 execution._event_time_v1_context(compact, (*records, event)))
                plan = ReplayPartitionPlan(OUTPUT, OUTPUT + 5_000)
                self.assertEqual(experiment_stream_sha256(prepared.canonical_replay_result,
                    prepared.experiment_points, plan), experiment_point_stream_sha256(
                    prepared.canonical_replay_result.manifest, prepared.experiment_points, plan))
                forward = TradePriceForwardEvidence(execution.ORDERED_SYMBOLS, (),
                    prepared.archive_dataset.archive_manifest.content_sha256,
                    OUTPUT - 60_000, OUTPUT + 61 * 60_000)
                coverage = {"coverage_manifest_sha256": "c" * 64,
                    "sources": {name: item["coverage"] for name, item in supplementary.items()}}
                manifest = SimpleNamespace(manifest_sha256="a" * 64)
                safe = execution.report_json_safe
                def fixture_safe(value):
                    return safe(vars(value)) if isinstance(value, SimpleNamespace) else safe(value)
                # The replay fixture is 60 seconds; outcome reports still use
                # the production contract of a complete UTC day.
                day_start = (OUTPUT // 86_400_000) * 86_400_000
                execution_period = SimpleNamespace(**vars(prepared.period))
                execution_period.start_boundary_time_ms = day_start
                execution_period.end_boundary_time_ms = day_start + 86_400_000
                # Reuse already-proven equivalent candidate outputs for report assembly.
                # Event context/outcome algorithms still run directly through transport.
                with patch.object(execution, "_prepare_study_period", side_effect=[
                        (prepared, coverage, v1, states), (compact, coverage, actual_v1, actual_states)]), \
                     patch.object(execution, "_load_source_evidence", return_value=supplementary), \
                     patch.object(execution, "_label_price_evidence", return_value=forward), \
                     patch.object(execution, "report_json_safe", side_effect=fixture_safe):
                    with patch.object(execution, "_candidate_execution", return_value=expected):
                        legacy = execution._execute_period(manifest, coverage, execution_period,
                                    root, {}, PRODUCER, model)
                    with patch.object(execution, "run_stage", side_effect=transport), \
                         patch.object(execution, "stage_dependencies", side_effect=bind_dependencies):
                        staged = execution._execute_period(manifest, coverage, execution_period,
                                    root, {}, PRODUCER, model)
                for stage_id in ("event-context", "outcomes"):
                    request = runtime.decode(checkpoints._read_json_bytes(requests[stage_id]))
                    self.assertEqual(request["required_stage_results"], dependencies)
                self.assertEqual(legacy, staged)
                self.assertEqual(legacy["report_sha256"], staged["report_sha256"])
                self.assertEqual(staged["hmm_model_sha256"], model.model_sha256)
                self.assertTrue(staged["pelt_no_causal_outcomes"])
                self.assertFalse(any(item["experiment_id"] == "EXP-75-04A"
                                     for item in staged["candidate_event_outcomes"]))
                calls.clear()
                reused.clear()
                with patch.object(execution, "run_stage", side_effect=transport):
                    execution._staged_candidate_execution(compact, supplementary, model)
                self.assertEqual(calls, list(selectors))
                self.assertEqual(reused, list(selectors))
            finally:
                prepared.archive_dataset.close()

    def test_one_fresh_fixed_worker_publication_reuse_and_corruption(self):
        with TemporaryDirectory() as folder:
            prepared, compact, supplementary, model, v1, states = self._fixture(Path(folder))
            try:
                stream = compact.canonical_replay_result.points
                request = {"action": "candidate", "prepared": execution._stage_prepared(compact, "fixed-00"),
                    "supplementary": {}, "hmm_model": model, "selector": "fixed-00",
                    "scientific_stage": execution._stage_science_identity("fixed-00")}
                expected = execution._candidate_execution(prepared, {}, model, stage_selector="fixed-00")
                progress = []
                original_run = runtime.subprocess.run
                with patch.object(runtime.subprocess, "run", wraps=original_run) as launch:
                    actual = runtime.run_stage(stream, "fixed-00", request,
                        progress=lambda event, detail: progress.append((event, detail)))
                self.assertEqual(launch.call_count, 1)
                self.assertEqual(launch.call_args.args[0][:3],
                    [runtime.sys.executable, "-m", "market_analysis.historical_study_runtime"])
                self.assertEqual(execution._canonical(actual), execution._canonical(expected))
                self.assertEqual([event for event, _ in progress],
                    ["POST_REPLAY_STAGE_STARTED", "POST_REPLAY_STAGE_COMPLETED"])
                self.assertIn("stage_result_sha256", progress[-1][1])
                dependency, = runtime.stage_dependencies(stream, ("fixed-00",))
                self.assertEqual(dependency["stage_id"], "fixed-00")
                self.assertEqual(dependency["stage_result_sha256"],
                                 progress[-1][1]["stage_result_sha256"])
                with patch.object(runtime.subprocess, "run", side_effect=AssertionError("unexpected child")):
                    self.assertEqual(runtime.run_stage(stream, "fixed-00", request), actual)
                path = stream.root.parent / "post-replay" / "fixed-00.json"
                raw = path.read_bytes()
                identity = checkpoints._read_json_bytes(raw)["identity"]
                for key in ("runtime_implementation_revision", "scientific_producer_revision",
                            "study_manifest_sha256", "extension_coverage_manifest_sha256",
                            "compact_stream_sha256", "run_fingerprint", "stage_id",
                            "supplementary_source_sha256", "request_sha256"):
                    with self.subTest(stale_stage_identity=key), self.assertRaises(ValueError):
                        runtime._load_stage(path, {**identity, key: "conflicting-identity"})
                path.write_bytes(raw[:-1])
                with self.assertRaises(ValueError):
                    runtime.run_stage(stream, "fixed-00", request)
                path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    runtime.run_stage(stream, "fixed-00", {**request,
                        "scientific_stage": {"config_version": "stale-config"}})
            finally:
                prepared.archive_dataset.close()

    def test_hmm_development_still_rejects_short_or_nonuniform_days(self):
        points = tuple(_point(tick * 5_000, "development") for tick in range(13))
        period = SimpleNamespace(study_period_index=0, utc_date=date(2024, 1, 1),
            phase="development", start_boundary_time_ms=0, end_boundary_time_ms=86_400_000)
        with self.assertRaises(ValueError):
            execution._hmm_study_evidence(period, points, None)
        period.phase = "validation"
        with self.assertRaises(ValueError):
            execution._hmm_study_evidence(period, points, None)


if __name__ == "__main__":
    unittest.main()
