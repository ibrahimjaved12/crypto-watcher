"""Verified local Binance USD-M daily archives for Issue #35 historical replay.

Archive exchange timestamps are availability surrogates, not historical socket
receive times. A LIVE interval asserts verified dataset coverage, not collector
health. No acquisition or replay calculation occurs in this adapter.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import zipfile

from .historical_replay import (
    HistoricalReplayConfig, HistoricalReplayDatasetManifest,
    HistoricalReplayInstrument, HistoricalReplayMovementCandle,
    HistoricalReplayRequest, HistoricalReplaySourceInterval,
    HistoricalReplayTrade,
)
from .historical_ohlc_evidence import (
    BinanceTradeOHLCEvidence, CompletedTradeOHLCCandle,
)
from .movement import MovementBucketEngine
from .movement_history import MINUTE_MS
from .movement_metrics import MarketUniverseInput, WINDOWS


BINANCE_ARCHIVE_ADAPTER_VERSION = "binance-usdm-daily-archive-adapter-v1"
BINANCE_ARCHIVE_DATASET_ID = "binance-public-data-usdm"
BINANCE_ARCHIVE_DATASET_VERSION = "binance-usdm-daily-archive-v1"
ARCHIVE_FIRST_SEEN_POLICY = "exchange-timestamp-surrogate-v1"
ARCHIVE_SOURCE_STATE_POLICY = "verified-archive-coverage-live-v1"

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_SHA256_LINE = re.compile(r"([0-9a-fA-F]{64})\s+\*?([^\s]+)")
_AGG_HEADER = (
    {"agg_trade_id", "aggregate_trade_id", "aggtradeid"},
    {"price"}, {"quantity", "qty"}, {"first_trade_id", "firsttradeid"},
    {"last_trade_id", "lasttradeid"},
    {"transact_time", "timestamp", "timestamp_ms", "time"},
    {"is_buyer_maker", "buyer_is_maker"},
)
_KLINE_HEADER = (
    {"open_time", "opentime"}, {"open"}, {"high"}, {"low"}, {"close"},
    {"volume"}, {"close_time", "closetime"},
    {"quote_asset_volume", "quote_volume", "quoteassetvolume"},
    {"number_of_trades", "count", "numberoftrades"},
    {"taker_buy_base_asset_volume", "taker_buy_base_volume", "taker_buy_volume"},
    {"taker_buy_quote_asset_volume", "taker_buy_quote_volume"}, {"ignore"},
)


class BinanceArchiveCoverageError(ValueError):
    """A required local daily ZIP or its checksum is absent."""


def _symbol(symbol: str) -> str:
    if (not isinstance(symbol, str) or not symbol or symbol != symbol.upper()
            or "_" in symbol or not symbol.isascii()
            or not symbol.isalnum()):
        raise ValueError("V1 requires uppercase USD-M perpetual symbols without underscores")
    return symbol


def _date(value: date) -> date:
    if type(value) is not date:
        raise ValueError("UTC archive date must be datetime.date")
    return value


def _utc_date(timestamp_ms: int) -> date:
    if type(timestamp_ms) is not int or timestamp_ms < 0:
        raise ValueError("UTC timestamp must be nonnegative integer milliseconds")
    try:
        return (_EPOCH + timedelta(milliseconds=timestamp_ms)).date()
    except OverflowError as exc:
        raise ValueError("UTC timestamp exceeds datetime range") from exc


def _dates(start_ms: int, end_ms: int) -> tuple[date, ...]:
    first, last = _utc_date(start_ms), _utc_date(end_ms)
    if first > last:
        raise ValueError("archive date interval is reversed")
    return tuple(first + timedelta(days=offset) for offset in range((last - first).days + 1))


def required_aggtrade_dates(config: HistoricalReplayConfig) -> tuple[date, ...]:
    if not isinstance(config, HistoricalReplayConfig):
        raise ValueError("config must be HistoricalReplayConfig")
    return _dates(config.engine_start_boundary_time_ms,
                  config.output_end_boundary_time_ms)


def historical_candle_start_ms(config: HistoricalReplayConfig) -> int:
    if not isinstance(config, HistoricalReplayConfig):
        raise ValueError("config must be HistoricalReplayConfig")
    start = (config.output_start_boundary_time_ms
             - config.movement_config.historical_lookback_ms
             - (max(WINDOWS) + 1) * MINUTE_MS)
    if start < 0:
        raise ValueError("historical candle start must be nonnegative")
    return start


def required_kline_dates(config: HistoricalReplayConfig) -> tuple[date, ...]:
    return _dates(historical_candle_start_ms(config),
                  config.output_end_boundary_time_ms)


def daily_aggtrades_relative_path(symbol: str, utc_date: date) -> PurePosixPath:
    symbol, utc_date = _symbol(symbol), _date(utc_date)
    return PurePosixPath("data", "futures", "um", "daily", "aggTrades", symbol,
                         f"{symbol}-aggTrades-{utc_date.isoformat()}.zip")


def daily_kline_relative_path(symbol: str, utc_date: date,
                              interval: str = "1m") -> PurePosixPath:
    symbol, utc_date = _symbol(symbol), _date(utc_date)
    if interval != "1m":
        raise ValueError("V1 supports only 1m klines")
    return PurePosixPath("data", "futures", "um", "daily", "klines", symbol,
                         "1m", f"{symbol}-1m-{utc_date.isoformat()}.zip")


def daily_aggtrades_checksum_relative_path(symbol: str, utc_date: date) -> PurePosixPath:
    return PurePosixPath(f"{daily_aggtrades_relative_path(symbol, utc_date)}.CHECKSUM")


def daily_kline_checksum_relative_path(symbol: str, utc_date: date,
                                       interval: str = "1m") -> PurePosixPath:
    return PurePosixPath(f"{daily_kline_relative_path(symbol, utc_date, interval)}.CHECKSUM")


@dataclass(frozen=True)
class BinanceUSDMArchiveRequest:
    archive_root: Path
    universe: MarketUniverseInput
    replay_config: HistoricalReplayConfig

    def __post_init__(self):
        try:
            root = Path(self.archive_root).expanduser().resolve()
        except (TypeError, ValueError, OSError) as exc:
            raise ValueError("archive_root must be a filesystem path") from exc
        object.__setattr__(self, "archive_root", root)
        if (not isinstance(self.universe, MarketUniverseInput)
                or not self.universe.symbols
                or not isinstance(self.replay_config, HistoricalReplayConfig)):
            raise ValueError("archive request requires a nonempty universe and replay config")
        for symbol in self.universe.symbols:
            _symbol(symbol)


@dataclass(frozen=True)
class BinanceArchiveFileIdentity:
    relative_path: str
    data_type: str
    symbol: str
    utc_date: date
    sha256: str


@dataclass(frozen=True)
class BinanceArchiveBundleManifest:
    adapter_version: str
    dataset_id: str
    dataset_version: str
    first_seen_policy: str
    source_state_policy: str
    archive_files: tuple[BinanceArchiveFileIdentity, ...]
    content_sha256: str


@dataclass(frozen=True)
class BinanceArchiveDiagnostics:
    archive_file_count: int
    verified_archive_file_count: int
    aggtrade_archive_count: int
    kline_archive_count: int
    aggtrade_row_count: int
    kline_row_count: int
    duplicate_aggtrade_count: int
    missing_kline_minute_count: int
    symbols_with_kline_gaps: tuple[str, ...]
    earliest_trade_time_ms: int | None
    latest_trade_time_ms: int | None
    earliest_kline_open_time_ms: int | None
    latest_kline_open_time_ms: int | None


@dataclass(frozen=True)
class BinanceHistoricalReplayDataset:
    archive_manifest: BinanceArchiveBundleManifest
    replay_request: HistoricalReplayRequest
    diagnostics: BinanceArchiveDiagnostics
    ohlc_evidence: BinanceTradeOHLCEvidence


@dataclass(frozen=True)
class _AggRow:
    aggregate_trade_id: int
    price: Decimal
    quantity: Decimal
    first_trade_id: int
    last_trade_id: int
    timestamp_ms: int
    buyer_is_maker: bool


@dataclass(frozen=True)
class _KlineRow:
    open_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    close_time_ms: int
    quote_volume: Decimal
    number_of_trades: int
    taker_buy_base_volume: Decimal
    taker_buy_quote_volume: Decimal
    ignore: Decimal


def _integer(value: str, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a nonnegative integer") from exc
    if number < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return number


def _decimal(value: str, name: str, *, positive: bool) -> Decimal:
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be finite numeric data") from exc
    if not number.is_finite() or (number <= 0 if positive else number < 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return number


def _agg_row(row: list[str], utc_date: date) -> _AggRow:
    if len(row) != 7:
        raise ValueError("aggTrade row must have seven columns")
    aggregate_id = _integer(row[0], "aggregate_trade_id")
    price = _decimal(row[1], "price", positive=True)
    quantity = _decimal(row[2], "quantity", positive=True)
    first_id = _integer(row[3], "first_trade_id")
    last_id = _integer(row[4], "last_trade_id")
    if last_id < first_id:
        raise ValueError("last_trade_id precedes first_trade_id")
    timestamp = _integer(row[5], "timestamp_ms")
    if _utc_date(timestamp) != utc_date:
        raise ValueError("aggTrade timestamp is outside archive UTC date")
    maker = row[6].strip().lower()
    if maker not in ("true", "false"):
        raise ValueError("buyer_is_maker must be true or false")
    return _AggRow(aggregate_id, price, quantity, first_id, last_id,
                   timestamp, maker == "true")


def _kline_row(row: list[str], utc_date: date) -> _KlineRow:
    if len(row) != 12:
        raise ValueError("kline row must have twelve columns")
    opening = _integer(row[0], "open_time")
    open_price = _decimal(row[1], "open", positive=True)
    high = _decimal(row[2], "high", positive=True)
    low = _decimal(row[3], "low", positive=True)
    close = _decimal(row[4], "close", positive=True)
    volume = _decimal(row[5], "volume", positive=False)
    closing = _integer(row[6], "close_time")
    quote_volume = _decimal(row[7], "quote_asset_volume", positive=False)
    count = _integer(row[8], "number_of_trades")
    taker_base = _decimal(row[9], "taker_buy_base_volume", positive=False)
    taker_quote = _decimal(row[10], "taker_buy_quote_volume", positive=False)
    ignore = _decimal(row[11], "ignore", positive=False)
    if _utc_date(opening) != utc_date:
        raise ValueError("kline open_time is outside archive UTC date")
    if opening % MINUTE_MS or closing != opening + MINUTE_MS - 1:
        raise ValueError("kline must be an aligned, complete 1m interval")
    if not low <= open_price <= high or not low <= close <= high:
        raise ValueError("kline OHLC prices are inconsistent")
    return _KlineRow(opening, open_price, high, low, close, volume, closing,
                     quote_volume, count, taker_base, taker_quote, ignore)


def _is_header(row: list[str], schema: tuple[set[str], ...]) -> bool:
    return (len(row) == len(schema) and all(cell.strip().lower() in names
                                            for cell, names in zip(row, schema)))


def _checksum(root: Path, relative: PurePosixPath, symbol: str,
              family: str, utc_date: date) -> str:
    archive = root.joinpath(*relative.parts)
    checksum = Path(f"{archive}.CHECKSUM")
    for path in (archive, checksum):
        if not path.is_file():
            suffix = "" if path == archive else ".CHECKSUM"
            raise BinanceArchiveCoverageError(
                f"missing {family} archive for {symbol} {utc_date}: {relative}{suffix}")
    try:
        lines = [line.strip() for line in checksum.read_text(encoding="utf-8").splitlines()
                 if line.strip()]
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read checksum for {relative}") from exc
    if len(lines) != 1:
        raise ValueError(f"checksum for {relative} must have one entry")
    match = _SHA256_LINE.fullmatch(lines[0])
    if match is None or match.group(2) != relative.name:
        raise ValueError(f"checksum for {relative} must name {relative.name}")
    expected = match.group(1).lower()
    digest = hashlib.sha256()
    try:
        with archive.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ValueError(f"cannot read archive {relative}") from exc
    actual = digest.hexdigest()
    if actual != expected:
        raise ValueError(f"checksum mismatch for {relative}: expected {expected}, actual {actual}")
    return actual


def _archive_rows(root: Path, relative: PurePosixPath, schema: tuple[set[str], ...],
                  parser, utc_date: date) -> tuple:
    archive = root.joinpath(*relative.parts)
    try:
        with zipfile.ZipFile(archive) as bundle:
            csv_members = []
            for member in bundle.infolist():
                name = member.filename.replace("\\", "/")
                path = PurePosixPath(name)
                if (member.flag_bits & 1 or path.is_absolute() or ".." in path.parts
                        or PureWindowsPath(member.filename).drive):
                    raise ValueError(f"unsafe ZIP member in {relative}: {member.filename}")
                if not member.is_dir() and path.suffix.lower() == ".csv":
                    csv_members.append(member)
            expected_name = f"{relative.stem}.csv"
            if len(csv_members) != 1 or PurePosixPath(csv_members[0].filename).name != expected_name:
                raise ValueError(f"{relative} must contain exactly one {expected_name} CSV")
            with bundle.open(csv_members[0]) as raw:
                with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text_stream:
                    reader = csv.reader(text_stream, strict=True)
                    rows = []
                    try:
                        for number, row in enumerate(reader, start=1):
                            if number == 1 and _is_header(row, schema):
                                continue
                            try:
                                rows.append(parser(row, utc_date))
                            except ValueError as exc:
                                raise ValueError(f"{relative} CSV row {number}: {exc}") from exc
                    except (csv.Error, UnicodeError) as exc:
                        raise ValueError(f"{relative} CSV row {reader.line_num}: {exc}") from exc
                    return tuple(rows)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ValueError(f"invalid ZIP archive {relative}: {exc}") from exc


def _content_sha256(files: tuple[BinanceArchiveFileIdentity, ...]) -> str:
    value = {
        "adapter_version": BINANCE_ARCHIVE_ADAPTER_VERSION,
        "dataset_version": BINANCE_ARCHIVE_DATASET_VERSION,
        "first_seen_policy": ARCHIVE_FIRST_SEEN_POLICY,
        "source_state_policy": ARCHIVE_SOURCE_STATE_POLICY,
        "archive_files": [[item.relative_path, item.sha256] for item in files],
    }
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _check_ohlc_movement_parity(ohlc: CompletedTradeOHLCCandle,
                                movement: HistoricalReplayMovementCandle) -> None:
    if ((ohlc.symbol, ohlc.open_time_ms, ohlc.close, ohlc.first_seen_at_ms)
            != (movement.symbol, movement.open_time_ms, movement.close,
                movement.first_seen_at_ms)):
        raise ValueError("OHLC evidence disagrees with the close-only movement candle")


def load_binance_usdm_historical_replay_dataset(
    request: BinanceUSDMArchiveRequest,
) -> BinanceHistoricalReplayDataset:
    """Verify exact daily packages and build an immutable Part 1 request."""
    if not isinstance(request, BinanceUSDMArchiveRequest):
        raise ValueError("request must be BinanceUSDMArchiveRequest")
    config = request.replay_config
    candle_start = historical_candle_start_ms(config)
    trade_dates = required_aggtrade_dates(config)
    kline_dates = required_kline_dates(config)
    files = []
    trades = []
    candles = []
    ohlc_candles = []
    gap_symbols = []
    missing_minutes = duplicate_trades = trade_rows = kline_rows = 0
    earliest_trade = latest_trade = earliest_kline = latest_kline = None
    for symbol in request.universe.symbols:
        trade_by_id = {}
        kline_by_open = {}
        for family, dates, path_builder, schema, parser in (
            ("aggTrades", trade_dates, daily_aggtrades_relative_path, _AGG_HEADER, _agg_row),
            ("klines/1m", kline_dates, daily_kline_relative_path, _KLINE_HEADER, _kline_row),
        ):
            for day in dates:
                relative = path_builder(symbol, day)
                sha256 = _checksum(request.archive_root, relative, symbol, family, day)
                files.append(BinanceArchiveFileIdentity(
                    relative.as_posix(), family, symbol, day, sha256))
                rows = _archive_rows(request.archive_root, relative, schema, parser, day)
                if family == "aggTrades":
                    trade_rows += len(rows)
                    for row in rows:
                        earliest_trade = (row.timestamp_ms if earliest_trade is None
                                          else min(earliest_trade, row.timestamp_ms))
                        latest_trade = (row.timestamp_ms if latest_trade is None
                                        else max(latest_trade, row.timestamp_ms))
                        prior = trade_by_id.get(row.aggregate_trade_id)
                        if prior is not None:
                            if prior != row:
                                raise ValueError(f"conflicting aggTrade ID {row.aggregate_trade_id} for {symbol}")
                            duplicate_trades += 1
                        else:
                            trade_by_id[row.aggregate_trade_id] = row
                else:
                    kline_rows += len(rows)
                    for row in rows:
                        earliest_kline = (row.open_time_ms if earliest_kline is None
                                          else min(earliest_kline, row.open_time_ms))
                        latest_kline = (row.open_time_ms if latest_kline is None
                                        else max(latest_kline, row.open_time_ms))
                        prior = kline_by_open.get(row.open_time_ms)
                        if prior is not None and prior != row:
                            raise ValueError(f"conflicting kline open {row.open_time_ms} for {symbol}")
                        kline_by_open[row.open_time_ms] = row
        for row in sorted(trade_by_id.values(), key=lambda item: (item.timestamp_ms,
                                                                    item.aggregate_trade_id)):
            if (MovementBucketEngine._bucket_boundary(row.timestamp_ms)
                    >= config.engine_start_boundary_time_ms
                    and row.timestamp_ms <= config.output_end_boundary_time_ms):
                trades.append(HistoricalReplayTrade(
                    symbol, f"binance-usdm:{symbol}", row.price, row.quantity,
                    row.timestamp_ms, row.timestamp_ms, row.aggregate_trade_id,
                    row.timestamp_ms))
        openings = sorted(kline_by_open)
        gaps = sum((right - left) // MINUTE_MS - 1
                   for left, right in zip(openings, openings[1:]))
        missing_minutes += gaps
        if gaps:
            gap_symbols.append(symbol)
        for opening in openings:
            row = kline_by_open[opening]
            if candle_start <= opening <= config.output_end_boundary_time_ms:
                movement = HistoricalReplayMovementCandle(
                    symbol, opening, row.close, row.volume, row.quote_volume,
                    row.close_time_ms + 1)
                ohlc = CompletedTradeOHLCCandle(
                    symbol, f"binance-usdm:{symbol}", opening, row.close_time_ms,
                    row.open, row.high, row.low, row.close, row.close_time_ms + 1)
                _check_ohlc_movement_parity(ohlc, movement)
                candles.append(movement)
                ohlc_candles.append(ohlc)
    sorted_files = tuple(sorted(files, key=lambda item: item.relative_path))
    content_sha256 = _content_sha256(sorted_files)
    manifest = BinanceArchiveBundleManifest(
        BINANCE_ARCHIVE_ADAPTER_VERSION, BINANCE_ARCHIVE_DATASET_ID,
        BINANCE_ARCHIVE_DATASET_VERSION, ARCHIVE_FIRST_SEEN_POLICY,
        ARCHIVE_SOURCE_STATE_POLICY, sorted_files, content_sha256)
    ohlc_evidence = BinanceTradeOHLCEvidence(
        BINANCE_ARCHIVE_DATASET_ID, BINANCE_ARCHIVE_DATASET_VERSION,
        content_sha256, request.universe.symbols, tuple(ohlc_candles))
    instruments = tuple(HistoricalReplayInstrument(
        symbol, f"binance-usdm:{symbol}", True) for symbol in request.universe.symbols)
    intervals = tuple(HistoricalReplaySourceInterval(
        symbol, config.engine_start_boundary_time_ms,
        config.output_end_boundary_time_ms, "LIVE") for symbol in request.universe.symbols)
    replay_request = HistoricalReplayRequest(
        HistoricalReplayDatasetManifest(BINANCE_ARCHIVE_DATASET_ID,
                                        BINANCE_ARCHIVE_DATASET_VERSION, content_sha256),
        request.universe, instruments, tuple(trades), tuple(candles), intervals, config)
    diagnostics = BinanceArchiveDiagnostics(
        len(sorted_files), len(sorted_files), len(trade_dates) * len(request.universe.symbols),
        len(kline_dates) * len(request.universe.symbols), trade_rows, kline_rows,
        duplicate_trades, missing_minutes, tuple(gap_symbols),
        earliest_trade, latest_trade, earliest_kline, latest_kline,
    )
    return BinanceHistoricalReplayDataset(
        manifest, replay_request, diagnostics, ohlc_evidence)
