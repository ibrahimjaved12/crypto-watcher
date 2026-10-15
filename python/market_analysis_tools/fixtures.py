"""SYNTHETIC chart fixtures (a seeded random walk), for tests and for the CI artifact
``alert-chart-fixtures``. Not market data; every window carries ``synthetic: true`` so the chart says so.

    python -m market_analysis_tools.fixtures OUT_DIR   # needs matplotlib
"""
from __future__ import annotations

from pathlib import Path
import random
import sys

from market_analysis_tools import alert_charts as ac

START = 1_767_225_600_000  # 2026-01-01 00:00 UTC


def synthetic_candles(n=60, timeframe=15, seed=7, doji_at=None, sigma=0.7):
    """SYNTHETIC seeded random-walk candles [open_ms, o, h, l, c, v]; not market data."""
    rng = random.Random(seed)
    price, rows = 100.0, []
    for i in range(n):
        o = price
        c = o + rng.gauss(0.02 * sigma / 0.7, sigma)
        if i == doji_at:
            c = o + 0.02
        wick = sigma / 0.7
        h = max(o, c) + abs(rng.gauss(0.18, 0.08)) * wick + 0.01 * wick + (0.5 if i == doji_at else 0)
        l = min(o, c) - abs(rng.gauss(0.18, 0.08)) * wick - 0.01 * wick - (0.5 if i == doji_at else 0)
        rows.append([START + i * timeframe * 60_000, round(o, 4), round(h, 4), round(l, 4), round(c, 4),
                     round(abs(rng.gauss(100, 35)), 2)])
        price = c
    return rows


def ema(values, period):
    out = [None] * len(values)
    if len(values) >= period:
        prev = sum(values[:period]) / period
        out[period - 1] = prev
        for i in range(period, len(values)):
            prev = (values[i] - prev) * 2 / (period + 1) + prev
            out[i] = prev
    return out


def rsi_flat(n):
    return [None] * 14 + [50.0] * (n - 14)


def pattern_window(n=60):
    candles = synthetic_candles(n, 60, doji_at=n - 1)
    closes = [c[4] for c in candles]
    return {"version": ac.WINDOW_VERSION, "kind": "pattern", "synthetic": True, "symbol": "BTCUSDT",
            "source": "binance-usdm", "timeframe_minutes": 60, "event_candle_ms": candles[-1][0],
            "patterns": ["doji"], "rule_version": "ta-v2", "candles": candles,
            "indicators": {"ema20": ema(closes, 20), "ema50": ema(closes, 50), "rsi14": rsi_flat(n)}}


def movement_window(n=96, close=False, minutes_after=7):
    """A movement alert. ``close=True``: calm candles and an alert that fired as soon as the change reached the
    threshold, so baseline and alert price are almost equal (the label-collision case)."""
    sigma = 0.08 if close else 0.7
    candles = synthetic_candles(n, 15, sigma=sigma)
    last = candles[-1]
    alert_ms = last[0] + 15 * 60_000 + minutes_after * 60_000
    baseline = candles[40][4]
    threshold, change = (1.5, 1.51) if close else (2.0, 2.1)
    price = round(baseline * (1 + change / 100), 4)
    closes = [c[4] for c in candles]
    return {"version": ac.WINDOW_VERSION, "kind": "movement_alert", "synthetic": True, "symbol": "ETHUSDT",
            "source": "binance-usdm", "timeframe_minutes": 15, "event_candle_ms": last[0], "candles": candles,
            "indicators": {"ema20": ema(closes, 20), "ema50": ema(closes, 50), "rsi14": rsi_flat(n)},
            "alert": {"alert_id": "00000000-0000-4000-8000-000000000001", "rule_version": "cumulative-baseline-v1",
                      "direction": "up", "threshold_pct": threshold, "change_pct": change, "baseline_price": baseline,
                      "baseline_ms": candles[40][0], "alert_price": price, "alert_ms": alert_ms,
                      "alert_candle_ms": alert_ms - 60_000,
                      "alert_minute": [alert_ms - 60_000, last[4], max(last[4], price) + 0.05,
                                       min(last[4], price) - 0.05, price, 12.0]},
            "breadth": {"status": "LIVE", "window_minutes": 5, "evaluation_boundary_ms": alert_ms,
                        "direction_state": "RISING", "denominator": 6, "rising": 5, "falling": 0, "flat": 1,
                        "material_rising": 4, "material_falling": 0},
            "breadth_unavailable_reason": None}


def trade_fixture(window, close_levels=False):
    entry = window["candles"][22]
    price = entry[4]
    stop, target, liq = (price + 0.12, price - 0.2, price + 0.3) if close_levels else (price + 1.4, price - 2.8, price + 3.6)
    return {"side": "short", "entry_ms": entry[0], "entry": price, "stop": stop, "target": target,
            "liquidation": liq}


def render_all(out_dir) -> list:
    """The three fixture charts the CI artifact shows: movement alert with a close baseline/alert, pattern
    snapshot, trade overlay (with close levels)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    trade_window = pattern_window(50)
    ac.render_alert_chart(movement_window(close=True), None, out / "movement-alert-close-baseline.png")
    ac.render_pattern_snapshot(pattern_window(), None, out / "pattern-snapshot.png")
    ac.render_trade_overlay(trade_window, trade_fixture(trade_window, close_levels=True), out / "trade-overlay.png")
    return sorted(p.name for p in out.glob("*.png"))


if __name__ == "__main__":
    print("\n".join(render_all(sys.argv[1] if len(sys.argv) > 1 else "alert-chart-fixtures")))
