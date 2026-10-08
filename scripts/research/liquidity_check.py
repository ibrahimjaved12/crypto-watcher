#!/usr/bin/env python3
"""Liquidity check of candidate USDT perpetuals against the frozen six (#183).

For every symbol (25 candidates + the frozen six) and month it downloads the
published Binance USD-M monthly 1m kline zip (data.binance.vision, um/monthly/
klines), verifies it against its .CHECKSUM, streams the CSV out of the zip and
sums quote volume (exact Decimal) and trades. A month without a published zip
(HTTP 404 on its .CHECKSUM) counts as not available; any other failure stops the run.

Per month the symbols with data are ranked by quote volume (1 = largest), and each
candidate's quote volume is divided by the smallest of the frozen six that month
(the sixth-largest of the six). Output is one public-safe table: per candidate the
months available, the minimum and median of that ratio, and the minimum (best)
rank. Nothing else from the archive is printed or kept. The ratios cover the whole
window, so they are a screening aid, not a point-in-time liquidity rule.

Standard library only; zips are deleted as soon as they are summed.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
import io
from pathlib import Path
import shutil
import sys
import urllib.error
import urllib.request
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_lake_build import USER_AGENT, fetch  # noqa: E402
from market_analysis import data_lake as lake  # noqa: E402

CANDIDATES = ("ADAUSDT", "LINKUSDT", "AVAXUSDT", "TRXUSDT", "LTCUSDT", "SUIUSDT", "1000PEPEUSDT", "BCHUSDT",
              "DOTUSDT", "TONUSDT", "NEARUSDT", "APTUSDT", "ARBUSDT", "WLDUSDT", "ENAUSDT", "XLMUSDT",
              "1000SHIBUSDT", "ONDOUSDT", "AAVEUSDT", "UNIUSDT", "FILUSDT", "ATOMUSDT", "INJUSDT", "OPUSDT",
              "HYPEUSDT")
FROZEN = lake.SYMBOLS
QUOTE_VOLUME, TRADES = 7, 8  # Binance kline CSV columns
OPEN_TIME = 0


@dataclass(frozen=True)
class MonthTotals:
    quote_volume: Decimal
    trades: int
    rows: int


def kline_url(symbol: str, month: str) -> str:
    lake.validate_month(month)
    return f"{lake.BINANCE_BASE}/klines/{symbol}/1m/{symbol}-1m-{month}.zip"


def published_checksum(url: str, attempts: int = 4) -> str | None:
    """sha256 from the .CHECKSUM, or None when Binance has not published this month (404)."""
    request = urllib.request.Request(url + ".CHECKSUM", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        body, _, _ = fetch(url + ".CHECKSUM", None, attempts)  # retries 429/5xx, fails on the rest
    return lake.parse_checksum(body.decode("ascii", "replace"))


def sum_klines(stream) -> MonthTotals:
    """Quote volume and trades of a kline CSV (with or without the header row)."""
    quote, trades, rows = Decimal(0), 0, 0
    for row in csv.reader(io.TextIOWrapper(stream, encoding="ascii", newline="")):
        if not row or not row[OPEN_TIME].isdigit():
            if rows == 0 and row:  # header of newer archives
                continue
            raise ValueError("unexpected non-data kline row")
        quote += lake.exact_decimal(row[QUOTE_VOLUME])
        trades += int(row[TRADES])
        rows += 1
    return MonthTotals(quote, trades, rows)


def month_totals(symbol: str, month: str, workdir: Path) -> MonthTotals | None:
    url = kline_url(symbol, month)
    expected = published_checksum(url)
    if expected is None:
        return None
    path = workdir / f"{symbol}-1m-{month}.zip"
    try:
        _, actual, _ = fetch(url, path)
        if actual != expected:
            raise RuntimeError(f"CHECKSUM MISMATCH for {url}")
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != 1:
                raise ValueError(f"{path.name}: expected one CSV, found {len(names)} members")
            with archive.open(names[0]) as stream:
                return sum_klines(stream)
    finally:
        path.unlink(missing_ok=True)


def median(values: list[Fraction]) -> Fraction:
    ordered, n = sorted(values), len(values)
    return ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2


def summarize(totals: dict, months: list[str], candidates=CANDIDATES, frozen=FROZEN) -> list[dict]:
    """Per candidate: months available, min/median ratio to the smallest frozen symbol, best rank.

    ``totals[(symbol, month)]`` is a MonthTotals or None (not published). Every
    frozen symbol must have every month.
    """
    rows = []
    ranks = {}
    floors = {}
    for month in months:
        present = {symbol: totals[(symbol, month)].quote_volume for symbol in (*candidates, *frozen)
                   if totals.get((symbol, month)) is not None}
        missing = [symbol for symbol in frozen if symbol not in present]
        if missing:
            raise ValueError(f"{month}: frozen symbols without data: {', '.join(missing)}")
        floors[month] = min(present[symbol] for symbol in frozen)
        ordered = sorted(present, key=lambda symbol: (-present[symbol], symbol))
        ranks.update({(symbol, month): position for position, symbol in enumerate(ordered, 1)})
    for symbol in candidates:
        available = [month for month in months if totals.get((symbol, month)) is not None]
        ratios = [Fraction(totals[(symbol, month)].quote_volume) / Fraction(floors[month]) for month in available]
        rows.append({"symbol": symbol, "months": len(available),
                     "min_ratio": min(ratios) if ratios else None, "median_ratio": median(ratios) if ratios else None,
                     "min_rank": min(ranks[(symbol, month)] for month in available) if available else None})
    rows.sort(key=lambda row: (row["median_ratio"] is None, -(row["median_ratio"] or 0), row["symbol"]))
    return rows


def _ratio(value: Fraction | None) -> str:
    return "-" if value is None else f"{float(value):.3f}"


def table(rows: list[dict], months: list[str], universe: int) -> list[str]:
    lines = [f"### Liquidity check {months[0]}..{months[-1]} ({len(months)} months, {universe} symbols ranked)", "",
             "Ratio = monthly quote volume / smallest monthly quote volume of the frozen six in that month; "
             "rank 1 = largest quote volume among symbols with data that month.", "",
             "| candidate | months available | min ratio | median ratio | min rank |",
             "| --- | ---: | ---: | ---: | ---: |"]
    for row in rows:
        lines.append(f"| {row['symbol']} | {row['months']}/{len(months)} | {_ratio(row['min_ratio'])} | "
                     f"{_ratio(row['median_ratio'])} | {row['min_rank'] if row['min_rank'] is not None else '-'} |")
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--first-month", default="2024-01")
    parser.add_argument("--last-month", default="2026-09")
    parser.add_argument("--workdir", type=Path, default=Path("liquidity-check-work"))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--summary", type=Path, help="append the table (markdown)")
    args = parser.parse_args(argv)
    months = lake.months_between(args.first_month, args.last_month)
    symbols = (*CANDIDATES, *FROZEN)
    args.workdir.mkdir(parents=True, exist_ok=False)
    try:
        jobs = [(symbol, month) for symbol in symbols for month in months]
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            results = list(pool.map(lambda job: month_totals(*job, args.workdir), jobs))
    finally:
        shutil.rmtree(args.workdir, ignore_errors=True)
    totals = dict(zip(jobs, results))
    published = sum(result is not None for result in results)
    print(f"summed {published} of {len(jobs)} symbol-months (checksums verified; others not published)", flush=True)
    lines = table(summarize(totals, months), months, len(symbols))
    print("\n".join(lines), flush=True)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
