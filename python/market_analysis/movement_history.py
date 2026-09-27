"""Pure #71 historical inputs from canonical completed one-minute candles."""

from dataclasses import dataclass
from decimal import Decimal
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
