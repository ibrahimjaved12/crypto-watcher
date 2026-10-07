"""Generated governance and identity fixtures for Part-B execution."""

from contextlib import ExitStack
import csv
from datetime import date, datetime, timezone
import hashlib
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import unittest
from unittest.mock import Mock, patch
import zipfile

from market_analysis.binance_historical_archive import (
    BinanceUSDMArchiveRequest, daily_aggtrades_relative_path,
    daily_kline_relative_path, historical_candle_start_ms,
    load_binance_usdm_bounded_historical_replay_dataset,
    load_binance_usdm_historical_replay_dataset, required_aggtrade_dates,
    required_kline_dates,
)

from market_analysis.historical_market_state_study import (
    parse_historical_market_state_study_manifest_json,
)
from market_analysis.experiments.market_state_hmm_regimes import HMMDevelopmentTrainingBlock
from market_analysis.historical_market_state_study_execution import (
    EXECUTION_VERSION, EXTENSION_COVERAGE_VERSION, SOURCE_IDENTITIES,
    TOOL_CONFIG_VERSION, _canonical,
    _source_coverage_summary, build_extension_coverage_manifest,
    load_and_validate_coverage, select_execution_periods,
    freeze_study_hmm_model, validate_hmm_development_cohort,
)
from market_analysis.historical_replay import (
    HistoricalReplayConfig, run_bounded_historical_market_replay,
    run_historical_market_replay,
)
from market_analysis.historical_liquidation_evidence import daily_liquidation_relative_path
from market_analysis.movement_metrics import MarketMovementConfig, MarketUniverseInput
import market_analysis.historical_market_state_study_execution as execution


MANIFEST_PATH = (Path(__file__).parents[2] / "research" /
                 "historical-market-state-study-v1" / "selection" /
                 "historical-market-state-study-v1-manifest.json")
SOURCE_NAMES = ("mark_trade", "open_interest", "funding", "liquidation")


class StudyExecutionGovernanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = parse_historical_market_state_study_manifest_json(
            MANIFEST_PATH.read_text(encoding="utf-8"))

    def _coverage_fixture(self, folder, revision):
        ordered = [{"study_period_index": item.study_period_index,
                    "utc_date": item.utc_date.isoformat(), "phase": item.phase}
                   for item in self.manifest.selected_periods]
        coverage = {
            "coverage_version": EXTENSION_COVERAGE_VERSION,
            "study_version": self.manifest.study_version,
            "study_manifest_sha256": self.manifest.manifest_sha256,
            "ordered_periods": ordered,
            "source_identities": execution.report_json_safe(SOURCE_IDENTITIES),
            "tool_config_version": TOOL_CONFIG_VERSION,
            "code_revision": revision,
            "periods": ordered,
        }
        coverage["coverage_manifest_sha256"] = execution._digest(coverage)
        path = Path(folder) / "coverage.json"
        path.write_text(_canonical(coverage), encoding="utf-8")
        return path, coverage

    def test_default_phase_and_first_three_frozen_development_dates(self):
        selected = select_execution_periods(self.manifest, period_limit=3)
        self.assertEqual(tuple(item.utc_date.isoformat() for item in selected),
                         ("2024-01-01", "2024-02-06", "2024-04-03"))
        self.assertTrue(all(item.phase == "development" for item in selected))
        self.assertEqual(len(select_execution_periods(
            self.manifest, "validation", period_limit=2)), 2)
        with self.assertRaises(ValueError):
            select_execution_periods(self.manifest, "test")
        with self.assertRaises(ValueError):
            select_execution_periods(self.manifest, allow_test=True)
        self.assertEqual(tuple(item.phase for item in select_execution_periods(
            self.manifest, "test", period_limit=1, allow_test=True)), ("test",))

    def test_hmm_freeze_guard_requires_all_development_blocks(self):
        with self.assertRaises(ValueError):
            validate_hmm_development_cohort((), self.manifest)
        with TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                freeze_study_hmm_model(
                    self.manifest, temporary, code_revision="fixture-revision")

    def test_hmm_freeze_holds_one_development_report_at_a_time(self):
        import weakref

        class Report(dict):
            """weakref-able stand-in for a decoded period report."""

        alive, reads = [], []

        def read_json(path):
            # Every previously read report must be unreferenced before the next read.
            self.assertTrue(all(ref() is None for ref in alive), "an earlier report is still referenced")
            report = Report(report_sha256=str(len(reads)) * 64, extension_coverage_manifest_sha256="c" * 64)
            alive.append(weakref.ref(report))
            reads.append(path)
            return report

        class StopAfterLoop(Exception):
            pass

        def cohort(blocks, manifest):
            self.assertTrue(all(ref() is None for ref in alive[:-1]))
            raise StopAfterLoop

        periods = tuple(item for item in self.manifest.selected_periods if item.phase == "development")
        with TemporaryDirectory() as temporary:
            period_dir = Path(temporary) / execution.PERIOD_DIRECTORY
            period_dir.mkdir()
            for period in periods:
                (period_dir / execution._period_filename(period)).write_text("{}")
            with patch.object(execution, "_read_json", read_json), \
                 patch.object(execution, "_validate_period_report", lambda *args: None), \
                 patch.object(execution, "_validated_period_hmm_block", lambda report, period: object()), \
                 patch.object(execution, "validate_hmm_development_cohort", cohort), \
                 self.assertRaises(StopAfterLoop):
                freeze_study_hmm_model(self.manifest, temporary, code_revision="fixture-revision")
        self.assertEqual(len(reads), len(periods))

    def test_period_preparation_loads_and_replays_once(self):
        period = self.manifest.selected_periods[0]
        content_sha = "a" * 64
        archive_manifest = SimpleNamespace(
            content_sha256=content_sha, archive_files=(),
            dataset_id="fixture-dataset", dataset_version="fixture-v1")
        dataset = SimpleNamespace(
            archive_manifest=archive_manifest, close=lambda: None)
        replay = SimpleNamespace(
            manifest=SimpleNamespace(dataset_content_sha256=content_sha,
                                     dataset_id="fixture-dataset",
                                     dataset_version="fixture-v1"),
            points=())
        eligibility = SimpleNamespace(
            provenance=SimpleNamespace(archive_manifest=archive_manifest),
            eligibility_sha256="b" * 64)
        coverage_period = {
            "study_period_index": period.study_period_index,
            "utc_date": period.utc_date.isoformat(),
            "core": {"archive_content_sha256": content_sha},
        }
        prepared_marker = object()
        ticks = iter(range(1000))
        runtime_metrics = execution.StudyPeriodRuntimeMetrics()
        with patch.object(execution.time, "perf_counter_ns",
                          side_effect=lambda: next(ticks)), patch.object(
                execution, "load_binance_usdm_bounded_historical_replay_dataset",
                          return_value=dataset) as loader, patch.object(
                execution, "run_bounded_historical_market_replay", return_value=replay) as runner, patch.object(
                execution, "_frozen_eligibility", return_value=eligibility), patch.object(
                execution, "_study_experiment_points", return_value=()), patch.object(
                execution, "_build_v1_evidence", return_value=((), {}, {})), patch.object(
                execution, "PreparedHistoricalMarketStatePeriod",
                return_value=prepared_marker) as prepared_type:
            prepared, frozen, _, _ = execution._prepare_study_period(
                self.manifest, {"periods": [coverage_period]}, period,
                Path("/local/core"), "fixture-rev", runtime_metrics=runtime_metrics)

        self.assertIs(prepared, prepared_marker)
        self.assertIs(frozen, coverage_period)
        self.assertEqual(loader.call_count, 1)
        self.assertEqual(runner.call_count, 1)
        self.assertIs(runner.call_args.args[0], dataset)
        prepared_type.assert_called_once()
        self.assertGreater(runtime_metrics.report_timings()["core_archive_load_seconds"], 0)
        self.assertGreater(runtime_metrics.report_timings()["canonical_replay_seconds"], 0)
        self.assertGreater(runtime_metrics.report_timings()["v1_preparation_seconds"], 0)

    def test_candidate_dispatch_reuses_shared_branch_and_atr_once(self):
        period = self.manifest.selected_periods[0]
        branch = {period.start_boundary_time_ms: ("classification", "lifecycle")}
        prepared = SimpleNamespace(
            period=period, archive_dataset=SimpleNamespace(ohlc_evidence=object()),
            canonical_replay_result=SimpleNamespace(
                manifest=SimpleNamespace(configured_universe=("BTCUSDT",))),
            experiment_points=(), canonical_v1_branch_by_boundary=branch)
        clock = {"now": 0}
        runner = Mock(return_value=SimpleNamespace())
        second_runner = Mock(return_value=SimpleNamespace())
        def run_bocpd(*args, **kwargs):
            clock["now"] += 10
            return SimpleNamespace()

        def timed_mock(return_value=()):
            def run_timed_work(*args, **kwargs):
                clock["now"] += 1
                return return_value

            return Mock(side_effect=run_timed_work)

        bocpd_runner = Mock(side_effect=run_bocpd)
        descriptors = (
            SimpleNamespace(experiment_id="EXP-75-09", algorithm_version="hmm-v1",
                            config_version="hmm-config"),
            SimpleNamespace(experiment_id="EXP-75-01", algorithm_version="ewma-v1",
                            config_version="ewma-config", config=object(), runner=runner),
            SimpleNamespace(experiment_id="EXP-75-02", algorithm_version="cusum-v1",
                            config_version="cusum-config", config=object(), runner=second_runner),
            SimpleNamespace(experiment_id="EXP-75-04B", algorithm_version="bocpd-v1",
                            config_version="bocpd-config", config=object(),
                            runner=bocpd_runner),
        )
        supplementary = {
            name: {"evidence": object(), "coverage": {"coverage_state": "FULL"},
                   "root": Path("/local/source")}
            for name in SOURCE_NAMES
        }
        bundle = SimpleNamespace(records=(), native_summaries={}, result_sha256="c" * 64)

        class TimedRecords:
            def __iter__(self):
                clock["now"] += 30
                return iter(())

            def __len__(self):
                return 0

        class TimedBundle:
            records = TimedRecords()

            @property
            def native_summaries(self):
                clock["now"] += 40
                return {}

            @property
            def result_sha256(self):
                clock["now"] += 50
                return "d" * 64

        timed_bundle = TimedBundle()

        def adapt_result(period, descriptor, result):
            if descriptor.experiment_id == "EXP-75-04B":
                clock["now"] += 20
                return timed_bundle
            return bundle
        extension_parts = (
            ("HistoricalMarkTradeExtensionPrepared",
             "build_historical_study_mark_trade_points", "_mark_summaries"),
            ("HistoricalOpenInterestExtensionPrepared",
             "build_historical_study_open_interest_points", "_oi_summaries"),
            ("HistoricalFundingExtensionPrepared",
             "build_historical_study_funding_points", "_funding_summaries"),
            ("HistoricalLiquidationExtensionPrepared",
             "build_historical_study_liquidation_points", "_liquidation_summaries"),
        )
        extension_preparers = []
        extension_builders = []
        runtime_metrics = execution.StudyPeriodRuntimeMetrics()
        with ExitStack() as stack:
            stack.enter_context(patch.object(
                execution.time, "perf_counter_ns", side_effect=lambda: clock["now"]))
            stack.enter_context(patch.object(execution, "EXPERIMENT_SUITE_V1", descriptors))
            stack.enter_context(patch.object(
                execution, "_hmm_study_evidence", return_value=((), {}, None, None)))
            atr = timed_mock()
            stack.enter_context(patch.object(
                execution, "run_market_state_atr_normalization_suite", atr))
            taker = timed_mock()
            stack.enter_context(patch.object(
                execution, "build_historical_study_taker_flow_points", taker))
            stack.enter_context(patch.object(
                execution, "_taker_summaries", return_value={"development": {}}))
            stack.enter_context(patch.object(
                execution, "adapt_candidate_result", side_effect=adapt_result))
            for prepared_name, builder_name, summary_name in extension_parts:
                preparer = Mock(return_value=object())
                builder = timed_mock()
                extension_preparers.append(preparer)
                extension_builders.append(builder)
                stack.enter_context(patch.object(execution, prepared_name, preparer))
                stack.enter_context(patch.object(execution, builder_name, builder))
                stack.enter_context(patch.object(
                    execution, summary_name, return_value={"development": {}}))
            execution._candidate_execution(
                prepared, supplementary, None, runtime_metrics=runtime_metrics)

        self.assertEqual(runner.call_count, 1)
        self.assertEqual(second_runner.call_count, 1)
        self.assertEqual(bocpd_runner.call_count, 1)
        self.assertTrue(all(item.call_count == 1 for item in extension_preparers))
        self.assertTrue(all(item.call_count == 1 for item in extension_builders))
        self.assertIs(runner.call_args.kwargs["canonical_branch_by_boundary"], branch)
        self.assertIs(second_runner.call_args.kwargs["canonical_branch_by_boundary"], branch)
        atr.assert_called_once()
        self.assertIs(atr.call_args.kwargs["canonical_branch_by_boundary"], branch)
        taker.assert_called_once()
        timings = runtime_metrics.report_timings()
        self.assertGreater(timings["core_experiment_seconds"], 0)
        self.assertEqual(runtime_metrics.elapsed_ns_by_field["bocpd_seconds"], 150)
        self.assertGreater(timings["atr_06b_seconds"], 0)
        self.assertGreater(timings["taker_flow_seconds"], 0)
        for field in ("mark_trade_seconds", "open_interest_seconds",
                      "funding_seconds", "liquidation_seconds"):
            self.assertGreater(timings[field], 0)

    def test_event_time_v1_context_is_exact_and_deduplicated(self):
        period = self.manifest.selected_periods[0]
        boundary = period.start_boundary_time_ms + 5_000
        movement = SimpleNamespace(
            evaluation_boundary_time_ms=boundary,
            algorithm_version="movement-v1", config_version="movement-config-v1",
            universe_id="universe-v1", universe_version="1",
            configured_universe=("BTCUSDT", "ETHUSDT"),
            provider="provider", exchange="exchange", price_type="trade")
        replay_point = SimpleNamespace(
            evaluation_boundary_time_ms=boundary,
            movement_evaluation=movement,
            source_time_evidence=({"symbol": "BTCUSDT", "observed_at_ms": boundary},))
        lifecycle = SimpleNamespace(
            next_state={"active_episode": None},
            transitions=({"transition": "STARTED",
                          "evaluation_boundary_time_ms": boundary},))
        prepared = SimpleNamespace(
            period=period,
            canonical_v1_branch_by_boundary={
                boundary: ({"windows": {"5": {"direction_state": "BROAD_RISE"}}},
                           lifecycle)},
            canonical_replay_result=SimpleNamespace(points=(replay_point,)))
        first = execution.HistoricalStudyCandidateEvidence(
            period.study_period_index, period.utc_date.isoformat(), period.phase,
            "EXP-75-02", "cusum-v1", "a", boundary, "EVENT", "ONSET", {})
        second = execution.HistoricalStudyCandidateEvidence(
            period.study_period_index, period.utc_date.isoformat(), period.phase,
            "EXP-75-04B", "bocpd-v1", "b", boundary, "EVENT", "ONSET",
            {"causal_onset_observation": {
                "evaluation_boundary_time_ms": boundary}})
        other_family = execution.HistoricalStudyCandidateEvidence(
            period.study_period_index, period.utc_date.isoformat(), period.phase,
            "EXP-75-09", "hmm-v1", "hmm", boundary, "EVENT", "ONSET", {})
        extension_event = execution.HistoricalStudyCandidateEvidence(
            period.study_period_index, period.utc_date.isoformat(), period.phase,
            "EXP-75-11-FUNDING", "funding-v1", "funding", boundary, "EVENT", "ONSET", {})
        continuous_v1 = execution.HistoricalStudyCandidateEvidence(
            period.study_period_index, period.utc_date.isoformat(), period.phase,
            "V1", "market-state-classifier-v1", "v1-config",
            boundary + 5_000, "CONTINUOUS", "READY", {})

        records = execution._event_time_v1_context(
            prepared, (first, second, other_family, extension_event, continuous_v1))

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["decision_time_ms"], boundary)
        self.assertEqual(records[0]["provenance"]["movement_config_version"],
                         "movement-config-v1")
        self.assertEqual(records[0]["transitions"][0]["transition"], "STARTED")
        with self.assertRaisesRegex(ValueError, "exact canonical V1 branch"):
            wrong = execution.HistoricalStudyCandidateEvidence(
                period.study_period_index, period.utc_date.isoformat(), period.phase,
                "EXP-75-02", "cusum-v1", "a", boundary + 5_000, "EVENT", "ONSET", {})
            execution._event_time_v1_context(prepared, (wrong,))

    def test_execute_period_times_source_and_outcome_paths_once(self):
        period = self.manifest.selected_periods[0]
        revision = "runtime-path-fixture"
        source_coverage = {name: {"coverage_state": "FULL"} for name in SOURCE_NAMES}
        supplementary = {
            name: {"coverage": source_coverage[name], "evidence": object(),
                   "root": Path("/generated/source")}
            for name in SOURCE_NAMES
        }
        taker_evidence = SimpleNamespace(
            schema_version="schema-v1", algorithm_version="flow-v1",
            dataset_content_sha256="a" * 64, configured_symbols=("BTCUSDT",),
            engine_start_boundary_time_ms=0, output_end_boundary_time_ms=60_000,
            bucket_interval_ms=5_000, bucket_rule="boundary-v1", side_mapping="side-v1",
            availability_basis="archive-surrogate", finalization_grace_ms=0,
            evidence_sha256="b" * 64)
        dataset = SimpleNamespace(
            archive_manifest=SimpleNamespace(content_sha256="c" * 64, archive_files=()),
            diagnostics={}, taker_flow_evidence=taker_evidence)
        replay = SimpleNamespace(
            manifest=SimpleNamespace(run_fingerprint="generated-replay",
                                     configured_universe=("BTCUSDT",)),
            diagnostics={})
        prepared = SimpleNamespace(
            period=period, archive_dataset=dataset, canonical_replay_result=replay,
            canonical_v1_branch_by_boundary=None,
            experiment_points=(), core_eligibility_sha256="d" * 64)
        forward = SimpleNamespace(
            evidence_version="forward-v1", evidence_sha256="e" * 64,
            source_dataset_content_sha256="f" * 64)
        coverage = {"coverage_manifest_sha256": "1" * 64,
                    "periods": (), "sources": source_coverage}
        safe_report_json = execution.report_json_safe

        def report_json_safe(value):
            if isinstance(value, SimpleNamespace):
                return {name: report_json_safe(item)
                        for name, item in vars(value).items()}
            return safe_report_json(value)

        ticks = iter(range(1000))
        runtime_metrics = execution.StudyPeriodRuntimeMetrics()
        with patch.object(execution.time, "perf_counter_ns",
                          side_effect=lambda: next(ticks)), patch.object(
                execution, "report_json_safe", side_effect=report_json_safe), patch.object(
                execution, "_prepare_study_period",
                return_value=(prepared, {"sources": source_coverage}, (), {})), patch.object(
                execution, "_load_source_evidence", return_value=supplementary) as source_load, patch.object(
                execution, "_candidate_execution",
                return_value=((), (), (), {}, None, None)) as candidates, patch.object(
                execution, "_label_price_evidence", return_value=forward) as label, patch.object(
                execution, "_continuous_and_event_outcomes",
                return_value=((), (), ())) as outcomes:
            report = execution._execute_period(
                self.manifest, coverage, period, Path("/generated/core"), {},
                revision, None, runtime_metrics=runtime_metrics)

        source_load.assert_called_once()
        candidates.assert_called_once_with(
            prepared, supplementary, None, runtime_metrics=runtime_metrics)
        label.assert_called_once_with(dataset, Path("/generated/core"), period)
        outcomes.assert_called_once_with(forward, (), {}, period)
        self.assertEqual(report["period"]["study_period_index"],
                         period.study_period_index)
        timings = runtime_metrics.report_timings()
        for field in ("supplementary_source_load_seconds",
                      "forward_label_evidence_seconds", "forward_outcomes_seconds"):
            self.assertGreater(timings[field], 0)

    def test_runtime_report_is_a_durable_sidecar_to_identical_period_artifact(self):
        revision = "runtime-fixture-revision"
        executed = []
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            coverage_path, coverage = self._coverage_fixture(root, revision)

            def execute_once(manifest, frozen_coverage, period, archive, roots,
                             code_revision, hmm_model, runtime_metrics=None,
                             checkpoint_root=None, progress=None,
                             runtime_implementation_revision=None,
                             source_validation=None):
                executed.append(period.study_period_index)
                if runtime_metrics is not None:
                    for name in execution.RUNTIME_MEASURED_FIELDS:
                        if name not in ("period_total_seconds",
                                        "period_artifact_write_seconds"):
                            runtime_metrics.record_elapsed_ns(name, 1_000_000)
                empty = []
                scope = ("movement-v1", "config-v1", "universe-v1", "1", ("BTCUSDT",),
                         "provider", "exchange", "trade")
                block = HMMDevelopmentTrainingBlock(
                    period.study_period_index, period.utc_date.isoformat(),
                    period.start_boundary_time_ms, period.end_boundary_time_ms,
                    scope, (), 1440)
                payload = {
                    "period_report_schema_version": execution.PERIOD_REPORT_SCHEMA_VERSION,
                    "execution_version": EXECUTION_VERSION,
                    "candidate_evidence_version": execution.CANDIDATE_EVIDENCE_VERSION,
                    "forward_outcomes_version": execution.FORWARD_OUTCOMES_VERSION,
                    "tool_config_version": TOOL_CONFIG_VERSION,
                    "study_version": self.manifest.study_version,
                    "study_manifest_sha256": manifest.manifest_sha256,
                    "extension_coverage_manifest_sha256": (
                        frozen_coverage["coverage_manifest_sha256"]),
                    "code_revision": code_revision,
                    "period": execution.report_json_safe(period),
                    "canonical_replay_manifest": {
                        "movement_algorithm_version": "movement-v1",
                        "movement_config_version": "config-v1", "universe_id": "universe-v1",
                        "universe_version": "1", "configured_universe": ["BTCUSDT"],
                        "provider": "provider", "exchange": "exchange", "price_type": "trade"},
                    "candidate_evidence": empty,
                    "candidate_evidence_sha256": execution._digest(empty),
                    "v1_evidence_sha256": execution._digest(empty),
                    "event_time_v1_context_version": execution.EVENT_TIME_V1_CONTEXT_VERSION,
                    "event_time_v1_context": empty,
                    "event_time_v1_context_sha256": execution._digest({
                        "version": execution.EVENT_TIME_V1_CONTEXT_VERSION, "records": empty}),
                    "bocpd_onset_evidence_version": execution.BOCPD_ONSET_EVIDENCE_VERSION,
                    "bocpd_onset_evidence": empty,
                    "bocpd_onset_evidence_sha256": execution._digest({
                        "version": execution.BOCPD_ONSET_EVIDENCE_VERSION, "records": empty}),
                    "hmm_development_training_block": execution.report_json_safe(block),
                    "hmm_development_training_block_sha256": block.block_sha256,
                }
                return json.loads(execution._artifact_json(payload, "report_sha256"))

            ticks = iter(range(1000))
            runtime_path = root / "runtime.jsonl"
            progress_path = root / "progress.jsonl"
            with patch.object(execution.time, "perf_counter_ns",
                              side_effect=lambda: next(ticks)), patch.object(
                    execution, "_current_code_revision",
                    side_effect=("runtime-off", "runtime-on")) as current_revision, patch.object(
                    execution, "_execute_period", side_effect=execute_once) as execute:
                off = execution.execute_study_periods(
                    self.manifest, coverage_path, root / "core", root / "off",
                    phase="development", period_limit=1, code_revision=revision)
                on = execution.execute_study_periods(
                    self.manifest, coverage_path, root / "core", root / "on",
                    phase="development", period_limit=1, code_revision=revision,
                    runtime_report_path=runtime_path,
                    progress_report_path=progress_path)

            self.assertEqual(execute.call_count, 2)
            self.assertEqual(current_revision.call_count, 2)
            self.assertEqual(executed, [0, 0])
            for invocation, expected_revision in zip(execute.call_args_list,
                                                     ("runtime-off", "runtime-on")):
                self.assertIsNotNone(invocation.kwargs["checkpoint_root"])
                self.assertTrue(callable(invocation.kwargs["progress"]))
                self.assertEqual(invocation.kwargs["runtime_implementation_revision"],
                                 expected_revision)
            self.assertEqual(off[0].read_bytes(), on[0].read_bytes())
            scientific_text = on[0].read_text(encoding="utf-8")
            scientific = json.loads(scientific_text)
            self.assertNotIn(str(runtime_path), scientific_text)
            self.assertEqual(scientific["report_sha256"],
                             execution._verify_hashed_payload(
                                scientific, "report_sha256", "period report"))
            self.assertNotIn("runtime_implementation_revision", scientific)

            lines = runtime_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            runtime = json.loads(lines[0])
            self.assertEqual(runtime["runtime_report_version"],
                             "historical-market-state-runtime-v1")
            self.assertEqual(runtime["execution_status"], "EXECUTED")
            self.assertEqual(runtime["study_manifest_sha256"],
                             self.manifest.manifest_sha256)
            self.assertEqual(runtime["extension_coverage_manifest_sha256"],
                             coverage["coverage_manifest_sha256"])
            self.assertEqual(runtime["period_report_filename"], on[0].name)
            self.assertEqual(runtime["period_report_sha256"], scientific["report_sha256"])
            self.assertEqual(runtime["artifact_size_bytes"], on[0].stat().st_size)
            progress_records = [json.loads(line) for line in
                                progress_path.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(progress_records)
            self.assertEqual({item["runtime_implementation_revision"]
                              for item in progress_records}, {"runtime-on"})
            self.assertTrue(set(execution.RUNTIME_REPORT_TIMING_FIELDS).issubset(runtime))
            self.assertGreater(runtime["period_total_seconds"], 0)
            self.assertGreater(runtime["period_artifact_write_seconds"], 0)
            self.assertAlmostEqual(
                runtime["all_extensions_seconds"],
                runtime["supplementary_source_load_seconds"]
                + sum(runtime[name] for name in execution.RUNTIME_EXTENSION_FIELDS))

    def test_materialized_and_bounded_period_payloads_are_identical(self):
        revision = "frozen-scientific-producer"
        coverage = {"coverage_manifest_sha256": "c" * 64, "sources": {}}
        utc_day = date(2024, 1, 1)
        start = int(datetime(2024, 1, 1, 12, tzinfo=timezone.utc).timestamp() * 1000)
        end = start + 10_000
        config = HistoricalReplayConfig(
            start, end, finalization_grace_ms=2_000,
            movement_config=MarketMovementConfig(
                historical_lookback_ms=60_000,
                minimum_historical_coverage_ms=60_000))
        period = SimpleNamespace(
            study_period_index=0, utc_date=utc_day, phase="development",
            start_boundary_time_ms=start, end_boundary_time_ms=end)
        forward = SimpleNamespace(
            evidence_version="forward-v1", evidence_sha256="1" * 64,
            source_dataset_content_sha256="2" * 64)
        safe = execution.report_json_safe

        def report_json_safe(value):
            if isinstance(value, SimpleNamespace):
                return {key: report_json_safe(item) for key, item in vars(value).items()}
            if isinstance(value, dict):
                return {key: report_json_safe(item) for key, item in value.items()}
            if isinstance(value, (tuple, list)):
                return [report_json_safe(item) for item in value]
            return safe(value)

        with TemporaryDirectory() as temporary:
            archive_root = Path(temporary)

            def write_package(relative, header, rows):
                archive = archive_root.joinpath(*relative.parts)
                archive.parent.mkdir(parents=True, exist_ok=True)
                text = io.StringIO(newline="")
                writer = csv.writer(text)
                writer.writerow(header)
                writer.writerows(rows)
                with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                    member = zipfile.ZipInfo(
                        f"{relative.stem}.csv", date_time=(1980, 1, 1, 0, 0, 0))
                    member.compress_type = zipfile.ZIP_DEFLATED
                    bundle.writestr(member, text.getvalue())
                digest = hashlib.sha256(archive.read_bytes()).hexdigest()
                Path(f"{archive}.CHECKSUM").write_text(
                    f"{digest}  {relative.name}\n", encoding="utf-8")

            trade_header = (
                "agg_trade_id", "price", "quantity", "first_trade_id",
                "last_trade_id", "transact_time", "is_buyer_maker")
            trades = (
                ("1", "100", "1", "1", "1", str(start - 5_000), "false"),
                ("2", "101", "1", "2", "2", str(start), "false"),
                ("3", "102", "1", "3", "3", str(start + 5_000), "false"),
            )
            kline_header = (
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_asset_volume", "number_of_trades",
                "taker_buy_base_asset_volume", "taker_buy_quote_asset_volume", "ignore")
            candle_open = historical_candle_start_ms(config)
            candles = ((str(candle_open), "100", "101", "99", "100", "1",
                        str(candle_open + 59_999), "100", "1", "0.5", "50", "0"),)
            for day in required_aggtrade_dates(config):
                write_package(
                    daily_aggtrades_relative_path("BTCUSDT", day), trade_header,
                    trades if day == utc_day else ())
            for day in required_kline_dates(config):
                write_package(
                    daily_kline_relative_path("BTCUSDT", day), kline_header,
                    candles if day == utc_day else ())

            request = BinanceUSDMArchiveRequest(
                archive_root, MarketUniverseInput("period-parity", "v1", ("BTCUSDT",)),
                config)
            materialized_dataset = load_binance_usdm_historical_replay_dataset(
                request, include_taker_flow_evidence=True)
            bounded_dataset = load_binance_usdm_bounded_historical_replay_dataset(request)
            try:
                materialized_replay = run_historical_market_replay(
                    materialized_dataset.replay_request)
                bounded_replay = run_bounded_historical_market_replay(bounded_dataset)
                prepared_routes = []
                v1_routes = []
                for dataset, replay in ((materialized_dataset, materialized_replay),
                                         (bounded_dataset, bounded_replay)):
                    experiment_points = execution._study_experiment_points(replay, period)
                    v1_records, v1_states, v1_branches = execution._build_v1_evidence(
                        period, replay.points)
                    prepared_routes.append((
                        SimpleNamespace(
                            archive_dataset=dataset,
                            canonical_replay_result=replay,
                            experiment_points=experiment_points,
                            canonical_v1_branch_by_boundary=v1_branches,
                            core_eligibility_sha256="b" * 64),
                        {"sources": {}}, v1_records, v1_states))
                    v1_routes.append((experiment_points, v1_records, v1_states, v1_branches))

                with patch.object(execution, "report_json_safe",
                                  side_effect=report_json_safe), patch.object(
                        execution, "_prepare_study_period",
                        side_effect=prepared_routes), patch.object(
                        execution, "_load_source_evidence", return_value={}), patch.object(
                        execution, "_candidate_execution",
                        return_value=((), (), (), {}, None, None)), patch.object(
                        execution, "_label_price_evidence", return_value=forward), patch.object(
                        execution, "_continuous_and_event_outcomes",
                        return_value=((), (), ())):
                    materialized_payload = execution._execute_period(
                        self.manifest, coverage, period, archive_root, {}, revision, None)
                    bounded_payload = execution._execute_period(
                        self.manifest, coverage, period, archive_root, {}, revision, None,
                        runtime_metrics=execution.StudyPeriodRuntimeMetrics(),
                        checkpoint_root=archive_root / "checkpoints",
                        progress=lambda *_args: None,
                        runtime_implementation_revision="runtime-implementation")
            finally:
                bounded_dataset.close()

        self.assertTrue(materialized_replay.points)
        self.assertEqual(materialized_replay.points, bounded_replay.points)
        self.assertEqual(
            tuple(item.movement_evaluation for item in materialized_replay.points),
            tuple(item.movement_evaluation for item in bounded_replay.points))
        self.assertEqual(materialized_replay.diagnostics, bounded_replay.diagnostics)
        self.assertEqual(materialized_replay.manifest, bounded_replay.manifest)
        self.assertTrue(v1_routes[0][1])
        self.assertEqual(v1_routes[0], v1_routes[1])
        self.assertEqual(execution._canonical(materialized_payload),
                         execution._canonical(bounded_payload))
        self.assertEqual(materialized_payload["report_sha256"],
                         bounded_payload["report_sha256"])
        for operational_key in ("checkpoint_dir", "progress", "runtime_report",
                                "runtime_implementation_revision", "timings"):
            self.assertNotIn(operational_key, bounded_payload)

    def test_coverage_only_freeze_binds_ordered_dates_and_semantic_sources(self):
        core = {
            "status": "MISSING_PACKAGE_OR_CHECKSUM",
            "expected_package_count": 1,
            "checksum_verified_package_count": 0,
            "archive_content_sha256": None,
            "raw_replayable_trade_evidence_present": False,
            "candle_coverage": None,
            "packages": ({"package_name": "BTCUSDT/aggTrades/fixture.zip",
                          "status": "MISSING_PACKAGE", "sha256": None},),
        }
        sources = {name: {"coverage_state": "UNAVAILABLE",
                          "expected_package_count": 1,
                          "checksum_verified_package_count": 0,
                          "packages": (), "per_symbol": ()}
                   for name in SOURCE_NAMES}
        revision = "fixture-revision"
        with patch("market_analysis.historical_market_state_study_execution._verify_core_coverage",
                   return_value=core), patch(
                "market_analysis.historical_market_state_study_execution._load_supplementary_sources",
                return_value=({}, sources)), patch(
                "market_analysis.historical_market_state_study_execution._candidate_execution",
                side_effect=AssertionError("coverage phase must not run candidates")), patch(
                "market_analysis.historical_market_state_study_execution.evaluate_continuous_grids",
                side_effect=AssertionError("coverage phase must not build labels")):
            artifact = build_extension_coverage_manifest(
                self.manifest, "/generated/archive-root", code_revision=revision)

        self.assertEqual(_canonical(artifact["source_identities"]),
                         _canonical(SOURCE_IDENTITIES))
        self.assertEqual(artifact["tool_config_version"], TOOL_CONFIG_VERSION)
        self.assertEqual(len(artifact["periods"]), 30)
        self.assertEqual(artifact["periods"][0]["utc_date"], "2024-01-01")
        with TemporaryDirectory() as temporary:
            report_path = Path(temporary) / "coverage.json"
            report_path.write_text(_canonical(artifact), encoding="utf-8")
            loaded = load_and_validate_coverage(report_path, self.manifest, revision)
            self.assertEqual(loaded["coverage_manifest_sha256"],
                             artifact["coverage_manifest_sha256"])
            self.assertNotIn(temporary, report_path.read_text(encoding="utf-8"))

    def test_no_observed_liquidation_is_distinct_from_unavailable_archive(self):
        package = SimpleNamespace(
            utc_day=date(2024, 1, 1),
            relative_path=daily_liquidation_relative_path(date(2024, 1, 1)).as_posix(),
            status="ARCHIVE_DAY_AVAILABLE", archive_present=True,
            compressed_sha256="a" * 64, row_count=1)
        evidence = SimpleNamespace(
            packages=(package,), configured_symbols=("BTCUSDT", "ETHUSDT"),
            rows=(SimpleNamespace(symbol="BTCUSDT"),))

        shared = _source_coverage_summary(evidence, source_kind="liquidation")
        unavailable_package = SimpleNamespace(
            utc_day=date(2024, 1, 1),
            relative_path=daily_liquidation_relative_path(date(2024, 1, 1)).as_posix(),
            status="INVALID_ARCHIVE", archive_present=True,
            compressed_sha256="b" * 64, row_count=0)
        unavailable = _source_coverage_summary(SimpleNamespace(
            packages=(unavailable_package,), configured_symbols=("BTCUSDT", "ETHUSDT"),
            rows=()), source_kind="liquidation")
        loader_error = _source_coverage_summary(
            None, "FileNotFoundError", source_kind="liquidation")

        self.assertEqual(shared["coverage_state"], "FULL")
        self.assertEqual(tuple(item["coverage_state"] for item in shared["per_symbol"]),
                         ("FULL", "FULL"))
        self.assertEqual(tuple(item["observed_row_count"] for item in shared["per_symbol"]),
                         (1, 0))
        self.assertEqual(shared["per_symbol"][1]["observation_state"],
                         "NO_OBSERVED_LIQUIDATION")
        self.assertEqual(unavailable["coverage_state"], "UNAVAILABLE")
        self.assertEqual(tuple(item["observation_state"] for item in unavailable["per_symbol"]),
                         ("UNAVAILABLE", "UNAVAILABLE"))
        self.assertEqual(loader_error["per_symbol"], ())

    def test_package_coverage_uses_source_native_status_and_row_issues(self):
        verified = SimpleNamespace(
            symbol="BTCUSDT", relative_path="BTCUSDT/verified.zip", status="VERIFIED",
            archive_present=True, checksum_present=True, checksum_verified=True,
            sha256="a" * 64, row_count=2, valid_row_count=2)
        missing = SimpleNamespace(
            symbol="BTCUSDT", relative_path="BTCUSDT/missing.zip", status="MISSING_PACKAGE",
            archive_present=False, checksum_present=False, checksum_verified=False)
        bad_checksum = SimpleNamespace(
            symbol="BTCUSDT", relative_path="BTCUSDT/bad.zip", status="CHECKSUM_MISMATCH",
            archive_present=True, checksum_present=True, checksum_verified=False)
        full = _source_coverage_summary(SimpleNamespace(
            packages=(verified,), configured_symbols=("BTCUSDT",), rows=()),
            source_kind="open_interest")
        partial = _source_coverage_summary(SimpleNamespace(
            packages=(verified, missing), configured_symbols=("BTCUSDT",), rows=()),
            source_kind="open_interest")
        invalid = _source_coverage_summary(SimpleNamespace(
            packages=(bad_checksum,), configured_symbols=("BTCUSDT",), rows=()),
            source_kind="open_interest")

        self.assertEqual(full["coverage_state"], "FULL")
        self.assertEqual(partial["coverage_state"], "PARTIAL")
        self.assertEqual(partial["packages"][1]["status"], "MISSING_PACKAGE")
        self.assertEqual(invalid["coverage_state"], "UNAVAILABLE")
        self.assertEqual(invalid["packages"][0]["status"], "CHECKSUM_MISMATCH")

        for source_kind in ("mark_trade", "open_interest", "funding"):
            for native_status in ("SCHEMA_MISMATCH", "INVALID_ARCHIVE"):
                bad_native_package = SimpleNamespace(
                    symbol="BTCUSDT", relative_path=f"BTCUSDT/{native_status}.zip",
                    status=native_status, archive_present=True, checksum_present=True,
                    checksum_verified=True, sha256="c" * 64, row_count=2)
                bad_native = _source_coverage_summary(SimpleNamespace(
                    packages=(bad_native_package,), configured_symbols=("BTCUSDT",), rows=()),
                    source_kind=source_kind)
                self.assertEqual(bad_native["coverage_state"], "UNAVAILABLE")
                self.assertFalse(bad_native["packages"][0]["package_valid"])

        malformed_only = SimpleNamespace(
            symbol="BTCUSDT", relative_path="BTCUSDT/malformed-only.zip",
            status="MALFORMED_ROW", archive_present=True, checksum_present=True,
            checksum_verified=True, sha256="e" * 64, row_count=1,
            malformed_rows=("row 1 is invalid",))
        malformed_only_summary = _source_coverage_summary(SimpleNamespace(
            packages=(malformed_only,), configured_symbols=("BTCUSDT",), rows=()),
            source_kind="open_interest")
        self.assertEqual(malformed_only_summary["coverage_state"], "UNAVAILABLE")
        self.assertFalse(malformed_only_summary["packages"][0]["package_valid"])

        partial_rows = SimpleNamespace(
            symbol="BTCUSDT", relative_path="BTCUSDT/partial.zip", status="VERIFIED",
            archive_present=True, checksum_present=True, checksum_verified=True,
            sha256="d" * 64, row_count=1, missing_ranges=((1, 2, 1),))
        partial_observations = _source_coverage_summary(SimpleNamespace(
            packages=(partial_rows,), configured_symbols=("BTCUSDT",), rows=()),
            source_kind="open_interest")
        self.assertEqual(partial_observations["coverage_state"], "PARTIAL")

        partial_mark_package = SimpleNamespace(
            symbol="BTCUSDT", utc_date=date(1970, 1, 1),
            relative_archive_path="BTCUSDT/mark.zip", status="MISSING_VALID_MINUTE",
            statuses=("MISSING_VALID_MINUTE",), archive_present=True,
            checksum_present=True, checksum_verified=True, archive_sha256="f" * 64,
            row_count=3, valid_row_count=1, missing_minute_ranges=((60_000, 120_000, 2),))
        partial_mark = _source_coverage_summary(SimpleNamespace(
            packages=(partial_mark_package,), configured_symbols=("BTCUSDT",),
            candles=(SimpleNamespace(symbol="BTCUSDT", open_time_ms=0),)),
            source_kind="mark_trade")
        self.assertEqual(partial_mark["coverage_state"], "PARTIAL")
        self.assertEqual(partial_mark["per_symbol"][0]["observation_state"], "OBSERVED")

    def test_zero_observation_labels_are_source_specific(self):
        cases = (
            ("mark_trade", SimpleNamespace(
                symbol="BTCUSDT", utc_date=date(1970, 1, 1),
                relative_archive_path="BTCUSDT/mark.zip",
                status="VERIFIED_COMPLETE", statuses=("VERIFIED_COMPLETE",),
                archive_present=True, checksum_present=True, checksum_verified=True,
                archive_sha256="a" * 64)),
            ("open_interest", SimpleNamespace(
                symbol="BTCUSDT", relative_path="BTCUSDT/oi.zip", status="VERIFIED",
                archive_present=True, checksum_present=True, checksum_verified=True,
                sha256="b" * 64)),
            ("funding", SimpleNamespace(
                symbol="BTCUSDT", relative_path="BTCUSDT/funding.zip", status="VERIFIED",
                archive_present=True, checksum_present=True, checksum_verified=True,
                sha256="c" * 64)),
        )
        for source_kind, package in cases:
            with self.subTest(source_kind=source_kind):
                summary = _source_coverage_summary(SimpleNamespace(
                    packages=(package,), configured_symbols=("BTCUSDT",), rows=()),
                    source_kind=source_kind)
                self.assertEqual(summary["coverage_state"], "FULL")
                self.assertNotEqual(summary["per_symbol"][0]["observation_state"],
                                    "NO_OBSERVED_LIQUIDATION")
                self.assertEqual(summary["per_symbol"][0]["observation_state"],
                                 execution._NO_OBSERVATION_STATES[source_kind])

    def test_period_resume_skips_valid_report_and_rejects_corruption(self):
        revision = "fixture-revision"
        periods = self.manifest.selected_periods
        ordered = [{"study_period_index": item.study_period_index,
                    "utc_date": item.utc_date.isoformat(), "phase": item.phase}
                   for item in periods]
        coverage = {
            "coverage_version": EXTENSION_COVERAGE_VERSION,
            "study_version": self.manifest.study_version,
            "study_manifest_sha256": self.manifest.manifest_sha256,
            "ordered_periods": ordered,
            "source_identities": execution.report_json_safe(SOURCE_IDENTITIES),
            "tool_config_version": TOOL_CONFIG_VERSION,
            "code_revision": revision,
            "periods": ordered,
        }
        coverage["coverage_manifest_sha256"] = execution._digest(coverage)
        period = periods[0]
        empty = []
        scope = ("movement-v1", "config-v1", "universe-v1", "1", ("BTCUSDT",),
                 "provider", "exchange", "trade")
        block = HMMDevelopmentTrainingBlock(
            period.study_period_index, period.utc_date.isoformat(),
            period.start_boundary_time_ms, period.end_boundary_time_ms,
            scope, (), 1440)
        replay_identity = {
            "movement_algorithm_version": "movement-v1",
            "movement_config_version": "config-v1", "universe_id": "universe-v1",
            "universe_version": "1", "configured_universe": ["BTCUSDT"],
            "provider": "provider", "exchange": "exchange", "price_type": "trade",
        }
        context_hashes = {
            name: {"version": version, "records": empty}
            for name, version in (
                ("event_time_v1_context", execution.EVENT_TIME_V1_CONTEXT_VERSION),
                ("bocpd_onset_evidence", execution.BOCPD_ONSET_EVIDENCE_VERSION),
            )
        }
        report = execution._artifact_json({
            "period_report_schema_version": execution.PERIOD_REPORT_SCHEMA_VERSION,
            "execution_version": EXECUTION_VERSION,
            "study_version": self.manifest.study_version,
            "candidate_evidence_version": execution.CANDIDATE_EVIDENCE_VERSION,
            "forward_outcomes_version": execution.FORWARD_OUTCOMES_VERSION,
            "tool_config_version": TOOL_CONFIG_VERSION,
            "study_manifest_sha256": self.manifest.manifest_sha256,
            "extension_coverage_manifest_sha256": coverage["coverage_manifest_sha256"],
            "code_revision": revision,
            "period": execution.report_json_safe(period),
            "canonical_replay_manifest": replay_identity,
            "candidate_evidence": empty,
            "candidate_evidence_sha256": execution._digest(empty),
            "v1_evidence_sha256": execution._digest(empty),
            "event_time_v1_context_version": execution.EVENT_TIME_V1_CONTEXT_VERSION,
            "event_time_v1_context": empty,
            "event_time_v1_context_sha256": execution._digest(
                context_hashes["event_time_v1_context"]),
            "bocpd_onset_evidence_version": execution.BOCPD_ONSET_EVIDENCE_VERSION,
            "bocpd_onset_evidence": empty,
            "bocpd_onset_evidence_sha256": execution._digest(
                context_hashes["bocpd_onset_evidence"]),
            "hmm_development_training_block": execution.report_json_safe(block),
            "hmm_development_training_block_sha256": block.block_sha256,
        }, "report_sha256")
        report_payload = json.loads(report)

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            coverage_path = root / "coverage.json"
            coverage_path.write_text(_canonical(coverage), encoding="utf-8")
            with patch.object(execution, "_execute_period", return_value=report_payload) as execute:
                first = execution.execute_study_periods(
                    self.manifest, coverage_path, root / "core", root / "output",
                    period_limit=1, code_revision=revision)
                second = execution.execute_study_periods(
                    self.manifest, coverage_path, root / "core", root / "output",
                    period_limit=1, code_revision=revision)
                self.assertEqual(first, second)
                runtime_path = root / "runtime.jsonl"
                resumed = execution.execute_study_periods(
                    self.manifest, coverage_path, root / "core", root / "output",
                    period_limit=1, code_revision=revision,
                    runtime_report_path=runtime_path)
                execute.assert_called_once()
                self.assertEqual(resumed, first)
                progress_rows = [json.loads(line) for line in
                                 (root / "output" / "study-progress.jsonl").read_text(
                                     encoding="utf-8").splitlines()]
                self.assertEqual([row["event"] for row in progress_rows],
                                 ["PERIOD_STARTED", "PERIOD_ARTIFACT_FINALIZED",
                                  "SKIPPED_EXISTING_ARTIFACT",
                                  "SKIPPED_EXISTING_ARTIFACT"])
                self.assertTrue(all(row["scientific_producer_revision"] == revision
                                    for row in progress_rows))
                self.assertTrue(all(row["runtime_implementation_revision"] != revision
                                    for row in progress_rows))
                self.assertEqual(report_payload["code_revision"], revision)
                self.assertNotIn("runtime_implementation_revision", report_payload)
                skipped = json.loads(runtime_path.read_text(encoding="utf-8"))
                self.assertEqual(skipped["execution_status"],
                                 "SKIPPED_EXISTING_ARTIFACT")
                self.assertEqual(skipped["artifact_size_bytes"], first[0].stat().st_size)
                self.assertEqual(skipped["period_report_sha256"],
                                 report_payload["report_sha256"])
                self.assertTrue(set(execution.RUNTIME_REPORT_TIMING_FIELDS).isdisjoint(
                    skipped))
                first[0].write_text("{}", encoding="utf-8")
                with self.assertRaises(ValueError):
                    execution.execute_study_periods(
                        self.manifest, coverage_path, root / "core", root / "output",
                        period_limit=1, code_revision=revision)


if __name__ == "__main__":
    unittest.main()
