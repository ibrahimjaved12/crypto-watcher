#!/usr/bin/env python3
"""Doji pilot (docs/pattern-study.md, "Doji pilot"): seeded stratified samples for visual inspection of the
detector, written to the private research-data repo.

Reads a candle-table-v1 report (reports/candle-table/ct1__<hash16>/), samples events (doji-geo-v2) stratified by
subtype x strength x range_atr tercile, size-matched controls and near-miss candles of one symbol inside the
development segments, and commits HTML contact sheets (one per timeframe), annotations.csv (a template),
pilot_key.csv (id -> row, class, stratum, detector fields) and manifest.json to reports/pattern-pilot/<hash16>/.
Blind mode shows 40 candles ending AT the signal candle (no later candle, no outcome, no detector label, opaque
ids); open mode shows 12 candles after it and the fields in the title. A directory with the same hash is skipped.

Public output: counts per timeframe and class, the private path and the hash. No chart, no row value. The window
must lie inside the development segments (the hidden guard also refuses hidden and validation months). Library
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

import experiment_run as xr  # noqa: E402  (scripts/research: Checkout, Progress)
from data_lake_build import ResearchDataRepo  # noqa: E402
from market_analysis import data_lake as lake  # noqa: E402
from market_analysis.benchmark.calibration import check_segment  # noqa: E402
from market_analysis.benchmark.canonical import canonical_bytes  # noqa: E402
from market_analysis.patterns import candle_table as ct  # noqa: E402
from market_analysis.patterns import pattern_explore as pe  # noqa: E402
from market_analysis.patterns import pattern_pilot as pp  # noqa: E402
from pattern_study import resolve  # noqa: E402  (scripts/research/pattern_study.py: hash or reports/ path)

NOW_UTC = xr.NOW_UTC
PublicError, GitError, Checkout, emit, _quiet = xr.PublicError, xr.GitError, xr.Checkout, xr.emit, xr._quiet

PHASES = frozenset({"checkout", "load", "sample", "report write", "push"})
COUNT_KEYS = xr.COUNT_KEYS
DEVELOPMENT = ("development-ext", "development")


class PilotProgress(xr.Progress):
    @staticmethod
    def _check(name: str, counts: dict) -> None:
        if name not in PHASES:
            raise ValueError(f"unknown progress phase {name!r}")
        for key, value in counts.items():
            if key not in COUNT_KEYS or type(value) is not int:
                raise ValueError(f"progress counts are allowlisted integers only, not {key!r}")

    def finish(self) -> list[str]:
        lines = super().finish()
        lines[0] = "### Doji pilot timings"
        return lines


def development_segments(tf: int) -> tuple:
    """The table's development segments of a timeframe: ((name, first_ms, end_ms), ...)."""
    return tuple(s for s in ct.table_segments(tf) if s[0] in DEVELOPMENT)


def parse_timeframes(text: str) -> tuple:
    try:
        values = tuple(int(item) for item in text.split(",") if item.strip())
    except ValueError:
        raise PublicError("timeframes must be comma-separated candle minutes") from None
    if not values or len(set(values)) != len(values) or any(v not in ct.TABLE_TIMEFRAMES for v in values):
        raise PublicError(f"timeframes must be distinct values of {ct.TABLE_TIMEFRAMES}")
    return tuple(sorted(values))


def symbol_paths(table_dir: Path, manifest: dict, symbol: str, timeframes) -> dict:
    out = {}
    for item in manifest["files"]:
        _, name, tf, _ = item["name"].split("__", 3)
        if name == symbol and int(tf) in timeframes:
            out[int(tf)] = [Path(table_dir) / item["name"]]
    return out


def run(args, checkout: Checkout, progress) -> list[str]:
    root = checkout.path
    timeframes = args.timeframes
    try:
        window = pp.check_window(timeframes, args.from_month, args.to_month, development_segments)
    except (pp.PilotError, ValueError) as error:
        raise PublicError(str(error)) from None
    progress.phase("load")
    table_dir = resolve(root, args.table_report, prefix="reports/candle-table/ct1__")
    table_manifest = json.loads((table_dir / "manifest.json").read_text(encoding="utf-8"))
    ct.check_manifest(table_manifest)
    paths = symbol_paths(table_dir, table_manifest, args.symbol, timeframes)
    missing = [tf for tf in timeframes if tf not in paths]
    if missing:
        raise PublicError(f"the table has no {args.symbol} file for timeframes {','.join(map(str, missing))}")
    frames = {tf: _quiet(pe.read_frame, paths[tf], DEVELOPMENT)[0] for tf in timeframes}
    with progress.stage("sample"):
        built = _quiet(pp.build_pilot, frames, symbol=args.symbol, window=window, per_stratum=args.per_stratum,
                       seed=args.seed, mode=args.mode, variant=args.variant)
    manifest = pp.manifest(symbol=args.symbol, table_report=str(table_dir.relative_to(root)),
                           table_hash=table_manifest["content_hash"], window=window, per_stratum=args.per_stratum,
                           seed=args.seed, mode=args.mode, counts=built["counts"], files=built["files"],
                           code_commit=os.environ.get("GITHUB_SHA", "local"), created_utc=NOW_UTC, variant=args.variant)
    directory = pp.pilot_dir(manifest)
    if (root / directory).exists():
        return [*pp.public_lines(manifest), "skipped: a pilot with this hash already exists"]
    progress.phase("report write", files=len(built["files"]) + 1)
    (root / directory).mkdir(parents=True)
    names = []
    for name, text in {**built["files"], "manifest.json": None}.items():
        data = canonical_bytes(manifest) if text is None else text.encode("utf-8")
        (root / directory / name).write_bytes(data)
        names.append(f"{directory}/{name}")
    progress.phase("push", files=len(names))
    checkout.commit_and_push(names, f"Doji pilot {args.symbol} ({args.mode}): {manifest['pilot_hash']}")
    return pp.public_lines(manifest)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workdir", type=Path, default=Path("pattern-pilot-work"))
    parser.add_argument("--summary", type=Path, help="append the public output (markdown)")
    parser.add_argument("--table-report", required=True, help="candle-table report hash or reports/ path")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--from-month", default="2025-01")
    parser.add_argument("--to-month", default="2025-06")
    parser.add_argument("--timeframes", default="60,240,1440,10080")
    parser.add_argument("--per-stratum", type=int, default=6)
    parser.add_argument("--seed", type=int, default=pp.DEFAULT_SEED)
    parser.add_argument("--mode", default="blind", choices=pp.MODES)
    parser.add_argument("--variant", default=None, choices=tuple(pe.GEO_V3),
                        help="doji-geo-v3 variant: adds the 'soft' class (its extra candles); default: none")
    args = parser.parse_args(argv)
    args.timeframes = parse_timeframes(args.timeframes)
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        check_segment("development")  # the hidden guard, before any network or file access
        lake.validate_daily_symbol(args.symbol)
        for month in (args.from_month, args.to_month):
            lake.validate_month(month)
        if not 1 <= args.per_stratum <= 50:
            raise ValueError("per_stratum must be in 1..50")
        if args.symbol not in lake.SYMBOLS and any(tf in ct.INTRADAY_TIMEFRAMES for tf in args.timeframes):
            raise ValueError("an extra symbol has daily/weekly timeframes only")
        pp.window_ms(args.from_month, args.to_month)
    except (ValueError, pp.PilotError) as error:
        raise PublicError(str(error)) from None
    args.workdir.mkdir(parents=True, exist_ok=False)
    progress = PilotProgress(sys.stdout)
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
        print(f"::error title=doji pilot failed::{error}", flush=True)
        sys.exit(1)
    except Exception as error:  # library errors can contain private values: type name only
        print(f"::error title=doji pilot failed::{type(error).__name__}", flush=True)
        sys.exit(1)
