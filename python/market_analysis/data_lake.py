"""Research data lake (#183): exact 1-minute bars built from Binance aggTrades.

Pure, standard-library logic shared by ``scripts/research/data_lake_build.py`` and
its tests. Binance 1m klines lose trades in rare incident windows and assign some
trades to the neighbouring minute, so bars are built from aggTrades; kline,
mark, index and premium values are kept only as cross-check columns with flags.
Nothing is forward-filled or repaired: differences are marked.

All stored numbers are exact decimal strings. Prices and quantities are parsed
to integers at scale 10**8 (quote amounts at 10**16); no floats are used.
"""
from __future__ import annotations

import calendar
import csv
import gzip
import io
import json
import re
import zipfile
from typing import Iterable, Iterator

LAKE_SCHEMA_VERSION = "bars1m-v1"
MANIFEST_VERSION = "data-lake-manifest-v1"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT")
FIRST_MONTH = "2024-01"
BINANCE_BASE = "https://data.binance.vision/data/futures/um/monthly"
MINUTE_MS = 60_000
SCALE_DIGITS = 8
QUOTE_DIGITS = 2 * SCALE_DIGITS
# Every release asset must be strictly smaller than GitHub's 2 GiB limit.
MAX_ASSET_BYTES = 2 * 1024 ** 3
GZIP_LEVEL = 6

KLINE_FAMILIES = ("klines", "markPriceKlines", "indexPriceKlines", "premiumIndexKlines")
FAMILIES = ("aggTrades", *KLINE_FAMILIES, "fundingRate")
COLUMN_COUNTS = {"aggTrades": 7, **{family: 12 for family in KLINE_FAMILIES}, "fundingRate": 3}

FLAG_NO_AGGTRADES = 1
FLAG_KLINE_ROW_MISSING = 2
FLAG_KLINE_VOLUME_DIFFERS = 4
FLAG_KLINE_ZERO_BUT_TRADES = 8
FLAG_MARK_MISSING = 16
FLAG_INDEX_MISSING = 32
FLAG_PREMIUM_MISSING = 64
FLAG_KLINE_TAKER_DIFFERS = 128
FLAG_BITS = {
    "NO_AGGTRADES": FLAG_NO_AGGTRADES,
    "KLINE_ROW_MISSING": FLAG_KLINE_ROW_MISSING,
    "KLINE_VOLUME_DIFFERS": FLAG_KLINE_VOLUME_DIFFERS,
    "KLINE_ZERO_BUT_TRADES": FLAG_KLINE_ZERO_BUT_TRADES,
    "MARK_MISSING": FLAG_MARK_MISSING,
    "INDEX_MISSING": FLAG_INDEX_MISSING,
    "PREMIUM_MISSING": FLAG_PREMIUM_MISSING,
    "KLINE_TAKER_DIFFERS": FLAG_KLINE_TAKER_DIFFERS,
}

KLINE_CHECK_COLUMNS = ("k_open", "k_high", "k_low", "k_close", "k_volume", "k_quote_volume", "k_trades",
                       "k_taker_buy_volume", "k_taker_buy_quote_volume")
# Source column of each k_* value in a 12-column Binance kline row.
_KLINE_SOURCE_INDEXES = (1, 2, 3, 4, 5, 7, 8, 9, 10)
_OHLC_SOURCE_INDEXES = (1, 2, 3, 4)
_K_VOLUME, _K_TAKER_VOLUME = 4, 7
COLUMNS = (
    "open_time_ms", "open", "high", "low", "close", "volume", "quote_volume", "taker_buy_volume",
    "taker_buy_quote_volume", "agg_rows", "trades", "first_agg_id", "last_agg_id",
    *KLINE_CHECK_COLUMNS,
    *(f"{prefix}_{field}" for prefix in ("mark", "index", "premium")
      for field in ("open", "high", "low", "close")),
    "flags",
)
FUNDING_COLUMNS = ("calc_time_ms", "funding_interval_hours", "last_funding_rate")

_MONTH = re.compile(r"(\d{4})-(0[1-9]|1[0-2])\Z")
_TAG = re.compile(r"rd-(" + "|".join(SYMBOLS) + r")-(\d{4}-(?:0[1-9]|1[0-2]))-r([1-9][0-9]{0,2})\Z")
_DECIMAL = re.compile(r"(-?)([0-9]+)(?:\.([0-9]+))?\Z")
_INTEGER = re.compile(r"-?[0-9]+\Z")
_CHECKSUM = re.compile(r"\s*([0-9a-fA-F]{64})(?:\s|\Z)")
_PAD = tuple("0" * (SCALE_DIGITS - n) for n in range(SCALE_DIGITS + 1))


# ---------------------------------------------------------------- months, names


def validate_month(month: str) -> str:
    if not isinstance(month, str) or not _MONTH.match(month):
        raise ValueError(f"invalid month (expected YYYY-MM): {month!r}")
    return month


def month_bounds_ms(month: str) -> tuple[int, int, int]:
    """(start_ms, end_ms exclusive, days) of a UTC calendar month."""
    year, mon = (int(part) for part in validate_month(month).split("-"))
    days = calendar.monthrange(year, mon)[1]
    start = calendar.timegm((year, mon, 1, 0, 0, 0))
    return start * 1000, (start + days * 86_400) * 1000, days


def month_finished(month: str, now_ms: int) -> bool:
    """True only when the whole month lies before ``now_ms`` (UTC)."""
    return month_bounds_ms(month)[1] <= now_ms


def months_between(first: str, last: str) -> list[str]:
    validate_month(first)
    validate_month(last)
    if first > last:
        raise ValueError(f"start month {first} is after end month {last}")
    year, mon = (int(part) for part in first.split("-"))
    months = []
    while f"{year:04d}-{mon:02d}" <= last:
        months.append(f"{year:04d}-{mon:02d}")
        year, mon = (year + 1, 1) if mon == 12 else (year, mon + 1)
    return months


def validate_symbol(symbol: str) -> str:
    if symbol not in SYMBOLS:
        raise ValueError(f"symbol is not in the frozen six-symbol universe: {symbol!r}")
    return symbol


def release_tag(symbol: str, month: str, revision: int = 1) -> str:
    return validate_tag(f"rd-{validate_symbol(symbol)}-{validate_month(month)}-r{revision}")


def validate_tag(tag: str) -> str:
    if not isinstance(tag, str) or not _TAG.match(tag):
        raise ValueError(f"invalid data lake release tag: {tag!r}")
    return tag


def source_url(family: str, symbol: str, month: str) -> str:
    validate_symbol(symbol)
    validate_month(month)
    if family == "aggTrades":
        return f"{BINANCE_BASE}/aggTrades/{symbol}/{symbol}-aggTrades-{month}.zip"
    if family == "fundingRate":
        return f"{BINANCE_BASE}/fundingRate/{symbol}/{symbol}-fundingRate-{month}.zip"
    if family in KLINE_FAMILIES:
        return f"{BINANCE_BASE}/{family}/{symbol}/1m/{symbol}-1m-{month}.zip"
    raise ValueError(f"unknown source family: {family!r}")


def raw_asset_name(family: str, file_name: str) -> str:
    return f"raw__{family}__{file_name}"


def bars_asset_name(symbol: str, month: str) -> str:
    return f"bars1m__{symbol}__{month}.csv.gz"


def funding_asset_name(symbol: str, month: str) -> str:
    return f"funding__{symbol}__{month}.csv.gz"


def parse_checksum(text: str) -> str:
    """sha256 from a Binance ``.CHECKSUM`` file (first token)."""
    match = _CHECKSUM.match(text)
    if not match:
        raise ValueError("unparseable .CHECKSUM content")
    return match.group(1).lower()


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n"


# ---------------------------------------------------------------- exact decimals


def parse_scaled(text: str, digits: int = SCALE_DIGITS) -> int:
    """Exact integer value of a plain decimal string at scale 10**digits."""
    match = _DECIMAL.match(text)
    if not match:
        raise ValueError(f"invalid decimal: {text!r}")
    sign, whole, fraction = match.groups()
    fraction = fraction or ""
    if len(fraction) > digits:
        raise ValueError(f"more than {digits} fractional digits: {text!r}")
    value = int(whole + fraction.ljust(digits, "0"))
    return -value if sign else value


def _scaled8(text: str) -> int:
    # Hot path for aggTrades; anything unusual goes through the strict parser.
    whole, dot, fraction = text.partition(".")
    if whole.isdigit() and len(fraction) <= SCALE_DIGITS and (fraction.isdigit() if dot else not fraction):
        return int(whole + fraction + _PAD[len(fraction)])
    return parse_scaled(text)


def render_scaled(value: int, digits: int = SCALE_DIGITS) -> str:
    """Canonical decimal string: no exponent, trailing zeros trimmed, "0" for zero."""
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    whole, fraction = divmod(abs(value), 10 ** digits)
    fraction_text = str(fraction).rjust(digits, "0").rstrip("0") if digits else ""
    return f"{sign}{whole}.{fraction_text}" if fraction_text else f"{sign}{whole}"


def canonical_decimal(text: str) -> str:
    """Canonical spelling of a decimal string with any number of fractional digits."""
    match = _DECIMAL.match(text)
    if not match:
        raise ValueError(f"invalid decimal: {text!r}")
    sign, whole, fraction = match.groups()
    whole = whole.lstrip("0") or "0"
    fraction = (fraction or "").rstrip("0")
    if whole == "0" and not fraction:
        return "0"
    return f"{sign}{whole}.{fraction}" if fraction else f"{sign}{whole}"


class ExactSum:
    """Exact sum of decimal strings with any number of fractional digits."""

    def __init__(self) -> None:
        self.value = 0
        self.digits = 0

    def add(self, text: str) -> None:
        match = _DECIMAL.match(text)
        if not match:
            raise ValueError(f"invalid decimal: {text!r}")
        sign, whole, fraction = match.groups()
        fraction = fraction or ""
        value = int(whole + fraction)
        if sign:
            value = -value
        if len(fraction) > self.digits:
            self.value *= 10 ** (len(fraction) - self.digits)
            self.digits = len(fraction)
        else:
            value *= 10 ** (self.digits - len(fraction))
        self.value += value

    def text(self) -> str:
        return render_scaled(self.value, self.digits)


def _integer(text: str, what: str) -> int:
    if not _INTEGER.match(text):
        raise ValueError(f"invalid integer {what}: {text!r}")
    return int(text)


# ---------------------------------------------------------------- streaming CSV


def read_zip_csv(source, columns: int, *, name: str = "archive") -> Iterator[list[str]]:
    """Stream the rows of the single CSV member of a zip (path or binary file object).

    A first line whose first field is not an integer is a header and is skipped.
    Exactly one ``.csv`` member and exactly ``columns`` fields per line are required.
    """
    with zipfile.ZipFile(source) as archive:
        members = [info for info in archive.infolist() if not info.is_dir()]
        if len(members) != 1 or not members[0].filename.lower().endswith(".csv"):
            raise ValueError(f"{name}: expected exactly one .csv member, found "
                             f"{[info.filename for info in members]}")
        with archive.open(members[0]) as raw:
            text = io.TextIOWrapper(raw, encoding="ascii", newline="")
            first = True
            for row in csv.reader(text):
                if not row:
                    continue
                if len(row) != columns:
                    raise ValueError(f"{name}: expected {columns} columns, got {len(row)}")
                if first:
                    first = False
                    if not _INTEGER.match(row[0].strip()):
                        continue  # header line (Binance added headers to some files)
                yield row


# ---------------------------------------------------------------- aggTrades -> minutes


class MinuteBars:
    """Per-minute accumulators for one month of aggTrades (single pass, exact ints)."""

    def __init__(self, month: str) -> None:
        self.month = month
        self.start_ms, self.end_ms, days = month_bounds_ms(month)
        size = days * 1440
        self.minutes = size
        self.open_id: list[int | None] = [None] * size
        self.open_price = [0] * size
        self.close_id = [0] * size
        self.close_price = [0] * size
        self.high = [0] * size
        self.low = [0] * size
        self.volume = [0] * size
        self.quote_volume = [0] * size
        self.taker_buy_volume = [0] * size
        self.taker_buy_quote_volume = [0] * size
        self.agg_rows = [0] * size
        self.trades = [0] * size
        self.rows = 0
        self.id_gaps = 0
        self.id_not_increasing = 0
        self.rows_outside_month = 0
        self._last_id: int | None = None

    def consume(self, rows: Iterable[list[str]]) -> None:
        start, end = self.start_ms, self.end_ms
        open_id, open_price = self.open_id, self.open_price
        close_id, close_price = self.close_id, self.close_price
        high, low = self.high, self.low
        volume, quote_volume = self.volume, self.quote_volume
        taker_volume, taker_quote = self.taker_buy_volume, self.taker_buy_quote_volume
        agg_rows, trades = self.agg_rows, self.trades
        last_id = self._last_id
        count = gaps = not_increasing = outside = 0
        try:
            for row in rows:
                count += 1
                agg_id = int(row[0])
                if last_id is not None:
                    if agg_id <= last_id:
                        not_increasing += 1
                    elif agg_id != last_id + 1:
                        gaps += 1
                if last_id is None or agg_id > last_id:
                    last_id = agg_id
                price = _scaled8(row[1])
                quantity = _scaled8(row[2])
                first_trade, last_trade = int(row[3]), int(row[4])
                transact = int(row[5])
                maker = row[6].strip().lower()
                if maker == "false":
                    taker_buys = True  # buyer was not the maker: the taker bought
                elif maker == "true":
                    taker_buys = False
                else:
                    raise ValueError(f"invalid is_buyer_maker value: {row[6]!r}")
                if transact < start or transact >= end:
                    outside += 1
                    continue
                minute = (transact - transact % MINUTE_MS - start) // MINUTE_MS
                quote = price * quantity
                if agg_rows[minute]:
                    if agg_id < open_id[minute]:
                        open_id[minute], open_price[minute] = agg_id, price
                    if agg_id >= close_id[minute]:
                        close_id[minute], close_price[minute] = agg_id, price
                    if price > high[minute]:
                        high[minute] = price
                    if price < low[minute]:
                        low[minute] = price
                else:
                    open_id[minute] = close_id[minute] = agg_id
                    open_price[minute] = close_price[minute] = high[minute] = low[minute] = price
                volume[minute] += quantity
                quote_volume[minute] += quote
                if taker_buys:
                    taker_volume[minute] += quantity
                    taker_quote[minute] += quote
                agg_rows[minute] += 1
                trades[minute] += last_trade - first_trade + 1
        finally:
            self.rows += count
            self.id_gaps += gaps
            self.id_not_increasing += not_increasing
            self.rows_outside_month += outside
            self._last_id = last_id

    def stats(self) -> dict:
        return {
            "rows": self.rows,
            "id_gaps": self.id_gaps,
            "id_not_increasing": self.id_not_increasing,
            "rows_outside_month": self.rows_outside_month,
            "minutes_without_trades": sum(1 for count in self.agg_rows if not count),
            "total_volume": render_scaled(sum(self.volume)),
            "total_quote_volume": render_scaled(sum(self.quote_volume), QUOTE_DIGITS),
            "total_taker_buy_volume": render_scaled(sum(self.taker_buy_volume)),
            "total_trades": sum(self.trades),
        }


# ---------------------------------------------------------------- kline cross-checks


class KlineIndex:
    """One kline family's in-month rows keyed by open_time_ms (first row wins)."""

    def __init__(self, family: str, month: str) -> None:
        if family not in KLINE_FAMILIES:
            raise ValueError(f"unknown kline family: {family!r}")
        self.family = family
        self.month = month
        self.rows: dict[int, tuple[str, ...]] = {}
        self.raw_rows = 0
        self.duplicates = 0
        self.duplicate_conflicts = 0
        self.rows_outside_month = 0
        self.misaligned = 0
        self._volume = ExactSum()
        self._taker = ExactSum()

    def consume(self, rows: Iterable[list[str]]) -> "KlineIndex":
        start, end, _ = month_bounds_ms(self.month)
        indexes = _KLINE_SOURCE_INDEXES if self.family == "klines" else _OHLC_SOURCE_INDEXES
        for row in rows:
            self.raw_rows += 1
            open_time = _integer(row[0].strip(), "kline open_time")
            if open_time < start or open_time >= end:
                self.rows_outside_month += 1
                continue
            if open_time % MINUTE_MS:
                self.misaligned += 1
                continue
            values = tuple(row[index].strip() for index in indexes)
            existing = self.rows.get(open_time)
            if existing is not None:
                self.duplicates += 1
                if existing != values:
                    self.duplicate_conflicts += 1
                continue
            self.rows[open_time] = values
            if self.family == "klines":
                self._volume.add(values[_K_VOLUME])
                self._taker.add(values[_K_TAKER_VOLUME])
        return self

    def stats(self) -> dict:
        _, _, days = month_bounds_ms(self.month)
        result = {
            "raw_rows": self.raw_rows,
            "rows": len(self.rows),
            "expected_rows": days * 1440,
            "missing_minutes": days * 1440 - len(self.rows),
            "duplicate_rows": self.duplicates,
            "duplicate_conflicts": self.duplicate_conflicts,
            "rows_outside_month": self.rows_outside_month,
            "misaligned_rows": self.misaligned,
        }
        if self.family == "klines":
            result["total_volume"] = self._volume.text()
            result["total_taker_buy_volume"] = self._taker.text()
        return result


def index_klines(rows: Iterable[list[str]], month: str, family: str = "klines") -> KlineIndex:
    return KlineIndex(family, month).consume(rows)


# ---------------------------------------------------------------- output


def _bar_lines(bars: MinuteBars, klines: KlineIndex | None, mark: KlineIndex | None,
               index: KlineIndex | None, premium: KlineIndex | None, flag_counts: dict) -> Iterator[str]:
    yield ",".join(COLUMNS) + "\n"
    empty_k = ("",) * len(KLINE_CHECK_COLUMNS)
    empty_ohlc = ("",) * 4
    kline_rows = klines.rows if klines is not None else {}
    families = ((mark.rows if mark is not None else {}, FLAG_MARK_MISSING),
                (index.rows if index is not None else {}, FLAG_INDEX_MISSING),
                (premium.rows if premium is not None else {}, FLAG_PREMIUM_MISSING))
    bit_counts = {bit: 0 for bit in FLAG_BITS.values()}
    for minute in range(bars.minutes):
        open_time = bars.start_ms + minute * MINUTE_MS
        flags = 0
        volume = render_scaled(bars.volume[minute])
        taker = render_scaled(bars.taker_buy_volume[minute])
        if bars.agg_rows[minute]:
            prices = (render_scaled(bars.open_price[minute]), render_scaled(bars.high[minute]),
                      render_scaled(bars.low[minute]), render_scaled(bars.close_price[minute]))
            ids = (str(bars.open_id[minute]), str(bars.close_id[minute]))
        else:
            prices, ids = empty_ohlc, ("", "")
            flags |= FLAG_NO_AGGTRADES
        kline = kline_rows.get(open_time)
        if kline is None:
            kline = empty_k
            flags |= FLAG_KLINE_ROW_MISSING
        else:
            k_volume = canonical_decimal(kline[_K_VOLUME])
            if k_volume != volume:
                flags |= FLAG_KLINE_VOLUME_DIFFERS
            if k_volume == "0" and bars.volume[minute] > 0:
                flags |= FLAG_KLINE_ZERO_BUT_TRADES
            if canonical_decimal(kline[_K_TAKER_VOLUME]) != taker:
                flags |= FLAG_KLINE_TAKER_DIFFERS
        cross = []
        for rows, missing_bit in families:
            values = rows.get(open_time)
            if values is None:
                values = empty_ohlc
                flags |= missing_bit
            cross.extend(values)
        for bit in bit_counts:
            if flags & bit:
                bit_counts[bit] += 1
        yield ",".join((
            str(open_time), *prices, volume, render_scaled(bars.quote_volume[minute], QUOTE_DIGITS), taker,
            render_scaled(bars.taker_buy_quote_volume[minute], QUOTE_DIGITS),
            str(bars.agg_rows[minute]), str(bars.trades[minute]), *ids, *kline, *cross, str(flags),
        )) + "\n"
    flag_counts.update({name: bit_counts[bit] for name, bit in FLAG_BITS.items()})


def write_csv_gz(fileobj, lines: Iterable[str]) -> int:
    """Deterministic gzip (mtime 0, empty name, fixed level). Returns data rows written."""
    count = 0
    buffer: list[str] = []
    with gzip.GzipFile(filename="", mode="wb", fileobj=fileobj, mtime=0, compresslevel=GZIP_LEVEL) as stream:
        for line in lines:
            buffer.append(line)
            count += 1
            if len(buffer) >= 4096:
                stream.write("".join(buffer).encode("ascii"))
                buffer.clear()
        if buffer:
            stream.write("".join(buffer).encode("ascii"))
    return max(count - 1, 0)  # header excluded


def write_bars_csv_gz(fileobj, bars: MinuteBars, *, klines: KlineIndex | None = None,
                      mark: KlineIndex | None = None, index: KlineIndex | None = None,
                      premium: KlineIndex | None = None) -> dict:
    """Write the bars CSV; returns {'rows', 'flag_counts'}."""
    flag_counts: dict = {}
    rows = write_csv_gz(fileobj, _bar_lines(bars, klines, mark, index, premium, flag_counts))
    return {"rows": rows, "flag_counts": flag_counts}


def write_funding_csv_gz(fileobj, rows: Iterable[list[str]], month: str) -> dict:
    """Funding rows inside the month, as published; the interval is never assumed."""
    start, end, _ = month_bounds_ms(month)
    stats = {"raw_rows": 0, "rows_outside_month": 0, "calc_time_not_increasing": 0, "interval_hours": []}
    intervals = set()

    def lines():
        yield ",".join(FUNDING_COLUMNS) + "\n"
        last = None
        for row in rows:
            stats["raw_rows"] += 1
            calc_time = _integer(row[0].strip(), "funding calc_time")
            interval = row[1].strip()
            _integer(interval, "funding interval hours")
            rate = row[2].strip()
            canonical_decimal(rate)  # validates the published spelling; stored as published
            if calc_time < start or calc_time >= end:
                stats["rows_outside_month"] += 1
                continue
            if last is not None and calc_time <= last:
                stats["calc_time_not_increasing"] += 1
            last = calc_time if last is None else max(last, calc_time)
            intervals.add(interval)
            yield f"{calc_time},{interval},{rate}\n"

    stats["rows"] = write_csv_gz(fileobj, lines())
    stats["interval_hours"] = sorted(intervals, key=int)
    return stats
