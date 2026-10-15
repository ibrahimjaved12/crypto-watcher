"""candle-table group lv3 (lv3-v1): period-open levels and the significant-level touch (docs/pattern-study.md).

Additive to loc-v1 (unchanged). The open-time rule of candle_location holds: a level is usable for
candle i iff ``valid_from_ms <= open_ms(i) < valid_to_ms`` (``LevelIndex``); distances are in
``atr_prev`` units and a touch is a level inside [low - 0.25 atr_prev, high + 0.25 atr_prev].

- Period opens: ``day_open`` (UTC day), ``week_open`` (Monday 00:00 UTC), ``month_open`` (UTC month)
  = the open price of the period's first candle (the row's own candles; weekly rows read the dk1 days
  for ``month_open``). A level is usable from the SECOND row candle of its period (valid_from =
  period start + one row candle) until the period ends, so the period's first candle never touches
  its own open trivially. No level when that first source candle is invalid. A set whose first
  usable candle would be the next period is not applicable: day_open on 1440/10080 rows, week_open
  on 10080 rows.
- ``touch_sig``: a touch of ANY significant set applicable to the row: prev_week, prev_month,
  week_open, month_open, swing1w, the coarse round grid (multiples of ``SIG_ROUND_MULTIPLE`` x the
  symbol's grid spacing; for BTC that is round_tier >= 5000) and liquidity pools (lv2 equal
  highs/lows) with ``tier`` (member swings) >= ``SIG_POOL_MIN_TOUCHES``.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import data_lake
from ..benchmark import levels as lv
from ..daily_lake import DAY_MS
from .candle_location import LOCATION_SETS, TOUCH_BAND_ATR, LevelIndex

SCALE = 10 ** data_lake.SCALE_DIGITS
MINUTE = data_lake.MINUTE_MS
WEEK_MS = 7 * DAY_MS
OPEN_SETS = ("day_open", "week_open", "month_open")
OPEN_SETS_BY_TF = {15: OPEN_SETS, 60: OPEN_SETS, 240: OPEN_SETS, 1440: ("week_open", "month_open"),
                   10080: ("month_open",)}
SIG_ROUND_MULTIPLE = 5
SIG_POOL_MIN_TOUCHES = 3
# Order = tie order of sig_nearest_set.
SIG_SETS = ("prev_week", "prev_month", "week_open", "month_open", "swing1w", "round_sig", "pool3")


def _month_start(ms: int) -> int:
    day = datetime.fromtimestamp(ms // 1000, tz=timezone.utc)
    return data_lake.month_bounds_ms(f"{day.year:04d}-{day.month:02d}")[0]


def _period_bounds(period: str, first_ms: int, end_ms: int):
    """(start, end) of every period that starts in [first_ms, end_ms)."""
    if period == "day":
        start = -(-first_ms // DAY_MS) * DAY_MS
        while start < end_ms:
            yield start, start + DAY_MS
            start += DAY_MS
    elif period == "week":
        start = lv._week_start(first_ms)
        if start < first_ms:
            start += WEEK_MS
        while start < end_ms:
            yield start, start + WEEK_MS
            start += WEEK_MS
    else:
        start = _month_start(first_ms)
        if start < first_ms:
            start = lv._next_month(first_ms)
        while start < end_ms:
            stop = lv._next_month(start)
            yield start, stop
            start = stop


def period_open_levels(source, period: str, row_minutes: int, set_name: str | None = None) -> list:
    """Open of each period's first ``source`` candle as a Level valid [start + one row candle, period end).

    The first source candle must start exactly at the period start and be valid, else no level."""
    if period not in ("day", "week", "month"):
        raise ValueError("period must be 'day', 'week' or 'month'")
    step = source.minutes * MINUTE
    end_ms = source.start_ms + len(source) * step
    name = set_name or f"{period}_open"
    out = []
    for start, stop in _period_bounds(period, source.start_ms, end_ms):
        offset = start - source.start_ms
        if offset % step:
            continue
        k = offset // step
        if k >= len(source) or not source.valid[k]:
            continue
        valid_from = start + row_minutes * MINUTE
        if valid_from < stop:
            out.append(lv.Level(name, "open", source.open[k], valid_from, stop, 1))
    return out


def prepare(ctx) -> None:
    """Attach the period-open indexes (called by TableContext.__post_init__)."""
    period = {"day_open": "day", "week_open": "week", "month_open": "month"}
    days = getattr(ctx, "history_days", None)
    ctx.open_index = {}
    for name in OPEN_SETS_BY_TF[ctx.minutes]:
        source = days if ctx.minutes == 10080 else ctx.candles
        if source is None:
            continue
        ctx.open_index[name] = LevelIndex(period_open_levels(source, period[name], ctx.minutes, name))


def _sig_round(symbol: str, close: int, lo: int, hi: int) -> list:
    """Coarse-grid prices in [lo, hi] plus the nearest coarse level on each side of close."""
    spacing = lv.ROUND_SPACING[symbol] * SIG_ROUND_MULTIPLE
    first = min(lo // spacing, close // spacing)
    last = max(-(-hi // spacing), -(-close // spacing))
    return [m * spacing for m in range(first, last + 1) if m * spacing > 0]


def _sig_prices(ctx, name: str, known_ms: int, close: int, lo: int, hi: int):
    """Usable prices of one significant set, or None when it is not applicable / its source is unavailable."""
    if name == "round_sig":
        return _sig_round(ctx.symbol, close, lo, hi) if ctx.symbol in lv.ROUND_SPACING else None
    if name == "pool3":
        index = getattr(ctx, "pool_index", None)
        if index is None:
            return None
        return [p.price for side in ("high", "low") for p in index[side].usable(known_ms)
                if p.tier >= SIG_POOL_MIN_TOUCHES]
    if name in OPEN_SETS:
        index = ctx.open_index.get(name)
    elif name in LOCATION_SETS[ctx.minutes]:
        index = ctx.level_index.get(name)
    else:
        return None
    if index is None:
        return None
    return [level.price for level in index.usable(known_ms)]


def levels3(ctx, i: int) -> dict:
    c = ctx.candles
    open_sets = OPEN_SETS_BY_TF[ctx.minutes]
    atr = ctx.ind[i - 1]["atr"] if i else None
    if not atr or atr <= 0:
        out = {}
        for name in open_sets:
            out.update(dict.fromkeys((f"touch_{name}", f"dist_up_{name}_atr", f"dist_dn_{name}_atr",
                                      f"level_side_{name}")))
        out.update(touch_sig=None, sig_nearest_level_dist_atr=None, sig_nearest_set=None)
        return out
    unit = atr * SCALE
    close, high, low, known_ms = c.close[i], c.high[i], c.low[i], c.open_ms(i)
    lo, hi = int(low - TOUCH_BAND_ATR * unit), int(high + TOUCH_BAND_ATR * unit)
    out = {}
    for name in open_sets:
        index = ctx.open_index.get(name)
        if index is None:  # no source: missing
            out.update(dict.fromkeys((f"touch_{name}", f"dist_up_{name}_atr", f"dist_dn_{name}_atr",
                                      f"level_side_{name}")))
            continue
        prices = sorted({level.price for level in index.usable(known_ms)})
        if not prices:  # the period's first candle, or its first candle was invalid: not applicable (empty)
            continue
        above = [price for price in prices if price >= close]
        below = [price for price in prices if price <= close]
        out[f"dist_up_{name}_atr"] = (above[0] - close) / unit if above else None
        out[f"dist_dn_{name}_atr"] = (close - below[-1]) / unit if below else None
        out[f"touch_{name}"] = any(low - TOUCH_BAND_ATR * unit <= price <= high + TOUCH_BAND_ATR * unit
                                   for price in prices)
        nearest = min(prices, key=lambda price: (abs(price - close), price))
        out[f"level_side_{name}"] = -1 if nearest > close else 1
    touched, seen, best = False, 0, None  # best = (distance, set rank, name)
    for rank, name in enumerate(SIG_SETS):
        prices = _sig_prices(ctx, name, known_ms, close, lo, hi)
        if prices is None:
            continue
        seen += 1
        for price in prices:
            if low - TOUCH_BAND_ATR * unit <= price <= high + TOUCH_BAND_ATR * unit:
                touched = True
            candidate = (abs(price - close), rank, name)
            best = candidate if best is None or candidate < best else best
    out["touch_sig"] = touched if seen else None
    out["sig_nearest_level_dist_atr"] = best[0] / unit if best else None
    out["sig_nearest_set"] = best[2] if best else None
    return out


_OPEN_DOCS = {"day_open": "UTC day open", "week_open": "Monday 00:00 UTC week open", "month_open": "UTC month open"}
LEVELS3_COLUMNS = tuple(
    item for name in OPEN_SETS for item in (
        (f"touch_{name}", f"the {_OPEN_DOCS[name]} lies in [low - 0.25 atr_prev, high + 0.25 atr_prev] "
                          "(usable from the period's second candle)"),
        (f"dist_up_{name}_atr", f"({_OPEN_DOCS[name]} - close) / atr_prev when it is >= close"),
        (f"dist_dn_{name}_atr", f"(close - {_OPEN_DOCS[name]}) / atr_prev when it is <= close"),
        (f"level_side_{name}", f"-1 the {_OPEN_DOCS[name]} lies above close (dist_up side), +1 at or below"),
    )) + (
    ("touch_sig", "a significant level (prev_week, prev_month, week_open, month_open, swing1w, 5x round grid, "
                  "pools with >= 3 swings) lies in [low - 0.25 atr_prev, high + 0.25 atr_prev]"),
    ("sig_nearest_level_dist_atr", "|nearest usable significant level - close| / atr_prev"),
    ("sig_nearest_set", "its set: prev_week | prev_month | week_open | month_open | swing1w | round_sig | pool3"),
)
