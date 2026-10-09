"""Binance USD-M daily ``metrics`` archive, phase 1 (#224): parse, check, store. No network here.

Source: ``{BASE_URL}/metrics/<SYMBOL>/<SYMBOL>-metrics-YYYY-MM-DD.zip`` plus ``.CHECKSUM``.
The column layout is NOT verified by us (the sandbox cannot reach the archive), so
the parser is header-driven: the first CSV line must equal ``EXPECTED_HEADER`` and
any other header fails with the observed header in the message. ``create_time`` may
be a ``YYYY-MM-DD HH:MM:SS`` UTC string or epoch milliseconds; both are normalized to
epoch milliseconds and the observed format is recorded. Every other value is kept as
the published text, validated as an exact decimal (``data_lake.exact_decimal``); an
empty value is kept empty and counted.

One release per symbol-month, ``mx-SYMBOL-YYYY-MM-rN`` (private research-data repo),
holding ``metrics__SYMBOL__YYYY-MM.csv.gz`` (rows sorted by timestamp, one row per
timestamp), ``metrics-raw__SYMBOL__YYYY-MM.tar`` (the original daily zips,
unmodified) and ``metrics__SYMBOL__YYYY-MM.manifest.json``. ``data_lake`` identities and
its ``rd-`` tags are untouched.

Point in time: see ``usable_from_ms``.
"""
from __future__ import annotations

import calendar
import csv
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import io
from pathlib import Path
import re
import tarfile
import time
from typing import Callable
import zipfile

from . import data_lake
from .benchmark.bars import _text
from .benchmark.hidden_guard import require_months

FAMILY = "metrics"
BASE_URL = "https://data.binance.vision/data/futures/um/daily"
EXPECTED_HEADER = ("create_time", "symbol", "sum_open_interest", "sum_open_interest_value",
                   "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio", "count_long_short_ratio",
                   "sum_taker_long_short_vol_ratio")
VALUE_COLUMNS = EXPECTED_HEADER[2:]
CSV_COLUMNS = ("create_time_ms", *VALUE_COLUMNS)
ROWS_PER_DAY = 288
PERIOD_MS = 300_000
POINT_IN_TIME_LAG_PERIODS = 1
FIRST_ARCHIVE_MONTH = "2020-01"
SCHEMA = "metrics-v1"
MANIFEST_VERSION = "metrics-manifest-v1"
JUMP_UP = Fraction(5)
JUMP_DOWN = Fraction(1, 5)
DAY_MS = 86_400_000
_TAG = re.compile(r"mx-(" + "|".join(data_lake.SYMBOLS) + r")-(\d{4}-(?:0[1-9]|1[0-2]))-r([1-9][0-9]{0,2})\Z")
_DATETIME = re.compile(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})\Z")
_EPOCH_MS = re.compile(r"[0-9]{13}\Z")
_DAY = re.compile(r"\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])\Z")


class MetricsFormatError(ValueError):
    """The archive differs from the expected layout; the message names the file and what was observed."""


def usable_from_ms(create_time_ms: int) -> int:
    """First decision time at which a row stamped ``create_time_ms`` may be used.

    Rule (conservative until the first dry run verifies the stamp semantics): a row
    stamped T is treated as describing the 5-minute period ending at T, and it is
    exposed only to decisions at T + POINT_IN_TIME_LAG_PERIODS * PERIOD_MS or later
    (one extra period of lag for publication delay).
    """
    if type(create_time_ms) is not int:
        raise TypeError("create_time_ms must be an int")
    return create_time_ms + POINT_IN_TIME_LAG_PERIODS * PERIOD_MS


# ---------------------------------------------------------------- names


def release_tag(symbol: str, month: str, revision: int = 1) -> str:
    return validate_tag(f"mx-{data_lake.validate_symbol(symbol)}-{data_lake.validate_month(month)}-r{revision}")


def validate_tag(tag: str) -> str:
    if not isinstance(tag, str) or not _TAG.match(tag):
        raise ValueError(f"invalid metrics release tag: {tag!r}")
    return tag


def validate_day(day: str) -> str:
    if not isinstance(day, str) or not _DAY.match(day):
        raise ValueError(f"invalid day (expected YYYY-MM-DD): {day!r}")
    calendar.timegm(time.strptime(day, "%Y-%m-%d"))  # rejects impossible dates
    return day


def day_bounds_ms(day: str) -> tuple[int, int]:
    start = calendar.timegm(time.strptime(validate_day(day), "%Y-%m-%d")) * 1000
    return start, start + DAY_MS


def days_of_month(month: str) -> list[str]:
    _, _, days = data_lake.month_bounds_ms(month)
    return [f"{month}-{day:02d}" for day in range(1, days + 1)]


def source_url(symbol: str, day: str) -> str:
    data_lake.validate_symbol(symbol)
    validate_day(day)
    return f"{BASE_URL}/{FAMILY}/{symbol}/{symbol}-metrics-{day}.zip"


def checksum_url(symbol: str, day: str) -> str:
    return source_url(symbol, day) + ".CHECKSUM"


def csv_asset_name(symbol: str, month: str) -> str:
    return f"metrics__{symbol}__{month}.csv.gz"


def raw_asset_name(symbol: str, month: str) -> str:
    return f"metrics-raw__{symbol}__{month}.tar"


def manifest_asset_name(symbol: str, month: str) -> str:
    return f"metrics__{symbol}__{month}.manifest.json"


# ---------------------------------------------------------------- parsing


@dataclass(frozen=True)
class MetricsRow:
    create_time_ms: int
    values: tuple  # exact published text per VALUE_COLUMNS ("" when empty)


def _timestamp(text: str) -> tuple[int, str]:
    token = text.strip()
    match = _DATETIME.match(token)
    if match:
        return calendar.timegm(tuple(int(part) for part in match.groups()) + (0, 0, 0)) * 1000, "datetime"
    if _EPOCH_MS.match(token):
        return int(token), "epoch_ms"
    raise ValueError(f"unrecognized create_time {text!r}")


def parse_daily_zip(data: bytes, symbol: str, day: str) -> tuple[list[MetricsRow], dict]:
    """(rows, info) of one daily zip; info: observed header, timestamp format, row counts.

    Raises MetricsFormatError for a wrong zip layout, a header that differs from
    EXPECTED_HEADER (the message carries the observed header), a symbol other than
    ``symbol``, mixed timestamp formats or a non-decimal value. Rows stamped outside
    [day start, day end] are dropped and counted.
    """
    data_lake.validate_symbol(symbol)
    first_ms, end_ms = day_bounds_ms(day)
    name = f"{symbol}-metrics-{day}.zip"
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise MetricsFormatError(f"{name}: not a zip archive") from None
    with archive:
        members = [info for info in archive.infolist() if not info.is_dir()]
        if len(members) != 1 or not members[0].filename.lower().endswith(".csv"):
            raise MetricsFormatError(f"{name}: expected exactly one .csv member, found "
                                     f"{[info.filename for info in members]}")
        try:
            text = archive.read(members[0]).decode("ascii")
        except UnicodeError:
            raise MetricsFormatError(f"{name}: CSV is not ASCII") from None
    reader = csv.reader(io.StringIO(text, newline=""))
    header = next(reader, None)
    observed = tuple(field.strip() for field in header) if header is not None else ()
    if observed != EXPECTED_HEADER:
        raise MetricsFormatError(f"{name}: unexpected header; observed: {','.join(observed) or '<empty file>'}; "
                                 f"expected: {','.join(EXPECTED_HEADER)}")
    rows, formats, outside, empty = [], set(), 0, 0
    for row in reader:
        if not row:
            continue
        where = f"{name} line {reader.line_num}"
        if len(row) != len(EXPECTED_HEADER):
            raise MetricsFormatError(f"{where}: expected {len(EXPECTED_HEADER)} columns, got {len(row)}")
        try:
            stamp, kind = _timestamp(row[0])
        except ValueError as error:
            raise MetricsFormatError(f"{where}: {error}") from None
        formats.add(kind)
        if row[1].strip() != symbol:
            raise MetricsFormatError(f"{where}: symbol {row[1].strip()!r} is not {symbol}")
        values = []
        for column, value in zip(VALUE_COLUMNS, row[2:]):
            token = value.strip()
            if token == "":
                empty += 1
            else:
                try:
                    data_lake.exact_decimal(token)
                except ValueError:
                    raise MetricsFormatError(f"{where} column {column}: not a decimal") from None
            values.append(token)
        if not first_ms <= stamp <= end_ms:
            outside += 1
            continue
        rows.append(MetricsRow(stamp, tuple(values)))
    if len(formats) > 1:
        raise MetricsFormatError(f"{name}: mixed create_time formats {sorted(formats)}")
    info = {"header": ",".join(observed), "timestamp_format": formats.pop() if formats else None,
            "rows": len(rows), "rows_outside_day": outside, "empty_values": empty}
    return rows, info


# ---------------------------------------------------------------- coverage


def _oi(row: MetricsRow):
    text = row.values[0]
    return None if text == "" else Fraction(data_lake.exact_decimal(text))


def coverage(rows_by_day: dict) -> dict:
    """Aggregate statistics of one symbol-month; ``rows_by_day`` maps every day to its rows (None = missing)."""
    days = sorted(rows_by_day)
    present = [day for day in days if rows_by_day[day] is not None]
    stamps, previous, non_monotonic = [], None, 0
    zero_oi = empty_oi = 0
    longest, run, last_oi_text = 0, 0, None
    day_last_oi = []
    for day in present:
        last_value = None
        for row in rows_by_day[day]:
            if previous is not None and row.create_time_ms < previous:
                non_monotonic += 1
            previous = row.create_time_ms
            stamps.append(row.create_time_ms)
            value = _oi(row)
            if value is None:
                empty_oi += 1
                run, last_oi_text = 0, None
                continue
            if value <= 0:
                zero_oi += 1
            run = run + 1 if row.values[0] == last_oi_text else 1
            last_oi_text = row.values[0]
            longest = max(longest, run)
            last_value = value
        if last_value is not None:
            day_last_oi.append(last_value)
    jumps_up = jumps_down = 0
    for before, after in zip(day_last_oi, day_last_oi[1:]):
        if before > 0:
            ratio = after / before
            jumps_up += ratio > JUMP_UP
            jumps_down += ratio < JUMP_DOWN
    counts = [len(rows_by_day[day]) for day in present]
    return {"days": len(days), "days_present": len(present),
            "days_missing": [day for day in days if rows_by_day[day] is None],
            "rows": sum(counts), "rows_per_day_expected": ROWS_PER_DAY,
            "rows_per_day_min": min(counts) if counts else None, "rows_per_day_max": max(counts) if counts else None,
            "days_not_expected_rows": sum(1 for count in counts if count != ROWS_PER_DAY),
            "duplicate_timestamps": len(stamps) - len(set(stamps)), "non_monotonic_timestamps": non_monotonic,
            "nonpositive_open_interest": zero_oi, "empty_open_interest": empty_oi,
            "longest_identical_open_interest_run": longest,
            "open_interest_day_jumps_above_5x": jumps_up, "open_interest_day_jumps_below_0_2x": jumps_down,
            "first_ms": min(stamps) if stamps else None, "last_ms": max(stamps) if stamps else None}


def merged_rows(rows_by_day: dict) -> tuple[list[MetricsRow], int]:
    """All rows sorted by timestamp, one per timestamp (first occurrence kept); returns (rows, conflicting dups)."""
    seen, out, conflicts = {}, [], 0
    for day in sorted(rows_by_day):
        for row in rows_by_day[day] or ():
            if row.create_time_ms in seen:
                conflicts += seen[row.create_time_ms] != row.values
                continue
            seen[row.create_time_ms] = row.values
            out.append(row)
    out.sort(key=lambda row: row.create_time_ms)
    return out, conflicts


# ---------------------------------------------------------------- daily collection (fetcher injected)


class ChecksumMismatch(RuntimeError):
    pass


def collect_month(symbol: str, month: str, fetcher: Callable[[str], bytes | None]) -> dict:
    """Download every day of the month through ``fetcher(url) -> bytes | None`` (None = HTTP 404).

    A day whose .CHECKSUM or zip is 404 is a recorded gap; a checksum mismatch raises.
    Returns {day: None | {"zip", "sha256", "checksum_sha256", "file"}}.
    """
    out = {}
    for day in days_of_month(data_lake.validate_month(month)):
        checksum = fetcher(checksum_url(symbol, day))
        body = fetcher(source_url(symbol, day)) if checksum is not None else None
        if checksum is None or body is None:
            out[day] = None
            continue
        expected = data_lake.parse_checksum(checksum.decode("ascii", "replace"))
        actual = hashlib.sha256(body).hexdigest()
        if actual != expected:
            raise ChecksumMismatch(f"CHECKSUM MISMATCH for {source_url(symbol, day)}: Binance {expected}, "
                                   f"downloaded {actual}")
        out[day] = {"zip": body, "sha256": actual, "checksum_sha256": expected,
                    "file": source_url(symbol, day).rsplit("/", 1)[1]}
    return out


# ---------------------------------------------------------------- outputs


def write_metrics_csv_gz(fileobj, rows) -> int:
    """Deterministic gzip CSV (data_lake.write_csv_gz); returns data rows written."""
    def lines():
        yield ",".join(CSV_COLUMNS) + "\n"
        for row in rows:
            yield ",".join((str(row.create_time_ms), *row.values)) + "\n"

    return data_lake.write_csv_gz(fileobj, lines())


def write_raw_tar(fileobj, files: dict) -> None:
    """Deterministic uncompressed tar of {name: bytes} (sorted names, mtime 0, uid/gid 0, mode 0644)."""
    with tarfile.open(fileobj=fileobj, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name in sorted(files):
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode, info.uid, info.gid = len(files[name]), 0, 0o644, 0, 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(files[name]))


def read_metrics_csv(fileobj, symbol: str, month: str) -> list[MetricsRow]:
    """Strict reader of a metrics csv.gz (gzip binary or text): header, increasing timestamps inside the month."""
    data_lake.validate_symbol(symbol)
    first_ms, end_ms, _ = data_lake.month_bounds_ms(month)
    reader = csv.reader(_text(fileobj))
    header = next(reader, None)
    if header is None or tuple(header) != CSV_COLUMNS:
        raise MetricsFormatError(f"{symbol} {month} metrics: header differs from {','.join(CSV_COLUMNS)}")
    rows, previous = [], None
    for line, values in enumerate(reader, start=1):
        if len(values) != len(CSV_COLUMNS) or not values[0].isdigit():
            raise MetricsFormatError(f"{symbol} {month} metrics line {line}: malformed row")
        stamp = int(values[0])
        if (previous is not None and stamp <= previous) or not first_ms <= stamp <= end_ms:
            raise MetricsFormatError(f"{symbol} {month} metrics line {line}: timestamp out of order or month")
        previous = stamp
        rows.append(MetricsRow(stamp, tuple(values[1:])))
    return rows


def load_symbol_metrics(metrics_dir, symbol: str, first_month: str, last_month: str, *, token=None,
                        gate=None) -> list[MetricsRow]:
    """Rows of ``first_month..last_month`` (inclusive), hidden months authorized before any file is opened.

    Consumers must use a row only from ``usable_from_ms(row.create_time_ms)`` on.
    """
    data_lake.validate_symbol(symbol)
    months = data_lake.months_between(first_month, last_month)
    require_months(months, token, gate)  # all months, before opening any file
    rows = []
    for month in months:
        with (Path(metrics_dir) / csv_asset_name(symbol, month)).open("rb") as stream:
            rows.extend(read_metrics_csv(stream, symbol, month))
    return rows


# ---------------------------------------------------------------- range loader (positioning screen, #188)


@dataclass(frozen=True)
class MetricsSeries:
    """5-minute metrics on a contiguous grid indexed by ``usable_from_ms``.

    Index ``i`` holds the row with ``usable_from_ms == start_ms + i * PERIOD_MS``; a period without a
    row, or an empty published value, is NaN (the float MISSING) in that column, never filled.
    ``index_at(t)`` is the latest period usable at decision time ``t`` (``usable_from <= t``).
    """

    symbol: str
    start_ms: int
    periods: int
    columns: dict  # VALUE_COLUMNS name -> numpy float array

    def index_at(self, time_ms: int) -> int | None:
        index = (time_ms - self.start_ms) // PERIOD_MS
        return index if 0 <= index < self.periods else None


def load_symbol_metrics_range(metrics_dir, symbol: str, first_month: str, last_month: str, *, token=None,
                              gate=None) -> MetricsSeries:
    """Guarded loader (hidden months need a verified token before any file is opened) -> ``MetricsSeries``.

    The grid starts at the first month's start + one period (the usable time of a row stamped at
    00:00) and ends at the usable time of a row stamped at the end of the last month. A row whose
    usable time is off the 5-minute grid raises ``MetricsFormatError``.
    """
    import numpy as np

    rows = load_symbol_metrics(metrics_dir, symbol, first_month, last_month, token=token, gate=gate)
    start = data_lake.month_bounds_ms(first_month)[0] + POINT_IN_TIME_LAG_PERIODS * PERIOD_MS
    end = data_lake.month_bounds_ms(last_month)[1] + POINT_IN_TIME_LAG_PERIODS * PERIOD_MS
    periods = (end - start) // PERIOD_MS + 1
    columns = {name: np.full(periods, np.nan) for name in VALUE_COLUMNS}
    for row in rows:
        offset = usable_from_ms(row.create_time_ms) - start
        if offset % PERIOD_MS:
            raise MetricsFormatError(f"{symbol} metrics row at {row.create_time_ms} is off the 5-minute grid")
        index = offset // PERIOD_MS
        if not 0 <= index < periods:
            continue
        for name, text in zip(VALUE_COLUMNS, row.values):
            if text != "":
                columns[name][index] = float(data_lake.exact_decimal(text))
    return MetricsSeries(symbol, start, periods, columns)
