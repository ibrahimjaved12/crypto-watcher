"""Pure calculation contract; see ../SPEC.md. All times are UTC epoch ms."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext

MINUTE = 60_000
WINDOWS = {5: 1, 15: 1, 60: 1, 240: 15, 1440: 15}


def positive(value) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("expected a finite positive decimal") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError("expected a finite positive decimal")
    return number


@dataclass(frozen=True)
class Candle:
    open_ms: int
    close: Decimal
    complete: bool = True


def percentage_change(previous, current) -> Decimal:
    previous, current = positive(previous), positive(current)
    with localcontext() as ctx:
        ctx.prec = 40
        return (current - previous) / previous * 100


def threshold_met(change, threshold) -> bool:
    threshold = positive(threshold)
    change = Decimal(str(change))
    if not change.is_finite():
        raise ValueError("change must be finite")
    return change.copy_abs() >= threshold


def analyze_window(candles, window_minutes, as_of_ms, threshold):
    """Require exact, contiguous completed bars; never fill gaps or use row offsets."""
    if window_minutes not in WINDOWS:
        raise ValueError("unsupported comparison window")
    threshold = positive(threshold)
    interval = WINDOWS[window_minutes] * MINUTE
    result = {"window_minutes": window_minutes, "interval_minutes": interval // MINUTE,
              "status": "unavailable", "reason": None, "change_pct": None,
              "threshold_met": None, "start_close_ms": None, "end_close_ms": None}
    by_open = {}
    for candle in candles:
        if (type(candle.open_ms) is not int or candle.open_ms < 0
                or candle.open_ms % interval or candle.open_ms > as_of_ms):
            return dict(result, reason="invalid_or_future_timestamp")
        if candle.open_ms in by_open:
            return dict(result, reason="duplicate_timestamp")
        try:
            close = positive(candle.close)
        except ValueError:
            return dict(result, reason="invalid_price")
        by_open[candle.open_ms] = Candle(candle.open_ms, close, candle.complete)
    completed = {t: c for t, c in by_open.items()
                 if c.complete and t + interval <= as_of_ms}
    if not completed:
        return dict(result, reason="no_completed_candles")
    end = max(completed)
    expected_close = as_of_ms // interval * interval
    lag = expected_close - (end + interval)
    result.update(end_close_ms=end + interval, lag_ms=lag)
    if lag > 10 * MINUTE:
        return dict(result, status="stale", reason="completed_data_lags_schedule")
    start = end - window_minutes * MINUTE
    result["start_close_ms"] = start + interval
    if any(t not in completed for t in range(start, end + interval, interval)):
        return dict(result, reason="missing_candles")
    change = percentage_change(completed[start].close, completed[end].close)
    return dict(result, status="ok", change_pct=str(change),
                start_price=str(completed[start].close), end_price=str(completed[end].close),
                threshold_met=threshold_met(change, threshold))


def analyze(series, as_of_ms, threshold="2"):
    return {str(window): analyze_window(series[interval], window, as_of_ms, threshold)
            for window, interval in WINDOWS.items()}
