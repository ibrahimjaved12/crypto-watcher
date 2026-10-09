#!/usr/bin/env python3
"""Daily bars and funding history builder (dk1, #222/#223): one immutable release per symbol and month range.

For one SYMBOL and FIRST..LAST finished months it downloads, per month, the Binance
USD-M ``klines/<SYMBOL>/1d`` zip and the ``fundingRate`` zip with their ``.CHECKSUM``
files (HTTP 404 = a recorded gap, as metrics_build.fetch_or_none; a checksum
mismatch = an error), parses them (``market_analysis.daily_lake``) and writes the
daily csv.gz, the combined funding csv.gz, a tar of the original zips and the
manifest. With --publish it creates ``dk1-SYMBOL-FIRST_LAST-rN`` in the private
research-data repository as metrics_build does: a draft, verified uploads with the
manifest last, then publication, never marked Latest. A range without any daily
kline is reported as empty and never published.

The dataset is indicator warm-up and context only; evaluation segments are unchanged.
Published releases are never edited or deleted. Logs and summaries carry only
names, sizes, hashes and aggregate statistics.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data_lake_build import ResearchDataRepo, annotate_failure, file_sha256, write_summary  # noqa: E402
from metrics_build import fetch_or_none  # noqa: E402
from market_analysis import daily_lake as dk  # noqa: E402
from market_analysis import data_lake as lake  # noqa: E402
from market_analysis import metrics_lake  # noqa: E402


def build(symbol: str, first: str, last: str, tag: str, workdir: Path, fetcher=fetch_or_none) -> tuple:
    """(manifest, uploads); (None, []) when no month has a daily kline file."""
    months = lake.months_between(first, last)
    sources = dk.collect(symbol, months, fetcher)
    daily_rows, outside, funding_lines, files, month_records = [], 0, [], {}, {}
    funding_stats = {"calc_time_not_increasing": 0, "rows_outside_month": 0}
    for month in months:
        record = {}
        for kind in ("klines", "funding"):
            item = sources[(kind, month)]
            if item is None:
                record[kind] = {"status": "missing"}
                continue
            files[item["file"]] = item["zip"]
            entry = {"status": "present", "file": item["file"], "url": item["url"], "sha256": item["sha256"],
                     "binance_checksum_sha256": item["checksum_sha256"], "checksum_verified": True,
                     "bytes": len(item["zip"])}
            if kind == "klines":
                rows, stats = dk.parse_kline_zip(item["zip"], month, item["file"])
                daily_rows.extend(rows)
                outside += stats["rows_outside_month"]
                entry.update(stats)
            else:
                lines, stats = dk.funding_month_lines(item["zip"], month, item["file"])
                funding_lines.extend(lines)
                for key in funding_stats:
                    funding_stats[key] += stats[key]
                entry.update(rows=stats["rows"], raw_rows=stats["raw_rows"])
            record[kind] = entry
        month_records[month] = record
    if not daily_rows:
        return None, []
    rows, duplicates, conflicts = dk.merge_daily(daily_rows)
    times = [int(line.split(",", 1)[0]) for line in funding_lines]
    intervals = [int(line.split(",", 2)[1]) for line in funding_lines]
    stats = dk.coverage(rows, duplicates=duplicates, conflicts=conflicts, rows_outside_month=outside,
                        funding_times=times, funding_intervals=intervals)
    stats["funding_calc_time_not_increasing"] = funding_stats["calc_time_not_increasing"]
    stats["funding_rows_outside_month"] = funding_stats["rows_outside_month"]
    workdir.mkdir(parents=True, exist_ok=True)
    names = (dk.daily_asset_name(symbol, first, last), dk.funding_asset_name(symbol, first, last),
             dk.raw_asset_name(symbol, first, last))
    with (workdir / names[0]).open("wb") as stream:
        daily_count = dk.write_daily_csv_gz(stream, rows)
    with (workdir / names[1]).open("wb") as stream:
        funding_count = dk.write_funding_range_csv_gz(stream, funding_lines)
    with (workdir / names[2]).open("wb") as stream:
        metrics_lake.write_raw_tar(stream, files)
    assets = []
    for name, count in zip(names, (daily_count, funding_count, len(files))):
        sha, size = file_sha256(workdir / name)
        assets.append({"name": name, "sha256": sha, "bytes": size, "rows": count})
    manifest = {"manifest_version": dk.MANIFEST_VERSION, "schema": dk.SCHEMA, "release_tag": tag, "symbol": symbol,
                "first_month": first, "last_month": last,
                "purpose": "indicator warm-up and context only; evaluation segments are unchanged",
                "daily_columns": list(dk.CSV_COLUMNS), "funding_columns": list(lake.FUNDING_COLUMNS),
                "months": month_records, "coverage": stats, "assets": assets}
    manifest_name = dk.manifest_asset_name(symbol, first, last)
    (workdir / manifest_name).write_text(lake.canonical_json(manifest), encoding="ascii")
    uploads = [{"name": a["name"], "path": workdir / a["name"], "sha256": a["sha256"], "bytes": a["bytes"]}
               for a in assets]
    sha, size = file_sha256(workdir / manifest_name)
    uploads.append({"name": manifest_name, "path": workdir / manifest_name, "sha256": sha, "bytes": size})
    return manifest, uploads


def publish_release(repo: ResearchDataRepo, tag: str, symbol: str, first: str, last: str, uploads: list) -> dict:
    body = (f"Daily bars and funding history {symbol} {first}..{last} ({dk.SCHEMA}) from the Binance public archive "
            "(data.binance.vision): 1d klines and funding settlements with verified checksums, the original monthly "
            "zips and a manifest with coverage statistics. Indicator warm-up and context only, not evaluation data.")
    revision = tag.rsplit("-r", 1)[1]
    release = repo.create_draft(tag, f"{symbol} · {first}..{last} · daily bars + funding history "
                                     f"(warm-up) (r{revision})", body)
    for upload in uploads:  # manifest last
        repo.upload_verified(release, upload["path"], upload["name"], upload["sha256"], upload["bytes"])
        print(f"uploaded and verified {upload['name']}", flush=True)
    return repo.publish(release, tag)  # never marked Latest


def summary_lines(tag: str, status: str, manifest: dict | None) -> list[str]:
    lines = [f"### Daily history `{tag}`: {status}", ""]
    if manifest is None:
        return lines
    lines += ["| asset | bytes | rows | sha256 |", "| --- | ---: | ---: | --- |"]
    lines += [f"| `{a['name']}` | {a['bytes']} | {a['rows']} | `{a['sha256']}` |" for a in manifest["assets"]]
    lines += ["", "| statistic | value |", "| --- | --- |"]
    for key, value in manifest["coverage"].items():
        shown = (f"{len(value)} ({', '.join(value[:10])}{', ...' if len(value) > 10 else ''})"
                 if isinstance(value, list) and key == "missing_days" else
                 ", ".join(value) if isinstance(value, list) else value)
        lines.append(f"| {key} | {shown} |")
    return lines


def notice(tag: str, status: str, manifest: dict | None) -> None:
    fields = {"status": status}
    if manifest is not None:
        coverage = {key: (len(value) if key == "missing_days" else value) for key, value in manifest["coverage"].items()}
        fields.update(bytes={a["name"]: a["bytes"] for a in manifest["assets"]}, coverage=coverage)
    text = json.dumps(fields, sort_keys=True, separators=(",", ":"))[:3000]
    print(f"::notice title=daily {tag}::" + text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"),
          flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--first-month", default=dk.DEFAULT_FIRST)
    parser.add_argument("--last-month", required=True, help="finished UTC month, YYYY-MM")
    parser.add_argument("--revision", type=int, default=1)
    parser.add_argument("--workdir", default="daily-build-work")
    parser.add_argument("--publish", action="store_true", help="create the release (otherwise a dry run)")
    parser.add_argument("--summary", help="append a markdown summary to this file")
    args = parser.parse_args(argv)
    try:
        symbol = lake.validate_symbol(args.symbol)
        tag = dk.release_tag(symbol, args.first_month, args.last_month, args.revision)
    except ValueError as error:
        parser.error(str(error))
    if args.first_month < dk.FIRST_ARCHIVE_MONTH or not lake.month_finished(args.last_month, int(time.time() * 1000)):
        parser.error(f"months must start at {dk.FIRST_ARCHIVE_MONTH} or later and end with a finished UTC month")

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
    manifest, uploads = build(symbol, args.first_month, args.last_month, tag, Path(args.workdir))
    if manifest is None:
        print(f"{tag}: no daily kline file exists in this range; nothing built or published")
        write_summary(args.summary, summary_lines(tag, "empty (no daily files)", None))
        notice(tag, "empty", None)
        return 0
    too_large = [upload["name"] for upload in uploads if upload["bytes"] >= lake.MAX_ASSET_BYTES]
    if too_large:
        raise RuntimeError(f"assets at or above the 2 GiB release limit: {too_large}")
    print(f"built {tag} in {time.monotonic() - started:.1f}s", flush=True)
    if repo is None:
        write_summary(args.summary, summary_lines(tag, "dry run (not published)", manifest))
        notice(tag, "dry run", manifest)
        return 0
    published = publish_release(repo, tag, symbol, args.first_month, args.last_month, uploads)
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
