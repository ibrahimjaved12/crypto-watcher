"""Separate settled funding archive evidence for EXP-75-11; network acquisition is opt-in."""
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

EVIDENCE_VERSION = "binance-usdm-settled-funding-evidence-v1"
SCHEMA_VERSION = "binance-usdm-funding-named-schema-v1"
SOURCE = "binance-public-data-usdm-fundingRate"
AVAILABILITY_BASIS = "calc-time-plus-one-millisecond-surrogate-v1"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
TIMESTAMP_FIELD = "calc_time"
REQUIRED_FIELDS = {"calc_time", "funding_interval_hours", "last_funding_rate"}
ALLOWED_FIELDS = REQUIRED_FIELDS


def _time_ms(value: str) -> int:
    if not isinstance(value, str):
        raise ValueError("missing source timestamp")
    text = value.strip()
    if not text.isascii() or not text.isdigit():
        raise ValueError("calc_time must be integer epoch milliseconds")
    return int(text)


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
class FundingPackage:
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
    package = FundingPackage(symbol, str(relative), str(relative) + ".CHECKSUM",
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
class SettledFundingEvent:
    symbol: str
    source_time_ms: int
    funding_rate: Decimal
    funding_interval_hours: int
    available_at_ms: int
    package_relative_path: str
    package_sha256: str

    def __post_init__(self):
        if (type(self.source_time_ms) is not int or self.source_time_ms < 0
                or self.available_at_ms != self.source_time_ms + 1
                or type(self.funding_interval_hours) is not int or self.funding_interval_hours <= 0
                or not isinstance(self.funding_rate, Decimal) or not self.funding_rate.is_finite()):
            raise ValueError("invalid settled funding event")

    @property
    def expected_next_calc_time_ms(self):
        return self.source_time_ms + self.funding_interval_hours * 3_600_000


def monthly_funding_relative_path(symbol: str, month: date):
    return PurePosixPath("data", "futures", "um", "monthly", "fundingRate", symbol,
                         f"{symbol}-fundingRate-{month:%Y-%m}.zip")


def _parse_row(row, symbol, month, path, digest):
    timestamp = _time_ms(row["calc_time"])
    if (_day(timestamp).year, _day(timestamp).month) != (month.year, month.month):
        raise ValueError("funding event outside package month")
    interval = row["funding_interval_hours"].strip()
    if not interval.isascii() or not interval.isdigit() or int(interval) <= 0:
        raise ValueError("funding interval must be a positive integer")
    return SettledFundingEvent(symbol, timestamp, _number(row["last_funding_rate"]),
                               int(interval), timestamp + 1, path, digest)



@dataclass(frozen=True)
class BinanceFundingEvidence:
    configured_symbols: tuple[str, ...]
    requested_start_boundary_time_ms: int
    requested_end_boundary_time_ms: int
    history_start_time_ms: int
    packages: tuple[FundingPackage, ...]
    rows: tuple[SettledFundingEvent, ...]
    row_issues: tuple[tuple[str, int, str], ...] = ()
    evidence_sha256: str = field(init=False)
    _index: object = field(init=False, repr=False, compare=False)
    _times: object = field(init=False, repr=False, compare=False)
    _issues: object = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        symbols = _validate_request(self.configured_symbols,
                                    self.requested_start_boundary_time_ms,
                                    self.requested_end_boundary_time_ms, False)
        if any(not isinstance(r, SettledFundingEvent) or r.symbol not in symbols for r in self.rows):
            raise ValueError("invalid source observation")
        if any(not isinstance(p, FundingPackage) or p.symbol not in symbols for p in self.packages):
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
        preceding = (_day(self.requested_start_boundary_time_ms).replace(day=1) - timedelta(days=1)).replace(day=1)
        if _day(self.history_start_time_ms) > preceding:
            raise ValueError("funding requires preceding calendar month")
        rows = tuple(sorted(self.rows, key=lambda r: (symbols.index(r.symbol), r.source_time_ms)))
        if any(not isinstance(r, SettledFundingEvent) or r.symbol not in symbols for r in rows):
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
            "availability": AVAILABILITY_BASIS, "policy": "preceding calendar month; expected next settlement plus 1ms expiry",
            "symbols": symbols, "start": self.requested_start_boundary_time_ms,
            "end": self.requested_end_boundary_time_ms, "history_start": self.history_start_time_ms,
            "packages": self.packages, "rows": rows, "issues": self.row_issues}))

    def as_of(self, symbol: str, boundary: int):
        timestamps = self._times.get(symbol, ())
        offset = bisect_right(timestamps, boundary - 1) - 1
        return self._index[(symbol, timestamps[offset])] if offset >= 0 else None

    def has_pending_observation(self, symbol: str, boundary: int):
        timestamps = self._times.get(symbol, ())
        position = bisect_right(timestamps, boundary - 1)
        return position < len(timestamps) and timestamps[position] <= boundary

    def exact(self, symbol: str, source_time_ms: int):
        return self._index.get((symbol, source_time_ms))

    def reason_at(self, symbol: str, source_time_ms: int):
        issue = self._issues.get((symbol, source_time_ms))
        if issue:
            return issue
        relative = monthly_funding_relative_path(symbol, _day(source_time_ms).replace(day=1))
        package = next((p for p in self.packages if p.symbol == symbol
                        and p.relative_path == str(relative)), None)
        if package is None:
            return "SOURCE_EVIDENCE_UNAVAILABLE"
        if not package.checksum_verified or package.status in ("SCHEMA_MISMATCH", "INVALID_ARCHIVE"):
            return package.status
        return "EXPECTED_SETTLEMENT_MISSING"


def load_binance_usdm_funding_evidence(archive_root, configured_symbols,
                                      start: int, end: int, *, download=False):
    symbols = _validate_request(configured_symbols, start, end, download)
    start_day = _day(start)
    first = date(start_day.year, start_day.month, 1)
    first = (first - timedelta(days=1)).replace(day=1)
    last = _day(end).replace(day=1)
    history_start = (datetime(first.year, first.month, 1, tzinfo=timezone.utc) - _EPOCH) // timedelta(milliseconds=1)
    if history_start < 0:
        raise ValueError("funding prehistory precedes epoch")
    root = Path(archive_root).expanduser().resolve()
    packages, observations, issues = [], {}, {}
    for symbol in symbols:
        month = first
        while month <= last:
            relative = monthly_funding_relative_path(symbol, month)
            package, rows, row_issues = _load_package(root, symbol, month, relative, download)
            packages.append(package)
            for timestamp, reason in row_issues:
                issues[(symbol, timestamp)] = reason
            for row in rows:
                key = (symbol, row.source_time_ms)
                if key in observations or issues.get(key) == "DUPLICATE_SOURCE_TIMESTAMP":
                    observations.pop(key, None)
                    issues[key] = "DUPLICATE_SOURCE_TIMESTAMP"
                else:
                    observations[key] = row
            month = (month + timedelta(days=32)).replace(day=1)
    return BinanceFundingEvidence(symbols, start, end, history_start,
                                   tuple(packages), tuple(observations.values()),
                                   tuple((s, t, r) for (s, t), r in sorted(issues.items())))
