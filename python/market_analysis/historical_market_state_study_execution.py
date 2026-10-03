"""Part-B frozen study coverage, one-replay execution, and outcome artifacts.

This module consumes the already frozen #123 selection. It never selects or
replaces dates and contains no inferential, ranking, promotion, or trading
logic. Supplementary acquisition exists only behind explicit ``coverage`` CLI
flags; execution is local-only and requires a matching frozen coverage report.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile
import time
from types import MappingProxyType, SimpleNamespace
from typing import Any, Mapping

from .binance_historical_archive import (
    ARCHIVE_FIRST_SEEN_POLICY, BINANCE_ARCHIVE_DATASET_ID,
    BINANCE_ARCHIVE_DATASET_VERSION,
    BinanceArchiveCoverageError, BinanceHistoricalCoreArchiveEvidence,
    BinanceHistoricalReplayDataset, BinanceBoundedHistoricalReplayDataset,
    BinanceUSDMArchiveRequest,
    _KLINE_HEADER, _archive_rows, _checksum,
    _kline_row, daily_aggtrades_relative_path, daily_kline_relative_path,
    load_binance_usdm_historical_replay_dataset, required_aggtrade_dates,
    load_binance_usdm_bounded_historical_replay_dataset,
    required_kline_dates, verify_binance_usdm_historical_core_archives,
)
from .binance_historical_download import (
    BinanceHistoricalDownloadRequest, acquire_binance_usdm_historical_archive_files,
)
from .experiments.market_state_common import (
    MarketStateExperimentPoint, advance_canonical_branch,
    validate_experiment_points,
)
from .experiments.market_state_atr_normalization import (
    ATR_ALGORITHM_VERSION, ATR_CONFIGURATIONS,
    run_market_state_atr_normalization_suite,
)
from .experiments.market_state_hmm_regimes import (
    HMM_ALGORITHM_VERSION, HMM_CONFIG_V1, HMMDevelopmentTrainingBlock,
    HMMFeatureRow, GaussianHMMModelArtifact, HMMFilterState,
    advance_hmm_regime_filter, extract_hmm_development_training_block,
    train_hmm_regime_model_from_blocks,
)
from .historical_experiment_batch import (
    EXPERIMENT_SUITE_V1,
)
from .historical_market_state_study_json import (
    canonical_study_json, study_json_safe as report_json_safe,
)
from .historical_market_state_candidate_evidence import (
    CANDIDATE_EVIDENCE_VERSION,
    HistoricalStudyCandidateEvidence, adapt_candidate_result, adapt_v1_evidence,
)
from .historical_market_state_forward_outcomes import (
    FORWARD_NUMERIC_POLICY, FORWARD_OUTCOMES_VERSION, HORIZONS_MINUTES,
    TradePriceForwardEvidence,
    evaluate_continuous_grids, evaluate_event_outcomes,
    evaluate_forward_outcome, evaluate_v1_state_path,
)
from .historical_market_state_study import (
    ORDERED_SYMBOLS, STUDY_VERSION, HistoricalMarketStateStudyManifest,
    HistoricalStudyPeriod, parse_historical_market_state_study_manifest_json,
    study_replay_config, study_universe,
)
from .historical_mark_price_evidence import (
    MARK_PRICE_EVIDENCE_VERSION, MARK_PRICE_EVIDENCE_SCHEMA_VERSION,
    MARK_PRICE_SOURCE, MARK_PRICE_TYPE, MARK_PRICE_INTERVAL,
    MARK_PRICE_AVAILABILITY_BASIS,
    load_binance_usdm_mark_price_evidence,
)
from .historical_open_interest_evidence import (
    EVIDENCE_VERSION as OI_EVIDENCE_VERSION,
    SCHEMA_VERSION as OI_SCHEMA_VERSION,
    SOURCE as OI_SOURCE, AVAILABILITY_BASIS as OI_AVAILABILITY_BASIS,
    load_binance_usdm_open_interest_evidence,
)
from .historical_funding_evidence import (
    EVIDENCE_VERSION as FUNDING_EVIDENCE_VERSION,
    SCHEMA_VERSION as FUNDING_SCHEMA_VERSION,
    SOURCE as FUNDING_SOURCE, AVAILABILITY_BASIS as FUNDING_AVAILABILITY_BASIS,
    load_binance_usdm_funding_evidence,
)
from .historical_liquidation_evidence import (
    EVIDENCE_VERSION as LIQ_EVIDENCE_VERSION,
    SCHEMA_VERSION as LIQ_SCHEMA_VERSION,
    SOURCE as LIQ_SOURCE, AVAILABILITY_BASIS as LIQ_AVAILABILITY_BASIS,
    load_tardis_liquidation_evidence,
)
from .historical_mark_trade_extension import (
    MARK_TRADE_ALGORITHM_VERSION, MARK_TRADE_CONFIG_VERSION,
    HistoricalMarkTradeExtensionPrepared, _partition_summaries as _mark_summaries,
    build_historical_study_mark_trade_points,
)
from .historical_open_interest_extension import (
    ALGORITHM_VERSION as OI_ALGORITHM_VERSION, CONFIG_VERSION as OI_CONFIG_VERSION,
    HistoricalOpenInterestExtensionPrepared,
    _partition_summaries as _oi_summaries,
    build_historical_study_open_interest_points,
)
from .historical_funding_extension import (
    ALGORITHM_VERSION as FUNDING_ALGORITHM_VERSION,
    CONFIG_VERSION as FUNDING_CONFIG_VERSION,
    HistoricalFundingExtensionPrepared,
    _partition_summaries as _funding_summaries,
    build_historical_study_funding_points,
)
from .historical_liquidation_extension import (
    ALGORITHM_VERSION as LIQ_ALGORITHM_VERSION, CONFIG_VERSION as LIQ_CONFIG_VERSION,
    HistoricalLiquidationExtensionPrepared,
    _partition_summaries as _liquidation_summaries,
    build_historical_study_liquidation_points,
)
from .historical_replay import (
    CompactStudyReplay, HistoricalMarketReplayResult, HistoricalReplayCheckpoint,
    historical_replay_run_manifest, run_bounded_historical_market_replay,
    run_historical_market_replay,
)
from .historical_replay_runtime import ReplayCheckpointStore
from .historical_study_runtime import (
    create_study_point_stream, current_runtime_implementation_revision,
    run_stage, run_stage_batch, run_stage_batches, stage_dependencies, input_descriptor,
)
from .historical_taker_flow_extension import (
    TAKER_FLOW_ALGORITHM_VERSION, TAKER_FLOW_CONFIG_VERSION,
    _partition_summaries as _taker_summaries,
    build_historical_study_taker_flow_points,
)
from .historical_ohlc_evidence import CompletedTradeOHLCCandle
from .historical_taker_flow_evidence import (
    TAKER_FLOW_AVAILABILITY_BASIS, TAKER_FLOW_BUCKET_RULE,
    TAKER_FLOW_EVIDENCE_SCHEMA_VERSION, TAKER_FLOW_SIDE_MAPPING,
)
from .movement_classifier import MarketClassifierConfig, ALGORITHM_VERSION as V1_CLASSIFIER_ALGORITHM_VERSION
from .market_episode_lifecycle import MarketEpisodeLifecycleConfig
from .movement_history import MINUTE_MS


EXECUTION_VERSION = "historical-market-state-execution-v1"
EXTENSION_COVERAGE_VERSION = "historical-market-state-extension-coverage-v2"
HMM_DEVELOPMENT_MODEL_VERSION = "historical-market-state-hmm-development-model-v1"
TOOL_CONFIG_VERSION = "historical-market-state-study-part-b-tool-v1"
PERIOD_REPORT_SCHEMA_VERSION = "historical-market-state-study-period-report-v2"
EVENT_TIME_V1_CONTEXT_VERSION = "historical-market-state-event-time-v1-context-v1"
BOCPD_ONSET_EVIDENCE_VERSION = "historical-market-state-bocpd-onset-evidence-v1"
EXECUTION_INDEX_VERSION = "historical-market-state-execution-index-v1"
RUNTIME_REPORT_VERSION = "historical-market-state-runtime-v1"
STUDY_PROGRESS_VERSION = "historical-market-state-progress-v1"
HMM_MODEL_FILENAME = "historical-market-state-study-v1-hmm-model.json"
EXECUTION_INDEX_FILENAME = "historical-market-state-study-v1-execution-index.json"
PERIOD_DIRECTORY = "periods"

RUNTIME_MEASURED_FIELDS = (
    "core_archive_load_seconds",
    "canonical_replay_seconds",
    "v1_preparation_seconds",
    "supplementary_source_load_seconds",
    "forward_label_evidence_seconds",
    "forward_outcomes_seconds",
    "core_experiment_seconds",
    "bocpd_seconds",
    "atr_06b_seconds",
    "taker_flow_seconds",
    "mark_trade_seconds",
    "open_interest_seconds",
    "funding_seconds",
    "liquidation_seconds",
    "period_total_seconds",
    "period_artifact_write_seconds",
)
RUNTIME_EXTENSION_FIELDS = (
    "atr_06b_seconds", "taker_flow_seconds", "mark_trade_seconds",
    "open_interest_seconds", "funding_seconds", "liquidation_seconds",
)
RUNTIME_REPORT_TIMING_FIELDS = (*RUNTIME_MEASURED_FIELDS, "all_extensions_seconds")


@dataclass
class StudyPeriodRuntimeMetrics:
    """Optional operational elapsed-time collector, kept outside study artifacts."""

    elapsed_ns_by_field: dict[str, int] = field(default_factory=dict)

    @contextmanager
    def measure(self, field_name: str):
        if field_name not in RUNTIME_MEASURED_FIELDS:
            raise ValueError(f"unknown study runtime timing field: {field_name}")
        started = time.perf_counter_ns()
        try:
            yield
        finally:
            self.record_elapsed_ns(field_name, time.perf_counter_ns() - started)

    def record_elapsed_ns(self, field_name: str, elapsed_ns: int) -> None:
        if field_name not in RUNTIME_MEASURED_FIELDS or elapsed_ns < 0:
            raise ValueError("invalid study runtime measurement")
        self.elapsed_ns_by_field[field_name] = (
            self.elapsed_ns_by_field.get(field_name, 0) + elapsed_ns)

    def report_timings(self) -> dict[str, float]:
        result = {
            name: self.elapsed_ns_by_field.get(name, 0) / 1_000_000_000
            for name in RUNTIME_MEASURED_FIELDS
        }
        result["all_extensions_seconds"] = (
            result["supplementary_source_load_seconds"]
            + sum(result[name] for name in RUNTIME_EXTENSION_FIELDS))
        return result


def _runtime_measure(metrics: StudyPeriodRuntimeMetrics | None,
                     field_name: str | None):
    return (metrics.measure(field_name)
            if metrics is not None and field_name is not None else nullcontext())

SOURCE_IDENTITIES = {
    "core": {
        "dataset_id": BINANCE_ARCHIVE_DATASET_ID,
        "dataset_version": BINANCE_ARCHIVE_DATASET_VERSION,
        "first_seen_policy": ARCHIVE_FIRST_SEEN_POLICY,
        "price_type": "trade",
        "interval": "1m OHLC for candidate evidence and labels",
    },
    "fixed_experiment_suite_v1": tuple({
        "experiment_id": item.experiment_id,
        "algorithm_version": item.algorithm_version,
        "config_version": item.config_version,
    } for item in EXPERIMENT_SUITE_V1),
    "atr": {"algorithm_version": ATR_ALGORITHM_VERSION,
            "config_versions": tuple(item.version for item in ATR_CONFIGURATIONS)},
    "taker_flow": {"algorithm_version": TAKER_FLOW_ALGORITHM_VERSION,
                   "config_version": TAKER_FLOW_CONFIG_VERSION,
                   "schema_version": TAKER_FLOW_EVIDENCE_SCHEMA_VERSION,
                   "bucket_rule": TAKER_FLOW_BUCKET_RULE,
                   "side_mapping": TAKER_FLOW_SIDE_MAPPING,
                   "availability_basis": TAKER_FLOW_AVAILABILITY_BASIS},
    "mark_trade": {"source": MARK_PRICE_SOURCE,
                    "type": MARK_PRICE_TYPE,
                    "interval": MARK_PRICE_INTERVAL,
                    "availability_basis": MARK_PRICE_AVAILABILITY_BASIS,
                    "evidence_version": MARK_PRICE_EVIDENCE_VERSION,
                    "schema_version": MARK_PRICE_EVIDENCE_SCHEMA_VERSION},
    "open_interest": {"evidence_version": OI_EVIDENCE_VERSION,
                       "schema_version": OI_SCHEMA_VERSION,
                       "source": OI_SOURCE,
                       "availability_basis": OI_AVAILABILITY_BASIS},
    "funding": {"evidence_version": FUNDING_EVIDENCE_VERSION,
                 "schema_version": FUNDING_SCHEMA_VERSION,
                 "source": FUNDING_SOURCE,
                 "availability_basis": FUNDING_AVAILABILITY_BASIS},
    "liquidation": {"evidence_version": LIQ_EVIDENCE_VERSION,
                    "schema_version": LIQ_SCHEMA_VERSION,
                    "source": LIQ_SOURCE,
                    "availability_basis": LIQ_AVAILABILITY_BASIS},
}


class StudyArtifactConflictError(ValueError):
    """An existing finalized scientific artifact conflicts with this request."""


@dataclass(frozen=True)
class PreparedHistoricalMarketStatePeriod:
    """Shared immutable period inputs and the single canonical V1 branch."""

    period: HistoricalStudyPeriod
    archive_dataset: BinanceHistoricalReplayDataset | BinanceBoundedHistoricalReplayDataset
    canonical_replay_result: Any
    experiment_points: tuple[MarketStateExperimentPoint, ...]
    canonical_v1_branch_by_boundary: Mapping[int, tuple]
    study_manifest_sha256: str
    core_eligibility_sha256: str
    code_revision: str

    def __post_init__(self):
        points = tuple(self.experiment_points)
        branch = dict(self.canonical_v1_branch_by_boundary)
        if (not isinstance(self.period, HistoricalStudyPeriod)
                or not isinstance(self.archive_dataset, (BinanceHistoricalReplayDataset,
                                                         BinanceBoundedHistoricalReplayDataset))
                or self.canonical_replay_result is None
                or not points or any(not isinstance(item, MarketStateExperimentPoint)
                                     for item in points)
                or not isinstance(self.study_manifest_sha256, str)
                or len(self.study_manifest_sha256) != 64
                or not isinstance(self.core_eligibility_sha256, str)
                or len(self.core_eligibility_sha256) != 64
                or not isinstance(self.code_revision, str) or not self.code_revision
                or len(points) != len(self.canonical_replay_result.points)
                or len(branch) != len(self.canonical_replay_result.points)):
            raise ValueError("invalid prepared frozen market-state study period")
        report_manifest = self.canonical_replay_result.manifest
        if (report_manifest.output_start_boundary_time_ms
                != self.period.start_boundary_time_ms
                or report_manifest.output_end_boundary_time_ms
                != self.period.end_boundary_time_ms):
            raise ValueError("prepared replay boundaries differ from the frozen study period")
        if (self.archive_dataset.archive_manifest.content_sha256
                != report_manifest.dataset_content_sha256
                or self.archive_dataset.archive_manifest.dataset_id
                != report_manifest.dataset_id
                or self.archive_dataset.archive_manifest.dataset_version
                != report_manifest.dataset_version):
            raise ValueError("prepared period mixes different verified replay datasets")
        expected_boundaries = tuple(
            item.evaluation_boundary_time_ms
            for item in self.canonical_replay_result.points)
        if tuple(branch) != expected_boundaries:
            raise ValueError("prepared V1 branch does not match canonical replay order")
        for replay_point, point in zip(self.canonical_replay_result.points, points):
            if (point.movement_evaluation is not replay_point.movement_evaluation
                    or point.source_time_evidence != replay_point.source_time_evidence
                    or point.partition != self.period.phase):
                raise ValueError("prepared period does not preserve the exact uniform-phase stream")
        object.__setattr__(self, "experiment_points", points)
        object.__setattr__(self, "canonical_v1_branch_by_boundary",
                           MappingProxyType(branch))


def _canonical(value) -> str:
    from .historical_market_state_study_json import iter_canonical_study_json
    return "".join(iter_canonical_study_json(value))


def _digest(value) -> str:
    from .historical_market_state_study_json import canonical_study_sha256
    return canonical_study_sha256(value)


def _artifact_json(payload: Mapping[str, Any], hash_field: str) -> str:
    value = dict(payload)
    value.pop(hash_field, None)
    finished = dict(payload)
    finished[hash_field] = _digest(value)
    return _canonical(finished)


def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"invalid JSON numeric constant: {value}")


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"),
                          object_pairs_hook=_strict_pairs,
                          parse_constant=_reject_constant)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"invalid JSON artifact: {path.name}") from exc


def _write_atomic_new(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise StudyArtifactConflictError(f"finalized artifact already exists: {path.name}")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{path.name}.",
                                         suffix=".tmp", dir=path.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write((content + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            raise StudyArtifactConflictError(
                f"finalized artifact appeared during write: {path.name}")
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _replace_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{path.name}.",
                                         suffix=".tmp", dir=path.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write((content + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _manifest_periods(manifest: HistoricalMarketStateStudyManifest):
    if not isinstance(manifest, HistoricalMarketStateStudyManifest):
        raise ValueError("execution requires the parsed frozen study manifest")
    if (len(manifest.selected_periods) != 30
            or tuple(item.phase for item in manifest.selected_periods)
            != ("development",) * 10 + ("validation",) * 8 + ("test",) * 12):
        raise ValueError("study manifest does not contain the frozen 10/8/12 period split")
    return manifest.selected_periods


def select_execution_periods(manifest, phase=None, period_limit=None, *, allow_test=False,
                             period_index=None):
    """Resolve only frozen phase members; the test phase has an extra guard."""
    if period_index is not None and phase is None:
        raise ValueError("period_index requires an explicit phase")
    phase = "development" if phase is None else phase
    if phase not in ("development", "validation", "test"):
        raise ValueError("phase must be development, validation, or test")
    if phase == "test" and allow_test is not True:
        raise ValueError("test execution requires explicit allow_test=True")
    if phase != "test" and allow_test:
        raise ValueError("allow_test is meaningful only with the explicit test phase")
    if period_limit is not None and (type(period_limit) is not int or period_limit <= 0):
        raise ValueError("period_limit must be a positive integer")
    periods = tuple(item for item in _manifest_periods(manifest) if item.phase == phase)
    if period_index is not None:
        if type(period_index) is not int or period_limit is not None:
            raise ValueError("period_index must be an integer and cannot combine with period_limit")
        selected = tuple(item for item in periods if item.study_period_index == period_index)
        if len(selected) != 1:
            raise ValueError("period_index is not an exact member of the selected phase")
        return selected
    return periods if period_limit is None else periods[:period_limit]


def load_study_manifest(path: Path | str) -> HistoricalMarketStateStudyManifest:
    path = Path(path)
    return parse_historical_market_state_study_manifest_json(
        path.read_text(encoding="utf-8"))


def _compatible_coverage_source_identities(supplied):
    """Accept only current coverage or the exact pre-correction source map.

    This compatibility is for frozen source facts, never derived calculations.
    Build independent comparison dictionaries; leave the artifact/hash intact.
    """
    legacy = {**SOURCE_IDENTITIES, "taker_flow": {
        **SOURCE_IDENTITIES["taker_flow"],
        "algorithm_version": "taker-buy-sell-imbalance-v1"}}
    return _canonical(supplied) in (_canonical(SOURCE_IDENTITIES), _canonical(legacy))


def load_and_validate_coverage(path: Path | str,
                               manifest: HistoricalMarketStateStudyManifest,
                               code_revision: str | None = None):
    artifact = _read_json(Path(path))
    if not isinstance(artifact, dict):
        raise ValueError("coverage manifest must be a JSON object")
    expected_dates = [{"study_period_index": item.study_period_index,
                       "utc_date": item.utc_date.isoformat(), "phase": item.phase}
                      for item in _manifest_periods(manifest)]
    if (artifact.get("coverage_version") != EXTENSION_COVERAGE_VERSION
            or artifact.get("study_version") != STUDY_VERSION
            or artifact.get("study_manifest_sha256") != manifest.manifest_sha256
            or artifact.get("ordered_periods") != expected_dates
            or not _compatible_coverage_source_identities(artifact.get("source_identities"))
            or artifact.get("tool_config_version") != TOOL_CONFIG_VERSION
            or not isinstance(artifact.get("code_revision"), str)
            or not artifact["code_revision"]
            or (code_revision is not None
                and artifact.get("code_revision") != code_revision)):
        raise ValueError("coverage manifest does not match frozen study and source identities")
    period_reports = artifact.get("periods")
    if (not isinstance(period_reports, list) or len(period_reports) != 30
            or any(not isinstance(item, dict)
                   or item.get("study_period_index") != expected_dates[index]["study_period_index"]
                   or item.get("utc_date") != expected_dates[index]["utc_date"]
                   or item.get("phase") != expected_dates[index]["phase"]
                   for index, item in enumerate(period_reports))):
        raise ValueError("coverage manifest does not contain the frozen ordered period set")
    supplied = artifact.get("coverage_manifest_sha256")
    if supplied != _digest({key: value for key, value in artifact.items()
                            if key != "coverage_manifest_sha256"}):
        raise ValueError("coverage manifest SHA-256 mismatch")
    return artifact


def _package_name(package):
    for field_name in ("relative_archive_path", "relative_path", "relative_checksum_path"):
        value = getattr(package, field_name, None)
        if isinstance(value, str) and value:
            return value
    return ""


_SOURCE_PACKAGE_STATUSES = {
    "mark_trade": {
        "VERIFIED_COMPLETE", "MALFORMED_ROW", "DUPLICATE_MINUTE",
        "OFF_GRID_MINUTE", "MISSING_VALID_MINUTE",
    },
    "open_interest": {
        "VERIFIED", "MALFORMED_ROW", "DUPLICATE_SOURCE_TIMESTAMP",
        "OFF_GRID_TIMESTAMP",
    },
    "funding": {
        "VERIFIED", "MALFORMED_ROW", "DUPLICATE_SOURCE_TIMESTAMP",
        "OFF_GRID_TIMESTAMP",
    },
    # This provider exposes one shared receipt-day package for all symbols.
    # A malformed day is rejected wholesale by its native evidence loader.
    "liquidation": {"ARCHIVE_DAY_AVAILABLE"},
}
_SOURCE_PARTIAL_STATUSES = {
    "mark_trade": {"MALFORMED_ROW", "DUPLICATE_MINUTE", "OFF_GRID_MINUTE",
                   "MISSING_VALID_MINUTE"},
    "open_interest": {"MALFORMED_ROW", "DUPLICATE_SOURCE_TIMESTAMP",
                      "OFF_GRID_TIMESTAMP"},
    "funding": {"MALFORMED_ROW", "DUPLICATE_SOURCE_TIMESTAMP",
                "OFF_GRID_TIMESTAMP"},
    "liquidation": set(),
}
_NO_OBSERVATION_STATES = {
    "mark_trade": "NO_OBSERVED_MARK_PRICE",
    "open_interest": "NO_OBSERVED_OPEN_INTEREST",
    "funding": "NO_OBSERVED_FUNDING",
    "liquidation": "NO_OBSERVED_LIQUIDATION",
}


def _package_evidence_row_count(package, source_kind: str, evidence_rows):
    symbol = getattr(package, "symbol", None)
    path = (getattr(package, "relative_archive_path", None)
            or getattr(package, "relative_path", None))
    if source_kind == "mark_trade":
        day = getattr(package, "utc_date", None)
        if isinstance(day, str):
            try:
                day = date.fromisoformat(day)
            except ValueError:
                return 0
        return sum(
            getattr(row, "symbol", None) == symbol
            and datetime.fromtimestamp(row.open_time_ms / 1000, timezone.utc).date() == day
            for row in evidence_rows
            if type(getattr(row, "open_time_ms", None)) is int)
    if path is None:
        return 0
    return sum((source_kind == "liquidation"
                or getattr(row, "symbol", None) == symbol)
               and getattr(row, "package_relative_path", None) == path
               for row in evidence_rows)


def _package_summary(package, source_kind: str, evidence_rows=()):
    if source_kind not in _SOURCE_PACKAGE_STATUSES:
        raise ValueError(f"unknown supplementary source kind: {source_kind}")
    name = _package_name(package)
    if not name or PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts:
        raise ValueError("source package identity must be a safe relative name")
    statuses = tuple(getattr(package, "statuses", ()) or ())
    malformed = tuple(getattr(package, "malformed_rows", ()) or ())
    duplicates = tuple(getattr(package, "duplicate_timestamps_ms", ()) or ())
    off_grid = tuple(getattr(package, "off_grid_timestamps_ms", ()) or ())
    missing = tuple(getattr(package, "missing_minute_ranges", ())
                    or getattr(package, "missing_ranges", ()) or ())
    checksum_verified = getattr(package, "checksum_verified", None)
    status = getattr(package, "status", "UNKNOWN")
    archive_present = getattr(package, "archive_present", False)
    native_statuses = {status, *(item for item in statuses if isinstance(item, str))}
    known_statuses = _SOURCE_PACKAGE_STATUSES[source_kind]
    if source_kind == "liquidation":
        digest = (getattr(package, "compressed_sha256", None)
                  or getattr(package, "sha256", None))
        digest_valid = (isinstance(digest, str) and len(digest) == 64
                        and all(character in "0123456789abcdef" for character in digest))
        valid = (status == "ARCHIVE_DAY_AVAILABLE" and archive_present is True
                 and digest_valid and not malformed)
    else:
        digest = (getattr(package, "archive_sha256", None)
                  or getattr(package, "sha256", None)
                  or getattr(package, "compressed_sha256", None))
        digest_valid = (isinstance(digest, str) and len(digest) == 64
                        and all(character in "0123456789abcdef" for character in digest))
        valid = (checksum_verified is True and digest_valid
                 and status in known_statuses
                 and native_statuses <= known_statuses)
    usable_row_count = _package_evidence_row_count(package, source_kind, evidence_rows)
    if (valid and source_kind != "liquidation"
            and native_statuses & _SOURCE_PARTIAL_STATUSES[source_kind]
            and usable_row_count == 0):
        valid = False
    issue_count = len(malformed) + len(duplicates) + len(off_grid) + len(missing)
    if valid and native_statuses & _SOURCE_PARTIAL_STATUSES[source_kind] and not issue_count:
        issue_count = 1
    return {
        "package_name": name,
        "symbol": getattr(package, "symbol", None),
        "utc_date": (getattr(package, "utc_date", None)
                     or getattr(package, "utc_day", None)),
        "status": status,
        "statuses": statuses,
        "archive_present": bool(archive_present),
        "checksum_present": getattr(package, "checksum_present", None),
        "checksum_verified": checksum_verified,
        "package_valid": valid,
        "sha256": digest,
        "row_count": getattr(package, "row_count", None),
        "valid_row_count": getattr(package, "valid_row_count", None),
        "usable_evidence_row_count": usable_row_count,
        "malformed_row_count": len(malformed),
        "duplicate_timestamp_count": len(duplicates),
        "off_grid_timestamp_count": len(off_grid),
        "missing_range_count": len(missing),
        "issue_count": issue_count,
        "acquisition_error_present": bool(
            getattr(package, "acquisition_error", None) or getattr(package, "error", None)),
    }


def _source_coverage_summary(evidence, error_type: str | None = None, *,
                             source_kind: str):
    if source_kind not in _SOURCE_PACKAGE_STATUSES:
        raise ValueError(f"unknown supplementary source kind: {source_kind}")
    if evidence is None:
        return {"coverage_state": "UNAVAILABLE", "exception_type": error_type,
                "expected_package_count": None, "checksum_verified_package_count": 0,
                "packages": (), "per_symbol": ()}
    rows = tuple(getattr(evidence, "rows", ()) or getattr(evidence, "candles", ()))
    packages = tuple(_package_summary(item, source_kind, rows)
                     for item in getattr(evidence, "packages", ()))
    expected = len(packages)
    valid_count = sum(item["package_valid"] for item in packages)
    issue_count = sum(item["issue_count"] for item in packages)
    if expected and valid_count == expected and issue_count == 0:
        state = "FULL"
    elif valid_count:
        state = "PARTIAL"
    else:
        state = "UNAVAILABLE"
    symbols = tuple(getattr(evidence, "configured_symbols", ()))
    per_symbol = []
    for symbol in symbols:
        selected = (packages if source_kind == "liquidation" else
                    tuple(item for item in packages if item["symbol"] == symbol))
        row_count = sum(getattr(row, "symbol", None) == symbol for row in rows)
        symbol_coverage = (
            "FULL" if selected and all(item["package_valid"] and not item["issue_count"]
                                        for item in selected)
            else "PARTIAL" if any(item["package_valid"] for item in selected)
            else "UNAVAILABLE")
        symbol_state = ("OBSERVED" if row_count else
                        _NO_OBSERVATION_STATES[source_kind]
                        if symbol_coverage == "FULL" else "UNAVAILABLE")
        per_symbol.append({
            "symbol": symbol,
            "expected_package_count": len(selected),
            "verified_package_count": sum(item["package_valid"] for item in selected),
            "coverage_state": symbol_coverage,
            "observed_row_count": row_count,
            "observation_state": symbol_state,
            "native_package_statuses": tuple(
                {"package_name": item["package_name"], "status": item["status"],
                 "statuses": item["statuses"]}
                for item in selected),
        })
    return {"coverage_state": state, "exception_type": None,
            "expected_package_count": expected,
            "checksum_verified_package_count": sum(
                item["checksum_verified"] is True for item in packages),
            "verified_package_count": valid_count,
            "package_issue_count": issue_count, "packages": packages,
            "row_issue_reason_counts": _row_issue_counts(evidence),
            "per_symbol": tuple(per_symbol)}


def _row_issue_counts(evidence):
    counts = {}
    for field_name in ("row_issue_by_symbol_open", "row_issues"):
        for item in getattr(evidence, field_name, ()) or ():
            if isinstance(item, (tuple, list)) and item:
                reason = item[-1]
                if isinstance(reason, str):
                    counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def _core_expected_packages(request):
    for symbol in request.universe.symbols:
        for day in required_aggtrade_dates(request.replay_config):
            yield symbol, "aggTrades", day, daily_aggtrades_relative_path(symbol, day)
        for day in required_kline_dates(request.replay_config):
            yield symbol, "klines/1m", day, daily_kline_relative_path(symbol, day)


def _verify_core_coverage(root, period, *, download=False):
    config = study_replay_config(period.utc_date)
    request = BinanceUSDMArchiveRequest(root, study_universe(), config)
    if download:
        acquisition = BinanceHistoricalDownloadRequest(root, study_universe(), config)
        acquire_binance_usdm_historical_archive_files(acquisition)
    checks = []
    for symbol, family, day, relative in _core_expected_packages(request):
        archive = root.joinpath(*relative.parts)
        checksum = Path(str(archive) + ".CHECKSUM")
        base = {"package_name": relative.as_posix(), "symbol": symbol,
                "data_type": family, "utc_date": day.isoformat(),
                "archive_present": archive.is_file(), "checksum_present": checksum.is_file(),
                "checksum_verified": False, "sha256": None, "status": None}
        if not archive.is_file():
            base["status"] = "MISSING_PACKAGE"
        elif not checksum.is_file():
            base["status"] = "MISSING_CHECKSUM"
        else:
            try:
                base["sha256"] = _checksum(root, relative, symbol, family, day)
                base["checksum_verified"] = True
                base["status"] = "CHECKSUM_VERIFIED"
            except ValueError as exc:
                base["status"] = ("CHECKSUM_MISMATCH" if "mismatch" in str(exc).lower()
                                  else "INVALID_CHECKSUM")
        checks.append(base)
    try:
        evidence = verify_binance_usdm_historical_core_archives(request)
        manifest = evidence.archive_manifest
        coverage = _core_candle_coverage(period, evidence)
        return {
            "status": "VERIFIED" if coverage["missing_minute_count"] == 0
            else "VERIFIED_WITH_MISSING_MINUTES",
            "expected_package_count": len(checks),
            "checksum_verified_package_count": sum(item["checksum_verified"] for item in checks),
            "archive_content_sha256": manifest.content_sha256,
            "raw_replayable_trade_evidence_present": evidence.raw_replayable_trade_evidence_present,
            "candle_coverage": coverage,
            "packages": tuple(checks),
        }
    except (BinanceArchiveCoverageError, ValueError, OSError) as exc:
        return {
            "status": ("MISSING_PACKAGE_OR_CHECKSUM"
                       if isinstance(exc, BinanceArchiveCoverageError)
                       else "INVALID_ARCHIVE_OR_SCHEMA"),
            "expected_package_count": len(checks),
            "checksum_verified_package_count": sum(item["checksum_verified"] for item in checks),
            "archive_content_sha256": None,
            "raw_replayable_trade_evidence_present": False,
            "candle_coverage": None,
            "packages": tuple(checks),
            "exception_type": type(exc).__name__,
        }


def _core_candle_coverage(period, evidence: BinanceHistoricalCoreArchiveEvidence):
    start, end = period.start_boundary_time_ms, period.end_boundary_time_ms
    expected_count = (end - start) // MINUTE_MS
    by_symbol = {symbol: {item.open_time_ms for item in evidence.ohlc_evidence.candles
                          if item.symbol == symbol and start <= item.open_time_ms < end}
                 for symbol in ORDERED_SYMBOLS}
    per_symbol = []
    for symbol in ORDERED_SYMBOLS:
        opens = by_symbol[symbol]
        per_symbol.append({"symbol": symbol, "expected_minute_count": expected_count,
                           "observed_unique_minute_count": len(opens),
                           "missing_minute_count": expected_count - len(opens),
                           "coverage_ratio": len(opens) / expected_count})
    missing = sum(item["missing_minute_count"] for item in per_symbol)
    return {"expected_minute_count_per_symbol": expected_count,
            "observed_unique_minute_count": sum(item["observed_unique_minute_count"]
                                                  for item in per_symbol),
            "missing_minute_count": missing,
            "coverage_ratio": (sum(item["observed_unique_minute_count"]
                                    for item in per_symbol)
                               / (expected_count * len(ORDERED_SYMBOLS))),
            "per_symbol": tuple(per_symbol)}


def _load_supplementary_sources(period, roots, downloads):
    start, end = period.start_boundary_time_ms, period.end_boundary_time_ms
    symbols = ORDERED_SYMBOLS
    loaders = {
        "mark_trade": lambda: load_binance_usdm_mark_price_evidence(
            roots["mark_trade"], symbols, start, end,
            download=downloads.get("mark_trade", False)),
        "open_interest": lambda: load_binance_usdm_open_interest_evidence(
            roots["open_interest"], symbols, start, end,
            download=downloads.get("open_interest", False)),
        "funding": lambda: load_binance_usdm_funding_evidence(
            roots["funding"], symbols, start, end,
            download=downloads.get("funding", False)),
        "liquidation": lambda: load_tardis_liquidation_evidence(
            roots["liquidation"], symbols, start, end,
            download=downloads.get("liquidation", False)),
    }
    evidence, coverage = {}, {}
    for name, loader in loaders.items():
        try:
            evidence[name] = loader()
            coverage[name] = _source_coverage_summary(evidence[name], source_kind=name)
        except (OSError, ValueError, RuntimeError) as exc:
            evidence[name] = None
            coverage[name] = _source_coverage_summary(
                None, type(exc).__name__, source_kind=name)
    return evidence, coverage


def build_extension_coverage_manifest(
    manifest: HistoricalMarketStateStudyManifest,
    archive_root: Path | str,
    *, mark_archive_root: Path | str | None = None,
    open_interest_archive_root: Path | str | None = None,
    funding_archive_root: Path | str | None = None,
    liquidation_archive_root: Path | str | None = None,
    downloads: Mapping[str, bool] | None = None,
    code_revision: str,
) -> dict:
    """Coverage-only scan; calls source loaders but no replay or candidate code."""
    if not isinstance(code_revision, str) or not code_revision:
        raise ValueError("coverage freeze requires code revision")
    periods = _manifest_periods(manifest)
    root = Path(archive_root).expanduser().resolve()
    roots = {
        "mark_trade": Path(mark_archive_root or root).expanduser().resolve(),
        "open_interest": Path(open_interest_archive_root or root).expanduser().resolve(),
        "funding": Path(funding_archive_root or root).expanduser().resolve(),
        "liquidation": Path(liquidation_archive_root or root).expanduser().resolve(),
    }
    download_flags = dict(downloads or {})
    if set(download_flags) - set(roots) - {"core"} or any(
            type(value) is not bool for value in download_flags.values()):
        raise ValueError("coverage download flags must be explicit booleans by source")
    period_reports = []
    for period in periods:
        core = _verify_core_coverage(root, period,
                                     download=download_flags.get("core", False))
        _sources, source_coverage = _load_supplementary_sources(
            period, roots, download_flags)
        period_reports.append({
            "study_period_index": period.study_period_index,
            "source_bin_index": period.source_bin_index,
            "utc_date": period.utc_date.isoformat(), "phase": period.phase,
            "core": core,
            "tier_status": {
                "tier_1": ("FULL" if core["status"] == "VERIFIED"
                           else "PARTIAL" if core["archive_content_sha256"] else "UNAVAILABLE"),
                "tier_2": _combined_coverage_state(
                    source_coverage["mark_trade"], source_coverage["open_interest"],
                    source_coverage["funding"]),
                "tier_3": source_coverage["liquidation"]["coverage_state"],
            },
            "sources": source_coverage,
        })
    body = {
        "coverage_version": EXTENSION_COVERAGE_VERSION,
        "study_version": STUDY_VERSION,
        "study_manifest_sha256": manifest.manifest_sha256,
        "ordered_periods": tuple({"study_period_index": item.study_period_index,
                                   "utc_date": item.utc_date.isoformat(),
                                   "phase": item.phase} for item in periods),
        "source_identities": SOURCE_IDENTITIES,
        "coverage_rules": {
            "tier_1": "core verified archive packages; full-day trade OHLC coverage; aggTrade input required",
            "tier_2": "mark-price, open-interest, and settled-funding package/checksum and native row coverage",
            "tier_3": "liquidation receipt-day package coverage; no observation is distinct from unavailable coverage",
            "date_selection": "coverage never selects, rejects, or replaces a frozen study date",
        },
        "tool_config_version": TOOL_CONFIG_VERSION,
        "code_revision": code_revision,
        "periods": tuple(period_reports),
    }
    payload = report_json_safe(body)
    payload["coverage_manifest_sha256"] = _digest(payload)
    return payload


def _combined_coverage_state(*items):
    states = tuple(item["coverage_state"] for item in items)
    if states and all(value == "FULL" for value in states):
        return "FULL"
    if any(value in ("FULL", "PARTIAL") for value in states):
        return "PARTIAL"
    return "UNAVAILABLE"


def _study_experiment_points(replay, period):
    points = tuple(MarketStateExperimentPoint(
        item.movement_evaluation, item.source_time_evidence, period.phase)
        for item in replay.points)
    validate_experiment_points(points)
    if any(point.partition != period.phase for point in points):
        raise ValueError("study phase must label the complete replay stream uniformly")
    return points


def _frozen_eligibility(manifest, period):
    evidence = manifest.selection_evidence[period.source_bin_index]
    if evidence.bin_index != period.source_bin_index:
        raise ValueError("frozen selection evidence is not indexed by source bin")
    eligibility = evidence.attempted_dates[-1].eligibility
    if (eligibility.utc_date != period.utc_date or eligibility.provenance is None
            or eligibility.state != "ELIGIBLE"):
        raise ValueError("selected study period lacks finalized eligible archive provenance")
    return eligibility


def _label_price_evidence(dataset, archive_root, period):
    """Verify and parse the already-bound next-day kline ZIP for label tail only."""
    start, end = period.start_boundary_time_ms, period.end_boundary_time_ms
    label_end = end + 60 * MINUTE_MS
    end_date = datetime.fromtimestamp(end / 1000, timezone.utc).date()
    identity_by_path = {item.relative_path: item
                        for item in dataset.archive_manifest.archive_files}
    candles = { (item.symbol, item.open_time_ms): item
                for item in dataset.ohlc_evidence.candles
                if start - MINUTE_MS <= item.open_time_ms < label_end }
    for symbol in ORDERED_SYMBOLS:
        relative = daily_kline_relative_path(symbol, end_date)
        identity = identity_by_path.get(relative.as_posix())
        if identity is None or identity.data_type != "klines/1m":
            raise ValueError("frozen core manifest does not bind the forward label-tail package")
        digest = _checksum(Path(archive_root), relative, symbol, "klines/1m", end_date)
        if digest != identity.sha256:
            raise StudyArtifactConflictError("label-tail package differs from frozen core identity")
        for row in _archive_rows(Path(archive_root), relative, _KLINE_HEADER,
                                 _kline_row, end_date):
            if not start - MINUTE_MS <= row.open_time_ms < label_end:
                continue
            candle = CompletedTradeOHLCCandle(
                symbol, f"binance-usdm:{symbol}", row.open_time_ms,
                row.close_time_ms, row.open, row.high, row.low, row.close,
                row.close_time_ms + 1)
            key = (symbol, row.open_time_ms)
            prior = candles.get(key)
            if prior is not None and prior != candle:
                raise ValueError("label-tail candle differs from verified replay candle")
            candles[key] = candle
    return TradePriceForwardEvidence(
        ORDERED_SYMBOLS, tuple(candles.values()),
        dataset.archive_manifest.content_sha256, start - MINUTE_MS, label_end)


def _build_v1_evidence(period, replay_points, *, retain_branch=True, branch_writer=None):
    classifier_config = MarketClassifierConfig()
    lifecycle_config = MarketEpisodeLifecycleConfig()
    lifecycle_state = None
    previous_direction = None
    records = []
    state_by_boundary = {}
    branch_by_boundary = {}
    for replay_point in replay_points:
        boundary = replay_point.evaluation_boundary_time_ms
        classification, lifecycle = advance_canonical_branch(
            replay_point.movement_evaluation, replay_point.source_time_evidence,
            lifecycle_state, classifier_config, lifecycle_config)
        lifecycle_state = lifecycle.next_state
        if branch_writer is not None:
            branch_writer.append(replay_point, classification, lifecycle)
        if retain_branch:
            branch_by_boundary[boundary] = (classification, lifecycle)
        if not period.start_boundary_time_ms <= boundary < period.end_boundary_time_ms:
            continue
        primary = classification.windows[5]
        direction = primary.direction_state
        usable = direction if direction in ("BROAD_RISE", "BROAD_DROP", "NEUTRAL") else None
        state_by_boundary[boundary] = usable
        if boundary % MINUTE_MS == 0:
            records.append(adapt_v1_evidence(
                period, boundary, classification, lifecycle.next_state,
                lifecycle.transitions))
        directional_change = (usable is not None and previous_direction is not None
                              and usable != previous_direction)
        if lifecycle.transitions or directional_change:
            native = {
                "classification": report_json_safe(classification),
                "lifecycle_state": report_json_safe(lifecycle.next_state),
                "transitions": report_json_safe(lifecycle.transitions),
                "primary_direction_state": direction,
            }
            records.append(HistoricalStudyCandidateEvidence(
                period.study_period_index, period.utc_date.isoformat(), period.phase,
                "V1", "market-state-classifier-v1", classifier_config.version,
                boundary, "EVENT", "V1_TRANSITION", native))
        previous_direction = usable
    return tuple(records), state_by_boundary, branch_by_boundary


def _hmm_study_evidence(period, points, model):
    from .experiments.market_state_hmm_regimes import HMM_CONFIG_V1
    descriptor = next(item for item in EXPERIMENT_SUITE_V1
                      if item.experiment_id == "EXP-75-09")
    if period.phase == "development":
        block = extract_hmm_development_training_block(period, points)
        summary = {"training_block_sha256": block.block_sha256,
                   "usable_feature_rows": sum(len(item) for item in block.feature_blocks),
                   "unavailable_feature_rows": block.unavailable_row_count,
                   "training_status": "TRAINING_EVIDENCE_STORED"}
        return (), summary, block, None
    if not isinstance(model, GaussianHMMModelArtifact):
        raise ValueError("validation/test inference requires the frozen study HMM model")
    state: HMMFilterState | None = None  # Reset independently for this selected UTC day.
    previous_hard = None
    records = []
    state_counts = {}
    transition_count = 0
    for point in points:
        boundary = point.movement_evaluation.evaluation_boundary_time_ms
        if boundary >= period.end_boundary_time_ms:
            continue
        item, state = advance_hmm_regime_filter(
            point.movement_evaluation, period.phase, model, state,
            config=HMM_CONFIG_V1)
        if boundary % MINUTE_MS:
            continue  # Unscheduled points preserve the previous usable regime.
        if item.hard_state is None:
            previous_hard = None
            continue
        state_counts[item.hard_state] = state_counts.get(item.hard_state, 0) + 1
        native = report_json_safe(item)
        if boundary % MINUTE_MS == 0:
            records.append(HistoricalStudyCandidateEvidence(
                period.study_period_index, period.utc_date.isoformat(), period.phase,
                descriptor.experiment_id, descriptor.algorithm_version,
                descriptor.config_version, boundary, "CONTINUOUS", item.status, native))
        if (previous_hard is not None and not item.filter_reset_before_observation
                and item.hard_state != previous_hard):
            transition_count += 1
            records.append(HistoricalStudyCandidateEvidence(
                period.study_period_index, period.utc_date.isoformat(), period.phase,
                descriptor.experiment_id, descriptor.algorithm_version,
                descriptor.config_version, boundary, "EVENT", "HMM_HARD_STATE_TRANSITION",
                native))
        previous_hard = item.hard_state
    return tuple(records), {
        "model_sha256": model.model_sha256,
        "state_observation_counts": dict(sorted(state_counts.items())),
        "hard_state_transition_count": transition_count,
        "filter_reset_at_period_start": True,
        "retrained_on_this_period": False,
    }, None, model.model_sha256


def _record_candidate_bundle(period, descriptor, result):
    bundle = adapt_candidate_result(period, descriptor, result)
    return bundle


def _candidate_execution(prepared_period, supplementary, hmm_model,
                         runtime_metrics: StudyPeriodRuntimeMetrics | None = None,
                         *, stage_selector=None):
    period = prepared_period.period
    dataset = prepared_period.archive_dataset
    replay = prepared_period.canonical_replay_result
    points = prepared_period.experiment_points
    canonical_branch_by_boundary = prepared_period.canonical_v1_branch_by_boundary
    records = []
    native_summaries = []
    fixed_identities = tuple({
        "experiment_id": item.experiment_id,
        "algorithm_version": item.algorithm_version,
        "config_version": item.config_version,
    } for item in EXPERIMENT_SUITE_V1)
    classifier_config = MarketClassifierConfig()
    lifecycle_config = MarketEpisodeLifecycleConfig()
    core_started_ns = time.perf_counter_ns() if runtime_metrics is not None else None
    hmm_block = hmm_model_sha = None
    try:
        if stage_selector in (None, "hmm"):
            hmm_records, hmm_summary, hmm_block, hmm_model_sha = _hmm_study_evidence(
                period, points, hmm_model)
            records.extend(hmm_records)
            native_summaries.append({
                "experiment_id": "EXP-75-09", "algorithm_version": HMM_ALGORITHM_VERSION,
                "config_version": HMM_CONFIG_V1.version, "summary": hmm_summary,
                "result_sha256": _digest(hmm_records if hmm_records else hmm_block),
            })
        for descriptor_index, descriptor in enumerate(EXPERIMENT_SUITE_V1):
            if (descriptor.experiment_id == "EXP-75-09"
                    or stage_selector not in (None, f"fixed-{descriptor_index:02d}")):
                continue
            bocpd_field = ("bocpd_seconds"
                           if descriptor.experiment_id == "EXP-75-04B" else None)
            with _runtime_measure(runtime_metrics, bocpd_field):
                result = descriptor.runner(
                    points, descriptor.config, classifier_config=classifier_config,
                    lifecycle_config=lifecycle_config,
                    canonical_branch_by_boundary=canonical_branch_by_boundary)
                bundle = _record_candidate_bundle(period, descriptor, result)
                records.extend(bundle.records)
                native_summaries.append({
                    "experiment_id": descriptor.experiment_id,
                    "algorithm_version": descriptor.algorithm_version,
                    "config_version": descriptor.config_version,
                    "summary": bundle.native_summaries,
                    "result_sha256": bundle.result_sha256,
                    "evidence_record_count": len(bundle.records),
                })
    finally:
        if runtime_metrics is not None:
            runtime_metrics.record_elapsed_ns(
                "core_experiment_seconds", time.perf_counter_ns() - core_started_ns)

    # One configuration per disposable worker in the bounded path.
    if stage_selector is None or stage_selector.startswith("atr-"):
        with _runtime_measure(runtime_metrics, "atr_06b_seconds"):
            kwargs = {} if stage_selector is None else {
                "configurations": (ATR_CONFIGURATIONS[int(stage_selector.split("-")[1])],)}
            atr_results = run_market_state_atr_normalization_suite(
                points, dataset.ohlc_evidence, replay.manifest,
                classifier_config=classifier_config, lifecycle_config=lifecycle_config,
                canonical_branch_by_boundary=canonical_branch_by_boundary, **kwargs)
            for result in atr_results:
                descriptor = SimpleNamespace(
                    experiment_id="EXP-75-06B", algorithm_version=ATR_ALGORITHM_VERSION,
                    config_version=result.atr_config.version)
                bundle = adapt_candidate_result(period, descriptor, result)
                records.extend(bundle.records)
                native_summaries.append({
                    "experiment_id": "EXP-75-06B", "algorithm_version": ATR_ALGORITHM_VERSION,
                    "config_version": result.atr_config.version,
                    "summary": bundle.native_summaries,
                    "result_sha256": bundle.result_sha256,
                    "evidence_record_count": len(bundle.records),
                })
                del result, bundle
            del atr_results

    # Build existing extension Prepared contracts around the same dataset,
    # replay and uniform-phase point stream; no from-archive helper is called.
    symbols = replay.manifest.configured_universe
    if stage_selector in (None, "taker-flow"):
        with _runtime_measure(runtime_metrics, "taker_flow_seconds"):
            from .historical_extension_stream import temporary_points
            with temporary_points(build_historical_study_taker_flow_points(
                    dataset, replay, points, period.phase, retain=False)) as taker_points:
                taker_summary = _taker_summaries(taker_points, symbols)
                _append_extension_records(records, native_summaries, period, "EXP-75-12",
                                          TAKER_FLOW_ALGORITHM_VERSION, TAKER_FLOW_CONFIG_VERSION,
                                          taker_points, taker_summary[period.phase])

    extension_specs = (
        ("mark_trade", "EXP-75-10", MARK_TRADE_ALGORITHM_VERSION,
         MARK_TRADE_CONFIG_VERSION, HistoricalMarkTradeExtensionPrepared,
         "mark_evidence", "mark_archive_root", build_historical_study_mark_trade_points,
         _mark_summaries),
        ("open_interest", "EXP-75-11-OI", OI_ALGORITHM_VERSION,
         OI_CONFIG_VERSION, HistoricalOpenInterestExtensionPrepared,
         "oi_evidence", "oi_archive_root", build_historical_study_open_interest_points,
         _oi_summaries),
        ("funding", "EXP-75-11-FUNDING", FUNDING_ALGORITHM_VERSION,
         FUNDING_CONFIG_VERSION, HistoricalFundingExtensionPrepared,
         "funding_evidence", "funding_archive_root", build_historical_study_funding_points,
         _funding_summaries),
        ("liquidation", "EXP-75-11-LIQUIDATION", LIQ_ALGORITHM_VERSION,
         LIQ_CONFIG_VERSION, HistoricalLiquidationExtensionPrepared,
         "liquidation_evidence", "liquidation_archive_root",
         build_historical_study_liquidation_points, _liquidation_summaries),
    )
    extension_reports = {}
    for (name, experiment_id, algorithm, config, prepared_type, evidence_field,
         root_field, builder, summarize) in extension_specs:
        if stage_selector not in (None, name):
            continue
        with _runtime_measure(runtime_metrics, f"{name}_seconds"):
            source = supplementary.get(name)
            source_coverage = source["coverage"]
            if source["evidence"] is None:
                extension_reports[name] = {
                    "execution_status": "COVERAGE_UNAVAILABLE",
                    "coverage_state": source_coverage["coverage_state"],
                    "source_coverage_sha256": _digest(source_coverage),
                }
                native_summaries.append({
                    "experiment_id": experiment_id, "algorithm_version": algorithm,
                    "config_version": config, "summary": source_coverage,
                    "result_sha256": _digest(source_coverage),
                })
                continue
            kwargs = {
                "archive_dataset": dataset, "replay_result": replay,
                "experiment_points": points, "partition_plan": None,
                "study_phase": period.phase,
                evidence_field: source["evidence"],
                root_field: source["root"],
            }
            prepared = prepared_type(**kwargs)
            from .historical_extension_stream import temporary_points
            with temporary_points(builder(prepared, retain=False)) as candidate_points:
                summary = summarize(candidate_points, symbols)[period.phase]
                extension_reports[name] = {
                    "execution_status": "EXECUTED",
                    "coverage_state": source_coverage["coverage_state"],
                    "source_coverage_sha256": _digest(source_coverage),
                    "candidate_output_sha256": _digest(candidate_points),
                    "native_summary": summary,
                }
                _append_extension_records(records, native_summaries, period, experiment_id,
                                          algorithm, config, candidate_points, summary)

    return (tuple(records), tuple(native_summaries), fixed_identities,
            extension_reports, hmm_block, hmm_model_sha)


def _stage_prepared(prepared, selector):
    # Pure fixed candidates, V1 and event context have no archive/provider handle.
    dataset = prepared.archive_dataset
    if selector in ("v1", "event-context", "hmm") or selector.startswith("fixed-"):
        dataset = None
    else:
        from .historical_study_inputs import HistoricalStudyArchiveInputs
        dataset = HistoricalStudyArchiveInputs(dataset.archive_manifest, dataset.universe,
            dataset.config,
            dataset.ohlc_evidence if selector.startswith("atr-") or selector == "mark_trade" else None,
            dataset.taker_flow_evidence if selector == "taker-flow" else None)
    branch = (prepared.canonical_v1_branch_by_boundary
              if selector == "event-context" or selector.startswith(("fixed-", "atr-"))
              else None)
    return SimpleNamespace(**{**vars(prepared), "archive_dataset": dataset,
                              "canonical_v1_branch_by_boundary": branch})


def _stage_science_identity_base(selector):
    if selector in ("v1", "event-context"):
        from .market_episode_lifecycle import ALGORITHM_VERSION as lifecycle_algorithm
        return {"experiment_id": "V1", "algorithm_version": V1_CLASSIFIER_ALGORITHM_VERSION,
                "config_version": MarketClassifierConfig().version,
                "lifecycle_algorithm_version": lifecycle_algorithm,
                "lifecycle_config_version": MarketEpisodeLifecycleConfig().version,
                "event_context_version": EVENT_TIME_V1_CONTEXT_VERSION if selector == "event-context" else None}
    if selector == "outcomes":
        return {"algorithm_version": FORWARD_OUTCOMES_VERSION,
                "numeric_policy": FORWARD_NUMERIC_POLICY, "horizons_minutes": HORIZONS_MINUTES}
    if selector == "hmm" or selector.startswith("fixed-"):
        descriptor = (next(item for item in EXPERIMENT_SUITE_V1 if item.experiment_id == "EXP-75-09")
                      if selector == "hmm" else EXPERIMENT_SUITE_V1[int(selector.split("-")[1])])
        return {"experiment_id": descriptor.experiment_id,
                "algorithm_version": descriptor.algorithm_version,
                "config_version": descriptor.config_version}
    if selector.startswith("atr-"):
        return {"experiment_id": "EXP-75-06B", "algorithm_version": ATR_ALGORITHM_VERSION,
                "config_version": ATR_CONFIGURATIONS[int(selector.split("-")[1])].version}
    identity = {
        "taker-flow": ("EXP-75-12", TAKER_FLOW_ALGORITHM_VERSION, TAKER_FLOW_CONFIG_VERSION),
        "mark_trade": ("EXP-75-10", MARK_TRADE_ALGORITHM_VERSION, MARK_TRADE_CONFIG_VERSION),
        "open_interest": ("EXP-75-11-OI", OI_ALGORITHM_VERSION, OI_CONFIG_VERSION),
        "funding": ("EXP-75-11-FUNDING", FUNDING_ALGORITHM_VERSION, FUNDING_CONFIG_VERSION),
        "liquidation": ("EXP-75-11-LIQUIDATION", LIQ_ALGORITHM_VERSION, LIQ_CONFIG_VERSION),
    }[selector]
    return dict(zip(("experiment_id", "algorithm_version", "config_version"), identity))


def _stage_science_identity(selector):
    identity = _stage_science_identity_base(selector)
    identity["classifier_config"] = report_json_safe(MarketClassifierConfig())
    identity["lifecycle_config"] = report_json_safe(MarketEpisodeLifecycleConfig())
    if selector == "hmm":
        identity["candidate_config"] = report_json_safe(HMM_CONFIG_V1)
    elif selector.startswith("fixed-"):
        identity["candidate_config"] = report_json_safe(EXPERIMENT_SUITE_V1[int(selector.split("-")[1])].config)
    elif selector.startswith("atr-"):
        identity["candidate_config"] = report_json_safe(ATR_CONFIGURATIONS[int(selector.split("-")[1])])
    elif selector in ("taker-flow", "mark_trade", "open_interest", "funding", "liquidation"):
        import importlib
        module_name = {"taker-flow": "taker_flow", "mark_trade": "mark_trade", "open_interest": "open_interest",
                       "funding": "funding", "liquidation": "liquidation"}[selector]
        module = importlib.import_module(f"market_analysis.historical_{module_name}_extension")
        identity["calculation_parameters"] = {name: report_json_safe(getattr(module, name))
            for name in ("CALCULATION_RULE", "DECIMAL_PRECISION", "MARK_TRADE_DECIMAL_PRECISION",
                         "TAKER_FLOW_WINDOWS", "MARK_TRADE_WINDOWS_MINUTES", "OI_WINDOWS_MINUTES",
                         "FUNDING_CONTEXT_WINDOWS_MINUTES", "LIQUIDATION_WINDOWS_MINUTES")
            if hasattr(module, name)}
    return identity


def _candidate_stage_selectors():
    return ("hmm", *(f"fixed-{index:02d}" for index, item in enumerate(EXPERIMENT_SUITE_V1)
                          if item.experiment_id != "EXP-75-09"),
                 *(f"atr-{index}" for index in range(len(ATR_CONFIGURATIONS))),
                 "taker-flow", "mark_trade", "open_interest", "funding", "liquidation")

STAGE_BATCH_SIZE = 9


def _stage_batch_size():
    """Consecutive core candidate stages per worker; 1 keeps per-stage workers."""
    raw = os.environ.get("STUDY_STAGE_BATCH_SIZE")
    if raw is None or raw == "":
        return STAGE_BATCH_SIZE
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise ValueError("STUDY_STAGE_BATCH_SIZE must be an integer >= 1")
    return value


STAGE_WORKERS = 3


def _stage_workers():
    """Concurrent candidate batch workers, clamped to [1, CPUs]; 1 is sequential."""
    raw = os.environ.get("STUDY_STAGE_WORKERS")
    if raw is None or raw == "":
        value = STAGE_WORKERS
    else:
        try:
            value = int(raw)
        except ValueError:
            raise ValueError("STUDY_STAGE_WORKERS must be an integer") from None
    return max(1, min(value, os.cpu_count() or 1))


def _batchable_candidate_stage(selector, supplementary):
    # Core candidates only; extension stages keep their own per-stage workers.
    return ((selector == "hmm" or selector.startswith(("fixed-", "atr-")))
            and selector not in supplementary)


def _staged_candidate_execution(prepared, supplementary, hmm_model, *, progress=None,
                                runtime_metrics=None, worker_lease=None):
    stream = prepared.canonical_replay_result.points
    selectors = _candidate_stage_selectors()
    upstream_v1 = stage_dependencies(stream, ("v1",))
    record_sources, summaries, reports = [], [], {}
    hmm_block = hmm_sha = identities = None

    def stage_request(selector):
        source = {selector: supplementary[selector]} if selector in supplementary else {}
        request = {"action": "candidate", "prepared": _stage_prepared(prepared, selector),
                   "supplementary": source, "hmm_model": hmm_model,
                   "prepared_stream": stream, "required_stage_results": upstream_v1,
                   "selector": selector, "scientific_stage": _stage_science_identity(selector),
                   "supplementary_source_sha256": _digest({
                       name: {"coverage": item["coverage"], "evidence_sha256":
                              getattr(item["evidence"], "evidence_sha256", None)}
                       for name, item in source.items()})}
        timing_field = ("core_experiment_seconds" if selector == "hmm" or selector.startswith("fixed-")
                        else "atr_06b_seconds" if selector.startswith("atr-")
                        else "taker_flow_seconds" if selector == "taker-flow"
                        else f"{selector}_seconds")
        bocpd_field = ("bocpd_seconds" if request["scientific_stage"]["experiment_id"] == "EXP-75-04B"
                       else None)
        return source, request, timing_field, bocpd_field

    def accept(selector, result):
        nonlocal hmm_block, hmm_sha, identities
        stage_records, stage_summaries, fixed, extension, block, model_sha = result
        record_sources.append(stage_records)
        summaries.extend(stage_summaries)
        reports.update(extension)
        identities = fixed
        if selector == "hmm":
            hmm_block, hmm_sha = block, model_sha

    def batch_group(start):
        group = []
        for selector in selectors[start:min(start + batch_size, batched)]:
            _, request, timing_field, bocpd_field = stage_request(selector)
            descriptor_started = time.perf_counter_ns()
            descriptor = input_descriptor(request)
            group.append((selector, descriptor, (lambda request=request: request),
                          timing_field, bocpd_field,
                          time.perf_counter_ns() - descriptor_started))
        return group

    def consume(completed, group):
        for (selector, result, seconds), entry in zip(completed, group):
            if selector != entry[0]:
                raise ValueError("post-replay stage batch order mismatch")
            # Worker-measured seconds for new stages, parent seconds for reuse.
            elapsed_ns = entry[5] + max(0, int(seconds * 1_000_000_000))
            for field_name in (entry[3], entry[4]):
                if runtime_metrics is not None and field_name is not None:
                    runtime_metrics.record_elapsed_ns(field_name, elapsed_ns)
            accept(selector, result)

    batch_size = _stage_batch_size()
    batched = 0
    if batch_size > 1:
        while (batched < len(selectors)
               and _batchable_candidate_stage(selectors[batched], supplementary)):
            batched += 1
    position = 0
    workers = _stage_workers()
    if workers > 1 and batched:
        # Same batches as a run where every batch completes; workers only decide
        # which process runs each. Results are consumed in the original order;
        # after a short batch the sequential loop below takes the remainder.
        groups = [batch_group(start) for start in range(0, batched, batch_size)]
        results = run_stage_batches(
            stream, [[(selector, descriptor, prepare)
                      for selector, descriptor, prepare, *_ in group] for group in groups],
            workers=workers, progress=progress, worker_lease=worker_lease)
        for completed, group in zip(results, groups):
            consume(completed, group)
            position += len(completed)
            if len(completed) < len(group):
                break
    while position < batched:
        group = batch_group(position)
        completed = run_stage_batch(
            stream, [(selector, descriptor, prepare)
                     for selector, descriptor, prepare, *_ in group],
            progress=progress, worker_lease=worker_lease)
        if not completed:
            raise ValueError("post-replay stage batch made no progress")
        consume(completed, group)
        position += len(completed)
    for selector in selectors[batched:]:
        source, request, timing_field, bocpd_field = stage_request(selector)
        with _runtime_measure(runtime_metrics, timing_field), _runtime_measure(runtime_metrics, bocpd_field):
            descriptor = input_descriptor(request)
            unavailable = selector in source and source[selector]["evidence"] is None
            result = run_stage(stream, selector, descriptor=descriptor,
                prepare=lambda request=request: request, progress=progress, worker_lease=worker_lease,
                local_result=(lambda request=request: _candidate_execution(
                    request["prepared"], request["supplementary"], request["hmm_model"],
                    stage_selector=request["selector"])) if unavailable else None)
        accept(selector, result)
    from .historical_stage_records import JoinedStageRecords
    return JoinedStageRecords(tuple(record_sources)), tuple(summaries), identities, reports, hmm_block, hmm_sha


def _append_extension_records(records, summaries, period, experiment_id,
                              algorithm, config, points, summary):
    descriptor = SimpleNamespace(experiment_id=experiment_id,
                                 algorithm_version=algorithm,
                                 config_version=config)
    result = SimpleNamespace(points=points,
                             summaries={period.phase: summary})
    bundle = adapt_candidate_result(period, descriptor, result)
    records.extend(bundle.records)
    summaries.append({
        "experiment_id": experiment_id, "algorithm_version": algorithm,
        "config_version": config, "summary": bundle.native_summaries,
        "result_sha256": bundle.result_sha256,
        "evidence_record_count": len(bundle.records),
    })


def _period_filename(period):
    return f"{period.study_period_index:03d}-{period.utc_date.isoformat()}.json"


def _verify_hashed_payload(payload, hash_field, label):
    if not isinstance(payload, dict) or not isinstance(payload.get(hash_field), str):
        raise ValueError(f"{label} lacks its canonical identity hash")
    if payload[hash_field] != _digest({key: value for key, value in payload.items()
                                       if key != hash_field}):
        raise ValueError(f"{label} SHA-256 mismatch")
    return payload[hash_field]


def _verify_existing_period(path, manifest, coverage, period, code_revision):
    return _period_sidecar(path, manifest, coverage, period, code_revision)["report_sha256"]


def _validate_period_report(payload, manifest, period, code_revision, coverage_sha=None):
    """Shared fail-closed gate for resume, indexing and HMM preparation."""
    supplied = _verify_hashed_payload(payload, "report_sha256", "period report")
    headers = {
        "period_report_schema_version": PERIOD_REPORT_SCHEMA_VERSION,
        "execution_version": EXECUTION_VERSION, "study_version": STUDY_VERSION,
        "candidate_evidence_version": CANDIDATE_EVIDENCE_VERSION,
        "forward_outcomes_version": FORWARD_OUTCOMES_VERSION,
        "tool_config_version": TOOL_CONFIG_VERSION,
        "study_manifest_sha256": manifest.manifest_sha256,
        "code_revision": code_revision,
    }
    current_coverage = payload.get("extension_coverage_manifest_sha256")
    if (any(payload.get(key) != value for key, value in headers.items())
            or payload.get("period") != report_json_safe(period)
            or not isinstance(current_coverage, str) or len(current_coverage) != 64
            or any(char not in "0123456789abcdef" for char in current_coverage)
            or coverage_sha is not None and current_coverage != coverage_sha):
        raise StudyArtifactConflictError("period report scientific identity mismatch")
    taker_identity = payload.get("taker_flow_evidence_identity")
    if (not isinstance(taker_identity, dict)
            or taker_identity.get("algorithm_version") != TAKER_FLOW_ALGORITHM_VERSION):
        raise StudyArtifactConflictError("period report uses an incompatible taker-flow calculation")
    records = payload.get("candidate_evidence")
    if not isinstance(records, list) or payload.get("candidate_evidence_sha256") != _digest(records):
        raise ValueError("period candidate evidence hash mismatch")
    if any(not isinstance(record, dict) for record in records):
        raise ValueError("period candidate evidence contains a malformed record")
    for record in records:
        _verify_hashed_payload(record, "candidate_evidence_sha256", "candidate evidence")
        if (record.get("experiment_id") == "EXP-75-12"
                and record.get("algorithm_version") != TAKER_FLOW_ALGORITHM_VERSION):
            raise StudyArtifactConflictError("candidate uses an incompatible taker-flow calculation")
        if (record.get("study_period_index") != period.study_period_index
                or record.get("utc_date") != period.utc_date.isoformat()
                or record.get("phase") != period.phase):
            raise ValueError("candidate evidence has a mixed period identity")
    v1 = [record for record in records if record.get("experiment_id") == "V1"]
    if payload.get("v1_evidence_sha256") != _digest(v1):
        raise ValueError("period V1 evidence hash mismatch")
    for name, version in (("event_time_v1_context", EVENT_TIME_V1_CONTEXT_VERSION),
                          ("bocpd_onset_evidence", BOCPD_ONSET_EVIDENCE_VERSION)):
        section = payload.get(name)
        if (payload.get(name + "_version") != version or not isinstance(section, list)
                or payload.get(name + "_sha256") != _digest({"version": version, "records": section})):
            raise ValueError(f"period {name} scientific section mismatch")
    expected_times = set()
    for record in records:
        if record.get("evidence_kind") != "EVENT":
            continue
        boundary = record.get("decision_time_ms")
        if (type(boundary) is not int
                or not period.start_boundary_time_ms <= boundary < period.end_boundary_time_ms
                or boundary % 5_000):
            raise ValueError("candidate event has no valid exact period boundary")
        expected_times.add(boundary)
    if period.phase == "development":
        _validated_period_hmm_block(payload, period)
    contexts = payload["event_time_v1_context"]
    replay = payload.get("canonical_replay_manifest")
    if (not isinstance(replay, dict)
            or not isinstance(contexts, list)
            or any(not isinstance(item, dict) for item in contexts)):
        raise ValueError("period report lacks its canonical replay identity")
    replay_provenance = {
        "movement_algorithm_version": replay.get("movement_algorithm_version"),
        "movement_config_version": replay.get("movement_config_version"),
        "universe_id": replay.get("universe_id"),
        "universe_version": replay.get("universe_version"),
        "configured_universe": replay.get("configured_universe"),
        "provider": replay.get("provider"),
        "exchange": replay.get("exchange"),
        "price_type": replay.get("price_type"),
    }
    provenance_string_keys = tuple(key for key in replay_provenance
                                  if key != "configured_universe")
    if (any(not isinstance(replay_provenance[key], str) or not replay_provenance[key]
            for key in provenance_string_keys)
            or not isinstance(replay_provenance["configured_universe"], list)
            or not replay_provenance["configured_universe"]
            or any(not isinstance(symbol, str) or not symbol
                   for symbol in replay_provenance["configured_universe"])
            or [item.get("decision_time_ms") for item in contexts] != sorted(expected_times)
            or any(not isinstance(item, dict)
                   or not isinstance(item.get("classification"), dict)
                   or not isinstance(item.get("lifecycle_state"), dict)
                   or not isinstance(item.get("transitions"), list)
                   or not isinstance(item.get("provenance"), dict)
                   or not isinstance(item["provenance"].get("source_time_evidence"), list)
                   or any(item["provenance"].get(key) != value
                          for key, value in replay_provenance.items())
                   for item in contexts)):
        raise ValueError("period lacks complete exact-time V1 context")
    bocpd_records = tuple(HistoricalStudyCandidateEvidence(**{
        key: value for key, value in record.items() if key != "candidate_evidence_sha256"})
        for record in records if record.get("experiment_id") == "EXP-75-04B")
    if payload["bocpd_onset_evidence"] != report_json_safe(_bocpd_onset_evidence(bocpd_records)):
        raise ValueError("BOCPD onset section differs from exact causal candidate evidence")
    return supplied


def load_finalized_period_report(path, manifest, period, *, coverage_sha256, code_revision):
    """Artifact-only gate. Never opens archives or selects other periods."""
    if not isinstance(code_revision, str) or not code_revision:
        raise ValueError("explicit Part-B producer revision is required")
    if period not in manifest.selected_periods:
        raise ValueError("period is outside the frozen manifest")
    report = _read_json(Path(path))
    _validate_period_report(report, manifest, period, code_revision, coverage_sha256)
    if (report.get('pelt_forward_label_count') != 0 or report.get('pelt_no_causal_outcomes') is not True
            or any(group.get('experiment_id') == 'EXP-75-04A' for group in report.get('candidate_event_outcomes', ()))):
        raise ValueError('PELT has acquired causal forward labels')
    for record in report['candidate_evidence']:
        reconstructed = HistoricalStudyCandidateEvidence(**{
            k: v for k, v in record.items() if k != 'candidate_evidence_sha256'})
        if reconstructed.candidate_evidence_sha256 != record['candidate_evidence_sha256']:
            raise ValueError('candidate evidence reconstruction mismatch')
        if reconstructed.experiment_id == 'V1' and (
                reconstructed.algorithm_version != V1_CLASSIFIER_ALGORITHM_VERSION
                or reconstructed.config_version != MarketClassifierConfig().version):
            raise ValueError('V1 evidence classifier identity mismatch')
        boundary = reconstructed.decision_time_ms
        if (boundary is not None and not period.start_boundary_time_ms <= boundary < period.end_boundary_time_ms
                or reconstructed.evidence_kind == 'CONTINUOUS' and (boundary is None or boundary % MINUTE_MS)):
            raise ValueError('candidate evidence has an invalid exact period/minute boundary')
    # Bind persisted scientific provenance without re-reading archive bytes.
    for name in ("core_eligibility_sha256", "core_archive_content_sha256",
                 "canonical_replay_run_fingerprint", "canonical_replay_diagnostics_sha256"):
        value = report.get(name)
        if (not isinstance(value, str) or len(value) != 64
                or any(c not in "0123456789abcdef" for c in value)):
            raise ValueError("invalid period scientific provenance: " + name)
    if report["canonical_replay_diagnostics_sha256"] != _digest(report.get("canonical_replay_diagnostics")):
        raise ValueError("canonical replay diagnostics hash mismatch")
    if report["canonical_replay_manifest"].get("run_fingerprint") != report["canonical_replay_run_fingerprint"]:
        raise ValueError("canonical replay fingerprint mismatch")
    _verify_hashed_payload(report['canonical_replay_manifest'], 'run_fingerprint', 'canonical replay manifest')
    if report['canonical_replay_manifest'].get('dataset_content_sha256') != report['core_archive_content_sha256']:
        raise ValueError('canonical replay archive identity mismatch')
    label = report.get("forward_label_evidence", {})
    if (label.get("evidence_version") != FORWARD_OUTCOMES_VERSION
            or label.get("numeric_policy") != FORWARD_NUMERIC_POLICY
            or label.get("price_type") != "trade" or label.get("interval") != "1m"
            or label.get("label_tail_is_not_in_replay_input") is not True
            or label.get('availability_convention') != 'close_time_ms < decision_time and first_seen_at_ms <= decision_time'
            or label.get('label_range_start_boundary_time_ms') != period.start_boundary_time_ms - MINUTE_MS
            or label.get('label_range_end_boundary_time_ms') != period.end_boundary_time_ms + 60 * MINUTE_MS):
        raise ValueError("forward label provenance mismatch")
    for name in ('evidence_sha256', 'source_dataset_content_sha256'):
        value = label.get(name)
        if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
            raise ValueError('invalid forward label content identity')
    return report


def _validated_period_hmm_block(report, period):
    block = _load_hmm_development_block(report.get("hmm_development_training_block"))
    if ((block.study_period_index, block.utc_date, block.start_boundary_time_ms, block.end_boundary_time_ms)
            != (period.study_period_index, period.utc_date.isoformat(), period.start_boundary_time_ms,
                period.end_boundary_time_ms)
            or report.get("hmm_development_training_block_sha256") != block.block_sha256
            or any(not block.start_boundary_time_ms <= row.evaluation_boundary_time_ms < block.end_boundary_time_ms
                   for rows in block.feature_blocks for row in rows)
            or sum(len(rows) for rows in block.feature_blocks) + block.unavailable_row_count != (period.end_boundary_time_ms - period.start_boundary_time_ms) // MINUTE_MS):
        raise ValueError("HMM block does not describe the complete frozen development day")
    replay = report.get("canonical_replay_manifest", {})
    scope = (replay.get("movement_algorithm_version"), replay.get("movement_config_version"),
             replay.get("universe_id"), replay.get("universe_version"),
             tuple(replay.get("configured_universe", ())), replay.get("provider"),
             replay.get("exchange"), replay.get("price_type"))
    if block.movement_scope != scope:
        raise ValueError("HMM block scope differs from its canonical replay source")
    return block


def _load_hmm_development_block(value):
    if not isinstance(value, dict):
        raise ValueError("period artifact lacks HMM development training block")
    feature_blocks = tuple(tuple(HMMFeatureRow(
        row["evaluation_boundary_time_ms"], tuple(row["values"])) for row in block)
        for block in value["feature_blocks"])
    return HMMDevelopmentTrainingBlock(
        value["study_period_index"], value["utc_date"],
        value["start_boundary_time_ms"], value["end_boundary_time_ms"],
        tuple(tuple(item) if isinstance(item, list) else item for item in value["movement_scope"]), feature_blocks,
        value["unavailable_row_count"], value["block_sha256"])


def _parse_hmm_model(value):
    if not isinstance(value, dict):
        raise ValueError("frozen HMM artifact model must be an object")
    normalized = dict(value)
    for name in ("state_names", "feature_means", "feature_population_stds", "pi"):
        normalized[name] = tuple(normalized[name])
    for name in ("transition_matrix", "emission_means", "emission_variances"):
        normalized[name] = tuple(tuple(row) for row in normalized[name])
    return GaussianHMMModelArtifact(**normalized)


def load_frozen_hmm_model(path: Path | str, manifest, coverage,
                          code_revision: str | None = None):
    payload = _read_json(Path(path))
    _verify_hashed_payload(payload, "artifact_sha256", "HMM model artifact")
    expected_development = tuple(
        {"study_period_index": item.study_period_index,
         "utc_date": item.utc_date.isoformat(), "phase": "development"}
        for item in _manifest_periods(manifest) if item.phase == "development")
    supplied_development = tuple(
        {key: item.get(key) for key in ("study_period_index", "utc_date", "phase")}
        for item in payload.get("ordered_development_periods", ())
        if isinstance(item, dict))
    training_hashes = tuple(payload.get("training_block_sha256s", ()))
    if (payload.get("artifact_version") != HMM_DEVELOPMENT_MODEL_VERSION
            or payload.get("study_version") != STUDY_VERSION
            or payload.get("study_manifest_sha256") != manifest.manifest_sha256
            or payload.get("extension_coverage_manifest_sha256")
            != coverage["coverage_manifest_sha256"]
            or payload.get("algorithm_version") != HMM_ALGORITHM_VERSION
            or payload.get("config_version") != HMM_CONFIG_V1.version
            or (code_revision is not None
                and payload.get("code_revision") != code_revision)
            or tuple(payload.get("ordered_development_period_indices", ()))
            != tuple(range(10))
            or supplied_development != expected_development
            or len(training_hashes) != 10
            or any(not isinstance(item, str) or len(item) != 64
                   or any(char not in "0123456789abcdef" for char in item)
                   for item in training_hashes)):
        raise ValueError("frozen HMM model does not match the complete frozen development cohort")
    model = _parse_hmm_model(payload.get("model"))
    if payload.get("model_sha256") != model.model_sha256:
        raise ValueError("frozen HMM model parameter SHA-256 mismatch")
    return model


def _load_source_evidence(period, roots):
    values, summaries = _load_supplementary_sources(
        period, roots, {"mark_trade": False, "open_interest": False,
                        "funding": False, "liquidation": False})
    return {name: {"evidence": values[name], "coverage": summaries[name],
                   "root": roots[name]} for name in summaries}



def _event_time_v1_context(prepared_period, candidate_records, *, event_times=None):
    """Persist the exact canonical V1 branch at every causal event boundary."""
    event_times = tuple(sorted({
        item.decision_time_ms for item in candidate_records
        if item.evidence_kind == "EVENT" and item.decision_time_ms is not None
    })) if event_times is None else tuple(event_times)
    if not event_times and prepared_period.canonical_v1_branch_by_boundary is None:
        return ()
    if (event_times != tuple(sorted(set(event_times)))
            or any(type(boundary) is not int or boundary % 5_000
                   or not prepared_period.period.start_boundary_time_ms <= boundary < prepared_period.period.end_boundary_time_ms
                   for boundary in event_times)):
        raise ValueError("event context requires sorted unique causal period boundaries")
    requested = set(event_times)
    records = []
    lifecycle_state = None
    classifier_config = MarketClassifierConfig()
    lifecycle_config = MarketEpisodeLifecycleConfig()
    branch_map = prepared_period.canonical_v1_branch_by_boundary
    for replay_point in prepared_period.canonical_replay_result.points:
        boundary = replay_point.evaluation_boundary_time_ms
        if branch_map is None:
            classification, lifecycle = advance_canonical_branch(
                replay_point.movement_evaluation, replay_point.source_time_evidence,
                lifecycle_state, classifier_config, lifecycle_config)
            lifecycle_state = lifecycle.next_state
        elif hasattr(branch_map, "branch_for_point"):
            from .experiments.market_state_common import canonical_branch_for_point
            classification, lifecycle = canonical_branch_for_point(
                replay_point.movement_evaluation, replay_point.source_time_evidence,
                lifecycle_state, classifier_config, lifecycle_config, branch_map)
            lifecycle_state = lifecycle.next_state
        elif boundary in requested:
            branch = branch_map.get(boundary)
            if branch is None:
                raise ValueError("causal event lacks exact canonical V1 branch context")
            classification, lifecycle = branch
        if boundary not in requested:
            continue
        requested.remove(boundary)
        if (getattr(replay_point.movement_evaluation,
                    "evaluation_boundary_time_ms", None) != boundary):
            raise ValueError("event-time V1 context boundary mismatch")
        movement = replay_point.movement_evaluation
        records.append({
            "decision_time_ms": boundary,
            "classification": report_json_safe(classification),
            "lifecycle_state": report_json_safe(lifecycle.next_state),
            "transitions": report_json_safe(lifecycle.transitions),
            "provenance": {
                "source_time_evidence": report_json_safe(
                    replay_point.source_time_evidence),
                "movement_algorithm_version": movement.algorithm_version,
                "movement_config_version": movement.config_version,
                "universe_id": movement.universe_id,
                "universe_version": movement.universe_version,
                "configured_universe": report_json_safe(
                    movement.configured_universe),
                "provider": movement.provider,
                "exchange": movement.exchange,
                "price_type": movement.price_type,
            },
        })
    if requested:
        raise ValueError("causal event lacks exact canonical V1 branch context")
    return tuple(records)


def _bocpd_onset_evidence(candidate_records):
    """Extract only causal BOCPD onset observations from event evidence."""
    from dataclasses import fields as dataclass_fields
    from .experiments.market_state_bocpd import BOCPDObservation

    allowed = {item.name for item in dataclass_fields(BOCPDObservation)}
    records = []
    for item in candidate_records:
        if item.experiment_id != "EXP-75-04B" or item.evidence_kind != "EVENT":
            continue
        native = item.native_evidence
        if not isinstance(native, dict):
            raise ValueError("BOCPD event evidence must be an object")
        onset = native.get("causal_onset_observation")
        if (not isinstance(onset, dict)
                or set(onset) != allowed
                or onset.get("evaluation_boundary_time_ms") != item.decision_time_ms
                or onset.get("candidate_algorithm_version") != item.algorithm_version
                or onset.get("candidate_config_version") != item.config_version
                or onset.get("available") is not True
                or type(onset.get("recent_change_probability")) not in (int, float)
                or not math.isfinite(onset["recent_change_probability"])
                or not 0 <= onset["recent_change_probability"] <= 1):
            raise ValueError("BOCPD event lacks exact causal onset observation")
        records.append({
            "experiment_id": item.experiment_id,
            "algorithm_version": item.algorithm_version,
            "config_version": item.config_version,
            "decision_time_ms": item.decision_time_ms,
            "onset_observation": onset,
        })
    return tuple(sorted(records, key=lambda item: (
        item["config_version"], item["decision_time_ms"])))



def _continuous_and_event_outcomes(evidence, candidate_records, v1_state_by_boundary, period):
    from .historical_forward_lookup import ForwardLookup
    with ForwardLookup() as lookup:
        return _continuous_and_event_outcomes_cached(evidence, candidate_records,
                                                    v1_state_by_boundary, period, lookup)


def _continuous_and_event_outcomes_cached(evidence, candidate_records, v1_state_by_boundary,
                                          period, lookup):
    from .historical_market_state_forward_outcomes import evaluate_forward_outcome
    raw_identity = _digest({"evidence_sha256": evidence.evidence_sha256,
                            "version": FORWARD_OUTCOMES_VERSION, "numeric_policy": FORWARD_NUMERIC_POLICY})
    state_identity = _digest({"states": v1_state_by_boundary, "period": period,
                              "version": FORWARD_OUTCOMES_VERSION})

    def raw_outcome(decision, horizon):
        return lookup.get("raw", raw_identity, decision, horizon,
                          lambda: evaluate_forward_outcome(evidence, decision, horizon))

    def state_path(decision, horizon):
        return lookup.get("v1", state_identity, decision, horizon,
                          lambda: evaluate_v1_state_path(v1_state_by_boundary, decision, horizon,
                              period.start_boundary_time_ms, period.end_boundary_time_ms))
    continuous = evaluate_continuous_grids(
        evidence, period.start_boundary_time_ms, period.end_boundary_time_ms,
        outcome_lookup=raw_outcome)
    event_groups = {}
    for item in candidate_records:
        if item.evidence_kind == "RETROSPECTIVE":
            # PELT's hindsight points remain descriptive and receive no labels.
            continue
        if item.evidence_kind == "EVENT":
            key = (item.experiment_id, item.algorithm_version, item.config_version)
            event_groups.setdefault(key, set()).add(item.decision_time_ms)
    event_outcomes = []
    state_outcomes = []
    for (experiment_id, algorithm, config), times in sorted(event_groups.items()):
        by_horizon = []
        for horizon in HORIZONS_MINUTES:
            rows = evaluate_event_outcomes(evidence, sorted(times), horizon, outcome_lookup=raw_outcome)
            by_horizon.append((horizon, rows))
            for row in rows:
                state_outcomes.append({
                    "experiment_id": experiment_id,
                    "algorithm_version": algorithm,
                    "config_version": config,
                    "decision_time_ms": row["decision_time_ms"],
                    "horizon_minutes": horizon,
                    "state_path": state_path(row["decision_time_ms"], horizon),
                })
        event_outcomes.append({
            "experiment_id": experiment_id, "algorithm_version": algorithm,
            "config_version": config, "outcomes_by_horizon": tuple(by_horizon),
        })
    pelt = tuple(item for item in candidate_records
                 if item.evidence_kind == "RETROSPECTIVE")
    if any(item.experiment_id == "EXP-75-04A" for item in pelt):
        if any(item["experiment_id"] == "EXP-75-04A" for item in event_outcomes):
            raise ValueError("PELT retrospective evidence must not receive forward labels")
    for horizon, rows in continuous:
        for outcome in rows:
            state_outcomes.append({
                "experiment_id": "CONTINUOUS_GRID", "algorithm_version": FORWARD_OUTCOMES_VERSION,
                "config_version": f"{horizon}m", "decision_time_ms": outcome.decision_time_ms,
                "horizon_minutes": horizon,
                "state_path": state_path(outcome.decision_time_ms, horizon),
            })
    return continuous, tuple(event_outcomes), tuple(state_outcomes)


def _replay_identity(manifest, coverage, period, dataset, config, code_revision,
                     runtime_implementation_revision):
    run_manifest = historical_replay_run_manifest(dataset)
    return {
        "study_version": STUDY_VERSION,
        "study_manifest_sha256": manifest.manifest_sha256,
        "extension_coverage_manifest_sha256": coverage["coverage_manifest_sha256"],
        "scientific_producer_revision": code_revision,
        "runtime_implementation_revision": (
            runtime_implementation_revision
            if runtime_implementation_revision is not None
            else _current_code_revision()),
        "study_period_index": period.study_period_index,
        "utc_date": period.utc_date.isoformat(),
        "phase": period.phase,
        "run_fingerprint": run_manifest.run_fingerprint,
        "replay_algorithm_version": run_manifest.algorithm_version,
        "replay_policy_version": run_manifest.policy_version,
        "movement_algorithm_version": run_manifest.movement_algorithm_version,
        "movement_config_version": run_manifest.movement_config_version,
        "movement_config_parameters": report_json_safe(
            run_manifest.movement_config_parameters),
        "universe_id": dataset.universe.id,
        "universe_version": dataset.universe.version,
        "configured_symbols": report_json_safe(dataset.universe.symbols),
        "finalization_grace_ms": config.finalization_grace_ms,
        "archive_content_sha256": dataset.archive_manifest.content_sha256,
        "archive_files": report_json_safe(dataset.archive_manifest.archive_files),
        "trade_stream_manifest": report_json_safe(dataset.trade_stream_manifest),
    }


def _prepare_study_period(manifest, coverage, period, archive_root, code_revision,
                          runtime_metrics=None, checkpoint_root=None, progress=None,
                          runtime_implementation_revision=None, worker_lease=None):
    """Restore verified post-replay inputs before opening the raw trade index."""
    root = (Path(checkpoint_root) / f"period-{period.study_period_index:02d}-{period.utc_date.isoformat()}-{period.phase}"
            if checkpoint_root is not None else None)
    bundle = root / "prepared-replay.json" if root is not None else None
    config = study_replay_config(period.utc_date)
    if bundle is None or not bundle.exists() or not (root / f"checkpoint-{config.output_end_boundary_time_ms}.json").exists():
        return _prepare_study_period_fresh(manifest, coverage, period, archive_root, code_revision,
                    runtime_metrics, checkpoint_root, progress, runtime_implementation_revision, worker_lease)
    from .historical_study_runtime import decode
    from .historical_replay_runtime import _canonical_bytes, _read_json_bytes, _sha
    raw = bundle.read_bytes()
    value = _read_json_bytes(raw)
    body = dict(value)
    sha = body.pop("bundle_sha256", None)
    if (raw != _canonical_bytes(value) or sha != _sha(_canonical_bytes(body))
            or body.get("version") != "historical-prepared-replay-v1"):
        raise StudyArtifactConflictError("prepared replay bundle version/SHA mismatch")
    # Verify operational identity before scientific reconstruction.
    expected_prefix = {"study_manifest_sha256": manifest.manifest_sha256,
                       "extension_coverage_manifest_sha256": coverage["coverage_manifest_sha256"],
                       "scientific_producer_revision": code_revision,
                       "runtime_implementation_revision": runtime_implementation_revision or _current_code_revision(),
                       "study_period_index": period.study_period_index,
                       "utc_date": period.utc_date.isoformat(), "phase": period.phase}
    if any(body["identity"].get(key) != item for key, item in expected_prefix.items()):
        raise StudyArtifactConflictError("prepared replay producer/runtime identity mismatch")
    eligibility = _frozen_eligibility(manifest, period)
    dataset = decode(body["dataset"])
    if (body["eligibility_sha256"] != eligibility.eligibility_sha256
            or dataset.archive_manifest != eligibility.provenance.archive_manifest
            or dataset.config != config or dataset.universe != study_universe()):
        raise StudyArtifactConflictError("prepared replay differs from frozen archive/configuration")
    identity = _replay_identity(manifest, coverage, period, dataset, config,
                               code_revision, runtime_implementation_revision)
    if identity != body["identity"]:
        raise StudyArtifactConflictError("prepared normalized stream identity mismatch")
    frozen_coverage = coverage["periods"][period.study_period_index]
    if frozen_coverage["core"]["archive_content_sha256"] != dataset.archive_manifest.content_sha256:
        raise StudyArtifactConflictError("prepared archive differs from frozen coverage")
    for package in dataset.archive_manifest.archive_files:
        if _checksum(Path(archive_root), PurePosixPath(package.relative_path), package.symbol,
                     package.data_type, package.utc_date) != package.sha256:
            raise StudyArtifactConflictError("raw archive checksum changed since prepared replay")
    store = ReplayCheckpointStore(root, identity, config.output_start_boundary_time_ms,
                                  config.output_end_boundary_time_ms, progress=progress)
    from .historical_owned_validation import consume_complete
    if worker_lease is None or not consume_complete(store, worker_lease):
        # Metadata-only is safe only after equivalent full semantic validation
        # in this owned unchanged tree. A standalone/new trust boundary decodes
        # the exact chain/state/points before it can claim COMPLETE.
        store.load_latest()
    if not store._complete:
        raise StudyArtifactConflictError("prepared replay lacks COMPLETE validation")
    stream = create_study_point_stream(store)
    replay = CompactStudyReplay(decode(body["manifest"]), stream,
                               decode(body["diagnostics"]), decode(body["final_checkpoint"]))
    if replay.manifest != historical_replay_run_manifest(dataset):
        raise StudyArtifactConflictError("prepared replay manifest differs from normalized inputs")
    prepared = SimpleNamespace(period=period, archive_dataset=dataset,
        canonical_replay_result=replay, experiment_points=None, canonical_v1_branch_by_boundary=None,
        study_manifest_sha256=manifest.manifest_sha256,
        core_eligibility_sha256=eligibility.eligibility_sha256, code_revision=code_revision)
    records, states, branch = run_stage(stream, "v1", {"action": "v1",
        "prepared": _stage_prepared(prepared, "v1"), "scientific_stage": _stage_science_identity("v1")},
        progress=progress, worker_lease=worker_lease)
    prepared.canonical_v1_branch_by_boundary = branch
    if progress:
        progress("PREPARED_REPLAY_RESTORED", {"bundle_sha256": sha})
    return prepared, frozen_coverage, records, states


def _prepare_study_period_fresh(manifest, coverage, period, archive_root, code_revision,
                          runtime_metrics: StudyPeriodRuntimeMetrics | None = None,
                          checkpoint_root: Path | None = None,
                          progress=None,
                          runtime_implementation_revision: str | None = None, worker_lease=None):
    """Load and replay core evidence once, then freeze shared V1 study context."""
    config = study_replay_config(period.utc_date)
    from . import historical_operational_events as events
    from . import historical_prepared_core as prepared_core
    eligibility = _frozen_eligibility(manifest, period)
    request = BinanceUSDMArchiveRequest(archive_root, study_universe(), config)
    with events.span("core-preparation"), _runtime_measure(runtime_metrics, "core_archive_load_seconds"):
        # Cache never bypasses original source verification. Missing/corrupt raw
        # inputs are fatal, independently of optional-cache validation failures.
        if prepared_core.enabled():
            with events.span("raw-source-verification"):
                for package in eligibility.provenance.archive_manifest.archive_files:
                    if _checksum(Path(archive_root), PurePosixPath(package.relative_path), package.symbol,
                                 package.data_type, package.utc_date) != package.sha256:
                        raise StudyArtifactConflictError("raw archive differs from frozen preparation")
        dataset = prepared_core.restore(request, eligibility.provenance.archive_manifest)
        if dataset is None:
            dataset = load_binance_usdm_bounded_historical_replay_dataset(request)
    expected_archive = eligibility.provenance.archive_manifest
    if (dataset.archive_manifest.content_sha256 != expected_archive.content_sha256
            or dataset.archive_manifest.archive_files != expected_archive.archive_files):
        dataset.close()
        raise StudyArtifactConflictError(
            f"verified core dataset changed after study selection: {period.utc_date}")
    frozen_coverage = coverage["periods"][period.study_period_index]
    if (frozen_coverage["study_period_index"] != period.study_period_index
            or frozen_coverage["utc_date"] != period.utc_date.isoformat()
            or frozen_coverage["core"]["archive_content_sha256"]
            != dataset.archive_manifest.content_sha256):
        dataset.close()
        raise ValueError("frozen coverage does not match verified core packages for this date")
    try:
        prepared_core.publish_local(dataset)
        if progress is not None:
            progress("CORE_ARCHIVE_INDEX_READY", {
                "archive_content_sha256": dataset.archive_manifest.content_sha256,
                "trade_stream_sha256": dataset.trade_stream_manifest.normalized_row_stream_sha256,
            })
        def replay_progress(completed, total):
            if progress:
                progress("REPLAY_CURRENT_PROGRESS", {"completed_output_boundaries": completed,
                         "total_output_boundaries": total})
        with events.span("replay"), _runtime_measure(runtime_metrics, "canonical_replay_seconds"):
            if checkpoint_root is None:
                replay = run_bounded_historical_market_replay(dataset, progress_callback=replay_progress)
            else:
                run_manifest = historical_replay_run_manifest(dataset)
                identity = _replay_identity(manifest, coverage, period, dataset, config,
                                            code_revision, runtime_implementation_revision)
                store = ReplayCheckpointStore(
                    checkpoint_root / f"period-{period.study_period_index:02d}-{period.utc_date.isoformat()}-{period.phase}",
                    identity, config.output_start_boundary_time_ms,
                    config.output_end_boundary_time_ms, progress=progress)
                with events.span("recovery-validation"):
                    state = store.load_latest()
                if progress:
                    progress("REPLAY_RESUMED", {"resumed_boundaries": store.point_count,
                             "completed_output_boundaries": store.point_count,
                             "total_output_boundaries": (config.output_end_boundary_time_ms - config.output_start_boundary_time_ms) // 5000 + 1,
                             "checkpoint_sha256": store.previous_sha})
                partial = run_bounded_historical_market_replay(
                    dataset, runtime_state=state, boundary_callback=store.add_point,
                    retain_points=False, progress_callback=replay_progress)
                if not store._complete:
                    raise ValueError("replay ended without a complete durable checkpoint")
                stream = create_study_point_stream(store)
                replay = CompactStudyReplay(partial.manifest, stream, partial.diagnostics,
                                            partial.final_checkpoint)
    finally:
        dataset.close()
    if checkpoint_root is not None:
        from .historical_study_runtime import encode
        from .historical_replay_runtime import _atomic_write, _canonical_bytes, _sha
        bundle_body = {"version": "historical-prepared-replay-v1", "identity": identity,
                       "eligibility_sha256": eligibility.eligibility_sha256,
                       "dataset": encode(dataset), "manifest": encode(replay.manifest),
                       "diagnostics": encode(replay.diagnostics),
                       "final_checkpoint": encode(replay.final_checkpoint)}
        _atomic_write(store.root / "prepared-replay.json", _canonical_bytes({**bundle_body,
                      "bundle_sha256": _sha(_canonical_bytes(bundle_body))}))
        if progress is not None:
            progress("PREPARED_REPLAY_WRITTEN", {"bundle_sha256": _sha(_canonical_bytes(bundle_body))})
        prepared_period = SimpleNamespace(
            period=period, archive_dataset=dataset, canonical_replay_result=replay,
            experiment_points=None, canonical_v1_branch_by_boundary=None,
            study_manifest_sha256=manifest.manifest_sha256,
            core_eligibility_sha256=eligibility.eligibility_sha256, code_revision=code_revision)
        with _runtime_measure(runtime_metrics, "v1_preparation_seconds"):
            v1_records, v1_state_by_boundary, branch_reference = run_stage(
                stream, "v1", {"action": "v1", "prepared": _stage_prepared(prepared_period, "v1"),
                            "scientific_stage": _stage_science_identity("v1")}, progress=progress,
                worker_lease=worker_lease)
        prepared_period.canonical_v1_branch_by_boundary = branch_reference
        return prepared_period, frozen_coverage, v1_records, v1_state_by_boundary
    with _runtime_measure(runtime_metrics, "v1_preparation_seconds"):
        points = _study_experiment_points(replay, period)
        v1_records, v1_state_by_boundary, canonical_branch_by_boundary = (
            _build_v1_evidence(period, replay.points))
    prepared_period = PreparedHistoricalMarketStatePeriod(
        period, dataset, replay, points, canonical_branch_by_boundary,
        manifest.manifest_sha256, eligibility.eligibility_sha256, code_revision)
    return prepared_period, frozen_coverage, v1_records, v1_state_by_boundary


def _execute_period(*args, **kwargs):
    from . import historical_operational_events as events
    checkpoint_root = kwargs.get("checkpoint_root")
    if checkpoint_root is None:
        return _execute_period_unlocked(*args, **kwargs)
    period = args[2] if len(args) > 2 else kwargs["period"]
    from .historical_run_directory import owned_run_directory
    root = Path(checkpoint_root) / f"period-{period.study_period_index:02d}-{period.utc_date.isoformat()}-{period.phase}"
    with owned_run_directory(root) as lease, events.span("period-compute"):
        return _execute_period_unlocked(*args, **kwargs, worker_lease=lease)


def _execute_period_unlocked(
    manifest, coverage, period, archive_root, roots, code_revision,
    hmm_model, runtime_metrics: StudyPeriodRuntimeMetrics | None = None,
    checkpoint_root: Path | None = None, progress=None,
    runtime_implementation_revision: str | None = None, worker_lease=None, source_validation=None,
):
    if runtime_metrics is None:
        prepared_period, frozen_coverage, v1_records, v1_state_by_boundary = (
            _prepare_study_period(manifest, coverage, period, archive_root, code_revision,
                                  checkpoint_root=checkpoint_root, progress=progress,
                                  runtime_implementation_revision=runtime_implementation_revision,
                                  worker_lease=worker_lease))
    else:
        prepared_period, frozen_coverage, v1_records, v1_state_by_boundary = (
            _prepare_study_period(manifest, coverage, period, archive_root, code_revision,
                                  runtime_metrics=runtime_metrics,
                                  checkpoint_root=checkpoint_root, progress=progress,
                                  runtime_implementation_revision=runtime_implementation_revision,
                                  worker_lease=worker_lease))
    dataset = prepared_period.archive_dataset
    replay = prepared_period.canonical_replay_result
    points = prepared_period.experiment_points
    taker_evidence = dataset.taker_flow_evidence
    if taker_evidence is None:
        raise ValueError("the shared core load did not provide EXP-75-12 taker-flow evidence")
    with _runtime_measure(runtime_metrics, "supplementary_source_load_seconds"):
        supplementary = source_validation.current(period, coverage) if source_validation is not None else None
        if supplementary is None:
            supplementary = _load_source_evidence(period, roots)
    if _canonical({name: item["coverage"] for name, item in supplementary.items()}) != _canonical(
            frozen_coverage["sources"]):
        raise StudyArtifactConflictError(
            "supplementary source packages changed since coverage was frozen")

    if isinstance(replay, CompactStudyReplay):
        extension_results = _staged_candidate_execution(
            prepared_period, supplementary, hmm_model, progress=progress, runtime_metrics=runtime_metrics,
            worker_lease=worker_lease)
    elif runtime_metrics is None:
        extension_results = _candidate_execution(
            prepared_period, supplementary, hmm_model)
    else:
        extension_results = _candidate_execution(
            prepared_period, supplementary, hmm_model, runtime_metrics=runtime_metrics)
    (candidate_records, native_summaries, fixed_identities,
     extension_reports, hmm_block, hmm_model_sha) = extension_results
    if isinstance(replay, CompactStudyReplay):
        from .historical_stage_records import JoinedStageRecords, extract_candidate_inputs
        candidate_records = JoinedStageRecords((candidate_records, v1_records))
        decision_records, bocpd_onset_evidence = extract_candidate_inputs(candidate_records)
    else:
        candidate_records = tuple((*candidate_records, *v1_records))
        decision_records = candidate_records
        bocpd_onset_evidence = _bocpd_onset_evidence(candidate_records)
    dependencies = (stage_dependencies(replay.points, ("v1", *_candidate_stage_selectors()))
                    if isinstance(replay, CompactStudyReplay) else ())
    event_time_v1_context = (run_stage(
        replay.points, "event-context", {"action": "event-context",
            "prepared": _stage_prepared(prepared_period, "event-context"),
            "boundaries": tuple(sorted({item.decision_time_ms for item in decision_records
                if item.evidence_kind == "EVENT" and item.decision_time_ms is not None})),
            "scientific_stage": _stage_science_identity("event-context"),
            "required_stage_results": dependencies}, progress=progress, worker_lease=worker_lease)
        if isinstance(replay, CompactStudyReplay) else
        _event_time_v1_context(prepared_period, candidate_records))
    with _runtime_measure(runtime_metrics, "forward_label_evidence_seconds"):
        forward_evidence = _label_price_evidence(dataset, archive_root, period)
    with _runtime_measure(runtime_metrics, "forward_outcomes_seconds"):
        if isinstance(replay, CompactStudyReplay):
            continuous, events, state_outcomes = run_stage(
                replay.points, "outcomes", {"action": "outcomes", "period": period,
                    "forward_evidence": forward_evidence, "records": decision_records,
                    "states": v1_state_by_boundary, "scientific_stage": _stage_science_identity("outcomes"),
                    "required_stage_results": dependencies}, progress=progress, worker_lease=worker_lease)
        else:
            continuous, events, state_outcomes = _continuous_and_event_outcomes(
                forward_evidence, candidate_records, v1_state_by_boundary, period)
    if isinstance(replay, CompactStudyReplay):
        from .historical_stage_records import FilteredStageRecords
        continuous_state_labels = FilteredStageRecords(state_outcomes, "CONTINUOUS_GRID", True)
        event_state_labels = FilteredStageRecords(state_outcomes, "CONTINUOUS_GRID", False)
    else:
        continuous_state_labels = tuple(item for item in state_outcomes
                                        if item["experiment_id"] == "CONTINUOUS_GRID")
        event_state_labels = tuple(item for item in state_outcomes
                                   if item["experiment_id"] != "CONTINUOUS_GRID")

    body = {
        "period_report_schema_version": PERIOD_REPORT_SCHEMA_VERSION,
        "execution_version": EXECUTION_VERSION,
        "candidate_evidence_version": CANDIDATE_EVIDENCE_VERSION,
        "forward_outcomes_version": FORWARD_OUTCOMES_VERSION,
        "tool_config_version": TOOL_CONFIG_VERSION,
        "study_version": STUDY_VERSION,
        "study_manifest_sha256": manifest.manifest_sha256,
        "period": report_json_safe(period),
        "core_eligibility_sha256": prepared_period.core_eligibility_sha256,
        "core_archive_content_sha256": dataset.archive_manifest.content_sha256,
        "core_archive_files": tuple({"relative_path": item.relative_path,
                                      "data_type": item.data_type,
                                      "symbol": item.symbol,
                                      "utc_date": item.utc_date,
                                      "sha256": item.sha256}
                                     for item in dataset.archive_manifest.archive_files),
        "core_archive_diagnostics": report_json_safe(dataset.diagnostics),
        "taker_flow_evidence_identity": {
            "schema_version": taker_evidence.schema_version,
            "algorithm_version": taker_evidence.algorithm_version,
            "dataset_content_sha256": taker_evidence.dataset_content_sha256,
            "configured_symbols": taker_evidence.configured_symbols,
            "engine_start_boundary_time_ms": taker_evidence.engine_start_boundary_time_ms,
            "output_end_boundary_time_ms": taker_evidence.output_end_boundary_time_ms,
            "bucket_interval_ms": taker_evidence.bucket_interval_ms,
            "bucket_rule": taker_evidence.bucket_rule,
            "side_mapping": taker_evidence.side_mapping,
            "availability_basis": taker_evidence.availability_basis,
            "finalization_grace_ms": taker_evidence.finalization_grace_ms,
            "evidence_sha256": taker_evidence.evidence_sha256,
        },
        "canonical_replay_run_fingerprint": replay.manifest.run_fingerprint,
        "canonical_replay_manifest": report_json_safe(replay.manifest),
        "canonical_replay_diagnostics": report_json_safe(replay.diagnostics),
        "canonical_replay_diagnostics_sha256": _digest(replay.diagnostics),
        "code_revision": code_revision,
        "experiment_suite_identity": fixed_identities,
        "native_state_quality_summaries": native_summaries,
        "candidate_evidence": candidate_records,
        "candidate_evidence_sha256": _digest(candidate_records),
        "v1_evidence_sha256": _digest(v1_records),
        "event_time_v1_context_version": EVENT_TIME_V1_CONTEXT_VERSION,
        "event_time_v1_context": event_time_v1_context,
        "event_time_v1_context_sha256": _digest({
            "version": EVENT_TIME_V1_CONTEXT_VERSION,
            "records": event_time_v1_context,
        }),
        "bocpd_onset_evidence_version": BOCPD_ONSET_EVIDENCE_VERSION,
        "bocpd_onset_evidence": bocpd_onset_evidence,
        "bocpd_onset_evidence_sha256": _digest({
            "version": BOCPD_ONSET_EVIDENCE_VERSION,
            "records": bocpd_onset_evidence,
        }),
        "hmm_development_training_block": hmm_block,
        "hmm_development_training_block_sha256": (
            hmm_block.block_sha256 if hmm_block is not None else None),
        "hmm_model_sha256": hmm_model_sha,
        "extension_coverage_manifest_sha256": coverage["coverage_manifest_sha256"],
        "tier_extension_results": extension_reports,
        "forward_label_evidence": {
            "evidence_version": forward_evidence.evidence_version,
            "numeric_policy": FORWARD_NUMERIC_POLICY,
            "evidence_sha256": forward_evidence.evidence_sha256,
            "source_dataset_content_sha256": forward_evidence.source_dataset_content_sha256,
            "price_type": "trade", "interval": "1m",
            "availability_convention": "close_time_ms < decision_time and first_seen_at_ms <= decision_time",
            "label_range_start_boundary_time_ms": period.start_boundary_time_ms - MINUTE_MS,
            "label_range_end_boundary_time_ms": period.end_boundary_time_ms + 60 * MINUTE_MS,
            "label_tail_is_not_in_replay_input": True,
        },
        "candidate_independent_continuous_outcomes": continuous,
        "candidate_event_outcomes": events,
        "secondary_v1_continuous_state_paths": continuous_state_labels,
        "secondary_v1_event_state_paths": event_state_labels,
        "pelt_forward_label_count": 0,
    }
    pelt_label_count = sum(item["experiment_id"] == "EXP-75-04A" for item in events)
    if pelt_label_count != 0:
        raise ValueError("PELT retrospective evidence received a forward label")
    body["pelt_no_causal_outcomes"] = True
    return {**body, "report_sha256": _digest(body)}


def _write_period_stream(path, payload):
    from .historical_market_state_study_json import iter_canonical_study_json
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            for part in iter_canonical_study_json(payload):
                stream.write(part.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            from .historical_shared_v1 import _file_sha
            if _file_sha(path) != _file_sha(temporary):
                raise StudyArtifactConflictError("conflicting period artifact")
        else:
            os.link(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _period_sidecar(path, manifest, coverage, period, code_revision, report_sha=None):
    """Verified small index metadata; report bytes are checked without decoding."""
    from .historical_shared_v1 import _file_sha
    sidecar = path.parent.parent / ".period-manifests" / path.name
    expected = {"version": "historical-period-index-metadata-v1",
        "study_manifest_sha256": manifest.manifest_sha256,
        "coverage_sha256": coverage["coverage_manifest_sha256"],
        "code_revision": code_revision, "period": report_json_safe(period),
        "taker_flow_algorithm_version": TAKER_FLOW_ALGORITHM_VERSION,
        "report_file": path.name}
    if sidecar.exists():
        value = _read_json(sidecar)
        _verify_hashed_payload(value, "metadata_sha256", "period index metadata")
        if (any(value.get(key) != item for key, item in expected.items())
                or value["report_file_sha256"] != _file_sha(path)
                or (report_sha is not None and value["report_sha256"] != report_sha)):
            raise StudyArtifactConflictError("period index metadata identity/file mismatch")
        return value
    if report_sha is None:
        report = _read_json(path)
        report_sha = _validate_period_report(report, manifest, period, code_revision,
                                            coverage["coverage_manifest_sha256"])
    body = {**expected, "report_file_sha256": _file_sha(path), "report_sha256": report_sha}
    sidecar.parent.mkdir(exist_ok=True)
    _write_atomic_new(sidecar, _artifact_json(body, "metadata_sha256"))
    return body


def period_report_sha_for_verification(path, manifest, coverage, period, code_revision, *, full):
    """Finalized ``report_sha256``; ``full`` decodes and validates the whole report.

    Without ``full`` the report is never decoded: the hashed sidecar binds its
    identity and the recomputed report file SHA to the report SHA recorded when
    the report was finalized, so the bytes are those a full validation accepted.
    A missing sidecar always takes the full path.
    """
    path = Path(path)
    if full or not (path.parent.parent / ".period-manifests" / path.name).exists():
        report = load_finalized_period_report(path, manifest, period,
            coverage_sha256=coverage["coverage_manifest_sha256"], code_revision=code_revision)
        _period_sidecar(path, manifest, coverage, period, code_revision, report["report_sha256"])
        return report["report_sha256"]
    return _period_sidecar(path, manifest, coverage, period, code_revision)["report_sha256"]


def _update_execution_index(output_dir, manifest, coverage, code_revision):
    period_dir = output_dir / PERIOD_DIRECTORY
    entries = []
    if period_dir.exists():
        for path in sorted(period_dir.glob("*.json")):
            selected = next((item for item in _manifest_periods(manifest)
                             if path.name == _period_filename(item)), None)
            if selected is None:
                raise StudyArtifactConflictError("period directory contains an unknown report")
            metadata = _period_sidecar(path, manifest, coverage, selected, code_revision)
            sha = metadata["report_sha256"]
            period = metadata["period"]
            entries.append({"study_period_index": period["study_period_index"],
                            "utc_date": period["utc_date"], "phase": period["phase"],
                            "report_file": path.name, "report_sha256": sha})
    body = {"index_version": EXECUTION_INDEX_VERSION,
            "study_version": STUDY_VERSION,
            "study_manifest_sha256": manifest.manifest_sha256,
            "extension_coverage_manifest_sha256": coverage["coverage_manifest_sha256"],
            "execution_version": EXECUTION_VERSION,
            "code_revision": code_revision,
            "periods": tuple(sorted(entries, key=lambda item: item["study_period_index"]))}
    content = _artifact_json(body, "index_sha256")
    _replace_atomic(output_dir / EXECUTION_INDEX_FILENAME, content)


def _runtime_report_record(manifest, coverage, period, code_revision, status,
                           report_path, report_sha256, metrics=None):
    record = {
        "runtime_report_version": RUNTIME_REPORT_VERSION,
        "study_version": STUDY_VERSION,
        "study_manifest_sha256": manifest.manifest_sha256,
        "extension_coverage_manifest_sha256": coverage["coverage_manifest_sha256"],
        "code_revision": code_revision,
        "study_period_index": period.study_period_index,
        "utc_date": period.utc_date.isoformat(),
        "phase": period.phase,
        "execution_status": status,
        "period_report_filename": report_path.name,
        "period_report_sha256": report_sha256,
        "artifact_size_bytes": report_path.stat().st_size,
    }
    if status == "EXECUTED":
        if metrics is None:
            raise ValueError("executed runtime records require measured period timings")
        record.update(metrics.report_timings())
    elif status != "SKIPPED_EXISTING_ARTIFACT":
        raise ValueError("unknown runtime period execution status")
    return record


def _append_runtime_report(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(_canonical(record))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _append_progress_report(path: Path, manifest, coverage, period,
                            code_revision: str, event: str,
                            runtime_implementation_revision: str,
                            details: Mapping[str, Any] | None = None) -> None:
    record = {
        **(details or {}),
        "progress_version": STUDY_PROGRESS_VERSION,
        "event": event,
        "study_manifest_sha256": manifest.manifest_sha256,
        "extension_coverage_manifest_sha256": coverage["coverage_manifest_sha256"],
        "study_period_index": period.study_period_index,
        "utc_date": period.utc_date.isoformat(),
        "phase": period.phase,
        "scientific_producer_revision": code_revision,
        "runtime_implementation_revision": runtime_implementation_revision,
    }
    _append_runtime_report(path, record)


def execute_study_periods(
    manifest: HistoricalMarketStateStudyManifest,
    coverage_path: Path | str,
    archive_root: Path | str,
    output_dir: Path | str,
    *, phase: str | None = None,
    period_limit: int | None = None,
    period_index: int | None = None,
    slice_controller=None,
    source_validation=None,
    allow_test: bool = False,
    code_revision: str,
    mark_archive_root: Path | str | None = None,
    open_interest_archive_root: Path | str | None = None,
    funding_archive_root: Path | str | None = None,
    liquidation_archive_root: Path | str | None = None,
    hmm_model_path: Path | str | None = None,
    runtime_report_path: Path | str | None = None,
    checkpoint_dir: Path | str | None = None,
    progress_report_path: Path | str | None = None,
) -> tuple[Path, ...]:
    """Resume deterministic period outputs using only frozen local evidence."""
    selected = select_execution_periods(
        manifest, phase, period_limit, allow_test=allow_test, period_index=period_index)
    phase = selected[0].phase if selected else (phase or "development")
    if not isinstance(code_revision, str) or not code_revision:
        raise ValueError("study execution requires code_revision")
    runtime_implementation_revision = _current_code_revision()
    coverage = load_and_validate_coverage(coverage_path, manifest, code_revision)
    output = Path(output_dir).expanduser().resolve()
    checkpoints = Path(checkpoint_dir or (output / ".runtime-checkpoints")).expanduser().resolve()
    progress_report = Path(progress_report_path or (output / "study-progress.jsonl")).expanduser().resolve()
    period_dir = output / PERIOD_DIRECTORY
    period_dir.mkdir(parents=True, exist_ok=True)
    runtime_report = (Path(runtime_report_path).expanduser().resolve()
                      if runtime_report_path is not None else None)
    if runtime_report is not None or progress_report is not None:
        protected_paths = {
            Path(coverage_path).expanduser().resolve(),
            (output / EXECUTION_INDEX_FILENAME).resolve(),
        }
        protected_paths.update(
            (period_dir / _period_filename(item)).resolve()
            for item in _manifest_periods(manifest))
        protected_paths.add(
            Path(hmm_model_path or (output / HMM_MODEL_FILENAME)).expanduser().resolve())
        if runtime_report in protected_paths or progress_report in protected_paths:
            raise ValueError("operational report path conflicts with a scientific study artifact")
        if runtime_report == progress_report:
            raise ValueError("runtime and progress reports require separate paths")
    archive = Path(archive_root).expanduser().resolve()
    roots = {
        "mark_trade": Path(mark_archive_root or archive).expanduser().resolve(),
        "open_interest": Path(open_interest_archive_root or archive).expanduser().resolve(),
        "funding": Path(funding_archive_root or archive).expanduser().resolve(),
        "liquidation": Path(liquidation_archive_root or archive).expanduser().resolve(),
    }
    hmm_model = None
    if phase in ("validation", "test"):
        model_path = Path(hmm_model_path or (output / HMM_MODEL_FILENAME))
        if not model_path.is_file():
            raise ValueError("validation/test execution requires a frozen study HMM model")
        hmm_model = load_frozen_hmm_model(model_path, manifest, coverage, code_revision)

    written_or_skipped = []
    for period in selected:
        def progress(event, details=None):
            _append_progress_report(progress_report, manifest, coverage, period,
                                    code_revision, event,
                                    runtime_implementation_revision, details)
            if slice_controller is not None:
                slice_controller.observe(event, details or {})
        path = period_dir / _period_filename(period)
        if path.exists():
            report_sha = _verify_existing_period(
                path, manifest, coverage, period, code_revision)
            written_or_skipped.append(path)
            progress("SKIPPED_EXISTING_ARTIFACT", {"report_sha256": report_sha})
            if runtime_report is not None:
                _append_runtime_report(runtime_report, _runtime_report_record(
                    manifest, coverage, period, code_revision,
                    "SKIPPED_EXISTING_ARTIFACT", path, report_sha))
            _update_execution_index(output, manifest, coverage, code_revision)
            continue
        runtime_metrics = (StudyPeriodRuntimeMetrics()
                           if runtime_report is not None else None)
        period_started_ns = (time.perf_counter_ns()
                             if runtime_metrics is not None else None)
        progress("PERIOD_STARTED")
        if runtime_metrics is None:
            payload = _execute_period(manifest, coverage, period, archive, roots,
                                      code_revision, hmm_model,
                                      checkpoint_root=checkpoints, progress=progress,
                                      runtime_implementation_revision=runtime_implementation_revision,
                                      source_validation=source_validation)
        else:
            payload = _execute_period(manifest, coverage, period, archive, roots,
                                      code_revision, hmm_model,
                                      runtime_metrics=runtime_metrics,
                                      checkpoint_root=checkpoints, progress=progress,
                                      runtime_implementation_revision=runtime_implementation_revision,
                                      source_validation=source_validation)
        if runtime_metrics is not None:
            artifact_write_started_ns = time.perf_counter_ns()
        if slice_controller is not None:
            progress("BEFORE_PERIOD_FINALIZATION")
        _write_period_stream(path, payload)
        _period_sidecar(path, manifest, coverage, period, code_revision, payload["report_sha256"])
        progress("PERIOD_ARTIFACT_FINALIZED", {"report_sha256": payload["report_sha256"]})
        if runtime_metrics is not None:
            finalized_at_ns = time.perf_counter_ns()
            runtime_metrics.record_elapsed_ns(
                "period_artifact_write_seconds",
                finalized_at_ns - artifact_write_started_ns)
            runtime_metrics.record_elapsed_ns(
                "period_total_seconds", finalized_at_ns - period_started_ns)
            report_sha = _verify_hashed_payload(payload, "report_sha256", "period report")
        else:
            report_sha = payload["report_sha256"]
        if runtime_report is not None:
            _append_runtime_report(runtime_report, _runtime_report_record(
                manifest, coverage, period, code_revision, "EXECUTED", path,
                report_sha, runtime_metrics))
        _update_execution_index(output, manifest, coverage, code_revision)
        written_or_skipped.append(path)
    return tuple(written_or_skipped)


def validate_hmm_development_cohort(blocks, manifest):
    periods = tuple(item for item in _manifest_periods(manifest)
                    if item.phase == "development")
    blocks = tuple(blocks)
    if (len(periods) != 10 or len(blocks) != 10
            or any(not isinstance(item, HMMDevelopmentTrainingBlock) for item in blocks)
            or tuple(item.study_period_index for item in blocks) != tuple(range(10))
            or tuple(item.utc_date for item in blocks)
            != tuple(item.utc_date.isoformat() for item in periods)):
        raise ValueError("the frozen study HMM requires all 10 ordered development-period artifacts")
    return blocks


def freeze_study_hmm_model(
    manifest: HistoricalMarketStateStudyManifest,
    output_dir: Path | str,
    *, code_revision: str,
    output_json: Path | str | None = None,
) -> Path:
    """Freeze the study HMM only after all ten development period artifacts exist."""
    if not isinstance(code_revision, str) or not code_revision:
        raise ValueError("HMM model freeze requires code revision")
    output = Path(output_dir).expanduser().resolve()
    period_dir = output / PERIOD_DIRECTORY
    development_periods = tuple(item for item in _manifest_periods(manifest)
                                 if item.phase == "development")
    blocks, identities, reports = [], [], []
    coverage_sha = None
    for period in development_periods:
        path = period_dir / _period_filename(period)
        if not path.is_file():
            raise ValueError(
                f"cannot freeze study HMM before development period {period.study_period_index} completes")
        report = _read_json(path)
        _validate_period_report(report, manifest, period, code_revision, coverage_sha)
        current_coverage = report["extension_coverage_manifest_sha256"]
        if coverage_sha is None:
            coverage_sha = current_coverage
        block = _validated_period_hmm_block(report, period)
        blocks.append(block)
        identities.append({"study_period_index": period.study_period_index,
                           "utc_date": period.utc_date.isoformat(),
                           "phase": period.phase,
                           "period_report_sha256": report["report_sha256"]})
        reports.append(report)
    ordered_blocks = validate_hmm_development_cohort(blocks, manifest)
    diagnostics, model = train_hmm_regime_model_from_blocks(ordered_blocks, HMM_CONFIG_V1)
    if model is None or diagnostics.status != "HMM_TRAINING_READY":
        raise ValueError("all development blocks are present but the frozen HMM model is unavailable")
    body = {
        "artifact_version": HMM_DEVELOPMENT_MODEL_VERSION,
        "study_version": STUDY_VERSION,
        "study_manifest_sha256": manifest.manifest_sha256,
        "extension_coverage_manifest_sha256": coverage_sha,
        "algorithm_version": HMM_ALGORITHM_VERSION,
        "config_version": HMM_CONFIG_V1.version,
        "ordered_development_period_indices": tuple(range(10)),
        "ordered_development_periods": tuple(identities),
        "training_block_sha256s": tuple(item.block_sha256 for item in ordered_blocks),
        "training_diagnostics": diagnostics,
        "model": model,
        "model_sha256": model.model_sha256,
        "code_revision": code_revision,
    }
    payload = json.loads(_artifact_json(body, "artifact_sha256"))
    destination = Path(output_json).expanduser().resolve() if output_json else output / HMM_MODEL_FILENAME
    if destination.exists():
        existing = _read_json(destination)
        _verify_hashed_payload(existing, "artifact_sha256", "HMM model artifact")
        if _canonical(existing) != _canonical(payload):
            raise StudyArtifactConflictError("a different frozen study HMM model already exists")
        return destination
    _write_atomic_new(destination, _canonical(payload))
    return destination


def _current_code_revision() -> str:
    return current_runtime_implementation_revision()


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Frozen historical market-state study Part-B evidence tools")
    commands = parser.add_subparsers(dest="command", required=True)

    coverage = commands.add_parser("coverage", help="freeze supplementary source coverage only")
    coverage.add_argument("--study-manifest", type=Path, required=True)
    coverage.add_argument("--archive-root", type=Path, required=True)
    coverage.add_argument("--output-json", type=Path, required=True)
    coverage.add_argument("--mark-archive-root", type=Path)
    coverage.add_argument("--open-interest-archive-root", type=Path)
    coverage.add_argument("--funding-archive-root", type=Path)
    coverage.add_argument("--liquidation-archive-root", type=Path)
    coverage.add_argument("--code-revision")
    coverage.add_argument("--download-core", action="store_true",
                          help="explicitly acquire missing verified core archives")
    coverage.add_argument("--download-mark", action="store_true")
    coverage.add_argument("--download-open-interest", action="store_true")
    coverage.add_argument("--download-funding", action="store_true")
    coverage.add_argument("--download-liquidation", action="store_true")

    execute = commands.add_parser("execute", help="execute frozen periods and outcomes")
    execute.add_argument("--study-manifest", type=Path, required=True)
    execute.add_argument("--coverage-manifest", type=Path, required=True)
    execute.add_argument("--archive-root", type=Path, required=True)
    execute.add_argument("--output-dir", type=Path, required=True)
    execute.add_argument("--phase", choices=("development", "validation", "test"))
    execute.add_argument("--period-limit", type=int)
    execute.add_argument("--period-index", type=int)
    execute.add_argument("--allow-test", action="store_true")
    execute.add_argument("--code-revision")
    execute.add_argument("--mark-archive-root", type=Path)
    execute.add_argument("--open-interest-archive-root", type=Path)
    execute.add_argument("--funding-archive-root", type=Path)
    execute.add_argument("--liquidation-archive-root", type=Path)
    execute.add_argument("--hmm-model", type=Path)
    execute.add_argument("--runtime-report", type=Path,
                         help="append operational per-period timings to JSONL")
    execute.add_argument("--checkpoint-dir", type=Path,
                         help="local replay checkpoints (default: output/.runtime-checkpoints)")
    execute.add_argument("--progress-report", type=Path,
                         help="operational progress JSONL (default: output/study-progress.jsonl)")

    freeze = commands.add_parser("freeze-hmm", help="fit/freeze HMM from all ten dev blocks")
    freeze.add_argument("--study-manifest", type=Path, required=True)
    freeze.add_argument("--output-dir", type=Path, required=True)
    freeze.add_argument("--output-json", type=Path)
    freeze.add_argument("--code-revision")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    try:
        manifest = load_study_manifest(args.study_manifest)
        revision = args.code_revision or _current_code_revision()
        if args.command == "coverage":
            downloads = {
                "core": args.download_core,
                "mark_trade": args.download_mark,
                "open_interest": args.download_open_interest,
                "funding": args.download_funding,
                "liquidation": args.download_liquidation,
            }
            artifact = build_extension_coverage_manifest(
                manifest, args.archive_root,
                mark_archive_root=args.mark_archive_root,
                open_interest_archive_root=args.open_interest_archive_root,
                funding_archive_root=args.funding_archive_root,
                liquidation_archive_root=args.liquidation_archive_root,
                downloads=downloads, code_revision=revision)
            _write_atomic_new(args.output_json, _canonical(artifact))
            print(f"Coverage manifest: {args.output_json}")
            print(f"Coverage SHA-256: {artifact['coverage_manifest_sha256']}")
            return 0
        if args.command == "execute":
            if any(item is not None and item.expanduser().resolve()
                   == args.study_manifest.expanduser().resolve()
                   for item in (args.runtime_report, args.progress_report)):
                raise ValueError("operational report path conflicts with the study manifest")
            results = execute_study_periods(
                manifest, args.coverage_manifest, args.archive_root, args.output_dir,
                phase=args.phase, period_limit=args.period_limit, period_index=args.period_index,
                allow_test=args.allow_test, code_revision=revision,
                mark_archive_root=args.mark_archive_root,
                open_interest_archive_root=args.open_interest_archive_root,
                funding_archive_root=args.funding_archive_root,
                liquidation_archive_root=args.liquidation_archive_root,
                hmm_model_path=args.hmm_model,
                runtime_report_path=args.runtime_report,
                checkpoint_dir=args.checkpoint_dir,
                progress_report_path=args.progress_report)
            for path in results:
                print(path)
            return 0
        if args.command == "freeze-hmm":
            path = freeze_study_hmm_model(
                manifest, args.output_dir, code_revision=revision,
                output_json=args.output_json)
            print(f"Frozen study HMM model: {path}")
            return 0
        parser.error("unknown command")
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
