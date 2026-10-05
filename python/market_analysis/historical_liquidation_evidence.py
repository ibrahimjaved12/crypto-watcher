"""Independent Tardis normalized liquidation evidence; acquisition is opt-in/free only."""
from __future__ import annotations

from bisect import bisect_left
import csv
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import gzip
import hashlib
from pathlib import Path, PurePosixPath
import tempfile
from types import MappingProxyType
from urllib.request import urlopen
import os

from .historical_experiment_batch import _sha256

EVIDENCE_VERSION = "tardis-binance-futures-liquidations-evidence-v1"
SCHEMA_VERSION = "tardis-normalized-liquidations-eight-fields-v1"
SOURCE = "tardis-binance-futures-liquidations"
AVAILABILITY_BASIS = "provider-local-timestamp-microseconds-v1"
FIELDS = ("exchange", "symbol", "timestamp", "local_timestamp", "id", "side", "price", "amount")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
DAY_MS = 86_400_000


def _day(ms):
    return (_EPOCH + timedelta(milliseconds=ms)).date()


def daily_liquidation_relative_path(day: date):
    return PurePosixPath("binance-futures", "liquidations", f"{day:%Y/%m/%d}", "PERPETUALS.csv.gz")


def _acquire_free_day(root, day):
    """Only unauthenticated first-of-month acquisition, staged before publication."""
    if day.day != 1:
        raise ValueError("only free first-day-of-month acquisition is permitted")
    relative = daily_liquidation_relative_path(day)
    target = root.joinpath(*relative.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            staged = Path(stream.name)
            with urlopen("https://datasets.tardis.dev/v1/" + str(relative), timeout=60) as response:
                while block := response.read(1024 * 1024):
                    stream.write(block)
            stream.flush()
            os.fsync(stream.fileno())
        # Verify the gzip trailer before installing; schema is checked by the loader.
        with gzip.open(staged, "rb") as stream:
            while stream.read(1024 * 1024):
                pass
        os.replace(staged, target)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def _integer_us(value):
    if not value.isascii() or not value.isdigit():
        raise ValueError("timestamps must be integer microseconds")
    return int(value)


def _positive(value):
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError("price/normalized amount must be finite and positive")
    return result


@dataclass(frozen=True)
class LiquidationSnapshot:
    symbol: str
    timestamp_us: int
    local_timestamp_us: int
    snapshot_id: str
    side: str
    price: Decimal
    amount: Decimal
    package_relative_path: str
    package_sha256: str
    capture_row: int

    def __post_init__(self):
        if (type(self.timestamp_us) is not int or self.timestamp_us < 0
                or type(self.local_timestamp_us) is not int or self.local_timestamp_us < 0
                or self.side not in ("buy", "sell")
                or any(not isinstance(v, Decimal) or not v.is_finite() or v <= 0
                       for v in (self.price, self.amount))):
            raise ValueError("invalid liquidation snapshot")


@dataclass(frozen=True)
class LiquidationPackage:
    utc_day: date
    relative_path: str
    archive_present: bool
    compressed_sha256: str | None
    status: str
    row_count: int = 0
    configured_row_count: int = 0
    ignored_symbol_row_count: int = 0
    malformed_rows: tuple[str, ...] = ()
    error: str | None = None


def _load_day(root, day, symbols, download):
    relative = daily_liquidation_relative_path(day)
    path = root.joinpath(*relative.parts)
    error = None
    if not path.is_file() and download and day.day == 1:
        try:
            _acquire_free_day(root, day)
        except (OSError, EOFError, ValueError) as exc:
            error = type(exc).__name__ + ": " + str(exc)
    if not path.is_file():
        return LiquidationPackage(day, str(relative), False, None,
            "NOT_COLLECTED" if day.day == 1 else "FREE_SAMPLE_UNAVAILABLE", error=error), ()
    digest = None
    rows, malformed = [], []
    count = ignored = configured = 0
    try:
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                hasher.update(block)
        digest = hasher.hexdigest()
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            header = tuple(name.strip().lower() for name in (reader.fieldnames or ()))
            if len(header) != len(FIELDS) or set(header) != set(FIELDS):
                # Consume to verify decompression/trailer even with incompatible schema.
                stream.read()
                return LiquidationPackage(day, str(relative), True, digest, "SCHEMA_MISMATCH"), ()
            reader.fieldnames = list(header)
            for line, row in enumerate(reader, 2):
                count += 1
                if row.get("symbol") not in symbols:
                    ignored += 1
                    # A missing symbol or malformed row shape cannot be safely attributed.
                    if not row.get("symbol") or None in row or any(v is None for v in row.values()):
                        malformed.append(f"row {line}: invalid grouped row shape")
                    continue
                configured += 1
                try:
                    if None in row or any(v is None for v in row.values()):
                        raise ValueError("row does not match named schema")
                    event, receipt = _integer_us(row["timestamp"]), _integer_us(row["local_timestamp"])
                    if row["exchange"] != "binance-futures" or _day(receipt // 1000) != day:
                        raise ValueError("exchange or local receipt day differs from package")
                    rows.append(LiquidationSnapshot(row["symbol"], event, receipt, row["id"],
                        row["side"], _positive(row["price"]), _positive(row["amount"]),
                        str(relative), digest, line))
                except (ValueError, TypeError, ArithmeticError, OverflowError) as exc:
                    malformed.append(f"row {line}: {exc}")
        status = "MALFORMED_ROW" if malformed else "ARCHIVE_DAY_AVAILABLE" if count else "EMPTY_PACKAGE"
    except (OSError, EOFError, UnicodeError, csv.Error, ValueError) as exc:
        status, error = "INVALID_ARCHIVE", type(exc).__name__ + ": " + str(exc)
    package = LiquidationPackage(day, str(relative), True, digest, status,
                                 count, configured, ignored, tuple(malformed), error)
    # Conservative whole-day rejection prevents malformed snapshots producing false zero totals.
    return package, tuple(rows) if status == "ARCHIVE_DAY_AVAILABLE" else ()


@dataclass(frozen=True)
class TardisLiquidationEvidence:
    configured_symbols: tuple[str, ...]
    requested_start_boundary_time_ms: int
    requested_end_boundary_time_ms: int
    history_start_time_ms: int
    packages: tuple[LiquidationPackage, ...]
    rows: tuple[LiquidationSnapshot, ...]
    evidence_sha256: str = field(init=False)
    _packages: object = field(init=False, repr=False, compare=False)
    _rows_by_symbol: object = field(init=False, repr=False, compare=False)
    _times: object = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        symbols = tuple(self.configured_symbols)
        start, end = self.requested_start_boundary_time_ms, self.requested_end_boundary_time_ms
        if (not symbols or len(set(symbols)) != len(symbols)
                or any(not isinstance(s, str) or not s or not s.isascii() or not s.isalnum()
                       or s != s.upper() for s in symbols)
                or type(start) is not int or type(end) is not int or start < 900_000
                or end < start or start % 5000 or end % 5000):
            raise ValueError("invalid ordered universe or replay range")
        rows, packages = tuple(self.rows), tuple(self.packages)
        if any(not isinstance(r, LiquidationSnapshot) or r.symbol not in symbols for r in rows):
            raise ValueError("invalid configured liquidation row")
        if any(not isinstance(p, LiquidationPackage) for p in packages):
            raise ValueError("invalid receipt-day package")
        by_day = {p.utc_day: p for p in packages}
        if len(by_day) != len(packages):
            raise ValueError("duplicate receipt-day package")
        if self.history_start_time_ms != start - 900_000:
            raise ValueError("liquidation prehistory must be 15m")
        for row in rows:
            package = by_day.get(_day(row.local_timestamp_us // 1000))
            if (package is None or package.status != "ARCHIVE_DAY_AVAILABLE"
                    or package.relative_path != row.package_relative_path
                    or package.compressed_sha256 != row.package_sha256):
                raise ValueError("usable snapshot must bind an available receipt-day package")
        indexed = {s: tuple(sorted((r for r in rows if r.symbol == s),
                                   key=lambda r: r.timestamp_us)) for s in symbols}
        object.__setattr__(self, "configured_symbols", symbols)
        object.__setattr__(self, "packages", packages)
        object.__setattr__(self, "rows", rows)  # Original archive capture order; no deduplication.
        object.__setattr__(self, "_packages", MappingProxyType(by_day))
        object.__setattr__(self, "_rows_by_symbol", MappingProxyType(indexed))
        object.__setattr__(self, "_times", MappingProxyType({s: tuple(r.timestamp_us for r in rs)
                                                          for s, rs in indexed.items()}))
        object.__setattr__(self, "evidence_sha256", _sha256({
            "version": EVIDENCE_VERSION, "schema": SCHEMA_VERSION, "source": SOURCE,
            "availability": AVAILABILITY_BASIS,
            "policy": "whole receipt-day coverage; malformed configured row rejects day; no dedup; normalized amount; disconnect visibility unavailable",
            "symbols": symbols, "start": start, "end": end,
            "history_start": self.history_start_time_ms, "packages": packages, "rows": rows}))

    def coverage_reasons(self, start_ms, end_ms):
        first, last = _day(start_ms), _day(end_ms - 1)
        reasons = []
        for offset in range((last - first).days + 1):
            day = first + timedelta(days=offset)
            package = self._packages.get(day)
            if package is None or package.status != "ARCHIVE_DAY_AVAILABLE":
                reasons.append((day.isoformat(), package.status if package else "NOT_COLLECTED"))
        return tuple(reasons)

    def window_rows(self, symbol, boundary_ms, minutes):
        start_us, end_us = (boundary_ms - minutes * 60_000) * 1000, boundary_ms * 1000
        rows, timestamps = self._rows_by_symbol[symbol], self._times[symbol]
        first, last = bisect_left(timestamps, start_us), bisect_left(timestamps, end_us)
        return tuple(r for r in rows[first:last] if r.local_timestamp_us <= end_us)


def liquidation_package_days(start, end):
    first, last = _day(start - 900_000), _day(end)
    return tuple(first + timedelta(days=offset) for offset in range((last - first).days + 1))


def load_tardis_liquidation_evidence(archive_root, configured_symbols, start, end, *, download=False):
    if type(download) is not bool:
        raise ValueError("download must be boolean")
    # Validate before allowing acquisition.
    symbols = tuple(configured_symbols)
    empty = TardisLiquidationEvidence(symbols, start, end, start - 900_000, (), ())
    root = Path(archive_root).expanduser().resolve()
    packages, rows = [], []
    for day in liquidation_package_days(start, end):
        package, observations = _load_day(root, day, symbols, download)
        packages.append(package)
        rows.extend(observations)
    return TardisLiquidationEvidence(symbols, start, end, start - 900_000, tuple(packages), tuple(rows))
