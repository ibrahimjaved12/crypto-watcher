"""Separate historical mark-versus-trade diagnostic for Issue #127.

This contemporaneous, descriptive extension uses archive mark prices and the
verified trade-price OHLC evidence from the core replay loader. It does not
modify V1, the replay dataset, or the fixed experiment suite.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, replace
from decimal import Decimal, localcontext
from pathlib import Path
import subprocess
import sys
from types import MappingProxyType
from typing import Mapping

from .binance_historical_archive import (
    BinanceHistoricalReplayDataset, BinanceUSDMArchiveRequest,
    load_binance_usdm_historical_replay_dataset,
)
from .experiments.market_state_common import validate_experiment_points
from .historical_experiment_batch import (
    _canonical_json, _sha256, _write_report, build_cli_parser,
    experiment_stream_sha256, report_json_safe,
)
from .historical_mark_price_evidence import (
    MARK_PRICE_AVAILABILITY_BASIS, MARK_PRICE_EVIDENCE_SCHEMA_VERSION,
    MARK_PRICE_EVIDENCE_VERSION, MARK_PRICE_INTERVAL, MARK_PRICE_SCHEMA_DESCRIPTION,
    MARK_PRICE_SOURCE, MARK_PRICE_TYPE, BinanceMarkPriceEvidence,
    MarkPricePackageEvidence, load_binance_usdm_mark_price_evidence,
)
from .historical_ohlc_evidence import (
    OHLC_AVAILABILITY_BASIS, OHLC_EVIDENCE_VERSION,
    BinanceTradeOHLCEvidence, CompletedTradeOHLCCandle,
)
from .historical_replay import (
    HistoricalMarketReplayResult, HistoricalReplayConfig, ReplayPartitionPlan,
    run_historical_market_replay, to_market_state_experiment_points,
)
from .movement_history import MINUTE_MS
from .movement_metrics import MarketMovementConfig, MarketUniverseInput


MARK_TRADE_EXTENSION_SUITE_VERSION = "historical-mark-trade-extension-suite-v1"
MARK_TRADE_EXTENSION_REPORT_VERSION = "historical-mark-trade-extension-report-v1"
MARK_TRADE_ALGORITHM_VERSION = "mark-trade-log-basis-divergence-v1"
MARK_TRADE_CONFIG_VERSION = "MARK-TRADE-LOG-BASIS-1M-5M-15M-DECIMAL50-v1"
MARK_TRADE_WINDOWS_MINUTES = (1, 5, 15)
MARK_TRADE_DECIMAL_PRECISION = 50
REPORT_PARTITIONS = ("development", "validation", "test", "all")
_EXPECTED_SYMBOL_COUNT = 5


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _log_ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    if numerator <= 0 or denominator <= 0:
        raise ValueError("log-return endpoints must be positive")
    with localcontext() as context:
        context.prec = MARK_TRADE_DECIMAL_PRECISION
        return (numerator / denominator).ln()


def _mean(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    with localcontext() as context:
        context.prec = MARK_TRADE_DECIMAL_PRECISION
        return sum(values, Decimal(0)) / Decimal(len(values))


def _median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    with localcontext() as context:
        context.prec = MARK_TRADE_DECIMAL_PRECISION
        return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def _minute_ranges(timestamps: list[int]) -> tuple[dict, ...]:
    if not timestamps:
        return ()
    ranges = []
    start = prior = timestamps[0]
    for current in timestamps[1:]:
        if current != prior + MINUTE_MS:
            ranges.append({"start_open_time_ms": start,
                           "end_open_time_ms": prior,
                           "missing_minutes": (prior - start) // MINUTE_MS + 1})
            start = current
        prior = current
    ranges.append({"start_open_time_ms": start,
                   "end_open_time_ms": prior,
                   "missing_minutes": (prior - start) // MINUTE_MS + 1})
    return tuple(ranges)


@dataclass(frozen=True)
class HistoricalMarkTradeExtensionRequest:
    archive_root: Path
    mark_archive_root: Path
    universe: MarketUniverseInput
    replay_config: HistoricalReplayConfig
    partition_plan: ReplayPartitionPlan
    code_revision: str
    download_mark_archives: bool = False

    def __post_init__(self):
        archive = BinanceUSDMArchiveRequest(
            self.archive_root, self.universe, self.replay_config)
        object.__setattr__(self, "archive_root", archive.archive_root)
        try:
            mark_root = Path(self.mark_archive_root).expanduser().resolve()
        except (TypeError, ValueError, OSError) as exc:
            raise ValueError("mark_archive_root must be a filesystem path") from exc
        if mark_root == archive.archive_root:
            raise ValueError("mark-price archives must use a separate archive root")
        object.__setattr__(self, "mark_archive_root", mark_root)
        if len(self.universe.symbols) != _EXPECTED_SYMBOL_COUNT:
            raise ValueError("mark/trade extension requires the configured five-symbol universe")
        if (not isinstance(self.partition_plan, ReplayPartitionPlan)
                or self.replay_config.movement_config != MarketMovementConfig()):
            raise ValueError("mark/trade extension requires canonical V1 replay and partition inputs")
        if not isinstance(self.code_revision, str) or not self.code_revision:
            raise ValueError("mark/trade extension requires a code revision")
        if type(self.download_mark_archives) is not bool:
            raise ValueError("download_mark_archives must be a boolean")


@dataclass(frozen=True)
class HistoricalMarkTradeExtensionPrepared:
    archive_dataset: BinanceHistoricalReplayDataset
    replay_result: HistoricalMarketReplayResult
    experiment_points: tuple
    partition_plan: ReplayPartitionPlan | None
    mark_evidence: BinanceMarkPriceEvidence
    mark_archive_root: Path
    study_phase: str | None = None

    def __post_init__(self):
        if (not isinstance(self.archive_dataset, BinanceHistoricalReplayDataset)
                or not isinstance(self.replay_result, HistoricalMarketReplayResult)
                or ((self.partition_plan is None) == (self.study_phase is None))
                or (self.partition_plan is not None
                    and not isinstance(self.partition_plan, ReplayPartitionPlan))
                or (self.study_phase is not None
                    and self.study_phase not in ("development", "validation", "test"))
                or not isinstance(self.mark_evidence, BinanceMarkPriceEvidence)):
            raise ValueError("mark/trade extension preparation has invalid contract types")
        object.__setattr__(self, "experiment_points", tuple(self.experiment_points))
        object.__setattr__(self, "mark_archive_root",
                           Path(self.mark_archive_root).expanduser().resolve())


@dataclass(frozen=True)
class MarkTradeUnavailableReason:
    source: str
    open_time_ms: int
    reason: str


@dataclass(frozen=True)
class V1MarketStateContext:
    window_minutes: int
    market_wide_eligible: bool
    eligible_count: int
    eligible_fraction: float
    median_normalized_movement: float | None
    median_raw_return: float | None
    breadth_available: bool
    breadth_reason: str | None
    breadth_denominator: int
    breadth_rising_count: int | None
    breadth_falling_count: int | None
    breadth_flat_count: int | None


@dataclass(frozen=True)
class MarkTradeSymbolWindowOutput:
    symbol: str
    window_minutes: int
    status: str
    reasons: tuple[MarkTradeUnavailableReason, ...]
    expected_candle_open_times_ms: tuple[int, ...]
    mark_valid_candle_open_times_ms: tuple[int, ...]
    mark_valid_candle_close_times_ms: tuple[int, ...]
    trade_valid_candle_open_times_ms: tuple[int, ...]
    trade_valid_candle_close_times_ms: tuple[int, ...]
    mark_start_close: str | None
    mark_end_close: str | None
    trade_start_close: str | None
    trade_end_close: str | None
    mark_return: str | None
    trade_return: str | None
    basis_at_start: str | None
    basis_at_end: str | None
    divergence: str | None
    v1_symbol_included: bool
    v1_direction: str | None
    v1_material_rising: bool
    v1_material_falling: bool


@dataclass(frozen=True)
class MarkTradeWindowOutput:
    window_minutes: int
    v1_market_state: V1MarketStateContext
    all_configured_symbols_ready: bool
    symbols: tuple[MarkTradeSymbolWindowOutput, ...]


@dataclass(frozen=True)
class MarkTradePointOutput:
    point_id: str
    evaluation_boundary_time_ms: int
    partition: str
    windows: tuple[MarkTradeWindowOutput, ...]


@dataclass(frozen=True)
class MarkTradeExtensionManifest:
    suite_version: str
    report_version: str
    algorithm_version: str
    config_version: str
    decimal_precision: int
    windows_minutes: tuple[int, ...]
    decimal_serialization: str
    coverage_rule: str
    point_selection_rule: str
    ordered_symbols: tuple[str, ...]
    requested_start_boundary_time_ms: int
    requested_end_boundary_time_ms: int
    first_output_minute_boundary_time_ms: int
    last_output_minute_boundary_time_ms: int
    mark_warmup_minutes: int
    mark_warmup_start_time_ms: int
    mark_expected_open_time_end_ms_exclusive: int
    mark_source: str
    mark_price_type: str
    mark_interval: str
    mark_schema_description: str
    mark_evidence_version: str
    mark_evidence_schema_version: str
    mark_availability_basis: str
    mark_evidence_sha256: str
    mark_packages: tuple[MarkPricePackageEvidence, ...]
    trade_dataset_id: str
    trade_dataset_version: str
    trade_dataset_content_sha256: str
    trade_ohlc_evidence_version: str
    trade_ohlc_evidence_sha256: str
    trade_ohlc_availability_basis: str
    replay_run_fingerprint: str
    point_stream_sha256: str
    universe_id: str
    universe_version: str
    development_end_boundary_time_ms: int
    validation_end_boundary_time_ms: int
    code_revision: str
    candidate_output_sha256: str
    extension_run_fingerprint: str


@dataclass(frozen=True)
class HistoricalMarkTradeExtensionReport:
    manifest: MarkTradeExtensionManifest
    archive_manifest: object
    archive_diagnostics: object
    replay_manifest: object
    replay_diagnostics: object
    mark_source_coverage: tuple[Mapping, ...]
    trade_source_coverage: tuple[Mapping, ...]
    candidate_points: tuple[MarkTradePointOutput, ...]
    partition_summaries: Mapping[str, tuple[Mapping, ...]]
    report_sha256: str


def prepare_historical_mark_trade_extension(
    request: HistoricalMarkTradeExtensionRequest,
) -> HistoricalMarkTradeExtensionPrepared:
    """Load base replay and separated mark evidence once each."""
    if not isinstance(request, HistoricalMarkTradeExtensionRequest):
        raise ValueError("request must be HistoricalMarkTradeExtensionRequest")
    archive_dataset = load_binance_usdm_historical_replay_dataset(
        BinanceUSDMArchiveRequest(request.archive_root, request.universe,
                                  request.replay_config))
    replay_result = run_historical_market_replay(archive_dataset.replay_request)
    experiment_points = to_market_state_experiment_points(
        replay_result, request.partition_plan)
    mark_evidence = load_binance_usdm_mark_price_evidence(
        request.mark_archive_root, request.universe.symbols,
        request.replay_config.output_start_boundary_time_ms,
        request.replay_config.output_end_boundary_time_ms,
        download=request.download_mark_archives,
    )
    return HistoricalMarkTradeExtensionPrepared(
        archive_dataset, replay_result, experiment_points,
        request.partition_plan, mark_evidence, request.mark_archive_root,
    )


def _validate_prepared(prepared: HistoricalMarkTradeExtensionPrepared) -> None:
    if not isinstance(prepared, HistoricalMarkTradeExtensionPrepared):
        raise ValueError("prepared must be HistoricalMarkTradeExtensionPrepared")
    archive, replay, mark = (prepared.archive_dataset, prepared.replay_result,
                             prepared.mark_evidence)
    replay_manifest = replay.manifest
    symbols = replay_manifest.configured_universe
    if (archive.archive_manifest.dataset_id != replay_manifest.dataset_id
            or archive.archive_manifest.dataset_version != replay_manifest.dataset_version
            or archive.archive_manifest.content_sha256 != replay_manifest.dataset_content_sha256
            or archive.ohlc_evidence.configured_symbols != symbols
            or mark.configured_symbols != symbols
            or (mark.requested_start_boundary_time_ms,
                mark.requested_end_boundary_time_ms)
            != (replay_manifest.output_start_boundary_time_ms,
                replay_manifest.output_end_boundary_time_ms)
            or len(symbols) != _EXPECTED_SYMBOL_COUNT):
        raise ValueError("mark/trade evidence must match replay dataset and ordered symbols")
    trade_ohlc = archive.ohlc_evidence
    if ((trade_ohlc.dataset_id, trade_ohlc.dataset_version,
         trade_ohlc.dataset_content_sha256)
            != (replay_manifest.dataset_id, replay_manifest.dataset_version,
                replay_manifest.dataset_content_sha256)):
        raise ValueError("trade OHLC evidence identity differs from the V1 replay input")
    validate_experiment_points(prepared.experiment_points)
    if len(prepared.experiment_points) != len(replay.points):
        raise ValueError("replay and exported experiment stream lengths differ")
    for replay_point, experiment_point in zip(replay.points, prepared.experiment_points):
        expected_partition = (
            "development" if replay_point.evaluation_boundary_time_ms
            <= prepared.partition_plan.development_end_boundary_time_ms
            else "validation" if replay_point.evaluation_boundary_time_ms
            <= prepared.partition_plan.validation_end_boundary_time_ms else "test")
        if (experiment_point.movement_evaluation is not replay_point.movement_evaluation
                or experiment_point.partition != expected_partition):
            raise ValueError("extension must use the exact exported V1 replay point stream")
    experiment_stream_sha256(replay, prepared.experiment_points,
                             prepared.partition_plan)


def _v1_context(window) -> V1MarketStateContext:
    breadth = window.breadth
    def count(name):
        metric = getattr(breadth, name)
        return metric.value.count if breadth.available and metric.available else None
    return V1MarketStateContext(
        window.window_minutes, window.market_wide_eligible,
        window.eligible_count, window.eligible_fraction,
        window.aggregates.median_normalized_movement.value
        if window.aggregates.median_normalized_movement.available else None,
        window.aggregates.median_raw_return.value
        if window.aggregates.median_raw_return.available else None,
        breadth.available, breadth.reason, breadth.denominator,
        count("rising"), count("falling"), count("flat"),
    )


def _source_candle_checks(source: str, symbol: str, open_times: tuple[int, ...],
                          boundary: int, *, mark_evidence: BinanceMarkPriceEvidence,
                          mark_index: Mapping[tuple[str, int], object],
                          trade_index: Mapping[tuple[str, int], CompletedTradeOHLCCandle]):
    candles = []
    valid_times = []
    reasons = []
    index = mark_index if source == "MARK" else trade_index
    for opening in open_times:
        candle = index.get((symbol, opening))
        if candle is None:
            reason = (mark_evidence.unavailable_reason(symbol, opening)
                      if source == "MARK" else "TRADE_MISSING_MINUTE")
            reasons.append(MarkTradeUnavailableReason(source, opening, reason))
            continue
        if (candle.close_time_ms >= boundary
                or candle.first_seen_at_ms > boundary):
            reasons.append(MarkTradeUnavailableReason(
                source, opening,
                "MARK_CANDLE_NOT_AVAILABLE" if source == "MARK"
                else "TRADE_CANDLE_NOT_AVAILABLE"))
            continue
        candles.append(candle)
        valid_times.append(opening)
    return tuple(candles), tuple(valid_times), tuple(reasons)


def _symbol_window_output(symbol: str, window_minutes: int, boundary: int,
                          mark_evidence: BinanceMarkPriceEvidence,
                          mark_index: Mapping[tuple[str, int], object],
                          trade_index: Mapping[tuple[str, int], CompletedTradeOHLCCandle],
                          v1_symbol) -> MarkTradeSymbolWindowOutput:
    expected = tuple(range(boundary - (window_minutes + 1) * MINUTE_MS,
                           boundary, MINUTE_MS))
    mark_candles, mark_times, mark_reasons = _source_candle_checks(
        "MARK", symbol, expected, boundary, mark_evidence=mark_evidence,
        mark_index=mark_index, trade_index=trade_index)
    trade_candles, trade_times, trade_reasons = _source_candle_checks(
        "TRADE", symbol, expected, boundary, mark_evidence=mark_evidence,
        mark_index=mark_index, trade_index=trade_index)
    reasons = mark_reasons + trade_reasons
    ready = not reasons and len(mark_candles) == len(expected) == len(trade_candles)
    mark_start = mark_end = trade_start = trade_end = None
    mark_return = trade_return = start_basis = end_basis = divergence = None
    if ready:
        mark_start, mark_end = mark_candles[0].close, mark_candles[-1].close
        trade_start, trade_end = trade_candles[0].close, trade_candles[-1].close
        mark_return = _log_ratio(mark_end, mark_start)
        trade_return = _log_ratio(trade_end, trade_start)
        start_basis = _log_ratio(mark_start, trade_start)
        end_basis = _log_ratio(mark_end, trade_end)
        with localcontext() as context:
            context.prec = MARK_TRADE_DECIMAL_PRECISION
            divergence = end_basis - start_basis
    direction_metric = v1_symbol.direction
    return MarkTradeSymbolWindowOutput(
        symbol, window_minutes, "READY" if ready else "UNAVAILABLE",
        reasons, expected, mark_times,
        tuple(candle.close_time_ms for candle in mark_candles),
        trade_times,
        tuple(candle.close_time_ms for candle in trade_candles),
        _decimal_text(mark_start), _decimal_text(mark_end),
        _decimal_text(trade_start), _decimal_text(trade_end),
        _decimal_text(mark_return), _decimal_text(trade_return),
        _decimal_text(start_basis), _decimal_text(end_basis),
        _decimal_text(divergence), v1_symbol.included,
        direction_metric.value if direction_metric.available else None,
        v1_symbol.material_rising, v1_symbol.material_falling,
    )


def _build_candidate_points(prepared: HistoricalMarkTradeExtensionPrepared):
    mark = prepared.mark_evidence
    trade = prepared.archive_dataset.ohlc_evidence
    symbols = prepared.replay_result.manifest.configured_universe
    mark_index = {(item.symbol, item.open_time_ms): item for item in mark.candles}
    trade_index = {(item.symbol, item.open_time_ms): item for item in trade.candles}
    candidate_points = []
    for replay_point, experiment_point in zip(
            prepared.replay_result.points, prepared.experiment_points):
        boundary = replay_point.evaluation_boundary_time_ms
        if boundary % MINUTE_MS:
            continue
        windows = []
        evaluation = replay_point.movement_evaluation
        for window_minutes in MARK_TRADE_WINDOWS_MINUTES:
            v1_window = evaluation.windows[window_minutes]
            v1_symbols = {item.symbol: item for item in v1_window.symbols}
            outputs = tuple(_symbol_window_output(
                symbol, window_minutes, boundary, mark, mark_index, trade_index,
                v1_symbols[symbol]) for symbol in symbols)
            windows.append(MarkTradeWindowOutput(
                window_minutes, _v1_context(v1_window),
                all(item.status == "READY" for item in outputs), outputs))
        candidate_points.append(MarkTradePointOutput(
            replay_point.point_id, boundary, experiment_point.partition,
            tuple(windows)))
    return tuple(candidate_points)


def _trade_source_coverage(prepared) -> tuple[Mapping, ...]:
    mark = prepared.mark_evidence
    evidence = prepared.archive_dataset.ohlc_evidence
    result = []
    for symbol in evidence.configured_symbols:
        expected = range(mark.expected_open_time_start_ms,
                         mark.expected_open_time_end_ms_exclusive, MINUTE_MS)
        by_open = {item.open_time_ms: item for item in evidence.candles
                   if item.symbol == symbol}
        present = [opening for opening in expected if opening in by_open]
        missing = [opening for opening in expected if opening not in by_open]
        ranges = _minute_ranges(missing)
        result.append({
            "symbol": symbol,
            "expected_minute_count": mark.expected_minute_count,
            "observed_unique_verified_trade_ohlc_minute_count": len(present),
            "coverage_ratio": len(present) / mark.expected_minute_count,
            "missing_minute_count": len(missing),
            "missing_minute_ranges": ranges,
            "longest_contiguous_gap_minutes": max(
                (item["missing_minutes"] for item in ranges), default=0),
        })
    return tuple(result)


def _divergence_sign(value: str | None) -> str:
    if value is None:
        return "UNAVAILABLE"
    number = Decimal(value)
    return "POSITIVE" if number > 0 else "NEGATIVE" if number < 0 else "ZERO"


def _partition_summaries(candidate_points, symbols):
    summaries = {}
    for partition in REPORT_PARTITIONS:
        points = tuple(point for point in candidate_points
                       if partition == "all" or point.partition == partition)
        windows = []
        for window_minutes in MARK_TRADE_WINDOWS_MINUTES:
            point_windows = [next(item for item in point.windows
                                  if item.window_minutes == window_minutes)
                             for point in points]
            per_symbol = []
            for symbol in symbols:
                rows = [next(item for item in item_window.symbols
                             if item.symbol == symbol) for item_window in point_windows]
                ready_rows = [row for row in rows if row.status == "READY"]
                reason_counts = Counter(reason.reason for row in rows
                                         for reason in row.reasons)
                per_symbol.append({
                    "symbol": symbol,
                    "minute_point_count": len(rows),
                    "ready_count": len(ready_rows),
                    "unavailable_count": len(rows) - len(ready_rows),
                    "coverage_ratio": len(ready_rows) / len(rows) if rows else None,
                    "reason_counts": dict(sorted(reason_counts.items())),
                })
            common_windows = [item for item in point_windows
                              if item.all_configured_symbols_ready]
            common_rows = [row for item_window in common_windows
                           for row in item_window.symbols]
            mark_returns = [Decimal(row.mark_return) for row in common_rows]
            trade_returns = [Decimal(row.trade_return) for row in common_rows]
            start_bases = [Decimal(row.basis_at_start) for row in common_rows]
            end_bases = [Decimal(row.basis_at_end) for row in common_rows]
            divergences = [Decimal(row.divergence) for row in common_rows]
            crosstab = Counter((row.v1_direction or "UNAVAILABLE",
                                _divergence_sign(row.divergence))
                               for row in common_rows)
            windows.append({
                "window_minutes": window_minutes,
                "minute_point_count": len(points),
                "configured_symbol_window_count": len(points) * len(symbols),
                "ready_symbol_window_count": sum(
                    item["ready_count"] for item in per_symbol),
                "unavailable_symbol_window_count": sum(
                    item["unavailable_count"] for item in per_symbol),
                "all_configured_symbols_ready_boundary_count": len(common_windows),
                "all_configured_symbols_ready_coverage_ratio": (
                    len(common_windows) / len(points) if points else None),
                "per_symbol_coverage": tuple(per_symbol),
                "common_ready_diagnostic_summary": {
                    "symbol_window_observation_count": len(common_rows),
                    "median_mark_return": _decimal_text(_median(mark_returns)),
                    "mean_mark_return": _decimal_text(_mean(mark_returns)),
                    "median_trade_return": _decimal_text(_median(trade_returns)),
                    "mean_trade_return": _decimal_text(_mean(trade_returns)),
                    "median_basis_at_start": _decimal_text(_median(start_bases)),
                    "median_basis_at_end": _decimal_text(_median(end_bases)),
                    "median_divergence": _decimal_text(_median(divergences)),
                    "mean_divergence": _decimal_text(_mean(divergences)),
                    "positive_divergence_count": sum(value > 0 for value in divergences),
                    "negative_divergence_count": sum(value < 0 for value in divergences),
                    "zero_divergence_count": sum(value == 0 for value in divergences),
                    "same_time_v1_direction_by_divergence_sign": tuple(
                        {"v1_direction": direction, "divergence_sign": sign,
                         "count": count}
                        for (direction, sign), count in sorted(crosstab.items())),
                },
            })
        summaries[partition] = tuple(windows)
    return MappingProxyType(summaries)


def _extension_manifest(prepared, candidate_sha: str, code_revision: str):
    archive = prepared.archive_dataset
    replay = prepared.replay_result.manifest
    mark = prepared.mark_evidence
    manifest = MarkTradeExtensionManifest(
        MARK_TRADE_EXTENSION_SUITE_VERSION,
        MARK_TRADE_EXTENSION_REPORT_VERSION,
        MARK_TRADE_ALGORITHM_VERSION,
        MARK_TRADE_CONFIG_VERSION,
        MARK_TRADE_DECIMAL_PRECISION,
        MARK_TRADE_WINDOWS_MINUTES,
        "Decimal serialized as fixed-point string with trailing fractional zeros removed",
        "exact contiguous w-plus-one verified 1m closes required for each w-minute return",
        "exact UTC minute-boundary replay points only; no finalization grace",
        replay.configured_universe,
        replay.output_start_boundary_time_ms,
        replay.output_end_boundary_time_ms,
        mark.first_output_minute_boundary_time_ms,
        mark.last_output_minute_boundary_time_ms,
        mark.warmup_minutes,
        mark.expected_open_time_start_ms,
        mark.expected_open_time_end_ms_exclusive,
        MARK_PRICE_SOURCE,
        MARK_PRICE_TYPE,
        MARK_PRICE_INTERVAL,
        MARK_PRICE_SCHEMA_DESCRIPTION,
        MARK_PRICE_EVIDENCE_VERSION,
        MARK_PRICE_EVIDENCE_SCHEMA_VERSION,
        MARK_PRICE_AVAILABILITY_BASIS,
        mark.evidence_sha256,
        mark.packages,
        replay.dataset_id,
        replay.dataset_version,
        replay.dataset_content_sha256,
        OHLC_EVIDENCE_VERSION,
        archive.ohlc_evidence.evidence_sha256,
        OHLC_AVAILABILITY_BASIS,
        replay.run_fingerprint,
        experiment_stream_sha256(prepared.replay_result,
                                 prepared.experiment_points,
                                 prepared.partition_plan),
        replay.universe_id,
        replay.universe_version,
        prepared.partition_plan.development_end_boundary_time_ms,
        prepared.partition_plan.validation_end_boundary_time_ms,
        code_revision,
        candidate_sha,
        "",
    )
    identity = {key: value for key, value in report_json_safe(manifest).items()
                if key != "extension_run_fingerprint"}
    return replace(manifest, extension_run_fingerprint=_sha256(identity))


def build_historical_study_mark_trade_points(prepared: HistoricalMarkTradeExtensionPrepared):
    """Run mark/trade over an already prepared uniform-phase study day."""
    from .historical_market_state_candidate_evidence import validate_study_phase_prepared
    validate_study_phase_prepared(prepared)
    return _build_candidate_points(prepared)


def run_historical_mark_trade_extension(
    prepared: HistoricalMarkTradeExtensionPrepared,
    *, code_revision: str,
) -> HistoricalMarkTradeExtensionReport:
    _validate_prepared(prepared)
    if not isinstance(code_revision, str) or not code_revision:
        raise ValueError("mark/trade extension requires a code revision")
    candidate_points = _build_candidate_points(prepared)
    symbols = prepared.replay_result.manifest.configured_universe
    summaries = _partition_summaries(candidate_points, symbols)
    mark_coverage = prepared.mark_evidence.coverage_by_symbol()
    trade_coverage = _trade_source_coverage(prepared)
    candidate_sha = _sha256({
        "candidate_points": candidate_points,
        "mark_source_coverage": mark_coverage,
        "trade_source_coverage": trade_coverage,
        "partition_summaries": summaries,
    })
    manifest = _extension_manifest(prepared, candidate_sha, code_revision)
    report = HistoricalMarkTradeExtensionReport(
        manifest,
        prepared.archive_dataset.archive_manifest,
        prepared.archive_dataset.diagnostics,
        prepared.replay_result.manifest,
        prepared.replay_result.diagnostics,
        mark_coverage,
        trade_coverage,
        candidate_points,
        summaries,
        "",
    )
    return replace(report, report_sha256=_sha256({
        key: value for key, value in report_json_safe(report).items()
        if key != "report_sha256"
    }))


def run_historical_mark_trade_extension_from_archive(
    request: HistoricalMarkTradeExtensionRequest,
) -> HistoricalMarkTradeExtensionReport:
    prepared = prepare_historical_mark_trade_extension(request)
    return run_historical_mark_trade_extension(
        prepared, code_revision=request.code_revision)


def historical_mark_trade_extension_report_to_json(
    report: HistoricalMarkTradeExtensionReport,
) -> str:
    if not isinstance(report, HistoricalMarkTradeExtensionReport):
        raise ValueError("report must be HistoricalMarkTradeExtensionReport")
    expected = _sha256({key: value for key, value in report_json_safe(report).items()
                        if key != "report_sha256"})
    if report.report_sha256 != expected:
        raise ValueError("mark/trade report digest does not match canonical output")
    return _canonical_json(report)


def _current_code_revision() -> str:
    repository_root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        ["git", "-C", str(repository_root), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    )
    revision = completed.stdout.strip()
    if not revision:
        raise ValueError("could not determine code revision")
    return revision


def build_mark_trade_extension_cli_parser() -> argparse.ArgumentParser:
    parser = build_cli_parser()
    parser.description = "Run the separate historical Binance mark-versus-trade diagnostic"
    parser.add_argument("--mark-archive-root", required=True, type=Path,
                        help="separate Binance USD-M markPriceKlines archive root")
    parser.add_argument("--download-mark-archives", action="store_true",
                        help="explicitly fetch absent or invalid mark-price packages")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_mark_trade_extension_cli_parser()
    args = parser.parse_args(argv)
    if args.overwrite and args.output_json is None:
        parser.error("--overwrite requires --output-json")
    if args.output_json is not None and args.output_json.exists() and not args.overwrite:
        parser.error("output file already exists; pass --overwrite to replace it")
    try:
        request = HistoricalMarkTradeExtensionRequest(
            args.archive_root, args.mark_archive_root,
            MarketUniverseInput(args.universe_id, args.universe_version,
                                tuple(args.symbols)),
            HistoricalReplayConfig(args.start, args.end,
                                   args.finalization_grace_ms,
                                   MarketMovementConfig()),
            ReplayPartitionPlan(args.development_end, args.validation_end),
            args.code_revision or _current_code_revision(),
            args.download_mark_archives,
        )
        report = run_historical_mark_trade_extension_from_archive(request)
        content = historical_mark_trade_extension_report_to_json(report)
        if args.output_json is None:
            sys.stdout.write(content + "\n")
        else:
            _write_report(args.output_json, content, overwrite=args.overwrite)
    except (ValueError, TypeError, ArithmeticError, OSError,
            subprocess.CalledProcessError) as exc:
        print(f"historical mark/trade extension: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
