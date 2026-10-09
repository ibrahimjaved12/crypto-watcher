#!/usr/bin/env python3
"""Objective regime/season table and calendar hypotheses (#223) against the private research-data repo.

Downloads the dk1 daily releases (2020-01..2026-09) of the six symbols and the BTCUSDT rd
1-minute bars of 2024-01..2025-12 (H3 only), builds the ``regime-v1`` table through 2025-12
with ``market_analysis.benchmark.regime`` (the hidden stretch is never read: the loaders stop
before 2026 rows and the guard refuses later months without a token), runs the four
pre-registered calendar hypotheses on development + validation days, and commits the table
(csv.gz), its JSON manifest and a Markdown summary to reports/regime/ in the private repo.
DESCRIPTIVE ONLY: nothing is written to the experiment ledger.

Public output: counts only (rows written, number of hypotheses) and the report hash, plus
allowlisted progress lines (experiment_run.Progress conventions). Library errors print
their type name only.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

import experiment_run as xr  # noqa: E402  (scripts/research: Checkout, Progress, downloads)
from daily_download import DAILY_FIRST_MONTH, DAILY_LAST_MONTH, download_daily  # noqa: E402
from data_lake_build import ResearchDataRepo  # noqa: E402
from market_analysis import data_lake as lake  # noqa: E402
from market_analysis.benchmark import regime as rg  # noqa: E402
from market_analysis.benchmark.canonical import canonical_bytes  # noqa: E402
from market_analysis.benchmark.market_data import load_symbol_bars  # noqa: E402

NOW_UTC = xr.NOW_UTC  # the only wall-clock read (at import of experiment_run)
PublicError, GitError, Checkout, emit, _quiet = xr.PublicError, xr.GitError, xr.Checkout, xr.emit, xr._quiet

PHASES = frozenset({"checkout", "daily download", "bars download", "load", "table", "calendar", "report write",
                    "push"})
COUNT_KEYS = xr.COUNT_KEYS
H3_FIRST_MONTH, H3_LAST_MONTH = "2024-01", rg.LAST_MONTH


class RegimeProgress(xr.Progress):
    """experiment_run.Progress with the regime build's own phase allowlist and timing-table title."""

    @staticmethod
    def _check(name: str, counts: dict) -> None:
        if name not in PHASES:
            raise ValueError(f"unknown progress phase {name!r}")
        for key, value in counts.items():
            if key not in COUNT_KEYS or type(value) is not int:
                raise ValueError(f"progress counts are allowlisted integers only, not {key!r}")

    def finish(self) -> list[str]:
        lines = super().finish()
        lines[0] = "### Regime build timings"
        return lines


def run(args, checkout: Checkout, repo: ResearchDataRepo, workdir: Path, progress) -> list[str]:
    daily_dir, bars_dir = workdir / "daily", workdir / "bars"
    daily_dir.mkdir(parents=True)
    bars_dir.mkdir(parents=True)
    symbols = lake.SYMBOLS
    provenance = {}
    with progress.stage("daily download", total=len(symbols)) as set_symbol:
        for symbol_index, symbol in enumerate(symbols, 1):
            set_symbol(symbol_index)
            provenance[symbol] = _quiet(download_daily, repo, symbol, DAILY_FIRST_MONTH, DAILY_LAST_MONTH,
                                        args.daily_revision, daily_dir)
            progress.phase("daily download", symbol_index=symbol_index, symbols=len(symbols), files=2)
    months = lake.months_between(H3_FIRST_MONTH, H3_LAST_MONTH)
    progress.phase("bars download", symbols=1, months=len(months))
    _quiet(xr._download_symbol_bars, repo, {"data_revision": args.data_revision}, bars_dir, None,
           rg.MARKET_SYMBOL, months)
    progress.phase("load", symbols=len(symbols))
    daily, funding = _quiet(rg.load_inputs, daily_dir, symbols)
    bars = _quiet(load_symbol_bars, bars_dir, rg.MARKET_SYMBOL, H3_FIRST_MONTH, H3_LAST_MONTH)
    progress.phase("table", symbols=len(symbols))
    first_ms, end_ms = rg.table_bounds()
    rows, columns = _quiet(rg.build_table, daily, funding, first_ms, end_ms)
    table_path = workdir / "table.csv.gz"
    with table_path.open("wb") as stream:
        written = rg.write_table(stream, rows)
    table_sha = hashlib.sha256(table_path.read_bytes()).hexdigest()
    diagnostics = _quiet(rg.diagnostics, columns, first_ms, end_ms)
    progress.phase("calendar", total=4)
    calendar = _quiet(rg.calendar_tests, daily[rg.MARKET_SYMBOL], bars)
    manifest = rg.build_manifest(rows=written, table_sha256=table_sha, first_day=rows[0][0], last_day=rows[-1][0],
                                 diagnostics=diagnostics, calendar=calendar,
                                 code_commit=os.environ.get("GITHUB_SHA", "local"), created_utc=NOW_UTC,
                                 inputs={"daily_revision": args.daily_revision, "data_revision": args.data_revision,
                                         "dk1": provenance, "h3_months": [H3_FIRST_MONTH, H3_LAST_MONTH]})
    progress.phase("report write", rows=written)
    table_name, json_name, md_name = rg.report_paths(manifest)
    root = checkout.path
    (root / json_name).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(table_path, root / table_name)
    (root / json_name).write_bytes(canonical_bytes(manifest))
    (root / md_name).write_text(rg.markdown(manifest), encoding="utf-8")
    progress.phase("push", files=3)
    checkout.commit_and_push([table_name, json_name, md_name], f"Regime table {rg.SCHEMA}: report "
                             f"{manifest['report_hash']}")
    return rg.public_lines(manifest)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workdir", type=Path, default=Path("regime-build-work"))
    parser.add_argument("--summary", type=Path, help="append the public output (markdown)")
    parser.add_argument("--data-revision", type=int, default=1, help="rd revision of the BTCUSDT bars (H3)")
    parser.add_argument("--daily-revision", type=int, default=1, help="dk1 release revision")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    # Never remove a pre-existing caller directory; own only this newly created one.
    args.workdir.mkdir(parents=True, exist_ok=False)
    progress = RegimeProgress(sys.stdout)  # captured before any _quiet redirect
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
        print(f"::error title=regime build failed::{error}", flush=True)
        sys.exit(1)
    except Exception as error:  # library errors can contain private values: type name only
        print(f"::error title=regime build failed::{type(error).__name__}", flush=True)
        sys.exit(1)
