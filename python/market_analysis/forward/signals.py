"""Forward signal registry and generation (OHLCV-computable strategies only).

Registry ``FORWARD_STRATEGIES`` (id -> ForwardStrategy):
- every ``ta-v1`` strategy of ``benchmark.ta_strategies.STRATEGIES`` at 15, 60 and 240 minutes,
  id ``NAME:TF`` (the rules are reused, not copied; the label horizon is the candle length);
- the CONTROL ``placebo-v1`` matched to each of them, id ``placebo-v1:NAME:TF``: at every hourly
  decision (``signal_ms % 3_600_000 == 0``) it emits a signal for the symbol with probability equal
  to the matched strategy's signal rate per hourly decision, side drawn independently. Draws are
  ``rng.u64_words(FORWARD_SEED, "forward-placebo:SYMBOL:MATCHED_ID", 2 * (signal_ms // 60_000), 2)``:
  word 0 decides emission (``word / 2**64 < rate``), word 1 the side (even -> long), so a decision
  is reproducible and stateless. The rate is the frozen ``historical_rate`` of the registry entry
  when set; while it is None the point-in-time rate of the matched strategy over the trailing
  30 days (signals with end in (t - 30 d, t] / 720 hourly decisions) is used and the reason
  ``rate:trailing-30d`` is reported.

``generate_signals(symbol, bars, from_ms, to_ms)`` covers decision times in ``(from_ms, to_ms]`` and
never later than the open of the last bar (the entry minute must exist). A strategy without
enough history returns no signal and a reason code; nothing is ever filled.
``Signal.signal_id = sha256("strategy_id|version|symbol|signal_ms|side")``: re-running is idempotent.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from fractions import Fraction
import hashlib

from ..benchmark import ta_strategies as ta
from ..benchmark.bars import BarSeries
from ..benchmark.candles import TIMEFRAMES
from ..benchmark.rng import u64_words
from .bars_adapter import MINUTE_MS, candles_from_bars

FORWARD_SEED = 20261009
PLACEBO_VERSION = "placebo-v1"
PLACEBO_STEP_MS = 3_600_000
RATE_WINDOW_MS = 30 * 86_400_000
RATE_DECISIONS = RATE_WINDOW_MS // PLACEBO_STEP_MS  # 720 hourly decisions
_TWO_64 = 2 ** 64


@dataclass(frozen=True)
class ForwardStrategy:
    strategy_id: str
    version: str
    kind: str                 # "ta" | "placebo"
    name: str                 # ta_strategies name (the matched one for a placebo)
    timeframe_min: int
    horizon_min: int
    matched_id: str | None = None
    historical_rate: Fraction | None = None  # placebo: frozen signals per hourly decision; None = trailing 30 d

    def config(self) -> dict:
        base = ta.strategy_config(self.name, self.timeframe_min)
        if self.kind == "ta":
            return base
        return {"kind": "placebo", "matched": self.matched_id, "matched_config": base,
                "decision_step_min": PLACEBO_STEP_MS // MINUTE_MS,
                "rate": "trailing-30d" if self.historical_rate is None else str(self.historical_rate)}


def _registry() -> dict:
    out = {}
    for name in ta.STRATEGIES:
        for minutes in TIMEFRAMES:
            ta_id = f"{name}:{minutes}"
            out[ta_id] = ForwardStrategy(ta_id, ta.VERSION, "ta", name, minutes, minutes)
            placebo_id = f"{PLACEBO_VERSION}:{ta_id}"
            out[placebo_id] = ForwardStrategy(placebo_id, PLACEBO_VERSION, "placebo", name, minutes, minutes,
                                              matched_id=ta_id)
    return out


FORWARD_STRATEGIES = _registry()


@dataclass(frozen=True)
class Signal:
    signal_id: str
    strategy_id: str
    version: str
    symbol: str
    signal_ms: int
    side: int
    horizon_min: int

    def to_dict(self) -> dict:
        return {"signal_id": self.signal_id, "strategy_id": self.strategy_id, "version": self.version,
                "symbol": self.symbol, "signal_ms": self.signal_ms, "side": self.side,
                "horizon_min": self.horizon_min}


def signal_id(strategy_id: str, version: str, symbol: str, signal_ms: int, side: int) -> str:
    return hashlib.sha256(f"{strategy_id}|{version}|{symbol}|{signal_ms}|{side}".encode("ascii")).hexdigest()


def make_signal(strategy: ForwardStrategy, symbol: str, signal_ms: int, side: int) -> Signal:
    return Signal(signal_id(strategy.strategy_id, strategy.version, symbol, signal_ms, side), strategy.strategy_id,
                  strategy.version, symbol, signal_ms, side, strategy.horizon_min)


def _ta_events(strategy: ForwardStrategy, candles) -> tuple[list, int | None]:
    """[(end_ms, side)] of every signal of the strategy on these candles, and the first end_ms that can
    carry one (None when the candles are too few for the warm-up)."""
    series = candles[strategy.timeframe_min]
    first = ta.first_signal_index(strategy.name)
    if len(series) <= first:
        return [], None
    sides = ta.strategy_sides(strategy.name, series)
    return [(series.end_ms[i], side) for i, side in enumerate(sides) if side], series.end_ms[first]


def placebo_draw(strategy_id: str, symbol: str, signal_ms: int) -> tuple[int, int]:
    words = u64_words(FORWARD_SEED, f"forward-placebo:{symbol}:{strategy_id}", 2 * (signal_ms // MINUTE_MS), 2)
    return int(words[0]), int(words[1])


def generate_signals(symbol: str, bars: BarSeries, from_ms: int, to_ms: int, strategy_ids=None) -> tuple[list, dict]:
    """(signals sorted by time then id, {strategy_id: reason}) for decisions in (from_ms, to_ms]."""
    if type(from_ms) is not int or type(to_ms) is not int or to_ms < from_ms:
        raise ValueError("decision window must be integers with from_ms <= to_ms")
    ids = sorted(FORWARD_STRATEGIES) if strategy_ids is None else list(strategy_ids)
    unknown = [item for item in ids if item not in FORWARD_STRATEGIES]
    if unknown:
        raise ValueError(f"unknown forward strategies: {unknown}")
    last = min(to_ms, bars.end_ms - MINUTE_MS)  # the entry minute (open at signal_ms) must exist
    candles = candles_from_bars(bars)
    events, warm = {}, {}
    signals, reasons = [], {}
    for item in ids:
        strategy = FORWARD_STRATEGIES[item]
        matched = item if strategy.kind == "ta" else strategy.matched_id
        if matched not in events:
            events[matched], warm[matched] = _ta_events(FORWARD_STRATEGIES[matched], candles)
        if warm[matched] is None:
            reasons[item] = "insufficient_history"
            continue
        if strategy.kind == "ta":
            for end_ms, side in events[matched]:
                if from_ms < end_ms <= last:
                    signals.append(make_signal(strategy, symbol, end_ms, side))
            continue
        times = [end_ms for end_ms, _ in events[matched]]
        first_decision = from_ms + PLACEBO_STEP_MS - from_ms % PLACEBO_STEP_MS
        skipped = 0
        for t in range(first_decision, last + 1, PLACEBO_STEP_MS):
            if strategy.historical_rate is not None:
                rate = strategy.historical_rate
            else:
                if t - RATE_WINDOW_MS < warm[matched]:
                    skipped += 1  # the matched strategy has no full 30-day history yet
                    continue
                rate = Fraction(bisect_right(times, t) - bisect_left(times, t - RATE_WINDOW_MS + 1), RATE_DECISIONS)
            emit, side_word = placebo_draw(matched, symbol, t)
            if Fraction(emit, _TWO_64) < rate:
                signals.append(make_signal(strategy, symbol, t, 1 if side_word % 2 == 0 else -1))
        if strategy.historical_rate is None:
            reasons[item] = "rate:trailing-30d" + (f"; insufficient_history for {skipped} decisions" if skipped else "")
    signals.sort(key=lambda s: (s.signal_ms, s.strategy_id, s.side))
    return signals, reasons
