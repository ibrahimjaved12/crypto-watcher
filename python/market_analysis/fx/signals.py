"""Forex TA signals (#374): the forward baseline strategies on trading-time candles.

Reuses the forward registry and signal identity (``signal_id = sha256(strategy_id|version|symbol|signal_ms|side)``)
with three forex rules: only strategies whose declared capabilities fit the instrument run; a signal
is dated at its candle's real end (``FxCandleSeries.end_ms``); and an entry is allowed only when the
whole time-limit window lies in ONE continuous open run of the calendar (no weekend, no break), so no
trade is truncated. A signal failing an entry check is still built and kept in ``suppressed`` with its
reason ("would have fired"), never silently dropped. Placebo controls for forex come later.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ..benchmark import ta_strategies as ta
from ..forward.bars_adapter import MINUTE_MS
from ..forward.signals import FORWARD_STRATEGIES, Signal, make_signal
from ..forward.strategy_capabilities import compatible_strategy_ids
from ..instruments import Instrument
from ..trading_calendar import TradingCalendar
from .bars import FLAG_MISSING_OPEN, FxBarSeries
from .candles import FxCandleSeries

FX_TIME_LIMIT_MULTIPLE = 4  # time limit = 4 x horizon, same as the crypto setups


@dataclass(frozen=True)
class SuppressedSignal:
    signal: Signal
    reason: str
    sessions: Tuple[str, ...]


@dataclass(frozen=True)
class FxSignalBatch:
    signals: List[Signal]
    suppressed: List[SuppressedSignal]
    reasons: Dict[str, str]
    sessions: Dict[str, Tuple[str, ...]]  # signal_id -> session tags at the decision time


def _entry_check(bars: FxBarSeries, calendar: TradingCalendar, signal_ms: int, horizon_min: int) -> Optional[str]:
    if not calendar.is_open(signal_ms):
        return "market_closed_at_entry"
    if bars.flags[bars.index_of(signal_ms)] == FLAG_MISSING_OPEN:
        return "entry_minute_missing"
    if calendar.open_until_ms(signal_ms) < signal_ms + FX_TIME_LIMIT_MULTIPLE * horizon_min * MINUTE_MS:
        return "window_exceeds_session"
    return None


def generate_fx_signals(instrument: Instrument, bars: FxBarSeries, candles: Dict[int, FxCandleSeries],
                        calendar: TradingCalendar, from_ms: int, to_ms: int, strategy_ids=None) -> FxSignalBatch:
    """Signals for candle closes in ``(from_ms, to_ms]`` (and inside the bars), plus the suppressed ones."""
    if type(from_ms) is not int or type(to_ms) is not int or to_ms < from_ms:
        raise ValueError("decision window must be integers with from_ms <= to_ms")
    if bars.instrument_id != instrument.instrument_id:
        raise ValueError("bars belong to another instrument")
    for minutes, series in candles.items():
        if series.symbol != bars.symbol or series.minutes != minutes:
            raise ValueError(f"{minutes}-minute candles do not match the bars")
    if strategy_ids is None:
        ids = sorted(item for item, strategy in FORWARD_STRATEGIES.items() if strategy.kind == "ta")
    else:
        ids = sorted(set(strategy_ids))
    unknown = [item for item in ids if item not in FORWARD_STRATEGIES]
    if unknown:
        raise ValueError(f"unknown forward strategies: {unknown}")

    reasons: Dict[str, str] = {}
    ta_ids = []
    for item in ids:
        if FORWARD_STRATEGIES[item].kind == "ta":
            ta_ids.append(item)
        else:
            reasons[item] = "placebo_not_supported_for_fx"
    runnable, refused = compatible_strategy_ids(instrument, ta_ids)
    for item, why in refused.items():
        reasons[item] = "incompatible:" + ",".join(why)

    last = min(to_ms, bars.end_ms - MINUTE_MS)  # the entry minute (open at signal_ms) must exist
    signals: List[Signal] = []
    suppressed: List[SuppressedSignal] = []
    sessions: Dict[str, Tuple[str, ...]] = {}
    for item in runnable:
        strategy = FORWARD_STRATEGIES[item]
        if strategy.timeframe_min not in candles:
            raise ValueError(f"no {strategy.timeframe_min}-minute candles supplied for {item}")
        series = candles[strategy.timeframe_min]
        if len(series) <= ta.first_signal_index(strategy.name):
            reasons[item] = "insufficient_history"
            continue
        sides = ta.strategy_sides(strategy.name, series)
        for index, side in enumerate(sides):
            end_ms = series.end_ms[index]
            if not side or not series.valid[index] or not from_ms < end_ms <= last:
                continue
            signal = make_signal(strategy, bars.symbol, end_ms, side)
            tags = calendar.session_tags(end_ms)
            sessions[signal.signal_id] = tags
            reason = _entry_check(bars, calendar, end_ms, strategy.horizon_min)
            if reason is None:
                signals.append(signal)
            else:
                suppressed.append(SuppressedSignal(signal, reason, tags))
    signals.sort(key=lambda s: (s.signal_ms, s.strategy_id, s.side))
    suppressed.sort(key=lambda s: (s.signal.signal_ms, s.signal.strategy_id, s.signal.side))
    return FxSignalBatch(signals, suppressed, reasons, sessions)


__all__ = ["FX_TIME_LIMIT_MULTIPLE", "FxSignalBatch", "SuppressedSignal", "generate_fx_signals"]
