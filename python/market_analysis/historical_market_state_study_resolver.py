"""Resume official archive eligibility resolution for the frozen study calendar.

This module prepares core archive eligibility and the outcome-blind scientific
manifest. It does not run replay or market-state experiments.
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys
from typing import Callable

from . import historical_market_state_study as study
from .binance_historical_download import (
    BinanceHistoricalDownloadRequest,
    acquire_binance_usdm_historical_archives,
)
from .historical_experiment_batch import _write_report


ELIGIBILITY_REPORT_FILENAME = "historical-market-state-study-v1-eligibility.json"
MANIFEST_FILENAME = "historical-market-state-study-v1-manifest.json"


class StudyResolutionError(RuntimeError):
    """A blocker prevented safe study-date resolution."""


def _records_by_date(path: Path) -> dict[date, study.CoreDateEligibility]:
    if not path.exists():
        return {}
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise StudyResolutionError("cannot read saved eligibility progress") from exc
    report = study.parse_historical_study_eligibility_report_json(content)
    return {record.utc_date: record for record in report.eligibility_records}


def _save_progress(path: Path, records: dict[date, study.CoreDateEligibility]) -> None:
    report = study.HistoricalStudyEligibilityReport(tuple(records.values()))
    content = study.historical_study_eligibility_report_json(report)
    _write_report(path, content, overwrite=True)


def _checked_candidate_record(candidate_date, record):
    if (not isinstance(record, study.CoreDateEligibility)
            or record.utc_date != candidate_date):
        raise StudyResolutionError(
            f"eligibility verifier returned an invalid record for {candidate_date.isoformat()}")
    return record


def _emit_candidate_result(bin_index: int, record, emit: Callable[[str], None]) -> None:
    if record.state == "ELIGIBLE":
        emit(f"Bin {bin_index}: {record.utc_date.isoformat()} ELIGIBLE → selected")
    elif record.state == "INELIGIBLE":
        emit(f"Bin {bin_index}: {record.utc_date.isoformat()} INELIGIBLE ({record.reason_code})")
        emit(f"Bin {bin_index}: selector advancing deterministically")


def _write_final_manifest(path: Path, canonical_content: str) -> bool:
    expected_bytes = (canonical_content + "\n").encode("utf-8")
    if path.exists():
        try:
            existing_bytes = path.read_bytes()
        except OSError as exc:
            raise StudyResolutionError("cannot read existing finalized manifest") from exc
        if existing_bytes != expected_bytes:
            raise StudyResolutionError(
                "existing finalized manifest differs; refusing to overwrite it")
        return False
    _write_report(path, canonical_content, overwrite=False)
    return True


def _print_completion(manifest, eligibility_path: Path, manifest_path: Path,
                      emit: Callable[[str], None]) -> None:
    periods = manifest.selected_periods
    phases = (
        ("Development", tuple(item for item in periods if item.phase == "development")),
        ("Validation", tuple(item for item in periods if item.phase == "validation")),
        ("Test", tuple(item for item in periods if item.phase == "test")),
    )
    emit("Historical market-state study resolved.")
    emit("")
    emit(f"Study version: {manifest.study_version}")
    emit(f"Selection policy: {manifest.selection_policy_version}")
    emit(f"Selected periods: {len(periods)}")
    emit(f"Development: {len(phases[0][1])}")
    emit(f"Validation: {len(phases[1][1])}")
    emit(f"Test: {len(phases[2][1])}")
    emit("")
    emit(f"Manifest SHA256: {manifest.manifest_sha256}")
    for name, phase_periods in phases:
        emit("")
        emit(f"{name} dates:")
        for period in phase_periods:
            emit(period.utc_date.isoformat())
    emit("")
    emit(f"Eligibility report: {eligibility_path}")
    emit(f"Manifest: {manifest_path}")


def resolve_study(
    archive_root: Path | str,
    output_dir: Path | str,
    *,
    emit: Callable[[str], None] = print,
) -> study.HistoricalMarketStateStudyManifest:
    """Resolve only each deterministic selector blocker and persist progress."""
    root = Path(output_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    eligibility_path = root / ELIGIBILITY_REPORT_FILENAME
    manifest_path = root / MANIFEST_FILENAME
    records = _records_by_date(eligibility_path)

    while True:
        try:
            manifest = study.build_historical_market_state_study_manifest(
                records.values())
        except study.NoEligibleStudyDateError as exc:
            raise StudyResolutionError(str(exc)) from exc
        except study.UnresolvedStudySelectionError as exc:
            bin_index = exc.bin_index
            candidate_date = exc.unresolved_date
        else:
            if not isinstance(manifest, study.HistoricalMarketStateStudyManifest):
                raise StudyResolutionError("study builder returned an invalid manifest")
            canonical_content = study.historical_market_state_study_manifest_json(manifest)
            created = _write_final_manifest(manifest_path, canonical_content)
            if created:
                emit(f"Manifest written: {manifest_path}")
            else:
                emit(f"Existing manifest matches: {manifest_path}")
            _print_completion(manifest, eligibility_path, manifest_path, emit)
            return manifest

        emit(f"Bin {bin_index}: candidate {candidate_date.isoformat()} requires verification")
        try:
            local_record = _checked_candidate_record(
                candidate_date,
                study.verify_local_core_date(candidate_date, archive_root))
        except Exception as exc:
            raise StudyResolutionError(
                f"Bin {bin_index}: local verification failed for "
                f"{candidate_date.isoformat()} ({type(exc).__name__})") from exc
        records[candidate_date] = local_record
        if local_record.state in ("ELIGIBLE", "INELIGIBLE"):
            _save_progress(eligibility_path, records)
            _emit_candidate_result(bin_index, local_record, emit)
            continue

        _save_progress(eligibility_path, records)
        emit(f"Bin {bin_index}: {candidate_date.isoformat()} local data unavailable; "
             "acquiring official archives")
        try:
            request = BinanceHistoricalDownloadRequest(
                Path(archive_root).expanduser(), study.study_universe(),
                study.study_replay_config(candidate_date))
            acquisition = acquire_binance_usdm_historical_archives(request)
            finalized = study.core_date_eligibility_from_verified_dataset(
                candidate_date, acquisition.dataset)
            del acquisition
            finalized = _checked_candidate_record(candidate_date, finalized)
            if finalized.state not in ("ELIGIBLE", "INELIGIBLE"):
                raise ValueError("verified dataset did not produce finalized eligibility")
        except Exception as exc:
            raise StudyResolutionError(
                f"Bin {bin_index}: archive acquisition failed for "
                f"{candidate_date.isoformat()}; candidate remains UNVERIFIED "
                f"({type(exc).__name__})") from exc
        records[candidate_date] = finalized
        _save_progress(eligibility_path, records)
        _emit_candidate_result(bin_index, finalized, emit)


class _ResolverArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def build_cli_parser() -> argparse.ArgumentParser:
    parser = _ResolverArgumentParser(
        description="Resolve the frozen historical market-state study calendar")
    parser.add_argument("--archive-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_cli_parser().parse_args(argv)
    try:
        resolve_study(args.archive_root, args.output_dir)
    except Exception as exc:
        print(f"Study resolver failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
