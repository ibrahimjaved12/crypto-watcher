"""Frozen historical market-state study design and outcome-blind selection.

This contract performs no acquisition, replay, candidate execution or outcome
evaluation. Study phases span independent UTC days, not ReplayPartitionPlan's
partitions inside one replay stream. Scientific hashes exclude local roots,
generation times and code revisions; later reports bind code separately.

Use verify_local_core_date for acquisition-independent verification, preserve
the records in HistoricalStudyEligibilityReport, then call
build_historical_market_state_study_manifest. The manifest retains only the
resolved selection paths. The JSON helpers return canonical text for callers
to save/freeze; this module writes no artifacts itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Literal

from .binance_historical_archive import (
    ARCHIVE_FIRST_SEEN_POLICY, ARCHIVE_SOURCE_STATE_POLICY,
    BINANCE_ARCHIVE_ADAPTER_VERSION, BINANCE_ARCHIVE_DATASET_ID,
    BINANCE_ARCHIVE_DATASET_VERSION,
    BinanceArchiveBundleManifest, BinanceArchiveCoverageError, BinanceArchiveFileIdentity,
    BinanceHistoricalCoreArchiveEvidence,
    BinanceHistoricalReplayDataset, _content_sha256,
    BinanceUSDMArchiveRequest, daily_aggtrades_relative_path,
    daily_kline_relative_path, historical_candle_start_ms,
    required_aggtrade_dates, required_kline_dates,
    verify_binance_usdm_historical_core_archives,
)
from .historical_experiment_batch import _canonical_json, _sha256
from .historical_replay import HistoricalReplayConfig
from .historical_ohlc_evidence import BinanceTradeOHLCEvidence
from .movement_metrics import MarketMovementConfig, MarketUniverseInput


STUDY_VERSION = "historical-market-state-study-v1"
SELECTION_POLICY_VERSION = "historical-market-state-selection-v2"
CORE_ELIGIBILITY_POLICY_VERSION = "historical-market-state-core-eligibility-v1"
ELIGIBILITY_REPORT_VERSION = "historical-market-state-eligibility-report-v1"
SECONDARY_HORIZON_POLICY_VERSION = "historical-market-state-secondary-horizons-v1"
SECONDARY_HORIZONS_MINUTES = (1, 5, 15, 30, 60)
CALENDAR_START = date(2024, 1, 1)
CALENDAR_END = date(2026, 8, 31)
BIN_COUNT = 30
DEVELOPMENT_PERIOD_COUNT = 10
VALIDATION_PERIOD_COUNT = 8
TEST_PERIOD_COUNT = 12
ORDERED_SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT")
UNIVERSE_ID = "historical-market-state-core5"
UNIVERSE_VERSION = "v1"
MINUTE_MS = 60_000
DAY_MS = 86_400_000

EligibilityState = Literal["ELIGIBLE", "INELIGIBLE", "UNVERIFIED"]
StudyPhase = Literal["development", "validation", "test"]
ELIGIBLE_CORE_DATA = "ELIGIBLE_CORE_DATA"
CORE_KLINE_GAP = "CORE_KLINE_GAP"
LOCAL_ARCHIVE_MISSING = "LOCAL_ARCHIVE_MISSING"
LOCAL_ARCHIVE_INVALID_OR_NEEDS_REACQUISITION = "LOCAL_ARCHIVE_INVALID_OR_NEEDS_REACQUISITION"
ELIGIBILITY_NOT_PROVIDED = "ELIGIBILITY_NOT_PROVIDED"


def _utc_date(value: date) -> date:
    if type(value) is not date or not CALENDAR_START <= value <= CALENDAR_END:
        raise ValueError("study UTC date must be within the frozen calendar")
    return value


def _digest(value: str) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def _integer(value: int, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _content(value, hash_field):
    return {item.name: getattr(value, item.name)
            for item in fields(value) if item.name != hash_field}


def study_universe() -> MarketUniverseInput:
    return MarketUniverseInput(UNIVERSE_ID, UNIVERSE_VERSION, ORDERED_SYMBOLS)


def study_replay_config(utc_date: date) -> HistoricalReplayConfig:
    """Canonical 24h boundaries and default V1 lookback; no extra pre-roll."""
    day = _utc_date(utc_date)
    start = int(datetime(day.year, day.month, day.day,
                         tzinfo=timezone.utc).timestamp()) * 1000
    return HistoricalReplayConfig(start, start + DAY_MS,
                                  movement_config=MarketMovementConfig())


def _required_paths(config: HistoricalReplayConfig) -> tuple[str, ...]:
    return tuple(sorted(
        path_builder(symbol, day).as_posix()
        for symbol in ORDERED_SYMBOLS
        for dates, path_builder in (
            (required_aggtrade_dates(config), daily_aggtrades_relative_path),
            (required_kline_dates(config), daily_kline_relative_path))
        for day in dates))


@dataclass(frozen=True)
class HistoricalStudyCalendarBin:
    bin_index: int
    start_date: date
    end_date: date
    target_weekday: int

    def __post_init__(self):
        if not _integer(self.bin_index) or self.bin_index >= BIN_COUNT:
            raise ValueError("invalid study bin index")
        count = (CALENDAR_END - CALENDAR_START).days + 1
        start = CALENDAR_START + timedelta(days=self.bin_index * count // BIN_COUNT)
        end = CALENDAR_START + timedelta(days=(self.bin_index + 1) * count // BIN_COUNT - 1)
        if ((self.start_date, self.end_date, self.target_weekday)
                != (start, end, self.bin_index % 7)
                or type(self.start_date) is not date or type(self.end_date) is not date
                or type(self.target_weekday) is not int):
            raise ValueError("bin boundaries/weekday differ from the frozen policy")

    @property
    def dates(self) -> tuple[date, ...]:
        return tuple(self.start_date + timedelta(days=i)
                     for i in range((self.end_date - self.start_date).days + 1))


def calendar_bins() -> tuple[HistoricalStudyCalendarBin, ...]:
    count = (CALENDAR_END - CALENDAR_START).days + 1
    if (CALENDAR_START > CALENDAR_END or count != 974 or BIN_COUNT != 30
            or (DEVELOPMENT_PERIOD_COUNT, VALIDATION_PERIOD_COUNT, TEST_PERIOD_COUNT)
            != (10, 8, 12)):
        raise ValueError("inconsistent frozen study calendar/counts")
    bins = tuple(HistoricalStudyCalendarBin(
        i, CALENDAR_START + timedelta(days=i * count // BIN_COUNT),
        CALENDAR_START + timedelta(days=(i + 1) * count // BIN_COUNT - 1), i % 7)
        for i in range(BIN_COUNT))
    days = tuple(day for item in bins for day in item.dates)
    expected = tuple(CALENDAR_START + timedelta(days=i) for i in range(count))
    if days != expected or len(set(days)) != count:
        raise ValueError("study bins must partition every UTC date exactly once")
    return bins


@dataclass(frozen=True)
class KlineGapRange:
    start_open_time_ms: int
    end_open_time_ms_exclusive: int

    def __post_init__(self):
        if (not _integer(self.start_open_time_ms)
                or not _integer(self.end_open_time_ms_exclusive)
                or self.start_open_time_ms % MINUTE_MS
                or self.end_open_time_ms_exclusive % MINUTE_MS
                or self.end_open_time_ms_exclusive <= self.start_open_time_ms):
            raise ValueError("kline gap must be a positive aligned minute range")

    @property
    def missing_minutes(self) -> int:
        return (self.end_open_time_ms_exclusive - self.start_open_time_ms) // MINUTE_MS


@dataclass(frozen=True)
class SymbolCoreCandleCoverage:
    symbol: str
    observed_minute_count: int
    gaps: tuple[KlineGapRange, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "gaps", tuple(self.gaps))
        if (self.symbol not in ORDERED_SYMBOLS or not _integer(self.observed_minute_count)
                or any(not isinstance(gap, KlineGapRange) for gap in self.gaps)
                or any(left.end_open_time_ms_exclusive >= right.start_open_time_ms
                       for left, right in zip(self.gaps, self.gaps[1:]))):
            raise ValueError("invalid ordered per-symbol candle coverage")


@dataclass(frozen=True)
class CoreDateProvenance:
    replay_config: HistoricalReplayConfig
    archive_manifest: BinanceArchiveBundleManifest
    ohlc_evidence_sha256: str
    candle_start_open_time_ms: int
    candle_end_open_time_ms_exclusive: int
    symbol_coverage: tuple[SymbolCoreCandleCoverage, ...]
    eligibility_policy_version: str = CORE_ELIGIBILITY_POLICY_VERSION
    provenance_sha256: str = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, "symbol_coverage", tuple(self.symbol_coverage))
        if (not isinstance(self.replay_config, HistoricalReplayConfig)
                or not isinstance(self.archive_manifest, BinanceArchiveBundleManifest)
                or not _digest(self.archive_manifest.content_sha256)
                or not _digest(self.ohlc_evidence_sha256)
                or self.eligibility_policy_version != CORE_ELIGIBILITY_POLICY_VERSION
                or not _integer(self.candle_start_open_time_ms)
                or not _integer(self.candle_end_open_time_ms_exclusive)
                or self.candle_start_open_time_ms != historical_candle_start_ms(self.replay_config)
                or self.candle_end_open_time_ms_exclusive != self.replay_config.output_end_boundary_time_ms
                or any(not isinstance(item, SymbolCoreCandleCoverage) for item in self.symbol_coverage)
                or tuple(item.symbol for item in self.symbol_coverage) != ORDERED_SYMBOLS):
            raise ValueError("invalid canonical core-data provenance")
        expected_count = (self.candle_end_open_time_ms_exclusive
                          - self.candle_start_open_time_ms) // MINUTE_MS
        for item in self.symbol_coverage:
            if (item.observed_minute_count + sum(g.missing_minutes for g in item.gaps)
                    != expected_count
                    or any(g.start_open_time_ms < self.candle_start_open_time_ms
                           or g.end_open_time_ms_exclusive > self.candle_end_open_time_ms_exclusive
                           for g in item.gaps)):
                raise ValueError("coverage must account for the complete expected candle range")
        archive_files = self.archive_manifest.archive_files
        if ((self.archive_manifest.adapter_version, self.archive_manifest.dataset_id,
             self.archive_manifest.dataset_version, self.archive_manifest.first_seen_policy,
             self.archive_manifest.source_state_policy)
                != (BINANCE_ARCHIVE_ADAPTER_VERSION, BINANCE_ARCHIVE_DATASET_ID,
                    BINANCE_ARCHIVE_DATASET_VERSION, ARCHIVE_FIRST_SEEN_POLICY, ARCHIVE_SOURCE_STATE_POLICY)
                or type(archive_files) is not tuple
                or any(not isinstance(item, BinanceArchiveFileIdentity) for item in archive_files)
                or tuple(item.relative_path for item in archive_files) != _required_paths(self.replay_config)
                or any(type(item.utc_date) is not date or item.symbol not in ORDERED_SYMBOLS
                       or item.data_type not in ("aggTrades", "klines/1m")
                       for item in archive_files)
                or any(not _digest(item.sha256) for item in archive_files)):
            raise ValueError("provenance must bind every required verified daily package")
        for item in archive_files:
            builder = daily_aggtrades_relative_path if item.data_type == "aggTrades" else daily_kline_relative_path
            if item.relative_path != builder(item.symbol, item.utc_date).as_posix():
                raise ValueError("verified package metadata must agree with its relative path")
        object.__setattr__(self, "provenance_sha256", _sha256(_content(self, "provenance_sha256")))


@dataclass(frozen=True)
class CoreDateEligibility:
    utc_date: date
    state: EligibilityState
    reason_code: str
    provenance: CoreDateProvenance | None = None
    missing_local_paths: tuple[str, ...] = ()
    eligibility_sha256: str = field(init=False)

    def __post_init__(self):
        _utc_date(self.utc_date)
        supplied_paths = tuple(self.missing_local_paths)
        if any(not isinstance(path, str) for path in supplied_paths):
            raise ValueError("local package diagnostics must contain only relative paths")
        paths = tuple(sorted(set(supplied_paths)))
        if any(not isinstance(p, str) or PurePosixPath(p).is_absolute()
               or ".." in PurePosixPath(p).parts or "\\" in p or ":" in p for p in paths):
            raise ValueError("local package diagnostics must contain only relative paths")
        object.__setattr__(self, "missing_local_paths", paths)
        if self.state == "UNVERIFIED":
            if (self.reason_code not in (LOCAL_ARCHIVE_MISSING,
                                         LOCAL_ARCHIVE_INVALID_OR_NEEDS_REACQUISITION,
                                         ELIGIBILITY_NOT_PROVIDED)
                    or self.provenance is not None):
                raise ValueError("unverified local data must not claim verified provenance")
        elif self.state in ("ELIGIBLE", "INELIGIBLE"):
            if (not isinstance(self.provenance, CoreDateProvenance) or paths
                    or self.provenance.replay_config != study_replay_config(self.utc_date)):
                raise ValueError("finalized eligibility requires canonical date provenance")
            has_gaps = any(item.gaps for item in self.provenance.symbol_coverage)
            expected = ("INELIGIBLE", CORE_KLINE_GAP) if has_gaps else ("ELIGIBLE", ELIGIBLE_CORE_DATA)
            if (self.state, self.reason_code) != expected:
                raise ValueError("eligibility state must agree with verified candle coverage")
        else:
            raise ValueError("unknown core eligibility state")
        object.__setattr__(self, "eligibility_sha256", _sha256(_content(self, "eligibility_sha256")))


def core_date_eligibility_from_verified_dataset(
    utc_date: date, dataset: BinanceHistoricalReplayDataset,
) -> CoreDateEligibility:
    """Apply the frozen core eligibility rule to an already verified dataset.

    This helper performs no acquisition or replay. Its caller must supply the
    canonical study date's verified Binance USD-M trade dataset.
    """
    config = study_replay_config(utc_date)
    if not isinstance(dataset, BinanceHistoricalReplayDataset):
        raise ValueError("dataset must be a verified BinanceHistoricalReplayDataset")
    if (dataset.replay_request.universe != study_universe()
            or dataset.replay_request.config != config):
        raise ValueError("verified dataset does not match the canonical study date and universe")
    return _core_date_eligibility_from_verified_evidence(
        utc_date, config, dataset.archive_manifest, dataset.ohlc_evidence)


def core_date_eligibility_from_verified_core_archives(
    utc_date: date, evidence: BinanceHistoricalCoreArchiveEvidence,
) -> CoreDateEligibility:
    """Apply frozen eligibility to compact, verified archive evidence only."""
    config = study_replay_config(utc_date)
    if not isinstance(evidence, BinanceHistoricalCoreArchiveEvidence):
        raise ValueError("evidence must be verified Binance historical core archives")
    if not evidence.raw_replayable_trade_evidence_present:
        raise ValueError("verified core archives lack replayable trade evidence")
    return _core_date_eligibility_from_verified_evidence(
        utc_date, config, evidence.archive_manifest, evidence.ohlc_evidence)


def _core_date_eligibility_from_verified_evidence(
    utc_date: date, config: HistoricalReplayConfig,
    archive_manifest: BinanceArchiveBundleManifest,
    ohlc_evidence: BinanceTradeOHLCEvidence,
) -> CoreDateEligibility:
    if (config != study_replay_config(utc_date)
            or not isinstance(archive_manifest, BinanceArchiveBundleManifest)
            or not isinstance(ohlc_evidence, BinanceTradeOHLCEvidence)
            or archive_manifest.content_sha256
            != _content_sha256(archive_manifest.archive_files)
            or (ohlc_evidence.dataset_id, ohlc_evidence.dataset_version,
                ohlc_evidence.dataset_content_sha256,
                ohlc_evidence.configured_symbols)
            != (archive_manifest.dataset_id, archive_manifest.dataset_version,
                archive_manifest.content_sha256, ORDERED_SYMBOLS)):
        raise ValueError("verified evidence does not match the canonical study date and universe")

    start = historical_candle_start_ms(config)
    end = config.output_end_boundary_time_ms
    by_symbol = {symbol: set() for symbol in ORDERED_SYMBOLS}
    for candle in ohlc_evidence.candles:
        if (start <= candle.open_time_ms < end and candle.close_time_ms < end
                and candle.first_seen_at_ms <= end):
            by_symbol[candle.symbol].add(candle.open_time_ms)
    coverage = []
    for symbol in ORDERED_SYMBOLS:
        observed = by_symbol[symbol]
        gaps = []
        gap_start = None
        for opening in range(start, end, MINUTE_MS):
            if opening not in observed and gap_start is None:
                gap_start = opening
            elif opening in observed and gap_start is not None:
                gaps.append(KlineGapRange(gap_start, opening))
                gap_start = None
        if gap_start is not None:
            gaps.append(KlineGapRange(gap_start, end))
        coverage.append(SymbolCoreCandleCoverage(symbol, len(observed), tuple(gaps)))
    provenance = CoreDateProvenance(config, archive_manifest,
                                    ohlc_evidence.evidence_sha256,
                                    start, end, tuple(coverage))
    has_gaps = any(item.gaps for item in coverage)
    return CoreDateEligibility(utc_date, "INELIGIBLE" if has_gaps else "ELIGIBLE",
                               CORE_KLINE_GAP if has_gaps else ELIGIBLE_CORE_DATA, provenance)


def verify_local_core_date(utc_date: date, archive_root: Path | str) -> CoreDateEligibility:
    """Verify cached core input with streamed aggTrades; daily ZIPs may be empty.

    All loader/cache failures remain UNVERIFIED. Only verified missing expected
    candle minutes establish scientific ineligibility. No raw exception text is
    serialized, since OS errors may contain absolute paths/environment details.
    The existing raw replayable trade-evidence requirement is retained; no
    additional aggTrade timestamp-gap rule is added.
    """
    config = study_replay_config(utc_date)
    request = BinanceUSDMArchiveRequest(archive_root, study_universe(), config)
    try:
        evidence = verify_binance_usdm_historical_core_archives(request)
    except BinanceArchiveCoverageError:
        missing = tuple(path for relative in _required_paths(config)
                        for path in (relative, f"{relative}.CHECKSUM")
                        if not request.archive_root.joinpath(*PurePosixPath(path).parts).is_file())
        return CoreDateEligibility(utc_date, "UNVERIFIED", LOCAL_ARCHIVE_MISSING,
                                   missing_local_paths=missing)
    except (ValueError, OSError):
        return CoreDateEligibility(utc_date, "UNVERIFIED",
                                   LOCAL_ARCHIVE_INVALID_OR_NEEDS_REACQUISITION)
    return core_date_eligibility_from_verified_core_archives(utc_date, evidence)


def _eligibility_by_date(records) -> dict[date, CoreDateEligibility]:
    by_date = {}
    for record in records:
        if not isinstance(record, CoreDateEligibility) or record.utc_date in by_date:
            raise ValueError("eligibility records must have unique study UTC dates")
        by_date[record.utc_date] = record
    return by_date


def _eligibility_report_records(records) -> tuple[CoreDateEligibility, ...]:
    by_date = _eligibility_by_date(records)
    return tuple(by_date.get(day) or CoreDateEligibility(
        day, "UNVERIFIED", ELIGIBILITY_NOT_PROVIDED)
        for item in calendar_bins() for day in item.dates)


def _selection_seed(bin_index: int) -> int:
    seed_text = f"crypto-watcher:historical-market-state-study-v1:{bin_index}"
    return int.from_bytes(hashlib.sha256(seed_text.encode("ascii")).digest(), "big")


def _weekday_candidates(calendar_bin: HistoricalStudyCalendarBin,
                        weekday: int) -> tuple[date, ...]:
    return tuple(day for day in calendar_bin.dates if day.weekday() == weekday)


def _rotated_candidates(calendar_bin: HistoricalStudyCalendarBin,
                        weekday: int) -> tuple[date, ...]:
    candidates = _weekday_candidates(calendar_bin, weekday)
    if not candidates:
        return ()
    index = _selection_seed(calendar_bin.bin_index) % len(candidates)
    return candidates[index:] + candidates[:index]


class UnresolvedStudySelectionError(ValueError):
    def __init__(self, bin_index: int, weekday: int, unresolved_date: date):
        self.bin_index = bin_index
        self.weekday = weekday
        self.unresolved_date = unresolved_date
        self.unresolved_dates = (unresolved_date,)
        super().__init__(f"bin {bin_index} weekday {weekday} candidate "
                         f"{unresolved_date.isoformat()} requires verification")


class NoEligibleStudyDateError(ValueError):
    def __init__(self, bin_index: int):
        self.bin_index = bin_index
        super().__init__(f"no eligible core date in study bin {bin_index}")


@dataclass(frozen=True)
class HistoricalStudySelectionAttempt:
    eligibility: CoreDateEligibility
    eligibility_sha256: str = field(init=False)

    def __post_init__(self):
        if (not isinstance(self.eligibility, CoreDateEligibility)
                or self.eligibility.state not in ("ELIGIBLE", "INELIGIBLE")):
            raise ValueError("finalized selection attempts require resolved eligibility")
        object.__setattr__(self, "eligibility_sha256", self.eligibility.eligibility_sha256)

    @property
    def utc_date(self) -> date:
        return self.eligibility.utc_date

    @property
    def state(self) -> EligibilityState:
        return self.eligibility.state


@dataclass(frozen=True)
class HistoricalStudyDateSelectionEvidence:
    bin_index: int
    target_weekday: int
    considered_weekdays: tuple[int, ...]
    attempted_dates: tuple[HistoricalStudySelectionAttempt, ...]
    selected_date: date
    fallback_distance: int
    selection_policy_version: str = SELECTION_POLICY_VERSION

    def __post_init__(self):
        object.__setattr__(self, "considered_weekdays", tuple(self.considered_weekdays))
        object.__setattr__(self, "attempted_dates", tuple(self.attempted_dates))
        calendar_bin = calendar_bins()[self.bin_index] if _integer(self.bin_index) and self.bin_index < BIN_COUNT else None
        if (calendar_bin is None or type(self.target_weekday) is not int
                or self.target_weekday != calendar_bin.target_weekday
                or self.selection_policy_version != SELECTION_POLICY_VERSION
                or not _integer(self.fallback_distance) or self.fallback_distance > 6
                or self.considered_weekdays != tuple(
                    (self.target_weekday + distance) % 7
                    for distance in range(self.fallback_distance + 1))
                or any(type(item) is not int for item in self.considered_weekdays)
                or not self.attempted_dates
                or any(not isinstance(item, HistoricalStudySelectionAttempt)
                       for item in self.attempted_dates)):
            raise ValueError("invalid finalized study selection evidence")

        attempts_at = 0
        for distance, weekday in enumerate(self.considered_weekdays):
            candidates = _rotated_candidates(calendar_bin, weekday)
            weekday_attempts = []
            while attempts_at < len(self.attempted_dates):
                attempt = self.attempted_dates[attempts_at]
                if attempt.utc_date.weekday() != weekday:
                    break
                if len(weekday_attempts) >= len(candidates):
                    raise ValueError("selection evidence repeats a weekday candidate")
                if attempt.utc_date != candidates[len(weekday_attempts)]:
                    raise ValueError("selection evidence skipped deterministic candidate order")
                weekday_attempts.append(attempt)
                attempts_at += 1
            if not weekday_attempts:
                raise ValueError("each considered weekday must contain a finalized attempt")
            if distance < self.fallback_distance:
                if (len(weekday_attempts) != len(candidates)
                        or any(item.state != "INELIGIBLE" for item in weekday_attempts)):
                    raise ValueError("weekday fallback requires every earlier candidate ineligible")
            elif (weekday_attempts[-1].utc_date != self.selected_date
                    or weekday_attempts[-1].state != "ELIGIBLE"
                    or any(item.state != "INELIGIBLE" for item in weekday_attempts[:-1])):
                raise ValueError("selection must stop at its first eligible candidate")
        if attempts_at != len(self.attempted_dates):
            raise ValueError("selection evidence contains attempts outside its considered weekdays")


@dataclass(frozen=True)
class HistoricalStudyDateSelection:
    calendar_bin: HistoricalStudyCalendarBin
    core_eligibility: CoreDateEligibility
    fallback_distance: int
    selection_evidence: HistoricalStudyDateSelectionEvidence

    def __post_init__(self):
        if (not isinstance(self.calendar_bin, HistoricalStudyCalendarBin)
                or not isinstance(self.core_eligibility, CoreDateEligibility)
                or not isinstance(self.selection_evidence, HistoricalStudyDateSelectionEvidence)
                or self.core_eligibility.state != "ELIGIBLE"
                or self.core_eligibility.utc_date not in self.calendar_bin.dates
                or self.selection_evidence.bin_index != self.calendar_bin.bin_index
                or self.selection_evidence.selected_date != self.core_eligibility.utc_date
                or self.selection_evidence.fallback_distance != self.fallback_distance
                or self.selection_evidence.attempted_dates[-1].eligibility_sha256
                != self.core_eligibility.eligibility_sha256
                or not _integer(self.fallback_distance) or self.fallback_distance > 6
                or self.core_eligibility.utc_date.weekday()
                != (self.calendar_bin.target_weekday + self.fallback_distance) % 7):
            raise ValueError("invalid finalized study date selection")


def select_study_date(calendar_bin: HistoricalStudyCalendarBin, records) -> HistoricalStudyDateSelection:
    if not isinstance(calendar_bin, HistoricalStudyCalendarBin):
        raise ValueError("calendar_bin must be HistoricalStudyCalendarBin")
    by_date = _eligibility_by_date(records)
    prior_attempts = []
    for distance in range(7):
        weekday = (calendar_bin.target_weekday + distance) % 7
        attempted = []
        for day in _rotated_candidates(calendar_bin, weekday):
            eligibility = by_date.get(day)
            if eligibility is None or eligibility.state == "UNVERIFIED":
                raise UnresolvedStudySelectionError(calendar_bin.bin_index, weekday, day)
            attempted.append(HistoricalStudySelectionAttempt(eligibility))
            if eligibility.state == "ELIGIBLE":
                evidence = HistoricalStudyDateSelectionEvidence(
                    calendar_bin.bin_index, calendar_bin.target_weekday,
                    tuple((calendar_bin.target_weekday + offset) % 7
                          for offset in range(distance + 1)),
                    tuple((*prior_attempts, *attempted)),
                    day, distance)
                return HistoricalStudyDateSelection(calendar_bin, eligibility, distance, evidence)
        prior_attempts.extend(attempted)
    raise NoEligibleStudyDateError(calendar_bin.bin_index)


def _phase(index: int) -> StudyPhase:
    if index < DEVELOPMENT_PERIOD_COUNT:
        return "development"
    if index < DEVELOPMENT_PERIOD_COUNT + VALIDATION_PERIOD_COUNT:
        return "validation"
    return "test"


@dataclass(frozen=True)
class HistoricalStudyPeriod:
    study_period_index: int
    source_bin_index: int
    bin_start_date: date
    bin_end_date: date
    utc_date: date
    phase: StudyPhase
    start_boundary_time_ms: int
    end_boundary_time_ms: int
    target_weekday: int
    selected_weekday: int
    fallback_distance: int
    weekday_fallback_needed: bool
    core_eligibility_sha256: str

    def __post_init__(self):
        if not _integer(self.study_period_index) or self.study_period_index >= BIN_COUNT:
            raise ValueError("invalid chronological study index")
        item = HistoricalStudyCalendarBin(self.source_bin_index, self.bin_start_date,
                                self.bin_end_date, self.target_weekday)
        config = study_replay_config(self.utc_date)
        if (self.utc_date not in item.dates or self.phase != _phase(self.study_period_index)
                or not _integer(self.start_boundary_time_ms)
                or not _integer(self.end_boundary_time_ms)
                or (self.start_boundary_time_ms, self.end_boundary_time_ms)
                != (config.output_start_boundary_time_ms, config.output_end_boundary_time_ms)
                or not _integer(self.fallback_distance) or self.fallback_distance > 6
                or type(self.selected_weekday) is not int
                or self.selected_weekday != self.utc_date.weekday()
                or self.selected_weekday != (self.target_weekday + self.fallback_distance) % 7
                or type(self.weekday_fallback_needed) is not bool
                or self.weekday_fallback_needed != (self.fallback_distance > 0)
                or not _digest(self.core_eligibility_sha256)):
            raise ValueError("invalid chronological study period/phase/boundaries")


def assign_study_phases(selections) -> tuple[HistoricalStudyPeriod, ...]:
    selections = tuple(selections)
    if (len(selections) != BIN_COUNT
            or any(not isinstance(item, HistoricalStudyDateSelection) for item in selections)
            or {item.calendar_bin.bin_index for item in selections} != set(range(BIN_COUNT))
            or len({item.core_eligibility.utc_date for item in selections}) != BIN_COUNT):
        raise ValueError("exactly 30 unique eligible dates/source bins are required")
    ordered = sorted(selections, key=lambda item: item.core_eligibility.utc_date)
    periods = []
    for index, item in enumerate(ordered):
        day, source = item.core_eligibility.utc_date, item.calendar_bin
        config = study_replay_config(day)
        periods.append(HistoricalStudyPeriod(
            index, source.bin_index, source.start_date, source.end_date, day, _phase(index),
            config.output_start_boundary_time_ms, config.output_end_boundary_time_ms,
            source.target_weekday, day.weekday(), item.fallback_distance,
            item.fallback_distance > 0, item.core_eligibility.eligibility_sha256))
    return tuple(periods)


def select_study_periods(records) -> tuple[HistoricalStudyPeriod, ...]:
    by_date = _eligibility_by_date(records)
    return assign_study_phases(
        select_study_date(item, by_date.values()) for item in calendar_bins())


def _selections_from_evidence(evidence_records):
    selections = []
    for evidence in evidence_records:
        calendar_bin = calendar_bins()[evidence.bin_index]
        selected = evidence.attempted_dates[-1].eligibility
        selections.append(HistoricalStudyDateSelection(
            calendar_bin, selected, evidence.fallback_distance, evidence))
    return tuple(selections)


@dataclass(frozen=True)
class PrimaryHypothesis:
    family_id: str
    display_name: str
    causal_forward_test: bool
    primary_horizon_minutes: int | None
    primary_outcome_id: str
    research_question: str

    def __post_init__(self):
        if (any(not isinstance(value, str) or not value for value in (
                self.family_id, self.display_name, self.primary_outcome_id, self.research_question))
                or type(self.causal_forward_test) is not bool):
            raise ValueError("primary hypothesis requires stable metadata")
        if self.family_id == "EXP-75-04A":
            if self.causal_forward_test or self.primary_horizon_minutes is not None:
                raise ValueError("PELT has no causal forward test or horizon")
        elif (not self.causal_forward_test or not _integer(self.primary_horizon_minutes, 1)):
            raise ValueError("causal hypothesis requires a positive primary horizon")


PRIMARY_HYPOTHESES = (
    PrimaryHypothesis("EXP-75-01", "EWMA", True, 15, "state_persistence",
                      "Does smoothing provide more persistent/useful state information without excessive lag?"),
    PrimaryHypothesis("EXP-75-02", "CUSUM", True, 15, "absolute_market_return",
                      "Does a causal directional-shift alarm identify materially different subsequent movement?"),
    PrimaryHypothesis("EXP-75-03", "Kalman", True, 15, "signed_market_return",
                      "Does filtered trend add information beyond V1's raw state?"),
    PrimaryHypothesis("EXP-75-04A", "PELT", False, None, "retrospective_structural_reference",
                      "Retrospective structural reference only; no causal forward test."),
    PrimaryHypothesis("EXP-75-04B", "BOCPD", True, 60, "realized_volatility",
                      "Does elevated causal change probability precede changing volatility/regime conditions?"),
    PrimaryHypothesis("EXP-75-05", "Regression acceleration", True, 5, "persistence_weakening",
                      "Does regression slope improve on V1 adjacent-window acceleration?"),
    PrimaryHypothesis("EXP-75-06A", "Realized-volatility normalization", True, 15, "absolute_market_return",
                      "Does alternate normalization better discriminate materially unusual moves?"),
    PrimaryHypothesis("EXP-75-06B", "ATR normalization", True, 15, "absolute_market_return",
                      "Does true-range normalization better discriminate materially unusual moves?"),
    PrimaryHypothesis("EXP-75-07", "PCA/common factor", True, 15, "future_breadth_extremity",
                      "Does coordination strength add information beyond breadth?"),
    PrimaryHypothesis("EXP-75-08", "Correlation/network", True, 15, "forward_dispersion",
                      "Does current coordination structure foreshadow cross-asset convergence/divergence?"),
    PrimaryHypothesis("EXP-75-09", "HMM", True, 60, "realized_volatility",
                      "Does the frozen causal regime add information beyond V1 state?"),
    PrimaryHypothesis("EXP-75-10", "Mark/trade divergence", True, 5, "signed_market_return",
                      "Does basis/divergence add short-horizon information to trade-price V1?"),
    PrimaryHypothesis("EXP-75-11-OI", "Open interest", True, 15, "v1_direction_persistence",
                      "Does OI change modify future behavior of the same V1 price state?"),
    PrimaryHypothesis("EXP-75-11-FUNDING", "Funding", True, 60, "v1_direction_persistence",
                      "Does settled funding context modify 60m persistence of the same V1 direction?"),
    PrimaryHypothesis("EXP-75-11-LIQUIDATION", "Liquidations", True, 15, "realized_volatility",
                      "Does observed liquidation intensity modify subsequent volatility/movement?"),
    PrimaryHypothesis("EXP-75-12", "Taker imbalance", True, 5, "signed_market_return",
                      "Does aggressive-side flow add directional information beyond V1?"),
)


@dataclass(frozen=True)
class HistoricalStudyEligibilityReport:
    eligibility_records: tuple[CoreDateEligibility, ...]
    study_version: str = STUDY_VERSION
    report_version: str = ELIGIBILITY_REPORT_VERSION
    eligibility_policy_version: str = CORE_ELIGIBILITY_POLICY_VERSION
    report_sha256: str = field(init=False)

    def __post_init__(self):
        if (self.study_version != STUDY_VERSION or self.report_version != ELIGIBILITY_REPORT_VERSION
                or self.eligibility_policy_version != CORE_ELIGIBILITY_POLICY_VERSION):
            raise ValueError("unsupported eligibility report version")
        object.__setattr__(self, "eligibility_records", _eligibility_report_records(self.eligibility_records))
        object.__setattr__(self, "report_sha256", _sha256(_content(self, "report_sha256")))


@dataclass(frozen=True)
class HistoricalMarketStateStudyManifest:
    selected_periods: tuple[HistoricalStudyPeriod, ...]
    selection_evidence: tuple[HistoricalStudyDateSelectionEvidence, ...]
    study_version: str = STUDY_VERSION
    selection_policy_version: str = SELECTION_POLICY_VERSION
    eligibility_policy_version: str = CORE_ELIGIBILITY_POLICY_VERSION
    calendar_start: date = CALENDAR_START
    calendar_end: date = CALENDAR_END
    universe: MarketUniverseInput = field(default_factory=study_universe)
    bin_count: int = BIN_COUNT
    development_period_count: int = DEVELOPMENT_PERIOD_COUNT
    validation_period_count: int = VALIDATION_PERIOD_COUNT
    test_period_count: int = TEST_PERIOD_COUNT
    primary_hypotheses: tuple[PrimaryHypothesis, ...] = PRIMARY_HYPOTHESES
    secondary_horizon_policy_version: str = SECONDARY_HORIZON_POLICY_VERSION
    secondary_horizons_minutes: tuple[int, ...] = SECONDARY_HORIZONS_MINUTES
    manifest_sha256: str = field(init=False)

    def __post_init__(self):
        for name in ("selected_periods", "selection_evidence", "primary_hypotheses",
                     "secondary_horizons_minutes"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if ((self.study_version, self.selection_policy_version, self.eligibility_policy_version,
             self.calendar_start, self.calendar_end, self.universe, self.bin_count,
             self.development_period_count, self.validation_period_count, self.test_period_count,
             self.secondary_horizon_policy_version, self.secondary_horizons_minutes)
                != (STUDY_VERSION, SELECTION_POLICY_VERSION, CORE_ELIGIBILITY_POLICY_VERSION,
                    CALENDAR_START, CALENDAR_END, study_universe(), BIN_COUNT,
                    DEVELOPMENT_PERIOD_COUNT, VALIDATION_PERIOD_COUNT, TEST_PERIOD_COUNT,
                    SECONDARY_HORIZON_POLICY_VERSION, SECONDARY_HORIZONS_MINUTES)
                or any(type(getattr(self, name)) is not int for name in (
                    "bin_count", "development_period_count", "validation_period_count", "test_period_count"))
                or any(type(value) is not int for value in self.secondary_horizons_minutes)
                or type(self.calendar_start) is not date or type(self.calendar_end) is not date):
            raise ValueError("manifest differs from the frozen historical market-state study")
        if (any(not isinstance(item, PrimaryHypothesis) for item in self.primary_hypotheses)
                or len({item.family_id for item in self.primary_hypotheses}) != len(self.primary_hypotheses)
                or self.primary_hypotheses != PRIMARY_HYPOTHESES):
            raise ValueError("manifest must retain the exact frozen primary hypothesis registry")
        if (len(self.selection_evidence) != BIN_COUNT
                or any(not isinstance(item, HistoricalStudyDateSelectionEvidence)
                       for item in self.selection_evidence)
                or tuple(item.bin_index for item in self.selection_evidence) != tuple(range(BIN_COUNT))):
            raise ValueError("manifest requires one ordered finalized selection path per calendar bin")
        expected = assign_study_phases(_selections_from_evidence(self.selection_evidence))
        if self.selected_periods != expected:
            raise ValueError("periods must agree with selection evidence and chronological phases")
        object.__setattr__(self, "manifest_sha256", _sha256(_content(self, "manifest_sha256")))


def build_historical_market_state_study_manifest(records) -> HistoricalMarketStateStudyManifest:
    by_date = _eligibility_by_date(records)
    selections = tuple(select_study_date(item, by_date.values()) for item in calendar_bins())
    return HistoricalMarketStateStudyManifest(
        assign_study_phases(selections),
        tuple(item.selection_evidence for item in selections))


def historical_market_state_study_manifest_json(manifest: HistoricalMarketStateStudyManifest) -> str:
    if not isinstance(manifest, HistoricalMarketStateStudyManifest):
        raise ValueError("manifest must be HistoricalMarketStateStudyManifest")
    return _canonical_json(manifest)


def historical_study_eligibility_report_json(report: HistoricalStudyEligibilityReport) -> str:
    if not isinstance(report, HistoricalStudyEligibilityReport):
        raise ValueError("report must be HistoricalStudyEligibilityReport")
    return _canonical_json(report)


def _strict_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError(f"invalid JSON numeric constant: {value}")


def _json_object(value, expected_fields, label):
    if not isinstance(value, dict) or set(value) != set(expected_fields):
        raise ValueError(f"invalid {label} fields")
    return value


def _json_date(value, label):
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO UTC date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO UTC date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must use canonical YYYY-MM-DD form")
    return parsed


def _parse_replay_config(value) -> HistoricalReplayConfig:
    value = _json_object(value, (
        "output_start_boundary_time_ms", "output_end_boundary_time_ms",
        "finalization_grace_ms", "movement_config"), "replay config")
    movement_fields = tuple(item.name for item in fields(MarketMovementConfig))
    movement = MarketMovementConfig(**_json_object(
        value["movement_config"], movement_fields, "movement config"))
    return HistoricalReplayConfig(
        value["output_start_boundary_time_ms"],
        value["output_end_boundary_time_ms"],
        value["finalization_grace_ms"], movement)


def _parse_archive_manifest(value) -> BinanceArchiveBundleManifest:
    value = _json_object(value, (
        "adapter_version", "dataset_id", "dataset_version", "first_seen_policy",
        "source_state_policy", "archive_files", "content_sha256"), "archive manifest")
    if not isinstance(value["archive_files"], list):
        raise ValueError("archive manifest files must be an array")
    files = []
    for item in value["archive_files"]:
        item = _json_object(item, (
            "relative_path", "data_type", "symbol", "utc_date", "sha256"),
            "archive file identity")
        files.append(BinanceArchiveFileIdentity(
            item["relative_path"], item["data_type"], item["symbol"],
            _json_date(item["utc_date"], "archive file date"), item["sha256"]))
    files = tuple(files)
    manifest = BinanceArchiveBundleManifest(
        value["adapter_version"], value["dataset_id"], value["dataset_version"],
        value["first_seen_policy"], value["source_state_policy"], files,
        value["content_sha256"])
    if (not _digest(manifest.content_sha256)
            or manifest.content_sha256 != _content_sha256(files)):
        raise ValueError("archive manifest content SHA-256 mismatch")
    return manifest


def _parse_core_provenance(value) -> CoreDateProvenance:
    value = _json_object(value, (
        "replay_config", "archive_manifest", "ohlc_evidence_sha256",
        "candle_start_open_time_ms", "candle_end_open_time_ms_exclusive",
        "symbol_coverage", "eligibility_policy_version", "provenance_sha256"),
        "core provenance")
    if not isinstance(value["symbol_coverage"], list):
        raise ValueError("symbol coverage must be an array")
    coverage = []
    for item in value["symbol_coverage"]:
        item = _json_object(item, ("symbol", "observed_minute_count", "gaps"),
                            "symbol coverage")
        if not isinstance(item["gaps"], list):
            raise ValueError("candle gaps must be an array")
        gaps = []
        for gap in item["gaps"]:
            gap = _json_object(gap, (
                "start_open_time_ms", "end_open_time_ms_exclusive"), "candle gap")
            gaps.append(KlineGapRange(
                gap["start_open_time_ms"], gap["end_open_time_ms_exclusive"]))
        coverage.append(SymbolCoreCandleCoverage(
            item["symbol"], item["observed_minute_count"], tuple(gaps)))
    provenance = CoreDateProvenance(
        _parse_replay_config(value["replay_config"]),
        _parse_archive_manifest(value["archive_manifest"]),
        value["ohlc_evidence_sha256"], value["candle_start_open_time_ms"],
        value["candle_end_open_time_ms_exclusive"], tuple(coverage),
        value["eligibility_policy_version"])
    if value["provenance_sha256"] != provenance.provenance_sha256:
        raise ValueError("core provenance SHA-256 mismatch")
    return provenance


def _parse_eligibility_record(value) -> CoreDateEligibility:
    value = _json_object(value, (
        "utc_date", "state", "reason_code", "provenance", "missing_local_paths",
        "eligibility_sha256"), "eligibility record")
    if not isinstance(value["missing_local_paths"], list):
        raise ValueError("missing local paths must be an array")
    provenance = (None if value["provenance"] is None
                  else _parse_core_provenance(value["provenance"]))
    record = CoreDateEligibility(
        _json_date(value["utc_date"], "eligibility date"), value["state"],
        value["reason_code"], provenance, tuple(value["missing_local_paths"]))
    if value["eligibility_sha256"] != record.eligibility_sha256:
        raise ValueError("eligibility record SHA-256 mismatch")
    return record


def parse_historical_study_eligibility_report_json(
    content: str,
) -> HistoricalStudyEligibilityReport:
    """Load and validate the versioned operational eligibility snapshot."""
    if not isinstance(content, str):
        raise ValueError("eligibility report content must be text")
    try:
        payload = json.loads(
            content, object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid eligibility report JSON") from exc
    payload = _json_object(payload, (
        "eligibility_records", "study_version", "report_version",
        "eligibility_policy_version", "report_sha256"), "eligibility report")
    if not isinstance(payload["eligibility_records"], list):
        raise ValueError("eligibility records must be an array")
    records = tuple(_parse_eligibility_record(item)
                    for item in payload["eligibility_records"])
    expected_dates = tuple(day for calendar_bin in calendar_bins()
                           for day in calendar_bin.dates)
    if (len(records) != len(expected_dates)
            or {item.utc_date for item in records} != set(expected_dates)):
        raise ValueError("eligibility report must contain the full frozen study calendar")
    report = HistoricalStudyEligibilityReport(
        records, payload["study_version"], payload["report_version"],
        payload["eligibility_policy_version"])
    if payload["report_sha256"] != report.report_sha256:
        raise ValueError("eligibility report SHA-256 mismatch")
    return report
