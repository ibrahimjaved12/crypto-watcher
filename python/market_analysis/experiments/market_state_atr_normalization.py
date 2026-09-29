"""EXP-75-06B: causal completed-candle ATR-SMA normalization research."""

from __future__ import annotations

from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass, replace
from decimal import Decimal, localcontext
import math
from statistics import median
from types import MappingProxyType
from typing import Iterable, Mapping

from ..historical_ohlc_evidence import (
    BinanceTradeOHLCEvidence, OHLC_EVIDENCE_VERSION,
)
from ..historical_replay import HistoricalReplayRunManifest
from ..market_episode_lifecycle import MarketEpisodeLifecycleConfig
from ..movement_classifier import MarketClassifierConfig
from ..movement_history import MINUTE_MS
from ..movement_metrics import (
    ALGORITHM_VERSION as BASELINE_MOVEMENT_ALGORITHM_VERSION,
    DEFAULT_CONFIG_VERSION as BASELINE_MOVEMENT_CONFIG_VERSION,
    EXCHANGE, MarketMovementConfig, Metric, PRICE_TYPE, PROVIDER, WINDOWS,
    _aggregates, _breadth, _outliers, WindowAggregates, WindowBreadth,
)
from .market_state_common import (
    MarketStateExperimentPoint, advance_canonical_branch,
    selected_points_for_partition, validate_experiment_points,
)
from .market_state_realized_vol_normalization import (
    _summary as _v1_candidate_summary,
    _validate_baseline,
)


ATR_ALGORITHM_VERSION = "market-movement-atr-sma-normalization-v1"
ATR_CONFIG_VERSION_PREFIX = "market-movement-atr-sma-normalization-config-v1"
ATR_LOOKBACK_MINUTES = (30, 60, 120)
ATR_KAPPA = math.sqrt(math.pi / 8.0)
ATR_DECIMAL_PRECISION = 50
ATR_MISSING_LATEST_MINUTE = "ATR_MISSING_LATEST_MINUTE"
ATR_LATEST_NOT_YET_AVAILABLE = "ATR_LATEST_NOT_YET_AVAILABLE"
ATR_MISSING_ADJACENT_MINUTE = "ATR_MISSING_ADJACENT_MINUTE"
ATR_PREVIOUS_CLOSE_NOT_YET_AVAILABLE = "ATR_PREVIOUS_CLOSE_NOT_YET_AVAILABLE"
ATR_INSUFFICIENT_HISTORY = "ATR_INSUFFICIENT_HISTORY"
ATR_ZERO_SCALE = "ATR_ZERO_SCALE"
ATR_SCALE_UNAVAILABLE = "ATR_SCALE_UNAVAILABLE"
ATR_NUMERATOR_UNAVAILABLE = "ATR_NUMERATOR_UNAVAILABLE"
ATR_NORMALIZATION_UNAVAILABLE = "ATR_NORMALIZATION_UNAVAILABLE"
_BASELINE_CONFIG = MarketMovementConfig()


def _config_version(lookback_minutes: int) -> str:
    return (
        f"{ATR_CONFIG_VERSION_PREFIX}:ohlc-binance-usdm-trade-1m"
        f":tr-max-high-low-previous-close:sma-{lookback_minutes}-true-ranges"
        f":divisor-latest-eligible-close:kappa-sqrt-pi-over-8"
        f":horizon-sqrt-minutes:decimal-precision-{ATR_DECIMAL_PRECISION}"
        ":strict-first-seen-before-t:latest-minute-end-before-t"
    )


@dataclass(frozen=True)
class ATRNormalizationConfig:
    version: str
    lookback_minutes: int
    smoothing: str = "sma"
    divisor: str = "latest-eligible-close"
    kappa: str = "sqrt(pi/8)"
    horizon_scaling: str = "sqrt-minutes"
    timing: str = "first-seen-and-minute-end-strictly-before-t"

    def __post_init__(self):
        if (type(self.lookback_minutes) is not int
                or self.lookback_minutes not in ATR_LOOKBACK_MINUTES
                or self.version != _config_version(self.lookback_minutes)
                or self.smoothing != "sma"
                or self.divisor != "latest-eligible-close"
                or self.kappa != "sqrt(pi/8)"
                or self.horizon_scaling != "sqrt-minutes"
                or self.timing != "first-seen-and-minute-end-strictly-before-t"):
            raise ValueError("ATR config must identify a preregistered 06B model")


ATR_CONFIG_30M = ATRNormalizationConfig(_config_version(30), 30)
ATR_CONFIG_60M = ATRNormalizationConfig(_config_version(60), 60)
ATR_CONFIG_120M = ATRNormalizationConfig(_config_version(120), 120)
ATR_CONFIGURATIONS = (ATR_CONFIG_30M, ATR_CONFIG_60M, ATR_CONFIG_120M)


@dataclass(frozen=True)
class ATRRangeHistory:
    latest_expected_end_time_ms: int
    latest_close: Decimal | None
    true_ranges_newest_first: tuple[Decimal, ...]
    stopped_reason: str | None


@dataclass(frozen=True)
class ATRCalibratedSnapshot:
    history: ATRRangeHistory
    by_lookback: tuple[
        tuple[int, Metric[Decimal], Metric[Decimal],
              tuple[tuple[int, Metric[float]], ...]], ...
    ]


class _ATRVisibilityCursor:
    """Index once; revisit at most 121 adjacent candles on a visibility change."""

    def __init__(self, evidence: BinanceTradeOHLCEvidence):
        self.symbols = evidence.configured_symbols
        self.all_by_open = {symbol: {} for symbol in self.symbols}
        self.visible = {symbol: {} for symbol in self.symbols}
        for candle in evidence.candles:
            self.all_by_open[candle.symbol][candle.open_time_ms] = candle
        self.opens = {
            symbol: tuple(sorted(self.all_by_open[symbol])) for symbol in self.symbols
        }
        self.events = tuple(sorted(
            evidence.candles,
            key=lambda candle: (candle.first_seen_at_ms, candle.symbol,
                                candle.open_time_ms),
        ))
        self.next_event = 0
        self.generation = {symbol: 0 for symbol in self.symbols}
        self.cache: dict[str, tuple[int, int, ATRRangeHistory]] = {}
        self.last_boundary: int | None = None

    def histories_at(self, boundary: int) -> Mapping[str, ATRRangeHistory]:
        if (type(boundary) is not int or boundary < 0 or boundary % 5_000
                or self.last_boundary is not None and boundary <= self.last_boundary):
            raise ValueError("ATR cursor requires increasing five-second boundaries")
        self.last_boundary = boundary
        # #111 as_of is inclusive. EXP-75-06B deliberately uses strict visibility.
        while (self.next_event < len(self.events)
               and self.events[self.next_event].first_seen_at_ms < boundary):
            candle = self.events[self.next_event]
            self.visible[candle.symbol][candle.open_time_ms] = candle
            self.generation[candle.symbol] += 1
            self.next_event += 1
        latest_end = ((boundary - 1) // MINUTE_MS) * MINUTE_MS
        result = {}
        for symbol in self.symbols:
            cached = self.cache.get(symbol)
            if (cached is None or cached[0] != latest_end
                    or cached[1] != self.generation[symbol]):
                history = self._history(symbol, latest_end)
                self.cache[symbol] = (latest_end, self.generation[symbol], history)
            result[symbol] = self.cache[symbol][2]
        return result

    def _history(self, symbol: str, latest_end: int) -> ATRRangeHistory:
        latest_open = latest_end - MINUTE_MS
        current = self.visible[symbol].get(latest_open)
        if current is None:
            reason = (ATR_LATEST_NOT_YET_AVAILABLE
                      if latest_open in self.all_by_open[symbol]
                      else ATR_MISSING_LATEST_MINUTE)
            return ATRRangeHistory(latest_end, None, (), reason)
        true_ranges = []
        reason = None
        for _ in range(max(ATR_LOOKBACK_MINUTES)):
            previous_open = current.open_time_ms - MINUTE_MS
            previous = self.visible[symbol].get(previous_open)
            if previous is None:
                if previous_open in self.all_by_open[symbol]:
                    reason = ATR_PREVIOUS_CLOSE_NOT_YET_AVAILABLE
                elif bisect_left(self.opens[symbol], previous_open) > 0:
                    reason = ATR_MISSING_ADJACENT_MINUTE
                else:
                    reason = ATR_INSUFFICIENT_HISTORY
                break
            with localcontext() as context:
                context.prec = ATR_DECIMAL_PRECISION
                true_ranges.append(max(
                    current.high - current.low,
                    abs(current.high - previous.close),
                    abs(current.low - previous.close),
                ))
            current = previous
        return ATRRangeHistory(latest_end, self.visible[symbol][latest_open].close,
                               tuple(true_ranges), reason)


def _atr_metrics(history: ATRRangeHistory,
                 lookback_minutes: int) -> tuple[Metric[Decimal], Metric[Decimal]]:
    if len(history.true_ranges_newest_first) < lookback_minutes:
        reason = history.stopped_reason or ATR_INSUFFICIENT_HISTORY
        return Metric.missing(reason), Metric.missing(reason)
    with localcontext() as context:
        context.prec = ATR_DECIMAL_PRECISION
        raw = sum(history.true_ranges_newest_first[:lookback_minutes],
                  Decimal(0)) / Decimal(lookback_minutes)
        relative = raw / history.latest_close
    if not raw.is_finite() or not relative.is_finite() or relative < 0:
        return (Metric.missing(ATR_SCALE_UNAVAILABLE),
                Metric.missing(ATR_SCALE_UNAVAILABLE))
    return Metric.present(raw), Metric.present(relative)


def _scale(relative_atr: Metric[Decimal], window_minutes: int) -> Metric[float]:
    if not relative_atr.available:
        return Metric.missing(relative_atr.reason)
    if relative_atr.value == 0:
        return Metric.missing(ATR_ZERO_SCALE)
    try:
        scale = float(relative_atr.value) * ATR_KAPPA * math.sqrt(window_minutes)
    except (OverflowError, ValueError):
        return Metric.missing(ATR_SCALE_UNAVAILABLE)
    return (Metric.present(scale) if math.isfinite(scale) and scale > 0
            else Metric.missing(ATR_SCALE_UNAVAILABLE))


def _calibrated_snapshot(history: ATRRangeHistory) -> ATRCalibratedSnapshot:
    entries = []
    for lookback in ATR_LOOKBACK_MINUTES:
        raw, relative = _atr_metrics(history, lookback)
        scales = tuple((minute, _scale(relative, minute)) for minute in WINDOWS)
        entries.append((lookback, raw, relative, scales))
    return ATRCalibratedSnapshot(history, tuple(entries))


def _candidate_symbol(item, scale: Metric[float]):
    if not item.included:
        return item
    if not scale.available:
        score = Metric.missing(scale.reason)
    elif (not item.current_return.available
          or not item.historical_median.available
          or not isinstance(item.current_return.value, (int, float))
          or not isinstance(item.historical_median.value, (int, float))
          or not math.isfinite(item.current_return.value)
          or not math.isfinite(item.historical_median.value)):
        score = Metric.missing(ATR_NUMERATOR_UNAVAILABLE)
    else:
        value = ((item.current_return.value - item.historical_median.value)
                 / scale.value)
        score = (Metric.present(value) if math.isfinite(value)
                 else Metric.missing(ATR_NUMERATOR_UNAVAILABLE))
    if not score.available:
        return replace(item, normalized_z=score,
                       direction=Metric.missing(score.reason),
                       material_rising=False, material_falling=False,
                       outlier_candidate=False)
    raw = item.current_return.value
    if (item.normalized_z.available and item.normalized_z.value != 0
            and score.value != 0
            and (item.normalized_z.value > 0) != (score.value > 0)):
        raise ValueError("ATR normalization changed the centered-return sign")
    direction = ("FLAT" if raw == 0 or abs(score.value) < _BASELINE_CONFIG.flat_z
                 else "RISING" if raw > 0 else "FALLING")
    return replace(
        item, normalized_z=score, direction=Metric.present(direction),
        material_rising=raw > 0 and abs(score.value) >= _BASELINE_CONFIG.material_z,
        material_falling=raw < 0 and abs(score.value) >= _BASELINE_CONFIG.material_z,
        outlier_candidate=False,
    )


def _missing_window_evidence(window, reason):
    missing = Metric.missing(reason)
    breadth = WindowBreadth(False, reason, window.eligible_count,
                            missing, missing, missing, missing, missing)
    aggregates = WindowAggregates(
        missing, window.aggregates.median_raw_return,
        missing, missing, missing, missing,
    )
    return breadth, aggregates


@dataclass(frozen=True)
class ATRSymbolEvidence:
    symbol: str
    latest_expected_end_time_ms: int
    consecutive_true_range_count: int
    raw_atr: Metric[Decimal]
    relative_atr: Metric[Decimal]
    calibrated_scales: tuple[tuple[int, Metric[float]], ...]


def _transform(evaluation, config: ATRNormalizationConfig,
               snapshots: Mapping[str, ATRCalibratedSnapshot]):
    evidence = []
    scale_by_symbol = {}
    for symbol in evaluation.configured_universe:
        snapshot = snapshots[symbol]
        history = snapshot.history
        _, raw, relative, scales = next(
            entry for entry in snapshot.by_lookback
            if entry[0] == config.lookback_minutes)
        evidence.append(ATRSymbolEvidence(
            symbol, history.latest_expected_end_time_ms,
            len(history.true_ranges_newest_first), raw, relative, scales,
        ))
        scale_by_symbol[symbol] = dict(scales)
    windows = {}
    for minute in WINDOWS:
        baseline = evaluation.windows[minute]
        results = tuple(_candidate_symbol(
            item, scale_by_symbol[item.symbol][minute]) for item in baseline.symbols)
        included = tuple(item for item in results if item.included)
        ready = baseline.market_wide_eligible and included and all(
            item.normalized_z.available for item in included)
        if ready:
            results = _outliers(results, _BASELINE_CONFIG)
            included = tuple(item for item in results if item.included)
            breadth = _breadth(included, True)
            aggregates = _aggregates(included, True, _BASELINE_CONFIG)
        elif not baseline.market_wide_eligible:
            breadth, aggregates = _missing_window_evidence(
                baseline, "MARKET_UNIVERSE_INELIGIBLE")
        else:
            breadth, aggregates = _missing_window_evidence(
                baseline, ATR_NORMALIZATION_UNAVAILABLE)
        windows[minute] = replace(
            baseline, algorithm_version=ATR_ALGORITHM_VERSION,
            config_version=config.version, symbols=results,
            breadth=breadth, aggregates=aggregates,
        )
    return replace(
        evaluation, algorithm_version=ATR_ALGORITHM_VERSION,
        config_version=config.version, windows=windows,
    ), tuple(evidence)


@dataclass(frozen=True)
class PairedMarketStateATRPoint:
    evaluation_boundary_time_ms: int
    partition: str
    baseline_evaluation: object
    candidate_evaluation: object
    baseline_classification: object
    candidate_classification: object
    baseline_lifecycle_state: object
    candidate_lifecycle_state: object
    baseline_transitions: tuple
    candidate_transitions: tuple
    symbol_evidence: tuple[ATRSymbolEvidence, ...]


@dataclass(frozen=True)
class ATRComparisonSummary:
    partition: str
    v1_candidate_comparison: object
    availability_by_symbol_window_reason: tuple[tuple[str, int, str, int], ...]
    median_raw_atr_by_symbol: tuple[tuple[str, Decimal | None], ...]
    median_relative_atr_by_symbol: tuple[tuple[str, Decimal | None], ...]
    median_calibrated_scale_by_symbol_window: tuple[tuple[str, int, float | None], ...]


@dataclass(frozen=True)
class MarketStateATRExperimentResult:
    atr_config: ATRNormalizationConfig
    paired_points: tuple[PairedMarketStateATRPoint, ...]
    summaries: Mapping[str, ATRComparisonSummary]

    def __post_init__(self):
        object.__setattr__(self, "paired_points", tuple(self.paired_points))
        object.__setattr__(self, "summaries", MappingProxyType(dict(self.summaries)))


def _summary(points: tuple[PairedMarketStateATRPoint, ...],
             partition: str) -> ATRComparisonSummary:
    selected = selected_points_for_partition(points, partition)
    symbols = (points[0].baseline_evaluation.configured_universe if points else ())
    availability = Counter()
    raw = {symbol: [] for symbol in symbols}
    relative = {symbol: [] for symbol in symbols}
    scales = {(symbol, minute): [] for symbol in symbols for minute in WINDOWS}
    for point in selected:
        by_symbol = {item.symbol: item for item in point.symbol_evidence}
        for symbol in symbols:
            item = by_symbol[symbol]
            if item.raw_atr.available:
                raw[symbol].append(item.raw_atr.value)
            if item.relative_atr.available:
                relative[symbol].append(item.relative_atr.value)
            for minute, scale in item.calibrated_scales:
                if scale.available:
                    scales[symbol, minute].append(scale.value)
                candidate = next(
                    row for row in point.candidate_evaluation.windows[minute].symbols
                    if row.symbol == symbol)
                status = ("READY" if candidate.included and candidate.normalized_z.available
                          else candidate.normalized_z.reason if candidate.included
                          else "V1_EXCLUDED")
                availability[symbol, minute, status] += 1
    with localcontext() as context:
        context.prec = ATR_DECIMAL_PRECISION
        raw_medians = tuple(
            (symbol, median(raw[symbol]) if raw[symbol] else None)
            for symbol in symbols)
        relative_medians = tuple(
            (symbol, median(relative[symbol]) if relative[symbol] else None)
            for symbol in symbols)
    return ATRComparisonSummary(
        partition,
        _v1_candidate_summary(points, partition),
        tuple((symbol, minute, reason, count)
              for (symbol, minute, reason), count in sorted(availability.items())),
        raw_medians,
        relative_medians,
        tuple((symbol, minute, float(median(scales[symbol, minute]))
               if scales[symbol, minute] else None)
              for symbol in symbols for minute in WINDOWS),
    )


def _validate_inputs(points: tuple[MarketStateExperimentPoint, ...],
                     evidence: BinanceTradeOHLCEvidence,
                     replay_manifest: HistoricalReplayRunManifest) -> None:
    if (not isinstance(evidence, BinanceTradeOHLCEvidence)
            or not isinstance(replay_manifest, HistoricalReplayRunManifest)):
        raise ValueError("ATR requires immutable OHLC evidence and replay manifest")
    validate_experiment_points(points)
    if not points:
        raise ValueError("ATR requires a nonempty chronological replay point stream")
    if (evidence.dataset_id, evidence.dataset_version,
            evidence.dataset_content_sha256) != (
            replay_manifest.dataset_id, replay_manifest.dataset_version,
            replay_manifest.dataset_content_sha256):
        raise ValueError("ATR OHLC dataset identity disagrees with replay")
    if (evidence.configured_symbols != replay_manifest.configured_universe
            or evidence.evidence_version != OHLC_EVIDENCE_VERSION
            or replay_manifest.movement_algorithm_version
            != BASELINE_MOVEMENT_ALGORITHM_VERSION
            or replay_manifest.movement_config_version
            != BASELINE_MOVEMENT_CONFIG_VERSION
            or replay_manifest.provider != PROVIDER
            or replay_manifest.exchange != EXCHANGE
            or replay_manifest.price_type != PRICE_TYPE):
        raise ValueError(
            "ATR OHLC symbols, canonical movement, or trade-price provenance "
            "disagree with replay"
        )
    if (points[0].movement_evaluation.evaluation_boundary_time_ms
            != replay_manifest.output_start_boundary_time_ms
            or points[-1].movement_evaluation.evaluation_boundary_time_ms
            != replay_manifest.output_end_boundary_time_ms):
        raise ValueError("ATR point stream does not cover the replay output interval")
    for point in points:
        evaluation = point.movement_evaluation
        _validate_baseline(evaluation)
        if (evaluation.configured_universe != evidence.configured_symbols
                or evaluation.provider != PROVIDER
                or evaluation.exchange != EXCHANGE
                or evaluation.price_type != PRICE_TYPE
                or evaluation.universe_id != replay_manifest.universe_id
                or evaluation.universe_version != replay_manifest.universe_version):
            raise ValueError("ATR point scope disagrees with OHLC or replay provenance")


def run_market_state_atr_normalization_suite(
    points: Iterable[MarketStateExperimentPoint],
    ohlc_evidence: BinanceTradeOHLCEvidence,
    replay_manifest: HistoricalReplayRunManifest,
    *,
    classifier_config: MarketClassifierConfig | None = None,
    lifecycle_config: MarketEpisodeLifecycleConfig | None = None,
    configurations: tuple[ATRNormalizationConfig, ...] = ATR_CONFIGURATIONS,
) -> tuple[MarketStateATRExperimentResult, ...]:
    """Share one strict-visibility cursor across all fixed ATR lookbacks."""
    points = tuple(points)
    _validate_inputs(points, ohlc_evidence, replay_manifest)
    if (not configurations
            or any(not isinstance(config, ATRNormalizationConfig)
                   for config in configurations)
            or len({config.version for config in configurations}) != len(configurations)):
        raise ValueError("ATR suite requires distinct preregistered configurations")
    classifier_config = MarketClassifierConfig() if classifier_config is None else classifier_config
    lifecycle_config = MarketEpisodeLifecycleConfig() if lifecycle_config is None else lifecycle_config
    if (not isinstance(classifier_config, MarketClassifierConfig)
            or not isinstance(lifecycle_config, MarketEpisodeLifecycleConfig)):
        raise ValueError("ATR classifier and lifecycle configs are invalid")
    cursor = _ATRVisibilityCursor(ohlc_evidence)
    scale_cache: dict[str, ATRCalibratedSnapshot] = {}
    baseline_state = None
    candidate_states = {config.version: None for config in configurations}
    paired = {config.version: [] for config in configurations}
    for point in points:
        evaluation = point.movement_evaluation
        boundary = evaluation.evaluation_boundary_time_ms
        histories = cursor.histories_at(boundary)
        snapshots = {}
        for symbol, history in histories.items():
            cached = scale_cache.get(symbol)
            if cached is None or cached.history is not history:
                cached = _calibrated_snapshot(history)
                scale_cache[symbol] = cached
            snapshots[symbol] = cached
        baseline_classification, baseline_result = advance_canonical_branch(
            evaluation, point.source_time_evidence, baseline_state,
            classifier_config, lifecycle_config)
        for config in configurations:
            candidate, symbol_evidence = _transform(evaluation, config, snapshots)
            candidate_classification, candidate_result = advance_canonical_branch(
                candidate, point.source_time_evidence,
                candidate_states[config.version], classifier_config, lifecycle_config)
            paired[config.version].append(PairedMarketStateATRPoint(
                boundary, point.partition, evaluation, candidate,
                baseline_classification, candidate_classification,
                baseline_result.next_state, candidate_result.next_state,
                baseline_result.transitions, candidate_result.transitions,
                symbol_evidence,
            ))
            candidate_states[config.version] = candidate_result.next_state
        baseline_state = baseline_result.next_state
    results = []
    for config in configurations:
        rows = tuple(paired[config.version])
        summaries = {partition: _summary(rows, partition)
                     for partition in ("all", "development", "validation", "test")}
        results.append(MarketStateATRExperimentResult(config, rows, summaries))
    return tuple(results)


def run_market_state_atr_normalization_experiment(
    points: Iterable[MarketStateExperimentPoint],
    ohlc_evidence: BinanceTradeOHLCEvidence,
    replay_manifest: HistoricalReplayRunManifest,
    config: ATRNormalizationConfig,
    *,
    classifier_config: MarketClassifierConfig | None = None,
    lifecycle_config: MarketEpisodeLifecycleConfig | None = None,
) -> MarketStateATRExperimentResult:
    return run_market_state_atr_normalization_suite(
        points, ohlc_evidence, replay_manifest,
        classifier_config=classifier_config, lifecycle_config=lifecycle_config,
        configurations=(config,),
    )[0]
