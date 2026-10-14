#!/usr/bin/env python3
"""Write doji-v1 reports and counted hypotheses only to the private research repo."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

import calibration_audit as audit  # noqa: E402
import experiment_run as xr  # noqa: E402
from data_lake_build import ResearchDataRepo  # noqa: E402
from market_analysis import data_lake as lake  # noqa: E402
from market_analysis.benchmark import calibration as cal, doji_report as dr, experiment_run as er  # noqa: E402
from market_analysis.benchmark.experiment_log import ExperimentLog, TrialRecord, TrialStatus  # noqa: E402
from market_analysis.benchmark.hidden_guard import HiddenStretchLocked  # noqa: E402
from market_analysis.benchmark.labels import LabelParams  # noqa: E402
from market_analysis.benchmark.ta_strategies import strategy_config  # noqa: E402

LEDGER = "experiments/doji-report-v1.jsonl"


def append_trials(ledger, report):
    """One counted entry per primary hypothesis, including side, never descriptive slices.

    A strategy/timeframe has four primary hypotheses: A/B x long/short.
    Keeping them as distinct identities makes ExperimentLog.trial_count count
    every hypothesis, and preserves its existing REPLAY behavior on reruns.
    """
    records = []
    for cell in report["primary"]:
        for question, key in (("direction", "direction"), ("trade", "trade")):
            config = {**strategy_config(cell["strategy"], cell["timeframe"]),
                      "report_schema": dr.SCHEMA, "primary_question": question, "side": cell["side"],
                      "k": report["k"], "rr": report["rr"], "symbols": report["symbols"],
                      "label_params_identity": report["label_params_identity"]}
            counts = cell[key]
            records.append(TrialRecord(
                question_id="doji-report-v1", family_id="doji", strategy_id=cell["strategy"],
                strategy_version=dr.VERSION, config=config, split_id=report["segment"],
                data_snapshot_id=report["data_snapshot_id"], code_commit=report["code_commit"],
                status=TrialStatus.OK, counts_toward_n=True, count_reason="pre-specified primary hypothesis",
                result_hash=report["report_hash"], result_summary={
                    "question": question, "side": cell["side"], "timeframe": cell["timeframe"],
                    "n": counts["n"] if key == "direction" else counts["n_trades"],
                    "private_report": dr.report_directory(report) + "/report.md"}, created_utc=report["created_utc"]))
    return ledger.append_many(records)


def run(args, checkout, repo, workdir, progress):
    months = cal.check_segment(args.segment)
    horizons = audit._int_list(args.horizons, cal.HORIZONS, "horizons")
    symbols = tuple(s.strip() for s in args.symbols.split(",") if s.strip())
    if not symbols or len(set(symbols)) != len(symbols) or any(s not in lake.SYMBOLS for s in symbols):
        raise xr.PublicError("symbols must be a nonempty distinct subset of the six benchmark symbols")
    symbols = tuple(s for s in lake.SYMBOLS if s in symbols)
    params = LabelParams()
    if dr.HEADLINE_K not in params.k_grid or dr.HEADLINE_RR not in params.rr_grid:
        raise xr.PublicError("headline k=1 rr=1 is absent from the label grid")
    bars_dir, label_dir = workdir / "bars", workdir / "labels"
    bars_dir.mkdir(parents=True)
    label_dir.mkdir(parents=True)
    revisions = {"data_revision": 1, "label_revision": 1}
    tags = {s: xr.label_tag(s, er.LABEL_FIRST_MONTH, er.LABEL_LAST_MONTH, 1, params.sigma_model) for s in symbols}
    with progress.stage("label download", total=len(symbols)) as set_symbol:
        for i, symbol in enumerate(symbols, 1):
            set_symbol(i)
            xr._quiet(audit.download_labels, repo, tags[symbol], args.segment, label_dir, symbol)
            progress.phase("label download", symbol_index=i, symbols=len(symbols))
    history = lake.months_between(lake.FIRST_MONTH, months[-1])
    with progress.stage("bars download", total=len(symbols)) as set_symbol:
        for i, symbol in enumerate(symbols, 1):
            set_symbol(i)
            xr._quiet(xr._download_symbol_bars, repo, revisions, bars_dir, label_dir, symbol, history)
            progress.phase("bars download", symbol_index=i, symbols=len(symbols), months=len(history))
    progress.phase("snapshot", symbols=len(symbols), months=len(months))
    snapshot = xr._quiet(er.data_snapshot, label_dir, months, params, symbols=symbols)
    events, unconditional, baseline, data_tags = [], {}, {}, set()
    with progress.stage("audit", total=len(symbols)) as set_symbol:
        for i, symbol in enumerate(symbols, 1):
            set_symbol(i)
            rows, directions, barriers = xr._quiet(dr.collect_symbol, bars_dir, label_dir, symbol,
                                                  args.segment, horizons, params)
            events.extend(rows)
            unconditional.update({(symbol, h, side, k): value for (h, side, k), value in directions.items()})
            baseline.update(barriers)
            # These exact tags were downloaded and checked against the label manifest by xr.
            data_tags.update(f"rd-{symbol}-{month}-r1" for month in history)
            progress.phase("audit", symbol_index=i, symbols=len(symbols))
    progress.phase("report write")
    report = xr._quiet(dr.build_report, events, unconditional, baseline, segment=args.segment, symbols=symbols,
                       horizons=horizons, params=params, code_commit=os.environ.get("GITHUB_SHA", "local"),
                       created_utc=xr.NOW_UTC, data_snapshot_id=snapshot, data_tags=data_tags, label_tags=tags.values())
    root = checkout.path
    paths = dr.write_report(root, report, events)
    if args.record_trials:
        ledger = root / LEDGER
        ledger.parent.mkdir(parents=True, exist_ok=True)
        xr._quiet(append_trials, ExperimentLog(ledger), report)
        paths.append(LEDGER)
    # Same single-writer merge/commit convention as experiment-run. The workflow
    # serializes both segments because they share this append-only ledger.
    if paths:
        progress.phase("push", files=len(paths))
        checkout.commit_and_push(paths, f"Doji report on {args.segment}: {report['report_hash']}")
    return dr.public_lines(report, record_trials=args.record_trials)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, default=Path("doji-report-work"))
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--segment", required=True)
    parser.add_argument("--symbols", default=",".join(lake.SYMBOLS))
    parser.add_argument("--horizons", default="15,60,240")
    parser.add_argument("--record-trials", action=argparse.BooleanOptionalAction, default=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        cal.check_segment(args.segment)  # before checkout, downloads or any file access
    except HiddenStretchLocked:
        raise xr.PublicError("hidden segment is locked; doji reports accept development or validation only") from None
    except ValueError:
        raise xr.PublicError("segment must be development or validation") from None
    args.workdir.mkdir(parents=True, exist_ok=False)
    progress = audit.AuditProgress(sys.stdout)
    try:
        repository = os.environ.get("RESEARCH_DATA_REPOSITORY", "")
        token = os.environ.get("RESEARCH_DATA_TOKEN", "")
        try:  # existing transport verifies a separate PRIVATE repository
            repo = ResearchDataRepo(repository, token)
        except ValueError as error:
            raise xr.PublicError(str(error)) from None
        progress.phase("checkout")
        checkout = xr.Checkout(repository, token, args.workdir / "research-data")
        xr.emit(run(args, checkout, repo, args.workdir / "data", progress), args.summary)
    finally:
        shutil.rmtree(args.workdir, ignore_errors=True)
        timings = progress.finish()
        timings[0] = "### Doji report timings"
        if args.summary:
            with args.summary.open("a", encoding="utf-8") as stream:
                stream.write("\n".join(timings))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (xr.PublicError, xr.GitError) as error:
        print(f"::error title=doji report failed::{error}", flush=True)
        sys.exit(1)
    except Exception as error:
        # Library errors can contain research values: print their type only.
        print(f"::error title=doji report failed::{type(error).__name__}", flush=True)
        sys.exit(1)
