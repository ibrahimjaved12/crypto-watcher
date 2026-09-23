"""Read-only analysis using existing calculations and cumulative state transitions."""
import asyncio
import time

import httpx

from .core import MINUTE, analyze, percentage_change, positive
from .cumulative import Baseline, observe
from .providers import (PROVIDERS, exchange_info_url, instrument,
                        parse, provider_endpoint, request_url, validate_exchange_info)


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


async def analyze_request(request, client, loader=load_series, clock=lambda: time.time_ns() // 1_000_000):
    attempts = []
    for provider in PROVIDERS:
        try:
            series = await loader(client, provider, request.symbol)
            now_ms = clock()
            last = latest_close(series[1], now_ms)
            rolling = analyze(series, now_ms, request.settings.threshold_pct)
            baseline = baseline_preview(request, last.close, last.open_ms + MINUTE, provider, now_ms)
            return {"schema_version": 1, "mode": "read_only", "symbol": request.symbol,
                    "status": "ok" if all(w["status"] == "ok" for w in rolling.values()) else "partial",
                    "source": provider, "as_of_ms": now_ms, "price": str(last.close),
                    "observed_at_ms": last.open_ms + MINUTE,
                    "instrument": instrument(request.symbol), "price_type": "trade",
                    "endpoint": provider_endpoint(provider), "retrieved_at_ms": now_ms,
                    "threshold_pct": str(request.settings.threshold_pct),
                    "rolling": rolling, "baseline": baseline, "attempts": attempts}
        except httpx.TimeoutException:
            attempts.append({"source": provider, "reason": "provider_timeout"})
        except httpx.HTTPError:
            attempts.append({"source": provider, "reason": "provider_unavailable"})
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, ArithmeticError):
            attempts.append({"source": provider, "reason": "invalid_or_stale_data"})
    now_ms = clock()
    return {"schema_version": 1, "mode": "read_only", "symbol": request.symbol,
            "status": "unavailable", "source": None, "as_of_ms": now_ms,
            "price": None, "observed_at_ms": None,
            "instrument": instrument(request.symbol), "price_type": "trade",
            "endpoint": "/fapi/v1/klines", "retrieved_at_ms": now_ms,
            "threshold_pct": str(request.settings.threshold_pct), "rolling": {},
            "baseline": baseline_preview(request, None, None, None, now_ms), "attempts": attempts}
