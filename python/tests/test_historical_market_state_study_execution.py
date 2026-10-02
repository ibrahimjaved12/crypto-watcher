"""Generated governance and identity fixtures for Part-B execution."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from datetime import date
import json
import unittest
from unittest.mock import Mock, patch

from market_analysis.historical_market_state_study import (
    parse_historical_market_state_study_manifest_json,
)
from market_analysis.historical_market_state_study_execution import (
    EXECUTION_VERSION, EXTENSION_COVERAGE_VERSION, SOURCE_IDENTITIES,
    TOOL_CONFIG_VERSION, _canonical,
    _source_coverage_summary, build_extension_coverage_manifest,
    load_and_validate_coverage, select_execution_periods,
    freeze_study_hmm_model, validate_hmm_development_cohort,
)
from market_analysis.historical_liquidation_evidence import daily_liquidation_relative_path
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

    def test_period_preparation_loads_and_replays_once(self):
        period = self.manifest.selected_periods[0]
        content_sha = "a" * 64
        archive_manifest = SimpleNamespace(
            content_sha256=content_sha, archive_files=(),
            dataset_id="fixture-dataset", dataset_version="fixture-v1")
        dataset = SimpleNamespace(
            archive_manifest=archive_manifest, replay_request=object())
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
        with patch.object(execution, "load_binance_usdm_historical_replay_dataset",
                          return_value=dataset) as loader, patch.object(
                execution, "run_historical_market_replay", return_value=replay) as runner, patch.object(
                execution, "_frozen_eligibility", return_value=eligibility), patch.object(
                execution, "_study_experiment_points", return_value=()), patch.object(
                execution, "_build_v1_evidence", return_value=((), {}, {})), patch.object(
                execution, "PreparedHistoricalMarketStatePeriod",
                return_value=prepared_marker) as prepared_type:
            prepared, frozen, _, _ = execution._prepare_study_period(
                self.manifest,
                {"periods": [coverage_period]}, period, Path("/local/core"), "fixture-rev")

        self.assertIs(prepared, prepared_marker)
        self.assertIs(frozen, coverage_period)
        self.assertEqual(loader.call_count, 1)
        self.assertEqual(runner.call_count, 1)
        self.assertIs(runner.call_args.args[0], dataset.replay_request)
        prepared_type.assert_called_once()

    def test_candidate_dispatch_reuses_shared_branch_and_atr_once(self):
        period = self.manifest.selected_periods[0]
        branch = {period.start_boundary_time_ms: ("classification", "lifecycle")}
        prepared = SimpleNamespace(
            period=period, archive_dataset=SimpleNamespace(ohlc_evidence=object()),
            canonical_replay_result=SimpleNamespace(
                manifest=SimpleNamespace(configured_universe=("BTCUSDT",))),
            experiment_points=(), canonical_v1_branch_by_boundary=branch)
        runner = Mock(return_value=SimpleNamespace())
        second_runner = Mock(return_value=SimpleNamespace())
        descriptors = (
            SimpleNamespace(experiment_id="EXP-75-09", algorithm_version="hmm-v1",
                            config_version="hmm-config"),
            SimpleNamespace(experiment_id="EXP-75-01", algorithm_version="ewma-v1",
                            config_version="ewma-config", config=object(), runner=runner),
            SimpleNamespace(experiment_id="EXP-75-02", algorithm_version="cusum-v1",
                            config_version="cusum-config", config=object(), runner=second_runner),
        )
        supplementary = {
            name: {"evidence": None, "coverage": {"coverage_state": "UNAVAILABLE"},
                   "root": Path("/local/source")}
            for name in SOURCE_NAMES
        }
        bundle = SimpleNamespace(records=(), native_summaries={}, result_sha256="c" * 64)
        with patch.object(execution, "EXPERIMENT_SUITE_V1", descriptors), patch.object(
                execution, "_hmm_study_evidence", return_value=((), {}, None, None)), patch.object(
                execution, "run_market_state_atr_normalization_suite", return_value=()) as atr, patch.object(
                execution, "build_historical_study_taker_flow_points", return_value=()), patch.object(
                execution, "_taker_summaries", return_value={"development": {}}), patch.object(
                execution, "adapt_candidate_result", return_value=bundle):
            execution._candidate_execution(prepared, supplementary, None)

        self.assertEqual(runner.call_count, 1)
        self.assertEqual(second_runner.call_count, 1)
        self.assertIs(runner.call_args.kwargs["canonical_branch_by_boundary"], branch)
        self.assertIs(second_runner.call_args.kwargs["canonical_branch_by_boundary"], branch)
        atr.assert_called_once()
        self.assertIs(atr.call_args.kwargs["canonical_branch_by_boundary"], branch)

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
        report = execution._artifact_json({
            "execution_version": EXECUTION_VERSION,
            "study_manifest_sha256": self.manifest.manifest_sha256,
            "extension_coverage_manifest_sha256": coverage["coverage_manifest_sha256"],
            "code_revision": revision,
            "period": execution.report_json_safe(period),
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
                execute.assert_called_once()
                first[0].write_text("{}", encoding="utf-8")
                with self.assertRaises(ValueError):
                    execution.execute_study_periods(
                        self.manifest, coverage_path, root / "core", root / "output",
                        period_limit=1, code_revision=revision)


if __name__ == "__main__":
    unittest.main()
