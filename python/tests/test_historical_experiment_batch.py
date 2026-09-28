"""Fixed-suite orchestration and generated-archive report fixtures."""

import argparse
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import MappingProxyType
import unittest
from unittest.mock import Mock, patch
import zipfile

from market_analysis import historical_experiment_batch as batch
from market_analysis.binance_historical_archive import (
    BinanceUSDMArchiveRequest, daily_aggtrades_relative_path,
    daily_kline_relative_path, load_binance_usdm_historical_replay_dataset,
    required_aggtrade_dates, required_kline_dates,
)
from market_analysis.experiments import (
    market_state_bocpd, market_state_correlation_clusters, market_state_cusum,
    market_state_ewma, market_state_hmm_regimes, market_state_kalman,
    market_state_pca_common_factor, market_state_pelt,
    market_state_realized_vol_normalization, market_state_regression_acceleration,
)
from market_analysis.historical_replay import (
    HistoricalReplayConfig, ReplayPartitionPlan, run_historical_market_replay,
    to_market_state_experiment_points,
)
from market_analysis.market_episode_lifecycle import MarketEpisodeLifecycleConfig
from market_analysis.movement_classifier import MarketClassifierConfig
from market_analysis.movement_metrics import MarketMovementConfig, MarketUniverseInput


OUTPUT = int(datetime(2026, 8, 20, 12, 30, tzinfo=timezone.utc).timestamp() * 1000)
SYMBOLS = ("BTCUSDT", "ETHUSDT")
PARTITIONS = ("development", "validation", "test", "all")


def _canonical(value):
    return json.dumps(batch.report_json_safe(value), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _write_archive(root, relative, rows):
    path = root.joinpath(*relative.parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    member = zipfile.ZipInfo(f"{relative.stem}.csv", (1980, 1, 1, 0, 0, 0))
    member.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr(member, "\n".join(",".join(str(field) for field in row)
                                         for row in rows))
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    Path(f"{path}.CHECKSUM").write_text(f"{sha}  {relative.name}\n", encoding="utf-8")


def _request(root, *, partition=None, code_revision=None):
    config = HistoricalReplayConfig(OUTPUT, OUTPUT + 15_000)
    universe = MarketUniverseInput("research-two", "v1", SYMBOLS)
    for symbol in SYMBOLS:
        for day in required_aggtrade_dates(config):
            trade_rows = ([1, "100", "1", 1, 1, OUTPUT, "false"],) if day == date(2026, 8, 20) else ()
            _write_archive(root, daily_aggtrades_relative_path(symbol, day), trade_rows)
        for day in required_kline_dates(config):
            _write_archive(root, daily_kline_relative_path(symbol, day), ())
    return batch.HistoricalExperimentBatchRequest(
        root, universe, config,
        partition or ReplayPartitionPlan(OUTPUT + 5_000, OUTPUT + 10_000),
        code_revision,
    )


def _cli_args(root):
    return [
        "--archive-root", str(root), "--symbols", "ETHUSDT", "BTCUSDT",
        "--universe-id", "research-two", "--universe-version", "v1",
        "--start", "2026-08-20T12:30:00Z",
        "--end", "2026-08-20T12:30:15Z",
        "--development-end", "2026-08-20T12:30:05Z",
        "--validation-end", "2026-08-20T12:30:10Z",
    ]


class HistoricalExperimentBatchTests(unittest.TestCase):
    def test_exact_registry_order_counts_and_bindings(self):
        families = (
            ("EXP-75-01", market_state_ewma.EWMA_CONFIGURATIONS,
             market_state_ewma.EWMA_ALGORITHM_VERSION,
             market_state_ewma.run_market_state_ewma_experiment),
            ("EXP-75-02", market_state_cusum.CUSUM_CONFIGURATIONS,
             market_state_cusum.CUSUM_ALGORITHM_VERSION,
             market_state_cusum.run_market_state_cusum_experiment),
            ("EXP-75-03", market_state_kalman.KALMAN_CONFIGURATIONS,
             market_state_kalman.KALMAN_ALGORITHM_VERSION,
             market_state_kalman.run_market_state_kalman_experiment),
            ("EXP-75-04A", market_state_pelt.PELT_CONFIGURATIONS,
             market_state_pelt.PELT_ALGORITHM_VERSION,
             market_state_pelt.run_market_state_pelt_experiment),
            ("EXP-75-04B", market_state_bocpd.BOCPD_CONFIGURATIONS,
             market_state_bocpd.BOCPD_ALGORITHM_VERSION,
             market_state_bocpd.run_market_state_bocpd_experiment),
            ("EXP-75-05", market_state_regression_acceleration.REGRESSION_CONFIGURATIONS,
             market_state_regression_acceleration.REGRESSION_ACCELERATION_ALGORITHM_VERSION,
             market_state_regression_acceleration.run_market_state_regression_acceleration_experiment),
            ("EXP-75-06A", market_state_realized_vol_normalization.REALIZED_VOL_CONFIGURATIONS,
             market_state_realized_vol_normalization.REALIZED_VOLATILITY_ALGORITHM_VERSION,
             market_state_realized_vol_normalization.run_market_state_realized_vol_normalization_experiment),
            ("EXP-75-07", market_state_pca_common_factor.PCA_CONFIGURATIONS,
             market_state_pca_common_factor.PCA_ALGORITHM_VERSION,
             market_state_pca_common_factor.run_market_state_pca_common_factor_experiment),
            ("EXP-75-08", market_state_correlation_clusters.CORRELATION_CONFIGURATIONS,
             market_state_correlation_clusters.CORRELATION_ALGORITHM_VERSION,
             market_state_correlation_clusters.run_market_state_correlation_cluster_experiment),
            ("EXP-75-09", (market_state_hmm_regimes.HMM_CONFIG_V1,),
             market_state_hmm_regimes.HMM_ALGORITHM_VERSION,
             market_state_hmm_regimes.run_market_state_hmm_experiment),
        )
        expected = tuple((family_id, config, version, runner)
                         for family_id, configs, version, runner in families
                         for config in configs)
        suite = batch.EXPERIMENT_SUITE_V1
        self.assertEqual(len(suite), 28)
        self.assertEqual(tuple((item.experiment_id, item.config,
                                item.algorithm_version, item.runner) for item in suite),
                         expected)
        self.assertEqual(tuple(Counter(item.experiment_id for item in suite).values()),
                         (3,) * 9 + (1,))
        for descriptor, (_, config, _, _) in zip(suite, expected):
            self.assertIs(type(descriptor.config), type(config))
            self.assertEqual(descriptor.config_version, config.version)

    def test_canonical_movement_config_required(self):
        with tempfile.TemporaryDirectory() as folder:
            canonical = _request(Path(folder))
            custom = replace(canonical.replay_config,
                             movement_config=MarketMovementConfig(version="custom"))
            with self.assertRaisesRegex(ValueError, "canonical default"):
                replace(canonical, replay_config=custom)
            with self.assertRaisesRegex(ValueError, "at least two symbols"):
                replace(canonical, universe=MarketUniverseInput("single", "v1", ("BTCUSDT",)))

    def test_one_load_replay_export_and_same_stream_for_all_28(self):
        with tempfile.TemporaryDirectory() as folder:
            request = _request(Path(folder))
            load = Mock(wraps=batch.load_binance_usdm_historical_replay_dataset)
            replay = Mock(wraps=batch.run_historical_market_replay)
            export = Mock(wraps=batch.to_market_state_experiment_points)
            seen = []
            wrapped = []
            for descriptor in batch.EXPERIMENT_SUITE_V1:
                def runner(points, config, *, classifier_config, lifecycle_config,
                           original=descriptor.runner):
                    seen.append((points, classifier_config, lifecycle_config))
                    return original(points, config, classifier_config=classifier_config,
                                    lifecycle_config=lifecycle_config)
                wrapped.append(replace(descriptor, runner=runner))
            with (patch.object(batch, "load_binance_usdm_historical_replay_dataset", load),
                  patch.object(batch, "run_historical_market_replay", replay),
                  patch.object(batch, "to_market_state_experiment_points", export),
                  patch.object(batch, "EXPERIMENT_SUITE_V1", tuple(wrapped))):
                report = batch.run_historical_experiment_batch(request)
            self.assertEqual(load.call_count, 1)
            self.assertEqual(replay.call_count, 1)
            self.assertEqual(export.call_count, 1)
            self.assertEqual(len(seen), 28)
            self.assertEqual(report.experiment_run_count, 28)
            self.assertTrue(all(item[0] is seen[0][0] for item in seen))
            self.assertTrue(all(item[1] is seen[0][1] for item in seen))
            self.assertTrue(all(item[2] is seen[0][2] for item in seen))

    def test_end_to_end_report_repeated_run_and_compact_summaries(self):
        with tempfile.TemporaryDirectory() as folder:
            request = _request(Path(folder))
            first = batch.run_historical_experiment_batch(request)
            second = batch.run_historical_experiment_batch(request)
            self.assertEqual(first, second)
            content = batch.historical_experiment_report_to_json(first)
            self.assertEqual(content, batch.historical_experiment_report_to_json(second))
            self.assertEqual(first.experiment_run_count, 28)
            self.assertEqual(len(first.experiment_runs), 28)
            self.assertTrue(all(tuple(run.summaries) == PARTITIONS
                                and run.status == "COMPLETED"
                                for run in first.experiment_runs))
            hmm = first.experiment_runs[-1]
            self.assertEqual(hmm.experiment_id, "EXP-75-09")
            self.assertEqual(hmm.status, "COMPLETED")
            self.assertEqual(hmm.diagnostics["training_diagnostics"].status,
                             "HMM_TRAINING_UNAVAILABLE")
            self.assertIn(hmm.diagnostics["training_diagnostics"].reason,
                          ("HMM_INSUFFICIENT_TRAINING_ROWS",
                           "HMM_INSUFFICIENT_TRAINING_TRANSITIONS"))
            self.assertIsNone(hmm.diagnostics["model_sha256"])
            for forbidden in ("paired_points", "baseline_points", "segmentations",
                              "detection_regions_by_partition"):
                self.assertNotIn(forbidden, content)
            dataset = load_binance_usdm_historical_replay_dataset(
                BinanceUSDMArchiveRequest(request.archive_root,
                                          request.universe, request.replay_config))
            replay = run_historical_market_replay(dataset.replay_request)
            points = to_market_state_experiment_points(replay, request.partition_plan)
            native = market_state_ewma.run_market_state_ewma_experiment(
                points, market_state_ewma.EWMA_CONFIGURATIONS[0],
                classifier_config=MarketClassifierConfig(),
                lifecycle_config=MarketEpisodeLifecycleConfig())
            self.assertEqual(batch.report_json_safe(first.experiment_runs[0].summaries["test"]),
                             batch.report_json_safe(native.summaries["test"]))

    def test_root_invariance_partition_change_and_stream_sha(self):
        with tempfile.TemporaryDirectory() as first_root, tempfile.TemporaryDirectory() as second_root:
            first_request = _request(Path(first_root))
            second_request = _request(Path(second_root))
            first = batch.run_historical_experiment_batch(first_request)
            second = batch.run_historical_experiment_batch(second_request)
            self.assertEqual(first, second)
            self.assertEqual(batch.historical_experiment_report_to_json(first),
                             batch.historical_experiment_report_to_json(second))
            self.assertNotIn(first_root, batch.historical_experiment_report_to_json(first))
            revised_plan = ReplayPartitionPlan(OUTPUT, OUTPUT + 5_000)
            changed = batch.run_historical_experiment_batch(
                replace(first_request, partition_plan=revised_plan))
            self.assertEqual(first.replay_manifest.run_fingerprint,
                             changed.replay_manifest.run_fingerprint)
            self.assertNotEqual(first.suite_manifest.experiment_stream_sha256,
                                changed.suite_manifest.experiment_stream_sha256)
            self.assertNotEqual(first.suite_manifest.batch_run_fingerprint,
                                changed.suite_manifest.batch_run_fingerprint)
            self.assertNotEqual(first.experiment_runs[0].experiment_run_id,
                                changed.experiment_runs[0].experiment_run_id)
            self.assertNotEqual(first.report_sha256, changed.report_sha256)
            dataset = load_binance_usdm_historical_replay_dataset(
                BinanceUSDMArchiveRequest(first_request.archive_root,
                                          first_request.universe, first_request.replay_config))
            replay = run_historical_market_replay(dataset.replay_request)
            points = to_market_state_experiment_points(replay, first_request.partition_plan)
            independent = _digest({
                "replay_run_fingerprint": replay.manifest.run_fingerprint,
                "development_end_boundary_time_ms": OUTPUT + 5_000,
                "validation_end_boundary_time_ms": OUTPUT + 10_000,
                "points": [[source.point_id, point.partition]
                           for source, point in zip(replay.points, points)],
            })
            self.assertEqual(first.suite_manifest.experiment_stream_sha256, independent)
            self.assertEqual(tuple(point.point_id for point in replay.points),
                             tuple(point.point_id for point in
                                   run_historical_market_replay(dataset.replay_request).points))

    def test_independent_sha_formulas_and_provenance_revision(self):
        with tempfile.TemporaryDirectory() as folder:
            request = _request(Path(folder))
            report = batch.run_historical_experiment_batch(request)
            suite_payload = batch.report_json_safe(report.suite_manifest)
            suite_fingerprint = suite_payload.pop("batch_run_fingerprint")
            self.assertEqual(suite_fingerprint, _digest(suite_payload))
            run = report.experiment_runs[0]
            expected_run_id = hashlib.sha256((
                f"{suite_fingerprint}|{run.experiment_id}|"
                f"{run.algorithm_version}|{run.config_version}"
            ).encode("utf-8")).hexdigest()
            self.assertEqual(run.experiment_run_id, expected_run_id)
            result_payload = {
                "experiment_id": run.experiment_id,
                "algorithm_version": run.algorithm_version,
                "config_version": run.config_version,
                "config_parameters": run.config_parameters,
                "summaries": run.summaries,
                "diagnostics": run.diagnostics,
            }
            self.assertEqual(run.result_sha256, _digest(result_payload))
            altered = dict(result_payload)
            altered["summaries"] = dict(run.summaries)
            altered["summaries"]["test"] = {"changed": True}
            self.assertNotEqual(run.result_sha256, _digest(altered))
            report_payload = batch.report_json_safe(report)
            report_sha = report_payload.pop("report_sha256")
            self.assertEqual(report_sha, _digest(report_payload))
            changed_run = replace(run, result_sha256="0" * 64)
            changed_report = replace(report, experiment_runs=(changed_run,) + report.experiment_runs[1:])
            changed_payload = batch.report_json_safe(changed_report)
            changed_payload.pop("report_sha256")
            self.assertNotEqual(report_sha, _digest(changed_payload))
            revised = batch.run_historical_experiment_batch(
                replace(request, code_revision="deadbeef"))
            self.assertEqual(suite_fingerprint, revised.suite_manifest.batch_run_fingerprint)
            self.assertNotEqual(report.report_sha256, revised.report_sha256)
            self.assertEqual(revised.code_revision, "deadbeef")
            for key, value in (
                ("dataset_content_sha256", "a" * 64),
                ("replay_run_fingerprint", "b" * 64),
                ("development_end_boundary_time_ms", OUTPUT),
                ("classifier_config_version", "different"),
                ("lifecycle_config_version", "different"),
                ("experiments", []),
            ):
                changed = dict(suite_payload)
                changed[key] = value
                self.assertNotEqual(suite_fingerprint, _digest(changed))

    def test_broken_runner_raises_instead_of_becoming_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            request = _request(Path(folder))
            def broken(*_args, **_kwargs):
                raise ValueError("broken contract")
            suite = ((replace(batch.EXPERIMENT_SUITE_V1[0], runner=broken),)
                     + batch.EXPERIMENT_SUITE_V1[1:])
            with patch.object(batch, "EXPERIMENT_SUITE_V1", suite):
                with self.assertRaisesRegex(ValueError, "broken contract"):
                    batch.run_historical_experiment_batch(request)

    def test_serializer_rejects_nonfinite_and_unknown_objects(self):
        for value in (float("nan"), float("inf"), object(), Decimal("NaN")):
            with self.subTest(value=repr(value)), self.assertRaises((TypeError, ValueError)):
                batch.report_json_safe(value)
        self.assertEqual(batch.report_json_safe(Decimal("1.2300")), "1.2300")
        self.assertEqual(batch.report_json_safe(MappingProxyType({"x": (1, 2)})),
                         {"x": [1, 2]})

    def test_cli_utc_contract_and_symbol_order(self):
        self.assertEqual(batch.parse_utc_cli_timestamp("2026-08-20T00:00:00Z"),
                         int(datetime(2026, 8, 20, tzinfo=timezone.utc).timestamp() * 1000))
        for value in ("2026-08-20", "2026-08-20T00:00:00",
                      "2026-08-20T01:00:00+01:00", "tomorrow",
                      "2026-08-20T00:00:01Z"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                batch.parse_utc_cli_timestamp(value)
        args = batch.build_cli_parser().parse_args(_cli_args("/tmp/archive"))
        self.assertEqual(tuple(args.symbols), ("ETHUSDT", "BTCUSDT"))
        self.assertEqual(args.finalization_grace_ms, 2_000)

    def test_cli_stdout_and_atomic_output_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            report = batch.run_historical_experiment_batch(_request(root))
            content = batch.historical_experiment_report_to_json(report)
            stdout = io.StringIO()
            with (patch.object(batch, "run_historical_experiment_batch", return_value=report),
                  patch("sys.stdout", stdout)):
                self.assertEqual(batch.main(_cli_args(root)), 0)
            self.assertEqual(stdout.getvalue(), content + "\n")
            self.assertEqual(json.loads(stdout.getvalue()), json.loads(content))
            output = root / "report.json"
            with patch.object(batch, "run_historical_experiment_batch", return_value=report):
                self.assertEqual(batch.main(_cli_args(root) + ["--output-json", str(output)]), 0)
                self.assertEqual(output.read_bytes(), (content + "\n").encode("utf-8"))
                self.assertEqual(tuple(root.glob(".report.json.*.tmp")), ())
                with self.assertRaises(SystemExit):
                    batch.main(_cli_args(root) + ["--output-json", str(output)])
                self.assertEqual(batch.main(_cli_args(root) + ["--output-json", str(output),
                                                              "--overwrite"]), 0)
                self.assertEqual(output.read_bytes(), (content + "\n").encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
