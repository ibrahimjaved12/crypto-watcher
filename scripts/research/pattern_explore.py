#!/usr/bin/env python3
"""Doji exploration (docs/pattern-study.md, "Doji exploration") against the private research-data repo.

Reads a candle-table-v1 report (reports/candle-table/ct1__<hash16>/), runs
``market_analysis.patterns.pattern_explore`` on one tier (intraday | daily_weekly) of the development
segment and commits summary.json, one CSV per metric family, leads.csv, report.md and the review packs
(review/*.html) to reports/pattern-explore/doji-<segment>__<hash16>/, then appends one line per
slice x metric family to experiments/doji-explore.jsonl in the same commit (the experiment log counts
the variants for the multiple-testing correction and the later validation plan). A directory with the
same report hash is skipped.

Validation is refused unless a plan id is given whose plan file (plans/doji-explore__<id16>.plan.json)
is committed in the private repo; the hidden stretch is always refused by the hidden guard, before the
private repository is touched. Public output: counts, the number of tests, the private path and the
report hash (``pattern_explore.public_lines``); never an effect size or a lead. Library errors print
their type name only.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

import experiment_run as xr  # noqa: E402  (scripts/research: Checkout, Progress)
from data_lake_build import ResearchDataRepo  # noqa: E402
from market_analysis.benchmark.calibration import AUDIT_SEGMENTS  # noqa: E402
from market_analysis.benchmark.canonical import canonical_bytes  # noqa: E402
from market_analysis.benchmark.experiment_log import ExperimentLog  # noqa: E402
from market_analysis.benchmark.hidden_guard import HiddenGuardError, HiddenStretchLocked, read_plan  # noqa: E402
from market_analysis.patterns import pattern_explore as pe  # noqa: E402
from pattern_study import resolve  # noqa: E402  (scripts/research/pattern_study.py: hash or reports/ path)

NOW_UTC = xr.NOW_UTC
PublicError, GitError, Checkout, emit, _quiet = xr.PublicError, xr.GitError, xr.Checkout, xr.emit, xr._quiet

PHASES = frozenset({"checkout", "load", "explore", "report write", "push"})
COUNT_KEYS = xr.COUNT_KEYS
LEDGER = f"experiments/{pe.QUESTION_ID}.jsonl"


class ExploreProgress(xr.Progress):
    @staticmethod
    def _check(name: str, counts: dict) -> None:
        if name not in PHASES:
            raise ValueError(f"unknown progress phase {name!r}")
        for key, value in counts.items():
            if key not in COUNT_KEYS or type(value) is not int:
                raise ValueError(f"progress counts are allowlisted integers only, not {key!r}")

    def finish(self) -> list[str]:
        lines = super().finish()
        lines[0] = "### Doji exploration timings"
        return lines


def check_plan(root: Path, plan_id: str) -> None:
    """The committed plan file of the exploration question must exist and hash to ``plan_id``."""
    path = root / "plans" / f"{pe.QUESTION_ID}__{plan_id[:16]}.plan.json"
    if not path.is_file():
        raise PublicError(f"no committed plan for {pe.QUESTION_ID} with this plan id")
    try:
        plan = read_plan(path)
    except HiddenGuardError:
        raise PublicError("the plan file does not verify") from None
    if plan.plan_id != plan_id or plan.question_id != pe.QUESTION_ID:
        raise PublicError("the plan file does not match the plan id or the question")


def parse_review(text: str):
    """'slice=bucket' or empty."""
    text = (text or "").strip()
    if not text:
        return None
    slice_id, sep, bucket = text.partition("=")
    if not sep or slice_id not in {s.id for s in pe.SLICES} or not bucket:
        raise PublicError("review slice must be '<slice id>=<bucket>' with a known slice id")
    return slice_id, bucket


def run(args, checkout: Checkout, progress) -> list[str]:
    root = checkout.path
    if args.segment != "development":
        check_plan(root, args.plan_id)
    progress.phase("load")
    table_dir = resolve(root, args.table_report, prefix="reports/candle-table/ct1__")
    frames, absent, manifest = _quiet(pe.load_tier, table_dir, args.tier, args.segment)
    with progress.stage("explore"):
        report, reviews = _quiet(pe.run_explore, frames, tier=args.tier, segment=args.segment,
                                 table_report=str(table_dir.relative_to(root)), table_hash=manifest["content_hash"],
                                 code_commit=os.environ.get("GITHUB_SHA", "local"), created_utc=NOW_UTC,
                                 plan_id=args.plan_id, cost_bp=args.cost_bp, absent=absent,
                                 review=parse_review(args.review_slice))
    directory = pe.report_dir(report)
    if (root / directory).exists():
        return [*pe.public_lines(report), "skipped: a report with this hash already exists"]
    files = {"summary.json": canonical_bytes(report),
             **{name: text.encode("utf-8") for name, text in pe.csv_files(report).items()},
             "report.md": pe.markdown(report, sorted(reviews)).encode("utf-8"),
             **{f"review/{name}": html.encode("utf-8") for name, html in reviews.items()}}
    progress.phase("report write", files=len(files))
    (root / directory / "review").mkdir(parents=True)
    for name, data in files.items():
        (root / directory / name).write_bytes(data)
    paths = [f"{directory}/{name}" for name in files]
    if args.record_trials:
        ledger = root / LEDGER
        ledger.parent.mkdir(parents=True, exist_ok=True)
        records = pe.experiment_records(report, run_id=os.environ.get("GITHUB_RUN_ID", report["report_hash"][:16]),
                                        data_snapshot_id=manifest["content_hash"])
        _quiet(ExperimentLog(ledger).append_many, records)
        paths.append(LEDGER)
    progress.phase("push", files=len(paths))
    checkout.commit_and_push(paths, f"Doji exploration {args.tier} {args.segment}: report {report['report_hash']}")
    lines = pe.public_lines(report)
    if not args.record_trials:
        lines.append("experiment log skipped")
    return lines


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workdir", type=Path, default=Path("pattern-explore-work"))
    parser.add_argument("--summary", type=Path, help="append the public output (markdown)")
    parser.add_argument("--segment", required=True, help="development (validation needs --plan-id; hidden is refused)")
    parser.add_argument("--plan-id", default="", help="sha256 plan id of a committed doji-explore plan (validation)")
    parser.add_argument("--table-report", required=True, help="candle-table report hash or reports/ path")
    parser.add_argument("--tier", required=True, help="intraday | daily_weekly")
    parser.add_argument("--review-slice", default="", help="optional '<slice id>=<bucket>' review pack")
    parser.add_argument("--cost-bp", type=float, default=pe.COST_BP, help="round-trip cost for the excursion map")
    parser.add_argument("--record-trials", action=argparse.BooleanOptionalAction, default=True)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        pe.check_explore_segment(args.segment, args.plan_id)  # the hidden guard, before any network or file access
        pe.check_tier(args.tier)
    except HiddenStretchLocked:
        raise PublicError("the hidden segment is locked: exploration never opens it") from None
    except pe.ExploreError as error:
        raise PublicError(str(error)) from None
    except ValueError:
        raise PublicError(f"segment must be one of {', '.join(AUDIT_SEGMENTS)}") from None
    if not 0 <= args.cost_bp < 1000:
        raise PublicError("cost_bp must be in [0, 1000)")
    parse_review(args.review_slice)
    args.workdir.mkdir(parents=True, exist_ok=False)
    progress = ExploreProgress(sys.stdout)
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
        print(f"::error title=doji exploration failed::{error}", flush=True)
        sys.exit(1)
    except Exception as error:  # library errors can contain private values: type name only
        print(f"::error title=doji exploration failed::{type(error).__name__}", flush=True)
        sys.exit(1)
