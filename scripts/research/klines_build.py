#!/usr/bin/env python3
"""Kline lake builder (rk-, owner decision 2026-10-15): 1-minute bars from Binance 1m klines before 2024-01.

For one (SYMBOL, finished MONTH) in 2020-12..2023-12 (2020-12 is warm-up) it downloads the four
Binance USD-M monthly 1m kline archives (klines, markPriceKlines, indexPriceKlines,
premiumIndexKlines), verifies each against its .CHECKSUM (a mismatch stops the run), streams them
without extracting and writes bars1m__SYMBOL__MONTH.csv.gz in the bars1m-v1 schema
(``data_lake.write_rk_bars_csv_gz``: every minute flagged KLINE_SOURCE, missing and no-trade
minutes marked with empty prices, never filled) plus manifest.json. aggTrades are NOT used before
2024-01; funding for these months comes from the dk1 history. With --publish it creates the
release ``rk-SYMBOL-MONTH-r1`` in the private research-data repository as a draft, uploads and
verifies every asset (manifest last) and only then publishes it, never marked Latest.

--parity builds 2024-01 from the klines the same way (never published) and compares it with the
published rd-SYMBOL-2024-01 bars (aggTrades) into the job summary: minutes differing in OHLC,
volume, taker volume and trades, priced-minute mismatches, the largest relative OHLC differences
and the month volume totals. It quantifies the source difference at the rk/rd boundary.

Standard library only. Published releases are never edited or deleted. Logs carry names, sizes,
hashes and aggregate statistics only (the code repo is public).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data_lake_build import (ResearchDataRepo, annotate_failure, download_source, file_sha256,  # noqa: E402
                             write_summary)
from market_analysis import data_lake as lake  # noqa: E402

PARITY_MONTH = lake.FIRST_MONTH  # 2024-01: the first rd month, built from klines for comparison only


def build(symbol: str, month: str, workdir: Path, sources: dict) -> tuple[dict, dict]:
    """(bars output, stats) of one month from the downloaded kline zips (streamed, never extracted)."""
    indexes, stats = {}, {"klines": {}}
    for family in lake.KLINE_FAMILIES:
        indexes[family] = lake.index_klines(
            lake.read_zip_csv(sources[family]["path"], lake.COLUMN_COUNTS[family], name=sources[family]["file"]),
            month, family)
        stats["klines"][family] = indexes[family].stats()
    path = workdir / lake.bars_asset_name(symbol, month)
    with path.open("wb") as stream:
        written = lake.write_rk_bars_csv_gz(stream, month, klines=indexes["klines"], mark=indexes["markPriceKlines"],
                                            index=indexes["indexPriceKlines"], premium=indexes["premiumIndexKlines"])
    stats["flag_counts"] = written["flag_counts"]
    stats["month_total_volume"] = stats["klines"]["klines"]["total_volume"]
    sha, size = file_sha256(path)
    output = {"name": path.name, "path": path, "sha256": sha, "bytes": size, "rows": written["rows"],
              "content": f"1-minute bars from 1m klines ({lake.LAKE_SCHEMA_VERSION} columns, bar source kline)"}
    return output, stats


def manifest_for(symbol: str, month: str, tag: str, sources: dict, output: dict, stats: dict) -> dict:
    assets = [{"name": source["asset"], "sha256": source["sha256"], "bytes": source["bytes"],
               "rows": stats["klines"][family]["raw_rows"], "content": f"raw Binance {family} archive"}
              for family, source in sources.items()]
    assets.append({key: output[key] for key in ("name", "sha256", "bytes", "rows", "content")})
    return {
        "manifest_version": lake.RK_MANIFEST_VERSION,
        "schema_version": lake.LAKE_SCHEMA_VERSION,
        "release_tag": tag,
        "symbol": symbol,
        "month": month,
        "bar_source": "kline",
        "funding": "not included: the dk1 funding history covers these months",
        "producer_commit": os.environ.get("GITHUB_SHA") or None,
        "build_time_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "sources": {family: {key: source[key] for key in ("url", "file", "asset", "binance_checksum_sha256",
                                                          "sha256", "bytes")}
                    for family, source in sources.items()},
        "assets": assets,
        "bars_columns": list(lake.COLUMNS),
        "flag_bits": lake.RK_FLAG_BITS,
        "stats": stats,
        "supersedes": None,
    }


def publish_release(repo: ResearchDataRepo, tag: str, symbol: str, month: str, uploads: list[dict]) -> dict:
    body = (f"Kline lake {symbol} {month} ({lake.LAKE_SCHEMA_VERSION} columns). Built by GitHub Actions from the "
            "Binance public archive (data.binance.vision): raw monthly 1m kline/mark/index/premium zips with "
            "checksums and 1-minute bars from the klines (aggTrades are not used before 2024-01; every minute is "
            "flagged KLINE_SOURCE, missing and no-trade minutes are marked, never filled). See manifest.json.")
    revision = tag.rsplit("-r", 1)[1]
    release = repo.create_draft(tag, f"{symbol} · {month} · 1-minute bars from klines (r{revision})", body)
    for upload in uploads:  # manifest.json is last
        repo.upload_verified(release, upload["path"], upload["name"], upload["sha256"], upload["bytes"])
        print(f"uploaded and verified {upload['name']}", flush=True)
    return repo.publish(release, tag)  # never marked Latest


def download_rd_bars(repo: ResearchDataRepo, symbol: str, month: str, revision: int, dest: Path) -> Path:
    """The published rd bars of one month, sha256-verified (asset digest, else the release manifest)."""
    from label_build import download  # private-repo asset download with verification

    tag = lake.release_tag(symbol, month, revision)
    release = repo.published_release(tag)
    if release is None:
        raise RuntimeError(f"missing published release: {tag}")
    assets = repo.assets(release["id"])
    name = lake.bars_asset_name(symbol, month)
    if name not in assets:
        raise RuntimeError(f"{tag}: missing asset {name}")
    expected = None
    if not assets[name].get("digest"):
        manifest_path = dest / f"{tag}.manifest.json"
        download(repo, assets["manifest.json"], manifest_path, limit=16 << 20)
        manifest = json.loads(manifest_path.read_bytes())
        if manifest.get("release_tag") != tag:
            raise RuntimeError(f"{tag}: manifest identity mismatch")
        expected = next(item["sha256"] for item in manifest["assets"] if item["name"] == name)
    path = dest / f"rd__{name}"
    download(repo, assets[name], path, expected)
    return path


def parity_lines(symbol: str, result: dict) -> list[str]:
    lines = [f"### Kline vs aggTrades parity {symbol} {PARITY_MONTH}", "", "| statistic | value |", "| --- | --- |"]
    for key, value in result.items():
        shown = ", ".join(f"{k} {v}" for k, v in value.items()) if isinstance(value, dict) else value
        lines.append(f"| {key} | {shown} |")
    return lines


def summary_lines(tag: str, status: str, manifest: dict | None) -> list[str]:
    lines = [f"### Kline lake `{tag}`: {status}", ""]
    if manifest is None:
        return lines
    lines += ["| asset | bytes | rows | sha256 |", "| --- | ---: | ---: | --- |"]
    lines += [f"| `{a['name']}` | {a['bytes']} | {a['rows']} | `{a['sha256']}` |" for a in manifest["assets"]]
    klines = manifest["stats"]["klines"]
    lines += ["", "| family | rows | missing minutes | duplicates | conflicts |", "| --- | ---: | ---: | ---: | ---: |"]
    lines += [f"| {name} | {v['rows']} | {v['missing_minutes']} | {v['duplicate_rows']} | {v['duplicate_conflicts']} |"
              for name, v in klines.items()]
    lines += ["", "| flag | minutes |", "| --- | ---: |"]
    lines += [f"| {name} | {count} |" for name, count in manifest["stats"]["flag_counts"].items()]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--month", help="finished UTC month in 2020-12..2023-12 (not with --parity)")
    parser.add_argument("--workdir", default="klines-build-work")
    parser.add_argument("--publish", action="store_true", help="create the release (otherwise a dry run)")
    parser.add_argument("--parity", action="store_true", help=f"compare {PARITY_MONTH} from klines with the rd bars")
    parser.add_argument("--data-revision", type=int, default=1, help="rd revision for --parity")
    parser.add_argument("--summary", help="append a markdown summary to this file")
    args = parser.parse_args(argv)
    try:
        symbol = lake.validate_symbol(args.symbol)
    except ValueError as error:
        parser.error(str(error))
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    if args.parity:
        if args.publish or args.month:
            parser.error("--parity builds 2024-01 for comparison only: no --month, never --publish")
        repo = ResearchDataRepo(os.environ.get("RESEARCH_DATA_REPOSITORY"), os.environ.get("RESEARCH_DATA_TOKEN"))
        sources = {family: download_source(family, symbol, PARITY_MONTH, workdir) for family in lake.KLINE_FAMILIES}
        output, _ = build(symbol, PARITY_MONTH, workdir, sources)
        rd_path = download_rd_bars(repo, symbol, PARITY_MONTH, args.data_revision, workdir)
        with output["path"].open("rb") as kline_bars, rd_path.open("rb") as rd_bars:
            result = lake.compare_bars(kline_bars, rd_bars)
        lines = parity_lines(symbol, result)
        print("\n".join(lines), flush=True)
        write_summary(args.summary, lines)
        return 0

    try:
        month = lake.validate_month(args.month or "")
        tag = lake.rk_release_tag(symbol, month)
    except ValueError as error:
        parser.error(str(error))
    if not lake.month_finished(month, int(time.time() * 1000)):
        parser.error(f"{month} is not a fully finished UTC month")
    repo = None
    if args.publish:
        repo = ResearchDataRepo(os.environ.get("RESEARCH_DATA_REPOSITORY"), os.environ.get("RESEARCH_DATA_TOKEN"))
        if repo.published_release(tag) is not None:
            print(f"{tag}: exists (published releases are immutable); nothing to do")
            write_summary(args.summary, summary_lines(tag, "exists", None))
            return 0
        for draft in repo.draft_releases(tag):
            repo.delete_draft(draft)
            print(f"{tag}: deleted a draft left by an interrupted run")

    started = time.monotonic()
    sources = {family: download_source(family, symbol, month, workdir) for family in lake.KLINE_FAMILIES}
    output, stats = build(symbol, month, workdir, sources)
    manifest = manifest_for(symbol, month, tag, sources, output, stats)
    manifest_path = workdir / "manifest.json"
    manifest_path.write_text(lake.canonical_json(manifest), encoding="ascii")
    uploads = [{"name": source["asset"], "path": source["path"], "sha256": source["sha256"], "bytes": source["bytes"]}
               for source in sources.values()]
    uploads.append({key: output[key] for key in ("name", "path", "sha256", "bytes")})
    manifest_sha, manifest_size = file_sha256(manifest_path)
    uploads.append({"name": "manifest.json", "path": manifest_path, "sha256": manifest_sha, "bytes": manifest_size})
    too_large = [upload["name"] for upload in uploads if upload["bytes"] >= lake.MAX_ASSET_BYTES]
    if too_large:
        raise RuntimeError(f"assets at or above the 2 GiB release limit: {too_large}")
    print(f"built {tag} in {time.monotonic() - started:.1f}s", flush=True)
    if repo is None:
        write_summary(args.summary, summary_lines(tag, "dry run (not published)", manifest))
        return 0
    published = publish_release(repo, tag, symbol, month, uploads)
    print(f"{tag}: published release {published['id']}")
    write_summary(args.summary, summary_lines(tag, "published", manifest))
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as error:  # noqa: BLE001 - re-raised after annotating
        annotate_failure(error)
        raise
    sys.exit(code)
