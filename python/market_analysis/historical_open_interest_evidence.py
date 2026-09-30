"""Separate open-interest metrics archive evidence for EXP-75-11; network acquisition is opt-in."""
from __future__ import annotations

from bisect import bisect_right
import csv
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import io
from pathlib import Path, PurePosixPath, PureWindowsPath
import time
from types import MappingProxyType
import zipfile

from .binance_historical_archive import _checksum
from .binance_historical_download import (
    BinanceArchiveDownloadItem, _acquire_item, _UrllibTransport,
)
from .historical_experiment_batch import _sha256

EVIDENCE_VERSION = "binance-usdm-open-interest-metrics-evidence-v1"
SCHEMA_VERSION = "binance-usdm-metrics-named-schema-v1"
SOURCE = "binance-public-data-usdm-metrics"
AVAILABILITY_BASIS = "create-time-plus-five-minutes-conservative-surrogate-v1"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
OI_CADENCE_MS = 300_000
TIMESTAMP_FIELD = "create_time"
REQUIRED_FIELDS = {"create_time", "symbol", "sum_open_interest"}
ALLOWED_FIELDS = REQUIRED_FIELDS | {"sum_open_interest_value", "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio", "count_long_short_ratio", "sum_taker_long_short_vol_ratio"}


def _time_ms(value: str) -> int:
    if not isinstance(value, str):
        raise ValueError("missing source timestamp")
    if value.strip().isascii() and value.strip().isdigit():
        return int(value)
    text = value.strip().removesuffix("Z").replace("T", " ")
    parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return (parsed - _EPOCH) // timedelta(milliseconds=1)


def _number(value: str, *, positive=False) -> Decimal:
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid decimal") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError("price/quantity must be finite and positive" if positive else "nonfinite number")
    return result


def _day(timestamp_ms: int) -> date:
    return (_EPOCH + timedelta(milliseconds=timestamp_ms)).date()


def _validate_request(symbols, start, end, download):
    symbols = tuple(symbols)
    if (not symbols or any(not isinstance(s, str) or not s or not s.isascii()
                           or not s.isalnum() or s != s.upper() for s in symbols)
            or len(set(symbols)) != len(symbols)):
        raise ValueError("source requires a unique ordered canonical universe")
    if (type(start) is not int or type(end) is not int or start < 0 or end < start
            or start % 5000 or end % 5000 or type(download) is not bool):
        raise ValueError("source range must be ordered and five-second aligned; download is boolean")
    return symbols


@dataclass(frozen=True)
class OpenInterestPackage:
    symbol: str
    relative_path: str
    checksum_relative_path: str
    archive_present: bool
    checksum_present: bool
    checksum_verified: bool
    sha256: str | None
    status: str
    row_count: int = 0
    malformed_rows: tuple[str, ...] = ()
    duplicate_timestamps_ms: tuple[int, ...] = ()
    off_grid_timestamps_ms: tuple[int, ...] = ()
    missing_ranges: tuple[tuple[int, int, int], ...] = ()
    error: str | None = None


def _load_package(root, symbol, day, relative, download):
    archive = root.joinpath(*relative.parts)
    checksum = Path(str(archive) + ".CHECKSUM")
    acquisition_error = None
    if download:
        try:
            item = BinanceArchiveDownloadItem(relative, PurePosixPath(str(relative) + ".CHECKSUM"),
                                               symbol, SOURCE, day)
            # Reuse the existing checksum/atomic-acquisition mechanics, without changing core planning.
            _acquire_item(type("SourceRoot", (), {"archive_root": root})(), item,
                          _UrllibTransport(), time.sleep)
        except (ValueError, OSError) as exc:
            acquisition_error = type(exc).__name__ + ": " + str(exc)
    present, checksum_present = archive.is_file(), checksum.is_file()
    status = None
    digest = None
    if not present:
        status = "MISSING_PACKAGE"
    elif not checksum_present:
        status = "MISSING_CHECKSUM"
    else:
        try:
            digest = _checksum(root, relative, symbol, SOURCE, day)
        except ValueError as exc:
            status = ("CHECKSUM_MISMATCH" if "checksum mismatch" in str(exc)
                      else "INVALID_CHECKSUM" if "checksum" in str(exc) else "INVALID_ARCHIVE")
    package = OpenInterestPackage(symbol, str(relative), str(relative) + ".CHECKSUM",
                              present, checksum_present, status is None, digest,
                              status or "VERIFIED", error=acquisition_error)
    if status is not None:
        return package, (), ()
    rows, issues, malformed, duplicates, off_grid = {}, {}, [], [], []
    count = 0
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = []
            for member in bundle.infolist():
                path = PurePosixPath(member.filename.replace("\\", "/"))
                if (member.flag_bits & 1 or path.is_absolute() or ".." in path.parts
                        or PureWindowsPath(member.filename).drive):
                    raise ValueError("unsafe ZIP member")
                if not member.is_dir() and path.suffix.lower() == ".csv":
                    members.append(member)
            if len(members) != 1 or PurePosixPath(members[0].filename).name != relative.stem + ".csv":
                raise ValueError("archive must contain its single expected CSV")
            with bundle.open(members[0]) as stream:
                with io.TextIOWrapper(stream, encoding="utf-8-sig", newline="") as text:
                    reader = csv.DictReader(text, strict=True)
                    header = tuple(name.strip().lower() for name in (reader.fieldnames or ()))
                    if (len(set(header)) != len(header) or not REQUIRED_FIELDS <= set(header)
                            or not set(header) <= ALLOWED_FIELDS):
                        return replace(package, status="SCHEMA_MISMATCH"), (), ()
                    reader.fieldnames = list(header)
                    seen = set()
                    for line, row in enumerate(reader, 2):
                        count += 1
                        timestamp = None
                        try:
                            timestamp = _time_ms(row[TIMESTAMP_FIELD])
                            if timestamp in seen:
                                duplicates.append(timestamp)
                                issues[timestamp] = "DUPLICATE_SOURCE_TIMESTAMP"
                                rows.pop(timestamp, None)
                                continue
                            seen.add(timestamp)
                            if None in row or any(v is None for v in row.values()):
                                raise ValueError("row does not match named schema")
                            observation = _parse_row(row, symbol, day, str(relative), digest)
                            if timestamp % OI_CADENCE_MS:
                                off_grid.append(timestamp)
                                issues[timestamp] = "OFF_GRID_TIMESTAMP"
                                continue
                            rows[timestamp] = observation
                        except (ValueError, TypeError, ArithmeticError, OverflowError) as exc:
                            malformed.append(f"row {line}: {exc}")
                            if timestamp is not None:
                                issues[timestamp] = "MALFORMED_ROW"
    except (OSError, EOFError, zipfile.BadZipFile, RuntimeError, UnicodeError, csv.Error, ValueError) as exc:
        return replace(package, status="INVALID_ARCHIVE", row_count=count,
                       error=type(exc).__name__), (), ()
    status = ("MALFORMED_ROW" if malformed else "DUPLICATE_SOURCE_TIMESTAMP" if duplicates
              else "OFF_GRID_TIMESTAMP" if off_grid else "VERIFIED")
    return replace(package, status=status, row_count=count, malformed_rows=tuple(malformed),
                   duplicate_timestamps_ms=tuple(duplicates),
                   off_grid_timestamps_ms=tuple(off_grid)), tuple(rows.values()), tuple(issues.items())


@dataclass(frozen=True)
class OpenInterestObservation:
    symbol: str
    source_time_ms: int
    quantity: Decimal
    quote_value: Decimal | None
    available_at_ms: int
    package_relative_path: str
    package_sha256: str

    def __post_init__(self):
        if (type(self.source_time_ms) is not int or self.source_time_ms < 0
                or self.source_time_ms % OI_CADENCE_MS
                or self.available_at_ms != self.source_time_ms + OI_CADENCE_MS
                or not isinstance(self.quantity, Decimal) or not self.quantity.is_finite()
                or self.quantity <= 0):
            raise ValueError("invalid aligned positive OI observation")
        if self.quote_value is not None and (not isinstance(self.quote_value, Decimal)
                or not self.quote_value.is_finite() or self.quote_value < 0):
            raise ValueError("invalid quote OI value")


def daily_open_interest_relative_path(symbol: str, day: date):
    return PurePosixPath("data", "futures", "um", "daily", "metrics", symbol,
                         f"{symbol}-metrics-{day.isoformat()}.zip")


def _parse_row(row, symbol, day, path, digest):
    timestamp = _time_ms(row["create_time"])
    if row["symbol"].strip() != symbol or _day(timestamp) != day:
        raise ValueError("row symbol/day differs from package")
    quantity = _number(row["sum_open_interest"], positive=True)
    quote = (_number(row["sum_open_interest_value"]) if "sum_open_interest_value" in row
             else None)
    if quote is not None and quote < 0:
        raise ValueError("negative quote OI value")
    # Off-grid rows are reported by the package parser before construction.
    if timestamp % OI_CADENCE_MS:
        return None
    return OpenInterestObservation(symbol, timestamp, quantity, quote,
                                   timestamp + OI_CADENCE_MS, path, digest)


def _gap_ranges(missing):
    ranges = []
    for timestamp in missing:
        if ranges and timestamp == ranges[-1][1] + OI_CADENCE_MS:
            start, _, count = ranges[-1]
            ranges[-1] = (start, timestamp, count + 1)
        else:
            ranges.append((timestamp, timestamp, 1))
    return tuple(ranges)



@dataclass(frozen=True)
class BinanceOpenInterestEvidence:
    configured_symbols: tuple[str, ...]
    requested_start_boundary_time_ms: int
    requested_end_boundary_time_ms: int
    history_start_time_ms: int
    packages: tuple[OpenInterestPackage, ...]
    rows: tuple[OpenInterestObservation, ...]
    row_issues: tuple[tuple[str, int, str], ...] = ()
    evidence_sha256: str = field(init=False)
    _index: object = field(init=False, repr=False, compare=False)
    _times: object = field(init=False, repr=False, compare=False)
    _issues: object = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        symbols = _validate_request(self.configured_symbols,
                                    self.requested_start_boundary_time_ms,
                                    self.requested_end_boundary_time_ms, False)
        if any(not isinstance(r, OpenInterestObservation) or r.symbol not in symbols for r in self.rows):
            raise ValueError("invalid source observation")
        if any(not isinstance(p, OpenInterestPackage) or p.symbol not in symbols for p in self.packages):
            raise ValueError("invalid source package")
        identities = {(p.symbol, p.relative_path): p for p in self.packages}
        if len(identities) != len(self.packages):
            raise ValueError("duplicate package identity")
        for row in self.rows:
            package = identities.get((row.symbol, row.package_relative_path))
            if (package is None or not package.checksum_verified
                    or package.sha256 != row.package_sha256
                    or package.status in ("SCHEMA_MISMATCH", "INVALID_ARCHIVE")):
                raise ValueError("usable row must bind a verified package")
        if type(self.history_start_time_ms) is not int or self.history_start_time_ms < 0:
            raise ValueError("invalid source prehistory")
        if self.history_start_time_ms > self.requested_start_boundary_time_ms - 1_200_000:
            raise ValueError("OI requires at least 20m prehistory")
        rows = tuple(sorted(self.rows, key=lambda r: (symbols.index(r.symbol), r.source_time_ms)))
        if any(not isinstance(r, OpenInterestObservation) or r.symbol not in symbols for r in rows):
            raise ValueError("invalid source observation")
        index = {(r.symbol, r.source_time_ms): r for r in rows}
        if len(index) != len(rows):
            raise ValueError("duplicate usable source timestamp")
        object.__setattr__(self, "configured_symbols", symbols)
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "packages", tuple(self.packages))
        object.__setattr__(self, "row_issues", tuple(self.row_issues))
        object.__setattr__(self, "_index", MappingProxyType(index))
        object.__setattr__(self, "_times", MappingProxyType({
            s: tuple(r.source_time_ms for r in rows if r.symbol == s) for s in symbols}))
        object.__setattr__(self, "_issues", MappingProxyType({
            (s, t): reason for s, t, reason in self.row_issues}))
        object.__setattr__(self, "evidence_sha256", _sha256({
            "version": EVIDENCE_VERSION, "schema": SCHEMA_VERSION, "source": SOURCE,
            "availability": AVAILABILITY_BASIS, "policy": "exact endpoints; no carry across missing nominal 5m slots; 20m prehistory",
            "symbols": symbols, "start": self.requested_start_boundary_time_ms,
            "end": self.requested_end_boundary_time_ms, "history_start": self.history_start_time_ms,
            "packages": self.packages, "rows": rows, "issues": self.row_issues}))

    def as_of(self, symbol: str, boundary: int):
        timestamps = self._times.get(symbol, ())
        offset = bisect_right(timestamps, boundary - OI_CADENCE_MS) - 1
        return self._index[(symbol, timestamps[offset])] if offset >= 0 else None

    def has_pending_observation(self, symbol: str, boundary: int):
        timestamps = self._times.get(symbol, ())
        position = bisect_right(timestamps, boundary - 300000)
        return position < len(timestamps) and timestamps[position] <= boundary

    def exact(self, symbol: str, source_time_ms: int):
        return self._index.get((symbol, source_time_ms))

    def reason_at(self, symbol: str, source_time_ms: int):
        issue = self._issues.get((symbol, source_time_ms))
        if issue:
            return issue
        relative = daily_open_interest_relative_path(symbol, _day(source_time_ms))
        package = next((p for p in self.packages if p.symbol == symbol
                        and p.relative_path == str(relative)), None)
        if package is None:
            return "SOURCE_EVIDENCE_UNAVAILABLE"
        if not package.checksum_verified or package.status in ("SCHEMA_MISMATCH", "INVALID_ARCHIVE"):
            return package.status
        return "MISSING_EXACT_OI_ENDPOINT"


def load_binance_usdm_open_interest_evidence(archive_root, configured_symbols,
                                            start: int, end: int, *, download=False):
    symbols = _validate_request(configured_symbols, start, end, download)
    history_start = (start - 20 * 60_000) // OI_CADENCE_MS * OI_CADENCE_MS
    if history_start < 0:
        raise ValueError("OI warm-up precedes epoch")
    root = Path(archive_root).expanduser().resolve()
    first, last = _day(history_start), _day(end)
    packages, observations, issues = [], {}, {}
    for symbol in symbols:
        for offset in range((last - first).days + 1):
            day = first + timedelta(days=offset)
            relative = daily_open_interest_relative_path(symbol, day)
            package, rows, row_issues = _load_package(root, symbol, day, relative, download)
            for timestamp, reason in row_issues:
                issues[(symbol, timestamp)] = reason
            for row in rows:
                key = (symbol, row.source_time_ms)
                if key in observations or issues.get(key) == "DUPLICATE_SOURCE_TIMESTAMP":
                    observations.pop(key, None)
                    issues[key] = "DUPLICATE_SOURCE_TIMESTAMP"
                else:
                    observations[key] = row
            day_start = (datetime(day.year, day.month, day.day, tzinfo=timezone.utc) - _EPOCH) // timedelta(milliseconds=1)
            expected = range(max(history_start, day_start), min(end + 1, day_start + 86_400_000), OI_CADENCE_MS)
            missing = [t for t in expected if (symbol, t) not in observations]
            packages.append(replace(package, missing_ranges=_gap_ranges(missing),
                                    status="MISSING_5M_OBSERVATIONS" if missing and package.status == "VERIFIED" else package.status))
    return BinanceOpenInterestEvidence(symbols, start, end, history_start,
                                        tuple(packages), tuple(observations.values()),
                                        tuple((s, t, r) for (s, t), r in sorted(issues.items())))
