"""Pure #71 historical inputs from canonical completed one-minute candles."""

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from itertools import islice
import math

from .movement_metrics import HistoricalWindowInput, MarketMovementConfig, WINDOWS

MINUTE_MS = 60_000


@dataclass(frozen=True)
class CompletedMovementCandle:
    open_time_ms: int
    close: Decimal
    volume: Decimal
    quote_volume: Decimal


def build_historical_window_inputs(candles, evaluation_boundary_time_ms: int,
                                   config: MarketMovementConfig):
    """Return boundary-specific aligned samples from the trusted trailing run.

    A conflicting duplicate is rejected; identical duplicate identities are
    harmless. This keeps results independent of transport row ordering.
    """
    if (type(evaluation_boundary_time_ms) is not int or
            evaluation_boundary_time_ms < 0 or
            evaluation_boundary_time_ms % 5_000):
        raise ValueError("historical evaluation boundary must align to five seconds")
    by_open = {}
    for candle in candles:
        if not isinstance(candle, CompletedMovementCandle):
            raise ValueError("history must contain completed movement candles")
        if (type(candle.open_time_ms) is not int or candle.open_time_ms < 0 or
                candle.open_time_ms % MINUTE_MS or
                not candle.close.is_finite() or candle.close <= 0 or
                not candle.volume.is_finite() or candle.volume < 0 or
                not candle.quote_volume.is_finite() or candle.quote_volume < 0):
            raise ValueError("invalid canonical movement candle")
        if candle.open_time_ms + MINUTE_MS >= evaluation_boundary_time_ms:
            continue
        previous = by_open.get(candle.open_time_ms)
        if previous is not None and previous != candle:
            raise ValueError("conflicting duplicate movement candle")
        by_open[candle.open_time_ms] = candle
    if not by_open:
        return {}
    opens = sorted(by_open)
    first = len(opens) - 1
    while first > 0 and opens[first] - opens[first - 1] == MINUTE_MS:
        first -= 1
    run_opens = opens[first:]
    oldest = run_opens[0]
    coverage = run_opens[-1] + MINUTE_MS - oldest
    result = {}
    for window in WINDOWS:
        window_ms = window * MINUTE_MS
        returns = []
        notionals = []
        for current_open in run_opens:
            end = current_open + MINUTE_MS
            if (end % window_ms or
                    end < evaluation_boundary_time_ms - config.historical_lookback_ms or
                    end >= evaluation_boundary_time_ms):
                continue
            previous_open = end - window_ms - MINUTE_MS
            if previous_open < oldest:
                continue
            previous = by_open[previous_open]
            current = by_open[current_open]
            value = math.log(float(current.close / previous.close))
            if not math.isfinite(value):
                raise ValueError("historical return is not finite")
            notional = sum((by_open[open_time].quote_volume
                            for open_time in range(end - window_ms, end, MINUTE_MS)),
                           Decimal(0))
            returns.append(value)
            notionals.append(notional)
        if returns:
            result[window] = HistoricalWindowInput(
                tuple(returns), coverage,
                tuple(notionals[-config.rvol_comparison_windows:]),
            )
    return result


class _IncrementalFallback(Exception):
    """The incremental state cannot prove equivalence for this input."""


def _valid_movement_candle(candle):
    """The per-candle checks of build_historical_window_inputs, as a predicate."""
    try:
        return (isinstance(candle, CompletedMovementCandle) and
                type(candle.open_time_ms) is int and candle.open_time_ms >= 0 and
                not candle.open_time_ms % MINUTE_MS and
                candle.close.is_finite() and candle.close > 0 and
                candle.volume.is_finite() and candle.volume >= 0 and
                candle.quote_volume.is_finite() and candle.quote_volume >= 0)
    except Exception:
        return False


# Candles needed behind the newest one: the widest window plus its start price.
_TAIL_CANDLES = max(WINDOWS) + 1


class _IncrementalWindowState:
    """Trailing contiguous run of one append-only visible candle list.

    Holds only what build_historical_window_inputs reads from the run: its
    oldest open, the newest candles, and per-window (end, return, notional)
    rows inside the lookback. Candles at or past the boundary wait in pending.
    """

    def __init__(self, candles, boundary, config, generation):
        self.candles = candles
        self.seen = 0
        self.last_seen = None
        self.generation = generation
        self.boundary = boundary
        self.config = config
        self.pending = {}
        self.oldest = None
        self.last_open = None
        self.tail = []
        self.windows = tuple(
            (window, deque(), deque(), deque(maxlen=config.rvol_comparison_windows))
            for window in WINDOWS)

    @classmethod
    def seed(cls, candles, boundary, config, generation, expected):
        """Rebuild from the full list after the reference builder succeeded."""
        try:
            state = cls(candles, boundary, config, generation)
            by_open = {}
            for candle in candles:
                if not _valid_movement_candle(candle):
                    return None
                if candle.open_time_ms + MINUTE_MS >= boundary:
                    if candle.open_time_ms in state.pending:
                        return None
                    state.pending[candle.open_time_ms] = candle
                else:
                    # The reference already rejected conflicting duplicates.
                    by_open[candle.open_time_ms] = candle
            if by_open:
                opens = sorted(by_open)
                first = len(opens) - 1
                while first > 0 and opens[first] - opens[first - 1] == MINUTE_MS:
                    first -= 1
                for open_time in opens[first:]:
                    state._extend(by_open[open_time])
            state.seen = len(candles)
            state.last_seen = candles[-1] if candles else None
            if state.result() != expected:
                return None
            return state
        except Exception:
            return None

    def advance(self, candles, boundary, config, generation):
        """Absorb newly appended candles and a later boundary, or raise fallback."""
        if (config is not self.config or candles is not self.candles or
                type(boundary) is not int or boundary < 0 or boundary % 5_000 or
                boundary < self.boundary):
            raise _IncrementalFallback
        count = len(candles)
        if (count < self.seen or generation - self.generation != count - self.seen or
                (self.seen and candles[self.seen - 1] is not self.last_seen)):
            raise _IncrementalFallback
        for candle in islice(candles, self.seen, count):
            if not _valid_movement_candle(candle):
                raise _IncrementalFallback
            open_time = candle.open_time_ms
            # Duplicates (identical or not) and out-of-order opens use the reference.
            if (open_time in self.pending or
                    (self.last_open is not None and open_time <= self.last_open)):
                raise _IncrementalFallback
            self.pending[open_time] = candle
        self.seen = count
        self.last_seen = candles[-1] if count else None
        self.generation = generation
        self.boundary = boundary
        ready = sorted(open_time for open_time in self.pending
                       if open_time + MINUTE_MS < boundary)
        for open_time in ready:
            self._extend(self.pending.pop(open_time))
        earliest = boundary - config.historical_lookback_ms
        for _, ends, returns, notionals in self.windows:
            while ends and ends[0] < earliest:
                ends.popleft()
                returns.popleft()
            while len(notionals) > len(returns):
                notionals.popleft()
        return self.result()

    def _extend(self, candle):
        open_time = candle.open_time_ms
        if self.last_open is None:
            self.oldest = open_time
        elif open_time != self.last_open + MINUTE_MS:
            # A gap restarts the trailing run; the reference handles that.
            raise _IncrementalFallback
        self.last_open = open_time
        tail = self.tail
        tail.append(candle)
        if len(tail) > _TAIL_CANDLES:
            del tail[0]
        end = open_time + MINUTE_MS
        if end < self.boundary - self.config.historical_lookback_ms:
            return
        for window, ends, returns, notionals in self.windows:
            window_ms = window * MINUTE_MS
            if end % window_ms or end - window_ms - MINUTE_MS < self.oldest:
                continue
            previous = tail[-1 - window]
            value = math.log(float(candle.close / previous.close))
            if not math.isfinite(value):
                raise _IncrementalFallback
            notional = sum((item.quote_volume for item in tail[-window:]), Decimal(0))
            ends.append(end)
            returns.append(value)
            notionals.append(notional)

    def result(self):
        if self.last_open is None:
            return {}
        coverage = self.last_open + MINUTE_MS - self.oldest
        result = {}
        for window, _, returns, notionals in self.windows:
            if returns:
                result[window] = HistoricalWindowInput(
                    tuple(returns), coverage, tuple(notionals))
        return result


class IncrementalHistoricalWindowInputs:
    """Per-symbol incremental equivalent of build_historical_window_inputs.

    For an append-only candle list (generation counts appends) and a
    nondecreasing boundary, each new candle is validated once and only new
    rows are computed. Any input it cannot prove equivalent is delegated to
    build_historical_window_inputs, which also supplies every exception, and
    the state is then reseeded from the full list.
    """

    def __init__(self):
        self._state = None

    def build(self, candles, evaluation_boundary_time_ms, config, generation):
        state, self._state = self._state, None
        if state is not None:
            try:
                result = state.advance(candles, evaluation_boundary_time_ms,
                                       config, generation)
            except Exception:
                pass
            else:
                self._state = state
                return result
        result = build_historical_window_inputs(candles, evaluation_boundary_time_ms, config)
        self._state = _IncrementalWindowState.seed(
            candles, evaluation_boundary_time_ms, config, generation, result)
        return result
