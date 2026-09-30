"""Separate verified Binance USD-M mark-price archive evidence for #127.

Mark-price packages remain outside the core trade-price replay dataset. Archive
close time plus one millisecond is a deterministic completion surrogate, not a
measurement of historical network receipt.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import tempfile
from types import MappingProxyType
from typing import Mapping
import urllib.request
import zipfile

from .binance_historical_archive import (
    BinanceArchiveCoverageError, _KLINE_HEADER, _SHA256_LINE, _checksum,
    _is_header,
)
from .movement_history import MINUTE_MS


MARK_PRICE_EVIDENCE_VERSION = "binance-usdm-mark-price-evidence-v1"
MARK_PRICE_EVIDENCE_SCHEMA_VERSION = "binance-usdm-mark-price-evidence-schema-v1"
MARK_PRICE_SOURCE = "binance-public-data-usdm-markPriceKlines"
MARK_PRICE_TYPE = "mark"
MARK_PRICE_INTERVAL = "1m"
MARK_PRICE_AVAILABILITY_BASIS = "exchange-close-time-plus-one-millisecond-surrogate-v1"
MARK_PRICE_SCHEMA_DESCRIPTION = "binance-mark-price-kline-12-column-v1"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ARCHIVE_ROOT_URL = "https://data.binance.vision/"


def daily_mark_price_relative_path(symbol: str, utc_date: date) -> PurePosixPath:
    if (not isinstance(symbol, str) or not symbol or symbol != symbol.upper()
            or not symbol.isascii() or not symbol.isalnum() or "_" in symbol):
        raise ValueError("mark-price archive requires uppercase USD-M symbols")
    if type(utc_date) is not date:
        raise ValueError("mark-price archive date must be datetime.date")
    return PurePosixPath(
        "data", "futures", "um", "daily", "markPriceKlines", symbol, "1m",
        f"{symbol}-1m-{utc_date.isoformat()}.zip",
    )


def _decimal(value: str, name: str) -> Decimal:
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal price") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return result


def _integer(value: str, name: str) -> int:
    if not isinstance(value, str) or re.fullmatch(r"\d+", value.strip()) is None:
        raise ValueError(f"{name} must be an unsigned integer timestamp")
    return int(value)


def _utc_date(timestamp_ms: int) -> date:
    return (_EPOCH + timedelta(milliseconds=timestamp_ms)).date()


def _timestamp_candidate(row: list[str]) -> int | None:
    if not row:
        return None
    try:
        return _integer(row[0], "open_time")
    except ValueError:
        return None


def _parse_mark_price_row(row: list[str], archive_day: date):
    if len(row) != 12:
        raise ValueError("markPriceKlines row must have twelve columns")
    opening = _integer(row[0], "open_time")
    open_price = _decimal(row[1], "open")
    high = _decimal(row[2], "high")
    low = _decimal(row[3], "low")
    close = _decimal(row[4], "close")
    closing = _integer(row[6], "close_time")
    if _utc_date(opening) != archive_day:
        raise ValueError("open_time is outside the archive UTC date")
    if closing != opening + MINUTE_MS - 1:
        raise ValueError("mark-price row is not one complete minute")
    if high < max(open_price, close) or low > min(open_price, close):
        raise ValueError("mark-price high/low are inconsistent with open/close")
    return opening, closing, open_price, high, low, close


@dataclass(frozen=True)
class CompletedMarkPriceCandle:
    symbol: str
    open_time_ms: int
    close_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    first_seen_at_ms: int
    interval: str = MARK_PRICE_INTERVAL
    source: str = MARK_PRICE_SOURCE
    price_type: str = MARK_PRICE_TYPE
    availability_basis: str = MARK_PRICE_AVAILABILITY_BASIS

    def __post_init__(self):
        if (not isinstance(self.symbol, str) or not self.symbol
                or self.symbol != self.symbol.upper() or not self.symbol.isascii()
                or not self.symbol.isalnum()):
            raise ValueError("mark candle requires a canonical symbol")
        if (type(self.open_time_ms) is not int or self.open_time_ms < 0
                or self.open_time_ms % MINUTE_MS
                or type(self.close_time_ms) is not int
                or self.close_time_ms != self.open_time_ms + MINUTE_MS - 1
                or type(self.first_seen_at_ms) is not int
                or self.first_seen_at_ms != self.close_time_ms + 1):
            raise ValueError("mark candle must be an aligned, completed 1m interval")
        if ((self.interval, self.source, self.price_type, self.availability_basis)
                != (MARK_PRICE_INTERVAL, MARK_PRICE_SOURCE, MARK_PRICE_TYPE,
                    MARK_PRICE_AVAILABILITY_BASIS)):
            raise ValueError("mark candle provenance or availability is invalid")
        for name in ("open", "high", "low", "close"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"mark candle {name} must be a finite positive Decimal")
        if (self.high < max(self.open, self.close)
                or self.low > min(self.open, self.close)):
            raise ValueError("mark candle high/low are inconsistent")


@dataclass(frozen=True)
class MarkMinuteRange:
    start_open_time_ms: int
    end_open_time_ms: int
    minute_count: int


@dataclass(frozen=True)
class MarkPricePackageEvidence:
    symbol: str
    utc_date: date
    relative_archive_path: str
    relative_checksum_path: str
    archive_present: bool
    checksum_present: bool
    checksum_verified: bool
    archive_sha256: str | None
    status: str
    statuses: tuple[str, ...]
    archive_row_count: int
    valid_row_count: int
    malformed_rows: tuple[str, ...]
    duplicate_timestamps_ms: tuple[int, ...]
    off_grid_timestamps_ms: tuple[int, ...]
    missing_minute_ranges: tuple[MarkMinuteRange, ...]
    acquisition_error: str | None = None


def _ranges(timestamps: list[int]) -> tuple[MarkMinuteRange, ...]:
    if not timestamps:
        return ()
    result = []
    start = prior = timestamps[0]
    for current in timestamps[1:]:
        if current != prior + MINUTE_MS:
            result.append(MarkMinuteRange(start, prior,
                                          (prior - start) // MINUTE_MS + 1))
            start = current
        prior = current
    result.append(MarkMinuteRange(start, prior,
                                  (prior - start) // MINUTE_MS + 1))
    return tuple(result)


def _package_status_reason(status: str) -> str:
    return {
        "MISSING_PACKAGE": "MARK_PACKAGE_MISSING",
        "MISSING_CHECKSUM": "MARK_CHECKSUM_MISSING",
        "INVALID_CHECKSUM": "MARK_CHECKSUM_INVALID",
        "CHECKSUM_MISMATCH": "MARK_CHECKSUM_MISMATCH",
        "INVALID_ARCHIVE": "MARK_ARCHIVE_INVALID",
    }.get(status, "MARK_MISSING_MINUTE")


@dataclass(frozen=True)
class BinanceMarkPriceEvidence:
    evidence_version: str
    schema_version: str
    configured_symbols: tuple[str, ...]
    requested_start_boundary_time_ms: int
    requested_end_boundary_time_ms: int
    first_output_minute_boundary_time_ms: int
    last_output_minute_boundary_time_ms: int
    warmup_minutes: int
    expected_open_time_start_ms: int
    expected_open_time_end_ms_exclusive: int
    packages: tuple[MarkPricePackageEvidence, ...]
    candles: tuple[CompletedMarkPriceCandle, ...]
    row_issue_by_symbol_open: tuple[tuple[str, int, str], ...]
    evidence_sha256: str = field(init=False)
    _candle_by_symbol_open: Mapping[tuple[str, int], CompletedMarkPriceCandle] = field(
        init=False, repr=False, compare=False)
    _package_by_symbol_date: Mapping[tuple[str, date], MarkPricePackageEvidence] = field(
        init=False, repr=False, compare=False)
    _row_issues: Mapping[tuple[str, int], str] = field(
        init=False, repr=False, compare=False)

    def __post_init__(self):
        symbols = tuple(self.configured_symbols)
        if (self.evidence_version != MARK_PRICE_EVIDENCE_VERSION
                or self.schema_version != MARK_PRICE_EVIDENCE_SCHEMA_VERSION
                or not symbols or len(set(symbols)) != len(symbols)):
            raise ValueError("mark evidence requires its version and ordered symbol identity")
        if (self.warmup_minutes != 16
                or self.expected_open_time_start_ms % MINUTE_MS
                or self.expected_open_time_end_ms_exclusive % MINUTE_MS
                or self.expected_open_time_end_ms_exclusive
                <= self.expected_open_time_start_ms):
            raise ValueError("mark evidence requires the exact 16-minute warm-up interval")
        candle_index = {}
        for candle in self.candles:
            if candle.symbol not in symbols:
                raise ValueError("mark candle is outside the configured symbol list")
            key = (candle.symbol, candle.open_time_ms)
            if key in candle_index:
                raise ValueError(f"duplicate usable mark candle {key}")
            candle_index[key] = candle
        package_index = {(item.symbol, item.utc_date): item for item in self.packages}
        if len(package_index) != len(self.packages):
            raise ValueError("mark evidence contains duplicate daily packages")
        issues = {(symbol, opening): reason
                  for symbol, opening, reason in self.row_issue_by_symbol_open}
        payload = {
            "evidence_version": self.evidence_version,
            "schema_version": self.schema_version,
            "source": MARK_PRICE_SOURCE,
            "price_type": MARK_PRICE_TYPE,
            "interval": MARK_PRICE_INTERVAL,
            "availability_basis": MARK_PRICE_AVAILABILITY_BASIS,
            "ordered_symbols": symbols,
            "requested_start_boundary_time_ms": self.requested_start_boundary_time_ms,
            "requested_end_boundary_time_ms": self.requested_end_boundary_time_ms,
            "first_output_minute_boundary_time_ms": self.first_output_minute_boundary_time_ms,
            "last_output_minute_boundary_time_ms": self.last_output_minute_boundary_time_ms,
            "warmup_minutes": self.warmup_minutes,
            "expected_open_time_start_ms": self.expected_open_time_start_ms,
            "expected_open_time_end_ms_exclusive": self.expected_open_time_end_ms_exclusive,
            "packages": self.packages,
            "candles": tuple(sorted(candle_index.values(),
                                     key=lambda item: (symbols.index(item.symbol),
                                                       item.open_time_ms))),
            "row_issues": tuple(sorted(issues.items())),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=True, default=_canonical_value).encode("ascii")
        digest = hashlib.sha256(canonical).hexdigest()
        object.__setattr__(self, "configured_symbols", symbols)
        object.__setattr__(self, "packages", tuple(self.packages))
        object.__setattr__(self, "candles", tuple(candle_index.values()))
        object.__setattr__(self, "_candle_by_symbol_open",
                           MappingProxyType(candle_index))
        object.__setattr__(self, "_package_by_symbol_date",
                           MappingProxyType(package_index))
        object.__setattr__(self, "_row_issues", MappingProxyType(issues))
        object.__setattr__(self, "evidence_sha256", digest)

    @property
    def expected_minute_count(self) -> int:
        return ((self.expected_open_time_end_ms_exclusive
                 - self.expected_open_time_start_ms) // MINUTE_MS)

    def candle(self, symbol: str, open_time_ms: int) -> CompletedMarkPriceCandle | None:
        return self._candle_by_symbol_open.get((symbol, open_time_ms))

    def unavailable_reason(self, symbol: str, open_time_ms: int) -> str | None:
        if (symbol, open_time_ms) in self._candle_by_symbol_open:
            return None
        issue = self._row_issues.get((symbol, open_time_ms))
        if issue is not None:
            return issue
        package = self._package_by_symbol_date.get((symbol, _utc_date(open_time_ms)))
        if package is None:
            return "MARK_PACKAGE_MISSING"
        if not package.checksum_verified:
            return _package_status_reason(package.status)
        return "MARK_MISSING_MINUTE"

    def coverage_by_symbol(self) -> tuple[dict, ...]:
        result = []
        for symbol in self.configured_symbols:
            expected = range(self.expected_open_time_start_ms,
                             self.expected_open_time_end_ms_exclusive, MINUTE_MS)
            present = [opening for opening in expected
                       if (symbol, opening) in self._candle_by_symbol_open]
            missing = [opening for opening in expected
                       if (symbol, opening) not in self._candle_by_symbol_open]
            result.append({
                "symbol": symbol,
                "expected_minute_count": self.expected_minute_count,
                "observed_unique_valid_minute_count": len(present),
                "coverage_ratio": (len(present) / self.expected_minute_count),
                "missing_minute_count": len(missing),
                "missing_minute_ranges": _ranges(missing),
                "longest_contiguous_gap_minutes": max(
                    (item.minute_count for item in _ranges(missing)), default=0),
                "expected_package_count": sum(item.symbol == symbol
                                                for item in self.packages),
                "checksum_verified_package_count": sum(
                    item.symbol == symbol and item.checksum_verified
                    for item in self.packages),
            })
        return tuple(result)


def _canonical_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return {name: getattr(value, name) for name in value.__dataclass_fields__}
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"unsupported mark evidence value {type(value).__name__}")


def _download_mark_archive(root: Path, relative: PurePosixPath) -> None:
    archive = root.joinpath(*relative.parts)
    checksum_path = Path(f"{archive}.CHECKSUM")
    checksum_relative = PurePosixPath(f"{relative.as_posix()}.CHECKSUM")
    archive.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(_ARCHIVE_ROOT_URL + checksum_relative.as_posix(),
                                timeout=30) as response:
        lines = [line.strip() for line in response.read(4096).decode("utf-8").splitlines()
                 if line.strip()]
    if len(lines) != 1:
        raise ValueError("official mark-price checksum must contain one entry")
    match = _SHA256_LINE.fullmatch(lines[0])
    if match is None or match.group(2) != relative.name:
        raise ValueError("official mark-price checksum has an unexpected filename")
    expected = match.group(1).lower()
    digest = hashlib.sha256()
    with tempfile.NamedTemporaryFile(dir=archive.parent, prefix=f".{archive.name}.",
                                     delete=False) as stream:
        temporary = Path(stream.name)
        try:
            with urllib.request.urlopen(_ARCHIVE_ROOT_URL + relative.as_posix(),
                                        timeout=60) as response:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    stream.write(chunk)
                    digest.update(chunk)
            stream.flush()
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        if digest.hexdigest() != expected:
            raise ValueError(f"downloaded mark-price checksum mismatch for {relative}")
        temporary_checksum = temporary.with_name(temporary.name + ".CHECKSUM")
        temporary_checksum.write_text(f"{expected}  {relative.name}\n", encoding="ascii")
        temporary.replace(archive)
        temporary_checksum.replace(checksum_path)
    finally:
        temporary.unlink(missing_ok=True)
        temporary.with_name(temporary.name + ".CHECKSUM").unlink(missing_ok=True)


def _parse_package(root: Path, symbol: str, utc_day: date,
                   expected_opens: range, *, download: bool):
    relative = daily_mark_price_relative_path(symbol, utc_day)
    archive = root.joinpath(*relative.parts)
    checksum_path = Path(f"{archive}.CHECKSUM")
    acquisition_error = None
    status = None
    if (not archive.is_file() or not checksum_path.is_file()) and download:
        try:
            _download_mark_archive(root, relative)
        except Exception as exc:
            acquisition_error = f"{type(exc).__name__}: {exc}"
    archive_present, checksum_present = archive.is_file(), checksum_path.is_file()
    if not archive_present:
        status = "MISSING_PACKAGE"
    elif not checksum_present:
        status = "MISSING_CHECKSUM"
    else:
        try:
            digest = _checksum(root, relative, symbol, "markPriceKlines/1m", utc_day)
        except BinanceArchiveCoverageError:
            status = "MISSING_PACKAGE" if not archive.is_file() else "MISSING_CHECKSUM"
            digest = None
        except ValueError as exc:
            error = str(exc)
            if download and ("checksum mismatch" in error
                             or "checksum" in error
                             or "cannot read archive" in error):
                try:
                    _download_mark_archive(root, relative)
                    digest = _checksum(root, relative, symbol,
                                       "markPriceKlines/1m", utc_day)
                except Exception as download_exc:
                    acquisition_error = f"{type(download_exc).__name__}: {download_exc}"
                    status = ("CHECKSUM_MISMATCH" if "checksum mismatch" in error
                              else "INVALID_CHECKSUM" if "checksum" in error
                              else "INVALID_ARCHIVE")
                    digest = None
            elif "checksum mismatch" in error:
                status = "CHECKSUM_MISMATCH"
                digest = None
            elif "checksum" in error:
                status = "INVALID_CHECKSUM"
                digest = None
            else:
                status = "INVALID_ARCHIVE"
                digest = None
        else:
            pass
        if status is None:
            return _parse_verified_zip(root, relative, symbol, utc_day,
                                       digest, expected_opens)
    expected_list = list(expected_opens)
    package = MarkPricePackageEvidence(
        symbol, utc_day, relative.as_posix(), f"{relative.as_posix()}.CHECKSUM",
        archive_present, checksum_present, False, None, status, (status,),
        0, 0, (), (), (), _ranges(expected_list), acquisition_error,
    )
    return package, (), ()


def _parse_verified_zip(root: Path, relative: PurePosixPath, symbol: str,
                        utc_day: date, digest: str, expected_opens: range):
    archive = root.joinpath(*relative.parts)
    valid = {}
    row_issues = {}
    seen = set()
    malformed = []
    duplicates = []
    off_grid = []
    row_count = valid_count = 0
    archive_parse_error = None
    try:
        with zipfile.ZipFile(archive) as bundle:
            csv_members = []
            for member in bundle.infolist():
                name = member.filename.replace("\\", "/")
                member_path = PurePosixPath(name)
                if (member.flag_bits & 1 or member_path.is_absolute()
                        or ".." in member_path.parts
                        or PureWindowsPath(member.filename).drive):
                    raise ValueError(f"unsafe ZIP member: {member.filename}")
                if not member.is_dir() and member_path.suffix.lower() == ".csv":
                    csv_members.append(member)
            expected_name = f"{relative.stem}.csv"
            if (len(csv_members) != 1
                    or PurePosixPath(csv_members[0].filename).name != expected_name):
                raise ValueError(f"archive must contain exactly one {expected_name} CSV")
            with bundle.open(csv_members[0]) as raw:
                with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as stream:
                    reader = csv.reader(stream, strict=True)
                    try:
                        for line_number, row in enumerate(reader, start=1):
                            if line_number == 1 and _is_header(row, _KLINE_HEADER):
                                continue
                            row_count += 1
                            candidate = _timestamp_candidate(row)
                            if candidate is not None and candidate in seen:
                                duplicates.append(candidate)
                                row_issues[candidate] = "MARK_DUPLICATE_MINUTE"
                                valid.pop(candidate, None)
                                continue
                            if candidate is not None:
                                seen.add(candidate)
                            try:
                                opening, closing, opened, high, low, close = (
                                    _parse_mark_price_row(row, utc_day))
                            except (ValueError, ArithmeticError, OverflowError) as exc:
                                malformed.append(f"CSV row {line_number}: {exc}")
                                if candidate is not None:
                                    row_issues[candidate] = "MARK_INVALID_ROW"
                                continue
                            if opening % MINUTE_MS:
                                off_grid.append(opening)
                                row_issues[opening] = "MARK_OFF_GRID_MINUTE"
                                continue
                            candle = CompletedMarkPriceCandle(
                                symbol, opening, closing, opened, high, low, close,
                                closing + 1)
                            valid_count += 1
                            if opening in expected_opens:
                                valid[opening] = candle
                    except (csv.Error, UnicodeError) as exc:
                        archive_parse_error = f"CSV row {reader.line_num}: {exc}"
    except (OSError, zipfile.BadZipFile, RuntimeError, UnicodeError, ValueError) as exc:
        archive_parse_error = str(exc)
    if archive_parse_error is not None:
        malformed.append(f"archive parse error: {archive_parse_error}")
        valid.clear()
        for opening in expected_opens:
            row_issues.setdefault(opening, "MARK_ARCHIVE_INVALID")
    missing = [opening for opening in expected_opens if opening not in valid]
    statuses = []
    if malformed:
        statuses.append("MALFORMED_ROW")
    if duplicates:
        statuses.append("DUPLICATE_MINUTE")
    if off_grid:
        statuses.append("OFF_GRID_MINUTE")
    if missing:
        statuses.append("MISSING_VALID_MINUTE")
    if not statuses:
        statuses.append("VERIFIED_COMPLETE")
    package = MarkPricePackageEvidence(
        symbol, utc_day, relative.as_posix(), f"{relative.as_posix()}.CHECKSUM",
        True, True, True, digest,
        statuses[0], tuple(statuses), row_count, valid_count,
        tuple(malformed), tuple(duplicates), tuple(off_grid), _ranges(missing), None,
    )
    return package, tuple(valid.values()), tuple(
        (symbol, opening, reason) for opening, reason in row_issues.items()
        if opening in expected_opens)


def _package_days(start_open_ms: int, end_open_exclusive_ms: int) -> tuple[date, ...]:
    first = _utc_date(start_open_ms)
    last = _utc_date(end_open_exclusive_ms - MINUTE_MS)
    return tuple(first + timedelta(days=offset)
                 for offset in range((last - first).days + 1))


def load_binance_usdm_mark_price_evidence(
    archive_root: Path,
    configured_symbols: tuple[str, ...],
    requested_start_boundary_time_ms: int,
    requested_end_boundary_time_ms: int,
    *, download: bool = False,
) -> BinanceMarkPriceEvidence:
    """Load each relevant mark package once; network is disabled unless opted in."""
    symbols = tuple(configured_symbols)
    if (not symbols or any(not isinstance(symbol, str) for symbol in symbols)
            or len(set(symbols)) != len(symbols)):
        raise ValueError("mark evidence requires a nonempty ordered symbol list")
    for symbol in symbols:
        daily_mark_price_relative_path(symbol, date(1970, 1, 1))
    start, end = requested_start_boundary_time_ms, requested_end_boundary_time_ms
    if (type(start) is not int or type(end) is not int or start < 0 or end < start
            or start % 5_000 or end % 5_000):
        raise ValueError("mark evidence range must be ordered and five-second aligned")
    if type(download) is not bool:
        raise ValueError("download must be a boolean opt-in")
    first_output = ((start + MINUTE_MS - 1) // MINUTE_MS) * MINUTE_MS
    last_output = (end // MINUTE_MS) * MINUTE_MS
    if first_output > last_output or first_output > end:
        raise ValueError("requested interval contains no exact minute-boundary replay point")
    warmup_start = first_output - 16 * MINUTE_MS
    if warmup_start < 0:
        raise ValueError("mark-price 16-minute warm-up precedes the Unix epoch")
    expected_end = last_output
    root = Path(archive_root).expanduser().resolve()
    package_days = _package_days(warmup_start, expected_end)
    expected_by_day = {}
    for utc_day in package_days:
        day_start = int((datetime(utc_day.year, utc_day.month, utc_day.day,
                                  tzinfo=timezone.utc) - _EPOCH)
                        // timedelta(milliseconds=1))
        expected_by_day[utc_day] = range(
            max(warmup_start, day_start),
            min(expected_end, day_start + 24 * 60 * MINUTE_MS), MINUTE_MS)

    packages = []
    candles = []
    issues = []
    for symbol in symbols:
        for utc_day in package_days:
            package, day_candles, day_issues = _parse_package(
                root, symbol, utc_day, expected_by_day[utc_day], download=download)
            packages.append(package)
            candles.extend(day_candles)
            issues.extend(day_issues)
    return BinanceMarkPriceEvidence(
        MARK_PRICE_EVIDENCE_VERSION, MARK_PRICE_EVIDENCE_SCHEMA_VERSION,
        symbols, start, end, first_output, last_output, 16,
        warmup_start, expected_end, tuple(packages), tuple(candles), tuple(issues),
    )
