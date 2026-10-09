#!/usr/bin/env python3
"""Sigma calibration audit (#220 slice A) against the private research-data repo. Read-only.

Downloads the rd bars (FIRST_MONTH..segment end, the labels' variance history) and,
when the 240 m horizon is audited, the segment's lb1 (ewma) and lb2 (ewma-seasonal) label files
(per audited sigma model; sha256-verified
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
import json
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
from market_analysis.benchmark.canonical import canonical_bytes, content_hash as canonical_hash  # noqa: E402
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


def download_labels(repo: ResearchDataRepo, tag: str, segment: str, label_dir: Path, symbol: str) -> None:
    """Manifest + the segment's monthly label files of one lb1/lb2 release, sha256-verified (as experiment_run)."""
    release = repo.published_release(tag)
    if release is None or release.get("tag_name") != tag or release.get("draft") is not False:
        raise PublicError(f"missing published release: {tag}")
    assets = repo.assets(release["id"])
    manifest_name = er.label_manifest_name(symbol)
    if manifest_name not in assets:
        raise PublicError(f"{tag}: missing {manifest_name}")
    manifest_path = label_dir / manifest_name
    xr.download(repo, assets[manifest_name], manifest_path, limit=16 << 20)
    outputs = {item["month"]: item for item in json.loads(manifest_path.read_bytes())["outputs"]}
    for month in segment_months(segment):
        item = outputs.get(month)
        if item is None or item["name"] not in assets:
            raise PublicError(f"{tag}: missing label file for {month}")
        xr.download(repo, assets[item["name"]], label_dir / item["name"], item["sha256"])


def check_label_inputs(reference_dir: Path, label_dir: Path, symbol: str) -> None:
    """A second label release of a symbol must be built from the same rd inputs as the first (checked against the bars)."""
    def tags(directory):
        return set(json.loads((directory / er.label_manifest_name(symbol)).read_bytes()).get("rd_tags", []))
    if tags(label_dir) != tags(reference_dir):
        raise PublicError(f"{symbol}: the lb1 and lb2 label releases were built from different rd inputs")


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
    models = tuple(item.strip() for item in args.sigma_models.split(",") if item.strip())
    if not models or len(set(models)) != len(models) or any(item not in cal.SIGMA_MODELS for item in models):
        raise PublicError(f"sigma_models must be a comma-separated subset of {','.join(cal.SIGMA_MODELS)}")
    models = tuple(model for model in cal.SIGMA_MODELS if model in models)
    revisions = {"label_revision": args.label_revision, "data_revision": args.data_revision}
    bars_dir = workdir / "bars"
    bars_dir.mkdir(parents=True)
    # Barrier labels per sigma model: lb1 (ewma, --label-revision) and lb2 (ewma-seasonal,
    # --seasonal-label-revision). A symbol without the model's published release gets barrier=NA.
    label_dirs = {model: {} for model in models}
    if cal.BARRIER_HORIZON in horizons:
        for model in models:
            revision = args.label_revision if model == "ewma" else args.seasonal_label_revision
            directory = workdir / f"labels-{model}"
            directory.mkdir(parents=True)
            tags = {symbol: xr.label_tag(symbol, er.LABEL_FIRST_MONTH, er.LABEL_LAST_MONTH, revision, model)
                    for symbol in symbols}
            labelled = tuple(symbol for symbol in symbols if repo.published_release(tags[symbol]) is not None)
            with progress.stage("label download", total=len(labelled)) as set_symbol:
                for symbol_index, symbol in enumerate(labelled, 1):
                    set_symbol(symbol_index)
                    _quiet(download_labels, repo, tags[symbol], args.segment, directory, symbol)
                    label_dirs[model][symbol] = directory
                    progress.phase("label download", symbol_index=symbol_index, symbols=len(labelled))
    months = lake.months_between(lake.FIRST_MONTH, segment_months(args.segment)[-1])
    with progress.stage("bars download", total=len(symbols)) as set_symbol:
        for symbol_index, symbol in enumerate(symbols, 1):
            set_symbol(symbol_index)
            checked = [label_dirs[model][symbol] for model in models if symbol in label_dirs[model]]
            _quiet(xr._download_symbol_bars, repo, revisions, bars_dir, checked[0] if checked else None, symbol, months)
            for directory in checked[1:]:  # every label release read must be built from these rd inputs
                check_label_inputs(checked[0], directory, symbol)
            progress.phase("bars download", symbol_index=symbol_index, symbols=len(symbols), months=len(months))
    snapshot = None
    snapshots = {}
    for model in models:
        labelled = tuple(symbol for symbol in symbols if symbol in label_dirs[model])
        if labelled:
            progress.phase("snapshot", symbols=len(labelled), months=len(segment_months(args.segment)))
            directory = next(iter(label_dirs[model].values()))
            snapshots[model] = _quiet(er.data_snapshot, directory, segment_months(args.segment),
                                      LabelParams(sigma_model=model), symbols=labelled)
    if snapshots:
        snapshot = snapshots["ewma"] if set(snapshots) == {"ewma"} else canonical_hash(snapshots)
    params = LabelParams()
    results = {}
    with progress.stage("audit", total=len(symbols)) as set_symbol:
        for symbol_index, symbol in enumerate(symbols, 1):
            set_symbol(symbol_index)
            results[symbol] = _quiet(cal.audit_symbol, bars_dir, label_dirs.get("ewma", {}).get(symbol), symbol,
                                     args.segment, horizons=horizons, half_lives=half_lives, params=params,
                                     sigma_models=models,
                                     seasonal_label_dir=label_dirs.get("ewma-seasonal", {}).get(symbol))
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
    parser.add_argument("--sigma-models", default=",".join(cal.SIGMA_MODELS), help="subset of ewma,ewma-seasonal")
    parser.add_argument("--seasonal-label-revision", type=int, default=1, help="lb2 label release revision")
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
