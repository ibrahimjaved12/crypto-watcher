#!/usr/bin/env python3
"""Build immutable labels-v1 releases from verified private data-lake assets.

Run as a script so data_lake_build is importable beside this module. Credentials
are required even for dry runs: both modes read private published rd releases.
Public output contains only names, sizes, hashes and aggregate statistics; the
full manifest (including inferred ticks and parameters) stays in the private
release. Dry runs print its public metadata projection and publish nothing.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time

from data_lake_build import ResearchDataRepo, GitHubError, file_sha256
from market_analysis import data_lake as lake
from market_analysis.benchmark import label_cli
from market_analysis.benchmark.canonical import exact_to_str

BLOCK = 1 << 20
_LABEL_TAG = re.compile(
    r"lb1-(" + "|".join(lake.SYMBOLS) + r")-([0-9]{4}-[0-9]{2})_([0-9]{4}-[0-9]{2})-r([1-9][0-9]{0,2})\Z")


def validate_symbols(raw: str) -> list[str]:
    if not re.fullmatch(r"[A-Z]+(,[A-Z]+)*", raw):
        raise ValueError("symbols must be a comma-separated list without spaces")
    symbols = raw.split(",")
    if len(set(symbols)) != len(symbols) or any(symbol not in lake.SYMBOLS for symbol in symbols):
        raise ValueError(f"symbols must be distinct members of {','.join(lake.SYMBOLS)}")
    return symbols


def revision(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]{0,2}", value):
        raise ValueError("revision must be an integer from 1 to 999 without leading zeros")
    return int(value)


def plan_months(first: str, last: str, *, now_ms: int | None = None) -> list[str]:
    months = lake.months_between(first, last)
    if first < lake.FIRST_MONTH:
        raise ValueError("first_month must be >= 2024-01 and <= last_month")
    if now_ms is not None and not lake.month_finished(last, now_ms):
        raise ValueError(f"{last} is not a fully finished UTC month")
    return months


def validate_label_tag(tag: str) -> str:
    match = _LABEL_TAG.fullmatch(tag)
    if match is None:
        raise ValueError("invalid labels-v1 release tag")
    _, first, last, _ = match.groups()
    plan_months(first, last)
    return tag


def label_tag(symbol: str, first: str, last: str, label_revision: int = 1) -> str:
    lake.validate_symbol(symbol)
    return validate_label_tag(f"lb1-{symbol}-{first}_{last}-r{label_revision}")


def parse_args(argv=None, *, now_ms: int | None = None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--first-month", default="2024-01")
    parser.add_argument("--last-month", default="2026-09")
    parser.add_argument("--data-revision", default="1")
    parser.add_argument("--label-revision", default="1")
    parser.add_argument("--workdir", type=Path, default=Path("label-build-work"))
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args(argv)
    try:
        lake.validate_symbol(args.symbol)
        args.data_revision = revision(args.data_revision)
        args.label_revision = revision(args.label_revision)
        args.months = plan_months(args.first_month, args.last_month, now_ms=now_ms)
    except ValueError as error:
        parser.error(str(error))
    return args


def published_inputs(repo: ResearchDataRepo, symbol: str, months: list[str], data_revision: int) -> dict:
    """Preflight every tag before downloading anything; report all missing tags."""
    releases, missing = {}, []
    for month in months:
        tag = lake.release_tag(symbol, month, data_revision)
        release = repo.published_release(tag)
        if release is None:
            missing.append(tag)
        else:
            if release.get("tag_name") != tag or release.get("draft") is not False:
                raise GitHubError(f"published release identity mismatch: {tag}")
            releases[month] = (tag, release)
    if missing:
        raise GitHubError("missing published data releases: " + ", ".join(missing))
    return releases


def delete_label_drafts(repo: ResearchDataRepo, tag: str) -> None:
    """The shared client's delete_draft intentionally accepts rd tags only."""
    validate_label_tag(tag)
    for draft in repo.draft_releases(tag):
        url = f"{repo.prefix}/releases/{int(draft['id'])}"
        current = repo.json("GET", url)
        if current.get("draft") is not True or current.get("tag_name") != tag:
            raise GitHubError("refusing to delete a release that is not the requested lb1 draft")
        with repo.request("DELETE", url):
            pass
        print(f"{tag}: deleted leftover draft", flush=True)


def download(repo: ResearchDataRepo, asset: dict, path: Path, expected: str | None = None,
             *, limit: int = lake.MAX_ASSET_BYTES - 1) -> tuple[str, int]:
    """Stream a private API asset and remove partial or unverified downloads."""
    try:
        size = asset.get("size")
        if asset.get("state") != "uploaded" or type(size) is not int or not 0 <= size <= limit:
            raise GitHubError(f"asset {path.name} has invalid state/size")
        api_digest = asset.get("digest")
        if api_digest:
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", api_digest):
                raise GitHubError(f"asset {path.name} has unsupported API digest")
            expected = api_digest[7:]
        if expected is not None and not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise GitHubError(f"asset {path.name} has invalid expected sha256")
        digest, count = hashlib.sha256(), 0
        with repo.request("GET", f"{repo.prefix}/releases/assets/{int(asset['id'])}",
                          accept="application/octet-stream", timeout=120) as response, path.open("wb") as stream:
            for block in iter(lambda: response.read(BLOCK), b""):
                count += len(block)
                if count > size:
                    raise GitHubError(f"asset {path.name} exceeds declared size")
                digest.update(block)
                stream.write(block)
        actual = digest.hexdigest()
        if count != size or (expected is not None and actual != expected):
            raise GitHubError(f"asset {path.name} sha256/size mismatch")
        # Without an API digest, the manifest is the trusted checksum source
        # fetched from this authenticated release. Read-back verifies that
        # metadata too; data files always supply its expected sha256 below.
        repo.verify_asset(asset, actual, count)
        print(f"downloaded {path.name}: {count} bytes sha256 {actual}", flush=True)
        return actual, count
    except Exception:
        path.unlink(missing_ok=True)
        raise


def download_inputs(repo: ResearchDataRepo, symbol: str, releases: dict, workdir: Path) -> dict:
    provenance = {}
    for month, (tag, release) in releases.items():
        assets = repo.assets(release["id"])
        names = {"bars": lake.bars_asset_name(symbol, month), "funding": lake.funding_asset_name(symbol, month)}
        missing = [name for name in names.values() if name not in assets]
        if missing:
            raise GitHubError(f"{tag}: missing assets {', '.join(missing)}")
        checksums = {}
        if any(not assets[name].get("digest") for name in names.values()):
            if "manifest.json" not in assets:
                raise GitHubError(f"{tag}: missing manifest.json for checksum fallback")
            manifest_path = workdir / f"{tag}.manifest.json"
            download(repo, assets["manifest.json"], manifest_path, limit=16 * BLOCK)
            manifest = json.loads(manifest_path.read_bytes())
            if (manifest.get("release_tag") != tag or manifest.get("symbol") != symbol
                    or manifest.get("month") != month):
                raise GitHubError(f"{tag}: manifest identity mismatch")
            for item in manifest["assets"]:
                if item["name"] in checksums:
                    raise GitHubError(f"{tag}: duplicate manifest asset name")
                checksums[item["name"]] = item["sha256"]
        provenance[month] = {"rd_tag": tag}
        for kind, name in names.items():
            expected = checksums.get(name)
            if not assets[name].get("digest") and expected is None:
                raise GitHubError(f"{tag}: no sha256 for {name}")
            sha, size = download(repo, assets[name], workdir / name, expected)
            provenance[month][kind] = {"name": name, "sha256": sha, "bytes": size}
    return provenance


def build(symbol: str, first: str, last: str, workdir: Path, provenance: dict) -> dict:
    out_dir = workdir / "labels"
    # label_cli logs no tick values, but parser exceptions can carry individual input values;
    # keep all of its output out of the public workflow log.
    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        manifest = label_cli.run(symbol, workdir, first, last, out_dir)
    for month, inputs in manifest["inputs"].items():
        verified = provenance[month]
        for kind in ("bars", "funding"):
            if inputs[kind]["sha256"] != verified[kind]["sha256"]:
                raise GitHubError("label input changed after download verification")
            inputs[kind]["bytes"] = verified[kind]["bytes"]
        inputs["rd_tag"] = verified["rd_tag"]
    manifest["rd_tags"] = [inputs["rd_tag"] for inputs in provenance.values()]
    (out_dir / label_cli.manifest_name(symbol)).write_text(lake.canonical_json(manifest), encoding="ascii")
    return manifest


def public_manifest(manifest: dict) -> dict:
    """Explicit public allowlist: no ticks, parameters or individual data rows."""
    return {name: manifest[name] for name in (
        "schema", "symbol", "first_month", "last_month", "params_identity", "cost_model_identity",
        "inputs", "rd_tags", "outputs")}


def aggregate_counts(manifest: dict, seconds: int) -> dict:
    statuses, outcomes = Counter(), [Counter() for _ in manifest["params"]["rr_grid"]]
    rows = 0
    for output in manifest["outputs"]:
        rows += output["rows"]
        statuses.update(output["status_counts"])
        for counter, counts in zip(outcomes, output["outcome_counts_pessimistic"]):
            counter.update(counts)
    shares = []
    for counts in outcomes:
        total = sum(counts.values())
        shares.append({name: exact_to_str(Fraction(count, total)) for name, count in sorted(counts.items())}
                      if total else {})
    # Aggregate tick checks only: tick values themselves stay in the private manifest.
    return {"rows": rows, "status_counts": dict(sorted(statuses.items())),
            "outcome_shares_pessimistic_by_target": shares, "seconds": seconds,
            "tick_rule": manifest.get("tick_rule", "tick-v1"), **label_cli.tick_summary(manifest)}


def report(tag: str, status: str, summary: Path | None, counts: dict) -> None:
    payload = json.dumps(counts, sort_keys=True, separators=(",", ":"))
    print(f"::notice title=labels {tag}::{status} {payload}", flush=True)
    lines = [f"### Labels `{tag}`: {status}", "", "```json", payload, "```", ""]
    print("\n".join(lines), flush=True)
    if summary:
        with summary.open("a", encoding="utf-8") as stream:
            stream.write("\n".join(lines))


def release_body(manifest: dict) -> str:
    body = (f"Trade labels schema: {manifest['schema']}.\n\n"
            f"Params identity: `{manifest['params_identity']}`.\n\n"
            f"Cost model identity: `{manifest['cost_model_identity']}`.\n\n")
    if manifest.get("tick_rule") == "tick-v2":
        body += "Tick rule: tick-v2 (outlier-tolerant)\n\n"
    return (body + "Input data releases:\n" + "\n".join(f"- `{tag}`" for tag in manifest["rd_tags"]) +
            f"\n\nSee {label_cli.manifest_name(manifest['symbol'])} for input sha256 hashes and aggregate counts.")


def publish_release(repo: ResearchDataRepo, tag: str, manifest: dict, out_dir: Path) -> str:
    # Recheck before any creation: published releases are immutable.
    if repo.published_release(tag) is not None:
        print(f"{tag}: exists; no release created", flush=True)
        return "exists (immutable)"
    delete_label_drafts(repo, tag)
    uploads = []
    for name in [output["name"] for output in manifest["outputs"]] + [label_cli.manifest_name(manifest["symbol"])]:
        path = out_dir / name
        sha, size = file_sha256(path)
        if size >= lake.MAX_ASSET_BYTES:
            raise GitHubError(f"asset {name} is at or above the 2 GiB release limit")
        uploads.append((path, name, sha, size))
    revision_text = tag.rsplit("-r", 1)[1]
    title = (f"{manifest['symbol']} · {manifest['first_month']} → {manifest['last_month']} · "
             f"trade labels ({manifest['schema']}, r{revision_text})")
    body = release_body(manifest)
    release = repo.create_draft(tag, title, body)
    for path, name, sha, size in uploads:  # manifest last
        repo.upload_verified(release, path, name, sha, size)
        print(f"uploaded {name}: {size} bytes sha256 {sha}", flush=True)
    repo.publish(release, tag)
    return "published"


def main(argv=None) -> int:
    args = parse_args(argv, now_ms=time.time_ns() // 1_000_000)
    tag = label_tag(args.symbol, args.first_month, args.last_month, args.label_revision)
    repo = ResearchDataRepo(os.environ.get("RESEARCH_DATA_REPOSITORY"), os.environ.get("RESEARCH_DATA_TOKEN"))
    if args.publish and repo.published_release(tag) is not None:
        report(tag, "exists (immutable)", args.summary, {})
        return 0
    releases = published_inputs(repo, args.symbol, args.months, args.data_revision)
    if args.publish:
        delete_label_drafts(repo, tag)
    # Never remove a pre-existing caller directory; own only this newly created one.
    args.workdir.mkdir(parents=True, exist_ok=False)
    started = time.monotonic_ns()
    try:
        provenance = download_inputs(repo, args.symbol, releases, args.workdir)
        manifest = build(args.symbol, args.first_month, args.last_month, args.workdir, provenance)
        status = "dry run (not published)"
        if args.publish:
            status = publish_release(repo, tag, manifest, args.workdir / "labels")
        else:
            print(lake.canonical_json(public_manifest(manifest)), end="", flush=True)
        counts = aggregate_counts(manifest, (time.monotonic_ns() - started) // 1_000_000_000)
        report(tag, status, args.summary, counts)
    finally:
        shutil.rmtree(args.workdir)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GitHubError as error:
        # Only the complete missing-tag list is approved for public error text.
        # Other errors (including library errors) may contain private values.
        message = str(error) if str(error).startswith("missing published data releases: ") else type(error).__name__
        print(f"::error title=label build failed::{message}", flush=True)
        sys.exit(1)
    except Exception as error:
        print(f"::error title=label build failed::{type(error).__name__}", flush=True)
        sys.exit(1)
