"""Alert setup chart renderer (#299, #298). Machine-side only: needs matplotlib
(python/requirements-machine.txt), which never enters the calculation core (market_analysis) or the
default CI path.

Charts, in the owner-approved look of the #299 reference script (dark palette):

- ``render_pattern_snapshot``: candles + EMA 20/50 + volume + RSI 14, the event candle highlighted;
- ``render_alert_chart``: the same panels for a movement alert, plus baseline and alert price lines,
  the threshold band, the completed 1m alert candle and the market breadth reading at alert time;
- ``render_trade_overlay``: a long/short position against the candles (entry, stop, target,
  liquidation);
- ``render_equity``: wallet equity after each closed trade, drawdown and an indicator-score strip.

Windows are the compact JSON the application captures at event time (alert-chart-window-v1:
candles as [open_ms, open, high, low, close, volume], oldest first, nothing after the event candle).
Output is deterministic for the same input and the same matplotlib/FreeType build: no timestamps in
the PNG metadata, no randomness.

Command line (used by the worker-host child ``alert-charts``)::

    python -m market_analysis_tools.alert_charts check
    python -m market_analysis_tools.alert_charts render < job.json   # {"window": {...}, "path": "..."}
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from math import isfinite, nan
import os
from pathlib import Path
import sys

WINDOW_VERSION = "alert-chart-window-v1"
BG, PANEL, GRID, TXT, MUT = "#0e0d1a", "#15142a", "#2a2845", "#e8e6f5", "#8f8cab"
BULL, BEAR, PRI, WARN, LIQ = "#34d399", "#f87171", "#a78bfa", "#fbbf24", "#ef4444"
DPI = 130
SYNTHETIC_NOTE = "SYNTHETIC candles for illustration, not market data"
PATTERN_LABELS = {
    "doji": "Doji\nbody ≤ 10% of range",
    "hammer": "Hammer",
    "shooting_star": "Shooting star",
    "bullish_engulfing": "Bullish engulfing",
    "bearish_engulfing": "Bearish engulfing",
    "ema_bullish_cross": "EMA 20/50 bullish cross",
    "ema_bearish_cross": "EMA 20/50 bearish cross",
    "rsi_recovered_30": "RSI recovered 30",
    "rsi_rejected_70": "RSI rejected 70",
    "volume_spike": "Volume spike (≥ 2× 20-candle mean)",
}


class WindowError(ValueError):
    """A window the renderer refuses (malformed, or a candle after the event candle)."""


# ---------------------------------------------------------------- validation (no matplotlib)


def _number(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise WindowError(f"{name} must be a finite number")
    return float(value)


def validate_candles(window: dict) -> list:
    """[(open_ms, o, h, l, c, v)] oldest first; refuses gaps in order, bad OHLC and any candle that
    opens after ``event_candle_ms`` (point-in-time)."""
    candles = window.get("candles")
    if not isinstance(candles, list) or not candles:
        raise WindowError("window has no candles")
    event = window.get("event_candle_ms")
    if isinstance(event, bool) or not isinstance(event, int):
        raise WindowError("window lacks event_candle_ms")
    out, previous = [], None
    for index, row in enumerate(candles):
        if not isinstance(row, list) or len(row) != 6:
            raise WindowError(f"candle {index} is not [open_ms, open, high, low, close, volume]")
        open_ms = row[0]
        if isinstance(open_ms, bool) or not isinstance(open_ms, int):
            raise WindowError(f"candle {index} open_ms must be an integer")
        o, h, l, c, v = (_number(x, f"candle {index}") for x in row[1:])
        if h < max(o, c) or l > min(o, c) or v < 0:
            raise WindowError(f"candle {index} has inconsistent OHLCV")
        if previous is not None and open_ms <= previous:
            raise WindowError("candles must be strictly increasing in time")
        if open_ms > event:
            raise WindowError("window contains a candle after the event candle")
        previous = open_ms
        out.append((open_ms, o, h, l, c, v))
    return out


def _series(window: dict, name: str, n: int) -> list:
    values = (window.get("indicators") or {}).get(name)
    if values is None:
        return [nan] * n
    if not isinstance(values, list) or len(values) != n:
        raise WindowError(f"indicator {name} must have one value per candle")
    return [nan if v is None else _number(v, name) for v in values]


def _label_time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _short_time(ms: int, timeframe: int) -> str:
    fmt = "%H:%M" if timeframe < 60 else "%m-%d %H:%M"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(fmt)


def _fmt_price(value: float) -> str:
    return f"{value:,.2f}" if abs(value) >= 100 else f"{value:,.4f}" if abs(value) >= 1 else f"{value:.6g}"


# ---------------------------------------------------------------- drawing helpers


def _pyplot():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.family": "DejaVu Sans", "svg.hashsalt": "alert-charts"})
    return plt


def _style(ax) -> None:
    ax.set_facecolor(PANEL)
    for spine in ax.spines.values():
        spine.set_color(GRID)
    ax.tick_params(colors=MUT, labelsize=8)
    ax.grid(color=GRID, lw=0.5, alpha=0.7)


def _draw_candles(ax, candles, offset: int = 0) -> None:
    from matplotlib.patches import Rectangle

    span = max(h for _, _, h, _, _, _ in candles) - min(l for _, _, _, l, _, _ in candles)
    min_body = span * 0.002 if span > 0 else 0.0
    for index, (_, o, h, l, c, _) in enumerate(candles):
        x = index + offset
        color = BULL if c >= o else BEAR
        ax.plot([x, x], [l, h], color=color, lw=1)
        b0, b1 = sorted((o, c))
        ax.add_patch(Rectangle((x - 0.33, b0), 0.66, max(b1 - b0, min_body), color=color))


def _footnote(ax, window: dict, text: str) -> None:
    note = SYNTHETIC_NOTE if window.get("synthetic") else text
    ax.text(0.995, 0.02, note, transform=ax.transAxes, ha="right", color=MUT, fontsize=8)


def _time_ticks(ax, candles, timeframe: int, extra: int = 0) -> None:
    n = len(candles)
    step = max(1, n // 6)
    ticks = list(range(0, n, step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([_short_time(candles[i][0], timeframe) for i in ticks])
    ax.set_xlim(-1, n + extra)


def _save(fig, path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # No Software/date metadata: the same input gives the same bytes on the same build.
    fig.savefig(path, dpi=DPI, facecolor=BG, bbox_inches="tight", metadata={"Software": None})
    import matplotlib.pyplot as plt

    plt.close(fig)


def _three_panels(plt):
    fig = plt.figure(figsize=(11, 8), facecolor=BG)
    grid = fig.add_gridspec(3, 1, height_ratios=[5, 1.4, 1.6], hspace=0.08)
    axes = [fig.add_subplot(grid[i]) for i in range(3)]
    for ax in axes:
        _style(ax)
    return fig, axes


def _lower_panels(a2, a3, candles, rsi, timeframe: int, extra: int = 0) -> None:
    n = len(candles)
    a2.bar(range(n), [v for *_, v in candles], width=0.66, alpha=0.8,
           color=[BULL if c >= o else BEAR for _, o, _, _, c, _ in candles])
    a2.set_xlim(-1, n + extra)
    a2.set_xticklabels([])
    a2.set_ylabel("Volume", color=MUT, fontsize=8)
    a3.plot(range(n), rsi, color=PRI, lw=1.3)
    a3.axhline(70, color=BEAR, lw=0.8, ls="--")
    a3.axhline(30, color=BULL, lw=0.8, ls="--")
    a3.set_ylim(0, 100)
    a3.set_ylabel("RSI 14", color=MUT, fontsize=8)
    _time_ticks(a3, candles, timeframe, extra)
    a3.set_xlabel("completed candles (oldest → newest, UTC)", color=MUT, fontsize=8)


def _highlight_indices(candles, markers: dict | None, default_ms: int) -> list:
    wanted = (markers or {}).get("highlight_ms") or [default_ms]
    index = {row[0]: i for i, row in enumerate(candles)}
    return [index[ms] for ms in wanted if ms in index]


# ---------------------------------------------------------------- charts


def render_pattern_snapshot(window: dict, markers: dict | None, path) -> None:
    """Candles + EMA 20/50 + volume + RSI 14 with the event candle (or ``markers['highlight_ms']``)
    highlighted and annotated with the pattern names."""
    candles = validate_candles(window)
    if candles[-1][0] != window["event_candle_ms"]:
        raise WindowError("a pattern window must end at the event candle")
    n, timeframe = len(candles), int(window.get("timeframe_minutes", 0))
    ema20, ema50, rsi = (_series(window, k, n) for k in ("ema20", "ema50", "rsi14"))
    patterns = [str(p) for p in window.get("patterns", [])]
    plt = _pyplot()
    fig, (a1, a2, a3) = _three_panels(plt)
    _draw_candles(a1, candles)
    a1.plot(range(n), ema20, color=PRI, lw=1.4, label="EMA 20")
    a1.plot(range(n), ema50, color=WARN, lw=1.4, label="EMA 50")
    for i in _highlight_indices(candles, markers, window["event_candle_ms"]):
        a1.axvspan(i - 0.6, i + 0.6, color=WARN, alpha=0.18)
    event = n - 1
    high = candles[event][2]
    lows = min(l for _, _, _, l, _, _ in candles)
    span = max(h for _, _, h, _, _, _ in candles) - lows or 1.0
    label = (markers or {}).get("label") or "\n".join(PATTERN_LABELS.get(p, p) for p in patterns) or "Event"
    lines = label.count("\n") + 1
    a1.annotate(label, xy=(event, high), xytext=(max(event - 17, 0.3 * n), high + 0.08 * span), color=WARN,
                fontsize=10, va="bottom", arrowprops={"arrowstyle": "->", "color": WARN})
    a1.legend(loc="upper left", facecolor=PANEL, edgecolor=GRID, labelcolor=TXT, fontsize=9)
    a1.set_xlim(-1, n + 1)
    a1.set_ylim(lows - 0.10 * span, max(h for _, _, h, _, _, _ in candles) + (0.16 + 0.07 * lines) * span)
    a1.set_xticklabels([])
    title = (f"{window.get('symbol', '')} {timeframe}m · {', '.join(patterns) or 'pattern'} · "
             f"{_label_time(window['event_candle_ms'])}")
    a1.set_title(title, color=TXT, loc="left", fontsize=13)
    _footnote(a1, window, f"{window.get('source', '')} · {window.get('rule_version', '')} · "
                          "completed candles up to the event candle")
    _lower_panels(a2, a3, candles, rsi, timeframe, 1)
    _save(fig, path)


def _breadth_text(window: dict) -> str:
    breadth = window.get("breadth")
    if not breadth:
        return f"Market breadth: unavailable ({window.get('breadth_unavailable_reason') or 'not recorded'})"
    total = breadth.get("denominator") or 0
    part = lambda key: "n/a" if breadth.get(key) is None else f"{breadth[key]}/{total}"  # noqa: E731
    return (f"Market breadth {breadth.get('window_minutes')}m at alert: rising {part('rising')} · "
            f"falling {part('falling')} · flat {part('flat')} · {breadth.get('direction_state')}")


# ---------------------------------------------------------------- label layout (pure, no matplotlib)

MIN_LABEL_GAP = 0.04  # minimum vertical distance between two label anchors, as a share of the visible span
RIGHT_MARGIN = 0.74   # right edge of the axes (figure fraction): labels live in the reserved margin


def spread_labels(anchors: dict, lo: float, hi: float, min_gap: float) -> dict:
    """{key: y} -> {key: y'}: y' keeps the order of the anchors, lies within [lo, hi] and no two values are
    closer than ``min_gap``. Anchors already far enough apart are unchanged (deterministic)."""
    items = sorted(anchors.items(), key=lambda kv: (kv[1], kv[0]))
    if len(items) > 1 and (len(items) - 1) * min_gap > hi - lo:
        raise WindowError("label gap does not fit in the visible range")
    pos = [min(max(y, lo), hi) for _, y in items]
    for i in range(1, len(pos)):
        pos[i] = max(pos[i], pos[i - 1] + min_gap)
    if pos and pos[-1] > hi:
        pos[-1] = hi
        for i in range(len(pos) - 2, -1, -1):
            pos[i] = min(pos[i], pos[i + 1] - min_gap)
    return {key: y for (key, _), y in zip(items, pos)}


def alert_gap_minutes(window: dict, timeframe: int | None = None) -> int:
    """Minutes between the close of the last completed candle of the window and the alert time."""
    candles = validate_candles(window)
    alert = window.get("alert") or {}
    tf = int(timeframe if timeframe is not None else window.get("timeframe_minutes", 15))
    return max(0, round((int(alert["alert_ms"]) - (candles[-1][0] + tf * 60_000)) / 60_000))


def movement_layout(window: dict) -> dict:
    """Pure layout of the movement chart's price panel: y limits, the label positions (baseline and the merged
    alert/threshold label, separated by at least MIN_LABEL_GAP of the visible span) and the timing note."""
    candles = validate_candles(window)
    alert = window.get("alert")
    if not isinstance(alert, dict):
        raise WindowError("a movement window needs an alert block")
    baseline = _number(alert.get("baseline_price"), "baseline_price")
    price = _number(alert.get("alert_price"), "alert_price")
    threshold = _number(alert.get("threshold_pct"), "threshold_pct")
    change = _number(alert.get("change_pct"), "change_pct")
    up = alert.get("direction") == "up"
    lows = min([l for _, _, _, l, _, _ in candles] + [baseline, price])
    highs = max([h for _, _, h, _, _, _ in candles] + [baseline, price])
    span = highs - lows or 1.0
    lo, hi = lows - 0.12 * span, highs + 0.30 * span
    gap = MIN_LABEL_GAP * (hi - lo)
    ys = spread_labels({"baseline": baseline, "alert": price}, lo + gap / 2, hi - gap / 2, gap)
    band = baseline * (1 + (threshold if up else -threshold) / 100)
    minutes = alert_gap_minutes(window)
    return {"ylim": (lo, hi), "min_gap": gap, "anchors": {"baseline": baseline, "alert": price},
            "label_y": ys, "band": band, "up": up, "price": price, "baseline": baseline,
            "baseline_text": f"Baseline {_fmt_price(baseline)}",
            "alert_text": f"Alert {_fmt_price(price)} ({change:+.2f}%, threshold {threshold:g}%)",
            "gap_minutes": minutes,
            "gap_note": f"alert {minutes} min after the last completed {int(window.get('timeframe_minutes', 15))}m "
                        "candle closed (candles are not adjacent)"}


def trade_layout(window: dict, trade: dict) -> dict:
    """Pure layout of the trade overlay: level labels (entry, stop, target, liquidation) separated by at least
    MIN_LABEL_GAP of the visible span, and the y limits."""
    candles = validate_candles(window)
    side = trade.get("side")
    entry, stop, target = (_number(trade.get(k), k) for k in ("entry", "stop", "target"))
    if side not in ("long", "short"):
        raise WindowError("trade side must be long or short")
    if (side == "long" and not stop < entry < target) or (side == "short" and not target < entry < stop):
        raise WindowError("stop and target are on the wrong side of the entry")
    anchors = {"Entry": entry, "Stop": stop, "Target": target}
    if trade.get("liquidation") is not None:
        anchors["Liquidation"] = _number(trade["liquidation"], "liquidation")
    values = [l for _, _, _, l, _, _ in candles] + [h for _, _, h, _, _, _ in candles] + list(anchors.values())
    span = max(values) - min(values) or 1.0
    lo, hi = min(values) - 0.08 * span, max(values) + 0.12 * span
    gap = MIN_LABEL_GAP * (hi - lo)
    return {"ylim": (lo, hi), "min_gap": gap, "anchors": anchors,
            "label_y": spread_labels(anchors, lo + gap / 2, hi - gap / 2, gap)}


def _side_label(ax, y_true: float, y_label: float, text: str, color: str) -> None:
    """A label in the reserved right margin (axes coordinates in x, data in y) with a short leader line."""
    transform = ax.get_yaxis_transform()
    ax.plot([1.0, 1.012], [y_true, y_label], color=color, lw=0.8, transform=transform, clip_on=False)
    ax.text(1.016, y_label, text, color=color, fontsize=8.5, va="center", ha="left", transform=transform,
            clip_on=False)


def render_alert_chart(window: dict, markers: dict | None, path) -> None:
    """Movement alert: 24 h of completed 15m candles ending before the alert, EMA 20/50, volume and RSI,
    the baseline line, the threshold band (unlabelled line), the alert price line with one merged label, the
    completed 1m alert candle drawn one slot after the window behind a dotted break (it can lie up to 14 minutes
    after the last completed 15m candle), and the market breadth reading under the title."""
    candles = validate_candles(window)
    layout = movement_layout(window)
    alert = window["alert"]
    up, baseline, price = layout["up"], layout["baseline"], layout["price"]
    n, timeframe = len(candles), int(window.get("timeframe_minutes", 15))
    ema20, ema50, rsi = (_series(window, k, n) for k in ("ema20", "ema50", "rsi14"))
    minute = alert.get("alert_minute")
    extra = 3  # slot n is the visible break, slot n + 1 holds the alert minute candle
    plt = _pyplot()
    fig, (a1, a2, a3) = _three_panels(plt)
    _draw_candles(a1, candles)
    a1.plot(range(n), ema20, color=PRI, lw=1.4, label="EMA 20")
    a1.plot(range(n), ema50, color=WARN, lw=1.4, label="EMA 50")
    a1.axvline(n, color=MUT, lw=1.0, ls=":")  # gap marker: the minute candle is not adjacent
    if isinstance(minute, list) and len(minute) == 6:
        if minute[0] < candles[-1][0]:
            raise WindowError("the alert minute must not precede the window's last candle")
        _draw_candles(a1, [tuple(minute)], offset=n + 1)
        a1.axvspan(n + 0.4, n + 1.6, color=WARN, alpha=0.18)
    a1.axhline(baseline, color=MUT, lw=1.1, ls="--")
    a1.axhline(layout["band"], color=WARN, lw=0.8, ls=":")
    a1.axhline(price, color=BULL if up else BEAR, lw=1.2)
    _side_label(a1, baseline, layout["label_y"]["baseline"], layout["baseline_text"], MUT)
    _side_label(a1, price, layout["label_y"]["alert"], layout["alert_text"], BULL if up else BEAR)
    lo, hi = layout["ylim"]
    span = (hi - lo) / 1.42
    baseline_ms = alert.get("baseline_ms")
    top = hi - 0.18 * span
    if isinstance(baseline_ms, int) and candles[0][0] <= baseline_ms:
        x = max(i for i, row in enumerate(candles) if row[0] <= baseline_ms)
        a1.axvline(x, color=MUT, lw=0.8, ls=":")
        a1.text(x, top, f"baseline set\n{_short_time(baseline_ms, timeframe)}", color=MUT, fontsize=8, ha="center")
    elif isinstance(baseline_ms, int):
        a1.text(0, top, f"baseline set {_label_time(baseline_ms)} (before window)", color=MUT, fontsize=8)
    a1.annotate("Alert 1m candle" if minute else "Alert", xy=(n + 1, price),
                xytext=(n - 14, price + (0.2 if up else -0.2) * span), color=WARN, fontsize=10,
                arrowprops={"arrowstyle": "->", "color": WARN})
    a1.legend(loc="upper left", facecolor=PANEL, edgecolor=GRID, labelcolor=TXT, fontsize=9)
    a1.set_xlim(-1, n + extra)
    a1.set_ylim(lo, hi)
    a1.set_xticklabels([])
    threshold = _number(alert.get("threshold_pct"), "threshold_pct")
    a1.set_title(f"{window.get('symbol', '')} movement alert {'up' if up else 'down'} ≥ {threshold:g}% · "
                 f"{_label_time(int(alert.get('alert_ms', 0)))}", color=TXT, loc="left", fontsize=13, pad=36)
    a1.text(0.0, 1.045, _breadth_text(window), transform=a1.transAxes, color=TXT, fontsize=8.5, va="bottom")
    a1.text(0.0, 1.012, layout["gap_note"], transform=a1.transAxes, color=MUT, fontsize=8.5, va="bottom")
    _footnote(a1, window, f"{window.get('source', '')} · {alert.get('rule_version', '')} · {timeframe}m candles "
                          "completed by the alert time")
    _lower_panels(a2, a3, candles, rsi, timeframe, extra)
    fig.subplots_adjust(right=RIGHT_MARGIN)
    _save(fig, path)


def render_trade_overlay(window: dict, trade: dict, path) -> None:
    """A position against the candles: entry, stop, target and (optional) liquidation from the entry candle to
    the right edge, risk and reward zones shaded, labels in the right margin kept apart by MIN_LABEL_GAP."""
    candles = validate_candles(window)
    n = len(candles)
    layout = trade_layout(window, trade)
    entry_ms = trade.get("entry_ms")
    index = {row[0]: i for i, row in enumerate(candles)}
    if entry_ms not in index:
        raise WindowError("trade entry_ms must be a candle of the window")
    start, side = index[entry_ms], trade["side"]
    entry, stop, target = (float(trade[k]) for k in ("entry", "stop", "target"))
    colors = {"Entry": TXT, "Stop": BEAR, "Target": BULL, "Liquidation": LIQ}
    plt = _pyplot()
    fig, ax = plt.subplots(figsize=(11, 5.2), facecolor=BG)
    _style(ax)
    _draw_candles(ax, candles)
    right = n - 1
    ax.axvspan(start - 0.5, right, color=PRI, alpha=0.06)
    for text, y in layout["anchors"].items():
        ax.hlines(y, start - 0.5, right, color=colors[text], lw=1.2, ls="-" if text == "Entry" else "--")
        _side_label(ax, y, layout["label_y"][text], f"{text} {_fmt_price(y)}", colors[text])
    ax.fill_between([start - 0.5, right], entry, stop, color=BEAR, alpha=0.10)
    ax.fill_between([start - 0.5, right], entry, target, color=BULL, alpha=0.10)
    lo, hi = layout["ylim"]
    span = (hi - lo) / 1.2
    ax.annotate(f"{side.capitalize()} opened", xy=(start, entry),
                xytext=(max(start - 10, 0), entry + (0.25 if side == "short" else -0.25) * span), color=TXT,
                fontsize=9, arrowprops={"arrowstyle": "->", "color": TXT})
    ax.set_xlim(-1, right + 2)
    ax.set_ylim(lo, hi)
    _time_ticks(ax, candles, int(window.get("timeframe_minutes", 15)), 2)
    ax.set_title(trade.get("title") or "Trade overlay: where the position sits against the candles",
                 color=TXT, loc="left", fontsize=13)
    _footnote(ax, window, trade.get("note") or f"{window.get('symbol', '')} · levels as recorded at entry")
    fig.subplots_adjust(right=0.84)
    _save(fig, path)


def render_equity(series: dict, path) -> None:
    """Wallet equity after each closed trade (step line, dots at trade closes), drawdown % and an
    optional indicator-score strip in [-100, 100]."""
    equity = [_number(v, "equity") for v in series.get("equity", [])]
    if len(equity) < 2:
        raise WindowError("equity needs at least two points")
    closes = [int(i) for i in series.get("closes", []) if 0 < int(i) < len(equity)]
    score = series.get("score")
    rows = 3 if score else 2
    plt = _pyplot()
    ratios = [3, 1.3, 1.3][:rows]
    fig, axes = plt.subplots(rows, 1, figsize=(11, 7.4 if rows == 3 else 6), facecolor=BG,
                             gridspec_kw={"height_ratios": ratios, "hspace": 0.28})
    for ax in axes:
        _style(ax)
    b1, b2 = axes[0], axes[1]
    x = list(range(len(equity)))
    b1.step(x, equity, where="post", color=PRI, lw=1.8)
    b1.fill_between(x, equity, min(equity) - 1, step="post", color=PRI, alpha=0.12)
    if closes:
        b1.scatter(closes, [equity[i] for i in closes], s=26, zorder=3,
                   c=[BULL if equity[i] >= equity[i - 1] else BEAR for i in closes])
    b1.set_title(series.get("title") or "Wallet equity after each closed trade (dots = trade closes)",
                 color=TXT, loc="left", fontsize=12)
    peak, drawdown = equity[0], []
    for value in equity:
        peak = max(peak, value)
        drawdown.append((value - peak) / peak * 100 if peak else 0.0)
    b2.fill_between(x, drawdown, 0, color=BEAR, alpha=0.35)
    b2.set_ylabel("Drawdown %", color=MUT, fontsize=8)
    if score:
        values = [max(-100.0, min(100.0, _number(v, "score"))) for v in score]
        b3 = axes[2]
        b3.bar(range(len(values)), values, width=0.8, color=[BULL if v >= 0 else BEAR for v in values])
        b3.axhline(0, color=MUT, lw=0.6)
        b3.set_ylim(-100, 100)
        b3.set_ylabel("Indicator score", color=MUT, fontsize=8)
    axes[-1].set_xlabel(series.get("x_label") or "time →", color=MUT, fontsize=8)
    note = "SYNTHETIC data for illustration, not results" if series.get("synthetic") else series.get("note", "")
    b1.text(1.0, 1.02, note, transform=b1.transAxes, ha="right", va="bottom", color=MUT, fontsize=8)
    _save(fig, path)


def render_window(window: dict, path) -> None:
    """Dispatch a captured window by kind."""
    if window.get("version") != WINDOW_VERSION:
        raise WindowError(f"unsupported window version {window.get('version')!r}")
    kind = window.get("kind")
    if kind == "pattern":
        render_pattern_snapshot(window, None, path)
    elif kind == "movement_alert":
        render_alert_chart(window, None, path)
    else:
        raise WindowError(f"unknown window kind {kind!r}")


def file_digest(path) -> dict:
    data = Path(path).read_bytes()
    return {"path": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["check"]:
        import matplotlib

        print(json.dumps({"matplotlib": matplotlib.__version__}))
        return 0
    if argv[:1] != ["render"]:
        print("usage: python -m market_analysis_tools.alert_charts check | render < job.json", file=sys.stderr)
        return 2
    job = json.loads(sys.stdin.read())
    path = Path(job["path"])
    if not path.is_absolute():
        raise WindowError("path must be absolute")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp.png")
    try:
        render_window(job["window"], temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps(file_digest(path)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # the consumer stores this one line as the job error
        print(f"{type(error).__name__}: {error}"[:400], file=sys.stderr)
        sys.exit(1)
