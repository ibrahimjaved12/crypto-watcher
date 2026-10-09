#!/usr/bin/env python3
"""Binance metrics archive builder (#224 phase 1): one immutable release per symbol-month.

For one (SYMBOL, finished MONTH) it downloads every daily
``metrics/<SYMBOL>/<SYMBOL>-metrics-YYYY-MM-DD.zip`` with its ``.CHECKSUM`` (HTTP 404 =
a recorded gap, a checksum mismatch = an error), parses each zip header-driven
(``market_analysis.metrics_lake``; an unexpected header fails with the observed
header), and writes the csv.gz, a tar of the original zips and the manifest. With
--publish it creates ``mx-SYMBOL-MONTH-rN`` in the private research-data repository
as a draft, uploads and verifies every asset (manifest last) and only then
publishes it, never marked Latest. Download, checksum, upload and publish mechanics
are data_lake_build's (imported). A month without any day is reported as empty and
never published.

Published releases are never edited or deleted. Logs and summaries carry only
names, sizes, hashes, the observed header text and aggregate statistics.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data_lake_build import ResearchDataRepo, annotate_failure, fetch, file_sha256, write_summary  # noqa: E402
from market_analysis import data_lake as lake  # noqa: E402
from market_analysis import metrics_lake as mx  # noqa: E402


def fetch_or_none(url: str) -> bytes | None:
    """data_lake_build.fetch into memory; HTTP 404 (a missing day) is None, anything else unchanged."""
    try:
        body, _, _ = fetch(url, None)
    except RuntimeError as error:
        if str(error).startswith("download failed: HTTP 404:"):
            return None
        raise
    return body


def build(symbol: str, month: str, tag: str, workdir: Path, fetcher=fetch_or_none) -> tuple[dict | None, list]:
    """(manifest, uploads); manifest None when the month has no day at all."""
    days = mx.collect_month(symbol, month, fetcher)
    rows_by_day, day_records, headers, formats = {}, {}, set(), set()
    for day, item in days.items():
        if item is None:
            rows_by_day[day] = None
            day_records[day] = {"status": "missing"}
            continue
        rows, info = mx.parse_daily_zip(item["zip"], symbol, day)
        rows_by_day[day] = rows
        headers.add(info["header"])
        if info["timestamp_format"]:
            formats.add(info["timestamp_format"])
        day_records[day] = {"status": "present", "file": item["file"], "sha256": item["sha256"],
                            "binance_checksum_sha256": item["checksum_sha256"], "checksum_verified": True,
                            "bytes": len(item["zip"]), **{key: info[key] for key in ("rows", "rows_outside_day",
                                                                                  "empty_values",
                                                                                  "timestamp_format")}}
    if not any(rows is not None for rows in rows_by_day.values()):
        return None, []
    if len(formats) > 1:
        raise mx.MetricsFormatError(f"{symbol} {month}: create_time formats differ between days: {sorted(formats)}")
    stats = mx.coverage(rows_by_day)
    rows, conflicts = mx.merged_rows(rows_by_day)
    stats["conflicting_duplicate_timestamps"] = conflicts
    workdir.mkdir(parents=True, exist_ok=True)
    csv_name, raw_name = mx.csv_asset_name(symbol, month), mx.raw_asset_name(symbol, month)
    with (workdir / csv_name).open("wb") as stream:
        written = mx.write_metrics_csv_gz(stream, rows)
    with (workdir / raw_name).open("wb") as stream:
        mx.write_raw_tar(stream, {item["file"]: item["zip"] for item in days.values() if item is not None})
    assets = []
    for name, count in ((csv_name, written), (raw_name, sum(1 for item in days.values() if item is not None))):
        sha, size = file_sha256(workdir / name)
        assets.append({"name": name, "sha256": sha, "bytes": size, "rows": count})
    manifest = {"manifest_version": mx.MANIFEST_VERSION, "schema": mx.SCHEMA, "release_tag": tag, "symbol": symbol,
                "month": month, "source": f"{mx.BASE_URL}/{mx.FAMILY}/{symbol}/",
                "header": sorted(headers)[0] if headers else None, "timestamp_format": formats.pop() if formats else None,
                "point_in_time": {"period_ms": mx.PERIOD_MS, "lag_periods": mx.POINT_IN_TIME_LAG_PERIODS,
                                  "rule": "row stamped T describes the 5-minute period ending at T; usable from "
                                          "T + lag_periods * period_ms (conservative until verified)"},
                "columns": list(mx.CSV_COLUMNS), "days": day_records, "coverage": stats, "assets": assets}
    manifest_name = mx.manifest_asset_name(symbol, month)
    (workdir / manifest_name).write_text(lake.canonical_json(manifest), encoding="ascii")
    uploads = [{"name": asset["name"], "path": workdir / asset["name"], "sha256": asset["sha256"],
                "bytes": asset["bytes"]} for asset in assets]
    sha, size = file_sha256(workdir / manifest_name)
    uploads.append({"name": manifest_name, "path": workdir / manifest_name, "sha256": sha, "bytes": size})
    return manifest, uploads


def publish_release(repo: ResearchDataRepo, tag: str, symbol: str, month: str, uploads: list[dict]) -> dict:
    body = (f"Binance USD-M metrics {symbol} {month} ({mx.SCHEMA}): daily 5-minute open interest and long/short "
            "ratios from the Binance public archive (data.binance.vision), the original daily zips with verified "
            "checksums, a csv.gz sorted by timestamp and a manifest with per-day hashes and coverage statistics.")
    revision = tag.rsplit("-r", 1)[1]
    release = repo.create_draft(tag, f"{symbol} · {month} · Binance metrics (5-minute OI, long/short "
                                     f"ratios) (r{revision})", body)
    for upload in uploads:  # manifest last
        repo.upload_verified(release, upload["path"], upload["name"], upload["sha256"], upload["bytes"])
        print(f"uploaded and verified {upload['name']}", flush=True)
    return repo.publish(release, tag)  # never marked Latest


def summary_lines(tag: str, status: str, manifest: dict | None) -> list[str]:
    lines = [f"### Metrics archive `{tag}`: {status}", ""]
    if manifest is None:
        return lines
    lines += ["| asset | bytes | rows | sha256 |", "| --- | ---: | ---: | --- |"]
    lines += [f"| `{a['name']}` | {a['bytes']} | {a['rows']} | `{a['sha256']}` |" for a in manifest["assets"]]
    stats = manifest["coverage"]
    lines += ["", f"Header: `{manifest['header']}`; timestamp format: {manifest['timestamp_format']}.",
              "", "| statistic | value |", "| --- | --- |"]
    lines += [f"| {key} | {', '.join(value) if isinstance(value, list) else value} |" for key, value in stats.items()]
    return lines


def notice(tag: str, status: str, manifest: dict | None) -> None:
    fields = {"status": status}
    if manifest is not None:
        fields.update(header=manifest["header"], timestamp_format=manifest["timestamp_format"],
                      bytes={a["name"]: a["bytes"] for a in manifest["assets"]}, coverage=manifest["coverage"])
    text = json.dumps(fields, sort_keys=True, separators=(",", ":"))[:3000]
    print(f"::notice title=metrics {tag}::" + text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"),
          flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--month", required=True, help="finished UTC month, YYYY-MM")
    parser.add_argument("--revision", type=int, default=1)
    parser.add_argument("--workdir", default="metrics-build-work")
    parser.add_argument("--publish", action="store_true", help="create the release (otherwise a dry run)")
    parser.add_argument("--summary", help="append a markdown summary to this file")
    args = parser.parse_args(argv)
    try:
        symbol = lake.validate_symbol(args.symbol)
        month = lake.validate_month(args.month)
        tag = mx.release_tag(symbol, month, args.revision)
    except ValueError as error:
        parser.error(str(error))
    if month < mx.FIRST_ARCHIVE_MONTH or not lake.month_finished(month, int(time.time() * 1000)):
        parser.error(f"{month} is before {mx.FIRST_ARCHIVE_MONTH} or not a fully finished UTC month")

    repo = None
    if args.publish:
        repo = ResearchDataRepo(os.environ.get("RESEARCH_DATA_REPOSITORY"), os.environ.get("RESEARCH_DATA_TOKEN"))
        if repo.published_release(tag) is not None:
            print(f"{tag}: exists (published releases are immutable); nothing to do")
            write_summary(args.summary, summary_lines(tag, "exists", None))
            notice(tag, "exists", None)
            return 0
        for draft in repo.draft_releases(tag):
            repo.delete_draft(draft)
            print(f"{tag}: deleted a draft left by an interrupted run")

    started = time.monotonic()
    manifest, uploads = build(symbol, month, tag, Path(args.workdir))
    if manifest is None:
        print(f"{tag}: no daily file exists for this month; nothing built or published")
        write_summary(args.summary, summary_lines(tag, "empty (no daily files)", None))
        notice(tag, "empty", None)
        return 0
    too_large = [upload["name"] for upload in uploads if upload["bytes"] >= lake.MAX_ASSET_BYTES]
    if too_large:
        raise RuntimeError(f"assets at or above the 2 GiB release limit: {too_large}")
    print(f"built {tag} in {time.monotonic() - started:.1f}s", flush=True)
    if repo is None:
        print(lake.canonical_json(manifest), end="")
        write_summary(args.summary, summary_lines(tag, "dry run (not published)", manifest))
        notice(tag, "dry run", manifest)
        return 0
    published = publish_release(repo, tag, symbol, month, uploads)
    print(f"{tag}: published release {published['id']}")
    write_summary(args.summary, summary_lines(tag, "published", manifest))
    notice(tag, "published", manifest)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as error:  # noqa: BLE001 - re-raised after annotating
        annotate_failure(error)
        raise
    sys.exit(code)
