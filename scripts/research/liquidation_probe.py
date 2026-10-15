#!/usr/bin/env python3
"""Probe of the Binance USD-M ``liquidationSnapshot`` public archive (docs/liquidation-data.md).

Answers what the archive actually contains before anything is built on it. It publishes nothing and
stores nothing: no release, no artifact, no file outside the temporary job summary. Output is a
table of findings only (counts, dates, byte totals, the observed header and counts of distinct values
of low-cardinality text columns); no row values, no prices or quantities.

Per symbol and family (daily, monthly) it lists the keys with the S3 listing API (following the
marker until the listing ends) and reports the zip key count, first and last period, missing periods
between them and the total compressed bytes. It then downloads three zips per symbol (first, middle
and last available; daily if the symbol has any, else monthly), verifies each against its .CHECKSUM
(404 = a recorded gap, a mismatch = an error) and profiles it. Downloads are limited to periods up
to 2025-12 so the hidden stretch is never read; the listing itself covers every key.

Standard library only; download mechanics are data_lake_build's.
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import os
from pathlib import Path
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ElementTree
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data_lake_build import fetch, write_summary  # noqa: E402
from market_analysis import data_lake as lake  # noqa: E402

S3_ROOT = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
FAMILIES = ("daily", "monthly")
MAX_PAGES = 1000
LAST_PROBE_PERIOD = "2025-12"  # downloads stop here: the hidden stretch (2026-01..) is never read
MAX_UNCOMPRESSED = 1 << 29
MAX_DISTINCT = 12
MAX_RANGES = 10
_INTEGER = re.compile(r"-?[0-9]+\Z")


class ProbeError(RuntimeError):
    """The archive differs from the expected layout; the message is safe for the public log."""


# ---------------------------------------------------------------- listing


def prefix(family: str, symbol: str) -> str:
    if family not in FAMILIES:
        raise ValueError(f"family must be one of {FAMILIES}")
    lake.validate_symbol(symbol)
    return f"data/futures/um/{family}/liquidationSnapshot/{symbol}/"


def listing_url(key_prefix: str, marker: str | None = None) -> str:
    query = [("delimiter", "/"), ("prefix", key_prefix)]
    if marker:
        query.append(("marker", marker))
    return S3_ROOT + "?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote, safe="/")


def parse_listing(body: bytes) -> tuple[list, bool, str | None]:
    """([(key, size)], is_truncated, next_marker) of one S3 ListBucketResult page (namespace-agnostic)."""
    if b"<!DOCTYPE" in body or b"<!ENTITY" in body:
        raise ProbeError("listing contains a DTD")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        raise ProbeError("listing is not valid XML") from None
    name = lambda element: element.tag.rsplit("}", 1)[-1]  # noqa: E731
    if name(root) != "ListBucketResult":
        raise ProbeError(f"unexpected listing root {name(root)!r}")
    keys, truncated, marker = [], False, None
    for child in root:
        tag = name(child)
        if tag == "IsTruncated":
            truncated = (child.text or "").strip().lower() == "true"
        elif tag == "NextMarker":
            marker = (child.text or "").strip() or None
        elif tag == "Contents":
            fields = {name(item): (item.text or "") for item in child}
            if "Key" not in fields or not _INTEGER.match(fields.get("Size", "")):
                raise ProbeError("listing entry without Key or integer Size")
            keys.append((fields["Key"], int(fields["Size"])))
    return keys, truncated, marker


def fetch_bytes(url: str) -> bytes:
    body, _, _ = fetch(url, None)
    return body


def list_keys(key_prefix: str, fetcher=fetch_bytes, max_pages: int = MAX_PAGES) -> list:
    """Every (key, size) under the prefix: pages are followed by marker (NextMarker, else the last key)."""
    out, marker = [], None
    for _ in range(max_pages):
        keys, truncated, next_marker = parse_listing(fetcher(listing_url(key_prefix, marker)))
        out.extend(keys)
        if not truncated:
            return out
        following = next_marker or (keys[-1][0] if keys else None)
        if following is None or following == marker:
            raise ProbeError("listing is truncated but the marker does not advance")
        marker = following
    raise ProbeError(f"listing exceeds {max_pages} pages")


# ---------------------------------------------------------------- periods and gaps


def period_pattern(family: str, symbol: str):
    stamp = r"\d{4}-\d{2}-\d{2}" if family == "daily" else r"\d{4}-\d{2}"
    return re.compile(rf"{re.escape(prefix(family, symbol))}{symbol}-liquidationSnapshot-({stamp})\.zip\Z")


def split_keys(family: str, symbol: str, listing: list) -> dict:
    """{'periods': sorted unique periods of the zip keys, 'bytes': compressed total, 'checksums': .CHECKSUM keys,
    'other': keys that match neither}."""
    pattern = period_pattern(family, symbol)
    periods, total, checksums, other = [], 0, 0, 0
    for key, size in listing:
        match = pattern.match(key)
        if match:
            periods.append(match.group(1))
            total += size
        elif key.endswith(".zip.CHECKSUM") and pattern.match(key[:-len(".CHECKSUM")]):
            checksums += 1
        else:
            other += 1
    return {"periods": sorted(set(periods)), "keys": len(periods), "bytes": total, "checksums": checksums,
            "other": other}


def missing_periods(periods: list, family: str) -> list:
    """Periods (YYYY-MM-DD or YYYY-MM) absent between the first and last of ``periods``."""
    if not periods:
        return []
    have = set(periods)
    if family == "daily":
        day, last = date.fromisoformat(min(have)), date.fromisoformat(max(have))
        out = []
        while day <= last:
            if day.isoformat() not in have:
                out.append(day.isoformat())
            day += timedelta(days=1)
        return out
    year, month = (int(part) for part in min(have).split("-"))
    last_year, last_month = (int(part) for part in max(have).split("-"))
    out = []
    while (year, month) <= (last_year, last_month):
        if f"{year:04d}-{month:02d}" not in have:
            out.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def compress_ranges(missing: list, family: str) -> list:
    """['2024-03-02..2024-03-05', '2024-04-01'] from sorted missing periods (consecutive ones merge)."""
    def number(text):
        if family == "daily":
            return date.fromisoformat(text).toordinal()
        year, month = (int(part) for part in text.split("-"))
        return year * 12 + month

    out, start, previous = [], None, None
    for text in missing:
        if start is not None and number(text) == number(previous) + 1:
            previous = text
            continue
        if start is not None:
            out.append(start if start == previous else f"{start}..{previous}")
        start = previous = text
    if start is not None:
        out.append(start if start == previous else f"{start}..{previous}")
    return out


def pick_probe_periods(periods: list, last_period: str = LAST_PROBE_PERIOD) -> list:
    """First, middle and last of the periods not after ``last_period`` (deduplicated, ascending)."""
    usable = [p for p in periods if p[:7] <= last_period]
    if not usable:
        return []
    return sorted({usable[0], usable[len(usable) // 2], usable[-1]})


# ---------------------------------------------------------------- zip profile


class Profile:
    """Streaming statistics of one zip: header, rows, timestamps, low-cardinality text columns, repeats."""

    def __init__(self):
        self.header, self.rows, self.time_column = None, 0, None
        self.first_ms = self.last_ms = self.min_ms = self.max_ms = None
        self.repeated, self.distinct = 0, {}

    def merge_distinct(self, other: dict) -> None:
        for column, counter in other.items():
            mine = self.distinct.setdefault(column, Counter())
            if mine is None or counter is None:
                self.distinct[column] = None
                continue
            mine.update(counter)
            if len(mine) > MAX_DISTINCT:
                self.distinct[column] = None


def _milliseconds(text: str) -> int | None:
    if not _INTEGER.match(text):
        return None
    value = int(text)
    if value >= 10 ** 15:
        return value // 1000  # microseconds
    return value * 1000 if value < 10 ** 11 else value  # seconds / milliseconds, by magnitude


def profile_zip(data: bytes) -> Profile:
    """Profile the single CSV member of a zip. The first line is a header unless its first field is an integer."""
    profile = Profile()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = [item for item in archive.infolist() if item.filename.lower().endswith(".csv")]
        if len(members) != 1:
            raise ProbeError(f"expected one csv member, found {len(members)}")
        if members[0].file_size > MAX_UNCOMPRESSED:
            raise ProbeError("csv member is larger than the probe limit")
        stream = io.TextIOWrapper(archive.open(members[0]), encoding="utf-8", newline="")
        reader = csv.reader(stream)
        previous, eligible, distinct = None, None, {}
        for row in reader:
            if not row:
                continue
            if profile.header is None and not _INTEGER.match(row[0].strip()):
                profile.header = tuple(item.strip() for item in row)
                continue
            if profile.header is None:
                profile.header = tuple(f"col{k}" for k in range(len(row)))
            if eligible is None:
                # time column: the first column named like *time* with an integer first value;
                # low-cardinality candidates: columns whose first value is not a number (text labels).
                for k, name in enumerate(profile.header[:len(row)]):
                    if profile.time_column is None and "time" in name.lower() and _milliseconds(row[k].strip()):
                        profile.time_column = k
                eligible = [k for k in range(len(row)) if k != profile.time_column
                            and not _is_number(row[k].strip())]
                distinct = {k: Counter() for k in eligible}
            profile.rows += 1
            if previous is not None and row == previous:
                profile.repeated += 1
            previous = row
            if profile.time_column is not None and profile.time_column < len(row):
                ms = _milliseconds(row[profile.time_column].strip())
                if ms is not None:
                    if profile.first_ms is None:
                        profile.first_ms = ms
                    profile.last_ms = ms
                    profile.min_ms = ms if profile.min_ms is None else min(profile.min_ms, ms)
                    profile.max_ms = ms if profile.max_ms is None else max(profile.max_ms, ms)
            for k in eligible:
                counter = distinct.get(k)
                if counter is None or k >= len(row):
                    continue
                counter[row[k].strip()] += 1
                if len(counter) > MAX_DISTINCT:
                    distinct[k] = None
        if profile.header is not None:
            for k, counter in distinct.items():
                profile.distinct[profile.header[k] if k < len(profile.header) else f"col{k}"] = counter
    return profile


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def _iso(ms: int | None) -> str:
    return "n/a" if ms is None else datetime.fromtimestamp(ms // 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- download


def fetch_or_none(url: str) -> bytes | None:
    """data_lake_build.fetch into memory; HTTP 404 is None, anything else unchanged."""
    try:
        return fetch_bytes(url)
    except RuntimeError as error:
        if str(error).startswith("download failed: HTTP 404:"):
            return None
        raise


def zip_url(family: str, symbol: str, period: str) -> str:
    return f"{S3_ROOT}/{prefix(family, symbol)}{symbol}-liquidationSnapshot-{period}.zip"


def download_verified(url: str, fetcher=fetch_or_none) -> bytes | None:
    """The zip, verified against its .CHECKSUM; None when either is HTTP 404 (a recorded gap)."""
    checksum = fetcher(url + ".CHECKSUM")
    body = fetcher(url) if checksum is not None else None
    if checksum is None or body is None:
        return None
    expected = lake.parse_checksum(checksum.decode("ascii", "replace"))
    actual = hashlib.sha256(body).hexdigest()
    if actual != expected:
        raise ProbeError(f"CHECKSUM MISMATCH for {url}: Binance {expected}, downloaded {actual}")
    return body


# ---------------------------------------------------------------- probe


def probe_symbol(symbol: str, lister=list_keys, fetcher=fetch_or_none) -> dict:
    families = {}
    for family in FAMILIES:
        found = split_keys(family, symbol, lister(prefix(family, symbol)))
        missing = missing_periods(found["periods"], family)
        families[family] = {**found, "first": found["periods"][0] if found["periods"] else None,
                            "last": found["periods"][-1] if found["periods"] else None,
                            "missing": len(missing), "missing_ranges": compress_ranges(missing, family)}
    source = next((f for f in FAMILIES if families[f]["periods"]), None)
    result = {"symbol": symbol, "families": families, "probe_family": source, "zips": [], "gaps": 0}
    if source is None:
        return result
    total = Profile()
    headers = []
    for period in pick_probe_periods(families[source]["periods"]):
        body = download_verified(zip_url(source, symbol, period), fetcher)
        if body is None:
            result["gaps"] += 1
            continue
        profile = profile_zip(body)
        headers.append(profile.header)
        total.merge_distinct(profile.distinct)
        total.rows += profile.rows
        total.repeated += profile.repeated
        result["zips"].append({"period": period, "bytes": len(body), "header": profile.header,
                               "rows": profile.rows, "first": _iso(profile.first_ms), "last": _iso(profile.last_ms),
                               "min": _iso(profile.min_ms), "max": _iso(profile.max_ms)})
    result["rows"], result["repeated"] = total.rows, total.repeated
    result["repeat_share"] = (total.repeated / max(total.rows - len(result["zips"]), 1)) if total.rows else None
    result["distinct"] = {name: dict(sorted(counter.items())) for name, counter in sorted(total.distinct.items())
                          if counter is not None}
    result["high_cardinality"] = sorted(name for name, counter in total.distinct.items() if counter is None)
    result["headers"] = sorted({",".join(h) for h in headers if h})
    return result


def report(results: list) -> list:
    lines = ["### Liquidation snapshot archive probe", "",
             "| symbol | family | zip keys | first | last | missing | bytes | checksums | other keys |",
             "|---|---|---|---|---|---|---|---|---|"]
    for result in results:
        for family in FAMILIES:
            item = result["families"][family]
            lines.append(f"| {result['symbol']} | {family} | {item['keys']} | {item['first'] or '-'} | "
                         f"{item['last'] or '-'} | {item['missing']} | {item['bytes']} | {item['checksums']} | "
                         f"{item['other']} |")
    for result in results:
        lines += ["", f"#### {result['symbol']}"]
        for family in FAMILIES:
            ranges = result["families"][family]["missing_ranges"]
            if ranges:
                shown = ", ".join(ranges[:MAX_RANGES]) + (f", ... (+{len(ranges) - MAX_RANGES} ranges)"
                                                           if len(ranges) > MAX_RANGES else "")
                lines.append(f"- missing {family} periods: {shown}")
        if result["probe_family"] is None:
            lines.append("- no zip keys in either family: nothing to download")
            continue
        lines.append(f"- probed family: {result['probe_family']} (downloads limited to periods <= "
                     f"{LAST_PROBE_PERIOD}); download gaps (404): {result['gaps']}")
        for header in result["headers"]:
            lines.append(f"- observed header: `{header}`")
        if len(result["headers"]) > 1:
            lines.append("- WARNING: the probed zips have different headers")
        for item in result["zips"]:
            lines.append(f"- {item['period']}: rows={item['rows']} bytes={item['bytes']} first={item['first']} "
                         f"last={item['last']} min={item['min']} max={item['max']}")
        if result["repeat_share"] is not None:
            lines.append(f"- consecutive identical rows: {result['repeated']} of {result['rows']} rows "
                         f"(share {result['repeat_share']:.4f})")
        for name, counts in result["distinct"].items():
            lines.append(f"- {name}: " + ", ".join(f"{value}={count}" for value, count in counts.items()))
        if result["high_cardinality"]:
            lines.append(f"- text columns with more than {MAX_DISTINCT} distinct values: "
                         + ", ".join(result["high_cardinality"]))
    return lines


def parse_symbols(text: str) -> list:
    if not re.fullmatch(r"[A-Z]+(,[A-Z]+)*", text or ""):
        raise ProbeError("symbols must be a comma-separated list without spaces")
    symbols = text.split(",")
    if len(set(symbols)) != len(symbols) or any(s not in lake.SYMBOLS for s in symbols):
        raise ProbeError(f"symbols must be distinct members of {','.join(lake.SYMBOLS)}")
    return symbols


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbols", default=",".join(lake.SYMBOLS))
    parser.add_argument("--summary", help="append the findings (markdown) to this file")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    symbols = parse_symbols(args.symbols)
    lines = report([probe_symbol(symbol) for symbol in symbols])
    print("\n".join(lines), flush=True)
    write_summary(args.summary or os.environ.get("LIQUIDATION_PROBE_SUMMARY"), lines)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # public log: the type and the (URL-only) message, no data
        print(f"::error title=liquidation probe failed::{type(error).__name__}: {error}", flush=True)
        sys.exit(1)
