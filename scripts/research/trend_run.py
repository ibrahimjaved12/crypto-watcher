#!/usr/bin/env python3
"""Multi-day trend Mode B evaluation (#222) against the private research-data repo.

Downloads the dk1 daily bars and funding (2020-01..2026-09 releases) of the six symbols,
loads 2020-01..the segment's last month (``trend.load_inputs``: the hidden guard runs
first and the loader stops before later rows), evaluates the K = 9 fixed variants with
``market_analysis.benchmark.trend``, appends one ledger record per variant to
experiments/trend-v1.jsonl and commits the full report (JSON + Markdown) to reports/trend/
in the private repo, ledger and report in one commit.

Public output: per variant only the name, segment, T days and number of defined symbols
(``trend.public_lines``), the report hash and allowlisted progress lines
(experiment_run.Progress conventions). No returns, Sharpe, t or verdicts. The hidden
segment is refused before anything is downloaded. Library errors print their type name only.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

import experiment_run as xr  # noqa: E402  (scripts/research: Checkout, Progress)
from daily_download import DAILY_FIRST_MONTH, DAILY_LAST_MONTH, download_daily  # noqa: E402
from data_lake_build import ResearchDataRepo  # noqa: E402
from market_analysis import data_lake as lake  # noqa: E402
from market_analysis.benchmark import trend as tr  # noqa: E402
from market_analysis.benchmark.canonical import canonical_bytes, content_hash  # noqa: E402
from market_analysis.benchmark.experiment_log import ExperimentLog  # noqa: E402
from market_analysis.benchmark.hidden_guard import HiddenStretchLocked  # noqa: E402

NOW_UTC = xr.NOW_UTC  # the only wall-clock read (at import of experiment_run)
PublicError, GitError, Checkout, emit, _quiet = xr.PublicError, xr.GitError, xr.Checkout, xr.emit, xr._quiet

PHASES = frozenset({"checkout", "daily download", "load", "evaluate", "spa bootstrap", "step-down",
                    "report write", "push"})
COUNT_KEYS = xr.COUNT_KEYS


class TrendProgress(xr.Progress):
    """experiment_run.Progress with the trend run's own phase allowlist and timing-table title."""

    @staticmethod
    def _check(name: str, counts: dict) -> None:
        if name not in PHASES:
            raise ValueError(f"unknown progress phase {name!r}")
        for key, value in counts.items():
            if key not in COUNT_KEYS or type(value) is not int:
                raise ValueError(f"progress counts are allowlisted integers only, not {key!r}")

    def finish(self) -> list[str]:
        lines = super().finish()
        lines[0] = "### Trend run timings"
        return lines


def run(args, checkout: Checkout, repo: ResearchDataRepo, workdir: Path, progress) -> list[str]:
    daily_dir = workdir / "daily"
    daily_dir.mkdir(parents=True)
    symbols = lake.SYMBOLS
    provenance = {}
    with progress.stage("daily download", total=len(symbols)) as set_symbol:
        for symbol_index, symbol in enumerate(symbols, 1):
            set_symbol(symbol_index)
            provenance[symbol] = _quiet(download_daily, repo, symbol, DAILY_FIRST_MONTH, DAILY_LAST_MONTH,
                                        args.daily_revision, daily_dir)
            progress.phase("daily download", symbol_index=symbol_index, symbols=len(symbols), files=2)
    progress.phase("load", symbols=len(symbols))
    inputs = _quiet(tr.load_inputs, daily_dir, args.segment, symbols)
    extra = {"daily_revision": args.daily_revision, "data_revision": args.data_revision, "dk1": provenance}
    snapshot = content_hash({"dk1": provenance, "daily_revision": args.daily_revision})
    root = checkout.path
    ledger = root / "experiments" / f"{tr.QUESTION_ID}.jsonl"
    ledger.parent.mkdir(exist_ok=True)
    report = _quiet(tr.evaluate_trend, inputs, args.segment, ExperimentLog(ledger),
                    code_commit=os.environ.get("GITHUB_SHA", "local"), data_snapshot_id=snapshot, now_utc=NOW_UTC,
                    extra=extra, progress=progress)
    progress.phase("report write")
    json_name, md_name = tr.report_paths(report)
    (root / json_name).parent.mkdir(parents=True, exist_ok=True)
    (root / json_name).write_bytes(canonical_bytes(report))
    (root / md_name).write_text(tr.markdown(report), encoding="utf-8")
    progress.phase("push", files=3)
    checkout.commit_and_push([str(ledger.relative_to(root)), json_name, md_name],
                             f"Trend Mode B on {args.segment}: report {report['report_hash']}")
    return tr.public_lines(report)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workdir", type=Path, default=Path("trend-run-work"))
    parser.add_argument("--summary", type=Path, help="append the public output (markdown)")
    parser.add_argument("--segment", required=True,
                        help="development | validation | development-ext (daily only; hidden is refused)")
    parser.add_argument("--data-revision", type=int, default=1, help="rd revision (recorded; trend reads dk1 only)")
    parser.add_argument("--daily-revision", type=int, default=1, help="dk1 release revision")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        tr.check_daily_segment(args.segment)  # the hidden guard, before any network or file access
    except HiddenStretchLocked:
        raise PublicError("the hidden segment is locked: the trend run evaluates development, validation or "
                          "development-ext only") from None
    except ValueError:
        raise PublicError(f"segment must be one of {', '.join(tr.TREND_SEGMENTS)}") from None
    # Never remove a pre-existing caller directory; own only this newly created one.
    args.workdir.mkdir(parents=True, exist_ok=False)
    progress = TrendProgress(sys.stdout)  # captured before any _quiet redirect
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
        print(f"::error title=trend run failed::{error}", flush=True)
        sys.exit(1)
    except Exception as error:  # library errors can contain private values: type name only
        print(f"::error title=trend run failed::{type(error).__name__}", flush=True)
        sys.exit(1)
