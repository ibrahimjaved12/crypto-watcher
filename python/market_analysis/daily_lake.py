"""Daily bars and funding history 2020-2026 (``dk1``) for indicator warm-up and context (#222, #223).

This dataset is INDICATOR WARM-UP and context only: evaluation segments stay
development 2024-01..2025-06, validation 2025-07..2025-12 and hidden 2026-01..2026-09,
and nothing is tuned on pre-2024 data.

Sources (Binance USD-M monthly archive, each zip with a ``.CHECKSUM``):
``{data_lake.BINANCE_BASE}/klines/<SYMBOL>/1d/<SYMBOL>-1d-YYYY-MM.zip`` and
``data_lake.source_url("fundingRate", symbol, month)``. HTTP 404 months are gaps
(a symbol listed after the first month simply starts later).

One release per symbol and month range, ``dk1-SYMBOL-FIRST_LAST-rN``, holding
``daily__SYMBOL__FIRST_LAST.csv.gz``, ``funding__SYMBOL__FIRST_LAST.csv.gz``,
``daily-raw__SYMBOL__FIRST_LAST.tar`` and ``daily__SYMBOL__FIRST_LAST.manifest.json``.
``data_lake`` identities and its ``rd-`` tag rule are untouched.

Daily rows: ``open_time`` must be an integer multiple of 86_400_000 (else an error
naming the file and line) and inside the zip's month (else dropped and counted).
Values stay the published text, validated with ``data_lake.exact_decimal`` (exponent
form allowed); ``trades`` must be an integer. On duplicate open times the first row
wins and differing duplicates are counted as conflicts.
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
import gzip
import hashlib
import io
from math import log
from pathlib import Path
import re
from typing import Callable

from . import data_lake
from .benchmark.bars import MISSING, _text
from .benchmark.funding import FundingSeries
from .benchmark.hidden_guard import require_months

FIRST_ARCHIVE_MONTH = "2019-09"
DEFAULT_FIRST = "2020-01"
SCHEMA = "daily-v1"
MANIFEST_VERSION = "daily-manifest-v1"
DAY_MS = 86_400_000
KLINE_COLUMNS = 12
VALUE_COLUMNS = ("open", "high", "low", "close", "volume", "quote_volume", "trades", "taker_buy_volume",
                 "taker_buy_quote_volume")
_SOURCE_INDEX = (1, 2, 3, 4, 5, 7, 8, 9, 10)  # kline columns of VALUE_COLUMNS (6 = close_time, 11 = ignore)
CSV_COLUMNS = ("open_time_ms", *VALUE_COLUMNS)
FUNDING_GAP_HOURS = 9
_SYMBOLS = "|".join(data_lake.SYMBOLS)
_MONTH = r"\d{4}-(?:0[1-9]|1[0-2])"
_TAG = re.compile(rf"dk1-({_SYMBOLS})-({_MONTH})_({_MONTH})-r([1-9][0-9]{{0,2}})\Z")
_RANGE_FILE = re.compile(rf"(daily|funding)__({_SYMBOLS})__({_MONTH})_({_MONTH})\.csv\.gz\Z")
_INTEGER = re.compile(r"-?[0-9]+\Z")


class DailyFormatError(ValueError):
    """An archive or file differs from the expected layout; the message names the file and line."""


# ---------------------------------------------------------------- names


def release_tag(symbol: str, first_month: str, last_month: str, revision: int = 1) -> str:
    data_lake.validate_symbol(symbol)
    data_lake.months_between(first_month, last_month)  # validates both and their order
    return validate_tag(f"dk1-{symbol}-{first_month}_{last_month}-r{revision}")


def validate_tag(tag: str) -> str:
    match = _TAG.match(tag) if isinstance(tag, str) else None
    if not match or match.group(2) > match.group(3):
        raise ValueError(f"invalid daily history release tag: {tag!r}")
    return tag


def kline_url(symbol: str, month: str) -> str:
    data_lake.validate_symbol(symbol)
    data_lake.validate_month(month)
    return f"{data_lake.BINANCE_BASE}/klines/{symbol}/1d/{symbol}-1d-{month}.zip"


def funding_url(symbol: str, month: str) -> str:
    return data_lake.source_url("fundingRate", symbol, month)


def _stem(symbol: str, first_month: str, last_month: str) -> str:
    return f"{symbol}__{first_month}_{last_month}"


def daily_asset_name(symbol: str, first_month: str, last_month: str) -> str:
    return f"daily__{_stem(symbol, first_month, last_month)}.csv.gz"


def funding_asset_name(symbol: str, first_month: str, last_month: str) -> str:
    return f"funding__{_stem(symbol, first_month, last_month)}.csv.gz"


def raw_asset_name(symbol: str, first_month: str, last_month: str) -> str:
    return f"daily-raw__{_stem(symbol, first_month, last_month)}.tar"


def manifest_asset_name(symbol: str, first_month: str, last_month: str) -> str:
    return f"daily__{_stem(symbol, first_month, last_month)}.manifest.json"


# ---------------------------------------------------------------- parsing


@dataclass(frozen=True)
class DailyRow:
    open_time_ms: int
    values: tuple  # published text per VALUE_COLUMNS


def parse_kline_zip(data: bytes, month: str, name: str) -> tuple[list[DailyRow], dict]:
    """(rows inside the month, stats) of one monthly 1d kline zip (a header line is skipped)."""
    start, end, _ = data_lake.month_bounds_ms(month)
    rows = data_lake.read_zip_csv(io.BytesIO(data), KLINE_COLUMNS, name=name)
    out, outside = [], 0
    try:
        for row in rows:
            where = f"{name} line {rows.line}"
            text = row[0].strip()
            if not _INTEGER.match(text):
                raise DailyFormatError(f"{where}: open_time {text!r} is not an integer")
            open_time = int(text)
            if open_time % DAY_MS:
                raise DailyFormatError(f"{where}: open_time {open_time} is not a UTC day start")
            values = tuple(row[index].strip() for index in _SOURCE_INDEX)
            for column, value in zip(VALUE_COLUMNS, values):
                try:
                    if column == "trades":
                        if not _INTEGER.match(value):
                            raise ValueError(f"invalid integer {value!r}")
                    else:
                        data_lake.exact_decimal(value)
                except ValueError as error:
                    raise DailyFormatError(f"{where} column {column}: {error}") from None
            if not start <= open_time < end:
                outside += 1
                continue
            out.append(DailyRow(open_time, values))
    except data_lake.ArchiveFormatError as error:
        raise DailyFormatError(str(error)) from None
    return out, {"rows": len(out), "rows_outside_month": outside}


def merge_daily(rows) -> tuple[list[DailyRow], int, int]:
    """(rows sorted by time with the first occurrence per open time, duplicates, conflicting duplicates)."""
    seen, out, duplicates, conflicts = {}, [], 0, 0
    for row in rows:
        if row.open_time_ms in seen:
            duplicates += 1
            conflicts += seen[row.open_time_ms] != row.values
            continue
        seen[row.open_time_ms] = row.values
        out.append(row)
    out.sort(key=lambda row: row.open_time_ms)
    return out, duplicates, conflicts


def funding_month_lines(data: bytes, month: str, name: str) -> tuple[list[str], dict]:
    """One month's funding rows validated by ``data_lake.write_funding_csv_gz`` (in memory): (lines, stats)."""
    buffer = io.BytesIO()
    stats = data_lake.write_funding_csv_gz(buffer, data_lake.read_zip_csv(io.BytesIO(data), 3, name=name), month)
    lines = gzip.decompress(buffer.getvalue()).decode("ascii").splitlines(keepends=True)
    return lines[1:], stats


def _date(ms: int) -> str:
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def coverage(rows, *, duplicates: int, conflicts: int, rows_outside_month: int, funding_times=(),
             funding_intervals=()) -> dict:
    """Aggregate statistics of one symbol over the whole range (rows sorted, one per day)."""
    times = [row.open_time_ms for row in rows]
    present = set(times)
    first, last = (times[0], times[-1]) if times else (None, None)
    missing = [] if first is None else [_date(ms) for ms in range(first, last + DAY_MS, DAY_MS) if ms not in present]
    high_below_low = close_outside = nonpositive = zero_volume = 0
    max_move, max_move_day, previous = None, None, None
    for row in rows:
        o, h, l, c, v = (Decimal(data_lake.exact_decimal(text)) for text in row.values[:5])
        high_below_low += h < l
        close_outside += not l <= c <= h
        nonpositive += min(o, h, l, c) <= 0
        zero_volume += v == 0
        if previous is not None and previous[0] == row.open_time_ms - DAY_MS and previous[1] > 0 and c > 0:
            move = abs(log(c / previous[1]))
            if max_move is None or move > max_move:
                max_move, max_move_day = move, _date(row.open_time_ms)
        previous = (row.open_time_ms, c)
    gaps = [b - a for a, b in zip(funding_times, funding_times[1:]) if b - a > FUNDING_GAP_HOURS * 3_600_000]
    return {"first_day": None if first is None else _date(first), "last_day": None if last is None else _date(last),
            "days_present": len(present),
            "days_expected": 0 if first is None else (last - first) // DAY_MS + 1, "missing_days": missing,
            "duplicate_rows": duplicates, "duplicate_conflicts": conflicts, "rows_outside_month": rows_outside_month,
            "high_below_low": high_below_low, "close_outside_low_high": close_outside,
            "nonpositive_prices": nonpositive, "zero_volume_days": zero_volume,
            "max_abs_daily_log_return": None if max_move is None else f"{max_move:.6f}",
            "max_abs_daily_log_return_day": max_move_day,
            "funding_settlements": len(funding_times),
            "funding_interval_hours": sorted({str(hours) for hours in funding_intervals}, key=int),
            "funding_gaps_over_9h": len(gaps),
            "funding_max_gap_hours": None if not gaps else str(Fraction(max(gaps), 3_600_000))}


# ---------------------------------------------------------------- collection (fetcher injected)


class ChecksumMismatch(RuntimeError):
    pass


def collect(symbol: str, months, fetcher: Callable[[str], bytes | None]) -> dict:
    """{(kind, month): None | {"zip", "sha256", "checksum_sha256", "file", "url"}} for kind klines / funding.

    ``fetcher(url) -> bytes | None`` (None = HTTP 404, a recorded gap); a checksum mismatch raises.
    """
    data_lake.validate_symbol(symbol)
    out = {}
    for month in months:
        for kind, url in (("klines", kline_url(symbol, month)), ("funding", funding_url(symbol, month))):
            checksum = fetcher(url + ".CHECKSUM")
            body = fetcher(url) if checksum is not None else None
            if checksum is None or body is None:
                out[(kind, month)] = None
                continue
            expected = data_lake.parse_checksum(checksum.decode("ascii", "replace"))
            actual = hashlib.sha256(body).hexdigest()
            if actual != expected:
                raise ChecksumMismatch(f"CHECKSUM MISMATCH for {url}: Binance {expected}, downloaded {actual}")
            out[(kind, month)] = {"zip": body, "sha256": actual, "checksum_sha256": expected,
                                  "file": url.rsplit("/", 1)[1], "url": url}
    return out


# ---------------------------------------------------------------- outputs


def write_daily_csv_gz(fileobj, rows) -> int:
    def lines():
        yield ",".join(CSV_COLUMNS) + "\n"
        for row in rows:
            yield ",".join((str(row.open_time_ms), *row.values)) + "\n"

    return data_lake.write_csv_gz(fileobj, lines())


def write_funding_range_csv_gz(fileobj, lines) -> int:
    """One combined funding file with a single header (lines from ``funding_month_lines``, in month order)."""
    return data_lake.write_csv_gz(fileobj, [",".join(data_lake.FUNDING_COLUMNS) + "\n", *lines])


# ---------------------------------------------------------------- loaders


@dataclass(frozen=True)
class DailySeries:
    """Contiguous UTC days from the first present day; a gap day holds MISSING in every column."""

    symbol: str
    start_ms: int
    days: int
    open: object
    high: object
    low: object
    close: object
    volume: object
    taker_buy_volume: object

    def open_time(self, index: int) -> int:
        return self.start_ms + index * DAY_MS

    @property
    def end_ms(self) -> int:
        return self.start_ms + self.days * DAY_MS


def _range_file(directory, kind: str, symbol: str, first_month: str, last_month: str) -> Path:
    matches = []
    for path in Path(directory).iterdir():
        match = _RANGE_FILE.match(path.name)
        if match and match.group(1) == kind and match.group(2) == symbol:
            if match.group(3) <= first_month and last_month <= match.group(4):
                matches.append(path)
    if len(matches) != 1:
        raise FileNotFoundError(f"expected exactly one {kind} file of {symbol} covering {first_month}..{last_month}, "
                                f"found {len(matches)}")
    return matches[0]


def _bounds(symbol: str, first_month: str, last_month: str, token, gate) -> tuple[int, int]:
    data_lake.validate_symbol(symbol)
    months = data_lake.months_between(first_month, last_month)
    require_months(months, token, gate)  # every requested month, before any file is opened
    return data_lake.month_bounds_ms(first_month)[0], data_lake.month_bounds_ms(last_month)[1]


def load_symbol_daily(daily_dir, symbol: str, first_month: str, last_month: str, *, token=None,
                      gate=None) -> DailySeries:
    """Days of ``first_month..last_month`` (inclusive). Reading stops at the first row after the range,
    so rows of later (for example hidden) months in the same file are never interpreted."""
    first_ms, end_ms = _bounds(symbol, first_month, last_month, token, gate)
    path = _range_file(daily_dir, "daily", symbol, first_month, last_month)
    rows = {}
    with path.open("rb") as stream:
        lines = iter(_text(stream))
        header = next(lines, "").rstrip("\r\n")
        if header != ",".join(CSV_COLUMNS):
            raise DailyFormatError(f"{path.name}: header differs from {','.join(CSV_COLUMNS)}")
        previous = None
        for number, line in enumerate(lines, start=1):
            fields = line.rstrip("\r\n").split(",")
            if len(fields) != len(CSV_COLUMNS) or not _INTEGER.match(fields[0]):
                raise DailyFormatError(f"{path.name} line {number}: malformed row")
            stamp = int(fields[0])
            if (previous is not None and stamp <= previous) or stamp % DAY_MS:
                raise DailyFormatError(f"{path.name} line {number}: open time out of order or not a day start")
            previous = stamp
            if stamp >= end_ms:
                break
            if stamp >= first_ms:
                rows[stamp] = fields[1:]
    start = min(rows) if rows else first_ms
    days = (max(rows) - start) // DAY_MS + 1 if rows else 0
    columns = {name: array("q", [MISSING]) * days for name in ("open", "high", "low", "close", "volume",
                                                               "taker_buy_volume")}
    index_of = {name: VALUE_COLUMNS.index(name) for name in columns}
    for stamp, values in rows.items():
        i = (stamp - start) // DAY_MS
        for name, column in columns.items():
            column[i] = data_lake.parse_published_scaled(values[index_of[name]])
    return DailySeries(symbol, start, days, **columns)


def load_symbol_funding_range(daily_dir, symbol: str, first_month: str, last_month: str, *, token=None,
                              gate=None) -> FundingSeries:
    """Funding settlements of ``first_month..last_month``; reading stops at the first row after the range.

    A row not after the previous kept one is skipped (the series must be strictly increasing).
    """
    first_ms, end_ms = _bounds(symbol, first_month, last_month, token, gate)
    path = _range_file(daily_dir, "funding", symbol, first_month, last_month)
    times, intervals, rates = [], [], []
    with path.open("rb") as stream:
        lines = iter(_text(stream))
        header = next(lines, "").rstrip("\r\n")
        if header != ",".join(data_lake.FUNDING_COLUMNS):
            raise DailyFormatError(f"{path.name}: header differs from {','.join(data_lake.FUNDING_COLUMNS)}")
        for number, line in enumerate(lines, start=1):
            fields = line.rstrip("\r\n").split(",")
            if len(fields) != len(data_lake.FUNDING_COLUMNS) or not _INTEGER.match(fields[0]) \
                    or not _INTEGER.match(fields[1]):
                raise DailyFormatError(f"{path.name} line {number}: malformed row")
            stamp = int(fields[0])
            if stamp >= end_ms:
                break
            if stamp < first_ms or (times and stamp <= times[-1]):
                continue
            times.append(stamp)
            intervals.append(int(fields[1]))
            rates.append(Fraction(data_lake.exact_decimal(fields[2])))
    return FundingSeries(tuple(times), tuple(intervals), tuple(rates), first_ms, end_ms)
