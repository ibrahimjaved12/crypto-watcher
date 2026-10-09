#!/usr/bin/env python3
"""Sigma calibration audit (#220 slice A) against the private research-data repo. Read-only.

Downloads the rd bars (FIRST_MONTH..segment end, the labels' variance history) and,
when the 240 m horizon is audited, the segment's lb1 label files (sha256-verified
against their manifests, and the bars checked to be the labels' rd inputs), runs
``market_analysis.benchmark.calibration`` one symbol at a time and commits the full
report (JSON + Markdown) to reports/calibration/ in the private repo. Labels, the
label engine, the experiment ledger and existing reports are never written.

Public output: per symbol x horizon x half-life only the symbol, horizon, half-life,
n and verdicts (sd, barrier, overall PASS/FAIL; ``calibration.public_lines``), the report hash, and allowlisted
progress lines (experiment_run.Progress conventions: phase names, integer counts,
seconds, peak memory, heartbeats, timing table in the job summary). The hidden
segment is refused by the hidden guard before anything is downloaded. Library
errors print their type name only.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

import experiment_run as xr  # noqa: E402  (scripts/research: Checkout, Progress, downloads)
from data_lake_build import ResearchDataRepo  # noqa: E402
from market_analysis import data_lake as lake  # noqa: E402
from market_analysis.benchmark import calibration as cal  # noqa: E402
from market_analysis.benchmark import experiment_run as er  # noqa: E402
from market_analysis.benchmark.canonical import canonical_bytes  # noqa: E402
from market_analysis.benchmark.hidden_guard import HiddenStretchLocked  # noqa: E402
from market_analysis.benchmark.labels import LabelParams  # noqa: E402
from market_analysis.benchmark.segments import segment_months  # noqa: E402

NOW_UTC = xr.NOW_UTC  # the only wall-clock read (at import of experiment_run)
PublicError, GitError, Checkout, emit, _quiet = xr.PublicError, xr.GitError, xr.Checkout, xr.emit, xr._quiet

PHASES = frozenset({"checkout", "label download", "bars download", "snapshot", "audit", "report write", "push"})
COUNT_KEYS = xr.COUNT_KEYS


class AuditProgress(xr.Progress):
    """experiment_run.Progress with the audit's own phase allowlist and timing-table title."""

    @staticmethod
    def _check(name: str, counts: dict) -> None:
        if name not in PHASES:
            raise ValueError(f"unknown progress phase {name!r}")
        for key, value in counts.items():
            if key not in COUNT_KEYS or type(value) is not int:
                raise ValueError(f"progress counts are allowlisted integers only, not {key!r}")

    def finish(self) -> list[str]:
        lines = super().finish()
        lines[0] = "### Calibration audit timings"
        return lines


def _int_list(text: str, allowed: tuple, name: str) -> tuple:
    try:
        values = tuple(int(item) for item in text.split(","))
        return cal._subset(values, allowed, name)
    except ValueError:
        raise PublicError(f"{name} must be a comma-separated subset of {','.join(map(str, allowed))}") from None


def run(args, checkout: Checkout, repo: ResearchDataRepo, workdir: Path, progress) -> list[str]:
    horizons = _int_list(args.horizons, cal.HORIZONS, "horizons")
    half_lives = _int_list(args.half_lives, cal.HALF_LIVES, "half_lives")
    symbols = tuple(item.strip() for item in args.symbols.split(",") if item.strip())
    if not symbols or len(set(symbols)) != len(symbols) or any(item not in lake.SYMBOLS for item in symbols):
        raise PublicError(f"symbols must be a comma-separated subset of {','.join(lake.SYMBOLS)}")
    symbols = tuple(item for item in lake.SYMBOLS if item in symbols)  # canonical order
    revisions = {"label_revision": args.label_revision, "data_revision": args.data_revision}
    bars_dir = workdir / "bars"
    bars_dir.mkdir(parents=True)
    label_dir = None
    if cal.BARRIER_HORIZON in horizons:
        label_dir = workdir / "labels"
        label_dir.mkdir(parents=True)
        missing = [tag for tag in (xr.label_tag(symbol, er.LABEL_FIRST_MONTH, er.LABEL_LAST_MONTH, args.label_revision)
                                   for symbol in symbols) if repo.published_release(tag) is None]
        if missing:  # all at once, before any download; audit only symbols with labels via --symbols
            raise PublicError("missing published label releases (build them with the label build workflow "
                              "or narrow symbols): " + ", ".join(missing))
        with progress.stage("label download", total=len(symbols)) as set_symbol:
            for symbol_index, symbol in enumerate(symbols, 1):
                set_symbol(symbol_index)
                _quiet(xr._download_symbol_labels, repo, revisions, args.segment, label_dir, symbol)
                progress.phase("label download", symbol_index=symbol_index, symbols=len(symbols))
    months = lake.months_between(lake.FIRST_MONTH, segment_months(args.segment)[-1])
    with progress.stage("bars download", total=len(symbols)) as set_symbol:
        for symbol_index, symbol in enumerate(symbols, 1):
            set_symbol(symbol_index)
            _quiet(xr._download_symbol_bars, repo, revisions, bars_dir, label_dir, symbol, months)
            progress.phase("bars download", symbol_index=symbol_index, symbols=len(symbols), months=len(months))
    snapshot = None
    if label_dir is not None:
        progress.phase("snapshot", symbols=len(symbols), months=len(segment_months(args.segment)))
        snapshot = _quiet(er.data_snapshot, label_dir, segment_months(args.segment), symbols=symbols)
    params = LabelParams()
    results = {}
    with progress.stage("audit", total=len(symbols)) as set_symbol:
        for symbol_index, symbol in enumerate(symbols, 1):
            set_symbol(symbol_index)
            results[symbol] = _quiet(cal.audit_symbol, bars_dir, label_dir, symbol, args.segment,
                                     horizons=horizons, half_lives=half_lives, params=params)
            progress.phase("audit", symbol_index=symbol_index, symbols=len(symbols))
    progress.phase("report write")
    report = cal.build_report(args.segment, results, horizons=horizons, half_lives=half_lives, params=params,
                              code_commit=os.environ.get("GITHUB_SHA", "local"), created_utc=NOW_UTC,
                              data_snapshot_id=snapshot)
    json_name, md_name = cal.report_paths(report)
    root = checkout.path
    (root / json_name).parent.mkdir(parents=True, exist_ok=True)
    (root / json_name).write_bytes(canonical_bytes(report))
    (root / md_name).write_text(cal.markdown(report), encoding="utf-8")
    progress.phase("push", files=2)
    checkout.commit_and_push([json_name, md_name],
                             f"Calibration audit on {args.segment}: report {report['report_hash']}")
    return cal.public_lines(report)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workdir", type=Path, default=Path("calibration-audit-work"))
    parser.add_argument("--summary", type=Path, help="append the public output (markdown)")
    parser.add_argument("--segment", required=True, help="development | validation (hidden is refused)")
    parser.add_argument("--horizons", default=",".join(map(str, cal.HORIZONS)))
    parser.add_argument("--half-lives", default=",".join(map(str, cal.HALF_LIVES)))
    parser.add_argument("--label-revision", type=int, default=1)
    parser.add_argument("--data-revision", type=int, default=1)
    parser.add_argument("--symbols", default=",".join(lake.SYMBOLS))
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        cal.check_segment(args.segment)  # the hidden guard, before any network or file access
    except HiddenStretchLocked:
        raise PublicError("the hidden segment is locked: the calibration audit runs on development or "
                          "validation only") from None
    except ValueError:
        raise PublicError(f"segment must be one of {', '.join(cal.AUDIT_SEGMENTS)}") from None
    # Never remove a pre-existing caller directory; own only this newly created one.
    args.workdir.mkdir(parents=True, exist_ok=False)
    progress = AuditProgress(sys.stdout)  # captured before any _quiet redirect
    try:
        repository, token = os.environ.get("RESEARCH_DATA_REPOSITORY", ""), os.environ.get("RESEARCH_DATA_TOKEN", "")
        try:  # validates owner/name, a separate repository, the token, and that it is private
            repo = ResearchDataRepo(repository, token)
        except ValueError as error:
            raise PublicError(str(error)) from None
        progress.phase("checkout")
        checkout = Checkout(repository, token, args.workdir / "research-data")
        emit(run(args, checkout, repo, args.workdir / "data", progress), args.summary)
    finally:
        shutil.rmtree(args.workdir, ignore_errors=True)
        timings = progress.finish()  # also on failure: shows where a run stopped
        if args.summary:
            with args.summary.open("a", encoding="utf-8") as stream:
                stream.write("\n".join(timings))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (PublicError, GitError) as error:
        print(f"::error title=calibration audit failed::{error}", flush=True)
        sys.exit(1)
    except Exception as error:  # library errors can contain private values: type name only
        print(f"::error title=calibration audit failed::{type(error).__name__}", flush=True)
        sys.exit(1)
