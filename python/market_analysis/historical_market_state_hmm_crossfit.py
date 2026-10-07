"""Deterministic development HMM cross-fit artifacts for study #123 Part B.

This module is artifact-only. It consumes the ten finalized development period
reports, trains the existing HMM on nine stored training blocks per fold, and
causally filters the held-out stored feature blocks. It never loads raw market
archives, reruns replay, or reimplements HMM math.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from .experiments.market_state_hmm_regimes import (
    HMM_ALGORITHM_VERSION,
    HMM_CONFIG_V1,
    _training_fingerprint,
    filter_hmm_regime_feature_blocks,
    train_hmm_regime_model_from_blocks,
)
from .historical_experiment_batch import report_json_safe
from .historical_market_state_study import HistoricalMarketStateStudyManifest
from .historical_market_state_study_execution import (
    HMM_DEVELOPMENT_MODEL_VERSION,
    PERIOD_DIRECTORY,
    PERIOD_REPORT_SCHEMA_VERSION,
    StudyArtifactConflictError,
    _artifact_json,
    _canonical,
    _log_peak_memory,
    _validate_period_report,
    _validated_period_hmm_block,
    _period_filename,
    _read_json,
    _verify_hashed_payload,
    _write_atomic_new,
    load_study_manifest,
    _parse_hmm_model,
)


HMM_DEVELOPMENT_CROSSFIT_VERSION = "historical-market-state-hmm-development-crossfit-v2"
HMM_DEVELOPMENT_CROSSFIT_INDEX_VERSION = (
    "historical-market-state-hmm-development-crossfit-index-v2"
)
HMM_CROSSFIT_DIRECTORY = "hmm-development-crossfit"
HMM_CROSSFIT_INDEX_FILENAME = "historical-market-state-study-v1-hmm-crossfit-index.json"


def _development_periods(manifest: HistoricalMarketStateStudyManifest):
    periods = tuple(item for item in manifest.selected_periods
                    if item.phase == "development")
    if (len(periods) != 10
            or tuple(item.study_period_index for item in periods) != tuple(range(10))):
        raise ValueError("HMM cross-fit requires the complete frozen ten-day development cohort")
    return periods


def _v1_continuous_times(report, period):
    """Index persisted minute V1 evidence for exact held-out HMM joins."""
    times = tuple(
        item.get("decision_time_ms") for item in report["candidate_evidence"]
        if item.get("experiment_id") == "V1"
        and item.get("evidence_kind") == "CONTINUOUS")
    if (len(times) != len(set(times))
            or any(type(boundary) is not int
                   or boundary % HMM_CONFIG_V1.sample_interval_ms
                   or not period.start_boundary_time_ms <= boundary < period.end_boundary_time_ms
                   for boundary in times)):
        raise ValueError("development report has invalid V1 continuous minute evidence")
    return frozenset(times)


def _load_development_inputs(manifest, output_dir: Path, code_revision: str, table_dir=None):
    """Development HMM inputs from the reports, or from analysis-table HMM views."""
    period_dir = output_dir / PERIOD_DIRECTORY
    blocks = []
    reports = []
    coverage_sha = None
    for period in _development_periods(manifest):
        report = None  # only one decoded report alive at a time
        if table_dir is not None:
            from . import historical_market_state_study_tables as tables
            path = tables.table_path(table_dir, period)
            if not path.is_file():
                raise ValueError(
                    f"cannot build HMM cross-fit before development table "
                    f"{period.study_period_index} is derived")
            report = tables.load_hmm_view(path, manifest, period, code_revision=code_revision,
                                          coverage_sha256=coverage_sha)
            report_sha = report["report_sha256"]
        else:
            path = period_dir / _period_filename(period)
            if not path.is_file():
                raise ValueError(
                    f"cannot build HMM cross-fit before development period "
                    f"{period.study_period_index} completes")
            report = _read_json(path)
            report_sha = _validate_period_report(report, manifest, period, code_revision, coverage_sha)
        current_coverage = report["extension_coverage_manifest_sha256"]
        if coverage_sha is None:
            coverage_sha = current_coverage
        block = _validated_period_hmm_block(report, period)
        v1_times = _v1_continuous_times(report, period)
        if any(row.evaluation_boundary_time_ms not in v1_times
               for rows in block.feature_blocks for row in rows):
            raise ValueError("development HMM feature row lacks exact V1 continuous evidence")
        blocks.append(block)
        reports.append({
            "study_period_index": period.study_period_index,
            "utc_date": period.utc_date.isoformat(),
            "period_report_sha256": report_sha,
            "training_block_sha256": block.block_sha256,
            "v1_continuous_times": v1_times,
        })
        _log_peak_memory(f"crossfit inputs period {period.study_period_index}")
    report = None
    return tuple(blocks), tuple(reports), coverage_sha


def _fold_payload(manifest, coverage_sha, code_revision, held_out, training_blocks,
                  report_identity, diagnostics, model, held_out_evidence, training_reports):
    body = {
        "artifact_version": HMM_DEVELOPMENT_CROSSFIT_VERSION,
        "study_version": manifest.study_version,
        "study_manifest_sha256": manifest.manifest_sha256,
        "extension_coverage_manifest_sha256": coverage_sha,
        "algorithm_version": HMM_ALGORITHM_VERSION,
        "config_version": HMM_CONFIG_V1.version,
        "held_out_period": {
            "study_period_index": held_out.study_period_index,
            "utc_date": held_out.utc_date,
            "training_block_sha256": held_out.block_sha256,
            "period_report_sha256": report_identity["period_report_sha256"],
        },
        "ordered_training_blocks": tuple({
            "study_period_index": block.study_period_index,
            "utc_date": block.utc_date,
            "training_block_sha256": block.block_sha256,
            "period_report_sha256": source["period_report_sha256"],
        } for block, source in zip(training_blocks, training_reports)),
        "training_diagnostics": report_json_safe(diagnostics),
        "fold_model_sha256": model.model_sha256,
        "fold_model": report_json_safe(model),
        "held_out_feature_block_count": len(held_out.feature_blocks),
        "held_out_evidence": report_json_safe(held_out_evidence),
        "filter_semantics": (
            "causal-forward-filter over stored held-out HMM feature blocks; "
            "state resets at day start and actual feature gaps; held-out day never enters fold training"
        ),
        "code_revision": code_revision,
    }
    return json.loads(_artifact_json(body, "artifact_sha256"))


def freeze_development_hmm_crossfit(
    manifest: HistoricalMarketStateStudyManifest,
    output_dir: Path | str,
    *,
    code_revision: str,
    table_dir: Path | str | None = None,
) -> tuple[Path, ...]:
    """Write ten immutable leave-one-development-day-out HMM fold artifacts."""
    if not isinstance(code_revision, str) or not code_revision:
        raise ValueError("HMM cross-fit requires code revision")
    output = Path(output_dir).expanduser().resolve()
    blocks, reports, coverage_sha = _load_development_inputs(
        manifest, output, code_revision, table_dir=table_dir)
    destination = output / HMM_CROSSFIT_DIRECTORY
    written = []
    index_entries = []

    for offset, held_out in enumerate(blocks):
        training_blocks = tuple(
            block for index, block in enumerate(blocks) if index != offset)
        diagnostics, model = train_hmm_regime_model_from_blocks(
            training_blocks, HMM_CONFIG_V1)
        if model is None or diagnostics.status != "HMM_TRAINING_READY":
            raise ValueError(
                f"HMM cross-fit training unavailable for held-out period "
                f"{held_out.study_period_index}: {diagnostics.reason}")
        held_out_evidence = filter_hmm_regime_feature_blocks(
            held_out.feature_blocks, model, HMM_CONFIG_V1)
        payload = _fold_payload(
            manifest, coverage_sha, code_revision, held_out, training_blocks,
            reports[offset], diagnostics, model, held_out_evidence,
            tuple(source for index, source in enumerate(reports) if index != offset))
        path = destination / (
            f"{held_out.study_period_index:03d}-{held_out.utc_date}.json")
        if path.exists():
            existing = _read_json(path)
            _verify_hashed_payload(existing, "artifact_sha256", "HMM cross-fit fold")
            if _canonical(existing) != _canonical(payload):
                raise StudyArtifactConflictError(
                    f"a different HMM cross-fit fold already exists: {path.name}")
        else:
            _write_atomic_new(path, _canonical(payload))
        written.append(path)
        index_entries.append({
            "study_period_index": held_out.study_period_index,
            "utc_date": held_out.utc_date,
            "fold_file": path.name,
            "fold_model_sha256": model.model_sha256,
            "artifact_sha256": payload["artifact_sha256"],
        })

    index_body = {
        "index_version": HMM_DEVELOPMENT_CROSSFIT_INDEX_VERSION,
        "artifact_version": HMM_DEVELOPMENT_CROSSFIT_VERSION,
        "study_version": manifest.study_version,
        "study_manifest_sha256": manifest.manifest_sha256,
        "extension_coverage_manifest_sha256": coverage_sha,
        "algorithm_version": HMM_ALGORITHM_VERSION,
        "config_version": HMM_CONFIG_V1.version,
        "ordered_folds": tuple(index_entries),
        "code_revision": code_revision,
    }
    index_payload = json.loads(_artifact_json(index_body, "index_sha256"))
    index_path = output / HMM_CROSSFIT_INDEX_FILENAME
    if index_path.exists():
        existing = _read_json(index_path)
        _verify_hashed_payload(existing, "index_sha256", "HMM cross-fit index")
        if _canonical(existing) != _canonical(index_payload):
            raise StudyArtifactConflictError(
                "a different HMM cross-fit index already exists")
    else:
        _write_atomic_new(index_path, _canonical(index_payload))
    return tuple(written)


def validate_hmm_crossfit_index(path: Path | str,
                                manifest: HistoricalMarketStateStudyManifest,
                                *, coverage_sha256: str, code_revision: str, table_dir=None):
    """Validate all ten folds and their source-report/model identities for Part C.

    With ``table_dir`` the development HMM views come from analysis tables
    instead of the reports beside the index; every other check is unchanged.
    """
    index_path = Path(path).expanduser().resolve()
    index = _read_json(index_path)
    _verify_hashed_payload(index, "index_sha256", "HMM cross-fit index")
    periods = _development_periods(manifest)
    if (index.get("index_version") != HMM_DEVELOPMENT_CROSSFIT_INDEX_VERSION
            or index.get("artifact_version") != HMM_DEVELOPMENT_CROSSFIT_VERSION
            or index.get("study_version") != manifest.study_version
            or index.get("study_manifest_sha256") != manifest.manifest_sha256
            or index.get("extension_coverage_manifest_sha256") != coverage_sha256
            or index.get("algorithm_version") != HMM_ALGORITHM_VERSION
            or index.get("config_version") != HMM_CONFIG_V1.version
            or index.get("code_revision") != code_revision):
        raise ValueError("HMM cross-fit index identity differs from the frozen study")
    entries = index.get("ordered_folds")
    if not isinstance(entries, list) or len(entries) != len(periods):
        raise ValueError("HMM cross-fit index must contain all ten ordered folds")
    source_identities = {}
    period_dir = index_path.parent / PERIOD_DIRECTORY
    for period in periods:
        report = None  # only one decoded report alive at a time
        if table_dir is not None:
            from . import historical_market_state_study_tables as tables
            report = tables.load_hmm_view(tables.table_path(table_dir, period), manifest, period,
                                          code_revision=code_revision, coverage_sha256=coverage_sha256)
            report_sha = report["report_sha256"]
        else:
            report = _read_json(period_dir / _period_filename(period))
            report_sha = _validate_period_report(report, manifest, period, code_revision, coverage_sha256)
        block = _validated_period_hmm_block(report, period)
        v1_times = _v1_continuous_times(report, period)
        if any(row.evaluation_boundary_time_ms not in v1_times
               for rows in block.feature_blocks for row in rows):
            raise ValueError("development HMM feature row lacks exact V1 continuous evidence")
        source_identities[period.study_period_index] = (
            report_sha, block.block_sha256, block, v1_times)
        _log_peak_memory(f"crossfit validate period {period.study_period_index}")
    report = None
    for offset, (entry, period) in enumerate(zip(entries, periods)):
        if (not isinstance(entry, dict)
                or entry.get("study_period_index") != offset
                or entry.get("utc_date") != period.utc_date.isoformat()
                or entry.get("fold_file") != f"{offset:03d}-{period.utc_date.isoformat()}.json"):
            raise ValueError("HMM cross-fit fold ordering/period identity mismatch")
        fold_path = index_path.parent / HMM_CROSSFIT_DIRECTORY / entry["fold_file"]
        fold = _read_json(fold_path)
        _verify_hashed_payload(fold, "artifact_sha256", "HMM cross-fit fold")
        if (fold.get("artifact_version") != HMM_DEVELOPMENT_CROSSFIT_VERSION
                or fold.get("study_version") != manifest.study_version
                or fold.get("study_manifest_sha256") != manifest.manifest_sha256
                or fold.get("extension_coverage_manifest_sha256") != coverage_sha256
                or fold.get("algorithm_version") != HMM_ALGORITHM_VERSION
                or fold.get("config_version") != HMM_CONFIG_V1.version
                or fold.get("code_revision") != code_revision
                or fold.get("artifact_sha256") != entry.get("artifact_sha256")):
            raise ValueError("HMM fold contract differs from the cross-fit index")
        held = fold.get("held_out_period", {})
        training = fold.get("ordered_training_blocks")
        if (not isinstance(held, dict) or not isinstance(training, list)
                or any(not isinstance(item, dict) for item in training)):
            raise ValueError("HMM fold period/training references are malformed")
        expected_training = tuple(item.study_period_index for item in periods if item.study_period_index != offset)
        if ((held.get("study_period_index"), held.get("utc_date"))
                != (offset, period.utc_date.isoformat())
                or held.get("period_report_sha256") != source_identities[offset][0]
                or held.get("training_block_sha256") != source_identities[offset][1]
                or not isinstance(held.get("period_report_sha256"), str)
                or len(held["period_report_sha256"]) != 64
                or not isinstance(training, list) or len(training) != 9
                or tuple(item.get("study_period_index") for item in training) != expected_training
                or any(not all(isinstance(item.get(key), str) and len(item[key]) == 64
                               for key in ("training_block_sha256", "period_report_sha256"))
                       for item in training)
                or tuple((item.get("period_report_sha256"), item.get("training_block_sha256"))
                         for item in training)
                != tuple((source_identities[index][0], source_identities[index][1])
                         for index in expected_training)):
            raise ValueError("HMM fold lacks its held-out/source training identities")
        model = _parse_hmm_model(fold.get("fold_model"))
        if (model.model_sha256 != fold.get("fold_model_sha256")
                or model.model_sha256 != entry.get("fold_model_sha256")):
            raise ValueError("HMM fold model hash mismatch")
        training_blocks = tuple(source_identities[index][2] for index in expected_training)
        if any(item.movement_scope != training_blocks[0].movement_scope for item in training_blocks):
            raise ValueError("HMM fold source training scopes differ")
        training_feature_blocks = tuple(feature_block for item in training_blocks
                                        for feature_block in item.feature_blocks)
        expected_training_sha = _training_fingerprint(
            training_feature_blocks, training_blocks[0].movement_scope, HMM_CONFIG_V1)
        diagnostics = fold.get("training_diagnostics")
        if not isinstance(diagnostics, dict):
            raise ValueError("HMM fold training diagnostics are malformed")
        if (model.training_data_sha256 != expected_training_sha
                or model.training_block_count != len(training_feature_blocks)
                or model.training_usable_row_count != sum(len(block) for block in training_feature_blocks)
                or model.training_unavailable_row_count != sum(item.unavailable_row_count
                                                               for item in training_blocks)
                or diagnostics.get("training_data_sha256") != expected_training_sha):
            raise ValueError("HMM fold model was not fit on exactly its nine source blocks")
        if (diagnostics.get("status") != "HMM_TRAINING_READY"
                or diagnostics.get("reason") is not None
                or diagnostics.get("usable_row_count") != model.training_usable_row_count
                or diagnostics.get("unavailable_row_count") != model.training_unavailable_row_count
                or diagnostics.get("block_count") != model.training_block_count
                or diagnostics.get("transition_count") != model.training_transition_count
                or diagnostics.get("first_usable_boundary_time_ms")
                != model.training_first_usable_boundary_time_ms
                or diagnostics.get("last_usable_boundary_time_ms")
                != model.training_last_usable_boundary_time_ms
                or fold.get("filter_semantics") != (
                    "causal-forward-filter over stored held-out HMM feature blocks; "
                    "state resets at day start and actual feature gaps; held-out day never enters fold training")):
            raise ValueError("HMM fold diagnostics/filter semantics conflict with the frozen model")
        evidence = fold.get("held_out_evidence")
        if (type(fold.get("held_out_feature_block_count")) is not int
                or not isinstance(evidence, list)
                or len(evidence) != fold["held_out_feature_block_count"]):
            raise ValueError("HMM fold held-out evidence block count mismatch")
        source_feature_blocks = source_identities[offset][2].feature_blocks
        if (len(evidence) != len(source_feature_blocks)
                or any(not isinstance(items, list) or len(items) != len(source_rows)
                       for items, source_rows in zip(evidence, source_feature_blocks))):
            raise ValueError("HMM fold evidence does not preserve held-out feature block boundaries")
        previous_boundary = None
        previous_ready = False
        for evidence_block, source_block in zip(evidence, source_feature_blocks):
            for item, source_row in zip(evidence_block, source_block):
                if not isinstance(item, dict):
                    raise ValueError("HMM fold contains malformed held-out evidence")
                if (item.get("model_sha256") != model.model_sha256
                        or item.get("algorithm_version") != HMM_ALGORITHM_VERSION
                        or item.get("config_version") != HMM_CONFIG_V1.version
                        or item.get("evaluation_boundary_time_ms")
                        != source_row.evaluation_boundary_time_ms
                        or item.get("evaluation_boundary_time_ms")
                        not in source_identities[offset][3]
                        or item.get("raw_feature_vector") != list(source_row.values)):
                    raise ValueError("HMM held-out evidence lacks an exact V1 continuous match")
                posterior = item.get("posterior_probabilities")
                if item.get("status") == "HMM_READY":
                    if (not isinstance(posterior, list) or len(posterior) != 3
                            or any(type(value) not in (int, float) or not math.isfinite(value)
                                   or value < 0 for value in posterior)
                            or abs(sum(posterior) - 1.0) > 1e-10):
                        raise ValueError("HMM fold contains malformed posterior evidence")
                    expected_reset = (not previous_ready or previous_boundary is None
                                      or source_row.evaluation_boundary_time_ms
                                      != previous_boundary + HMM_CONFIG_V1.sample_interval_ms)
                    if item.get("filter_reset_before_observation") is not expected_reset:
                        raise ValueError("HMM fold filtering did not reset at day start/gaps")
                    previous_ready = True
                elif item.get("status") != "HMM_FEATURE_UNAVAILABLE" or posterior is not None:
                    raise ValueError("HMM fold contains an invalid held-out status")
                else:
                    if item.get("filter_reset_before_observation") is not None:
                        raise ValueError("unavailable HMM fold evidence carries a filter transition")
                    previous_ready = False
                previous_boundary = source_row.evaluation_boundary_time_ms
    return index


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build artifact-only development HMM cross-fit evidence")
    parser.add_argument("--study-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--code-revision")
    parser.add_argument("--analysis-table-dir", type=Path,
                        help="read development HMM views from derived analysis tables")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    try:
        manifest = load_study_manifest(args.study_manifest)
        revision = args.code_revision
        if not revision:
            raise ValueError("--code-revision is required for cross-fit freeze")
        paths = freeze_development_hmm_crossfit(
            manifest, args.output_dir, code_revision=revision, table_dir=args.analysis_table_dir)
        for path in paths:
            print(path)
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
