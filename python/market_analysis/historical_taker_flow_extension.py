"""Separate EXP-75-12 historical taker buy/sell imbalance extension.

This is a contemporaneous diagnostic over verified public aggTrades. It does
not alter V1 state decisions and does not estimate forward returns.
"""

from __future__ import annotations

import argparse
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
from .historical_experiment_batch import (
    _canonical_json, _sha256, _write_report, build_cli_parser,
    experiment_stream_sha256, report_json_safe,
)
from .experiments.market_state_common import validate_experiment_points
from .historical_replay import (
    HistoricalMarketReplayResult, HistoricalReplayConfig, ReplayPartitionPlan,
    run_historical_market_replay, to_market_state_experiment_points,
)
from .historical_taker_flow_evidence import (
    HistoricalTakerFlowEvidence, TAKER_FLOW_ALGORITHM_VERSION,
    TAKER_FLOW_CONFIG_VERSION, TAKER_FLOW_EVIDENCE_SCHEMA_VERSION,
    TAKER_FLOW_WINDOWS, TakerFlowWindowSums,
)
from .movement_metrics import MarketMovementConfig, MarketUniverseInput


TAKER_FLOW_EXTENSION_SUITE_VERSION = "historical-taker-flow-extension-suite-v1"
TAKER_FLOW_EXTENSION_REPORT_VERSION = "historical-taker-flow-extension-report-v1"
TAKER_FLOW_EXPERIMENT_ID = "EXP-75-12"
REPORT_PARTITIONS = ("development", "validation", "test", "all")
DECIMAL_DIVISION_PRECISION = 50


def _exact_sum(values) -> Decimal:
    values = tuple(values)
    if not values:
        return Decimal(0)
    exponent = min(value.as_tuple().exponent for value in values)
    adjusted_values = [value.adjusted() for value in values if value]
    adjusted = max(adjusted_values, default=0)
    precision = max(1, adjusted - exponent + 1 + len(str(len(values))))
    with localcontext() as context:
        context.prec = precision
        return sum(values, Decimal(0))


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator <= 0:
        raise ValueError("ratio denominator must be positive")
    with localcontext() as context:
        context.prec = DECIMAL_DIVISION_PRECISION
        return numerator / denominator


def _median(values) -> Decimal | None:
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return _ratio(_exact_sum((ordered[middle - 1], ordered[middle])), Decimal(2))


def _sign_flow(metric: "TakerFlowSymbolOutput") -> str | None:
    if metric.status != "ACTIVE" or metric.signed_net_quote_notional is None:
        return None
    if metric.signed_net_quote_notional > 0:
        return "BUY"
    if metric.signed_net_quote_notional < 0:
        return "SELL"
    return "BALANCED"


def _sign_v1(direction: str | None) -> str | None:
    return {"RISING": "BUY", "FALLING": "SELL", "FLAT": "BALANCED"}.get(direction)


@dataclass(frozen=True)
class HistoricalTakerFlowExtensionRequest:
    archive_root: Path
    universe: MarketUniverseInput
    replay_config: HistoricalReplayConfig
    partition_plan: ReplayPartitionPlan
    code_revision: str

    def __post_init__(self):
        archive = BinanceUSDMArchiveRequest(
            self.archive_root, self.universe, self.replay_config)
        object.__setattr__(self, "archive_root", archive.archive_root)
        if (not isinstance(self.partition_plan, ReplayPartitionPlan)
                or self.replay_config.movement_config != MarketMovementConfig()):
            raise ValueError(
                "taker flow extension requires a partition plan and canonical V1 movement config")
        if not isinstance(self.code_revision, str) or not self.code_revision:
            raise ValueError("taker flow extension requires an actual code_revision")


@dataclass(frozen=True)
class HistoricalTakerFlowExtensionPrepared:
    archive_dataset: BinanceHistoricalReplayDataset
    replay_result: HistoricalMarketReplayResult
    experiment_points: tuple
    partition_plan: ReplayPartitionPlan

    def __post_init__(self):
        if (not isinstance(self.archive_dataset, BinanceHistoricalReplayDataset)
                or not isinstance(self.replay_result, HistoricalMarketReplayResult)
                or not isinstance(self.partition_plan, ReplayPartitionPlan)):
            raise ValueError("taker flow preparation has invalid contract types")
        object.__setattr__(self, "experiment_points", tuple(self.experiment_points))


@dataclass(frozen=True)
class TakerFlowSymbolOutput:
    symbol: str
    window_minutes: int
    buy_quote_notional: Decimal | None
    sell_quote_notional: Decimal | None
    gross_quote_notional: Decimal | None
    signed_net_quote_notional: Decimal | None
    imbalance: Decimal | None
    buy_aggtrade_count: int | None
    sell_aggtrade_count: int | None
    total_aggtrade_count: int | None
    status: str
    reason: str | None
    v1_current_notional_volume: Decimal | None
    v1_direction: str | None
    parity_status: str


@dataclass(frozen=True)
class TakerFlowMarketSummary:
    window_minutes: int
    universe_size: int
    flow_available_symbol_count: int
    active_symbol_count: int
    no_observation_symbol_count: int
    unavailable_symbol_count: int
    partial_coverage: bool
    eligible_count: int
    median_active_symbol_imbalance: Decimal | None
    buy_sign_count: int
    sell_sign_count: int
    balanced_sign_count: int
    sign_breadth_denominator: int
    pooled_notional_imbalance: Decimal | None
    largest_active_symbol: str | None
    largest_active_symbol_gross_share: Decimal | None


@dataclass(frozen=True)
class TakerFlowComparisonDiagnostics:
    paired_symbol_count: int
    direction_agreement_count: int
    direction_agreement_fraction: Decimal | None
    flow_buy_count_on_common: int
    flow_sell_count_on_common: int
    flow_balanced_count_on_common: int
    v1_rising_count_on_common: int
    v1_falling_count_on_common: int
    v1_flat_count_on_common: int
    common_breadth_denominator: int
    v1_breadth_available: bool
    v1_breadth_denominator: int
    v1_rising_breadth_count: int | None
    v1_falling_breadth_count: int | None
    v1_flat_breadth_count: int | None
    parity_compared_symbol_count: int


@dataclass(frozen=True)
class TakerFlowWindowOutput:
    window_minutes: int
    symbols: tuple[TakerFlowSymbolOutput, ...]
    market_summary: TakerFlowMarketSummary
    comparison: TakerFlowComparisonDiagnostics


@dataclass(frozen=True)
class TakerFlowPointOutput:
    point_id: str
    evaluation_boundary_time_ms: int
    partition: str
    windows: tuple[TakerFlowWindowOutput, ...]


@dataclass(frozen=True)
class TakerFlowPartitionWindowSummary:
    partition: str
    window_minutes: int
    point_count: int
    universe_symbol_window_count: int
    flow_available_symbol_window_count: int
    active_symbol_window_count: int
    no_observation_symbol_window_count: int
    unavailable_symbol_window_count: int
    partial_coverage_point_count: int
    eligible_symbol_window_count: int
    flow_buy_sign_count: int
    flow_sell_sign_count: int
    flow_balanced_sign_count: int
    v1_breadth_available_point_count: int
    v1_breadth_denominator_total: int
    v1_rising_breadth_count_total: int
    v1_falling_breadth_count_total: int
    v1_flat_breadth_count_total: int
    paired_v1_direction_count: int
    v1_direction_agreement_count: int
    v1_direction_agreement_fraction: Decimal | None
    common_breadth_denominator: int
    flow_buy_count_on_common: int
    flow_sell_count_on_common: int
    flow_balanced_count_on_common: int
    v1_rising_count_on_common: int
    v1_falling_count_on_common: int
    v1_flat_count_on_common: int
    parity_compared_symbol_window_count: int


@dataclass(frozen=True)
class TakerFlowExtensionManifest:
    suite_version: str
    report_version: str
    experiment_id: str
    algorithm_version: str
    algorithm_config_version: str
    decimal_division_precision: int
    fixed_windows_minutes: tuple[int, ...]
    dataset_id: str
    dataset_version: str
    dataset_content_sha256: str
    flow_evidence_schema_version: str | None
    flow_evidence_sha256: str | None
    flow_availability_basis: str
    universe_id: str
    universe_version: str
    configured_universe: tuple[str, ...]
    output_start_boundary_time_ms: int
    output_end_boundary_time_ms: int
    finalization_grace_ms: int
    replay_algorithm_version: str
    replay_policy_version: str
    movement_algorithm_version: str
    movement_config_version: str
    movement_config_parameters: tuple[tuple[str, str], ...]
    replay_run_fingerprint: str
    experiment_stream_sha256: str
    development_end_boundary_time_ms: int
    validation_end_boundary_time_ms: int
    code_revision: str
    candidate_output_sha256: str
    extension_run_fingerprint: str


@dataclass(frozen=True)
class HistoricalTakerFlowExtensionReport:
    manifest: TakerFlowExtensionManifest
    archive_diagnostics: object
    replay_diagnostics: object
    candidate_points: tuple[TakerFlowPointOutput, ...]
    partition_summaries: Mapping[str, tuple[TakerFlowPartitionWindowSummary, ...]]
    report_sha256: str


def prepare_historical_taker_flow_extension(
    request: HistoricalTakerFlowExtensionRequest,
) -> HistoricalTakerFlowExtensionPrepared:
    """Load once with opt-in side evidence, replay once, and export once."""
    if not isinstance(request, HistoricalTakerFlowExtensionRequest):
        raise ValueError("request must be HistoricalTakerFlowExtensionRequest")
    archive_dataset = load_binance_usdm_historical_replay_dataset(
        BinanceUSDMArchiveRequest(
            request.archive_root, request.universe, request.replay_config),
        include_taker_flow_evidence=True,
    )
    replay_result = run_historical_market_replay(archive_dataset.replay_request)
    experiment_points = to_market_state_experiment_points(
        replay_result, request.partition_plan)
    return HistoricalTakerFlowExtensionPrepared(
        archive_dataset, replay_result, experiment_points, request.partition_plan)


def _validate_prepared(prepared: HistoricalTakerFlowExtensionPrepared) -> None:
    if not isinstance(prepared, HistoricalTakerFlowExtensionPrepared):
        raise ValueError("prepared data must be HistoricalTakerFlowExtensionPrepared")
    archive = prepared.archive_dataset
    replay = prepared.replay_result
    points = prepared.experiment_points
    replay_manifest = replay.manifest
    validate_experiment_points(points)
    if (not points or len(points) != len(replay.points)
            or archive.archive_manifest.dataset_id != replay_manifest.dataset_id
            or archive.archive_manifest.dataset_version != replay_manifest.dataset_version
            or archive.archive_manifest.content_sha256 != replay_manifest.dataset_content_sha256
            or archive.replay_request.universe.symbols != replay_manifest.configured_universe):
        raise ValueError("taker flow archive and replay identities disagree")
    evidence = archive.taker_flow_evidence
    if evidence is not None:
        if not evidence.matches_dataset(
                replay_manifest.dataset_id, replay_manifest.dataset_version,
                replay_manifest.dataset_content_sha256,
                replay_manifest.configured_universe):
            raise ValueError("taker flow evidence dataset or symbol order mismatch")
        if ((evidence.engine_start_boundary_time_ms,
             evidence.output_end_boundary_time_ms, evidence.finalization_grace_ms)
                != (archive.replay_request.config.engine_start_boundary_time_ms,
                    replay_manifest.output_end_boundary_time_ms,
                    replay_manifest.finalization_grace_ms)):
            raise ValueError("taker flow evidence range or finalization grace mismatch")
    for replay_point, experiment_point in zip(replay.points, points):
        boundary = replay_point.evaluation_boundary_time_ms
        expected = (
            "development" if boundary <= prepared.partition_plan.development_end_boundary_time_ms
            else "validation" if boundary <= prepared.partition_plan.validation_end_boundary_time_ms
            else "test"
        )
        if (experiment_point.movement_evaluation is not replay_point.movement_evaluation
                or experiment_point.source_time_evidence != replay_point.source_time_evidence
                or experiment_point.partition != expected):
            raise ValueError("taker flow extension must use the exact exported replay stream")
    experiment_stream_sha256(replay, points, prepared.partition_plan)


def _flow_symbol_output(symbol: str, window_minutes: int,
                         sums: TakerFlowWindowSums | None,
                         unavailable_reason: str | None, v1_symbol) -> TakerFlowSymbolOutput:
    v1_notional = (v1_symbol.current_notional_volume.value
                   if v1_symbol is not None
                   and v1_symbol.current_notional_volume.available else None)
    v1_direction = (v1_symbol.direction.value
                    if v1_symbol is not None and v1_symbol.direction.available else None)
    if unavailable_reason is not None or sums is None:
        return TakerFlowSymbolOutput(
            symbol, window_minutes, None, None, None, None, None, None, None, None,
            "FLOW_EVIDENCE_UNAVAILABLE", "FLOW_EVIDENCE_UNAVAILABLE",
            v1_notional, v1_direction, "FLOW_EVIDENCE_UNAVAILABLE",
        )
    buy = sums.buy_quote_notional
    sell = sums.sell_quote_notional
    gross = sums.gross_quote_notional
    net = _exact_sum((buy, -sell))
    buy_count, sell_count = sums.buy_aggtrade_count, sums.sell_aggtrade_count
    total_count = buy_count + sell_count
    if gross == 0:
        if total_count != 0:
            raise ValueError("zero flow notional has nonzero aggTrade record count")
        status, reason, imbalance = "NO_OBSERVED_AGGTRADES", "NO_OBSERVED_AGGTRADES", None
    else:
        if total_count <= 0:
            raise ValueError("positive flow notional has no aggTrade records")
        status, reason = "ACTIVE", None
        imbalance = _ratio(net, gross)
        if not Decimal(-1) <= imbalance <= Decimal(1):
            raise ValueError("taker flow imbalance exceeded [-1, +1]")
    if v1_notional is None:
        parity_status = "V1_NOTIONAL_UNAVAILABLE"
    else:
        if gross != v1_notional:
            raise ValueError(
                "evidence-integrity error: taker flow B + S does not equal "
                f"V1 current_notional_volume for {symbol} at window {window_minutes}m"
            )
        parity_status = "MATCH"
    return TakerFlowSymbolOutput(
        symbol, window_minutes, buy, sell, gross, net, imbalance,
        buy_count, sell_count, total_count, status, reason,
        v1_notional, v1_direction, parity_status,
    )


def _market_summary(window_minutes: int,
                    symbols: tuple[TakerFlowSymbolOutput, ...]) -> TakerFlowMarketSummary:
    active = tuple(item for item in symbols if item.status == "ACTIVE")
    no_observation = sum(item.status == "NO_OBSERVED_AGGTRADES" for item in symbols)
    unavailable = sum(item.status == "FLOW_EVIDENCE_UNAVAILABLE" for item in symbols)
    buy_count = sell_count = balanced_count = 0
    for item in active:
        side = _sign_flow(item)
        if side == "BUY":
            buy_count += 1
        elif side == "SELL":
            sell_count += 1
        else:
            balanced_count += 1
    active_gross = _exact_sum(item.gross_quote_notional for item in active)
    pooled_net = _exact_sum(item.signed_net_quote_notional for item in active)
    pooled = _ratio(pooled_net, active_gross) if active_gross > 0 else None
    largest = max(active, key=lambda item: item.gross_quote_notional) if active else None
    return TakerFlowMarketSummary(
        window_minutes, len(symbols), len(symbols) - unavailable,
        len(active), no_observation, unavailable, unavailable > 0, len(active),
        _median(item.imbalance for item in active), buy_count, sell_count,
        balanced_count, len(active), pooled,
        largest.symbol if largest is not None else None,
        _ratio(largest.gross_quote_notional, active_gross)
        if largest is not None and active_gross > 0 else None,
    )


def _comparison(window, symbol_outputs, v1_window) -> TakerFlowComparisonDiagnostics:
    signs = {"BUY": 0, "SELL": 0, "BALANCED": 0}
    v1_signs = {"BUY": 0, "SELL": 0, "BALANCED": 0}
    pair_count = agree = parity_count = 0
    for item in symbol_outputs:
        if item.parity_status == "MATCH":
            parity_count += 1
        flow_sign = _sign_flow(item)
        v1_sign = _sign_v1(item.v1_direction)
        if flow_sign is None or v1_sign is None:
            continue
        pair_count += 1
        signs[flow_sign] += 1
        v1_signs[v1_sign] += 1
        agree += flow_sign == v1_sign
    breadth = v1_window.breadth
    breadth_available = bool(breadth.available)
    breadth_values = {}
    if breadth_available:
        for field_name, side in (("rising", "rising"), ("falling", "falling"),
                                 ("flat", "flat")):
            metric = getattr(breadth, field_name)
            breadth_values[side] = metric.value.count if metric.available else None
    else:
        breadth_values = {"rising": None, "falling": None, "flat": None}
    return TakerFlowComparisonDiagnostics(
        pair_count, agree, _ratio(Decimal(agree), Decimal(pair_count))
        if pair_count else None,
        signs["BUY"], signs["SELL"], signs["BALANCED"],
        v1_signs["BUY"], v1_signs["SELL"], v1_signs["BALANCED"], pair_count,
        breadth_available, breadth.denominator,
        breadth_values["rising"], breadth_values["falling"],
        breadth_values["flat"], parity_count,
    )


def _build_candidate_points(prepared) -> tuple[TakerFlowPointOutput, ...]:
    evidence = prepared.archive_dataset.taker_flow_evidence
    symbols = prepared.replay_result.manifest.configured_universe
    output = []
    for replay_point, experiment_point in zip(
            prepared.replay_result.points, prepared.experiment_points):
        evaluation = replay_point.movement_evaluation
        window_outputs = []
        for window_minutes in TAKER_FLOW_WINDOWS:
            v1_window = evaluation.windows[window_minutes]
            v1_symbols = {item.symbol: item for item in v1_window.symbols}
            metrics = []
            for symbol in symbols:
                sums, reason = ((None, "FLOW_EVIDENCE_UNAVAILABLE") if evidence is None
                                else evidence.query_window(
                                    symbol, replay_point.evaluation_boundary_time_ms,
                                    window_minutes))
                metrics.append(_flow_symbol_output(
                    symbol, window_minutes, sums, reason, v1_symbols.get(symbol)))
            metrics = tuple(metrics)
            window_outputs.append(TakerFlowWindowOutput(
                window_minutes, metrics, _market_summary(window_minutes, metrics),
                _comparison(window_minutes, metrics, v1_window),
            ))
        output.append(TakerFlowPointOutput(
            replay_point.point_id, replay_point.evaluation_boundary_time_ms,
            experiment_point.partition, tuple(window_outputs),
        ))
    return tuple(output)


def _partition_summaries(candidate_points, symbols) -> Mapping[str, tuple[TakerFlowPartitionWindowSummary, ...]]:
    result = {}
    for partition in REPORT_PARTITIONS:
        selected = tuple(point for point in candidate_points
                         if partition == "all" or point.partition == partition)
        by_window = []
        for window in TAKER_FLOW_WINDOWS:
            rows = tuple(next(item for item in point.windows
                              if item.window_minutes == window) for point in selected)
            active = sum(item.market_summary.active_symbol_count for item in rows)
            no_observation = sum(item.market_summary.no_observation_symbol_count
                                 for item in rows)
            unavailable = sum(item.market_summary.unavailable_symbol_count for item in rows)
            available = active + no_observation
            universe_window_count = len(rows) * len(symbols)
            pair_count = sum(item.comparison.paired_symbol_count for item in rows)
            agreement = sum(item.comparison.direction_agreement_count for item in rows)
            common_count = sum(item.comparison.common_breadth_denominator for item in rows)
            breadth_points = sum(item.comparison.v1_breadth_available for item in rows)
            breadth_rows = tuple(item.comparison for item in rows
                                 if item.comparison.v1_breadth_available)
            by_window.append(TakerFlowPartitionWindowSummary(
                partition=partition,
                window_minutes=window,
                point_count=len(rows),
                universe_symbol_window_count=universe_window_count,
                flow_available_symbol_window_count=available,
                active_symbol_window_count=active,
                no_observation_symbol_window_count=no_observation,
                unavailable_symbol_window_count=unavailable,
                partial_coverage_point_count=sum(
                    item.market_summary.partial_coverage for item in rows),
                eligible_symbol_window_count=active,
                flow_buy_sign_count=sum(item.market_summary.buy_sign_count for item in rows),
                flow_sell_sign_count=sum(item.market_summary.sell_sign_count for item in rows),
                flow_balanced_sign_count=sum(
                    item.market_summary.balanced_sign_count for item in rows),
                v1_breadth_available_point_count=breadth_points,
                v1_breadth_denominator_total=sum(
                    item.v1_breadth_denominator for item in breadth_rows),
                v1_rising_breadth_count_total=sum(
                    item.v1_rising_breadth_count or 0 for item in breadth_rows),
                v1_falling_breadth_count_total=sum(
                    item.v1_falling_breadth_count or 0 for item in breadth_rows),
                v1_flat_breadth_count_total=sum(
                    item.v1_flat_breadth_count or 0 for item in breadth_rows),
                paired_v1_direction_count=pair_count,
                v1_direction_agreement_count=agreement,
                v1_direction_agreement_fraction=(
                    _ratio(Decimal(agreement), Decimal(pair_count))
                    if pair_count else None),
                common_breadth_denominator=common_count,
                flow_buy_count_on_common=sum(
                    item.comparison.flow_buy_count_on_common for item in rows),
                flow_sell_count_on_common=sum(
                    item.comparison.flow_sell_count_on_common for item in rows),
                flow_balanced_count_on_common=sum(
                    item.comparison.flow_balanced_count_on_common for item in rows),
                v1_rising_count_on_common=sum(
                    item.comparison.v1_rising_count_on_common for item in rows),
                v1_falling_count_on_common=sum(
                    item.comparison.v1_falling_count_on_common for item in rows),
                v1_flat_count_on_common=sum(
                    item.comparison.v1_flat_count_on_common for item in rows),
                parity_compared_symbol_window_count=sum(
                    item.comparison.parity_compared_symbol_count for item in rows),
            ))
        result[partition] = tuple(by_window)
    return MappingProxyType(result)


def _candidate_output_sha256(candidate_points, partition_summaries) -> str:
    return _sha256({
        "point_outputs_in_order": candidate_points,
        "partition_summaries": partition_summaries,
    })


def _manifest(prepared, candidate_output_sha256, code_revision):
    archive = prepared.archive_dataset
    replay = prepared.replay_result.manifest
    evidence = archive.taker_flow_evidence
    manifest = TakerFlowExtensionManifest(
        TAKER_FLOW_EXTENSION_SUITE_VERSION,
        TAKER_FLOW_EXTENSION_REPORT_VERSION,
        TAKER_FLOW_EXPERIMENT_ID,
        TAKER_FLOW_ALGORITHM_VERSION,
        TAKER_FLOW_CONFIG_VERSION,
        DECIMAL_DIVISION_PRECISION,
        TAKER_FLOW_WINDOWS,
        replay.dataset_id, replay.dataset_version, replay.dataset_content_sha256,
        evidence.schema_version if evidence is not None else None,
        evidence.evidence_sha256 if evidence is not None else None,
        evidence.availability_basis if evidence is not None
        else "exchange-timestamp-surrogate-v1",
        replay.universe_id, replay.universe_version, replay.configured_universe,
        replay.output_start_boundary_time_ms, replay.output_end_boundary_time_ms,
        replay.finalization_grace_ms, replay.algorithm_version, replay.policy_version,
        replay.movement_algorithm_version, replay.movement_config_version,
        replay.movement_config_parameters, replay.run_fingerprint,
        experiment_stream_sha256(
            prepared.replay_result, prepared.experiment_points,
            prepared.partition_plan),
        prepared.partition_plan.development_end_boundary_time_ms,
        prepared.partition_plan.validation_end_boundary_time_ms,
        code_revision, candidate_output_sha256, "",
    )
    identity = {key: value for key, value in report_json_safe(manifest).items()
                if key != "extension_run_fingerprint"}
    return replace(manifest, extension_run_fingerprint=_sha256(identity))


def run_historical_taker_flow_extension(
    prepared: HistoricalTakerFlowExtensionPrepared,
    *, code_revision: str,
) -> HistoricalTakerFlowExtensionReport:
    """Produce one separate EXP-75-12 diagnostic report for a prepared replay."""
    _validate_prepared(prepared)
    if not isinstance(code_revision, str) or not code_revision:
        raise ValueError("taker flow extension requires an actual code_revision")
    candidate_points = _build_candidate_points(prepared)
    summaries = _partition_summaries(
        candidate_points, prepared.replay_result.manifest.configured_universe)
    candidate_sha = _candidate_output_sha256(candidate_points, summaries)
    manifest = _manifest(prepared, candidate_sha, code_revision)
    report = HistoricalTakerFlowExtensionReport(
        manifest, prepared.archive_dataset.diagnostics,
        prepared.replay_result.diagnostics, candidate_points, summaries, "",
    )
    report_sha = _sha256({
        key: value for key, value in report_json_safe(report).items()
        if key != "report_sha256"
    })
    return replace(report, report_sha256=report_sha)


def run_historical_taker_flow_extension_from_archive(
    request: HistoricalTakerFlowExtensionRequest,
) -> HistoricalTakerFlowExtensionReport:
    prepared = prepare_historical_taker_flow_extension(request)
    return run_historical_taker_flow_extension(
        prepared, code_revision=request.code_revision)


def historical_taker_flow_extension_report_to_json(
    report: HistoricalTakerFlowExtensionReport,
) -> str:
    if not isinstance(report, HistoricalTakerFlowExtensionReport):
        raise ValueError("report must be HistoricalTakerFlowExtensionReport")
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


def build_taker_flow_extension_cli_parser() -> argparse.ArgumentParser:
    parser = build_cli_parser()
    parser.description = "Run the separate EXP-75-12 taker buy/sell imbalance extension"
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_taker_flow_extension_cli_parser()
    args = parser.parse_args(argv)
    if args.overwrite and args.output_json is None:
        parser.error("--overwrite requires --output-json")
    if args.output_json is not None and args.output_json.exists() and not args.overwrite:
        parser.error("output file already exists; pass --overwrite to replace it")
    try:
        request = HistoricalTakerFlowExtensionRequest(
            args.archive_root,
            MarketUniverseInput(args.universe_id, args.universe_version,
                                tuple(args.symbols)),
            HistoricalReplayConfig(
                args.start, args.end, args.finalization_grace_ms,
                MarketMovementConfig(),
            ),
            ReplayPartitionPlan(args.development_end, args.validation_end),
            args.code_revision or _current_code_revision(),
        )
        report = run_historical_taker_flow_extension_from_archive(request)
        content = historical_taker_flow_extension_report_to_json(report)
        if args.output_json is None:
            sys.stdout.write(content + "\n")
        else:
            _write_report(args.output_json, content, overwrite=args.overwrite)
    except (ValueError, TypeError, ArithmeticError, OSError,
            subprocess.CalledProcessError) as exc:
        print(f"historical taker flow extension: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
