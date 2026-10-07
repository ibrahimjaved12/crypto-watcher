#!/usr/bin/env python3
"""Research data lake builder (#183): one immutable release per symbol-month.

For one (SYMBOL, finished MONTH) it downloads the six Binance USD-M monthly
archives (aggTrades, klines, mark/index/premium klines, funding), verifies each
against its .CHECKSUM, streams them WITHOUT extracting, builds exact 1-minute
bars from aggTrades (kline values only as flagged cross-checks) plus a funding
table, and writes manifest.json. With --publish it creates the release
``rd-SYMBOL-MONTH-r1`` in the private research-data repository as a draft,
uploads and verifies every asset (manifest last) and only then publishes it.

Standard library only. Published releases are never edited or deleted; a run
whose release already exists reports "exists" and does nothing. Logs contain
only names, sizes, hashes and aggregate statistics (the code repo is public).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))
from market_analysis import data_lake as lake  # noqa: E402

BLOCK = 1 << 20
USER_AGENT = "crypto-watcher-data-lake-builder"
CONTENT_TYPES = {".zip": "application/zip", ".gz": "application/gzip", ".json": "application/json"}


# ---------------------------------------------------------------- Binance downloads


def fetch(url: str, destination: Path | None, attempts: int = 4) -> tuple[bytes | None, str, int]:
    """Download url (to destination, or into memory); return (body, sha256, bytes).

    Network errors and HTTP 5xx/429 are retried; any other HTTP error, or failure
    after the last attempt, is a hard failure.
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=120) as response:
                digest, size, chunks = hashlib.sha256(), 0, []
                stream = destination.open("wb") if destination else None
                try:
                    while True:
                        block = response.read(BLOCK)
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
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f"download failed: HTTP {error.code}: {url}") from None
            last = error
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
            last = error
        if destination:
            destination.unlink(missing_ok=True)
        if attempt < attempts:
            time.sleep(3 * attempt)
    raise RuntimeError(f"download failed after {attempts} attempts: {url}: {type(last).__name__}")


def download_source(family: str, symbol: str, month: str, workdir: Path) -> dict:
    url = lake.source_url(family, symbol, month)
    file_name = url.rsplit("/", 1)[1]
    body, _, _ = fetch(url + ".CHECKSUM", None)
    expected = lake.parse_checksum(body.decode("ascii", "replace"))
    asset = lake.raw_asset_name(family, file_name)
    path = workdir / asset
    started = time.monotonic()
    _, actual, size = fetch(url, path)
    if actual != expected:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"CHECKSUM MISMATCH for {url}: Binance {expected}, downloaded {actual}")
    print(f"downloaded {asset}: {size} bytes in {time.monotonic() - started:.1f}s, checksum verified", flush=True)
    return {"family": family, "url": url, "file": file_name, "asset": asset, "path": path,
            "binance_checksum_sha256": expected, "sha256": actual, "bytes": size}


def file_sha256(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(BLOCK), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


# ---------------------------------------------------------------- build


def build(symbol: str, month: str, workdir: Path, sources: dict) -> tuple[list[dict], dict]:
    """Bars and funding outputs plus statistics; every zip is streamed, never extracted."""
    stats: dict = {"klines": {}}
    indexes = {}
    for family in lake.KLINE_FAMILIES:
        started = time.monotonic()
        indexes[family] = lake.index_klines(
            lake.read_zip_csv(sources[family]["path"], lake.COLUMN_COUNTS[family], name=sources[family]["file"]),
            month, family)
        stats["klines"][family] = indexes[family].stats()
        print(f"indexed {family}: {indexes[family].stats()['rows']} rows in {time.monotonic() - started:.1f}s",
              flush=True)

    started = time.monotonic()
    bars = lake.MinuteBars(month)
    bars.consume(lake.read_zip_csv(sources["aggTrades"]["path"], lake.COLUMN_COUNTS["aggTrades"],
                                   name=sources["aggTrades"]["file"]))
    stats["aggTrades"] = bars.stats()
    print(f"aggregated {bars.rows} aggTrades rows in {time.monotonic() - started:.1f}s", flush=True)

    bars_path = workdir / lake.bars_asset_name(symbol, month)
    with bars_path.open("wb") as stream:
        written = lake.write_bars_csv_gz(stream, bars, klines=indexes["klines"], mark=indexes["markPriceKlines"],
                                         index=indexes["indexPriceKlines"], premium=indexes["premiumIndexKlines"])
    stats["flag_counts"] = written["flag_counts"]
    del bars, indexes

    funding_path = workdir / lake.funding_asset_name(symbol, month)
    with funding_path.open("wb") as stream:
        stats["fundingRate"] = lake.write_funding_csv_gz(
            stream, lake.read_zip_csv(sources["fundingRate"]["path"], lake.COLUMN_COUNTS["fundingRate"],
                                      name=sources["fundingRate"]["file"]), month)
    stats["month_total_volume"] = {"aggTrades": stats["aggTrades"]["total_volume"],
                                   "klines": stats["klines"]["klines"]["total_volume"]}
    stats["month_total_taker_buy_volume"] = {"aggTrades": stats["aggTrades"]["total_taker_buy_volume"],
                                             "klines": stats["klines"]["klines"]["total_taker_buy_volume"]}

    outputs = []
    for path, rows, content in ((bars_path, written["rows"], f"1-minute bars ({lake.LAKE_SCHEMA_VERSION})"),
                                (funding_path, stats["fundingRate"]["rows"], "funding rate rows")):
        sha, size = file_sha256(path)
        outputs.append({"name": path.name, "path": path, "sha256": sha, "bytes": size, "rows": rows,
                        "content": content})
    return outputs, stats


def raw_rows(family: str, stats: dict) -> int:
    if family == "aggTrades":
        return stats["aggTrades"]["rows"]
    if family == "fundingRate":
        return stats["fundingRate"]["raw_rows"]
    return stats["klines"][family]["raw_rows"]


def manifest_for(symbol: str, month: str, tag: str, sources: dict, outputs: list[dict], stats: dict) -> dict:
    assets = [{"name": source["asset"], "sha256": source["sha256"], "bytes": source["bytes"],
               "rows": raw_rows(family, stats), "content": f"raw Binance {family} archive"}
              for family, source in sources.items()]
    assets += [{key: output[key] for key in ("name", "sha256", "bytes", "rows", "content")} for output in outputs]
    return {
        "manifest_version": lake.MANIFEST_VERSION,
        "schema_version": lake.LAKE_SCHEMA_VERSION,
        "release_tag": tag,
        "symbol": symbol,
        "month": month,
        "producer_commit": os.environ.get("GITHUB_SHA") or None,
        "build_time_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "sources": {family: {key: source[key] for key in ("url", "file", "asset", "binance_checksum_sha256",
                                                          "sha256", "bytes")}
                    for family, source in sources.items()},
        "assets": assets,
        "bars_columns": list(lake.COLUMNS),
        "funding_columns": list(lake.FUNDING_COLUMNS),
        "flag_bits": lake.FLAG_BITS,
        "stats": stats,
        "supersedes": None,
    }


# ---------------------------------------------------------------- GitHub (private research-data repo)


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, ambiguous: bool = False):
        super().__init__(message)
        self.status, self.ambiguous = status, ambiguous


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # redirects are followed manually, never with credentials to another host


class ResearchDataRepo:
    """Minimal release client mirroring the safety rules of github_study.GitHub.

    Credentials go only to api.github.com / uploads.github.com, redirects are
    followed manually without them, reads are retried, mutations are not.
    """

    API = "https://api.github.com"
    AUTH_HOSTS = ("api.github.com", "uploads.github.com")

    def __init__(self, repository: str | None, token: str | None):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository or ""):
            raise ValueError("configure RESEARCH_DATA_REPOSITORY as owner/private-repo")
        if repository.lower() == os.environ.get("GITHUB_REPOSITORY", "").lower():
            raise ValueError("research data must go to the separate private research-data repository")
        if not token:
            raise ValueError("configure the RESEARCH_DATA_TOKEN secret")
        self._token = token
        self.opener = urllib.request.build_opener(NoRedirect())
        self.prefix = f"{self.API}/repos/{repository}"
        details = self.json("GET", self.prefix)
        if details.get("private") is not True:
            raise ValueError("research-data repository must be private")
        self.default_branch = details["default_branch"]

    def request(self, method: str, url: str, *, data=None, size: int | None = None,
                content_type: str | None = None, accept: str = "application/vnd.github+json", timeout: int = 30):
        original = urllib.parse.urlparse(url).hostname
        if original not in self.AUTH_HOSTS:
            raise ValueError("requests start at the GitHub API only")
        for attempt in range(3 if method == "GET" else 1):
            current = url
            try:
                for _ in range(5):
                    parsed = urllib.parse.urlparse(current)
                    host = parsed.hostname or ""
                    if (parsed.scheme != "https" or parsed.username or parsed.password
                            or not (host in self.AUTH_HOSTS or host == "github.com"
                                    or host.endswith(".githubusercontent.com"))):
                        raise GitHubError("untrusted redirect host")
                    headers = {"Accept": accept, "User-Agent": USER_AGENT, "X-GitHub-Api-Version": "2022-11-28"}
                    if host == original and host in self.AUTH_HOSTS:
                        headers["Authorization"] = "Bearer " + self._token
                    if content_type:
                        headers["Content-Type"] = content_type
                    if size is not None:
                        headers["Content-Length"] = str(size)
                    request = urllib.request.Request(current, data=data, headers=headers, method=method)
                    try:
                        return self.opener.open(request, timeout=timeout)
                    except urllib.error.HTTPError as error:
                        code = error.code
                        location = error.headers.get("Location", "")
                        error.close()
                        if code in (301, 302, 303, 307, 308) and method == "GET" and location:
                            current = urllib.parse.urljoin(current, location)
                            continue
                        if method == "GET" and code in (429, 500, 502, 503, 504) and attempt < 2:
                            break  # retry the read
                        raise GitHubError(f"GitHub {method} {urllib.parse.urlparse(url).path} -> HTTP {code}",
                                          status=code) from None
                else:
                    raise GitHubError("too many redirects")
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                # HTTP errors were handled above; this is a transport failure.
                if method != "GET":
                    raise GitHubError(f"ambiguous GitHub {method} {urllib.parse.urlparse(url).path}",
                                      ambiguous=True) from None
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
        raise GitHubError(f"GitHub {method} {urllib.parse.urlparse(url).path} failed after retries")

    def json(self, method: str, url: str, value=None, timeout: int = 30):
        data = json.dumps(value).encode() if value is not None else None
        with self.request(method, url, data=data, size=len(data) if data else None,
                          content_type="application/json" if data else None, timeout=timeout) as response:
            payload = response.read(16 * BLOCK + 1)
        if len(payload) > 16 * BLOCK:
            raise GitHubError("GitHub metadata exceeds bounded size")
        return json.loads(payload) if payload else None

    # Releases -------------------------------------------------------------

    def published_release(self, tag: str):
        """The published release for tag, or None (tag lookups never return drafts)."""
        try:
            release = self.json("GET", f"{self.prefix}/releases/tags/{urllib.parse.quote(tag, safe='')}")
        except GitHubError as error:
            if error.status == 404:
                return None
            raise
        return None if release.get("draft") else release

    def draft_releases(self, tag: str) -> list[dict]:
        drafts = []
        for page in range(1, 101):
            rows = self.json("GET", f"{self.prefix}/releases?per_page=100&page={page}")
            drafts += [row for row in rows if row.get("draft") is True and row.get("tag_name") == tag]
            if len(rows) < 100:
                return drafts
        raise GitHubError("release inventory exceeds the bounded listing")

    def delete_draft(self, release: dict) -> None:
        lake.validate_tag(release["tag_name"])
        current = self.json("GET", f"{self.prefix}/releases/{int(release['id'])}")
        if current.get("draft") is not True or current.get("tag_name") != release["tag_name"]:
            raise GitHubError("refusing to delete a release that is not a crashed rd- draft")
        with self.request("DELETE", f"{self.prefix}/releases/{int(release['id'])}"):
            pass

    def create_draft(self, tag: str, body: str) -> dict:
        return self.json("POST", f"{self.prefix}/releases", {
            "tag_name": tag, "target_commitish": self.default_branch, "name": tag, "body": body,
            "draft": True, "prerelease": True, "make_latest": "false"})

    def assets(self, release_id: int) -> dict:
        found = {}
        for page in range(1, 11):
            rows = self.json("GET", f"{self.prefix}/releases/{release_id}/assets?per_page=100&page={page}")
            for row in rows:
                if row["name"] in found:
                    raise GitHubError("duplicate release asset name")
                found[row["name"]] = row
            if len(rows) < 100:
                return found
        raise GitHubError("asset inventory exceeds the bounded listing")

    def _downloaded_sha256(self, asset: dict) -> tuple[str, int]:
        digest, size = hashlib.sha256(), 0
        with self.request("GET", f"{self.prefix}/releases/assets/{int(asset['id'])}",
                          accept="application/octet-stream", timeout=120) as response:
            for block in iter(lambda: response.read(BLOCK), b""):
                digest.update(block)
                size += len(block)
        return digest.hexdigest(), size

    def verify_asset(self, asset: dict, sha256: str, size: int) -> None:
        if asset.get("state") != "uploaded" or asset.get("size") != size:
            raise GitHubError(f"asset {asset.get('name')} state/size mismatch")
        digest = asset.get("digest")
        if digest:
            if digest != "sha256:" + sha256:
                raise GitHubError(f"asset {asset.get('name')} API digest mismatch")
            return
        actual, actual_size = self._downloaded_sha256(asset)
        if actual != sha256 or actual_size != size:
            raise GitHubError(f"asset {asset.get('name')} read-back mismatch")

    def upload_verified(self, release: dict, path: Path, name: str, sha256: str, size: int) -> None:
        url = release["upload_url"].split("{", 1)[0] + "?name=" + urllib.parse.quote(name, safe="")
        content_type = CONTENT_TYPES.get(path.suffix, "application/octet-stream")
        for attempt in range(2):
            try:
                with path.open("rb") as stream, self.request("POST", url, data=stream, size=size,
                                                             content_type=content_type, timeout=600) as response:
                    asset = json.loads(response.read(16 * BLOCK))
            except GitHubError as error:
                if not error.ambiguous or attempt:
                    raise
                # The upload may have been stored before the connection failed: reconcile once.
                existing = self.assets(release["id"]).get(name)
                if existing is not None:
                    try:
                        self.verify_asset(existing, sha256, size)
                        return
                    except GitHubError:
                        with self.request("DELETE", f"{self.prefix}/releases/assets/{int(existing['id'])}"):
                            pass
                continue
            confirmed = self.json("GET", f"{self.prefix}/releases/assets/{int(asset['id'])}")
            self.verify_asset(confirmed, sha256, size)
            return

    def publish(self, release: dict, tag: str) -> dict:
        url = f"{self.prefix}/releases/{int(release['id'])}"
        try:
            result = self.json("PATCH", url, {"draft": False})
        except GitHubError as error:
            if not error.ambiguous:
                raise
            result = self.json("GET", url)  # the PATCH may have applied before the failure
        if result.get("draft") is not False or result.get("tag_name") != tag:
            raise GitHubError("release publication was not confirmed")
        confirmed = self.published_release(tag)
        if confirmed is None or confirmed.get("id") != release["id"]:
            raise GitHubError("published release is not visible under its tag")
        return confirmed


def publish_release(repo: ResearchDataRepo, tag: str, symbol: str, month: str, uploads: list[dict]) -> dict:
    body = (f"Research data lake {symbol} {month} ({lake.LAKE_SCHEMA_VERSION}). Built by GitHub Actions from the "
            "Binance public archive (data.binance.vision): raw monthly zips with checksums, exact 1-minute bars "
            "from aggTrades with flagged kline/mark/index/premium cross-checks, funding rows. See manifest.json.")
    release = repo.create_draft(tag, body)
    for upload in uploads:  # manifest.json is last
        repo.upload_verified(release, upload["path"], upload["name"], upload["sha256"], upload["bytes"])
        print(f"uploaded and verified {upload['name']}", flush=True)
    return repo.publish(release, tag)


# ---------------------------------------------------------------- summary and main


def write_summary(path: str | None, lines: list[str]) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")


def summary_lines(tag: str, status: str, manifest: dict | None) -> list[str]:
    lines = [f"### Data lake `{tag}`: {status}", ""]
    if manifest is None:
        return lines
    lines += ["| asset | bytes | rows | sha256 |", "| --- | ---: | ---: | --- |"]
    lines += [f"| `{a['name']}` | {a['bytes']} | {a['rows']} | `{a['sha256']}` |" for a in manifest["assets"]]
    stats = manifest["stats"]
    agg = stats["aggTrades"]
    lines += ["", f"aggTrades rows {agg['rows']}, id gaps {agg['id_gaps']}, ids not increasing "
              f"{agg['id_not_increasing']}, rows outside month {agg['rows_outside_month']}, minutes without trades "
              f"{agg['minutes_without_trades']}.",
              f"Month volume: aggTrades {stats['month_total_volume']['aggTrades']}, "
              f"klines {stats['month_total_volume']['klines']}.",
              "", "| flag | minutes |", "| --- | ---: |"]
    lines += [f"| {name} | {count} |" for name, count in stats["flag_counts"].items()]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--month", required=True, help="finished UTC month, YYYY-MM")
    parser.add_argument("--workdir", default="data-lake-work")
    parser.add_argument("--publish", action="store_true", help="create the release (otherwise a dry run)")
    parser.add_argument("--summary", help="append a markdown summary to this file")
    args = parser.parse_args(argv)
    try:
        symbol = lake.validate_symbol(args.symbol)
        month = lake.validate_month(args.month)
    except ValueError as error:
        parser.error(str(error))
    if month < lake.FIRST_MONTH or not lake.month_finished(month, int(time.time() * 1000)):
        parser.error(f"{month} is before {lake.FIRST_MONTH} or not a fully finished UTC month")
    tag = lake.release_tag(symbol, month)

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

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    sources = {family: download_source(family, symbol, month, workdir) for family in lake.FAMILIES}
    outputs, stats = build(symbol, month, workdir, sources)
    manifest = manifest_for(symbol, month, tag, sources, outputs, stats)
    manifest_path = workdir / "manifest.json"
    manifest_path.write_text(lake.canonical_json(manifest), encoding="ascii")
    uploads = [{"name": source["asset"], "path": source["path"], "sha256": source["sha256"], "bytes": source["bytes"]}
               for source in sources.values()]
    uploads += [{key: output[key] for key in ("name", "path", "sha256", "bytes")} for output in outputs]
    manifest_sha, manifest_size = file_sha256(manifest_path)
    uploads.append({"name": "manifest.json", "path": manifest_path, "sha256": manifest_sha, "bytes": manifest_size})
    too_large = [upload["name"] for upload in uploads if upload["bytes"] >= lake.MAX_ASSET_BYTES]
    if too_large:
        raise RuntimeError(f"assets at or above the 2 GiB release limit: {too_large}")
    print(f"built {tag} in {time.monotonic() - started:.1f}s", flush=True)

    if repo is None:
        print(lake.canonical_json(manifest), end="")
        write_summary(args.summary, summary_lines(tag, "dry run (not published)", manifest))
        return 0
    published = publish_release(repo, tag, symbol, month, uploads)
    print(f"{tag}: published release {published['id']}")
    write_summary(args.summary, summary_lines(tag, "published", manifest))
    return 0


def annotate_failure(error: BaseException) -> None:
    """Emit the failure as a GitHub Actions error annotation.

    Job logs are not retrievable through the API without leaving GitHub, but
    annotations are, so the failure reason is repeated there. Single line,
    workflow-command encoded, token redacted, length bounded; no data values.
    """
    import traceback

    frames = traceback.extract_tb(error.__traceback__)[-3:]
    where = " <- ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in reversed(frames))
    message = f"{type(error).__name__}: {error} [{where}]"
    token = os.environ.get("RESEARCH_DATA_TOKEN")
    if token:
        message = message.replace(token, "***")
    message = message[:1500].replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::error title=data lake build failed::{message}", flush=True)


if __name__ == "__main__":
    try:
        code = main()
    except Exception as error:  # noqa: BLE001 - re-raised after annotating
        annotate_failure(error)
        raise
    sys.exit(code)
