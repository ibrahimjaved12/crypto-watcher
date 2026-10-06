"""Verified local Binance USD-M daily archives for Issue #35 historical replay.

Archive exchange timestamps are availability surrogates, not historical socket
receive times. A LIVE interval asserts verified dataset coverage, not collector
health. No acquisition or replay calculation occurs in this adapter.
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
import sqlite3
import tempfile
import time
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
from .historical_taker_flow_evidence import (
    HistoricalTakerFlowEvidence, HistoricalTakerFlowEvidenceBuilder,
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
    taker_flow_evidence: HistoricalTakerFlowEvidence | None = None

    def __post_init__(self):
        archive_identity = (
            self.archive_manifest.dataset_id,
            self.archive_manifest.dataset_version,
            self.archive_manifest.content_sha256,
        )
        replay_identity = (
            self.replay_request.dataset.dataset_id,
            self.replay_request.dataset.dataset_version,
            self.replay_request.dataset.content_sha256,
        )
        ohlc_identity = (
            self.ohlc_evidence.dataset_id,
            self.ohlc_evidence.dataset_version,
            self.ohlc_evidence.dataset_content_sha256,
        )
        if ohlc_identity != archive_identity or ohlc_identity != replay_identity:
            raise ValueError(
                "OHLC evidence dataset identity must match the archive manifest "
                "and replay request dataset"
            )
        if (self.ohlc_evidence.configured_symbols
                != self.replay_request.universe.symbols):
            raise ValueError(
                "OHLC evidence configured symbols must match replay universe "
                "symbols in order"
            )
        flow = self.taker_flow_evidence
        if flow is not None:
            if not flow.matches_dataset(*archive_identity,
                                        self.replay_request.universe.symbols):
                raise ValueError(
                    "taker flow evidence dataset identity and ordered symbols "
                    "must match archive manifest"
                )
            config = self.replay_request.config
            if ((flow.engine_start_boundary_time_ms,
                 flow.output_end_boundary_time_ms, flow.finalization_grace_ms)
                    != (config.engine_start_boundary_time_ms,
                        config.output_end_boundary_time_ms,
                        config.finalization_grace_ms)):
                raise ValueError("taker flow evidence replay range or grace mismatch")


TRADE_STREAM_ORDERING_VERSION = "replay-availability-symbol-time-numeric-id-v1"
TRADE_STREAM_DUPLICATE_VERSION = "symbol-aggregate-id-exact-factual-row-v1"


@dataclass(frozen=True)
class HistoricalReplayTradeStreamManifest:
    dataset_id: str
    dataset_version: str
    archive_content_sha256: str
    configured_symbols: tuple[str, ...]
    adapter_version: str
    ordering_policy_version: str
    duplicate_policy_version: str
    unique_replayable_row_count: int
    duplicate_row_count: int
    first_canonical_trade_key: tuple | None
    last_canonical_trade_key: tuple | None
    normalized_row_stream_sha256: str


@dataclass(frozen=True)
class BinanceBoundedHistoricalReplayDataset:
    archive_manifest: BinanceArchiveBundleManifest
    trade_stream_manifest: HistoricalReplayTradeStreamManifest
    dataset: HistoricalReplayDatasetManifest
    universe: MarketUniverseInput
    instruments: tuple[HistoricalReplayInstrument, ...]
    source_intervals: tuple[HistoricalReplaySourceInterval, ...]
    candles: tuple[HistoricalReplayMovementCandle, ...]
    config: HistoricalReplayConfig
    diagnostics: BinanceArchiveDiagnostics
    ohlc_evidence: BinanceTradeOHLCEvidence
    taker_flow_evidence: HistoricalTakerFlowEvidence
    _index: _AggTradeDuplicateIndex = field(compare=False, repr=False)

    def __post_init__(self):
        identity = (self.archive_manifest.dataset_id,
                    self.archive_manifest.dataset_version,
                    self.archive_manifest.content_sha256)
        if (identity != (self.dataset.dataset_id, self.dataset.dataset_version,
                         self.dataset.content_sha256)
                or identity != (self.ohlc_evidence.dataset_id,
                                self.ohlc_evidence.dataset_version,
                                self.ohlc_evidence.dataset_content_sha256)
                or identity != (self.trade_stream_manifest.dataset_id,
                                self.trade_stream_manifest.dataset_version,
                                self.trade_stream_manifest.archive_content_sha256)
                or self.trade_stream_manifest.configured_symbols != self.universe.symbols
                or self.ohlc_evidence.configured_symbols != self.universe.symbols
                or not self.taker_flow_evidence.matches_dataset(*identity,
                                                                self.universe.symbols)
                or tuple(item.symbol for item in self.instruments) != self.universe.symbols):
            raise ValueError("bounded archive, replay, OHLC, and flow identities differ")

    def iter_trades(self):
        return self._index.iter_replay_trades(self.universe.symbols)

    def close(self):
        self._index.__exit__(None, None, None)


@dataclass(frozen=True)
class BinanceHistoricalCoreArchiveEvidence:
    """Verified archive facts needed for study eligibility, without replay rows."""

    archive_manifest: BinanceArchiveBundleManifest
    ohlc_evidence: BinanceTradeOHLCEvidence
    raw_replayable_trade_evidence_present: bool

    def __post_init__(self):
        if (not isinstance(self.archive_manifest, BinanceArchiveBundleManifest)
                or not isinstance(self.ohlc_evidence, BinanceTradeOHLCEvidence)
                or self.raw_replayable_trade_evidence_present is not True):
            raise ValueError("core archive evidence requires verified replayable trade data")
        manifest = self.archive_manifest
        evidence = self.ohlc_evidence
        if (manifest.content_sha256 != _content_sha256(manifest.archive_files)
                or (evidence.dataset_id, evidence.dataset_version,
                    evidence.dataset_content_sha256)
                != (manifest.dataset_id, manifest.dataset_version,
                    manifest.content_sha256)):
            raise ValueError("core archive evidence identities must match verified packages")


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


def _iter_archive_rows(root: Path, relative: PurePosixPath,
                       schema: tuple[set[str], ...], parser,
                       utc_date: date):
    """Yield validated CSV rows without retaining a complete archive in memory."""
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
                    try:
                        for number, row in enumerate(reader, start=1):
                            if number == 1 and _is_header(row, schema):
                                continue
                            try:
                                yield parser(row, utc_date)
                            except ValueError as exc:
                                raise ValueError(f"{relative} CSV row {number}: {exc}") from exc
                    except (csv.Error, UnicodeError) as exc:
                        raise ValueError(f"{relative} CSV row {reader.line_num}: {exc}") from exc
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ValueError(f"invalid ZIP archive {relative}: {exc}") from exc


def _archive_rows(root: Path, relative: PurePosixPath, schema: tuple[set[str], ...],
                  parser, utc_date: date) -> tuple:
    """Materializing compatibility wrapper for replay callers."""
    return tuple(_iter_archive_rows(root, relative, schema, parser, utc_date))


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
    *, include_taker_flow_evidence: bool = False,
) -> BinanceHistoricalReplayDataset:
    """Verify exact daily packages and build an immutable Part 1 request."""
    if not isinstance(request, BinanceUSDMArchiveRequest):
        raise ValueError("request must be BinanceUSDMArchiveRequest")
    if type(include_taker_flow_evidence) is not bool:
        raise ValueError("include_taker_flow_evidence must be a boolean")
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
    flow_builder = (HistoricalTakerFlowEvidenceBuilder(
        dataset_id=BINANCE_ARCHIVE_DATASET_ID,
        dataset_version=BINANCE_ARCHIVE_DATASET_VERSION,
        dataset_content_sha256=None,
        configured_symbols=request.universe.symbols,
        engine_start_boundary_time_ms=config.engine_start_boundary_time_ms,
        output_end_boundary_time_ms=config.output_end_boundary_time_ms,
        finalization_grace_ms=config.finalization_grace_ms,
    ) if include_taker_flow_evidence else None)
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
                            if flow_builder is not None:
                                flow_builder.add_trade(
                                    symbol, row.timestamp_ms, row.timestamp_ms,
                                    row.price, row.quantity, row.buyer_is_maker)
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
    taker_flow_evidence = (flow_builder.build(
        dataset_id=BINANCE_ARCHIVE_DATASET_ID,
        dataset_version=BINANCE_ARCHIVE_DATASET_VERSION,
        dataset_content_sha256=content_sha256,
    ) if flow_builder is not None else None)
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
        manifest, replay_request, diagnostics, ohlc_evidence,
        taker_flow_evidence)


def _decimal_identity(value: Decimal) -> str:
    """Canonical exact Decimal identity, without applying context rounding."""
    sign, digits, exponent = value.as_tuple()
    digits = list(digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    return f"{sign}:{''.join(str(digit) for digit in digits)}:{exponent}"


def load_binance_usdm_bounded_historical_replay_dataset(
    request: BinanceUSDMArchiveRequest,
) -> BinanceBoundedHistoricalReplayDataset:
    """Verify local packages while indexing only replayable trades on disk."""
    if not isinstance(request, BinanceUSDMArchiveRequest):
        raise ValueError("request must be BinanceUSDMArchiveRequest")
    config = request.replay_config
    candle_start = historical_candle_start_ms(config)
    trade_dates = required_aggtrade_dates(config)
    kline_dates = required_kline_dates(config)
    files, candles, ohlc_candles, gap_symbols = [], [], [], []
    missing_minutes = duplicate_trades = trade_rows = kline_rows = replayable = 0
    earliest_trade = latest_trade = earliest_kline = latest_kline = None
    flow_builder = HistoricalTakerFlowEvidenceBuilder(
        dataset_id=BINANCE_ARCHIVE_DATASET_ID,
        dataset_version=BINANCE_ARCHIVE_DATASET_VERSION,
        dataset_content_sha256=None,
        configured_symbols=request.universe.symbols,
        engine_start_boundary_time_ms=config.engine_start_boundary_time_ms,
        output_end_boundary_time_ms=config.output_end_boundary_time_ms,
        finalization_grace_ms=config.finalization_grace_ms)
    from . import historical_operational_events as events
    events.emit("STARTED", stage="sqlite-preparation")
    preparation_started = time.monotonic()
    def observed_rows(relative, schema, parser, day):
        with events.span("archive-parsing"):
            yield from _iter_archive_rows(request.archive_root, relative, schema, parser, day)
    index = _AggTradeDuplicateIndex(replay=True)
    index.__enter__()
    try:
        for symbol_index, symbol in enumerate(request.universe.symbols):
            kline_by_open = {}
            for family, dates, path_builder, schema, parser in (
                ("aggTrades", trade_dates, daily_aggtrades_relative_path,
                 _AGG_HEADER, _agg_row),
                ("klines/1m", kline_dates, daily_kline_relative_path,
                 _KLINE_HEADER, _kline_row),
            ):
                for day in dates:
                    relative = path_builder(symbol, day)
                    with events.span("archive-verification"):
                        sha256 = _checksum(request.archive_root, relative, symbol, family, day)
                    files.append(BinanceArchiveFileIdentity(
                        relative.as_posix(), family, symbol, day, sha256))
                    for row in observed_rows(relative, schema, parser, day):
                        if family == "aggTrades":
                            trade_rows += 1
                            earliest_trade = (row.timestamp_ms if earliest_trade is None
                                              else min(earliest_trade, row.timestamp_ms))
                            latest_trade = (row.timestamp_ms if latest_trade is None
                                            else max(latest_trade, row.timestamp_ms))
                            if not index.add(symbol, row):
                                duplicate_trades += 1
                                continue
                            flow_builder.add_trade(
                                symbol, row.timestamp_ms, row.timestamp_ms,
                                row.price, row.quantity, row.buyer_is_maker)
                            if (MovementBucketEngine._bucket_boundary(row.timestamp_ms)
                                    >= config.engine_start_boundary_time_ms
                                    and row.timestamp_ms <= config.output_end_boundary_time_ms):
                                index.add_replay_trade(symbol, symbol_index, row)
                                replayable += 1
                        else:
                            kline_rows += 1
                            earliest_kline = (row.open_time_ms if earliest_kline is None
                                              else min(earliest_kline, row.open_time_ms))
                            latest_kline = (row.open_time_ms if latest_kline is None
                                            else max(latest_kline, row.open_time_ms))
                            prior = kline_by_open.get(row.open_time_ms)
                            if prior is not None and prior != row:
                                raise ValueError(
                                    f"conflicting kline open {row.open_time_ms} for {symbol}")
                            kline_by_open[row.open_time_ms] = row
                    if family == "aggTrades":
                        index.commit()
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
        if not replayable:
            raise ValueError("dataset lacks raw replayable trade evidence")
        index.commit()
        sorted_files = tuple(sorted(files, key=lambda item: item.relative_path))
        content_sha256 = _content_sha256(sorted_files)
        manifest = BinanceArchiveBundleManifest(
            BINANCE_ARCHIVE_ADAPTER_VERSION, BINANCE_ARCHIVE_DATASET_ID,
            BINANCE_ARCHIVE_DATASET_VERSION, ARCHIVE_FIRST_SEEN_POLICY,
            ARCHIVE_SOURCE_STATE_POLICY, sorted_files, content_sha256)
        digest = hashlib.sha256()
        first_key = last_key = None
        indexed_count = 0
        for trade in index.iter_replay_trades(request.universe.symbols):
            indexed_count += 1
            key = (trade.first_seen_at_ms,
                   request.universe.symbols.index(trade.symbol),
                   trade.trade_time_ms, trade.aggregate_trade_id)
            if first_key is None:
                first_key = key
            last_key = key
            normalized = (trade.symbol, *key,
                          _decimal_identity(trade.price),
                          _decimal_identity(trade.quantity))
            digest.update(json.dumps(normalized, separators=(",", ":"),
                                     ensure_ascii=True).encode("ascii") + b"\n")
        if indexed_count != replayable:
            raise ValueError("replay trade index row count changed")
        stream_manifest = HistoricalReplayTradeStreamManifest(
            BINANCE_ARCHIVE_DATASET_ID, BINANCE_ARCHIVE_DATASET_VERSION,
            content_sha256, request.universe.symbols,
            BINANCE_ARCHIVE_ADAPTER_VERSION, TRADE_STREAM_ORDERING_VERSION,
            TRADE_STREAM_DUPLICATE_VERSION, replayable, duplicate_trades,
            first_key, last_key, digest.hexdigest())
        ohlc_evidence = BinanceTradeOHLCEvidence(
            BINANCE_ARCHIVE_DATASET_ID, BINANCE_ARCHIVE_DATASET_VERSION,
            content_sha256, request.universe.symbols, tuple(ohlc_candles))
        flow = flow_builder.build(
            dataset_id=BINANCE_ARCHIVE_DATASET_ID,
            dataset_version=BINANCE_ARCHIVE_DATASET_VERSION,
            dataset_content_sha256=content_sha256)
        instruments = tuple(HistoricalReplayInstrument(
            symbol, f"binance-usdm:{symbol}", True)
            for symbol in request.universe.symbols)
        intervals = tuple(HistoricalReplaySourceInterval(
            symbol, config.engine_start_boundary_time_ms,
            config.output_end_boundary_time_ms, "LIVE")
            for symbol in request.universe.symbols)
        diagnostics = BinanceArchiveDiagnostics(
            len(sorted_files), len(sorted_files),
            len(trade_dates) * len(request.universe.symbols),
            len(kline_dates) * len(request.universe.symbols), trade_rows, kline_rows,
            duplicate_trades, missing_minutes, tuple(gap_symbols),
            earliest_trade, latest_trade, earliest_kline, latest_kline)
        events.emit("COMPLETED", stage="sqlite-preparation", duration_seconds=time.monotonic() - preparation_started)
        return BinanceBoundedHistoricalReplayDataset(
            manifest, stream_manifest,
            HistoricalReplayDatasetManifest(BINANCE_ARCHIVE_DATASET_ID,
                                            BINANCE_ARCHIVE_DATASET_VERSION,
                                            content_sha256),
            request.universe, instruments, intervals, tuple(candles), config,
            diagnostics, ohlc_evidence, flow, index)
    except BaseException:
        index.__exit__(None, None, None)
        raise


def _agg_identity(row: _AggRow) -> bytes:
    # Decimal spellings such as 1.0 and 1.00 compare equal in _AggRow; retain
    # that behavior while storing only a compact canonical identity on disk.
    identity = [
        _decimal_identity(row.price), _decimal_identity(row.quantity),
        row.first_trade_id, row.last_trade_id, row.timestamp_ms,
        row.buyer_is_maker,
    ]
    return json.dumps(identity, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


class _AggTradeDuplicateIndex:
    """Disk-backed exact duplicate index with a bounded SQLite page cache."""

    def __init__(self, *, replay: bool = False):
        self._replay = replay

    def __enter__(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="binance-core-verify-")
        try:
            self._connection = sqlite3.connect(
                Path(self._temporary.name) / "aggregate-identities.sqlite3")
            self._connection.execute("PRAGMA journal_mode=OFF")
            self._connection.execute("PRAGMA synchronous=OFF")
            self._connection.execute("PRAGMA temp_store=FILE")
            self._connection.execute("PRAGMA cache_size=-8192")
            self._connection.execute(
                "CREATE TABLE aggregate_trade_ids ("
                "symbol TEXT NOT NULL, aggregate_trade_id TEXT NOT NULL, "
                "identity BLOB NOT NULL, "
                "PRIMARY KEY (symbol, aggregate_trade_id)) WITHOUT ROWID")
            if self._replay:
                self._connection.execute(
                    "CREATE TABLE replay_trades ("
                    "symbol TEXT NOT NULL, symbol_index INTEGER NOT NULL, "
                    "first_seen_at_ms INTEGER NOT NULL, trade_time_ms INTEGER NOT NULL, "
                    "aggregate_trade_id TEXT NOT NULL, id_length INTEGER NOT NULL, "
                    "price TEXT NOT NULL, quantity TEXT NOT NULL, "
                    "PRIMARY KEY (symbol, aggregate_trade_id)) WITHOUT ROWID")
                self._connection.execute(
                    "CREATE INDEX replay_canonical_order ON replay_trades "
                    "(first_seen_at_ms, symbol_index, trade_time_ms, id_length, aggregate_trade_id)")
        except sqlite3.Error as exc:
            self.__exit__(None, None, None)
            raise ValueError("unable to prepare compact aggTrade identity index") from exc
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        connection = getattr(self, "_connection", None)
        if connection is not None:
            connection.close()
        temporary = getattr(self, "_temporary", None)
        if temporary is not None:
            temporary.cleanup()

    def add(self, symbol: str, row: _AggRow) -> bool:
        identity = _agg_identity(row)
        key = (symbol, str(row.aggregate_trade_id))
        try:
            result = self._connection.execute(
                "INSERT OR IGNORE INTO aggregate_trade_ids "
                "(symbol, aggregate_trade_id, identity) VALUES (?, ?, ?)",
                (*key, identity))
            if result.rowcount == 0:
                previous = self._connection.execute(
                    "SELECT identity FROM aggregate_trade_ids "
                    "WHERE symbol = ? AND aggregate_trade_id = ?", key).fetchone()
                if previous is None:
                    raise ValueError("aggTrade identity index lost a duplicate row")
                if previous[0] != identity:
                    raise ValueError(
                        f"conflicting aggTrade ID {row.aggregate_trade_id} for {symbol}")
                return False
        except sqlite3.Error as exc:
            raise ValueError("unable to verify compact aggTrade identities") from exc
        return True

    def add_replay_trade(self, symbol: str, symbol_index: int, row: _AggRow) -> None:
        if not self._replay:
            raise ValueError("replay index is not enabled")
        trade_id = str(row.aggregate_trade_id)
        self._connection.execute(
            "INSERT INTO replay_trades VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol, symbol_index, row.timestamp_ms, row.timestamp_ms,
             trade_id, len(trade_id), str(row.price), str(row.quantity)))

    def iter_replay_trades(self, symbols: tuple[str, ...], after_key=None):
        if not self._replay:
            raise ValueError("replay index is not enabled")
        statement = ("SELECT symbol, symbol_index, first_seen_at_ms, trade_time_ms, "
                     "aggregate_trade_id, price, quantity FROM replay_trades")
        parameters = ()
        if after_key is not None:
            seen, symbol_index, trade_time, trade_id = after_key
            statement += (" WHERE (first_seen_at_ms, symbol_index, trade_time_ms, "
                          "id_length, aggregate_trade_id) > (?, ?, ?, ?, ?)")
            parameters = (seen, symbol_index, trade_time, len(str(trade_id)), str(trade_id))
        statement += (" ORDER BY first_seen_at_ms, symbol_index, trade_time_ms, "
                      "id_length, aggregate_trade_id")
        for symbol, index, seen, time_ms, trade_id, price, quantity in self._connection.execute(
                statement, parameters):
            if symbols[index] != symbol:
                raise ValueError("replay index universe order changed")
            yield HistoricalReplayTrade(
                symbol, f"binance-usdm:{symbol}", Decimal(price), Decimal(quantity),
                time_ms, time_ms, int(trade_id), seen)

    def commit(self) -> None:
        try:
            self._connection.commit()
        except sqlite3.Error as exc:
            raise ValueError("unable to commit compact aggTrade identity index") from exc


def verify_binance_usdm_historical_core_archives(
    request: BinanceUSDMArchiveRequest,
) -> BinanceHistoricalCoreArchiveEvidence:
    """Verify core archives with streamed aggTrades and no replay materialization."""
    if not isinstance(request, BinanceUSDMArchiveRequest):
        raise ValueError("request must be BinanceUSDMArchiveRequest")
    config = request.replay_config
    candle_start = historical_candle_start_ms(config)
    trade_dates = required_aggtrade_dates(config)
    kline_dates = required_kline_dates(config)
    files = []
    ohlc_candles = []
    raw_replayable_trade_evidence_present = False

    with _AggTradeDuplicateIndex() as duplicate_index:
        for symbol in request.universe.symbols:
            kline_by_open = {}
            for family, dates, path_builder, schema, parser in (
                ("aggTrades", trade_dates, daily_aggtrades_relative_path,
                 _AGG_HEADER, _agg_row),
                ("klines/1m", kline_dates, daily_kline_relative_path,
                 _KLINE_HEADER, _kline_row),
            ):
                for day in dates:
                    relative = path_builder(symbol, day)
                    sha256 = _checksum(request.archive_root, relative, symbol,
                                       family, day)
                    files.append(BinanceArchiveFileIdentity(
                        relative.as_posix(), family, symbol, day, sha256))
                    for row in _iter_archive_rows(
                            request.archive_root, relative, schema, parser, day):
                        if family == "aggTrades":
                            duplicate_index.add(symbol, row)
                            if (MovementBucketEngine._bucket_boundary(row.timestamp_ms)
                                    >= config.engine_start_boundary_time_ms
                                    and row.timestamp_ms
                                    <= config.output_end_boundary_time_ms):
                                raw_replayable_trade_evidence_present = True
                        else:
                            prior = kline_by_open.get(row.open_time_ms)
                            if prior is not None and prior != row:
                                raise ValueError(
                                    f"conflicting kline open {row.open_time_ms} for {symbol}")
                            kline_by_open[row.open_time_ms] = row
                    if family == "aggTrades":
                        duplicate_index.commit()

            for opening in sorted(kline_by_open):
                row = kline_by_open[opening]
                if candle_start <= opening <= config.output_end_boundary_time_ms:
                    ohlc_candles.append(CompletedTradeOHLCCandle(
                        symbol, f"binance-usdm:{symbol}", opening,
                        row.close_time_ms, row.open, row.high, row.low, row.close,
                        row.close_time_ms + 1))

    if not raw_replayable_trade_evidence_present:
        raise ValueError("dataset lacks raw replayable trade evidence")

    sorted_files = tuple(sorted(files, key=lambda item: item.relative_path))
    content_sha256 = _content_sha256(sorted_files)
    manifest = BinanceArchiveBundleManifest(
        BINANCE_ARCHIVE_ADAPTER_VERSION, BINANCE_ARCHIVE_DATASET_ID,
        BINANCE_ARCHIVE_DATASET_VERSION, ARCHIVE_FIRST_SEEN_POLICY,
        ARCHIVE_SOURCE_STATE_POLICY, sorted_files, content_sha256)
    ohlc_evidence = BinanceTradeOHLCEvidence(
        BINANCE_ARCHIVE_DATASET_ID, BINANCE_ARCHIVE_DATASET_VERSION,
        content_sha256, request.universe.symbols, tuple(ohlc_candles))
    return BinanceHistoricalCoreArchiveEvidence(
        manifest, ohlc_evidence, raw_replayable_trade_evidence_present)


def completed_candle_series_from_archive(dataset, symbol):
    """Project paired native 1m archive facts without changing replay availability."""
    from .completed_candles import (
        COMPLETED_CANDLE_CONTRACT_VERSION, ArchiveCandleProvenance, CompletedCandle,
        CompletedCandleObservation, CompletedCandleSeries, CompletedCandleSeriesIdentity,
    )
    if not isinstance(dataset, (BinanceHistoricalReplayDataset, BinanceBoundedHistoricalReplayDataset)):
        raise ValueError("completed candles require a paired replay/archive dataset")
    manifest = dataset.archive_manifest
    replay = dataset.replay_request if isinstance(dataset, BinanceHistoricalReplayDataset) else dataset
    identity = CompletedCandleSeriesIdentity("binance-usdm", "binance", "futures", "perpetual",
        f"binance-usdm:{symbol}", symbol, symbol, "trade", "native-kline", 1)
    if symbol not in replay.universe.symbols or not any(
            item.symbol == symbol and item.instrument_id == identity.instrument_id for item in replay.instruments):
        raise ValueError("archive instrument identity mismatch")
    evidence = dataset.ohlc_evidence
    if (manifest.dataset_id, manifest.dataset_version, manifest.content_sha256) != (
            evidence.dataset_id, evidence.dataset_version, evidence.dataset_content_sha256):
        raise ValueError("archive dataset identity mismatch")
    movement = {}
    ohlc = {}
    for rows, target in ((replay.candles, movement), (evidence.candles, ohlc)):
        for row in rows:
            if row.symbol != symbol:
                continue
            previous = target.setdefault(row.open_time_ms, row)
            if previous != row:
                raise ValueError("conflicting paired archive candle")
    if movement.keys() != ohlc.keys():
        raise ValueError("missing paired archive candle")
    provenance = ArchiveCandleProvenance(manifest.dataset_id, manifest.dataset_version,
                                        manifest.content_sha256)
    observations = []
    for opening in sorted(ohlc):
        full, compact = ohlc[opening], movement[opening]
        _check_ohlc_movement_parity(full, compact)
        if full.instrument_id != identity.instrument_id:
            raise ValueError("archive candle instrument identity mismatch")
        observations.append(CompletedCandleObservation(CompletedCandle(identity,
            full.open_time_ms, full.close_time_ms, full.open, full.high, full.low, full.close,
            compact.volume, compact.quote_volume), provenance))
    return CompletedCandleSeries(COMPLETED_CANDLE_CONTRACT_VERSION, identity, tuple(observations))
