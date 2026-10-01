"""Issue #35 Part 4: acquire official daily USD-M archives into a verified cache.

Acquisition mechanics are separate from Part 2's archive interpretation and
scientific content identity. This module never reads market CSV rows itself.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

from .binance_historical_archive import (
    BinanceHistoricalReplayDataset, BinanceUSDMArchiveRequest,
    daily_aggtrades_checksum_relative_path, daily_aggtrades_relative_path,
    daily_kline_checksum_relative_path, daily_kline_relative_path,
    load_binance_usdm_historical_replay_dataset, required_aggtrade_dates,
    required_kline_dates,
)
from .historical_replay import DEFAULT_FINALIZATION_GRACE_MS, HistoricalReplayConfig
from .movement_metrics import MarketMovementConfig, MarketUniverseInput


BINANCE_ARCHIVE_DOWNLOAD_VERSION = "binance-usdm-daily-archive-download-v1"
BINANCE_PUBLIC_DATA_BASE_URL = "https://data.binance.vision/"
BINANCE_DOWNLOAD_TIMEOUT_SECONDS = 30
BINANCE_DOWNLOAD_MAX_ATTEMPTS = 3
BINANCE_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_USER_AGENT = "CryptoWatch-Historical-Research/1"
_CHECKSUM_LINE = re.compile(r"([0-9a-fA-F]{64})\s+\*?([^\s]+)")
_UTC_CLI = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_RETRY_HTTP_STATUSES = frozenset((408, 429, 500, 502, 503, 504))


class BinanceArchiveDownloadError(ValueError):
    """An official HTTP/transport request failed."""


class BinanceArchiveRemoteMissingError(BinanceArchiveDownloadError):
    """A requested official daily ZIP or CHECKSUM returned HTTP 404."""


@dataclass(frozen=True)
class BinanceHistoricalDownloadRequest:
    archive_root: Path
    universe: MarketUniverseInput
    replay_config: HistoricalReplayConfig

    def __post_init__(self):
        validated = BinanceUSDMArchiveRequest(
            self.archive_root, self.universe, self.replay_config)
        object.__setattr__(self, "archive_root", validated.archive_root)


@dataclass(frozen=True)
class BinanceArchiveDownloadItem:
    relative_zip_path: PurePosixPath
    relative_checksum_path: PurePosixPath
    symbol: str
    family: str
    utc_date: date


@dataclass(frozen=True)
class BinanceArchiveDownloadItemResult:
    relative_zip_path: str
    symbol: str
    family: str
    utc_date: date
    expected_sha256: str
    final_sha256: str
    status: str
    bytes_downloaded: int
    attempts: int


@dataclass(frozen=True)
class BinanceHistoricalDownloadResult:
    download_version: str
    requested_item_count: int
    downloaded_count: int
    reused_count: int
    refreshed_count: int
    bytes_downloaded: int
    items: tuple[BinanceArchiveDownloadItemResult, ...]


@dataclass(frozen=True)
class BinanceHistoricalAcquisitionResult:
    download: BinanceHistoricalDownloadResult
    dataset: BinanceHistoricalReplayDataset


def plan_binance_usdm_historical_download(
    request: BinanceHistoricalDownloadRequest,
) -> tuple[BinanceArchiveDownloadItem, ...]:
    """Plan exact Part 2 paths in universe, family, then UTC-date order."""
    if not isinstance(request, BinanceHistoricalDownloadRequest):
        raise ValueError("request must be BinanceHistoricalDownloadRequest")
    plan = []
    for symbol in request.universe.symbols:
        for day in required_aggtrade_dates(request.replay_config):
            plan.append(BinanceArchiveDownloadItem(
                daily_aggtrades_relative_path(symbol, day),
                daily_aggtrades_checksum_relative_path(symbol, day),
                symbol, "aggTrades", day))
        for day in required_kline_dates(request.replay_config):
            plan.append(BinanceArchiveDownloadItem(
                daily_kline_relative_path(symbol, day),
                daily_kline_checksum_relative_path(symbol, day),
                symbol, "klines/1m", day))
    return tuple(plan)


def official_archive_url(relative_path: PurePosixPath) -> str:
    if (not isinstance(relative_path, PurePosixPath)
            or relative_path.is_absolute() or ".." in relative_path.parts):
        raise ValueError("official archive URL requires a safe relative POSIX path")
    return urljoin(BINANCE_PUBLIC_DATA_BASE_URL,
                   quote(relative_path.as_posix(), safe="/"))


class _UrllibTransport:
    def open(self, url: str, timeout: int):
        return urlopen(Request(url, headers={"User-Agent": _USER_AGENT}),
                       timeout=timeout)


def _remote_error(item: BinanceArchiveDownloadItem, relative: PurePosixPath,
                  attempts: int, reason: str, *, missing: bool = False):
    kind = BinanceArchiveRemoteMissingError if missing else BinanceArchiveDownloadError
    return kind(f"{item.symbol} {item.family} {item.utc_date} {relative}: "
                f"after {attempts} attempt(s): {reason}")


def _transfer(item, relative, consume, transport, sleeper):
    """Retry only transient HTTP/transport failures, including streaming reads."""
    url = official_archive_url(relative)
    for attempt in range(1, BINANCE_DOWNLOAD_MAX_ATTEMPTS + 1):
        try:
            with transport.open(url, timeout=BINANCE_DOWNLOAD_TIMEOUT_SECONDS) as response:
                return consume(response), attempt
        except HTTPError as exc:
            if exc.code == 404:
                raise _remote_error(item, relative, attempt, "HTTP 404", missing=True) from exc
            if exc.code not in _RETRY_HTTP_STATUSES or attempt == BINANCE_DOWNLOAD_MAX_ATTEMPTS:
                raise _remote_error(item, relative, attempt, f"HTTP {exc.code}") from exc
        except (URLError, TimeoutError, ConnectionError) as exc:
            if attempt == BINANCE_DOWNLOAD_MAX_ATTEMPTS:
                raise _remote_error(item, relative, attempt, str(exc)) from exc
        sleeper(attempt)
    raise AssertionError("unreachable retry state")


def _parse_remote_checksum(payload: bytes, item: BinanceArchiveDownloadItem) -> str:
    try:
        lines = [line.strip() for line in payload.decode("utf-8").splitlines()
                 if line.strip()]
    except UnicodeError as exc:
        raise ValueError(f"invalid remote checksum for {item.relative_zip_path}: UTF-8 required") from exc
    if len(lines) != 1:
        raise ValueError(f"invalid remote checksum for {item.relative_zip_path}: one entry required")
    match = _CHECKSUM_LINE.fullmatch(lines[0])
    if match is None or match.group(2) != item.relative_zip_path.name:
        raise ValueError(f"invalid remote checksum for {item.relative_zip_path}: "
                         f"must name {item.relative_zip_path.name} with SHA-256")
    return match.group(1).lower()


def _remote_read(response, size: int) -> bytes:
    try:
        return response.read(size)
    except HTTPError:
        raise
    except OSError as exc:
        raise URLError(exc) from exc


def _fetch_checksum(item, transport, sleeper):
    def consume(response):
        payload = _remote_read(response, 8193)
        if len(payload) > 8192:
            raise ValueError(f"remote checksum is too large: {item.relative_checksum_path}")
        return payload
    payload, attempts = _transfer(item, item.relative_checksum_path,
                                  consume, transport, sleeper)
    return _parse_remote_checksum(payload, item), attempts


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(BINANCE_DOWNLOAD_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _temporary_sibling(destination: Path, *, label: str) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.{label}-", suffix=".tmp",
        dir=destination.parent)
    os.close(descriptor)
    return Path(name)


def _stage_checksum(destination: Path, expected_sha256: str,
                    zip_name: str) -> Path:
    temporary = _temporary_sibling(destination, label="checksum")
    try:
        with temporary.open("wb") as stream:
            stream.write(f"{expected_sha256}  {zip_name}\n".encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _download_verified_zip(item, destination, expected_sha256,
                           transport, sleeper):
    def consume(response):
        temporary = _temporary_sibling(destination, label="download")
        digest = hashlib.sha256()
        byte_count = 0
        try:
            with temporary.open("wb") as stream:
                while True:
                    chunk = _remote_read(response, BINANCE_DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    stream.write(chunk)
                    digest.update(chunk)
                    byte_count += len(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            actual = digest.hexdigest()
            if actual != expected_sha256:
                raise ValueError(f"download checksum mismatch for {item.relative_zip_path}: "
                                 f"expected {expected_sha256}, actual {actual}")
            return temporary, byte_count
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    return _transfer(item, item.relative_zip_path, consume, transport, sleeper)


def _acquire_item(request, item, transport, sleeper):
    expected, checksum_attempts = _fetch_checksum(item, transport, sleeper)
    final_zip = request.archive_root.joinpath(*item.relative_zip_path.parts)
    final_checksum = request.archive_root.joinpath(*item.relative_checksum_path.parts)
    final_zip.parent.mkdir(parents=True, exist_ok=True)
    local_exists = final_zip.is_file()
    if local_exists and _sha256_file(final_zip) == expected:
        canonical_checksum = f"{expected}  {final_zip.name}\n".encode("ascii")
        if not final_checksum.is_file() or final_checksum.read_bytes() != canonical_checksum:
            temporary_checksum = _stage_checksum(final_checksum, expected, final_zip.name)
            try:
                os.replace(temporary_checksum, final_checksum)
            finally:
                temporary_checksum.unlink(missing_ok=True)
        return BinanceArchiveDownloadItemResult(
            item.relative_zip_path.as_posix(), item.symbol, item.family,
            item.utc_date, expected, expected, "REUSED", 0, checksum_attempts)
    (temporary_zip, byte_count), zip_attempts = _download_verified_zip(
        item, final_zip, expected, transport, sleeper)
    try:
        temporary_checksum = _stage_checksum(final_checksum, expected, final_zip.name)
        try:
            os.replace(temporary_zip, final_zip)
            os.replace(temporary_checksum, final_checksum)
        finally:
            temporary_checksum.unlink(missing_ok=True)
    finally:
        temporary_zip.unlink(missing_ok=True)
    return BinanceArchiveDownloadItemResult(
        item.relative_zip_path.as_posix(), item.symbol, item.family,
        item.utc_date, expected, expected,
        "REFRESHED" if local_exists else "DOWNLOADED", byte_count,
        checksum_attempts + zip_attempts)


def acquire_binance_usdm_historical_archive_files(
    request: BinanceHistoricalDownloadRequest,
    *,
    _transport=None,
    _sleeper=None,
) -> BinanceHistoricalDownloadResult:
    """Acquire verified archive files without constructing a replay dataset."""
    if not isinstance(request, BinanceHistoricalDownloadRequest):
        raise ValueError("request must be BinanceHistoricalDownloadRequest")
    transport = _UrllibTransport() if _transport is None else _transport
    sleeper = time.sleep if _sleeper is None else _sleeper
    plan = plan_binance_usdm_historical_download(request)
    results = tuple(_acquire_item(request, item, transport, sleeper) for item in plan)
    return BinanceHistoricalDownloadResult(
        BINANCE_ARCHIVE_DOWNLOAD_VERSION, len(plan),
        sum(item.status == "DOWNLOADED" for item in results),
        sum(item.status == "REUSED" for item in results),
        sum(item.status == "REFRESHED" for item in results),
        sum(item.bytes_downloaded for item in results), results)


def acquire_binance_usdm_historical_archives(
    request: BinanceHistoricalDownloadRequest,
    *,
    _transport=None,
    _sleeper=None,
) -> BinanceHistoricalAcquisitionResult:
    """Acquire official files, then validate them through the full replay loader."""
    if not isinstance(request, BinanceHistoricalDownloadRequest):
        raise ValueError("request must be BinanceHistoricalDownloadRequest")
    download = acquire_binance_usdm_historical_archive_files(
        request, _transport=_transport, _sleeper=_sleeper)
    dataset = load_binance_usdm_historical_replay_dataset(
        BinanceUSDMArchiveRequest(request.archive_root, request.universe,
                                  request.replay_config))
    return BinanceHistoricalAcquisitionResult(download, dataset)


def parse_utc_cli_timestamp(value: str) -> int:
    if not isinstance(value, str) or _UTC_CLI.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("timestamp must be YYYY-MM-DDTHH:MM:SSZ")
    try:
        instant = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("invalid UTC timestamp") from exc
    milliseconds = (instant - _EPOCH) // timedelta(milliseconds=1)
    if milliseconds < 0 or milliseconds % 5_000:
        raise argparse.ArgumentTypeError("timestamp must be nonnegative and five-second aligned")
    return milliseconds


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Acquire official Binance USD-M daily archives")
    parser.add_argument("--archive-root", required=True)
    parser.add_argument("--symbols", required=True, nargs="+")
    parser.add_argument("--universe-id", required=True)
    parser.add_argument("--universe-version", required=True)
    parser.add_argument("--start", required=True, type=parse_utc_cli_timestamp)
    parser.add_argument("--end", required=True, type=parse_utc_cli_timestamp)
    parser.add_argument("--finalization-grace-ms", type=int,
                        default=DEFAULT_FINALIZATION_GRACE_MS)
    return parser


def acquisition_summary_json(result: BinanceHistoricalAcquisitionResult) -> str:
    download = result.download
    return json.dumps({
        "bytes_downloaded": download.bytes_downloaded,
        "dataset_content_sha256": result.dataset.archive_manifest.content_sha256,
        "downloaded_count": download.downloaded_count,
        "refreshed_count": download.refreshed_count,
        "requested_item_count": download.requested_item_count,
        "reused_count": download.reused_count,
    }, sort_keys=True, separators=(",", ":"), allow_nan=False)


def main(argv: list[str] | None = None) -> int:
    args = build_cli_parser().parse_args(argv)
    try:
        request = BinanceHistoricalDownloadRequest(
            args.archive_root,
            MarketUniverseInput(args.universe_id, args.universe_version,
                                tuple(args.symbols)),
            HistoricalReplayConfig(args.start, args.end,
                                   args.finalization_grace_ms,
                                   MarketMovementConfig()),
        )
        result = acquire_binance_usdm_historical_archives(request)
        sys.stdout.write(acquisition_summary_json(result) + "\n")
    except (ValueError, OSError) as exc:
        print(f"Binance archive acquisition: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
