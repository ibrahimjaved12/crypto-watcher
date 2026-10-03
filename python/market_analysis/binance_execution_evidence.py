"""Execution adapters for existing verified Binance archives and frozen events.

Ordering is local to each tape, never a total order across evidence streams.
Archive timestamps describe exchange events/completion, not network observation.
"""

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
import json
from pathlib import Path

from .binance_historical_archive import (
    ARCHIVE_FIRST_SEEN_POLICY, BINANCE_ARCHIVE_ADAPTER_VERSION,
    _AGG_HEADER, _agg_row, _checksum, _iter_archive_rows, _utc_date,
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


@dataclass(frozen=True)
class ExecutionTradeTape(EvidenceIdentity):
    symbol: str
    instrument_id: str
    packages: tuple[TradeArchivePackage, ...]
    rows: tuple[ExecutionTrade, ...]
    ordering_policy: str = "single-symbol:exchange-timestamp-then-aggregate-id:no-cross-stream-order-v1"

    def __post_init__(self):
        instrument(self.symbol, self.instrument_id)
        if not isinstance(self.packages, tuple) or not isinstance(self.rows, tuple):
            raise ValueError("immutable package/row tuples required")
        packages = {}
        for package in self.packages:
            if (not isinstance(package, TradeArchivePackage)
                    or package.instrument_id != self.instrument_id):
                raise ValueError("tape package instrument mismatch")
            prior = packages.get(package.relative_path)
            if prior is not None and prior != package:
                raise ValueError("source/package hash conflict")
            packages[package.relative_path] = package
        rows = {}
        for row in self.rows:
            if (not isinstance(row, ExecutionTrade) or row.instrument_id != self.instrument_id
                    or packages.get(row.package.relative_path) != row.package):
                raise ValueError("trade must bind tape's verified package")
            prior = rows.get(row.aggregate_trade_id)
            if prior is not None and prior != row:
                raise ValueError("conflicting duplicate aggregate_trade_id")
            rows[row.aggregate_trade_id] = row
        if self.ordering_policy != type(self).__dataclass_fields__["ordering_policy"].default:
            raise ValueError("unsupported tape ordering policy")
        object.__setattr__(self, "packages", tuple(sorted(packages.values(), key=lambda p: p.relative_path)))
        object.__setattr__(self, "rows", tuple(sorted(rows.values(), key=lambda r: (r.timestamp_ms, r.aggregate_trade_id))))


def load_execution_trade_tape(archive_root, symbol, utc_days):
    """Read local checksum-verified packages with the original aggTrade parser."""
    instrument(symbol, f"binance-usdm:{symbol}")
    days = tuple(utc_days)
    if not days or any(type(day) is not date for day in days):
        raise ValueError("explicit archive UTC days required")
    root = Path(archive_root).expanduser().resolve()
    packages, trades = [], []
    for day in sorted(set(days)):
        relative = daily_aggtrades_relative_path(symbol, day)
        digest = _checksum(root, relative, symbol, "aggTrades", day)
        package = TradeArchivePackage(symbol, f"binance-usdm:{symbol}", day.isoformat(), str(relative), digest)
        packages.append(package)
        for row in _iter_archive_rows(root, relative, _AGG_HEADER, _agg_row, day):
            trades.append(ExecutionTrade(symbol, package.instrument_id, row.aggregate_trade_id,
                                         row.first_trade_id, row.last_trade_id, row.timestamp_ms,
                                         row.price, row.quantity, row.buyer_is_maker, package,
                                         row.timestamp_ms))
        if _checksum(root, relative, symbol, "aggTrades", day) != digest:
            raise ValueError("source/package hash conflict during read")
    return ExecutionTradeTape(symbol, f"binance-usdm:{symbol}", tuple(packages), tuple(trades))


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
