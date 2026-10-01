"""Separate EXP-75-06B historical extension over one prepared replay stream."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, fields, is_dataclass, replace
import hashlib
import math
from pathlib import Path
from statistics import median
import subprocess
import sys
from types import MappingProxyType
from typing import Any, Mapping

from .binance_historical_archive import (
    BinanceHistoricalReplayDataset, BinanceUSDMArchiveRequest,
    load_binance_usdm_historical_replay_dataset,
)
from .experiments.market_state_atr_normalization import (
    ATR_ALGORITHM_VERSION, ATR_CONFIGURATIONS,
    run_market_state_atr_normalization_suite,
)
from .experiments.market_state_common import validate_experiment_points
from .experiments.market_state_realized_vol_normalization import (
    REALIZED_VOLATILITY_ALGORITHM_VERSION, REALIZED_VOL_CONFIGURATIONS,
    run_market_state_realized_vol_normalization_experiment,
)
from .historical_experiment_batch import (
    _canonical_json, _parameters, _sha256, _write_report, build_cli_parser,
    experiment_stream_sha256, report_json_safe,
)
from .historical_ohlc_evidence import OHLC_AVAILABILITY_BASIS
from .historical_replay import (
    HistoricalMarketReplayResult, HistoricalReplayConfig, ReplayPartitionPlan,
    run_historical_market_replay, to_market_state_experiment_points,
)
from .market_episode_lifecycle import (
    ALGORITHM_VERSION as LIFECYCLE_ALGORITHM_VERSION,
    MarketEpisodeLifecycleConfig,
)
from .movement_classifier import (
    ALGORITHM_VERSION as CLASSIFIER_ALGORITHM_VERSION,
    MarketClassifierConfig,
)
from .movement_metrics import MarketMovementConfig, MarketUniverseInput, WINDOWS


ATR_EXTENSION_SUITE_VERSION = "historical-atr-extension-suite-v1"
ATR_EXTENSION_REPORT_VERSION = "historical-atr-extension-report-v1"
REPORT_PARTITIONS = ("development", "validation", "test", "all")
WARMUP_ASYMMETRY = (
    "06A starts cold at the first output point; 06B uses causally visible "
    "pre-output archive candles."
)


@dataclass(frozen=True)
class HistoricalATRExtensionRequest:
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
            raise ValueError("ATR extension requires a partition plan and canonical V1 movement config")
        if not isinstance(self.code_revision, str) or not self.code_revision:
            raise ValueError("ATR extension requires an actual code_revision")


@dataclass(frozen=True)
class HistoricalATRExtensionPrepared:
    archive_dataset: BinanceHistoricalReplayDataset
    replay_result: HistoricalMarketReplayResult
    experiment_points: tuple
    partition_plan: ReplayPartitionPlan

    def __post_init__(self):
        if (not isinstance(self.archive_dataset, BinanceHistoricalReplayDataset)
                or not isinstance(self.replay_result, HistoricalMarketReplayResult)
                or not isinstance(self.partition_plan, ReplayPartitionPlan)):
            raise ValueError("ATR extension preparation has invalid contract types")
        object.__setattr__(self, "experiment_points", tuple(self.experiment_points))


@dataclass(frozen=True)
class ATRThreeWaySummary:
    partition: str
    evaluation_count: int
    individual_ready_by_symbol_window: tuple[tuple[str, int, int, int, int, int], ...]
    common_ready_boundary_count_by_window: tuple[tuple[int, int], ...]
    median_06a_rms_by_symbol: tuple[tuple[str, float | None], ...]
    median_06a_scale_by_symbol_window: tuple[tuple[str, int, float | None], ...]
    median_06b_to_06a_scale_ratio_by_symbol_window: tuple[
        tuple[str, int, float | None], ...
    ]
    median_abs_score_by_method_window: tuple[tuple[str, int, float | None], ...]
    median_abs_score_difference_by_pair_window: tuple[
        tuple[str, int, float | None], ...
    ]
    median_abs_material_rising_breadth_difference_06a_06b_by_window: tuple[
        tuple[int, float | None], ...
    ]
    median_abs_material_falling_breadth_difference_06a_06b_by_window: tuple[
        tuple[int, float | None], ...
    ]
    outlier_disagreement_06a_06b_by_window: tuple[tuple[int, int], ...]
    direction_state_disagreement_by_pair_window: tuple[
        tuple[str, int, int], ...
    ]
    full_stream_lifecycle: Mapping[str, Any]


@dataclass(frozen=True)
class ATRExtensionManifest:
    suite_version: str
    report_version: str
    dataset_id: str
    dataset_version: str
    dataset_content_sha256: str
    ohlc_evidence_version: str
    ohlc_evidence_sha256: str
    ohlc_availability_basis: str
    universe_id: str
    universe_version: str
    configured_universe: tuple[str, ...]
    replay_run_fingerprint: str
    experiment_stream_sha256: str
    development_end_boundary_time_ms: int
    validation_end_boundary_time_ms: int
    classifier_algorithm_version: str
    classifier_config_version: str
    classifier_config_parameters: Mapping[str, Any]
    lifecycle_algorithm_version: str
    lifecycle_config_version: str
    lifecycle_config_parameters: Mapping[str, Any]
    candidate_algorithm_version: str
    candidate_config_versions: tuple[str, ...]
    comparison_algorithm_version: str
    comparison_config_versions: tuple[str, ...]
    candidate_output_evidence_sha256: str
    extension_run_fingerprint: str


@dataclass(frozen=True)
class ATRExtensionRunReport:
    experiment_id: str
    algorithm_version: str
    config_version: str
    config_parameters: Mapping[str, Any]
    comparison_06a_algorithm_version: str
    comparison_06a_config_version: str
    candidate_output_sha256: str
    atr_summaries: Mapping[str, Any]
    realized_vol_summaries: Mapping[str, Any]
    three_way_summaries: Mapping[str, ATRThreeWaySummary]
    result_sha256: str


@dataclass(frozen=True)
class HistoricalATRExtensionReport:
    manifest: ATRExtensionManifest
    archive_diagnostics: object
    replay_diagnostics: object
    runs: tuple[ATRExtensionRunReport, ...]
    code_revision: str
    warmup_asymmetry: str
    report_sha256: str


def prepare_historical_atr_extension(
    request: HistoricalATRExtensionRequest,
) -> HistoricalATRExtensionPrepared:
    """Load the verified archive, replay, and export exactly once."""
    if not isinstance(request, HistoricalATRExtensionRequest):
        raise ValueError("request must be HistoricalATRExtensionRequest")
    archive_dataset = load_binance_usdm_historical_replay_dataset(
        BinanceUSDMArchiveRequest(
            request.archive_root, request.universe, request.replay_config))
    replay_result = run_historical_market_replay(archive_dataset.replay_request)
    experiment_points = to_market_state_experiment_points(
        replay_result, request.partition_plan)
    return HistoricalATRExtensionPrepared(
        archive_dataset, replay_result, experiment_points, request.partition_plan)


def _validate_prepared(prepared: HistoricalATRExtensionPrepared) -> None:
    if not isinstance(prepared, HistoricalATRExtensionPrepared):
        raise ValueError("prepared data must be HistoricalATRExtensionPrepared")
    replay = prepared.replay_result.manifest
    archive = prepared.archive_dataset
    points = prepared.experiment_points
    validate_experiment_points(points)
    if not points or len(points) != len(prepared.replay_result.points):
        raise ValueError("ATR extension requires every replay point")
    if ((archive.archive_manifest.dataset_id,
         archive.archive_manifest.dataset_version,
         archive.archive_manifest.content_sha256)
            != (replay.dataset_id, replay.dataset_version,
                replay.dataset_content_sha256)
            or archive.replay_request.universe.symbols != replay.configured_universe):
        raise ValueError("ATR extension archive and replay identities disagree")
    for replay_point, experiment_point in zip(prepared.replay_result.points, points):
        boundary = replay_point.evaluation_boundary_time_ms
        expected = (
            "development" if boundary <= prepared.partition_plan.development_end_boundary_time_ms
            else "validation" if boundary <= prepared.partition_plan.validation_end_boundary_time_ms
            else "test"
        )
        if (experiment_point.movement_evaluation is not replay_point.movement_evaluation
                or experiment_point.source_time_evidence != replay_point.source_time_evidence
                or experiment_point.partition != expected):
            raise ValueError("ATR extension must use the exact exported replay stream")
    # This validates point IDs, chronological boundary pairing, and partition cutoffs.
    experiment_stream_sha256(prepared.replay_result, points, prepared.partition_plan)


def _median(values) -> float | None:
    return float(median(values)) if values else None


def _three_way_summary(atr_result, rv_result, partition: str) -> ATRThreeWaySummary:
    atr_points = atr_result.paired_points
    rv_points = rv_result.paired_points
    if len(atr_points) != len(rv_points):
        raise ValueError("06A and 06B point counts differ")
    symbols = atr_points[0].baseline_evaluation.configured_universe
    counts = Counter()
    common_boundaries = Counter()
    rms = {symbol: [] for symbol in symbols}
    rv_scales = {(symbol, minute): [] for symbol in symbols for minute in WINDOWS}
    ratios = {(symbol, minute): [] for symbol in symbols for minute in WINDOWS}
    score_abs = {(method, minute): [] for method in ("V1", "06A", "06B")
                 for minute in WINDOWS}
    score_differences = {(pair, minute): [] for pair in ("V1/06A", "V1/06B", "06A/06B")
                         for minute in WINDOWS}
    rising = {minute: [] for minute in WINDOWS}
    falling = {minute: [] for minute in WINDOWS}
    outlier_disagreement = Counter()
    state_disagreement = Counter()
    selected_count = 0
    for atr_point, rv_point in zip(atr_points, rv_points):
        if (atr_point.evaluation_boundary_time_ms != rv_point.evaluation_boundary_time_ms
                or atr_point.partition != rv_point.partition
                or atr_point.baseline_evaluation is not rv_point.baseline_evaluation):
            raise ValueError("06A and 06B must use identical canonical points")
        if partition != "all" and atr_point.partition != partition:
            continue
        selected_count += 1
        atr_evidence = {item.symbol: item for item in atr_point.symbol_evidence}
        rv_evidence = {item.symbol: item for item in rv_point.symbol_evidence}
        for symbol in symbols:
            rv_sigma = rv_evidence[symbol].sigma_1m
            if rv_sigma.available:
                rms[symbol].append(rv_sigma.value)
            for minute in WINDOWS:
                rv_scale = dict(rv_evidence[symbol].sigma_by_window)[minute]
                atr_scale = dict(atr_evidence[symbol].calibrated_scales)[minute]
                if rv_scale.available:
                    rv_scales[symbol, minute].append(rv_scale.value)
                if (rv_scale.available and atr_scale.available
                        and rv_scale.value > 0):
                    ratio = atr_scale.value / rv_scale.value
                    if math.isfinite(ratio):
                        ratios[symbol, minute].append(ratio)
                v1 = next(item for item in atr_point.baseline_evaluation.windows[minute].symbols
                          if item.symbol == symbol)
                rv = next(item for item in rv_point.candidate_evaluation.windows[minute].symbols
                          if item.symbol == symbol)
                atr = next(item for item in atr_point.candidate_evaluation.windows[minute].symbols
                           if item.symbol == symbol)
                ready = tuple(item.included and item.normalized_z.available
                              for item in (v1, rv, atr))
                for method, available in zip(("V1", "06A", "06B"), ready):
                    counts[symbol, minute, method] += bool(available)
                if all(ready):
                    counts[symbol, minute, "COMMON"] += 1
                    values = (v1.normalized_z.value, rv.normalized_z.value,
                              atr.normalized_z.value)
                    for method, value in zip(("V1", "06A", "06B"), values):
                        score_abs[method, minute].append(abs(value))
                    for pair, left, right in (
                        ("V1/06A", values[0], values[1]),
                        ("V1/06B", values[0], values[2]),
                        ("06A/06B", values[1], values[2]),
                    ):
                        score_differences[pair, minute].append(abs(left - right))
        for minute in WINDOWS:
            windows = (
                atr_point.baseline_evaluation.windows[minute],
                rv_point.candidate_evaluation.windows[minute],
                atr_point.candidate_evaluation.windows[minute],
            )
            if not all(window.breadth.available for window in windows):
                continue
            common_boundaries[minute] += 1
            rv_breadth, atr_breadth = windows[1].breadth, windows[2].breadth
            for side, target in (("material_rising", rising), ("material_falling", falling)):
                target[minute].append(abs(
                    getattr(rv_breadth, side).value.fraction
                    - getattr(atr_breadth, side).value.fraction))
            outlier_disagreement[minute] += sum(
                left.outlier_candidate != right.outlier_candidate
                for left, right in zip(windows[1].symbols, windows[2].symbols)
                if left.included and right.included
            )
            states = (
                atr_point.baseline_classification.windows[minute].direction_state,
                rv_point.candidate_classification.windows[minute].direction_state,
                atr_point.candidate_classification.windows[minute].direction_state,
            )
            if any(state in ("WARMING", "UNAVAILABLE") for state in states):
                continue
            for pair, left, right in (
                ("V1/06A", states[0], states[1]),
                ("V1/06B", states[0], states[2]),
                ("06A/06B", states[1], states[2]),
            ):
                state_disagreement[pair, minute] += left != right
    rv_summary = rv_result.summaries[partition]
    atr_summary = atr_result.summaries[partition].v1_candidate_comparison
    lifecycle = MappingProxyType({
        "V1": {
            "transitions": rv_summary.baseline_transition_counts,
            "episodes": rv_summary.baseline_episode_count,
            "short_closed_episodes": rv_summary.baseline_short_lived_closed_episode_count,
            "directional_onsets": rv_summary.baseline_directional_onset_count,
        },
        "06A": {
            "transitions": rv_summary.candidate_transition_counts,
            "episodes": rv_summary.candidate_episode_count,
            "short_closed_episodes": rv_summary.candidate_short_lived_closed_episode_count,
            "directional_onsets": rv_summary.candidate_directional_onset_count,
            "matched_v1_onsets": rv_summary.matched_baseline_onset_count,
            "unmatched_v1_onsets": rv_summary.unmatched_baseline_onset_count,
            "median_signed_minus_v1_onset_ms":
                rv_summary.median_signed_candidate_minus_baseline_onset_ms,
        },
        "06B": {
            "transitions": atr_summary.candidate_transition_counts,
            "episodes": atr_summary.candidate_episode_count,
            "short_closed_episodes": atr_summary.candidate_short_lived_closed_episode_count,
            "directional_onsets": atr_summary.candidate_directional_onset_count,
            "matched_v1_onsets": atr_summary.matched_baseline_onset_count,
            "unmatched_v1_onsets": atr_summary.unmatched_baseline_onset_count,
            "median_signed_minus_v1_onset_ms":
                atr_summary.median_signed_candidate_minus_baseline_onset_ms,
        },
    })
    return ATRThreeWaySummary(
        partition, selected_count,
        tuple((symbol, minute, counts[symbol, minute, "V1"],
               counts[symbol, minute, "06A"], counts[symbol, minute, "06B"],
               counts[symbol, minute, "COMMON"])
              for symbol in symbols for minute in WINDOWS),
        tuple((minute, common_boundaries[minute]) for minute in WINDOWS),
        tuple((symbol, _median(rms[symbol])) for symbol in symbols),
        tuple((symbol, minute, _median(rv_scales[symbol, minute]))
              for symbol in symbols for minute in WINDOWS),
        tuple((symbol, minute, _median(ratios[symbol, minute]))
              for symbol in symbols for minute in WINDOWS),
        tuple((method, minute, _median(score_abs[method, minute]))
              for method in ("V1", "06A", "06B") for minute in WINDOWS),
        tuple((pair, minute, _median(score_differences[pair, minute]))
              for pair in ("V1/06A", "V1/06B", "06A/06B") for minute in WINDOWS),
        tuple((minute, _median(rising[minute])) for minute in WINDOWS),
        tuple((minute, _median(falling[minute])) for minute in WINDOWS),
        tuple((minute, outlier_disagreement[minute]) for minute in WINDOWS),
        tuple((pair, minute, state_disagreement[pair, minute])
              for pair in ("V1/06A", "V1/06B", "06A/06B") for minute in WINDOWS),
        lifecycle,
    )


def _candidate_output_sha256(atr_result) -> str:
    digest = hashlib.sha256()
    for point in atr_result.paired_points:
        payload = {
            "boundary": point.evaluation_boundary_time_ms,
            "partition": point.partition,
            "symbol_evidence": point.symbol_evidence,
            # These immutable canonical values include every evaluation window
            # (symbols, breadth, aggregates, and metadata), every classification
            # field, and the complete resulting lifecycle state.
            "candidate_evaluation": point.candidate_evaluation,
            "candidate_classification": point.candidate_classification,
            "candidate_lifecycle_state": point.candidate_lifecycle_state,
            "candidate_transitions": point.candidate_transitions,
        }
        digest.update((_canonical_json(_atr_json_safe(payload)) + "\n").encode("utf-8"))
    return digest.hexdigest()


def _atr_json_safe(value):
    """Encode ATR dataclasses without losing non-string mapping keys.

    String-key mappings retain the normal report object form. A mapping with
    non-string keys becomes an ordered list of key/value records, so the real
    integer-keyed movement windows remain distinct and stable in hashes/JSON.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _atr_json_safe(getattr(value, item.name))
                for item in fields(value)}
    if isinstance(value, Mapping):
        if all(isinstance(key, str) for key in value):
            return {key: _atr_json_safe(value[key]) for key in sorted(value)}
        entries = [(_atr_mapping_key_json(key), _atr_json_safe(item))
                   for key, item in value.items()]
        entries.sort(key=lambda entry: _atr_mapping_order_key(entry[0]))
        return [[key, item] for key, item in entries]
    if isinstance(value, (tuple, list)):
        return [_atr_json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_atr_json_safe(item) for item in value]
        return sorted(items, key=_canonical_json)
    return report_json_safe(value)


def _atr_mapping_key_json(key):
    """Keep uncommon non-JSON mapping-key types explicit and distinguishable."""
    if key is None or type(key) in (str, bool, int, float):
        return report_json_safe(key)
    if is_dataclass(key) and not isinstance(key, type):
        type_name = f"{type(key).__module__}.{type(key).__qualname__}"
    else:
        type_name = f"{type(key).__module__}.{type(key).__qualname__}"
    return {"key_type": type_name, "key_value": _atr_json_safe(key)}


def _atr_mapping_order_key(key):
    if type(key) is int:
        return 0, key
    return 1, _canonical_json(key)


def _atr_sha256(value) -> str:
    return hashlib.sha256(
        _canonical_json(_atr_json_safe(value)).encode("utf-8")
    ).hexdigest()


def _manifest(prepared, classifier_config, lifecycle_config, output_sha):
    archive = prepared.archive_dataset
    replay = prepared.replay_result.manifest
    ohlc = archive.ohlc_evidence
    manifest = ATRExtensionManifest(
        ATR_EXTENSION_SUITE_VERSION, ATR_EXTENSION_REPORT_VERSION,
        replay.dataset_id, replay.dataset_version, replay.dataset_content_sha256,
        ohlc.evidence_version, ohlc.evidence_sha256, OHLC_AVAILABILITY_BASIS,
        replay.universe_id, replay.universe_version, replay.configured_universe,
        replay.run_fingerprint,
        experiment_stream_sha256(
            prepared.replay_result, prepared.experiment_points, prepared.partition_plan),
        prepared.partition_plan.development_end_boundary_time_ms,
        prepared.partition_plan.validation_end_boundary_time_ms,
        CLASSIFIER_ALGORITHM_VERSION, classifier_config.version,
        _parameters(classifier_config),
        LIFECYCLE_ALGORITHM_VERSION, lifecycle_config.version,
        _parameters(lifecycle_config),
        ATR_ALGORITHM_VERSION, tuple(config.version for config in ATR_CONFIGURATIONS),
        REALIZED_VOLATILITY_ALGORITHM_VERSION,
        tuple(config.version for config in REALIZED_VOL_CONFIGURATIONS),
        output_sha, "",
    )
    # Candidate output is a separate result digest, not an input identity.
    identity = {key: value for key, value in report_json_safe(manifest).items()
                if key not in ("extension_run_fingerprint", "candidate_output_evidence_sha256")}
    return replace(manifest, extension_run_fingerprint=_sha256(identity))


def run_historical_atr_extension(
    prepared: HistoricalATRExtensionPrepared,
    *,
    code_revision: str,
) -> HistoricalATRExtensionReport:
    """Consume already prepared data; leave the fixed 28-run suite untouched."""
    _validate_prepared(prepared)
    if not isinstance(code_revision, str) or not code_revision:
        raise ValueError("ATR extension requires an actual code_revision")
    classifier_config = MarketClassifierConfig()
    lifecycle_config = MarketEpisodeLifecycleConfig()
    atr_results = list(run_market_state_atr_normalization_suite(
        prepared.experiment_points,
        prepared.archive_dataset.ohlc_evidence,
        prepared.replay_result.manifest,
        classifier_config=classifier_config,
        lifecycle_config=lifecycle_config,
    ))
    if len(atr_results) != len(REALIZED_VOL_CONFIGURATIONS):
        raise ValueError("ATR and realized-volatility config counts disagree")
    run_reports = []
    output_digests = []
    for index, (atr_config, rv_config) in enumerate(zip(
            ATR_CONFIGURATIONS, REALIZED_VOL_CONFIGURATIONS)):
        atr_result = atr_results[index]
        if atr_result.atr_config != atr_config:
            raise ValueError("ATR result configuration order disagrees with extension suite")
        rv_result = run_market_state_realized_vol_normalization_experiment(
            prepared.experiment_points, rv_config,
            classifier_config=classifier_config,
            lifecycle_config=lifecycle_config,
        )
        three_way = MappingProxyType({
            partition: _three_way_summary(atr_result, rv_result, partition)
            for partition in REPORT_PARTITIONS
        })
        atr_summaries = MappingProxyType({
            partition: atr_result.summaries[partition] for partition in REPORT_PARTITIONS
        })
        rv_summaries = MappingProxyType({
            partition: rv_result.summaries[partition] for partition in REPORT_PARTITIONS
        })
        output_sha = _candidate_output_sha256(atr_result)
        output_digests.append((atr_config.version, output_sha))
        payload = {
            "experiment_id": "EXP-75-06B",
            "algorithm_version": ATR_ALGORITHM_VERSION,
            "config_version": atr_config.version,
            "config_parameters": _parameters(atr_config),
            "comparison_06a_algorithm_version": REALIZED_VOLATILITY_ALGORITHM_VERSION,
            "comparison_06a_config_version": rv_config.version,
            "candidate_output_sha256": output_sha,
            "atr_summaries": atr_summaries,
            "realized_vol_summaries": rv_summaries,
            "three_way_summaries": three_way,
        }
        run_reports.append(ATRExtensionRunReport(
            payload["experiment_id"], ATR_ALGORITHM_VERSION,
            atr_config.version, payload["config_parameters"],
            REALIZED_VOLATILITY_ALGORITHM_VERSION, rv_config.version, output_sha,
            atr_summaries, rv_summaries, three_way, _atr_sha256(payload),
        ))
        atr_results[index] = None
        del rv_result, atr_result
    manifest = _manifest(prepared, classifier_config, lifecycle_config,
                         _sha256(tuple(output_digests)))
    report = HistoricalATRExtensionReport(
        manifest, prepared.archive_dataset.diagnostics,
        prepared.replay_result.diagnostics, tuple(run_reports),
        code_revision, WARMUP_ASYMMETRY, "",
    )
    report_value = _atr_json_safe(report)
    return replace(report, report_sha256=_atr_sha256({
        key: value for key, value in report_value.items()
        if key != "report_sha256"
    }))


def run_historical_atr_extension_from_archive(
    request: HistoricalATRExtensionRequest,
) -> HistoricalATRExtensionReport:
    return run_historical_atr_extension(
        prepare_historical_atr_extension(request),
        code_revision=request.code_revision,
    )


def historical_atr_extension_report_to_json(
    report: HistoricalATRExtensionReport,
) -> str:
    if not isinstance(report, HistoricalATRExtensionReport):
        raise ValueError("report must be HistoricalATRExtensionReport")
    report_value = _atr_json_safe(report)
    expected = _atr_sha256({key: value for key, value in report_value.items()
                            if key != "report_sha256"})
    if report.report_sha256 != expected:
        raise ValueError("ATR extension report digest does not match canonical output")
    return _canonical_json(report_value)


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


def build_atr_extension_cli_parser() -> argparse.ArgumentParser:
    parser = build_cli_parser()
    parser.description = "Run the separate EXP-75-06B ATR historical extension"
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_atr_extension_cli_parser()
    args = parser.parse_args(argv)
    if args.overwrite and args.output_json is None:
        parser.error("--overwrite requires --output-json")
    if args.output_json is not None and args.output_json.exists() and not args.overwrite:
        parser.error("output file already exists; pass --overwrite to replace it")
    try:
        request = HistoricalATRExtensionRequest(
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
        report = run_historical_atr_extension_from_archive(request)
        content = historical_atr_extension_report_to_json(report)
        if args.output_json is None:
            sys.stdout.write(content + "\n")
        else:
            _write_report(args.output_json, content, overwrite=args.overwrite)
    except (ValueError, TypeError, ArithmeticError, OSError,
            subprocess.CalledProcessError) as exc:
        print(f"historical ATR extension: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
