"""Read-only analysis using existing calculations and cumulative state transitions."""
import asyncio
import time

import httpx

from .core import MINUTE, analyze, percentage_change, positive
from .cumulative import Baseline, observe
from .providers import (ANALYSIS_PROVIDERS, exchange_info_url, instrument, parse,
                        parse_technical, provider_endpoint, request_url,
                        source_instrument, validate_exchange_info)
from .technical import (MINIMUM_HISTORY, SUPPORTED_TIMEFRAMES, TA_VERSION,
                        INTERPRETATION_VERSION, FuturesInstrument, TechnicalConfig,
                        TechnicalInput, calculate_technical_analysis)


async def load_series(client, provider, symbol):
    metadata_url = exchange_info_url(provider, symbol)
    if metadata_url:
        metadata = await client.get(metadata_url)
        metadata.raise_for_status()
        if len(metadata.content) > 4_000_000:
            raise ValueError("oversized exchange information response")
        validate_exchange_info(metadata.json(), symbol, provider)
    async def get(interval):
        response = await client.get(request_url(provider, symbol, interval))
        response.raise_for_status()
        if len(response.content) > 2_000_000:
            raise ValueError("oversized provider response")
        return parse(provider, response.json(), interval)
    results = await asyncio.gather(get(1), get(15), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return dict(zip((1, 15), results))


async def load_workload(client, provider, symbol):
    """Fetch one provider's complete movement and TA workload concurrently."""
    metadata_url = exchange_info_url(provider, symbol)
    if metadata_url:
        metadata = await client.get(metadata_url)
        metadata.raise_for_status()
        if len(metadata.content) > 4_000_000:
            raise ValueError("oversized exchange information response")
        validate_exchange_info(metadata.json(), symbol, provider)

    async def get(interval, limit):
        response = await client.get(request_url(provider, symbol, interval, limit))
        response.raise_for_status()
        if len(response.content) > 2_000_000:
            raise ValueError("oversized provider response")
        return response.json()

    intervals = ((1, 62), (15, 250), (60, 250), (240, 250))
    bodies = await asyncio.gather(*(get(interval, limit) for interval, limit in intervals),
                                  return_exceptions=True)
    for result in bodies:
        if isinstance(result, BaseException):
            raise result
    raw = dict(zip((interval for interval, _ in intervals), bodies))
    return {
        "rolling": {1: parse(provider, raw[1], 1), 15: parse(provider, raw[15], 15)},
        "technical": {timeframe: parse_technical(provider, raw[timeframe], timeframe)
                      for timeframe in SUPPORTED_TIMEFRAMES},
        "source_instrument": source_instrument(provider, symbol),
    }


def latest_close(candles, now_ms):
    # Same completion/freshness policy as the existing monitor, independent of
    # whether enough history is available for every rolling window.
    seen = set()
    for candle in candles:
        if (type(candle.open_ms) is not int or candle.open_ms < 0
                or candle.open_ms % MINUTE or candle.open_ms > now_ms
                or candle.open_ms in seen):
            raise ValueError("invalid candle timestamps")
        positive(candle.close)
        seen.add(candle.open_ms)
    completed = [c for c in candles if c.complete and c.open_ms + MINUTE <= now_ms]
    if not completed:
        raise ValueError("no completed candle")
    last = max(completed, key=lambda c: c.open_ms)
    if now_ms - (last.open_ms + MINUTE) > 10 * MINUTE:
        raise ValueError("stale candle")
    return last


def baseline_preview(request, price, observed_ms, source, now_ms):
    saved = request.baseline
    result = {"status": "unavailable", "change_pct": None, "direction": None,
              "baseline_price": str(saved.price) if saved else None,
              "baseline_at_ms": saved.at_ms if saved else None,
              "cooldown_evaluated": False, "cooldown_until_ms": None,
              "eligibility_evaluated": False, "alert_eligible": None}
    if price is None:
        return result
    state = Baseline(**saved.model_dump()) if saved else None
    if state and any(t is not None and t > now_ms for t in (
            state.at_ms, state.last_observed_ms, state.last_up_alert_ms, state.last_down_alert_ms)):
        return dict(result, status="invalid_state")
    if state and state.source == source:
        result["change_pct"] = str(percentage_change(state.price, price))
    if not request.settings.monitoring_enabled:
        return dict(result, status="disabled", eligibility_evaluated=True, alert_eligible=False)
    # Discard the proposed state: this request must never initialize/reset/save it.
    _, transition = observe(state, price, observed_ms, source, now_ms,
                            request.settings.threshold_pct, request.settings.cooldown_minutes)
    status = transition["status"]
    if status in ("initialized", "reinitialized"):
        return dict(result, status="baseline_required" if status == "initialized" else "baseline_reset_required")
    result.update(status="eligible" if status == "alerted" else status,
                  eligibility_evaluated=True, alert_eligible=status == "alerted",
                  direction=transition.get("direction"))
    if status in ("alerted", "cooldown"):
        result["cooldown_evaluated"] = True
        last_alert = state.last_up_alert_ms if transition["direction"] == "up" else state.last_down_alert_ms
        result["cooldown_until_ms"] = (last_alert + request.settings.cooldown_minutes * MINUTE
                                       if last_alert is not None else None)
    return result


def compact_technical(result):
    return {key: result[key] for key in (
        "schema_version", "status", "reason", "classification", "direction", "score",
        "atr_pct", "factor_breakdown", "reasons", "patterns", "timeframe_minutes",
        "candle_open_time_ms", "candle_close_time_ms", "source_event_time_ms",
        "evaluation_time_ms", "detection_time_ms", "ta_version", "strategy_version",
        "provenance")}


def technical_results(candles_by_frame, provider, symbol, now_ms):
    identity = source_instrument(provider, symbol)
    contract = FuturesInstrument(**identity)
    output = {}
    for timeframe in SUPPORTED_TIMEFRAMES:
        candles = candles_by_frame[timeframe]
        request = TechnicalInput(
            instrument=contract,
            timeframe_minutes=timeframe,
            candles=candles,
            source=provider,
            # The manual path fetches REST klines, which carry no exchange event time;
            # record the absence honestly instead of synthesizing the completion boundary.
            source_event_time_ms=None,
            evaluation_time_ms=now_ms,
            detection_time_ms=now_ms,
            price_type="trade",
            config=TechnicalConfig(ta_version=TA_VERSION,
                                   interpretation_version=INTERPRETATION_VERSION,
                                   minimum_history=MINIMUM_HISTORY),
        )
        output[str(timeframe)] = compact_technical(calculate_technical_analysis(request))
    return output


def incomplete_category(rolling, technical):
    reasons = [row.get("reason") for row in rolling.values() if row.get("status") != "ok"]
    reasons += [row.get("reason") for row in technical.values() if row.get("status") != "ok"]
    if any(reason in ("stale", "stale_candles") for reason in reasons):
        return "stale_data"
    if any(reason in ("insufficient_history", "missing_candles", "no_completed_candles")
           for reason in reasons):
        return "insufficient_history"
    return "invalid_data" if reasons else None


def _analyze_workload(request, workload, provider, now_ms, attempts):
    series = workload["rolling"]
    last = latest_close(series[1], now_ms)
    rolling = analyze(series, now_ms, request.settings.threshold_pct)
    technical = technical_results(workload["technical"], provider, request.symbol, now_ms)
    baseline = baseline_preview(request, last.close, last.open_ms + MINUTE, provider, now_ms)
    status = "ok" if (all(w["status"] == "ok" for w in rolling.values())
                      and all(w["status"] == "ok" for w in technical.values())) else "partial"
    return {"schema_version": 1, "mode": "read_only", "symbol": request.symbol,
            "status": status,
            "source": provider, "as_of_ms": now_ms, "price": str(last.close),
            "observed_at_ms": last.open_ms + MINUTE,
            "instrument": instrument(request.symbol), "price_type": "trade",
            "source_instrument": workload["source_instrument"],
            "endpoint": provider_endpoint(provider), "retrieved_at_ms": now_ms,
            "threshold_pct": str(request.settings.threshold_pct),
            "rolling": rolling, "technical": technical, "baseline": baseline,
            "failure_category": incomplete_category(rolling, technical),
            "attempts": attempts}


async def analyze_request(request, client, loader=load_workload,
                          clock=lambda: time.time_ns() // 1_000_000):
    attempts = []
    for provider in ANALYSIS_PROVIDERS:
        try:
            workload = await loader(client, provider, request.symbol)
            return await asyncio.to_thread(
                _analyze_workload, request, workload, provider, clock(), attempts)
        except httpx.TimeoutException:
            attempts.append({"source": provider, "reason": "provider_timeout"})
        except httpx.HTTPError:
            attempts.append({"source": provider, "reason": "provider_unavailable"})
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, ArithmeticError) as error:
            message = str(error).lower()
            reason = ("stale_data" if "stale" in message
                      else "insufficient_history" if "no completed" in message
                      else "invalid_data")
            attempts.append({"source": provider, "reason": reason})
    now_ms = clock()
    return {"schema_version": 1, "mode": "read_only", "symbol": request.symbol,
            "status": "unavailable", "source": None, "as_of_ms": now_ms,
            "price": None, "observed_at_ms": None,
            "instrument": instrument(request.symbol), "price_type": "trade",
            "source_instrument": None,
            "endpoint": None, "retrieved_at_ms": now_ms,
            "threshold_pct": str(request.settings.threshold_pct), "rolling": {}, "technical": {},
            "baseline": baseline_preview(request, None, None, None, now_ms),
            "failure_category": attempts[-1]["reason"] if attempts else "provider_unavailable",
            "attempts": attempts}
