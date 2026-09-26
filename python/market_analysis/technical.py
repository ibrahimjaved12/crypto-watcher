"""Deterministic TA v2 calculations with no I/O, clock, or framework dependency."""
from dataclasses import dataclass, field
from math import isfinite, sqrt

TA_VERSION = "ta-v2"
INTERPRETATION_VERSION = "interpretation-v1"
SUPPORTED_TIMEFRAMES = (15, 60, 240)
MINIMUM_HISTORY = 200
MINUTE = 60_000

BULLISH_PATTERNS = frozenset(
    ("hammer", "bullish_engulfing", "ema_bullish_cross", "rsi_recovered_30")
)
BEARISH_PATTERNS = frozenset(
    ("shooting_star", "bearish_engulfing", "ema_bearish_cross", "rsi_rejected_70")
)


@dataclass(frozen=True)
class FuturesInstrument:
    instrument_id: str
    exchange: str
    native_symbol: str
    market_type: str = "futures"
    contract_type: str = "perpetual"


@dataclass(frozen=True)
class TechnicalCandle:
    open_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    complete: bool = True


@dataclass(frozen=True)
class TechnicalConfig:
    ta_version: str = TA_VERSION
    interpretation_version: str = INTERPRETATION_VERSION
    minimum_history: int = MINIMUM_HISTORY


@dataclass(frozen=True)
class TechnicalInput:
    instrument: FuturesInstrument
    timeframe_minutes: int
    candles: tuple[TechnicalCandle, ...]
    source: str
    # Actual exchange event time, or None when the source has no exchange event
    # (e.g. REST bootstrap/recovery). Provenance only: completion is
    # `target_candle_open_time_ms + timeframe_minutes * MINUTE`.
    source_event_time_ms: int | None
    evaluation_time_ms: int
    detection_time_ms: int
    price_type: str = "trade"
    target_candle_open_time_ms: int | None = None
    warmup_candles: tuple[TechnicalCandle, ...] = ()
    missing_open_times_ms: tuple[int, ...] = ()
    config: TechnicalConfig = field(default_factory=TechnicalConfig)


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def _finite_or_none(value):
    return value if _finite(value) else None


def _base_result(request, status, reason):
    return {
        "schema_version": 2,
        "status": status,
        "reason": reason,
        "classification": "unavailable",
        "direction": "unavailable",
        "score": None,
        "atr_pct": None,
        "factor_breakdown": None,
        "reasons": [],
        "indicators": None,
        "patterns": [],
        "timeframe_minutes": request.timeframe_minutes,
        "candle_open_time_ms": None,
        "candle_close_time_ms": None,
        "source_event_time_ms": request.source_event_time_ms,
        "evaluation_time_ms": request.evaluation_time_ms,
        "detection_time_ms": request.detection_time_ms,
        "ta_version": request.config.ta_version,
        "strategy_version": request.config.interpretation_version,
        "provenance": {
            "instrument_id": request.instrument.instrument_id,
            "exchange": request.instrument.exchange,
            "native_symbol": request.instrument.native_symbol,
            "market_type": request.instrument.market_type,
            "contract_type": request.instrument.contract_type,
            "source": request.source,
            "price_type": request.price_type,
            "candle_count": 0,
            "warmup_candle_count": len(request.warmup_candles),
            "missing_open_times_ms": list(request.missing_open_times_ms),
        },
    }


def _validate_contract(request):
    instrument = request.instrument
    if (
        instrument.market_type != "futures"
        or instrument.contract_type != "perpetual"
        or not instrument.exchange
        or not instrument.native_symbol
        or instrument.instrument_id != f"{instrument.exchange}:{instrument.native_symbol}"
        or request.source != instrument.exchange
    ):
        return "invalid_futures_contract_identity"
    if request.price_type not in ("trade", "mark", "index"):
        return "unsupported_price_type"
    if request.timeframe_minutes not in SUPPORTED_TIMEFRAMES:
        return "unsupported_timeframe"
    if (
        request.config.ta_version != TA_VERSION
        or request.config.interpretation_version != INTERPRETATION_VERSION
        or request.config.minimum_history != MINIMUM_HISTORY
    ):
        return "unsupported_calculation_version"
    timestamps = (
        request.source_event_time_ms,
        request.evaluation_time_ms,
        request.detection_time_ms,
    )
    if any(value is not None and (type(value) is not int or value < 0)
           for value in timestamps):
        return "invalid_timestamp"
    if (
        (request.source_event_time_ms is not None
         and request.source_event_time_ms > request.evaluation_time_ms)
        or request.detection_time_ms > request.evaluation_time_ms
    ):
        return "future_timestamp"
    if request.target_candle_open_time_ms is not None:
        target = request.target_candle_open_time_ms
        if type(target) is not int or target < 0:
            return "invalid_target_candle"
        # Finality/no-lookahead uses the deterministic completion boundary, never the
        # exchange event time. The target candle must be complete at evaluation time.
        if target + request.timeframe_minutes * MINUTE > request.evaluation_time_ms:
            return "target_candle_not_complete"
    return None


def _completed_candles(request):
    duration = request.timeframe_minutes * MINUTE
    combined = request.warmup_candles + request.candles
    visible = []
    for candle in combined:
        if type(candle.open_ms) is not int or type(candle.complete) is not bool:
            return None, "invalid_ohlcv_or_gap"
        if candle.complete and candle.open_ms + duration <= request.evaluation_time_ms:
            visible.append(candle)
    candles = sorted(visible, key=lambda candle: candle.open_ms)
    if not candles:
        return None, "insufficient_history"
    seen = set()
    for index, candle in enumerate(candles):
        values = (candle.open, candle.high, candle.low, candle.close, candle.volume)
        if (
            type(candle.open_ms) is not int
            or candle.open_ms < 0
            or candle.open_ms % duration
            or candle.open_ms in seen
            or not all(_finite(value) for value in values)
            or candle.low <= 0
            or candle.volume < 0
            or candle.high < max(candle.open, candle.close)
            or candle.low > min(candle.open, candle.close)
            or (index and candle.open_ms - candles[index - 1].open_ms != duration)
        ):
            return None, "invalid_ohlcv_or_gap"
        seen.add(candle.open_ms)
    markers = request.missing_open_times_ms
    if any(type(value) is not int or value < 0 or value % duration for value in markers):
        return None, "invalid_gap_marker"
    if any(candles[0].open_ms <= value <= candles[-1].open_ms for value in markers):
        return None, "missing_candles"
    if request.evaluation_time_ms - (candles[-1].open_ms + duration) > duration + 120_000:
        return None, "stale_candles"
    target = request.target_candle_open_time_ms
    if target is not None:
        if type(target) is not int or target < 0 or target % duration:
            return None, "invalid_target_candle"
        if not any(candle.open_ms == target for candle in candles):
            return None, "target_candle_unavailable"
        candles = [candle for candle in candles if candle.open_ms <= target]
    if len(candles) < request.config.minimum_history:
        return None, "insufficient_history"
    return candles, None


def _ema(values, period):
    output = [None] * len(values)
    if len(values) < period:
        return output
    total = 0.0
    for value in values[:period]:
        total += value
    previous = total / period
    output[period - 1] = previous
    exponent = 2 / (period + 1)
    for index in range(period, len(values)):
        previous = (values[index] - previous) * exponent + previous
        output[index] = previous
    return output


def _wema(values, period):
    output = [None] * len(values)
    if len(values) < period:
        return output
    total = 0.0
    for value in values[:period]:
        total += value
    previous = total / period
    output[period - 1] = previous
    for index in range(period, len(values)):
        previous = ((previous * (period - 1)) + values[index]) / period
        output[index] = previous
    return output


def _rsi(values, period=14):
    if len(values) <= period:
        return []
    gains, losses = [], []
    for previous, current in zip(values, values[1:]):
        change = current - previous
        gains.append(change if change > 0 else 0.0)
        losses.append(-change if change < 0 else 0.0)
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period
    output = []
    for index in range(period - 1, len(gains)):
        if index >= period:
            average_gain = ((average_gain * (period - 1)) + gains[index]) / period
            average_loss = ((average_loss * (period - 1)) + losses[index]) / period
        if average_loss == 0:
            value = 100.0
        elif average_gain == 0:
            value = 0.0
        else:
            value = round(100 - (100 / (1 + average_gain / average_loss)), 2)
        output.append(value)
    return output


def _true_ranges(candles):
    return [
        max(
            candle.high - candle.low,
            abs(candle.high - candles[index - 1].close),
            abs(candle.low - candles[index - 1].close),
        )
        for index, candle in enumerate(candles)
        if index > 0
    ]


def _macd(values):
    fast, slow = _ema(values, 12), _ema(values, 26)
    lines = [fast[index] - slow[index] for index in range(25, len(values))]
    signals = _ema(lines, 9)
    histograms = [
        line - signal if signal is not None else None
        for line, signal in zip(lines, signals)
    ]
    return {
        "line": _finite_or_none(lines[-1]),
        "signal": _finite_or_none(signals[-1]),
        "histogram": _finite_or_none(histograms[-1]),
        "previous_histogram": _finite_or_none(histograms[-2]),
    }


def _adx(candles, period=14):
    true_ranges, plus_dm, minus_dm = [], [], []
    for previous, current in zip(candles, candles[1:]):
        true_ranges.append(max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        ))
        up_move = current.high - previous.high
        down_move = previous.low - current.low
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
    if len(true_ranges) < period:
        return None, None, None
    tr_sum = sum(true_ranges[:period])
    plus_sum = sum(plus_dm[:period])
    minus_sum = sum(minus_dm[:period])
    dx_values = []
    pdi = mdi = None
    for index in range(period - 1, len(true_ranges)):
        if index >= period:
            tr_sum = tr_sum - tr_sum / period + true_ranges[index]
            plus_sum = plus_sum - plus_sum / period + plus_dm[index]
            minus_sum = minus_sum - minus_sum / period + minus_dm[index]
        pdi = plus_sum * 100 / tr_sum if tr_sum else float("nan")
        mdi = minus_sum * 100 / tr_sum if tr_sum else float("nan")
        denominator = pdi + mdi
        dx_values.append(abs(pdi - mdi) / denominator * 100 if denominator else float("nan"))
    adx_values = _wema(dx_values, period)
    return _finite_or_none(adx_values[-1]), _finite_or_none(pdi), _finite_or_none(mdi)


def detect_patterns(candle, previous, trend):
    candle_range = candle.high - candle.low
    if candle_range <= 0:
        return []
    body = abs(candle.close - candle.open)
    upper = candle.high - max(candle.open, candle.close)
    lower = min(candle.open, candle.close) - candle.low
    found = []
    if body <= candle_range * 0.1:
        found.append("doji")
    if body > candle_range * 0.1 and lower >= body * 2 and upper <= body * 0.5 and trend < 0:
        found.append("hammer")
    if body > candle_range * 0.1 and upper >= body * 2 and lower <= body * 0.5 and trend > 0:
        found.append("shooting_star")
    if (
        candle.close > candle.open
        and previous.close < previous.open
        and candle.open <= previous.close
        and candle.close >= previous.open
        and body > abs(previous.close - previous.open)
    ):
        found.append("bullish_engulfing")
    if (
        candle.close < candle.open
        and previous.close > previous.open
        and candle.open >= previous.close
        and candle.close <= previous.open
        and body > abs(previous.close - previous.open)
    ):
        found.append("bearish_engulfing")
    return found


def calculate_indicators(candles):
    closes = [candle.close for candle in candles]
    ema20, ema50, ema200 = _ema(closes, 20), _ema(closes, 50), _ema(closes, 200)
    rsi = _rsi(closes)
    true_ranges = _true_ranges(candles)
    atr = _wema(true_ranges, 14)
    last, previous = candles[-1], candles[-2]
    prior = candles[-21:-1]
    volume_average = sum(candle.volume for candle in prior) / 20
    detected = detect_patterns(last, previous, candles[-2].close - candles[-5].close)
    if ema20[-2] <= ema50[-2] and ema20[-1] > ema50[-1]:
        detected.append("ema_bullish_cross")
    if ema20[-2] >= ema50[-2] and ema20[-1] < ema50[-1]:
        detected.append("ema_bearish_cross")
    if rsi[-2] < 30 and rsi[-1] >= 30:
        detected.append("rsi_recovered_30")
    if rsi[-2] > 70 and rsi[-1] <= 70:
        detected.append("rsi_rejected_70")
    if volume_average > 0 and last.volume >= 2 * volume_average:
        detected.append("volume_spike")
    band_values = closes[-20:]
    middle = sum(band_values) / 20
    deviation = sqrt(sum((value - middle) ** 2 for value in band_values) / 20)
    upper, lower = middle + 2 * deviation, middle - 2 * deviation
    adx, plus_di, minus_di = _adx(candles)
    return {
        "candle": {
            "open_ms": last.open_ms,
            "open": last.open,
            "high": last.high,
            "low": last.low,
            "close": last.close,
            "volume": last.volume,
            "complete": last.complete,
        },
        "previous_candle": {
            "open_ms": previous.open_ms,
            "open": previous.open,
            "high": previous.high,
            "low": previous.low,
            "close": previous.close,
            "volume": previous.volume,
            "complete": previous.complete,
        },
        "ema20": ema20[-1],
        "ema50": ema50[-1],
        "ema200": _finite_or_none(ema200[-1]),
        "macd": _macd(closes),
        "bollinger": {
            "middle": _finite_or_none(middle),
            "upper": _finite_or_none(upper),
            "lower": _finite_or_none(lower),
            "bandwidth_pct": _finite_or_none((upper - lower) / middle * 100 if middle > 0 else None),
            "percent_b": _finite_or_none((last.close - lower) / (upper - lower) if upper > lower else None),
        },
        "adx14": adx,
        "plus_di14": plus_di,
        "minus_di14": minus_di,
        "range20": {
            "low": min(candle.low for candle in prior),
            "high": max(candle.high for candle in prior),
        },
        "volume_average20": volume_average,
        "volume_ratio": _finite_or_none(last.volume / volume_average if volume_average > 0 else None),
        "candle_count": len(candles),
        "rsi14": rsi[-1],
        "atr14": atr[-1],
        "volume_change_pct": (last.volume / volume_average - 1) * 100 if volume_average > 0 else None,
        "patterns": detected,
    }


def interpret_indicators(price, indicators, patterns):
    fast, slow = indicators.get("ema20"), indicators.get("ema50")
    rsi, atr = indicators.get("rsi14"), indicators.get("atr14")
    volume = indicators.get("volume_change_pct")
    trend_valid = (
        _finite(price) and price > 0 and _finite(fast) and fast > 0 and _finite(slow) and slow > 0
    )
    trend_direction = (
        1 if trend_valid and price > fast > slow
        else -1 if trend_valid and price < fast < slow
        else 0 if trend_valid
        else None
    )
    trend = (
        "bullish" if trend_direction == 1
        else "bearish" if trend_direction == -1
        else "neutral" if trend_direction == 0
        else "unavailable"
    )
    momentum_valid = _finite(rsi) and 0 <= rsi <= 100
    if not momentum_valid:
        momentum, momentum_points = "unavailable", None
    elif rsi < 30:
        momentum, momentum_points = "oversold", -20
    elif rsi < 45:
        momentum, momentum_points = "weak", -10
    elif rsi <= 55:
        momentum, momentum_points = "neutral", 0
    elif rsi <= 70:
        momentum, momentum_points = "strong", 10
    else:
        momentum, momentum_points = "overbought", 20
    up = any(pattern in BULLISH_PATTERNS for pattern in patterns)
    down = any(pattern in BEARISH_PATTERNS for pattern in patterns)
    pattern_direction = 0 if up == down else 1 if up else -1
    volume_valid = _finite(volume) and volume >= -100
    aligned = pattern_direction != 0 and pattern_direction == trend_direction
    if up and down:
        support, pattern_reason = "conflicting_directions", "patterns_conflicting"
    elif not up and not down:
        support, pattern_reason = "no_directional_pattern", "patterns_none"
    elif trend_direction is None:
        support, pattern_reason = "trend_unavailable", "patterns_trend_unavailable"
    elif not aligned:
        support, pattern_reason = "against_or_neutral_trend", "patterns_not_aligned"
    elif not volume_valid:
        support, pattern_reason = "aligned_volume_unavailable", "volume_unavailable"
    elif volume < 0:
        support, pattern_reason = "aligned_below_average_volume", "volume_below_average"
    else:
        support, pattern_reason = "aligned_volume_supported", "volume_supported"
    contributions = {
        "trend": None if trend_direction is None else trend_direction * 40,
        "momentum": momentum_points,
        "patterns": pattern_direction * 20,
        "volume": pattern_direction * 20 if volume_valid and aligned and volume >= 0 else 0 if volume_valid else None,
    }
    score = sum(contributions.values()) if all(_finite(value) for value in contributions.values()) else None
    classification = (
        "bullish" if score is not None and score > 0
        else "bearish" if score is not None and score < 0
        else "neutral" if score == 0
        else "unavailable"
    )
    factors = {
        "trend": {
            "classification": trend,
            "contribution": contributions["trend"],
            "reason": f"trend_{trend}",
        },
        "momentum": {
            "classification": momentum,
            "contribution": contributions["momentum"],
            "reason": f"momentum_{momentum}",
        },
        "patterns": {
            "classification": support,
            "contribution": contributions["patterns"],
            "reason": pattern_reason,
        },
        "volume": {
            "classification": "unavailable" if not volume_valid else "supported" if aligned and volume >= 0 else "unsupported",
            "contribution": contributions["volume"],
            "reason": "volume_unavailable" if not volume_valid else "volume_aligned" if aligned and volume >= 0 else "volume_not_aligned",
        },
    }
    return {
        "classification": classification,
        "direction": classification,
        "trend": trend,
        "momentum": momentum,
        "support": support,
        "score": score,
        "factor_breakdown": factors,
        "reasons": [factor["reason"] for factor in factors.values()],
        "atr_pct": _finite_or_none(atr / price * 100 if _finite(atr) and atr >= 0 and _finite(price) and price > 0 else None),
    }


def calculate_technical_analysis(request):
    """Calculate one versioned snapshot solely from the supplied immutable input."""
    reason = _validate_contract(request)
    if reason:
        return _base_result(request, "unavailable", reason)
    candles, reason = _completed_candles(request)
    if reason:
        status = "insufficient" if reason == "insufficient_history" else "unavailable"
        return _base_result(request, status, reason)
    indicators = calculate_indicators(candles)
    interpretation = interpret_indicators(
        candles[-1].close, indicators, indicators["patterns"]
    )
    result = _base_result(request, "ok", None)
    result.update(
        classification=interpretation["classification"],
        direction=interpretation["direction"],
        score=interpretation["score"],
        atr_pct=interpretation["atr_pct"],
        factor_breakdown=interpretation["factor_breakdown"],
        reasons=interpretation["reasons"],
        indicators=indicators,
        patterns=indicators["patterns"],
        candle_open_time_ms=candles[-1].open_ms,
        candle_close_time_ms=candles[-1].open_ms + request.timeframe_minutes * MINUTE,
    )
    result["provenance"]["candle_count"] = len(candles)
    return result
