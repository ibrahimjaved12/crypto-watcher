#!/usr/bin/env python3
"""candle-table-v1 build (docs/pattern-study.md) against the private research-data repo.

Downloads the dk1 daily releases (2020-01..2026-09; the loaders stop at through_month) of the
requested symbols plus BTCUSDT (regime market labels and funding) and the rd 1-minute bars of
2024-01..through_month, builds one csv.gz per symbol x timeframe with
``market_analysis.patterns.candle_table`` (15/60/240 from the bars, 1440/10080 from dk1), the
regime-v1 join and the dk1-vs-1m daily parity per symbol, and commits the files plus
manifest.json to reports/candle-table/ct1__<hash16>/ in the private repo. A directory with the
same content hash that already exists is skipped. The hidden stretch is never read:
through_month is at most 2025-12 and the hidden guard runs before any file is opened.

Public output: counts only (files, rows by status) and the report hash, plus allowlisted
progress lines (experiment_run.Progress conventions). Library errors print their type name only.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

import experiment_run as xr  # noqa: E402  (scripts/research: Checkout, Progress, downloads)
from daily_download import DAILY_FIRST_MONTH, DAILY_LAST_MONTH, download_daily  # noqa: E402
from data_lake_build import GitHubError, ResearchDataRepo  # noqa: E402
from market_analysis import data_lake as lake  # noqa: E402
from market_analysis.benchmark.candles import build_candles  # noqa: E402
from market_analysis.benchmark.canonical import canonical_bytes  # noqa: E402
from market_analysis.benchmark.market_data import load_symbol_bars  # noqa: E402
from market_analysis.benchmark.volatility import build_variance  # noqa: E402
from market_analysis.patterns import candle_location as loc  # noqa: E402
from market_analysis.patterns import candle_table as ct  # noqa: E402
from market_analysis.patterns.daily_candles import candles_from_daily, daily_parity  # noqa: E402

NOW_UTC = xr.NOW_UTC  # the only wall-clock read (at import of experiment_run)
PublicError, GitError, Checkout, emit, _quiet = xr.PublicError, xr.GitError, xr.Checkout, xr.emit, xr._quiet

PHASES = frozenset({"checkout", "daily download", "bars download", "load", "regime", "table", "report write",
                    "push"})
COUNT_KEYS = xr.COUNT_KEYS


class CandleTableProgress(xr.Progress):
    """experiment_run.Progress with this build's own phase allowlist and timing-table title."""

    @staticmethod
    def _check(name: str, counts: dict) -> None:
        if name not in PHASES:
            raise ValueError(f"unknown progress phase {name!r}")
        for key, value in counts.items():
            if key not in COUNT_KEYS or type(value) is not int:
                raise ValueError(f"progress counts are allowlisted integers only, not {key!r}")

    def finish(self) -> list[str]:
        lines = super().finish()
        lines[0] = "### Candle table build timings"
        return lines


def _bars_provenance(repo, symbol: str, months: list[str], data_revision: int, bars_dir: Path) -> dict:
    try:
        releases = xr.published_inputs(repo, symbol, months, data_revision)
    except GitHubError as error:
        if str(error).startswith("missing published data releases: "):
            raise PublicError(str(error)) from None
        raise
    provenance = xr.download_inputs(repo, symbol, releases, bars_dir)
    return {month: {"rd_tag": item["rd_tag"], "bars_sha256": item["bars"]["sha256"]}
            for month, item in sorted(provenance.items())}


def run(args, checkout: Checkout, repo: ResearchDataRepo, workdir: Path, progress) -> list[str]:
    symbols, timeframes, through = args.symbols, args.timeframes, args.through_month
    ct.guard_months(through)  # hidden guard and through_month limit before anything is downloaded
    daily_dir, bars_dir, out_dir = workdir / "daily", workdir / "bars", workdir / "out"
    for path in (daily_dir, bars_dir, out_dir):
        path.mkdir(parents=True)
    daily_symbols = [symbol for symbol in lake.SYMBOLS if symbol in set(symbols) | {"BTCUSDT"}]
    inputs = {"daily_revision": args.daily_revision, "data_revision": args.data_revision, "dk1": {}, "rd": {}}
    with progress.stage("daily download", total=len(daily_symbols)) as set_symbol:
        for index, symbol in enumerate(daily_symbols, 1):
            set_symbol(index)
            inputs["dk1"][symbol] = _quiet(download_daily, repo, symbol, DAILY_FIRST_MONTH, DAILY_LAST_MONTH,
                                           args.daily_revision, daily_dir)
            progress.phase("daily download", symbol_index=index, symbols=len(daily_symbols), files=2)
    months = lake.months_between(ct.INTRADAY_FIRST_MONTH, through)
    with progress.stage("bars download", total=len(symbols)) as set_symbol:
        for index, symbol in enumerate(symbols, 1):
            set_symbol(index)
            inputs["rd"][symbol] = _quiet(_bars_provenance, repo, symbol, months, args.data_revision, bars_dir)
            progress.phase("bars download", symbol_index=index, symbols=len(symbols), months=len(months))
    progress.phase("load", symbols=len(daily_symbols))
    daily, funding = _quiet(ct.load_daily_inputs, daily_dir, symbols, through)
    progress.phase("regime", symbols=len(daily_symbols))
    regime = _quiet(ct.regime_join, daily, funding, through)
    files, counts, parity = [], {}, {}
    for index, symbol in enumerate(symbols, 1):
        progress.phase("table", symbol_index=index, symbols=len(symbols))
        bars = _quiet(load_symbol_bars, bars_dir, symbol, ct.INTRADAY_FIRST_MONTH, through)
        parity[symbol] = daily_parity(daily[symbol], bars)
        contexts = {}
        intraday = [m for m in timeframes if m in ct.INTRADAY_TIMEFRAMES]
        if intraday:
            variance = build_variance(bars, ct.INTRADAY_SIGMA_HALF_LIFE_DAYS).variance
            # 60/240 are always built: higher-timeframe states and the swing4h levels need them.
            candles = {m: build_candles(bars, m) for m in sorted({*intraday, 60, 240})}
            days = candles_from_daily(daily[symbol], 1440)
            weeks = candles_from_daily(daily[symbol], 10080)
            shared = loc.shared_levels(bars=bars, days=days, weeks=weeks, candles_240=candles[240])
            for minutes in intraday:
                contexts[minutes] = ct.intraday_context(
                    symbol, minutes, bars, candles[minutes], variance, regime, through,
                    levels=loc.context_levels(minutes, candles[minutes], shared),
                    htf=loc.htf_sources(minutes, candles, days, weeks), history_days=days)
            del candles, shared
        daily_tfs = [m for m in timeframes if m in ct.DAILY_TIMEFRAMES]
        if daily_tfs:
            contexts.update(ct.daily_contexts(symbol, daily[symbol], regime, daily_tfs, through))
        del bars
        for minutes in timeframes:
            name = ct.file_name(symbol, minutes, through)
            path = out_dir / name
            with path.open("wb") as stream:
                rows = ct.write_table(stream, ct.build_rows(contexts.pop(minutes)), counts=counts)
            files.append({"name": name, "rows": rows, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        gc.collect()
    manifest = ct.build_manifest(files=files, counts=counts, inputs=inputs, through_month=through, parity=parity,
                                 symbols=symbols, timeframes=timeframes,
                                 code_commit=os.environ.get("GITHUB_SHA", "local"), created_utc=NOW_UTC)
    directory = ct.report_dir(manifest)
    root = checkout.path
    if (root / directory).exists():
        return [*ct.public_lines(manifest), "skipped: a report with this content hash already exists"]
    progress.phase("report write", files=len(files) + 1)
    (root / directory).mkdir(parents=True)
    names = []
    for item in files:
        shutil.copyfile(out_dir / item["name"], root / directory / item["name"])
        names.append(f"{directory}/{item['name']}")
    (root / directory / "manifest.json").write_bytes(canonical_bytes(manifest))
    names.append(f"{directory}/manifest.json")
    progress.phase("push", files=len(names))
    checkout.commit_and_push(names, f"Candle table {ct.SCHEMA}: report {manifest['content_hash']}")
    return ct.public_lines(manifest)


def _symbols(text: str) -> list[str]:
    symbols = [item.strip() for item in text.split(",") if item.strip()]
    if not symbols or len(set(symbols)) != len(symbols) or any(s not in lake.SYMBOLS for s in symbols):
        raise PublicError(f"symbols must be distinct values of {','.join(lake.SYMBOLS)}")
    return [symbol for symbol in lake.SYMBOLS if symbol in symbols]


def _timeframes(text: str) -> tuple:
    try:
        return ct.check_timeframes(int(item) for item in text.split(",") if item.strip())
    except (ValueError, ct.CandleTableError):
        raise PublicError(f"timeframes must be distinct values of {ct.TABLE_TIMEFRAMES}") from None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workdir", type=Path, default=Path("candle-table-work"))
    parser.add_argument("--summary", type=Path, help="append the public output (markdown)")
    parser.add_argument("--symbols", default=",".join(lake.SYMBOLS))
    parser.add_argument("--timeframes", default=",".join(str(m) for m in ct.TABLE_TIMEFRAMES))
    parser.add_argument("--through-month", default=ct.LAST_MONTH)
    parser.add_argument("--data-revision", type=int, default=1, help="rd revision of the 1-minute bars")
    parser.add_argument("--daily-revision", type=int, default=1, help="dk1 release revision")
    args = parser.parse_args(argv)
    args.symbols = _symbols(args.symbols)
    args.timeframes = _timeframes(args.timeframes)
    try:
        ct.check_through_month(args.through_month)
    except ValueError as error:
        raise PublicError(str(error)) from None
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    # Never remove a pre-existing caller directory; own only this newly created one.
    args.workdir.mkdir(parents=True, exist_ok=False)
    progress = CandleTableProgress(sys.stdout)  # captured before any _quiet redirect
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
        print(f"::error title=candle table build failed::{error}", flush=True)
        sys.exit(1)
    except Exception as error:  # library errors can contain private values: type name only
        print(f"::error title=candle table build failed::{type(error).__name__}", flush=True)
        sys.exit(1)
