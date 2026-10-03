"""Deterministic development HMM cross-fit artifacts for study #123 Part B.

This module is artifact-only. It consumes the ten finalized development period
reports, trains the existing HMM on nine stored training blocks per fold, and
causally filters the held-out stored feature blocks. It never loads raw market
archives, reruns replay, or reimplements HMM math.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .experiments.market_state_hmm_regimes import (
    HMM_ALGORITHM_VERSION,
    HMM_CONFIG_V1,
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
    _load_hmm_development_block,
    _period_filename,
    _read_json,
    _verify_hashed_payload,
    _write_atomic_new,
    load_study_manifest,
)


HMM_DEVELOPMENT_CROSSFIT_VERSION = "historical-market-state-hmm-development-crossfit-v1"
HMM_DEVELOPMENT_CROSSFIT_INDEX_VERSION = (
    "historical-market-state-hmm-development-crossfit-index-v1"
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


def _load_development_inputs(manifest, output_dir: Path, code_revision: str):
    period_dir = output_dir / PERIOD_DIRECTORY
    blocks = []
    reports = []
    coverage_sha = None
    for period in _development_periods(manifest):
        path = period_dir / _period_filename(period)
        if not path.is_file():
            raise ValueError(
                f"cannot build HMM cross-fit before development period "
                f"{period.study_period_index} completes")
        report = _read_json(path)
        report_sha = _verify_hashed_payload(report, "report_sha256", "development period report")
        identity = report.get("period", {})
        if (report.get("period_report_schema_version") != PERIOD_REPORT_SCHEMA_VERSION
                or report.get("study_manifest_sha256") != manifest.manifest_sha256
                or report.get("code_revision") != code_revision
                or identity.get("study_period_index") != period.study_period_index
                or identity.get("phase") != "development"
                or identity.get("utc_date") != period.utc_date.isoformat()):
            raise StudyArtifactConflictError(
                f"development period artifact identity mismatch: {path.name}")
        current_coverage = report.get("extension_coverage_manifest_sha256")
        if not isinstance(current_coverage, str) or len(current_coverage) != 64:
            raise ValueError("development period lacks frozen extension coverage identity")
        if coverage_sha is None:
            coverage_sha = current_coverage
        elif current_coverage != coverage_sha:
            raise ValueError("development period artifacts use different frozen coverage reports")
        block = _load_hmm_development_block(
            report.get("hmm_development_training_block"))
        if (block.study_period_index != period.study_period_index
                or block.utc_date != period.utc_date.isoformat()
                or report.get("hmm_development_training_block_sha256")
                != block.block_sha256):
            raise ValueError("development period HMM feature evidence hash mismatch")
        blocks.append(block)
        reports.append({
            "study_period_index": period.study_period_index,
            "utc_date": period.utc_date.isoformat(),
            "period_report_sha256": report_sha,
            "training_block_sha256": block.block_sha256,
        })
    return tuple(blocks), tuple(reports), coverage_sha


def _fold_payload(manifest, coverage_sha, code_revision, held_out, training_blocks,
                  report_identity, diagnostics, model, held_out_evidence):
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
        } for block in training_blocks),
        "training_diagnostics": report_json_safe(diagnostics),
        "fold_model_sha256": model.model_sha256,
        "held_out_feature_block_count": len(held_out.feature_blocks),
        "held_out_evidence": report_json_safe(held_out_evidence),
        "filter_semantics": (
            "causal-forward-filter over stored held-out HMM feature blocks; "
            "state resets at every stored block/gap; held-out day never enters fold training"
        ),
        "code_revision": code_revision,
    }
    return json.loads(_artifact_json(body, "artifact_sha256"))


def freeze_development_hmm_crossfit(
    manifest: HistoricalMarketStateStudyManifest,
    output_dir: Path | str,
    *,
    code_revision: str,
) -> tuple[Path, ...]:
    """Write ten immutable leave-one-development-day-out HMM fold artifacts."""
    if not isinstance(code_revision, str) or not code_revision:
        raise ValueError("HMM cross-fit requires code revision")
    output = Path(output_dir).expanduser().resolve()
    blocks, reports, coverage_sha = _load_development_inputs(
        manifest, output, code_revision)
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
            reports[offset], diagnostics, model, held_out_evidence)
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


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build artifact-only development HMM cross-fit evidence")
    parser.add_argument("--study-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--code-revision")
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
            manifest, args.output_dir, code_revision=revision)
        for path in paths:
            print(path)
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
