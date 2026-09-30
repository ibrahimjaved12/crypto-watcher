"""Independent historical observed-liquidation context for EXP-75-11; descriptive only."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from decimal import Decimal, localcontext
from pathlib import Path
import subprocess
import sys
from types import MappingProxyType

from .binance_historical_archive import (
    BinanceHistoricalReplayDataset, BinanceUSDMArchiveRequest,
    load_binance_usdm_historical_replay_dataset,
)
from .experiments.market_state_common import validate_experiment_points
from .historical_experiment_batch import (
    _canonical_json, _sha256, _write_report, build_cli_parser,
    experiment_stream_sha256, report_json_safe,
)
from .historical_liquidation_evidence import (
    EVIDENCE_VERSION, SCHEMA_VERSION, SOURCE, AVAILABILITY_BASIS,
    TardisLiquidationEvidence, load_tardis_liquidation_evidence,
)
from .historical_replay import (
    HistoricalMarketReplayResult, HistoricalReplayConfig, ReplayPartitionPlan,
    run_historical_market_replay, to_market_state_experiment_points,
)
from .movement_metrics import MarketMovementConfig, MarketUniverseInput
from .historical_taker_flow_evidence import _sum_decimals_exact

EXPERIMENT_ID = "EXP-75-11-LIQUIDATION"
ALGORITHM_VERSION = "observed-liquidation-snapshot-notional-v1"
CONFIG_VERSION = "LIQ-OBSERVED-NOTIONAL-1M-5M-15M-DECIMAL50-v1"
SUITE_VERSION = "historical-liquidation-extension-suite-v1"
REPORT_VERSION = "historical-liquidation-extension-report-v1"
LIQUIDATION_WINDOWS_MINUTES = (1, 5, 15)
DECIMAL_PRECISION = 50
CALCULATION_RULE = "event in [t-w,t); receipt us <= t ms * 1000; whole receipt-day interval coverage; exact price * provider normalized amount; no dedup; no finalization grace"
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
class HistoricalLiquidationExtensionRequest:
    archive_root: Path
    liquidation_archive_root: Path
    universe: MarketUniverseInput
    replay_config: HistoricalReplayConfig
    partition_plan: ReplayPartitionPlan
    code_revision: str
    download_free_liquidation_samples: bool = False

    def __post_init__(self):
        archive = BinanceUSDMArchiveRequest(self.archive_root, self.universe, self.replay_config)
        source_root = Path(self.liquidation_archive_root).expanduser().resolve()
        if source_root == archive.archive_root:
            raise ValueError("sidecar source requires a separate archive root")
        if (not isinstance(self.partition_plan, ReplayPartitionPlan)
                or self.replay_config.movement_config != MarketMovementConfig()):
            raise ValueError("extension requires canonical V1 replay and partition inputs")
        if not isinstance(self.code_revision, str) or not self.code_revision:
            raise ValueError("extension requires a code revision")
        if type(self.download_free_liquidation_samples) is not bool:
            raise ValueError("download flag must be boolean")
        object.__setattr__(self, "archive_root", archive.archive_root)
        object.__setattr__(self, "liquidation_archive_root", source_root)


@dataclass(frozen=True)
class HistoricalLiquidationExtensionPrepared:
    archive_dataset: BinanceHistoricalReplayDataset
    replay_result: HistoricalMarketReplayResult
    experiment_points: tuple
    partition_plan: ReplayPartitionPlan
    liquidation_evidence: TardisLiquidationEvidence
    liquidation_archive_root: Path

    def __post_init__(self):
        if (not isinstance(self.archive_dataset, BinanceHistoricalReplayDataset)
                or not isinstance(self.replay_result, HistoricalMarketReplayResult)
                or not isinstance(self.partition_plan, ReplayPartitionPlan)
                or not isinstance(self.liquidation_evidence, TardisLiquidationEvidence)):
            raise ValueError("invalid prepared extension contract")
        object.__setattr__(self, "experiment_points", tuple(self.experiment_points))
        object.__setattr__(self, "liquidation_archive_root", Path(self.liquidation_archive_root).expanduser().resolve())


@dataclass(frozen=True)
class LiquidationPointOutput:
    point_id: str
    evaluation_boundary_time_ms: int
    partition: str
    windows: tuple


@dataclass(frozen=True)
class LiquidationExtensionManifest:
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
class HistoricalLiquidationExtensionReport:
    manifest: LiquidationExtensionManifest
    archive_manifest: object
    replay_manifest: object
    archive_diagnostics: object
    replay_diagnostics: object
    source_coverage: object
    candidate_points: tuple[LiquidationPointOutput, ...]
    partition_summaries: object
    report_sha256: str


def prepare_historical_liquidation_extension(request):
    if not isinstance(request, HistoricalLiquidationExtensionRequest):
        raise ValueError("invalid extension request")
    dataset = load_binance_usdm_historical_replay_dataset(
        BinanceUSDMArchiveRequest(request.archive_root, request.universe, request.replay_config))
    replay = run_historical_market_replay(dataset.replay_request)
    points = to_market_state_experiment_points(replay, request.partition_plan)
    evidence = load_tardis_liquidation_evidence(
        request.liquidation_archive_root, request.universe.symbols,
        request.replay_config.output_start_boundary_time_ms,
        request.replay_config.output_end_boundary_time_ms,
        download=request.download_free_liquidation_samples)
    return HistoricalLiquidationExtensionPrepared(dataset, replay, points,
        request.partition_plan, evidence, request.liquidation_archive_root)


def _validate_prepared(prepared):
    if not isinstance(prepared, HistoricalLiquidationExtensionPrepared):
        raise ValueError("invalid prepared extension")
    archive, replay, evidence = prepared.archive_dataset, prepared.replay_result, prepared.liquidation_evidence
    manifest = replay.manifest
    request = archive.replay_request
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

def _product(price, amount):
    with localcontext() as context:
        context.prec = max(1, len(price.as_tuple().digits) + len(amount.as_tuple().digits))
        return price * amount


def _symbol_output(evidence, point, symbol, minutes):
    boundary = point.evaluation_boundary_time_ms
    start = boundary - minutes * 60_000
    reasons = evidence.coverage_reasons(start, boundary)
    rows = evidence.window_rows(symbol, boundary, minutes) if not reasons else ()
    long = short = total = imbalance = None
    status = "UNAVAILABLE"
    if not reasons:
        long = _sum_decimals_exact(tuple(_product(r.price, r.amount) for r in rows if r.side == "sell"))
        short = _sum_decimals_exact(tuple(_product(r.price, r.amount) for r in rows if r.side == "buy"))
        total = _sum_decimals_exact((long, short))
        status = "READY" if total > 0 else "NO_OBSERVED_LIQUIDATION"
        if total > 0:
            with localcontext() as context:
                context.prec = DECIMAL_PRECISION
                imbalance = _sum_decimals_exact((long, short.copy_negate())) / total
    return _freeze({"symbol": symbol, "window_minutes": minutes, "status": status,
        "reason": reasons[0][1] if reasons else None, "coverage_reasons": reasons,
        "coverage_state": "UNAVAILABLE" if reasons else "ARCHIVE_DAY_AVAILABLE",
        "window_start_event_time_us": start * 1000,
        "window_end_event_time_us_exclusive": boundary * 1000,
        "receipt_cutoff_us": boundary * 1000,
        "observed_snapshot_row_count": len(rows) if not reasons else None,
        "observed_event_time_range_us": (min(r.timestamp_us for r in rows), max(r.timestamp_us for r in rows)) if rows else None,
        "observed_receipt_time_range_us": (min(r.local_timestamp_us for r in rows), max(r.local_timestamp_us for r in rows)) if rows else None,
        "source_packages": tuple(dict.fromkeys(r.package_relative_path for r in rows)),
        "observed_long_liquidation_notional": _decimal_text(long),
        "observed_short_liquidation_notional": _decimal_text(short),
        "observed_total_liquidation_notional": _decimal_text(total),
        "observed_liquidation_imbalance": _decimal_text(imbalance),
        **_v1_context(point, symbol, minutes)})


def _market_summary(rows):
    covered = [r for r in rows if r["status"] != "UNAVAILABLE"]
    active = [r for r in covered if r["status"] == "READY"]
    long = _sum_decimals_exact(tuple(Decimal(r["observed_long_liquidation_notional"]) for r in covered)) if covered else None
    short = _sum_decimals_exact(tuple(Decimal(r["observed_short_liquidation_notional"]) for r in covered)) if covered else None
    total = _sum_decimals_exact((long, short)) if covered else None
    # Ordered configured universe breaks equal-notional ties deterministically.
    largest = max(active, key=lambda r: Decimal(r["observed_total_liquidation_notional"])) if active else None
    share = None
    if largest:
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            share = Decimal(largest["observed_total_liquidation_notional"]) / total
    imbalances = [Decimal(r["observed_liquidation_imbalance"]) for r in active]
    return {"universe_count": len(rows), "covered_count": len(covered), "ready_count": len(covered),
        "active_count": len(active), "no_observation_count": len(covered) - len(active),
        "unavailable_count": len(rows) - len(covered), "partial_coverage": len(covered) < len(rows),
        "summary_denominator": len(covered), "breadth_denominator": len(covered),
        "active_imbalance_denominator": len(active),
        "long_dominant_count": sum(v > 0 for v in imbalances),
        "short_dominant_count": sum(v < 0 for v in imbalances),
        "balanced_active_count": sum(v == 0 for v in imbalances),
        "liquidation_breadth": _ratio(len(active), len(covered)),
        "pooled_observed_long_notional": _decimal_text(long),
        "pooled_observed_short_notional": _decimal_text(short),
        "pooled_observed_total_notional": _decimal_text(total),
        "largest_active_symbol": largest["symbol"] if largest else None,
        "largest_symbol_share": _decimal_text(share)}



def _build_candidate_points(prepared):
    evidence = prepared.liquidation_evidence
    symbols = prepared.replay_result.manifest.configured_universe
    outputs = []
    for replay_point, point in zip(prepared.replay_result.points, prepared.experiment_points):
        windows = []
        for minutes in LIQUIDATION_WINDOWS_MINUTES:
            rows = tuple(_symbol_output(evidence, replay_point, symbol, minutes) for symbol in symbols)
            windows.append(_freeze({"window_minutes": minutes, "symbols": rows,
                                    "market_summary": _market_summary(rows)}))
        outputs.append(LiquidationPointOutput(replay_point.point_id, replay_point.evaluation_boundary_time_ms,
                                         point.partition, tuple(windows)))
    return tuple(outputs)


def _partition_summaries(points, symbols):
    summaries = {}
    for partition in REPORT_PARTITIONS:
        selected = tuple(p for p in points if partition == "all" or p.partition == partition)
        windows = []
        for minutes in LIQUIDATION_WINDOWS_MINUTES:
            rows = [row for p in selected for window in p.windows
                    if window["window_minutes"] == minutes for row in window["symbols"]]
            ready = [r for r in rows if r["status"] in ("READY", "NO_OBSERVED_LIQUIDATION")]
            per_symbol = []
            for symbol in symbols:
                projected = [r for r in rows if r["symbol"] == symbol]
                usable = [r for r in ready if r["symbol"] == symbol]
                per_symbol.append({"symbol": symbol, "point_count": len(projected),
                    "ready_count": len(usable), "unavailable_count": len(projected) - len(usable),
                    "ready_coverage_ratio": _ratio(len(usable), len(projected)),
                    "unavailable_reasons": dict(sorted(Counter(r["reason"] for r in projected if r["status"] == "UNAVAILABLE").items())),
                    "observed_snapshot_projection_count": sum(r["observed_snapshot_row_count"] for r in usable),})
            windows.append({"window_minutes": minutes, "point_count": len(selected),
                "configured_symbol_point_count": len(rows), "ready_symbol_point_count": len(ready),
                "unavailable_symbol_point_count": len(rows) - len(ready),
                "summary_denominator": len(ready), "per_symbol": per_symbol,
                "observed_snapshot_projection_count": sum(r["observed_snapshot_row_count"] for r in ready),})
        summaries[partition] = windows
    return _freeze(summaries)


def _ratio(numerator, denominator):
    if denominator == 0:
        return None
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        return _decimal_text(Decimal(numerator) / Decimal(denominator))


def _source_coverage(evidence):
    return _freeze({"packages": evidence.packages, "usable_snapshot_count": len(evidence.rows),
        "requested_start_boundary_time_ms": evidence.requested_start_boundary_time_ms,
        "requested_end_boundary_time_ms": evidence.requested_end_boundary_time_ms,
        "history_start_time_ms": evidence.history_start_time_ms,
        "disconnect_visibility": "UNAVAILABLE_IN_NORMALIZED_CSV",
        "expected_receipt_day_package_count": len(evidence.packages),
        "available_archive_day_count": sum(p.status == "ARCHIVE_DAY_AVAILABLE" for p in evidence.packages),})


def run_historical_liquidation_extension(prepared, *, code_revision):
    """Calculate from already prepared canonical replay objects without loading/replaying again."""
    _validate_prepared(prepared)
    if not isinstance(code_revision, str) or not code_revision:
        raise ValueError("extension requires a code revision")
    points = _build_candidate_points(prepared)
    summaries = _partition_summaries(points, prepared.replay_result.manifest.configured_universe)
    evidence, replay = prepared.liquidation_evidence, prepared.replay_result.manifest
    coverage = _source_coverage(evidence)
    candidate_sha = _sha256({"candidate_points": points, "partition_summaries": summaries,
                             "source_coverage": coverage})
    manifest = LiquidationExtensionManifest(EXPERIMENT_ID, SUITE_VERSION, REPORT_VERSION,
        ALGORITHM_VERSION, CONFIG_VERSION, replay.configured_universe, replay.universe_id,
        replay.universe_version, _freeze({"source": SOURCE, "schema_version": SCHEMA_VERSION,
            "evidence_version": EVIDENCE_VERSION, "availability_basis": AVAILABILITY_BASIS,
            "evidence_sha256": evidence.evidence_sha256, "packages": evidence.packages,
            "requested_start_boundary_time_ms": evidence.requested_start_boundary_time_ms,
            "requested_end_boundary_time_ms": evidence.requested_end_boundary_time_ms,
            "history_start_time_ms": evidence.history_start_time_ms}),
        _freeze({"decimal_precision": DECIMAL_PRECISION, "windows_minutes": LIQUIDATION_WINDOWS_MINUTES,
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
    report = HistoricalLiquidationExtensionReport(manifest, prepared.archive_dataset.archive_manifest,
        replay, prepared.archive_dataset.diagnostics, prepared.replay_result.diagnostics,
        coverage, points, summaries, "")
    return replace(report, report_sha256=_sha256({k: v for k, v in report_json_safe(report).items()
                                                 if k != "report_sha256"}))


def run_historical_liquidation_extension_from_archive(request):
    return run_historical_liquidation_extension(prepare_historical_liquidation_extension(request),
                                            code_revision=request.code_revision)


def historical_liquidation_extension_report_to_json(report):
    if not isinstance(report, HistoricalLiquidationExtensionReport):
        raise ValueError("invalid extension report")
    if report.report_sha256 != _sha256({k: v for k, v in report_json_safe(report).items()
                                       if k != "report_sha256"}):
        raise ValueError("report digest differs from canonical content")
    return _canonical_json(report)


def build_liquidation_extension_cli_parser():
    parser = build_cli_parser()
    parser.description = "Run independent historical observed-liquidation supporting context"
    parser.add_argument("--liquidation-archive-root", type=Path, required=True)
    parser.add_argument("--download-free-liquidation-samples", action="store_true",
                        help="explicit opt-in unauthenticated first-of-month Tardis sample acquisition only")
    return parser


def main(argv=None):
    parser = build_liquidation_extension_cli_parser()
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
        request = HistoricalLiquidationExtensionRequest(args.archive_root, args.liquidation_archive_root,
            MarketUniverseInput(args.universe_id, args.universe_version, tuple(args.symbols)),
            HistoricalReplayConfig(args.start, args.end, args.finalization_grace_ms, MarketMovementConfig()),
            ReplayPartitionPlan(args.development_end, args.validation_end), revision, args.download_free_liquidation_samples)
        content = historical_liquidation_extension_report_to_json(
            run_historical_liquidation_extension_from_archive(request))
        if args.output_json is None:
            sys.stdout.write(content + "\n")
        else:
            _write_report(args.output_json, content, overwrite=args.overwrite)
    except (ValueError, TypeError, ArithmeticError, OSError, subprocess.CalledProcessError) as exc:
        print(f"historical observed-liquidation extension: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
