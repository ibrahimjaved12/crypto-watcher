"""Execution adapters for existing verified Binance archives and frozen events.

Ordering is local to each tape, never a total order across evidence streams.
Archive timestamps describe exchange events/completion, not network observation.
"""

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path, PurePosixPath
import sqlite3

from .binance_historical_archive import (
    ARCHIVE_FIRST_SEEN_POLICY, BINANCE_ARCHIVE_ADAPTER_VERSION,
    _AGG_HEADER, _AggTradeDuplicateIndex, _agg_row, _checksum, _decimal_identity,
    _iter_archive_rows, _utc_date,
    daily_aggtrades_relative_path,
)
from .canonical_identity import canonical_digest
from .execution_evidence import (
    EvidenceIdentity, SourceIdentity, SourceKind, SourceProvenance, decimal,
    instrument, relative_path, sha256, text, timestamp,
)
from .futures_execution_contracts import OrderSide, Provenance
from .historical_funding_evidence import (
    AVAILABILITY_BASIS as FUNDING_AVAILABILITY, EVIDENCE_VERSION as FUNDING_VERSION,
    SOURCE as FUNDING_SOURCE, BinanceFundingEvidence, SettledFundingEvent,
    monthly_funding_relative_path,
)
from .historical_mark_price_evidence import (
    BinanceMarkPriceEvidence, CompletedMarkPriceCandle, MARK_PRICE_EVIDENCE_VERSION,
    MARK_PRICE_SOURCE, daily_mark_price_relative_path,
    load_binance_usdm_mark_price_evidence,
)


@dataclass(frozen=True)
class TradeArchivePackage(EvidenceIdentity):
    symbol: str
    instrument_id: str
    utc_day: str
    relative_path: str
    archive_sha256: str
    source_version: str = BINANCE_ARCHIVE_ADAPTER_VERSION
    source: str = "binance-public-data-usdm-aggTrades"

    def __post_init__(self):
        instrument(self.symbol, self.instrument_id)
        sha256(self.archive_sha256)
        relative_path(self.relative_path)
        try:
            day = date.fromisoformat(self.utc_day)
        except (ValueError, TypeError) as exc:
            raise ValueError("archive UTC day required") from exc
        if (self.utc_day != day.isoformat()
                or self.relative_path != str(daily_aggtrades_relative_path(self.symbol, day))
                or self.source_version != BINANCE_ARCHIVE_ADAPTER_VERSION
                or self.source != "binance-public-data-usdm-aggTrades"):
            raise ValueError("archive source/package identity mismatch")


@dataclass(frozen=True)
class ExecutionTrade(EvidenceIdentity):
    symbol: str
    instrument_id: str
    aggregate_trade_id: int
    first_trade_id: int
    last_trade_id: int
    timestamp_ms: int
    price: Decimal
    quantity: Decimal
    buyer_is_maker: bool
    package: TradeArchivePackage
    available_at_ms: int
    availability_policy: str = ARCHIVE_FIRST_SEEN_POLICY

    def __post_init__(self):
        instrument(self.symbol, self.instrument_id)
        if (not isinstance(self.package, TradeArchivePackage)
                or (self.package.symbol, self.package.instrument_id) != (self.symbol, self.instrument_id)):
            raise ValueError("trade source/package mismatch")
        for value in (self.aggregate_trade_id, self.first_trade_id, self.last_trade_id,
                      self.timestamp_ms, self.available_at_ms):
            timestamp(value)
        if (self.first_trade_id > self.last_trade_id
                or _utc_date(self.timestamp_ms).isoformat() != self.package.utc_day):
            raise ValueError("invalid trade ID range or row outside archive UTC day")
        decimal(self.price, "trade price", positive=True)
        decimal(self.quantity, "trade quantity", positive=True)
        if type(self.buyer_is_maker) is not bool:
            raise ValueError("buyer_is_maker must be boolean")
        if (self.available_at_ms != self.timestamp_ms
                or self.availability_policy != ARCHIVE_FIRST_SEEN_POLICY):
            raise ValueError("archive timestamp availability surrogate required")

    @property
    def aggressor_side(self):
        return OrderSide.SELL if self.buyer_is_maker else OrderSide.BUY

    @property
    def source_archive_relative_path(self):
        return self.package.relative_path

    @property
    def source_archive_sha256(self):
        return self.package.archive_sha256


EXECUTION_TAPE_VERSION = "binance-usdm-execution-trade-tape-v2:disk-canonical"
TRADE_ORDERING_POLICY = "single-symbol:exchange-timestamp-then-aggregate-id:no-cross-stream-order-v1"
TRADE_DUPLICATE_POLICY = "same-aggregate-id-equal-facts-collapse-v1"
_STREAM_DIGEST_PREFIX = b"binance-usdm-execution-normalized-row-stream-v1\n"


@dataclass(frozen=True)
class ExecutionTradeTapeEvidence(EvidenceIdentity):
    """Small immutable manifest. Rows and temporary iteration state live elsewhere."""

    symbol: str
    instrument_id: str
    packages: tuple[TradeArchivePackage, ...]
    row_count: int
    duplicate_count: int
    first_event_key: tuple[int, int] | None
    last_event_key: tuple[int, int] | None
    normalized_rows_sha256: str
    tape_version: str = EXECUTION_TAPE_VERSION
    parser_version: str = BINANCE_ARCHIVE_ADAPTER_VERSION
    ordering_policy: str = TRADE_ORDERING_POLICY
    duplicate_policy: str = TRADE_DUPLICATE_POLICY

    def __post_init__(self):
        instrument(self.symbol, self.instrument_id)
        if not isinstance(self.packages, tuple) or not self.packages:
            raise ValueError("nonempty immutable package tuple required")
        packages = {}
        for package in self.packages:
            if (not isinstance(package, TradeArchivePackage)
                    or package.instrument_id != self.instrument_id):
                raise ValueError("tape package instrument mismatch")
            prior = packages.get(package.relative_path)
            if prior is not None and prior != package:
                raise ValueError("source/package hash conflict")
            packages[package.relative_path] = package
        for count in (self.row_count, self.duplicate_count):
            timestamp(count)
        sha256(self.normalized_rows_sha256)
        for key in (self.first_event_key, self.last_event_key):
            if key is not None:
                if not isinstance(key, tuple) or len(key) != 2:
                    raise ValueError("canonical event key must be (timestamp_ms, aggregate_trade_id)")
                for value in key:
                    timestamp(value)
                if _utc_date(key[0]).isoformat() not in {p.utc_day for p in packages.values()}:
                    raise ValueError("event key outside selected archive UTC days")
        if self.row_count == 0:
            if (self.first_event_key is not None or self.last_event_key is not None
                    or self.duplicate_count or self.normalized_rows_sha256 != hashlib.sha256(_STREAM_DIGEST_PREFIX).hexdigest()):
                raise ValueError("empty tape metadata mismatch")
        elif (self.first_event_key is None or self.last_event_key is None
              or self.first_event_key > self.last_event_key
              or (self.first_event_key == self.last_event_key) != (self.row_count == 1)):
            raise ValueError("nonempty tape event-key bounds mismatch")
        if ((self.tape_version, self.parser_version, self.ordering_policy, self.duplicate_policy)
                != (EXECUTION_TAPE_VERSION, BINANCE_ARCHIVE_ADAPTER_VERSION,
                    TRADE_ORDERING_POLICY, TRADE_DUPLICATE_POLICY)):
            raise ValueError("unsupported tape/parser/ordering/duplicate policy")
        object.__setattr__(self, "packages", tuple(sorted(packages.values(), key=lambda p: p.relative_path)))


class _ExecutionTradeIndex(_AggTradeDuplicateIndex):
    """Extend the existing temporary, bounded-cache duplicate index with row order.

    SQLite's on-disk primary key supplies numeric ordering even for aggregate IDs
    larger than signed 64-bit integers. No SELECT is fetched as a complete list.
    """

    def __enter__(self):
        super().__enter__()
        try:
            self._connection.execute(
                "CREATE TABLE execution_trade_order ("
                "timestamp_ms INTEGER NOT NULL, id_length INTEGER NOT NULL, "
                "aggregate_trade_id TEXT NOT NULL, package_offset INTEGER NOT NULL, "
                "price TEXT NOT NULL, quantity TEXT NOT NULL, first_trade_id TEXT NOT NULL, "
                "last_trade_id TEXT NOT NULL, buyer_is_maker INTEGER NOT NULL, "
                "PRIMARY KEY (timestamp_ms, id_length, aggregate_trade_id)) WITHOUT ROWID")
        except sqlite3.Error as exc:
            self.__exit__(None, None, None)
            raise ValueError("unable to prepare bounded execution trade index") from exc
        self.duplicate_count = 0
        return self

    def add_execution(self, symbol, row, package_offset):
        # Keep the archive's exact duplicate semantics, including Decimal equality.
        super().add(symbol, row)
        aggregate_id = str(row.aggregate_trade_id)
        try:
            result = self._connection.execute(
                "INSERT OR IGNORE INTO execution_trade_order VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row.timestamp_ms, len(aggregate_id), aggregate_id, package_offset,
                 str(row.price), str(row.quantity), str(row.first_trade_id), str(row.last_trade_id),
                 int(row.buyer_is_maker)))
        except sqlite3.Error as exc:
            raise ValueError("unable to index execution trade order") from exc
        if result.rowcount == 0:
            self.duplicate_count += 1

    def trades(self, packages):
        try:
            cursor = self._connection.execute(
                "SELECT timestamp_ms, aggregate_trade_id, package_offset, price, quantity, "
                "first_trade_id, last_trade_id, buyer_is_maker FROM execution_trade_order "
                "ORDER BY timestamp_ms, id_length, aggregate_trade_id")
            try:
                for time_ms, aggregate_id, offset, price, quantity, first_id, last_id, maker in cursor:
                    package = packages[offset]
                    yield ExecutionTrade(package.symbol, package.instrument_id, int(aggregate_id),
                                         int(first_id), int(last_id), time_ms, Decimal(price),
                                         Decimal(quantity), bool(maker), package, time_ms)
            finally:
                cursor.close()
        except sqlite3.Error as exc:
            raise ValueError("unable to stream canonical execution trades") from exc


def _index_trade_package(root, package, offset, index, *, verify_before=True):
    relative = PurePosixPath(package.relative_path)
    day = date.fromisoformat(package.utc_day)
    if verify_before and _checksum(root, relative, package.symbol, "aggTrades", day) != package.archive_sha256:
        raise ValueError("source/package hash conflict before read")
    for row in _iter_archive_rows(root, relative, _AGG_HEADER, _agg_row, day):
        index.add_execution(package.symbol, row, offset)
    if _checksum(root, relative, package.symbol, "aggTrades", day) != package.archive_sha256:
        raise ValueError("source/package hash conflict during read")
    index.commit()


def _trade_manifest(symbol, packages, index):
    digest = hashlib.sha256(_STREAM_DIGEST_PREFIX)
    count, first, last = 0, None, None
    for row in index.trades(packages):
        key = (row.timestamp_ms, row.aggregate_trade_id)
        if first is None:
            first = key
        last = key
        count += 1
        # Fixed-size per-row hashes avoid giant canonical lists/strings. Normalize
        # Decimal spelling exactly, retaining the archive's duplicate semantics.
        payload = (row.symbol, row.instrument_id, row.aggregate_trade_id,
                   row.first_trade_id, row.last_trade_id, row.timestamp_ms,
                   _decimal_identity(row.price), _decimal_identity(row.quantity),
                   row.buyer_is_maker, row.package.relative_path,
                   row.available_at_ms, row.availability_policy)
        digest.update(canonical_digest(payload).encode("ascii"))
        digest.update(b"\n")
    return ExecutionTradeTapeEvidence(symbol, f"binance-usdm:{symbol}", tuple(packages), count,
                                      index.duplicate_count, first, last, digest.hexdigest())


def load_execution_trade_tape(archive_root, symbol, utc_days):
    """Verify local archives into a small manifest using bounded temporary disk.

    Memory scales with selected package metadata, not the aggTrade population.
    """
    instrument(symbol, f"binance-usdm:{symbol}")
    days = tuple(utc_days)
    if not days or any(type(day) is not date for day in days):
        raise ValueError("explicit archive UTC days required")
    root = Path(archive_root).expanduser().resolve()
    packages = []
    with _ExecutionTradeIndex() as index:
        for day in sorted(set(days)):
            relative = daily_aggtrades_relative_path(symbol, day)
            digest = _checksum(root, relative, symbol, "aggTrades", day)
            package = TradeArchivePackage(symbol, f"binance-usdm:{symbol}", day.isoformat(), str(relative), digest)
            _index_trade_package(root, package, len(packages), index, verify_before=False)
            packages.append(package)
        return _trade_manifest(symbol, packages, index)


def iter_execution_trades(archive_root, evidence):
    """Yield canonically ordered immutable rows bound to the supplied manifest.

    Verify packages and recomputed metadata/digest before the first yield. This
    rebuilds a temporary on-disk index; it adds no persistent evidence state.
    Exhaust or explicitly close the generator (e.g. contextlib.closing) to clean
    up temporary disk when stopping early. No acquisition or event resolution.
    """
    if not isinstance(evidence, ExecutionTradeTapeEvidence):
        raise ValueError("immutable execution tape manifest required")
    root = Path(archive_root).expanduser().resolve()
    with _ExecutionTradeIndex() as index:
        for offset, package in enumerate(evidence.packages):
            _index_trade_package(root, package, offset, index)
        if _trade_manifest(evidence.symbol, evidence.packages, index) != evidence:
            raise ValueError("execution tape manifest/content identity mismatch")
        yield from index.trades(evidence.packages)


@dataclass(frozen=True)
class ExactSettlementMark(EvidenceIdentity):
    symbol: str
    instrument_id: str
    funding_timestamp_ms: int
    funding_event_identity: str
    mark_price: Decimal
    source: SourceIdentity

    def __post_init__(self):
        instrument(self.symbol, self.instrument_id)
        timestamp(self.funding_timestamp_ms)
        sha256(self.funding_event_identity)
        decimal(self.mark_price, "exact settlement mark", positive=True)
        if (not isinstance(self.source, SourceIdentity)
                or self.source.provenance.kind is not SourceKind.EXCHANGE_EVENT
                or self.source.provenance.classification is not Provenance.ACTUAL_HISTORICAL):
            raise ValueError("exact mark requires factual funding-event provenance")
        if self.source.provenance.effective_at_ms != self.funding_timestamp_ms:
            raise ValueError("exact mark effective timestamp must equal funding settlement")
        observed = self.source.provenance.observed_at_ms
        if observed is not None and observed < self.funding_timestamp_ms:
            raise ValueError("settlement mark cannot be observed before settlement")

    @property
    def available_at_ms(self):
        # An exact historical value does not prove when it was originally received.
        return self.source.provenance.observed_at_ms

    @property
    def availability_policy(self):
        return "explicit-observed-time-v1" if self.available_at_ms is not None else "OBSERVATION_TIME_UNAVAILABLE"


@dataclass(frozen=True)
class FundingSettlementEvidence(EvidenceIdentity):
    symbol: str
    instrument_id: str
    funding_timestamp_ms: int
    funding_rate: Decimal
    funding_interval_hours: int
    available_at_ms: int
    availability_policy: str
    source: SourceIdentity
    package_relative_path: str | None = None
    upstream_evidence_sha256: str | None = None
    factual_fields_json: str = "{}"
    exact_mark: ExactSettlementMark | None = None

    def __post_init__(self):
        instrument(self.symbol, self.instrument_id)
        timestamp(self.funding_timestamp_ms)
        timestamp(self.available_at_ms)
        if self.available_at_ms < self.funding_timestamp_ms:
            raise ValueError("funding available before settlement")
        decimal(self.funding_rate, "funding rate")
        if type(self.funding_interval_hours) is not int or self.funding_interval_hours <= 0:
            raise ValueError("positive integer funding interval required")
        text(self.availability_policy, "funding availability policy")
        if (not isinstance(self.source, SourceIdentity)
                or self.source.provenance.kind is not SourceKind.EXCHANGE_EVENT
                or self.source.provenance.classification is not Provenance.ACTUAL_HISTORICAL
                or self.source.provenance.effective_at_ms != self.funding_timestamp_ms):
            raise ValueError("factual funding settlement source required")
        if self.package_relative_path is not None:
            relative_path(self.package_relative_path)
        if self.upstream_evidence_sha256 is not None:
            sha256(self.upstream_evidence_sha256)
        # Freeze supplied ancillary facts (including rateType), without mutable maps.
        from .binance_execution_snapshots import frozen_factual_json
        object.__setattr__(self, "factual_fields_json", frozen_factual_json(self.factual_fields_json))
        facts = json.loads(self.factual_fields_json)
        if "rateType" in facts:
            text(facts["rateType"], "funding rateType")
        if "fundingIntervalHours" in facts and (type(facts["fundingIntervalHours"]) is not int
                                                or facts["fundingIntervalHours"] != self.funding_interval_hours):
            raise ValueError("contradictory factual funding interval")
        if self.exact_mark is not None:
            mark = self.exact_mark
            if (not isinstance(mark, ExactSettlementMark)
                    or (mark.symbol, mark.instrument_id, mark.funding_timestamp_ms, mark.funding_event_identity)
                    != (self.symbol, self.instrument_id, self.funding_timestamp_ms, self.event_identity)):
                raise ValueError("settlement mark symbol/time/event identity mismatch")

    @property
    def event_identity(self):
        # Stable join key independent of whether a mark has subsequently been supplied.
        return canonical_digest({"version": "execution-funding-event-v1", "symbol": self.symbol,
                                 "instrument": self.instrument_id, "time": self.funding_timestamp_ms,
                                 "rate": self.funding_rate, "interval": self.funding_interval_hours,
                                 "available": self.available_at_ms, "policy": self.availability_policy,
                                 "source": self.source, "package": self.package_relative_path,
                                 "upstream": self.upstream_evidence_sha256, "facts": self.factual_fields_json})

    @property
    def settlement_mark(self):
        return self.exact_mark.mark_price if self.exact_mark is not None else None

    @property
    def funding_mark_status(self):
        return "EXACT_SETTLEMENT_MARK" if self.exact_mark is not None else "UNAVAILABLE_FUNDING_MARK"

    @property
    def exact_cashflow_evidence_available_at_ms(self):
        if self.exact_mark is None or self.exact_mark.available_at_ms is None:
            return None
        return max(self.available_at_ms, self.exact_mark.available_at_ms)


def join_settlement_mark(event, mark):
    if not isinstance(event, FundingSettlementEvidence) or not isinstance(mark, ExactSettlementMark):
        raise ValueError("exact funding event and settlement-mark contracts required")
    if event.exact_mark is not None and event.exact_mark != mark:
        raise ValueError("conflicting exact settlement mark")
    return replace(event, exact_mark=mark)


@dataclass(frozen=True)
class FundingEvidence(EvidenceIdentity):
    symbol: str
    instrument_id: str
    events: tuple[FundingSettlementEvidence, ...]
    upstream_evidence_sha256: str

    def __post_init__(self):
        instrument(self.symbol, self.instrument_id)
        sha256(self.upstream_evidence_sha256)
        if (not isinstance(self.events, tuple)
                or any(not isinstance(e, FundingSettlementEvidence) or e.instrument_id != self.instrument_id
                       for e in self.events)):
            raise ValueError("immutable same-instrument funding events required")
        if any(e.upstream_evidence_sha256 != self.upstream_evidence_sha256 for e in self.events):
            raise ValueError("funding event/collection upstream evidence hash mismatch")
        # rateType can distinguish factual events at the same timestamp; never order them as executions.
        keys = {(e.funding_timestamp_ms, json.loads(e.factual_fields_json).get("rateType")) for e in self.events}
        if len(keys) != len(self.events):
            raise ValueError("duplicate or conflicting funding event")
        object.__setattr__(self, "events", tuple(sorted(self.events, key=lambda e: (e.funding_timestamp_ms, e.event_identity))))


def adapt_funding_evidence(evidence, symbol):
    """Reuse #128 archive contracts; no change to their scientific identities."""
    if not isinstance(evidence, BinanceFundingEvidence) or symbol not in evidence.configured_symbols:
        raise ValueError("existing funding evidence and configured symbol required")
    events = []
    for row in evidence.rows:
        if row.symbol != symbol:
            continue
        if not isinstance(row, SettledFundingEvent):
            raise ValueError("settled funding archive row required")
        package = next((p for p in evidence.packages if (p.symbol, p.relative_path)
                        == (symbol, row.package_relative_path)), None)
        if (package is None or not package.checksum_verified or package.sha256 != row.package_sha256
                or package.status in ("SCHEMA_MISMATCH", "INVALID_ARCHIVE")):
            raise ValueError("funding row/package hash conflict")
        if row.package_relative_path != str(monthly_funding_relative_path(
                symbol, _utc_date(row.source_time_ms).replace(day=1))):
            raise ValueError("funding event/package month mismatch")
        source = SourceIdentity(SourceProvenance(FUNDING_SOURCE, FUNDING_VERSION,
                                                Provenance.ACTUAL_HISTORICAL, SourceKind.EXCHANGE_EVENT,
                                                effective_at_ms=row.source_time_ms), row.package_sha256)
        events.append(FundingSettlementEvidence(symbol, f"binance-usdm:{symbol}", row.source_time_ms,
                                                row.funding_rate, row.funding_interval_hours,
                                                row.available_at_ms, FUNDING_AVAILABILITY, source,
                                                row.package_relative_path, evidence.evidence_sha256))
    return FundingEvidence(symbol, f"binance-usdm:{symbol}", tuple(events), evidence.evidence_sha256)


@dataclass(frozen=True)
class MarkRiskMinute(EvidenceIdentity):
    candle: CompletedMarkPriceCandle
    instrument_id: str
    package_relative_path: str
    source: SourceIdentity
    upstream_evidence_sha256: str
    resolution: str = "ONE_MINUTE_OHLC"
    exact_intraminute_mark: None = None
    exact_intraminute_timestamp_ms: None = None

    def __post_init__(self):
        if not isinstance(self.candle, CompletedMarkPriceCandle):
            raise ValueError("existing completed mark candle required")
        instrument(self.candle.symbol, self.instrument_id)
        relative_path(self.package_relative_path)
        expected = str(daily_mark_price_relative_path(self.candle.symbol, _utc_date(self.candle.open_time_ms)))
        if self.package_relative_path != expected:
            raise ValueError("mark candle package mismatch")
        if (not isinstance(self.source, SourceIdentity)
                or self.source.provenance.source != MARK_PRICE_SOURCE
                or self.source.provenance.classification is not Provenance.ACTUAL_HISTORICAL
                or self.source.provenance.kind is not SourceKind.EXCHANGE_EVENT):
            raise ValueError("factual mark package source required")
        sha256(self.upstream_evidence_sha256)
        if (self.resolution != "ONE_MINUTE_OHLC" or self.exact_intraminute_mark is not None
                or self.exact_intraminute_timestamp_ms is not None):
            raise ValueError("1m mark evidence cannot claim exact intraminute mark/time")

    @property
    def symbol(self):
        return self.candle.symbol

    @property
    def available_at_ms(self):
        return self.candle.first_seen_at_ms

    @property
    def availability_policy(self):
        return self.candle.availability_basis


@dataclass(frozen=True)
class MarkRiskEvidence(EvidenceIdentity):
    symbol: str
    instrument_id: str
    minutes: tuple[MarkRiskMinute, ...]
    upstream_evidence_sha256: str

    def __post_init__(self):
        instrument(self.symbol, self.instrument_id)
        sha256(self.upstream_evidence_sha256)
        if (not isinstance(self.minutes, tuple)
                or any(not isinstance(m, MarkRiskMinute) or m.instrument_id != self.instrument_id
                       or m.upstream_evidence_sha256 != self.upstream_evidence_sha256 for m in self.minutes)):
            raise ValueError("immutable same-source mark minutes required")
        if len({m.candle.open_time_ms for m in self.minutes}) != len(self.minutes):
            raise ValueError("duplicate mark minute")
        object.__setattr__(self, "minutes", tuple(sorted(self.minutes, key=lambda m: m.candle.open_time_ms)))


def adapt_mark_risk_evidence(evidence, symbol):
    if not isinstance(evidence, BinanceMarkPriceEvidence) or symbol not in evidence.configured_symbols:
        raise ValueError("existing mark-price evidence and configured symbol required")
    minutes = []
    for candle in evidence.candles:
        if candle.symbol != symbol:
            continue
        relative = str(daily_mark_price_relative_path(symbol, _utc_date(candle.open_time_ms)))
        package = next((p for p in evidence.packages if p.symbol == symbol
                        and p.relative_archive_path == relative), None)
        if (package is None or not package.checksum_verified or package.archive_sha256 is None
                or package.status == "INVALID_ARCHIVE"):
            raise ValueError("usable mark candle must bind verified source package")
        source = SourceIdentity(SourceProvenance(MARK_PRICE_SOURCE, MARK_PRICE_EVIDENCE_VERSION,
                                                Provenance.ACTUAL_HISTORICAL, SourceKind.EXCHANGE_EVENT),
                                package.archive_sha256)
        minutes.append(MarkRiskMinute(candle, f"binance-usdm:{symbol}", relative, source,
                                      evidence.evidence_sha256))
    return MarkRiskEvidence(symbol, f"binance-usdm:{symbol}", tuple(minutes), evidence.evidence_sha256)


def load_execution_mark_risk_evidence(archive_root, symbol, start, end):
    return adapt_mark_risk_evidence(load_binance_usdm_mark_price_evidence(
        archive_root, (symbol,), start, end, download=False), symbol)
