"""Candidate-independent forward labels for the frozen market-state study.

Only verified Binance trade-price one-minute candles are accepted. Candidate
replay inputs must remain limited to the frozen study interval; the caller may
provide separately verified candles through the final one-hour label tail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from bisect import bisect_right
from statistics import median
from types import MappingProxyType
from typing import Iterable, Mapping

from .historical_ohlc_evidence import CompletedTradeOHLCCandle


FORWARD_OUTCOMES_VERSION = "historical-market-state-forward-outcomes-v1"
FORWARD_NUMERIC_POLICY = "binary64-log-close-difference-v1"
HORIZONS_MINUTES = (1, 5, 15, 30, 60)
MINUTE_MS = 60_000
POINT_MS = 5_000
USABLE_V1_STATES = frozenset(("BROAD_RISE", "BROAD_DROP", "NEUTRAL"))

START_PRICE_UNAVAILABLE = "START_PRICE_UNAVAILABLE"
END_PRICE_UNAVAILABLE = "END_PRICE_UNAVAILABLE"
INCOMPLETE_FUTURE_MINUTE_PATH = "INCOMPLETE_FUTURE_MINUTE_PATH"
NO_AVAILABLE_SYMBOL_OUTCOME = "NO_AVAILABLE_SYMBOL_OUTCOME"
INSUFFICIENT_CROSS_SECTION = "INSUFFICIENT_CROSS_SECTION"
STATE_PATH_UNAVAILABLE = "STATE_PATH_UNAVAILABLE"
STATE_PATH_OUTSIDE_EVALUATION_INTERVAL = "STATE_PATH_OUTSIDE_EVALUATION_INTERVAL"


def _sha256(value) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _candle_identity(candle: CompletedTradeOHLCCandle) -> tuple:
    return (candle.symbol, candle.open_time_ms, candle.close_time_ms,
            str(candle.open), str(candle.high), str(candle.low), str(candle.close),
            candle.first_seen_at_ms)


@dataclass(frozen=True)
class TradePriceForwardEvidence:
    """Verified candle view, separately bounded from canonical replay inputs."""

    configured_symbols: tuple[str, ...]
    candles: tuple[CompletedTradeOHLCCandle, ...]
    source_dataset_content_sha256: str
    requested_start_boundary_time_ms: int
    requested_end_boundary_time_ms: int
    evidence_version: str = FORWARD_OUTCOMES_VERSION
    evidence_sha256: str = field(init=False)
    _by_symbol: Mapping[str, tuple[CompletedTradeOHLCCandle, ...]] = field(
        init=False, repr=False, compare=False)
    _by_open: Mapping[str, Mapping[int, CompletedTradeOHLCCandle]] = field(
        init=False, repr=False, compare=False)
    _opens: Mapping[str, tuple[int, ...]] = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        symbols = tuple(self.configured_symbols)
        candles = tuple(self.candles)
        if (not symbols or len(set(symbols)) != len(symbols)
                or any(not isinstance(item, str) or not item for item in symbols)
                or any(not isinstance(item, CompletedTradeOHLCCandle) for item in candles)
                or any(item.symbol not in symbols for item in candles)
                or type(self.requested_start_boundary_time_ms) is not int
                or type(self.requested_end_boundary_time_ms) is not int
                or self.requested_start_boundary_time_ms % MINUTE_MS
                or self.requested_end_boundary_time_ms % MINUTE_MS
                or self.requested_end_boundary_time_ms <= self.requested_start_boundary_time_ms
                or not isinstance(self.source_dataset_content_sha256, str)
                or len(self.source_dataset_content_sha256) != 64
                or any(ch not in "0123456789abcdef" for ch in self.source_dataset_content_sha256)
                or self.evidence_version != FORWARD_OUTCOMES_VERSION):
            raise ValueError("invalid separately verified trade-price forward evidence")
        if any(not self.requested_start_boundary_time_ms <= item.open_time_ms
               < self.requested_end_boundary_time_ms for item in candles):
            raise ValueError("forward candles fall outside their verified evidence range")
        by_key = {}
        for candle in candles:
            key = (candle.symbol, candle.open_time_ms)
            previous = by_key.get(key)
            if previous is not None and previous != candle:
                raise ValueError("conflicting trade-price label candles")
            by_key[key] = candle
        ordered = tuple(sorted(by_key.values(), key=lambda item: (
            symbols.index(item.symbol), item.open_time_ms)))
        grouped = {symbol: tuple(item for item in ordered if item.symbol == symbol)
                   for symbol in symbols}
        by_open = {symbol: MappingProxyType({item.open_time_ms: item
                                             for item in grouped[symbol]})
                   for symbol in symbols}
        opens = {symbol: tuple(item.open_time_ms for item in grouped[symbol])
                 for symbol in symbols}
        payload = {
            "evidence_version": self.evidence_version,
            "numeric_policy": FORWARD_NUMERIC_POLICY,
            "configured_symbols": symbols,
            "source_dataset_content_sha256": self.source_dataset_content_sha256,
            "requested_start_boundary_time_ms": self.requested_start_boundary_time_ms,
            "requested_end_boundary_time_ms": self.requested_end_boundary_time_ms,
            "candles": [_candle_identity(item) for item in ordered],
        }
        object.__setattr__(self, "configured_symbols", symbols)
        object.__setattr__(self, "candles", ordered)
        object.__setattr__(self, "_by_symbol", MappingProxyType(grouped))
        object.__setattr__(self, "_by_open", MappingProxyType(by_open))
        object.__setattr__(self, "_opens", MappingProxyType(opens))
        object.__setattr__(self, "evidence_sha256", _sha256(payload))

    def by_symbol(self) -> Mapping[str, tuple[CompletedTradeOHLCCandle, ...]]:
        return self._by_symbol

    def _as_of(self, symbol: str, boundary: int):
        rows = self._by_symbol[symbol]
        position = bisect_right(self._opens[symbol], boundary - MINUTE_MS) - 1
        while position >= 0:
            candle = rows[position]
            if candle.close_time_ms < boundary and candle.first_seen_at_ms <= boundary:
                return candle
            position -= 1
        return None


@dataclass(frozen=True)
class SymbolForwardOutcome:
    symbol: str
    status: str
    unavailable_reason: str | None
    start_candle_open_time_ms: int | None
    end_candle_open_time_ms: int | None
    forward_log_return: float | None
    realized_volatility_status: str
    realized_volatility_unavailable_reason: str | None
    forward_realized_volatility: float | None


@dataclass(frozen=True)
class ForwardOutcome:
    decision_time_ms: int
    horizon_minutes: int
    configured_symbol_count: int
    available_symbol_count: int
    unavailable_symbol_count: int
    partial_coverage: bool
    market_forward_return: float | None
    market_forward_return_unavailable_reason: str | None
    future_directional_breadth: float | None
    future_directional_breadth_unavailable_reason: str | None
    future_directional_breadth_denominator: int
    market_forward_realized_volatility: float | None
    market_forward_realized_volatility_unavailable_reason: str | None
    realized_volatility_available_symbol_count: int
    realized_volatility_unavailable_symbol_count: int
    realized_volatility_partial_coverage: bool
    forward_cross_sectional_dispersion: float | None
    forward_cross_sectional_dispersion_unavailable_reason: str | None
    forward_cross_sectional_dispersion_denominator: int
    per_symbol: tuple[SymbolForwardOutcome, ...]


def _raw_log_return(start, end) -> float:
    # Difference of endpoint logs avoids an intermediate ratio overflow.
    value = math.log(float(end)) - math.log(float(start))
    if not math.isfinite(value):
        raise ArithmeticError("non-finite forward return")
    return value


def _symbol_forward(evidence, symbol, decision, horizon):
    end_decision = decision + horizon * MINUTE_MS
    rows = evidence.by_symbol()[symbol]
    start = evidence._as_of(symbol, decision)
    if start is None:
        return SymbolForwardOutcome(symbol, "UNAVAILABLE", START_PRICE_UNAVAILABLE,
                                    None, None, None, "UNAVAILABLE",
                                    START_PRICE_UNAVAILABLE, None)
    end = evidence._as_of(symbol, end_decision)
    if (end is None
            or end.open_time_ms != start.open_time_ms + horizon * MINUTE_MS):
        return SymbolForwardOutcome(symbol, "UNAVAILABLE", END_PRICE_UNAVAILABLE,
                                    start.open_time_ms, None, None, "UNAVAILABLE",
                                    END_PRICE_UNAVAILABLE, None)
    try:
        forward = _raw_log_return(start.close, end.close)
    except (ArithmeticError, OverflowError, ValueError):
        return SymbolForwardOutcome(symbol, "UNAVAILABLE", END_PRICE_UNAVAILABLE,
                                    start.open_time_ms, end.open_time_ms, None,
                                    "UNAVAILABLE", END_PRICE_UNAVAILABLE, None)
    returns = []
    prior = start
    complete = True
    for opening in range(start.open_time_ms + MINUTE_MS,
                         end.open_time_ms + MINUTE_MS, MINUTE_MS):
        candle = evidence._by_open[symbol].get(opening)
        if (candle is None or candle.close_time_ms >= end_decision
                or candle.first_seen_at_ms > end_decision):
            complete = False
            break
        try:
            returns.append(_raw_log_return(prior.close, candle.close))
        except (ArithmeticError, OverflowError, ValueError):
            complete = False
            break
        prior = candle
    rv = math.sqrt(math.fsum(value * value for value in returns)) if complete else None
    if rv is not None and not math.isfinite(rv):
        rv = None
    return SymbolForwardOutcome(
        symbol, "AVAILABLE", None, start.open_time_ms, end.open_time_ms, forward,
        "AVAILABLE" if rv is not None else "UNAVAILABLE",
        None if rv is not None else INCOMPLETE_FUTURE_MINUTE_PATH, rv)


def evaluate_forward_outcome(
    evidence: TradePriceForwardEvidence,
    decision_time_ms: int,
    horizon_minutes: int,
) -> ForwardOutcome:
    """Evaluate the frozen log-return, breadth, unscaled-RV and MAD labels."""
    if (not isinstance(evidence, TradePriceForwardEvidence)
            or type(decision_time_ms) is not int or decision_time_ms < 0
            or decision_time_ms % POINT_MS
            or horizon_minutes not in HORIZONS_MINUTES):
        raise ValueError("invalid forward-outcome request")
    rows = tuple(_symbol_forward(evidence, symbol, decision_time_ms,
                                 horizon_minutes)
                 for symbol in evidence.configured_symbols)
    available = tuple(item.forward_log_return for item in rows
                      if item.status == "AVAILABLE")
    rv_values = tuple(item.forward_realized_volatility for item in rows
                      if item.realized_volatility_status == "AVAILABLE")
    configured = len(rows)
    available_count = len(available)
    rv_count = len(rv_values)
    if available:
        center = median(available)
        breadth = sum(value > 0 for value in available) / available_count
        dispersion = (median(tuple(abs(value - center) for value in available))
                      if available_count >= 2 else None)
    else:
        center = breadth = dispersion = None
    rv_market = median(rv_values) if rv_values else None
    return ForwardOutcome(
        decision_time_ms, horizon_minutes, configured, available_count,
        configured - available_count, available_count != configured,
        center, None if center is not None else NO_AVAILABLE_SYMBOL_OUTCOME,
        breadth, None if breadth is not None else NO_AVAILABLE_SYMBOL_OUTCOME,
        available_count, rv_market,
        None if rv_market is not None else INCOMPLETE_FUTURE_MINUTE_PATH,
        rv_count, configured - rv_count, rv_count != configured,
        dispersion,
        None if dispersion is not None else INSUFFICIENT_CROSS_SECTION,
        available_count, rows)


def continuous_decision_times(start_boundary_time_ms: int,
                              end_boundary_time_ms: int,
                              horizon_minutes: int) -> tuple[int, ...]:
    if (type(start_boundary_time_ms) is not int
            or type(end_boundary_time_ms) is not int
            or start_boundary_time_ms % MINUTE_MS
            or end_boundary_time_ms - start_boundary_time_ms != 86_400_000
            or horizon_minutes not in HORIZONS_MINUTES):
        raise ValueError("continuous grids require one complete UTC day")
    step = horizon_minutes * MINUTE_MS
    return tuple(range(start_boundary_time_ms, end_boundary_time_ms, step))


def evaluate_continuous_grids(evidence, start_boundary_time_ms,
                              end_boundary_time_ms, *, outcome_lookup=None):
    """Return the 1/5/15/30/60-minute non-overlapping UTC grids (1,896 keys)."""
    result = tuple((horizon, tuple(
        (outcome_lookup(decision, horizon) if outcome_lookup is not None
         else evaluate_forward_outcome(evidence, decision, horizon))
        for decision in continuous_decision_times(
            start_boundary_time_ms, end_boundary_time_ms, horizon)))
        for horizon in HORIZONS_MINUTES)
    if sum(len(items) for _, items in result) != 1896:
        raise ValueError("complete UTC-day grids must contain 1,896 outcome keys")
    return result


def evaluate_event_outcomes(evidence, event_times_ms: Iterable[int],
                            horizon_minutes: int, *, outcome_lookup=None):
    """Retain all events and mark overlapping later confirmatory windows."""
    if horizon_minutes not in HORIZONS_MINUTES:
        raise ValueError("unsupported event horizon")
    ordered = tuple(sorted(event_times_ms))
    if (any(type(value) is not int or value < 0 or value % POINT_MS
            for value in ordered)
            or len(set(ordered)) != len(ordered)):
        raise ValueError("event times must be unique chronological 5-second boundaries")
    last_confirmatory = None
    observations = []
    for decision in ordered:
        independent = (last_confirmatory is None
                       or decision >= last_confirmatory + horizon_minutes * MINUTE_MS)
        if independent:
            last_confirmatory = decision
        observations.append({
            "decision_time_ms": decision,
            "horizon_minutes": horizon_minutes,
            "confirmatory_independent": independent,
            "outcome": (outcome_lookup(decision, horizon_minutes) if outcome_lookup is not None
                        else evaluate_forward_outcome(evidence, decision, horizon_minutes)),
        })
    return tuple(observations)


def evaluate_v1_state_path(
    state_by_boundary: Mapping[int, str | None],
    decision_time_ms: int,
    horizon_minutes: int,
    period_start_boundary_time_ms: int,
    period_end_boundary_time_ms: int,
) -> dict:
    """Secondary causal state labels, censored outside the replay interval."""
    if (horizon_minutes not in HORIZONS_MINUTES
            or decision_time_ms % POINT_MS
            or period_start_boundary_time_ms % POINT_MS
            or period_end_boundary_time_ms % POINT_MS):
        raise ValueError("invalid V1 state-path request")
    terminal = decision_time_ms + horizon_minutes * MINUTE_MS
    if (decision_time_ms < period_start_boundary_time_ms
            or decision_time_ms >= period_end_boundary_time_ms
            or terminal >= period_end_boundary_time_ms):
        return {"status": "CENSORED",
                "unavailable_reason": STATE_PATH_OUTSIDE_EVALUATION_INTERVAL,
                "initial_v1_direction_state": None,
                "terminal_v1_direction_state": None,
                "state_persistence": None,
                "v1_direction_persistence": None,
                "persistence_weakening": None,
                "reversed_within_horizon": None,
                "time_to_reversal_ms": None,
                "reversal_observation_status": "CENSORED",
                "reversal_unavailable_reason": STATE_PATH_OUTSIDE_EVALUATION_INTERVAL}
    expected = tuple(range(decision_time_ms, terminal + POINT_MS, POINT_MS))
    if any(boundary not in state_by_boundary for boundary in expected):
        return {"status": "UNAVAILABLE", "unavailable_reason": STATE_PATH_UNAVAILABLE,
                "initial_v1_direction_state": None,
                "terminal_v1_direction_state": None,
                "state_persistence": None,
                "v1_direction_persistence": None,
                "persistence_weakening": None,
                "reversed_within_horizon": None,
                "time_to_reversal_ms": None,
                "reversal_observation_status": "UNAVAILABLE",
                "reversal_unavailable_reason": STATE_PATH_UNAVAILABLE}
    initial = state_by_boundary[decision_time_ms]
    terminal_state = state_by_boundary[terminal]
    if initial not in USABLE_V1_STATES or terminal_state not in USABLE_V1_STATES:
        return {"status": "UNAVAILABLE", "unavailable_reason": STATE_PATH_UNAVAILABLE,
                "initial_v1_direction_state": initial,
                "terminal_v1_direction_state": terminal_state,
                "state_persistence": None,
                "v1_direction_persistence": None,
                "persistence_weakening": None,
                "reversed_within_horizon": None,
                "time_to_reversal_ms": None,
                "reversal_observation_status": "UNAVAILABLE",
                "reversal_unavailable_reason": STATE_PATH_UNAVAILABLE}
    directional = initial in ("BROAD_RISE", "BROAD_DROP")
    opposite = "BROAD_DROP" if initial == "BROAD_RISE" else "BROAD_RISE"
    reversal_path = tuple(state_by_boundary[boundary] for boundary in expected[1:])
    reversal_path_usable = all(state in USABLE_V1_STATES for state in reversal_path)
    reversal_time = (next((boundary for boundary in expected[1:]
                           if state_by_boundary[boundary] == opposite), None)
                     if directional and reversal_path_usable else None)
    if directional and not reversal_path_usable:
        reversal_status = "UNAVAILABLE"
        reversal_reason = STATE_PATH_UNAVAILABLE
    elif directional and reversal_time is not None:
        reversal_status = "OBSERVED_REVERSAL"
        reversal_reason = None
    elif directional:
        reversal_status = "CENSORED_NO_REVERSAL"
        reversal_reason = None
    else:
        reversal_status = "NOT_APPLICABLE"
        reversal_reason = None
    weakening = None
    if directional:
        weakening = ("PERSISTED" if terminal_state == initial else
                     "WEAKENED" if terminal_state == "NEUTRAL" else "REVERSED")
    return {"status": "AVAILABLE", "unavailable_reason": None,
            "initial_v1_direction_state": initial,
            "terminal_v1_direction_state": terminal_state,
            "state_persistence": initial == terminal_state,
            "v1_direction_persistence": (terminal_state == initial if directional else None),
            "persistence_weakening": weakening,
            "reversed_within_horizon": (reversal_time is not None
                                        if directional and reversal_path_usable else None),
            "time_to_reversal_ms": (reversal_time - decision_time_ms
                                    if reversal_time is not None else None),
            "reversal_observation_status": reversal_status,
            "reversal_unavailable_reason": reversal_reason}


def reject_retrospective_forward_labels(evidence_kind: str) -> None:
    """Guard the PELT hindsight path from ever acquiring causal labels."""
    if evidence_kind == "RETROSPECTIVE":
        raise ValueError("retrospective evidence cannot receive causal forward labels")
