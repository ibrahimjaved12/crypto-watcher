#!/usr/bin/env python3
"""Doji event study (docs/pattern-study.md) against the private research-data repo. Descriptive only, never a PASS.

Reads a candle-table-v1 report (reports/candle-table/ct1__<hash16>/, from the candle-table build)
in the private repo, runs ``market_analysis.patterns.pattern_study`` on the requested segment and
commits report.md, cells.csv and summary.json to reports/pattern-study/doji-<segment>__<hash16>/.
A directory with the same report hash is skipped, so identical inputs are idempotent. An optional
development reference report adds a same-sign replication column to a validation run.

Public output: per cell only members, controls, days and exclusion counts, the number of hypotheses
examined, the private path and the report hash (``pattern_study.public_lines``), plus allowlisted
progress lines. No effect sizes. The hidden segment is refused by the hidden guard before the
private repository is touched. Library errors print their type name only.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

import experiment_run as xr  # noqa: E402  (scripts/research: Checkout, Progress)
from data_lake_build import ResearchDataRepo  # noqa: E402
from market_analysis.benchmark.calibration import AUDIT_SEGMENTS, check_segment  # noqa: E402
from market_analysis.benchmark.canonical import canonical_bytes  # noqa: E402
from market_analysis.benchmark.hidden_guard import HiddenStretchLocked  # noqa: E402
from market_analysis.patterns import pattern_study as ps  # noqa: E402

NOW_UTC = xr.NOW_UTC
PublicError, GitError, Checkout, emit, _quiet = xr.PublicError, xr.GitError, xr.Checkout, xr.emit, xr._quiet

PHASES = frozenset({"checkout", "load", "study", "report write", "push"})
COUNT_KEYS = xr.COUNT_KEYS
_HASH = re.compile(r"[0-9a-f]{16,64}\Z")
_PATH = re.compile(r"reports/[A-Za-z0-9_./-]+\Z")


class StudyProgress(xr.Progress):
    @staticmethod
    def _check(name: str, counts: dict) -> None:
        if name not in PHASES:
            raise ValueError(f"unknown progress phase {name!r}")
        for key, value in counts.items():
            if key not in COUNT_KEYS or type(value) is not int:
                raise ValueError(f"progress counts are allowlisted integers only, not {key!r}")

    def finish(self) -> list[str]:
        lines = super().finish()
        lines[0] = "### Doji event study timings"
        return lines


def resolve(root: Path, text: str, *, prefix: str, leaf: str = "") -> Path:
    """A report hash (16..64 hex) or a path under reports/ inside the private checkout."""
    text = (text or "").strip()
    if _HASH.match(text):
        relative = f"{prefix}{text[:16]}{leaf}"
    elif _PATH.match(text) and ".." not in text.split("/"):
        relative = text.rstrip("/")
    else:
        raise PublicError("a report must be given as its hash or a reports/ path")
    path = root / relative
    if not path.exists():
        raise PublicError(f"report not found in the private repo: {relative}")
    return path


def run(args, checkout: Checkout, progress) -> list[str]:
    root = checkout.path
    progress.phase("load")
    table_dir = resolve(root, args.table_report, prefix="reports/candle-table/ct1__")
    reference = None
    if args.reference_report:
        path = resolve(root, args.reference_report, prefix="reports/pattern-study/doji-development__",
                       leaf="/summary.json")
        reference = json.loads((path / "summary.json" if path.is_dir() else path).read_text(encoding="utf-8"))
    with progress.stage("study"):
        report = _quiet(ps.run_study, table_dir, args.segment, table_report=str(table_dir.relative_to(root)),
                        reference=reference, code_commit=os.environ.get("GITHUB_SHA", "local"),
                        created_utc=NOW_UTC)
    directory = ps.report_dir(report)
    if (root / directory).exists():
        return [*ps.public_lines(report), "skipped: a report with this hash already exists"]
    progress.phase("report write", files=3)
    (root / directory).mkdir(parents=True)
    files = {"summary.json": canonical_bytes(report), "cells.csv": ps.cells_csv(report).encode("utf-8"),
             "report.md": ps.markdown(report).encode("utf-8")}
    for name, data in files.items():
        (root / directory / name).write_bytes(data)
    progress.phase("push", files=3)
    checkout.commit_and_push([f"{directory}/{name}" for name in files],
                             f"Doji event study {args.segment}: report {report['report_hash']}")
    return ps.public_lines(report)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workdir", type=Path, default=Path("pattern-study-work"))
    parser.add_argument("--summary", type=Path, help="append the public output (markdown)")
    parser.add_argument("--segment", required=True, help="development | validation (hidden is refused)")
    parser.add_argument("--table-report", required=True, help="candle-table report hash or reports/ path")
    parser.add_argument("--reference-report", default="", help="development doji report hash or path (optional)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        check_segment(args.segment)  # the hidden guard, before any network or file access
    except HiddenStretchLocked:
        raise PublicError("the hidden segment is locked: the study runs on development or validation only") from None
    except ValueError:
        raise PublicError(f"segment must be one of {', '.join(AUDIT_SEGMENTS)}") from None
    if args.reference_report and args.segment != "validation":
        raise PublicError("a reference report only applies to a validation run")
    args.workdir.mkdir(parents=True, exist_ok=False)
    progress = StudyProgress(sys.stdout)
    try:
        repository, token = os.environ.get("RESEARCH_DATA_REPOSITORY", ""), os.environ.get("RESEARCH_DATA_TOKEN", "")
        try:
            ResearchDataRepo(repository, token)  # validates owner/name, the token, and that it is private
        except ValueError as error:
            raise PublicError(str(error)) from None
        progress.phase("checkout")
        checkout = Checkout(repository, token, args.workdir / "research-data")
        emit(run(args, checkout, progress), args.summary)
    finally:
        shutil.rmtree(args.workdir, ignore_errors=True)
        timings = progress.finish()
        if args.summary:
            with args.summary.open("a", encoding="utf-8") as stream:
                stream.write("\n".join(timings))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (PublicError, GitError) as error:
        print(f"::error title=doji event study failed::{error}", flush=True)
        sys.exit(1)
    except Exception as error:  # library errors can contain private values: type name only
        print(f"::error title=doji event study failed::{type(error).__name__}", flush=True)
        sys.exit(1)
