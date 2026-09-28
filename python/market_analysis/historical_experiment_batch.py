"""Issue #35 Part 3: one replay, the fixed Issue #75 suite, compact JSON.

This module orchestrates existing research calculations; it never recalculates
movement, classifier, lifecycle, or experiment metrics.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from types import MappingProxyType
from typing import Any, Callable

from .binance_historical_archive import (
    BinanceArchiveBundleManifest, BinanceArchiveDiagnostics,
    BinanceUSDMArchiveRequest, load_binance_usdm_historical_replay_dataset,
)
from .experiments.market_state_common import validate_experiment_points
from .experiments.market_state_ewma import (
    EWMA_ALGORITHM_VERSION, EWMA_CONFIGURATIONS,
    run_market_state_ewma_experiment,
)
from .experiments.market_state_cusum import (
    CUSUM_ALGORITHM_VERSION, CUSUM_CONFIGURATIONS,
    run_market_state_cusum_experiment,
)
from .experiments.market_state_kalman import (
    KALMAN_ALGORITHM_VERSION, KALMAN_CONFIGURATIONS,
    run_market_state_kalman_experiment,
)
from .experiments.market_state_pelt import (
    PELT_ALGORITHM_VERSION, PELT_CONFIGURATIONS,
    run_market_state_pelt_experiment,
)
from .experiments.market_state_bocpd import (
    BOCPD_ALGORITHM_VERSION, BOCPD_CONFIGURATIONS,
    run_market_state_bocpd_experiment,
)
from .experiments.market_state_regression_acceleration import (
    REGRESSION_ACCELERATION_ALGORITHM_VERSION, REGRESSION_CONFIGURATIONS,
    run_market_state_regression_acceleration_experiment,
)
from .experiments.market_state_realized_vol_normalization import (
    REALIZED_VOLATILITY_ALGORITHM_VERSION, REALIZED_VOL_CONFIGURATIONS,
    run_market_state_realized_vol_normalization_experiment,
)
from .experiments.market_state_pca_common_factor import (
    PCA_ALGORITHM_VERSION, PCA_CONFIGURATIONS,
    run_market_state_pca_common_factor_experiment,
)
from .experiments.market_state_correlation_clusters import (
    CORRELATION_ALGORITHM_VERSION, CORRELATION_CONFIGURATIONS,
    run_market_state_correlation_cluster_experiment,
)
from .experiments.market_state_hmm_regimes import (
    HMM_ALGORITHM_VERSION, HMM_CONFIG_V1,
    run_market_state_hmm_experiment,
)
from .historical_replay import (
    DEFAULT_FINALIZATION_GRACE_MS, HistoricalMarketReplayResult,
    HistoricalReplayConfig, HistoricalReplayDiagnostics, HistoricalReplayRunManifest,
    ReplayPartitionPlan, run_historical_market_replay,
    to_market_state_experiment_points,
)
from .market_episode_lifecycle import (
    ALGORITHM_VERSION as LIFECYCLE_ALGORITHM_VERSION,
    MarketEpisodeLifecycleConfig,
)
from .movement_classifier import (
    ALGORITHM_VERSION as CLASSIFIER_ALGORITHM_VERSION,
    MarketClassifierConfig,
)
from .movement_metrics import MarketMovementConfig, MarketUniverseInput


HISTORICAL_EXPERIMENT_BATCH_VERSION = "historical-experiment-batch-v1"
HISTORICAL_EXPERIMENT_REPORT_VERSION = "historical-experiment-report-v1"
REPORT_PARTITIONS = ("development", "validation", "test", "all")
_UTC_CLI = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class HistoricalExperimentDescriptor:
    experiment_id: str
    experiment_name: str
    algorithm_version: str
    config_version: str
    config: Any
    runner: Callable


def _descriptors(experiment_id, experiment_name, algorithm_version,
                 configurations, runner):
    return tuple(HistoricalExperimentDescriptor(
        experiment_id, experiment_name, algorithm_version, config.version,
        config, runner) for config in configurations)


EXPERIMENT_SUITE_V1 = (
    _descriptors("EXP-75-01", "EWMA", EWMA_ALGORITHM_VERSION,
                 EWMA_CONFIGURATIONS, run_market_state_ewma_experiment)
    + _descriptors("EXP-75-02", "CUSUM", CUSUM_ALGORITHM_VERSION,
                   CUSUM_CONFIGURATIONS, run_market_state_cusum_experiment)
    + _descriptors("EXP-75-03", "Kalman", KALMAN_ALGORITHM_VERSION,
                   KALMAN_CONFIGURATIONS, run_market_state_kalman_experiment)
    + _descriptors("EXP-75-04A", "PELT", PELT_ALGORITHM_VERSION,
                   PELT_CONFIGURATIONS, run_market_state_pelt_experiment)
    + _descriptors("EXP-75-04B", "BOCPD", BOCPD_ALGORITHM_VERSION,
                   BOCPD_CONFIGURATIONS, run_market_state_bocpd_experiment)
    + _descriptors("EXP-75-05", "Regression acceleration",
                   REGRESSION_ACCELERATION_ALGORITHM_VERSION,
                   REGRESSION_CONFIGURATIONS,
                   run_market_state_regression_acceleration_experiment)
    + _descriptors("EXP-75-06A", "Realized-volatility normalization",
                   REALIZED_VOLATILITY_ALGORITHM_VERSION,
                   REALIZED_VOL_CONFIGURATIONS,
                   run_market_state_realized_vol_normalization_experiment)
    + _descriptors("EXP-75-07", "PCA common factor", PCA_ALGORITHM_VERSION,
                   PCA_CONFIGURATIONS, run_market_state_pca_common_factor_experiment)
    + _descriptors("EXP-75-08", "Correlation clusters",
                   CORRELATION_ALGORITHM_VERSION, CORRELATION_CONFIGURATIONS,
                   run_market_state_correlation_cluster_experiment)
    + _descriptors("EXP-75-09", "Gaussian HMM", HMM_ALGORITHM_VERSION,
                   (HMM_CONFIG_V1,), run_market_state_hmm_experiment)
)
_EXPECTED_COUNTS = (
    ("EXP-75-01", 3), ("EXP-75-02", 3), ("EXP-75-03", 3),
    ("EXP-75-04A", 3), ("EXP-75-04B", 3), ("EXP-75-05", 3),
    ("EXP-75-06A", 3), ("EXP-75-07", 3), ("EXP-75-08", 3),
    ("EXP-75-09", 1),
)
if (len(EXPERIMENT_SUITE_V1) != 28
        or tuple(Counter(item.experiment_id for item in EXPERIMENT_SUITE_V1).items())
        != _EXPECTED_COUNTS):
    raise RuntimeError("historical experiment suite v1 must have the fixed 28-run order")


@dataclass(frozen=True)
class HistoricalExperimentBatchRequest:
    archive_root: Path
    universe: MarketUniverseInput
    replay_config: HistoricalReplayConfig
    partition_plan: ReplayPartitionPlan
    code_revision: str | None = None

    def __post_init__(self):
        archive = BinanceUSDMArchiveRequest(
            self.archive_root, self.universe, self.replay_config)
        object.__setattr__(self, "archive_root", archive.archive_root)
        if not isinstance(self.partition_plan, ReplayPartitionPlan):
            raise ValueError("partition_plan must be ReplayPartitionPlan")
        if self.replay_config.movement_config != MarketMovementConfig():
            raise ValueError("batch v1 requires canonical default MarketMovementConfig")
        if (self.code_revision is not None
                and (not isinstance(self.code_revision, str) or not self.code_revision)):
            raise ValueError("code_revision must be a nonempty string or None")


@dataclass(frozen=True)
class HistoricalExperimentSuiteEntry:
    experiment_id: str
    algorithm_version: str
    config_version: str
    config_parameters: Mapping[str, Any]


@dataclass(frozen=True)
class HistoricalExperimentSuiteManifest:
    batch_version: str
    report_version: str
    replay_run_fingerprint: str
    dataset_content_sha256: str
    universe_id: str
    universe_version: str
    configured_universe: tuple[str, ...]
    output_start_boundary_time_ms: int
    output_end_boundary_time_ms: int
    finalization_grace_ms: int
    development_end_boundary_time_ms: int
    validation_end_boundary_time_ms: int
    classifier_algorithm_version: str
    classifier_config_version: str
    classifier_config_parameters: Mapping[str, Any]
    lifecycle_algorithm_version: str
    lifecycle_config_version: str
    lifecycle_config_parameters: Mapping[str, Any]
    experiment_stream_sha256: str
    experiments: tuple[HistoricalExperimentSuiteEntry, ...]
    batch_run_fingerprint: str


@dataclass(frozen=True)
class HistoricalExperimentRunReport:
    experiment_run_id: str
    experiment_id: str
    experiment_name: str
    algorithm_version: str
    config_version: str
    status: str
    config_parameters: Mapping[str, Any]
    summaries: Mapping[str, Any]
    diagnostics: Mapping[str, Any]
    result_sha256: str


@dataclass(frozen=True)
class HistoricalExperimentBatchReport:
    suite_manifest: HistoricalExperimentSuiteManifest
    archive_manifest: BinanceArchiveBundleManifest
    archive_diagnostics: BinanceArchiveDiagnostics
    replay_manifest: HistoricalReplayRunManifest
    replay_diagnostics: HistoricalReplayDiagnostics
    experiment_run_count: int
    experiment_runs: tuple[HistoricalExperimentRunReport, ...]
    code_revision: str | None
    report_sha256: str


def report_json_safe(value: Any) -> Any:
    """Convert supported immutable report values to deterministic JSON data."""
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: report_json_safe(getattr(value, field.name))
                for field in fields(value)}
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("report mapping keys must be strings")
        return {key: report_json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [report_json_safe(item) for item in value]
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("nonfinite Decimal in report")
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("nonfinite float in report")
        return value
    raise TypeError(f"unsupported report value type: {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(report_json_safe(value), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def experiment_stream_sha256(replay_result: HistoricalMarketReplayResult,
                             experiment_points: tuple,
                             partition_plan: ReplayPartitionPlan) -> str:
    replay_points = replay_result.points
    if len(replay_points) != len(experiment_points):
        raise ValueError("replay/export point counts differ")
    paired = []
    for replay_point, experiment_point in zip(replay_points, experiment_points):
        if (replay_point.evaluation_boundary_time_ms
                != experiment_point.movement_evaluation.evaluation_boundary_time_ms):
            raise ValueError("replay/export evaluation boundaries differ")
        paired.append([replay_point.point_id, experiment_point.partition])
    return _sha256({
        "replay_run_fingerprint": replay_result.manifest.run_fingerprint,
        "development_end_boundary_time_ms": partition_plan.development_end_boundary_time_ms,
        "validation_end_boundary_time_ms": partition_plan.validation_end_boundary_time_ms,
        "points": paired,
    })


def _parameters(config) -> Mapping[str, Any]:
    if not is_dataclass(config) or isinstance(config, type):
        raise ValueError("registered experiment config must be a dataclass")
    return MappingProxyType({field.name: getattr(config, field.name)
                             for field in fields(config)})


def _suite_manifest(replay_result, archive_dataset, partition_plan,
                    experiment_points, classifier_config, lifecycle_config):
    replay = replay_result.manifest
    entries = tuple(HistoricalExperimentSuiteEntry(
        descriptor.experiment_id, descriptor.algorithm_version,
        descriptor.config_version, _parameters(descriptor.config))
        for descriptor in EXPERIMENT_SUITE_V1)
    manifest = HistoricalExperimentSuiteManifest(
        HISTORICAL_EXPERIMENT_BATCH_VERSION,
        HISTORICAL_EXPERIMENT_REPORT_VERSION,
        replay.run_fingerprint, archive_dataset.archive_manifest.content_sha256,
        replay.universe_id, replay.universe_version, replay.configured_universe,
        replay.output_start_boundary_time_ms, replay.output_end_boundary_time_ms,
        replay.finalization_grace_ms,
        partition_plan.development_end_boundary_time_ms,
        partition_plan.validation_end_boundary_time_ms,
        CLASSIFIER_ALGORITHM_VERSION, classifier_config.version,
        _parameters(classifier_config),
        LIFECYCLE_ALGORITHM_VERSION, lifecycle_config.version,
        _parameters(lifecycle_config),
        experiment_stream_sha256(replay_result, experiment_points, partition_plan),
        entries, "",
    )
    return replace(manifest, batch_run_fingerprint=_sha256(
        {key: value for key, value in report_json_safe(manifest).items()
         if key != "batch_run_fingerprint"}))


def _run_report(descriptor, result, batch_run_fingerprint):
    summaries = result.summaries
    if set(summaries) != set(REPORT_PARTITIONS):
        raise ValueError(f"{descriptor.experiment_id} missing required partition summaries")
    native_summaries = MappingProxyType({name: summaries[name] for name in REPORT_PARTITIONS})
    diagnostics = (MappingProxyType({
        "training_diagnostics": result.training_diagnostics,
        "model_sha256": (result.model_artifact.model_sha256
                         if result.model_artifact is not None else None),
    }) if descriptor.experiment_id == "EXP-75-09" else MappingProxyType({}))
    parameters = _parameters(descriptor.config)
    run_id = hashlib.sha256((
        f"{batch_run_fingerprint}|{descriptor.experiment_id}|"
        f"{descriptor.algorithm_version}|{descriptor.config_version}"
    ).encode("utf-8")).hexdigest()
    result_identity = {
        "experiment_id": descriptor.experiment_id,
        "algorithm_version": descriptor.algorithm_version,
        "config_version": descriptor.config_version,
        "config_parameters": parameters,
        "summaries": native_summaries,
        "diagnostics": diagnostics,
    }
    return HistoricalExperimentRunReport(
        run_id, descriptor.experiment_id, descriptor.experiment_name,
        descriptor.algorithm_version, descriptor.config_version, "COMPLETED",
        parameters, native_summaries, diagnostics, _sha256(result_identity))


def run_historical_experiment_batch(
    request: HistoricalExperimentBatchRequest,
) -> HistoricalExperimentBatchReport:
    """Load once, replay once, export once, then run all 28 configs in order."""
    if not isinstance(request, HistoricalExperimentBatchRequest):
        raise ValueError("request must be HistoricalExperimentBatchRequest")
    classifier_config = MarketClassifierConfig()
    lifecycle_config = MarketEpisodeLifecycleConfig()
    archive_dataset = load_binance_usdm_historical_replay_dataset(
        BinanceUSDMArchiveRequest(request.archive_root, request.universe,
                                  request.replay_config))
    replay_result = run_historical_market_replay(archive_dataset.replay_request)
    experiment_points = to_market_state_experiment_points(
        replay_result, request.partition_plan)
    validate_experiment_points(experiment_points)
    manifest = _suite_manifest(replay_result, archive_dataset,
                               request.partition_plan, experiment_points,
                               classifier_config, lifecycle_config)
    run_reports = []
    for descriptor in EXPERIMENT_SUITE_V1:
        result = descriptor.runner(
            experiment_points, descriptor.config,
            classifier_config=classifier_config,
            lifecycle_config=lifecycle_config,
        )
        run_reports.append(_run_report(descriptor, result,
                                       manifest.batch_run_fingerprint))
        del result
    report = HistoricalExperimentBatchReport(
        manifest, archive_dataset.archive_manifest, archive_dataset.diagnostics,
        replay_result.manifest, replay_result.diagnostics,
        len(run_reports), tuple(run_reports), request.code_revision, "")
    return replace(report, report_sha256=_sha256(
        {key: value for key, value in report_json_safe(report).items()
         if key != "report_sha256"}))


def historical_experiment_report_to_json(report: HistoricalExperimentBatchReport) -> str:
    if not isinstance(report, HistoricalExperimentBatchReport):
        raise ValueError("report must be HistoricalExperimentBatchReport")
    return _canonical_json(report)


def parse_utc_cli_timestamp(value: str) -> int:
    if not isinstance(value, str) or _UTC_CLI.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("timestamp must be YYYY-MM-DDTHH:MM:SSZ")
    try:
        instant = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("invalid UTC timestamp") from exc
    milliseconds = (instant - _EPOCH) // timedelta(milliseconds=1)
    if milliseconds < 0 or milliseconds % 5_000:
        raise argparse.ArgumentTypeError("timestamp must be nonnegative and five-second aligned")
    return milliseconds


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run fixed Issue #75 historical suite")
    parser.add_argument("--archive-root", required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--universe-id", required=True)
    parser.add_argument("--universe-version", required=True)
    parser.add_argument("--start", required=True, type=parse_utc_cli_timestamp)
    parser.add_argument("--end", required=True, type=parse_utc_cli_timestamp)
    parser.add_argument("--development-end", required=True, type=parse_utc_cli_timestamp)
    parser.add_argument("--validation-end", required=True, type=parse_utc_cli_timestamp)
    parser.add_argument("--finalization-grace-ms", type=int,
                        default=DEFAULT_FINALIZATION_GRACE_MS)
    parser.add_argument("--code-revision")
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def _write_report(path: Path, content: str, *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"report already exists: {path}; use --overwrite")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".tmp",
            dir=path.parent, delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write((content + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists() and not overwrite:
            raise FileExistsError(f"report already exists: {path}; use --overwrite")
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    if args.overwrite and args.output_json is None:
        parser.error("--overwrite requires --output-json")
    if args.output_json is not None and args.output_json.exists() and not args.overwrite:
        parser.error("output file already exists; pass --overwrite to replace it")
    try:
        request = HistoricalExperimentBatchRequest(
            args.archive_root,
            MarketUniverseInput(args.universe_id, args.universe_version,
                                tuple(args.symbols)),
            HistoricalReplayConfig(args.start, args.end,
                                   args.finalization_grace_ms,
                                   MarketMovementConfig()),
            ReplayPartitionPlan(args.development_end, args.validation_end),
            args.code_revision,
        )
        report = run_historical_experiment_batch(request)
        content = historical_experiment_report_to_json(report)
        if args.output_json is None:
            sys.stdout.write(content + "\n")
        else:
            _write_report(args.output_json, content, overwrite=args.overwrite)
    except (ValueError, TypeError, ArithmeticError, OSError) as exc:
        print(f"historical experiment batch: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
