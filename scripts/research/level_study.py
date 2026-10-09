#!/usr/bin/env python3
"""Level-reaction event study L0 (#185) against the private research-data repo. Descriptive only.

Downloads the rd bars (FIRST_MONTH..segment end) of the selected symbols, runs
``market_analysis.benchmark.level_study`` one symbol at a time and commits the full
report (JSON + Markdown) to reports/levels/ in the private repo. Labels, the ledger
and existing reports are never read or written.

Public output: per symbol x level set only the symbol, set, segment, real and
placebo event counts (``level_study.public_lines``), the report hash and allowlisted
progress lines (experiment_run.Progress conventions). No rates or returns. The
hidden segment is refused by the hidden guard before anything is downloaded.
Library errors print their type name only.
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
from market_analysis.benchmark import level_study as ls  # noqa: E402
from market_analysis.benchmark import levels as lv  # noqa: E402
from market_analysis.benchmark.calibration import AUDIT_SEGMENTS, check_segment  # noqa: E402
from market_analysis.benchmark.canonical import canonical_bytes  # noqa: E402
from market_analysis.benchmark.hidden_guard import HiddenStretchLocked  # noqa: E402
from market_analysis.benchmark.segments import segment_months  # noqa: E402

NOW_UTC = xr.NOW_UTC  # the only wall-clock read (at import of experiment_run)
PublicError, GitError, Checkout, emit, _quiet = xr.PublicError, xr.GitError, xr.Checkout, xr.emit, xr._quiet

PHASES = frozenset({"checkout", "bars download", "study", "cells", "report write", "push"})
COUNT_KEYS = xr.COUNT_KEYS


class StudyProgress(xr.Progress):
    """experiment_run.Progress with the level study's own phase allowlist and timing-table title."""

    @staticmethod
    def _check(name: str, counts: dict) -> None:
        if name not in PHASES:
            raise ValueError(f"unknown progress phase {name!r}")
        for key, value in counts.items():
            if key not in COUNT_KEYS or type(value) is not int:
                raise ValueError(f"progress counts are allowlisted integers only, not {key!r}")

    def finish(self) -> list[str]:
        lines = super().finish()
        lines[0] = "### Level study timings"
        return lines


def _names(text: str, allowed: tuple, name: str) -> tuple:
    values = tuple(item.strip() for item in text.split(",") if item.strip())
    if not values or len(set(values)) != len(values) or any(value not in allowed for value in values):
        raise PublicError(f"{name} must be a comma-separated subset of {','.join(allowed)}")
    return tuple(value for value in allowed if value in values)  # canonical order


def run(args, checkout: Checkout, repo: ResearchDataRepo, workdir: Path, progress) -> list[str]:
    symbols = _names(args.symbols, lake.SYMBOLS, "symbols")
    level_sets = _names(args.level_sets, lv.LEVEL_SETS, "level_sets")
    bars_dir = workdir / "bars"
    bars_dir.mkdir(parents=True)
    months = lake.months_between(lake.FIRST_MONTH, segment_months(args.segment)[-1])
    revisions = {"data_revision": args.data_revision}
    with progress.stage("bars download", total=len(symbols)) as set_symbol:
        for symbol_index, symbol in enumerate(symbols, 1):
            set_symbol(symbol_index)
            _quiet(xr._download_symbol_bars, repo, revisions, bars_dir, None, symbol, months)
            progress.phase("bars download", symbol_index=symbol_index, symbols=len(symbols), months=len(months))
    results = {}
    with progress.stage("study", total=len(symbols)) as set_symbol:
        for symbol_index, symbol in enumerate(symbols, 1):
            set_symbol(symbol_index)
            results[symbol] = _quiet(ls.run_symbol, bars_dir, symbol, args.segment, level_sets)
            progress.phase("study", symbol_index=symbol_index, symbols=len(symbols))
    with progress.stage("cells"):
        report = _quiet(ls.build_report, args.segment, results, level_sets,
                        code_commit=os.environ.get("GITHUB_SHA", "local"), created_utc=NOW_UTC)
    progress.phase("report write")
    json_name, md_name = ls.report_paths(report)
    root = checkout.path
    (root / json_name).parent.mkdir(parents=True, exist_ok=True)
    (root / json_name).write_bytes(canonical_bytes(report))
    (root / md_name).write_text(ls.markdown(report), encoding="utf-8")
    progress.phase("push", files=2)
    checkout.commit_and_push([json_name, md_name], f"Level study on {args.segment}: report {report['report_hash']}")
    return ls.public_lines(report)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workdir", type=Path, default=Path("level-study-work"))
    parser.add_argument("--summary", type=Path, help="append the public output (markdown)")
    parser.add_argument("--segment", required=True, help="development | validation (hidden is refused)")
    parser.add_argument("--data-revision", type=int, default=1)
    parser.add_argument("--level-sets", default=",".join(lv.LEVEL_SETS))
    parser.add_argument("--symbols", default=",".join(lake.SYMBOLS))
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        check_segment(args.segment)  # the hidden guard, before any network or file access
    except HiddenStretchLocked:
        raise PublicError("the hidden segment is locked: the level study runs on development or validation "
                          "only") from None
    except ValueError:
        raise PublicError(f"segment must be one of {', '.join(AUDIT_SEGMENTS)}") from None
    # Never remove a pre-existing caller directory; own only this newly created one.
    args.workdir.mkdir(parents=True, exist_ok=False)
    progress = StudyProgress(sys.stdout)  # captured before any _quiet redirect
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
        print(f"::error title=level study failed::{error}", flush=True)
        sys.exit(1)
    except Exception as error:  # library errors can contain private values: type name only
        print(f"::error title=level study failed::{type(error).__name__}", flush=True)
        sys.exit(1)
