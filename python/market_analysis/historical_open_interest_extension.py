"""Independent historical open-interest context for EXP-75-11; descriptive only."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from decimal import Decimal, localcontext
from pathlib import Path
import subprocess
import sys
from types import MappingProxyType

from .binance_historical_archive import (
    BinanceHistoricalReplayDataset, BinanceBoundedHistoricalReplayDataset, BinanceUSDMArchiveRequest,
    load_binance_usdm_historical_replay_dataset,
)
from .experiments.market_state_common import validate_experiment_points
from .historical_experiment_batch import (
    _canonical_json, _sha256, _write_report, build_cli_parser,
    experiment_stream_sha256, report_json_safe,
)
from .historical_open_interest_evidence import (
    EVIDENCE_VERSION, SCHEMA_VERSION, SOURCE, AVAILABILITY_BASIS,
    BinanceOpenInterestEvidence, load_binance_usdm_open_interest_evidence,
)
from .historical_replay import (
    CompactStudyReplay, HistoricalMarketReplayResult, HistoricalReplayConfig,
    ReplayPartitionPlan,
    run_historical_market_replay, to_market_state_experiment_points,
)
from .movement_metrics import MarketMovementConfig, MarketUniverseInput


EXPERIMENT_ID = "EXP-75-11-OI"
ALGORITHM_VERSION = "open-interest-log-change-v1"
CONFIG_VERSION = "OI-LOG-CHANGE-5M-15M-DECIMAL50-v1"
SUITE_VERSION = "historical-open_interest-extension-suite-v1"
REPORT_VERSION = "historical-open_interest-extension-report-v1"
OI_WINDOWS_MINUTES = (5, 15)
DECIMAL_PRECISION = 50
CALCULATION_RULE = "ln(base quantity current / exact prior); create time + 5m; latest nominal slot required; 20m prehistory; no finalization grace"
REPORT_PARTITIONS = ("development", "validation", "test", "all")


def _decimal_text(value):
    if value is None:
        return None
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _median(values):
    if not values:
        return None
    values = sorted(values)
    mid = len(values) // 2
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / Decimal(2)


def _v1_context(point, symbol, minutes):
    window = point.movement_evaluation.windows[minutes]
    row = next(r for r in window.symbols if r.symbol == symbol)
    return {"v1_symbol_included": row.included,
            "v1_direction": row.direction.value if row.direction.available else None,
            "v1_direction_reason": row.direction.reason if not row.direction.available else None}


@dataclass(frozen=True)
class HistoricalOpenInterestExtensionRequest:
    archive_root: Path
    oi_archive_root: Path
    universe: MarketUniverseInput
    replay_config: HistoricalReplayConfig
    partition_plan: ReplayPartitionPlan
    code_revision: str
    download_oi_archives: bool = False

    def __post_init__(self):
        archive = BinanceUSDMArchiveRequest(self.archive_root, self.universe, self.replay_config)
        source_root = Path(self.oi_archive_root).expanduser().resolve()
        if source_root == archive.archive_root:
            raise ValueError("sidecar source requires a separate archive root")
        if (not isinstance(self.partition_plan, ReplayPartitionPlan)
                or self.replay_config.movement_config != MarketMovementConfig()):
            raise ValueError("extension requires canonical V1 replay and partition inputs")
        if not isinstance(self.code_revision, str) or not self.code_revision:
            raise ValueError("extension requires a code revision")
        if type(self.download_oi_archives) is not bool:
            raise ValueError("download flag must be boolean")
        object.__setattr__(self, "archive_root", archive.archive_root)
        object.__setattr__(self, "oi_archive_root", source_root)


@dataclass(frozen=True)
class HistoricalOpenInterestExtensionPrepared:
    archive_dataset: BinanceHistoricalReplayDataset | BinanceBoundedHistoricalReplayDataset
    replay_result: HistoricalMarketReplayResult
    experiment_points: tuple
    partition_plan: ReplayPartitionPlan | None
    oi_evidence: BinanceOpenInterestEvidence
    oi_archive_root: Path
    study_phase: str | None = None

    def __post_init__(self):
        if (not isinstance(self.archive_dataset, (BinanceHistoricalReplayDataset, BinanceBoundedHistoricalReplayDataset))
                or not (isinstance(self.replay_result, HistoricalMarketReplayResult)
                        or (self.study_phase is not None
                            and isinstance(self.replay_result, CompactStudyReplay)))
                or ((self.partition_plan is None) == (self.study_phase is None))
                or (self.partition_plan is not None
                    and not isinstance(self.partition_plan, ReplayPartitionPlan))
                or (self.study_phase is not None
                    and self.study_phase not in ("development", "validation", "test"))
                or not isinstance(self.oi_evidence, BinanceOpenInterestEvidence)):
            raise ValueError("invalid prepared extension contract")
        object.__setattr__(self, "experiment_points", tuple(self.experiment_points))
        object.__setattr__(self, "oi_archive_root", Path(self.oi_archive_root).expanduser().resolve())


@dataclass(frozen=True)
class OpenInterestPointOutput:
    point_id: str
    evaluation_boundary_time_ms: int
    partition: str
    windows: tuple


@dataclass(frozen=True)
class OpenInterestExtensionManifest:
    experiment_id: str
    suite_version: str
    report_version: str
    algorithm_version: str
    config_version: str
    ordered_symbols: tuple[str, ...]
    universe_id: str
    universe_version: str
    source_identity: object
    calculation_parameters: object
    replay_identity: object
    partition_cutoffs: object
    code_revision: str
    candidate_output_sha256: str
    extension_run_fingerprint: str


@dataclass(frozen=True)
class HistoricalOpenInterestExtensionReport:
    manifest: OpenInterestExtensionManifest
    archive_manifest: object
    replay_manifest: object
    archive_diagnostics: object
    replay_diagnostics: object
    source_coverage: object
    candidate_points: tuple[OpenInterestPointOutput, ...]
    partition_summaries: object
    report_sha256: str


def prepare_historical_open_interest_extension(request):
    if not isinstance(request, HistoricalOpenInterestExtensionRequest):
        raise ValueError("invalid extension request")
    dataset = load_binance_usdm_historical_replay_dataset(
        BinanceUSDMArchiveRequest(request.archive_root, request.universe, request.replay_config))
    replay = run_historical_market_replay(dataset.replay_request)
    points = to_market_state_experiment_points(replay, request.partition_plan)
    evidence = load_binance_usdm_open_interest_evidence(
        request.oi_archive_root, request.universe.symbols,
        request.replay_config.output_start_boundary_time_ms,
        request.replay_config.output_end_boundary_time_ms,
        download=request.download_oi_archives)
    return HistoricalOpenInterestExtensionPrepared(dataset, replay, points,
        request.partition_plan, evidence, request.oi_archive_root)


def _validate_prepared(prepared):
    if not isinstance(prepared, HistoricalOpenInterestExtensionPrepared):
        raise ValueError("invalid prepared extension")
    archive, replay, evidence = prepared.archive_dataset, prepared.replay_result, prepared.oi_evidence
    manifest = replay.manifest
    request = archive if isinstance(archive, BinanceBoundedHistoricalReplayDataset) else archive.replay_request
    if (archive.archive_manifest.dataset_id != manifest.dataset_id
            or archive.archive_manifest.dataset_version != manifest.dataset_version
            or archive.archive_manifest.content_sha256 != manifest.dataset_content_sha256
            or request.universe.symbols != manifest.configured_universe
            or (request.universe.id, request.universe.version) != (manifest.universe_id, manifest.universe_version)
            or archive.ohlc_evidence.configured_symbols != manifest.configured_universe
            or evidence.configured_symbols != manifest.configured_universe
            or (evidence.requested_start_boundary_time_ms, evidence.requested_end_boundary_time_ms)
            != (manifest.output_start_boundary_time_ms, manifest.output_end_boundary_time_ms)
            or request.config.movement_config != MarketMovementConfig()
            or manifest.movement_config_version != request.config.movement_config.version
            or (request.config.output_start_boundary_time_ms, request.config.output_end_boundary_time_ms)
            != (manifest.output_start_boundary_time_ms, manifest.output_end_boundary_time_ms)):
        raise ValueError("evidence must match canonical replay identity, ordered universe and range")
    validate_experiment_points(prepared.experiment_points)
    if len(prepared.experiment_points) != len(replay.points):
        raise ValueError("replay point stream length differs")
    for replay_point, point in zip(replay.points, prepared.experiment_points):
        expected_partition = (
            "development" if replay_point.evaluation_boundary_time_ms <= prepared.partition_plan.development_end_boundary_time_ms
            else "validation" if replay_point.evaluation_boundary_time_ms <= prepared.partition_plan.validation_end_boundary_time_ms
            else "test")
        if (point.movement_evaluation is not replay_point.movement_evaluation
                or point.source_time_evidence != replay_point.source_time_evidence
                or point.partition != expected_partition):
            raise ValueError("extension requires exact exported V1 replay objects and partition labels")
    experiment_stream_sha256(replay, prepared.experiment_points, prepared.partition_plan)

def _symbol_output(evidence, point, symbol, minutes):
    boundary = point.evaluation_boundary_time_ms
    current = evidence.as_of(symbol, boundary)
    expected_current = boundary // 300_000 * 300_000 - 300_000
    prior = evidence.exact(symbol, current.source_time_ms - minutes * 60_000) if current else None
    reason = None
    if current is None and evidence.has_pending_observation(symbol, boundary):
        reason = "OI_NOT_YET_AVAILABLE"
    elif current is None or current.source_time_ms != expected_current:
        reason = evidence.reason_at(symbol, expected_current)
    elif prior is None:
        reason = evidence.reason_at(symbol, current.source_time_ms - minutes * 60_000)
    value = None
    if reason is None:
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            value = (current.quantity / prior.quantity).ln()
    return _freeze({"symbol": symbol, "window_minutes": minutes,
        "status": "UNAVAILABLE" if reason else "READY", "reason": reason,
        "expected_current_source_time_ms": expected_current,
        "current_source_time_ms": current.source_time_ms if current else None,
        "current_available_at_ms": current.available_at_ms if current else None,
        "current_quantity": _decimal_text(current.quantity) if current else None,
        "current_quote_value": _decimal_text(current.quote_value) if current else None,
        "prior_source_time_ms": prior.source_time_ms if prior else None,
        "prior_available_at_ms": prior.available_at_ms if prior else None,
        "prior_quantity": _decimal_text(prior.quantity) if prior else None,
        "current_source_age_ms": boundary - current.source_time_ms if current else None,
        "current_availability_age_ms": boundary - current.available_at_ms if current else None,
        "delta_oi": _decimal_text(value), **_v1_context(point, symbol, minutes)})


def _market_summary(rows):
    ready = [r for r in rows if r["status"] == "READY"]
    values = [Decimal(r["delta_oi"]) for r in ready]
    return {"universe_count": len(rows), "ready_count": len(ready),
        "unavailable_count": len(rows) - len(ready), "partial_coverage": len(ready) < len(rows),
        "summary_denominator": len(ready), "positive_count": sum(v > 0 for v in values),
        "negative_count": sum(v < 0 for v in values), "zero_count": sum(v == 0 for v in values),
        "median_delta_oi": _decimal_text(_median(values))}



def _build_candidate_points(prepared):
    evidence = prepared.oi_evidence
    symbols = prepared.replay_result.manifest.configured_universe
    outputs = []
    for replay_point, point in zip(prepared.replay_result.points, prepared.experiment_points):
        windows = []
        for minutes in OI_WINDOWS_MINUTES:
            rows = tuple(_symbol_output(evidence, replay_point, symbol, minutes) for symbol in symbols)
            windows.append(_freeze({"window_minutes": minutes, "symbols": rows,
                                    "market_summary": _market_summary(rows)}))
        outputs.append(OpenInterestPointOutput(replay_point.point_id, replay_point.evaluation_boundary_time_ms,
                                         point.partition, tuple(windows)))
    return tuple(outputs)


def _partition_summaries(points, symbols):
    summaries = {}
    for partition in REPORT_PARTITIONS:
        selected = tuple(p for p in points if partition == "all" or p.partition == partition)
        windows = []
        for minutes in OI_WINDOWS_MINUTES:
            rows = [row for p in selected for window in p.windows
                    if window["window_minutes"] == minutes for row in window["symbols"]]
            ready = [r for r in rows if r["status"] == "READY"]
            per_symbol = []
            for symbol in symbols:
                projected = [r for r in rows if r["symbol"] == symbol]
                usable = [r for r in ready if r["symbol"] == symbol]
                per_symbol.append({"symbol": symbol, "point_count": len(projected),
                    "ready_count": len(usable), "unavailable_count": len(projected) - len(usable),
                    "ready_coverage_ratio": _ratio(len(usable), len(projected)),
                    "unavailable_reasons": dict(sorted(Counter(r["reason"] for r in projected if r["status"] == "UNAVAILABLE").items())),
                    "unique_source_endpoint_pair_count": len({(r["current_source_time_ms"], r["prior_source_time_ms"]) for r in usable}),})
            windows.append({"window_minutes": minutes, "point_count": len(selected),
                "configured_symbol_point_count": len(rows), "ready_symbol_point_count": len(ready),
                "unavailable_symbol_point_count": len(rows) - len(ready),
                "summary_denominator": len(ready), "per_symbol": per_symbol,
                "unique_source_endpoint_pair_count": len({(r["symbol"], r["current_source_time_ms"], r["prior_source_time_ms"]) for r in ready}),})
        summaries[partition] = windows
    return _freeze(summaries)


def _ratio(numerator, denominator):
    if denominator == 0:
        return None
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        return _decimal_text(Decimal(numerator) / Decimal(denominator))


def _source_coverage(evidence):
    return _freeze({"packages": evidence.packages, "usable_observation_count": len(evidence.rows),
        "requested_start_boundary_time_ms": evidence.requested_start_boundary_time_ms,
        "requested_end_boundary_time_ms": evidence.requested_end_boundary_time_ms,
        "history_start_time_ms": evidence.history_start_time_ms,
        "row_issues": evidence.row_issues,
        "per_symbol": tuple({"symbol": symbol,
            "expected_package_count": sum(p.symbol == symbol for p in evidence.packages),
            "checksum_verified_package_count": sum(p.symbol == symbol and p.checksum_verified for p in evidence.packages),
            "usable_source_row_count": sum(r.symbol == symbol for r in evidence.rows)}
            for symbol in evidence.configured_symbols),})


def build_historical_study_open_interest_points(prepared):
    """Run OI context over an already prepared uniform-phase study day."""
    from .historical_market_state_candidate_evidence import validate_study_phase_prepared
    validate_study_phase_prepared(prepared)
    return _build_candidate_points(prepared)


def run_historical_open_interest_extension(prepared, *, code_revision):
    """Calculate from already prepared canonical replay objects without loading/replaying again."""
    _validate_prepared(prepared)
    if not isinstance(code_revision, str) or not code_revision:
        raise ValueError("extension requires a code revision")
    points = _build_candidate_points(prepared)
    summaries = _partition_summaries(points, prepared.replay_result.manifest.configured_universe)
    evidence, replay = prepared.oi_evidence, prepared.replay_result.manifest
    coverage = _source_coverage(evidence)
    candidate_sha = _sha256({"candidate_points": points, "partition_summaries": summaries,
                             "source_coverage": coverage})
    manifest = OpenInterestExtensionManifest(EXPERIMENT_ID, SUITE_VERSION, REPORT_VERSION,
        ALGORITHM_VERSION, CONFIG_VERSION, replay.configured_universe, replay.universe_id,
        replay.universe_version, _freeze({"source": SOURCE, "schema_version": SCHEMA_VERSION,
            "evidence_version": EVIDENCE_VERSION, "availability_basis": AVAILABILITY_BASIS,
            "evidence_sha256": evidence.evidence_sha256, "packages": evidence.packages,
            "requested_start_boundary_time_ms": evidence.requested_start_boundary_time_ms,
            "requested_end_boundary_time_ms": evidence.requested_end_boundary_time_ms,
            "history_start_time_ms": evidence.history_start_time_ms}),
        _freeze({"decimal_precision": DECIMAL_PRECISION, "windows_minutes": OI_WINDOWS_MINUTES,
            "decimal_serialization": "fixed-point string; trailing fractional zeros removed",
            "rule": CALCULATION_RULE, "point_selection": "all exact canonical five-second replay points",
            "summary_rule": "ready symbols only; explicit denominators; partial coverage permitted"}),
        _freeze({"dataset_id": replay.dataset_id, "dataset_version": replay.dataset_version,
            "dataset_content_sha256": replay.dataset_content_sha256,
            "run_fingerprint": replay.run_fingerprint,
            "experiment_stream_sha256": experiment_stream_sha256(prepared.replay_result,
                prepared.experiment_points, prepared.partition_plan),
            "movement_algorithm_version": replay.movement_algorithm_version,
            "movement_config_version": replay.movement_config_version,
            "movement_config_parameters": replay.movement_config_parameters}),
        _freeze({"development_end_boundary_time_ms": prepared.partition_plan.development_end_boundary_time_ms,
            "validation_end_boundary_time_ms": prepared.partition_plan.validation_end_boundary_time_ms}),
        code_revision, candidate_sha, "")
    manifest = replace(manifest, extension_run_fingerprint=_sha256({k: v
        for k, v in report_json_safe(manifest).items() if k != "extension_run_fingerprint"}))
    report = HistoricalOpenInterestExtensionReport(manifest, prepared.archive_dataset.archive_manifest,
        replay, prepared.archive_dataset.diagnostics, prepared.replay_result.diagnostics,
        coverage, points, summaries, "")
    return replace(report, report_sha256=_sha256({k: v for k, v in report_json_safe(report).items()
                                                 if k != "report_sha256"}))


def run_historical_open_interest_extension_from_archive(request):
    return run_historical_open_interest_extension(prepare_historical_open_interest_extension(request),
                                            code_revision=request.code_revision)


def historical_open_interest_extension_report_to_json(report):
    if not isinstance(report, HistoricalOpenInterestExtensionReport):
        raise ValueError("invalid extension report")
    if report.report_sha256 != _sha256({k: v for k, v in report_json_safe(report).items()
                                       if k != "report_sha256"}):
        raise ValueError("report digest differs from canonical content")
    return _canonical_json(report)


def build_open_interest_extension_cli_parser():
    parser = build_cli_parser()
    parser.description = "Run independent historical open-interest supporting context"
    parser.add_argument("--oi-archive-root", type=Path, required=True)
    parser.add_argument("--download-oi-archives", action="store_true",
                        help="explicit opt-in Binance verified OI archive acquisition")
    return parser


def main(argv=None):
    parser = build_open_interest_extension_cli_parser()
    args = parser.parse_args(argv)
    if args.overwrite and args.output_json is None:
        parser.error("--overwrite requires --output-json")
    if args.output_json is not None and args.output_json.exists() and not args.overwrite:
        parser.error("output exists; pass --overwrite to replace")
    try:
        revision = args.code_revision
        if revision is None:
            revision = subprocess.run(["git", "-C", str(Path(__file__).resolve().parents[2]),
                "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        request = HistoricalOpenInterestExtensionRequest(args.archive_root, args.oi_archive_root,
            MarketUniverseInput(args.universe_id, args.universe_version, tuple(args.symbols)),
            HistoricalReplayConfig(args.start, args.end, args.finalization_grace_ms, MarketMovementConfig()),
            ReplayPartitionPlan(args.development_end, args.validation_end), revision, args.download_oi_archives)
        content = historical_open_interest_extension_report_to_json(
            run_historical_open_interest_extension_from_archive(request))
        if args.output_json is None:
            sys.stdout.write(content + "\n")
        else:
            _write_report(args.output_json, content, overwrite=args.overwrite)
    except (ValueError, TypeError, ArithmeticError, OSError, subprocess.CalledProcessError) as exc:
        print(f"historical open-interest extension: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
