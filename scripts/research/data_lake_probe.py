#!/usr/bin/env python3
"""E4 probe for the research data lake plan (#183).

Downloads ONE symbol-month from the Binance public archive (data.binance.vision),
verifies each zip against its .CHECKSUM, streams the aggTrades zip WITHOUT
extracting it, aggregates it to 1-minute buckets, and reconciles those buckets
against the 1m trade klines. It writes a small JSON report: sizes, timings,
peak disk use, gaps, duplicates and reconciliation differences.

Standard library only. It never writes outside --workdir and publishes nothing.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import io
import json
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "https://data.binance.vision/data/futures/um/monthly"
MINUTE_MS = 60_000
CHECKSUM_LINE = re.compile(r"([0-9a-fA-F]{64})\s+\*?(\S+)")

# family -> (directory, file stem builder)
KLINE_FAMILIES = {
    "klines": "klines",
    "markPriceKlines": "markPriceKlines",
    "indexPriceKlines": "indexPriceKlines",
    "premiumIndexKlines": "premiumIndexKlines",
}


def month_bounds_ms(month: str) -> tuple[int, int, int]:
    year, mon = (int(part) for part in month.split("-"))
    days = calendar.monthrange(year, mon)[1]
    start = calendar.timegm((year, mon, 1, 0, 0, 0))
    return start * 1000, (start + days * 86400) * 1000, days


def fetch(url: str, destination: Path | None, attempts: int = 4) -> tuple[bytes | None, str, int]:
    """Download url; return (body if destination is None, sha256, bytes)."""
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = Request(url, headers={"User-Agent": "crypto-watcher-data-lake-probe"})
            with urlopen(request, timeout=60) as response:
                digest = hashlib.sha256()
                size = 0
                chunks: list[bytes] = []
                stream = destination.open("wb") if destination else None
                try:
                    while True:
                        block = response.read(1 << 20)
                        if not block:
                            break
                        digest.update(block)
                        size += len(block)
                        if stream:
                            stream.write(block)
                        else:
                            chunks.append(block)
                finally:
                    if stream:
                        stream.close()
                return (None if destination else b"".join(chunks)), digest.hexdigest(), size
        except HTTPError as error:
            if error.code == 404:
                raise
            last = error
        except (URLError, TimeoutError, ConnectionError) as error:
            last = error
        time.sleep(2 * attempt)
    raise RuntimeError(f"download failed after {attempts} attempts: {url}: {last}")


def download_verified(url: str, destination: Path) -> dict:
    started = time.monotonic()
    body, _, _ = fetch(url + ".CHECKSUM", None)
    match = CHECKSUM_LINE.search(body.decode("ascii", "replace"))
    if not match:
        raise RuntimeError(f"unparseable checksum file for {url}")
    expected = match.group(1).lower()
    _, actual, size = fetch(url, destination)
    if actual != expected:
        raise RuntimeError(f"CHECKSUM MISMATCH for {url}: expected {expected}, got {actual}")
    return {
        "url": url,
        "bytes": size,
        "sha256": actual,
        "seconds": round(time.monotonic() - started, 2),
        "checksum_verified": True,
    }


def rows_from_zip(path: Path):
    with zipfile.ZipFile(path) as archive:
        members = [info for info in archive.infolist() if info.filename.endswith(".csv")]
        if len(members) != 1:
            raise RuntimeError(f"{path.name}: expected exactly one CSV member, found {len(members)}")
        with archive.open(members[0]) as raw:
            text = io.TextIOWrapper(raw, encoding="ascii", newline="")
            for row in csv.reader(text):
                if not row or not row[0].lstrip("-").isdigit():
                    continue  # header line (Binance added headers to some files)
                yield row


def analyse_klines(path: Path, month: str) -> dict:
    start_ms, end_ms, days = month_bounds_ms(month)
    expected = days * 1440
    seen: dict[int, list[str]] = {}
    duplicates = 0
    out_of_month = 0
    for row in rows_from_zip(path):
        open_time = int(row[0])
        if open_time < start_ms or open_time >= end_ms:
            out_of_month += 1
            continue
        if open_time in seen:
            duplicates += 1
        seen[open_time] = row
    missing = [t for t in range(start_ms, end_ms, MINUTE_MS) if t not in seen]
    return {
        "rows": len(seen),
        "expected_rows": expected,
        "missing_minutes": len(missing),
        "first_missing_ms": missing[:5],
        "duplicate_rows": duplicates,
        "rows_outside_month": out_of_month,
        "_by_minute": seen,
    }


def analyse_aggtrades(path: Path, month: str) -> dict:
    start_ms, end_ms, _ = month_bounds_ms(month)
    buckets: dict[int, list[float]] = {}  # minute -> [qty, taker_buy_qty, trades]
    rows = 0
    last_id = -1
    id_gaps = 0
    id_not_increasing = 0
    outside = 0
    started = time.monotonic()
    for row in rows_from_zip(path):
        rows += 1
        agg_id = int(row[0])
        if agg_id <= last_id:
            id_not_increasing += 1
        elif last_id >= 0 and agg_id != last_id + 1:
            id_gaps += 1
        last_id = max(last_id, agg_id)
        transact = int(row[5])
        if transact < start_ms or transact >= end_ms:
            outside += 1
            continue
        quantity = float(row[2])
        buyer_is_maker = row[6].strip().lower() == "true"
        bucket = buckets.setdefault(transact - transact % MINUTE_MS, [0.0, 0.0, 0.0])
        bucket[0] += quantity
        if not buyer_is_maker:  # taker was the buyer
            bucket[1] += quantity
        bucket[2] += 1
    return {
        "rows": rows,
        "id_gaps": id_gaps,
        "id_not_increasing": id_not_increasing,
        "rows_outside_month": outside,
        "minutes_with_trades": len(buckets),
        "parse_seconds": round(time.monotonic() - started, 1),
        "_buckets": buckets,
    }


def reconcile(klines: dict, agg: dict) -> dict:
    by_minute = klines["_by_minute"]
    buckets = agg["_buckets"]
    worst_volume = 0.0
    worst_taker = 0.0
    volume_mismatch = 0
    taker_mismatch = 0
    only_in_klines_with_volume = 0
    only_in_aggtrades = 0
    for minute, row in by_minute.items():
        kline_volume = float(row[5])
        kline_taker = float(row[9])
        bucket = buckets.get(minute)
        if bucket is None:
            if kline_volume > 0:
                only_in_klines_with_volume += 1
            continue
        for kline_value, agg_value, which in (
            (kline_volume, bucket[0], "volume"),
            (kline_taker, bucket[1], "taker"),
        ):
            scale = max(abs(kline_value), 1e-12)
            relative = abs(kline_value - agg_value) / scale
            if which == "volume":
                worst_volume = max(worst_volume, relative)
                volume_mismatch += relative > 1e-6
            else:
                worst_taker = max(worst_taker, relative)
                taker_mismatch += relative > 1e-6
    for minute in buckets:
        if minute not in by_minute:
            only_in_aggtrades += 1
    return {
        "volume_minutes_mismatching_1e-6": int(volume_mismatch),
        "taker_buy_minutes_mismatching_1e-6": int(taker_mismatch),
        "worst_relative_volume_diff": worst_volume,
        "worst_relative_taker_diff": worst_taker,
        "minutes_with_kline_volume_but_no_aggtrades": only_in_klines_with_volume,
        "minutes_with_aggtrades_but_no_kline": only_in_aggtrades,
    }


def public(analysis: dict) -> dict:
    return {key: value for key, value in analysis.items() if not key.startswith("_")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--month", default="2025-01", help="YYYY-MM")
    parser.add_argument("--workdir", default="data-lake-probe")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Z0-9]{5,20}", args.symbol) or not re.fullmatch(r"\d{4}-\d{2}", args.month):
        parser.error("invalid --symbol or --month")

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    symbol, month = args.symbol, args.month
    started = time.monotonic()
    disk_start_free = shutil.disk_usage(workdir).free
    report: dict = {"symbol": symbol, "month": month, "downloads": {}}

    plan = {"aggTrades": f"{BASE}/aggTrades/{symbol}/{symbol}-aggTrades-{month}.zip"}
    for family, directory in KLINE_FAMILIES.items():
        plan[family] = f"{BASE}/{directory}/{symbol}/1m/{symbol}-1m-{month}.zip"
    plan["fundingRate"] = f"{BASE}/fundingRate/{symbol}/{symbol}-fundingRate-{month}.zip"

    paths: dict[str, Path] = {}
    for family, url in plan.items():
        destination = workdir / f"{family}__{Path(url).name}"
        try:
            report["downloads"][family] = download_verified(url, destination)
            paths[family] = destination
        except HTTPError as error:
            report["downloads"][family] = {"url": url, "error": f"HTTP {error.code}"}
    peak_used = disk_start_free - shutil.disk_usage(workdir).free
    report["disk_used_after_downloads_bytes"] = peak_used

    if "klines" in paths and "aggTrades" in paths:
        klines = analyse_klines(paths["klines"], month)
        agg = analyse_aggtrades(paths["aggTrades"], month)
        report["klines"] = public(klines)
        report["aggTrades"] = public(agg)
        report["reconciliation"] = reconcile(klines, agg)
    for family in ("markPriceKlines", "indexPriceKlines", "premiumIndexKlines"):
        if family in paths:
            analysis = analyse_klines(paths[family], month)
            report[family] = public(analysis)
    if "fundingRate" in paths:
        funding = [row for row in rows_from_zip(paths["fundingRate"])]
        report["fundingRate"] = {"rows": len(funding)}

    report["disk_used_peak_bytes"] = disk_start_free - shutil.disk_usage(workdir).free
    report["total_seconds"] = round(time.monotonic() - started, 1)
    output = workdir / "report.json"
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
