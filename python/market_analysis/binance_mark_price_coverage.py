"""Separate, opt-in source-coverage audit for Binance USD-M mark-price archives.

This tool inspects daily markPriceKlines packages only. It does not alter the
core replay dataset, replay fingerprints, points, manifests, or experiments.
Network acquisition is available only when the CLI receives ``--download``.
"""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

from .binance_historical_archive import (
    _KLINE_HEADER, _SHA256_LINE, _checksum, _is_header,
)
from .movement_history import MINUTE_MS


MARK_PRICE_COVERAGE_REPORT_ID = "binance-usdm-mark-price-source-coverage"
MARK_PRICE_COVERAGE_REPORT_VERSION = "binance-usdm-mark-price-source-coverage-v1"
MARK_PRICE_COVERAGE_SCHEMA_VERSION = "binance-mark-price-coverage-schema-v1"
MARK_PRICE_COVERAGE_TOOL_VERSION = "binance-mark-price-coverage-tool-v1"
MARK_PRICE_SOURCE = "binance-public-data-usdm-markPriceKlines"
MARK_PRICE_TYPE = "mark-price"
MARK_PRICE_INTERVAL = "1m"
MARK_PRICE_WARMUP_MINUTES = 16
MARK_PRICE_HORIZONS_MINUTES = (1, 5, 15)
MARK_PRICE_AVAILABILITY_CONVENTION = (
    "completed-candle-close-time-plus-one-millisecond-equals-boundary-v1"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_UTC_CLI = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_BINANCE_ARCHIVE_ROOT_URL = "https://data.binance.vision/"


def daily_mark_price_relative_path(symbol: str, utc_date: date) -> PurePosixPath:
    if (not isinstance(symbol, str) or not symbol or symbol != symbol.upper()
            or not symbol.isascii() or not symbol.isalnum() or "_" in symbol):
        raise ValueError("symbols must be uppercase USD-M perpetual symbols without underscores")
    if type(utc_date) is not date:
        raise ValueError("UTC archive date must be datetime.date")
    return PurePosixPath(
        "data", "futures", "um", "daily", "markPriceKlines", symbol, "1m",
        f"{symbol}-1m-{utc_date.isoformat()}.zip",
    )


def _timestamp_ms(value: str, label: str) -> int:
    if not isinstance(value, str) or _UTC_CLI.fullmatch(value) is None:
        raise ValueError(f"{label} must be YYYY-MM-DDTHH:MM:SSZ")
    try:
        instant = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(f"invalid {label}") from exc
    result = int((instant - _EPOCH) // timedelta(milliseconds=1))
    if result < 0 or result % MINUTE_MS:
        raise ValueError(f"{label} must be a nonnegative minute boundary")
    return result


def _utc_date(timestamp_ms: int) -> date:
    return (_EPOCH + timedelta(milliseconds=timestamp_ms)).date()


def _date_range(start_ms: int, end_ms: int) -> tuple[date, ...]:
    first = _utc_date(start_ms)
    last = _utc_date(end_ms - MINUTE_MS)
    return tuple(first + timedelta(days=n) for n in range((last - first).days + 1))


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _sha256(value) -> str:
    return hashlib.sha256(_canonical_json(value).encode("ascii")).hexdigest()


def _current_code_revision() -> str:
    repository_root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        ["git", "-C", str(repository_root), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    )
    revision = completed.stdout.strip()
    if not revision:
        raise ValueError("could not determine code revision")
    return revision


def _gap_ranges(timestamps: list[int]) -> list[dict]:
    if not timestamps:
        return []
    ranges = []
    first = previous = timestamps[0]
    for current in timestamps[1:]:
        if current != previous + MINUTE_MS:
            ranges.append({"start_open_time_ms": first,
                           "end_open_time_ms": previous,
                           "missing_minutes": (previous - first) // MINUTE_MS + 1})
            first = current
        previous = current
    ranges.append({"start_open_time_ms": first, "end_open_time_ms": previous,
                   "missing_minutes": (previous - first) // MINUTE_MS + 1})
    return ranges


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 12) if denominator else 1.0


def _parse_integer(value: str, label: str) -> int:
    if not isinstance(value, str) or re.fullmatch(r"\d+", value.strip()) is None:
        raise ValueError(f"{label} must be an unsigned integer")
    return int(value)


def _parse_positive_decimal(value: str, label: str) -> Decimal:
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"{label} must be a decimal number") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return result


def _parse_mark_row(row: list[str], utc_day: date) -> tuple[int, int]:
    if len(row) != 12:
        raise ValueError("mark-price kline row must have twelve columns")
    opening = _parse_integer(row[0], "open_time")
    opened = _parse_positive_decimal(row[1], "open")
    high = _parse_positive_decimal(row[2], "high")
    low = _parse_positive_decimal(row[3], "low")
    closed = _parse_positive_decimal(row[4], "close")
    closing = _parse_integer(row[6], "close_time")
    if _utc_date(opening) != utc_day:
        raise ValueError("open_time is outside the archive UTC date")
    if closing != opening + MINUTE_MS - 1:
        raise ValueError("mark-price kline must cover exactly one complete minute")
    if high < max(opened, closed) or low > min(opened, closed):
        raise ValueError("mark-price OHLC values are inconsistent")
    return opening, closing


def _official_checksum(checksum_url: str, filename: str) -> str:
    with urllib.request.urlopen(checksum_url, timeout=30) as response:
        raw = response.read(4096)
    lines = [line.strip() for line in raw.decode("utf-8").splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError("official checksum must contain exactly one entry")
    match = _SHA256_LINE.fullmatch(lines[0])
    if match is None or match.group(2) != filename:
        raise ValueError(f"official checksum must name {filename}")
    return match.group(1).lower()


def _download_package(root: Path, relative: PurePosixPath) -> None:
    """Fetch one package only when --download explicitly enabled this path."""
    archive = root.joinpath(*relative.parts)
    checksum_path = Path(f"{archive}.CHECKSUM")
    checksum_relative = PurePosixPath(f"{relative.as_posix()}.CHECKSUM")
    archive.parent.mkdir(parents=True, exist_ok=True)
    remote_zip = _BINANCE_ARCHIVE_ROOT_URL + relative.as_posix()
    remote_checksum = _BINANCE_ARCHIVE_ROOT_URL + checksum_relative.as_posix()
    expected = _official_checksum(remote_checksum, relative.name)
    with urllib.request.urlopen(remote_zip, timeout=60) as response:
        payload = response.read()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise ValueError(f"download checksum mismatch for {relative.as_posix()}")
    with tempfile.NamedTemporaryFile(dir=archive.parent, prefix=f".{archive.name}.",
                                     delete=False) as stream:
        temporary_archive = Path(stream.name)
        stream.write(payload)
        stream.flush()
    try:
        temporary_checksum = temporary_archive.with_name(temporary_archive.name + ".CHECKSUM")
        temporary_checksum.write_text(f"{expected}  {relative.name}\n", encoding="ascii")
        temporary_archive.replace(archive)
        temporary_checksum.replace(checksum_path)
    finally:
        temporary_archive.unlink(missing_ok=True)
        temporary_archive.with_name(temporary_archive.name + ".CHECKSUM").unlink(missing_ok=True)


def _package_report(root: Path, symbol: str, utc_day: date,
                    expected_opens: list[int], *, download: bool) -> tuple[dict, dict[int, bool]]:
    relative = daily_mark_price_relative_path(symbol, utc_day)
    archive = root.joinpath(*relative.parts)
    checksum_path = Path(f"{archive}.CHECKSUM")
    download_status = "NOT_REQUESTED"
    if download:
        if archive.is_file() and checksum_path.is_file():
            try:
                _checksum(root, relative, symbol, "markPriceKlines/1m", utc_day)
                download_status = "REUSED_VERIFIED_LOCAL"
                download_error = None
            except ValueError:
                download_status = "FETCHED_AND_VERIFIED"
                try:
                    _download_package(root, relative)
                    download_error = None
                except Exception as exc:
                    download_status = "FAILED"
                    download_error = f"{type(exc).__name__}: {exc}"
        else:
            download_status = "FETCHED_AND_VERIFIED"
            try:
                _download_package(root, relative)
                download_error = None
            except Exception as exc:
                download_status = "FAILED"
                download_error = f"{type(exc).__name__}: {exc}"
    else:
        download_error = None

    archive_present = archive.is_file()
    checksum_present = checksum_path.is_file()
    statuses = []
    report = {
        "relative_archive_path": relative.as_posix(),
        "archive_present": archive_present,
        "checksum_present": checksum_present,
        "checksum_verified": False,
        "verified_archive_sha256": None,
        "download_status": download_status,
        "status": None,
        "statuses": statuses,
        "expected_minute_count": len(expected_opens),
        "observed_unique_on_grid_minute_count": 0,
        "missing_minute_count": len(expected_opens),
        "missing_minute_ranges": _gap_ranges(expected_opens),
        "row_count": 0,
        "parse_error_count": 0,
        "parse_errors": [],
        "duplicate_row_count": 0,
        "duplicate_timestamps_ms": [],
        "off_grid_row_count": 0,
        "off_grid_timestamps_ms": [],
    }
    if download_error is not None:
        report["download_error"] = download_error
    if not archive_present:
        statuses.append("MISSING_PACKAGE")
    if not checksum_present:
        statuses.append("MISSING_CHECKSUM")
    if not archive_present or not checksum_present:
        report["status"] = statuses[0]
        return report, {}

    try:
        digest = _checksum(root, relative, symbol, "markPriceKlines/1m", utc_day)
    except ValueError as exc:
        message = str(exc)
        if "checksum mismatch" in message:
            statuses.append("CHECKSUM_MISMATCH")
        else:
            statuses.append("INVALID_CHECKSUM")
        report["checksum_error"] = message
        report["status"] = statuses[0]
        return report, {}
    report["checksum_verified"] = True
    report["verified_archive_sha256"] = digest

    observed: dict[int, bool] = {}
    expected_opens_set = set(expected_opens)
    seen: dict[int, tuple[int, int]] = {}
    parse_errors = report["parse_errors"]
    try:
        with zipfile.ZipFile(archive) as bundle:
            csv_members = []
            for member in bundle.infolist():
                member_path = PurePosixPath(member.filename.replace("\\", "/"))
                if (member.flag_bits & 1 or member_path.is_absolute()
                        or PureWindowsPath(member.filename).drive
                        or ".." in member_path.parts):
                    raise ValueError(f"unsafe ZIP member: {member.filename}")
                if not member.is_dir() and member_path.suffix.lower() == ".csv":
                    csv_members.append(member)
            expected_name = f"{relative.stem}.csv"
            if (len(csv_members) != 1
                    or PurePosixPath(csv_members[0].filename).name != expected_name):
                raise ValueError(f"archive must contain exactly one {expected_name} CSV")
            with bundle.open(csv_members[0]) as raw:
                with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text_stream:
                    reader = csv.reader(text_stream, strict=True)
                    try:
                        for line_number, row in enumerate(reader, start=1):
                            report["row_count"] += 1
                            if line_number == 1 and _is_header(row, _KLINE_HEADER):
                                report["row_count"] -= 1
                                continue
                            try:
                                opening, closing = _parse_mark_row(row, utc_day)
                            except (ValueError, OverflowError) as exc:
                                parse_errors.append({"line": line_number, "error": str(exc)})
                                continue
                            prior = seen.get(opening)
                            if prior is not None:
                                report["duplicate_row_count"] += 1
                                report["duplicate_timestamps_ms"].append(opening)
                                continue
                            seen[opening] = (opening, closing)
                            if opening % MINUTE_MS:
                                report["off_grid_row_count"] += 1
                                report["off_grid_timestamps_ms"].append(opening)
                                continue
                            if opening in expected_opens_set:
                                observed[opening] = True
                    except (csv.Error, UnicodeError) as exc:
                        parse_errors.append({"line": reader.line_num,
                                             "error": f"CSV parse error: {exc}"})
    except (OSError, zipfile.BadZipFile, RuntimeError, UnicodeError, ValueError) as exc:
        parse_errors.append({"line": None, "error": f"archive parse error: {exc}"})

    report["parse_error_count"] = len(parse_errors)
    report["observed_unique_on_grid_minute_count"] = len(observed)
    missing = [timestamp for timestamp in expected_opens if timestamp not in observed]
    report["missing_minute_count"] = len(missing)
    report["missing_minute_ranges"] = _gap_ranges(missing)
    if parse_errors:
        statuses.append("INVALID_SCHEMA_ROW")
    if report["duplicate_row_count"]:
        statuses.append("DUPLICATE_TIMESTAMP")
    if report["off_grid_row_count"]:
        statuses.append("OFF_GRID_TIMESTAMP")
    if missing:
        statuses.append("VALID_WITH_MISSING_MINUTES")
    if not statuses:
        statuses.append("VALID_COMPLETE")
    report["statuses"] = statuses
    report["status"] = statuses[0]
    return report, observed


def _contiguous_ready(observed: set[int], output_boundaries: list[int],
                      horizon: int) -> tuple[int, list[int]]:
    not_ready = []
    ready_count = 0
    for boundary in output_boundaries:
        first_open = boundary - (horizon + 1) * MINUTE_MS
        required = range(first_open, boundary, MINUTE_MS)
        if all(timestamp in observed for timestamp in required):
            ready_count += 1
        else:
            not_ready.append(boundary)
    return ready_count, not_ready


def build_mark_price_coverage_report(
    symbols: tuple[str, ...], start_ms: int, end_ms: int, archive_root: Path,
    *, code_revision: str, download: bool = False,
) -> dict:
    if (not isinstance(symbols, tuple) or not symbols
            or len(set(symbols)) != len(symbols)):
        raise ValueError("symbols must be a nonempty ordered tuple without duplicates")
    for symbol in symbols:
        daily_mark_price_relative_path(symbol, date(1970, 1, 1))
    if (type(start_ms) is not int or type(end_ms) is not int
            or start_ms < 0 or end_ms <= start_ms
            or start_ms % MINUTE_MS or end_ms % MINUTE_MS):
        raise ValueError("study range must be a nonempty minute-aligned [start, end) range")
    if not isinstance(code_revision, str) or not code_revision:
        raise ValueError("code_revision is required")
    root = Path(archive_root).expanduser().resolve()
    effective_start = start_ms - MARK_PRICE_WARMUP_MINUTES * MINUTE_MS
    if effective_start < 0:
        raise ValueError("16-minute warm-up would begin before the Unix epoch")
    expected_opens = list(range(effective_start, end_ms, MINUTE_MS))
    period_expected = set(expected_opens)
    output_boundaries = list(range(start_ms, end_ms, MINUTE_MS))
    days = _date_range(effective_start, end_ms)

    symbol_results = []
    observed_by_symbol: dict[str, set[int]] = {}
    all_package_reports = []
    for symbol in symbols:
        unique: set[int] = set()
        package_reports = []
        for day in days:
            day_start = int((datetime(day.year, day.month, day.day,
                                      tzinfo=timezone.utc) - _EPOCH)
                            // timedelta(milliseconds=1))
            day_opens = list(range(max(effective_start, day_start),
                                   min(end_ms, day_start + 24 * 60 * MINUTE_MS),
                                   MINUTE_MS))
            package_report, package_observed = _package_report(
                root, symbol, day, day_opens, download=download)
            package_reports.append(package_report)
            all_package_reports.append({"symbol": symbol, **package_report})
            unique.update(package_observed)
        observed_by_symbol[symbol] = unique
        missing = sorted(period_expected - unique)
        horizon_results = {}
        for horizon in MARK_PRICE_HORIZONS_MINUTES:
            ready, unavailable = _contiguous_ready(unique, output_boundaries, horizon)
            gaps = _gap_ranges(unavailable)
            horizon_results[f"{horizon}m"] = {
                "ready_boundary_count": ready,
                "expected_boundary_count": len(output_boundaries),
                "coverage_ratio": _ratio(ready, len(output_boundaries)),
                "unavailable_boundary_ranges": gaps,
                "longest_unavailable_boundary_gap_minutes": max(
                    (item["missing_minutes"] for item in gaps), default=0),
            }
        symbol_results.append({
            "symbol": symbol,
            "expected_package_count": len(days),
            "checksum_verified_package_count": sum(
                package["checksum_verified"] for package in package_reports),
            "expected_unique_on_grid_minute_count": len(expected_opens),
            "observed_unique_on_grid_minute_count": len(unique),
            "coverage_ratio": _ratio(len(unique), len(expected_opens)),
            "missing_minute_count": len(missing),
            "missing_minute_ranges": _gap_ranges(missing),
            "longest_contiguous_gap_minutes": max(
                (item["missing_minutes"] for item in _gap_ranges(missing)), default=0),
            "contiguous_ready_coverage": horizon_results,
            "packages": package_reports,
        })

    shared = set.intersection(*(observed_by_symbol[symbol] for symbol in symbols))
    shared_missing = sorted(period_expected - shared)
    shared_horizons = {}
    for horizon in MARK_PRICE_HORIZONS_MINUTES:
        ready, unavailable = _contiguous_ready(shared, output_boundaries, horizon)
        gaps = _gap_ranges(unavailable)
        shared_horizons[f"{horizon}m"] = {
            "ready_boundary_count": ready,
            "expected_boundary_count": len(output_boundaries),
            "coverage_ratio": _ratio(ready, len(output_boundaries)),
            "unavailable_boundary_ranges": gaps,
            "longest_unavailable_boundary_gap_minutes": max(
                (item["missing_minutes"] for item in gaps), default=0),
        }

    verified_packages = [
        {"symbol": item["symbol"], "relative_archive_path": item["relative_archive_path"],
         "sha256": item["verified_archive_sha256"]}
        for item in all_package_reports if item["checksum_verified"]
    ]
    identity = {
        "report_id": MARK_PRICE_COVERAGE_REPORT_ID,
        "report_version": MARK_PRICE_COVERAGE_REPORT_VERSION,
        "schema_version": MARK_PRICE_COVERAGE_SCHEMA_VERSION,
        "ordered_symbols": list(symbols),
        "requested_study_range": {
            "start_boundary_time_ms_inclusive": start_ms,
            "end_boundary_time_ms_exclusive": end_ms,
        },
        "effective_warmup": {
            "minutes": MARK_PRICE_WARMUP_MINUTES,
            "start_time_ms_inclusive": effective_start,
            "end_time_ms_exclusive": start_ms,
        },
        "source": {"name": MARK_PRICE_SOURCE, "type": MARK_PRICE_TYPE,
                   "interval": MARK_PRICE_INTERVAL},
        "availability_convention": MARK_PRICE_AVAILABILITY_CONVENTION,
        "coverage_rules": {
            "expected_candle_open_times": "[warmup_start, requested_end), one-minute UTC grid",
            "requested_output_boundaries": "[requested_start, requested_end), one-minute UTC grid",
            "contiguous_ready_horizons_minutes": list(MARK_PRICE_HORIZONS_MINUTES),
            "horizon_candles_required": "h + 1 consecutive one-minute candles ending at boundary - 1m",
            "no_threshold_pass_rule": True,
        },
        "verified_packages": verified_packages,
        "tool_config_version": MARK_PRICE_COVERAGE_TOOL_VERSION,
        "code_revision": code_revision,
    }
    report = {
        "identity": identity,
        "study_period": {
            "requested_start_boundary_time_ms_inclusive": start_ms,
            "requested_end_boundary_time_ms_exclusive": end_ms,
            "effective_warmup_start_time_ms_inclusive": effective_start,
            "effective_data_end_time_ms_exclusive": end_ms,
            "expected_minute_count_including_warmup": len(expected_opens),
            "expected_output_boundary_count": len(output_boundaries),
        },
        "period_coverage": symbol_results,
        "shared_exact_minute_coverage": {
            "expected_unique_on_grid_minute_count": len(expected_opens),
            "observed_unique_on_grid_minute_count": len(shared),
            "coverage_ratio": _ratio(len(shared), len(expected_opens)),
            "missing_minute_count": len(shared_missing),
            "missing_minute_ranges": _gap_ranges(shared_missing),
        },
        "shared_contiguous_ready_coverage": shared_horizons,
        "audit_sha256": None,
    }
    report["audit_sha256"] = _sha256({
        key: value for key, value in report.items() if key != "audit_sha256"
    })
    return report


def mark_price_coverage_report_to_json(report: dict) -> str:
    if not isinstance(report, dict) or not isinstance(report.get("identity"), dict):
        raise ValueError("report must be a mark-price coverage report")
    expected = _sha256({key: value for key, value in report.items()
                        if key != "audit_sha256"})
    if report.get("audit_sha256") != expected:
        raise ValueError("mark-price coverage audit digest does not match canonical content")
    return _canonical_json(report)


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit local Binance USD-M daily 1m markPriceKlines coverage.")
    parser.add_argument("--symbols", nargs="+", required=True,
                        help="explicit ordered USD-M symbols")
    parser.add_argument("--start", required=True,
                        help="inclusive output boundary, UTC YYYY-MM-DDTHH:MM:SSZ")
    parser.add_argument("--end", required=True,
                        help="exclusive output boundary, UTC YYYY-MM-DDTHH:MM:SSZ")
    parser.add_argument("--archive-root", type=Path, required=True,
                        help="separate local mark-price archive root")
    parser.add_argument("--output", type=Path, required=True,
                        help="audit JSON output path")
    parser.add_argument("--download", action="store_true",
                        help="opt in to fetch missing or checksum-invalid daily packages from Binance")
    parser.add_argument("--overwrite", action="store_true",
                        help="replace an existing output JSON file")
    parser.add_argument("--code-revision",
                        help="explicit revision label; defaults to git HEAD")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    if args.output.exists() and not args.overwrite:
        parser.error("output file already exists; pass --overwrite to replace it")
    try:
        start_ms = _timestamp_ms(args.start, "--start")
        end_ms = _timestamp_ms(args.end, "--end")
        revision = args.code_revision or _current_code_revision()
        report = build_mark_price_coverage_report(
            tuple(args.symbols), start_ms, end_ms, args.archive_root,
            code_revision=revision, download=args.download,
        )
        content = mark_price_coverage_report_to_json(report)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content + "\n", encoding="utf-8")
    except (ValueError, TypeError, ArithmeticError, OSError,
            subprocess.CalledProcessError, urllib.error.URLError) as exc:
        print(f"mark-price coverage audit: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
