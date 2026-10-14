"""candle-table-v1 groups location (loc-v1), sweep (sweep-v1) and htf (htf-v1); see docs/pattern-study.md.

Levels come from ``benchmark/levels.py`` (``PATTERN_LEVEL_SETS``). A level is usable for a row iff
``valid_from_ms <= decision_ms < valid_to_ms``; a swing is known only ``n`` candles after its
candle, so a candle is never its own swing. All distances are in ``atr_prev`` units (Wilder
ATR14 through candle i-1). This module reads a ``TableContext`` (duck-typed; no import of
candle_table) and is wired in by ``candle_table``.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

from .. import data_lake
from ..benchmark import levels as lv
from ..benchmark.bars import BarSeries
from ..benchmark.candles import CandleSeries
from .features import CandleFeatureHistory

SCALE = 10 ** data_lake.SCALE_DIGITS
MINUTE = data_lake.MINUTE_MS
TOUCH_BAND_ATR = 0.25
NEAR_BAND_ATR = 0.5
SWEEP_PIERCE_ATR = 0.1
SET_ORDER = lv.PATTERN_LEVEL_SETS  # round, prev_day, prev_week, prev_month, swing_tf, swing4h, swing1d, swing1w
# Sets that apply to a row's timeframe. prev_day on daily/weekly rows and prev_week on weekly rows would be
# the row's own candle (or contain it) at its decision time, and no 4h data exists before 2024 for
# daily/weekly rows; those sets are not applicable (empty, not missing).
LOCATION_SETS = {
    15: SET_ORDER, 60: SET_ORDER, 240: SET_ORDER,
    1440: ("round", "prev_week", "prev_month", "swing_tf", "swing1d", "swing1w"),
    10080: ("round", "prev_month", "swing_tf", "swing1d", "swing1w"),
}
SWEEP_REFS = ("prior20", "swing_tf", "prev_day", "prev_week", "prev_month")
PRIOR_N = 20
HTF_MAP = {15: (60, 240), 60: (240, 1440), 240: (1440, 10080), 1440: (10080,), 10080: ()}
HTF_LABEL = {60: "1h", 240: "4h", 1440: "1d", 10080: "1w"}
_HTF_PARAMS = dict(atr_period=14, adx_period=14, ema_fast_period=20, ema_slow_period=50, lookback=20)


# ---------------------------------------------------------------- sources


class LevelIndex:
    """Levels of one set, answered for non-decreasing decision times with a sweep cursor
    (an earlier time restarts the sweep, so answers depend only on the time asked)."""

    def __init__(self, levels):
        self.levels = sorted(levels, key=lambda level: (level.valid_from_ms, level.price, level.side_hint))
        self._starts = [level.valid_from_ms for level in self.levels]
        self._time, self._next, self._active = None, 0, []

    def usable(self, decision_ms: int) -> list:
        if self._time is None or decision_ms < self._time:
            self._next, self._active = 0, []
        stop = bisect_right(self._starts, decision_ms)
        self._active.extend(self.levels[self._next:stop])
        self._next = max(self._next, stop)
        self._active = [level for level in self._active if level.valid_to_ms > decision_ms]
        self._time = decision_ms
        return self._active


@dataclass(frozen=True)
class HtfState:
    candles: CandleSeries
    ind: list  # inclusive indicators per candle (CandleFeatureHistory.indicators)


def htf_state(candles: CandleSeries) -> HtfState:
    history = CandleFeatureHistory(candles, **_HTF_PARAMS)
    return HtfState(candles, [history.indicators(k) for k in range(len(candles))])


def last_closed_index(candles: CandleSeries, decision_ms: int) -> int | None:
    """Index of the last candle with ``end_ms <= decision_ms`` (never one still forming), else None."""
    step = candles.minutes * MINUTE
    k = (decision_ms - candles.start_ms) // step - 1
    return k if 0 <= k < len(candles) else None


def shared_levels(*, bars: BarSeries | None = None, days: CandleSeries | None = None,
                  weeks: CandleSeries | None = None, candles_240: CandleSeries | None = None) -> dict:
    """Level sets of one symbol that do not depend on the row's timeframe.

    ``bars`` gives intraday prev_day / prev_week / prev_month (keys without suffix);
    ``days`` gives prev_week / prev_month for daily and weekly rows (keys ``*@daily``)."""
    out = {}
    if bars is not None:
        out["prev_day"] = lv.prev_period_levels(bars, "day")
        out["prev_week"] = lv.prev_period_levels(bars, "week")
        out["prev_month"] = lv.prev_month_levels(bars)
    if candles_240 is not None:
        out["swing4h"] = lv.swing_levels(candles_240)
    if days is not None:
        out["swing1d"] = lv.pattern_swing_levels(days, "swing1d")
        out["prev_week@daily"] = lv.prev_period_levels_daily(days, "week")
        out["prev_month@daily"] = lv.prev_period_levels_daily(days, "month")
    if weeks is not None:
        out["swing1w"] = lv.pattern_swing_levels(weeks, "swing1w")
    return out


def context_levels(minutes: int, candles: CandleSeries, shared: dict) -> dict:
    """The applicable non-round sets of a row timeframe (round levels are computed per row)."""
    daily = minutes not in (15, 60, 240)
    out = {"swing_tf": lv.pattern_swing_levels(candles, "swing_tf")}
    for name in LOCATION_SETS[minutes]:
        if name in ("round", "swing_tf"):
            continue
        key = f"{name}@daily" if daily and name in ("prev_week", "prev_month") else name
        if key in shared:
            out[name] = shared[key]
    return out


def htf_sources(minutes: int, intraday_candles: dict, days: CandleSeries | None,
                weeks: CandleSeries | None) -> dict:
    """{H: CandleSeries} for the row timeframe: 60/240 from the 1m bars, 1440/10080 from dk1."""
    out = {}
    for higher in HTF_MAP[minutes]:
        source = intraday_candles.get(higher) if higher in (60, 240) else (days if higher == 1440 else weeks)
        if source is not None:
            out[higher] = source
    return out


def running_extremes(candles: CandleSeries) -> tuple[list, list]:
    """(highest high, lowest low) of the valid candles 0..j inclusive; None before the first valid one."""
    ath, atl, hi, lo = [], [], None, None
    for j in range(len(candles)):
        if candles.valid[j]:
            hi = candles.high[j] if hi is None else max(hi, candles.high[j])
            lo = candles.low[j] if lo is None else min(lo, candles.low[j])
        ath.append(hi)
        atl.append(lo)
    return ath, atl


def prepare(ctx) -> None:
    """Attach the indexes the three groups read (called by TableContext.__post_init__)."""
    ctx.level_index = {name: LevelIndex(levels) for name, levels in ctx.levels.items()}
    ctx.htf_state = {higher: htf_state(candles) for higher, candles in ctx.htf.items()}
    ctx.ath, ctx.atl = running_extremes(ctx.candles)


# ---------------------------------------------------------------- per-row helpers


def _round_prices(symbol: str, close: int, lo: float, hi: float) -> list:
    """Grid prices covering [lo, hi] plus the nearest grid level on each side of close."""
    spacing = lv.ROUND_SPACING[symbol]
    first = min(int(lo) // spacing, close // spacing)
    last = max(-(-int(hi) // spacing), -(-close // spacing))
    return [m * spacing for m in range(first, last + 1) if m * spacing > 0]


def _set_prices(ctx, name: str, decision: int, close: int, lo: float, hi: float):
    """Usable (price, side) pairs of a set, or None when the set's source is unavailable."""
    if name == "round":
        return [(price, "grid") for price in _round_prices(ctx.symbol, close, lo, hi)]
    index = ctx.level_index.get(name)
    if index is None:
        return None
    return [(level.price, level.side_hint) for level in index.usable(decision)]


def _side(previous_close, price: int):
    if previous_close is None:
        return None
    return (previous_close < price) - (previous_close > price)


# ---------------------------------------------------------------- location (loc-v1)


def location(ctx, i: int) -> dict:
    c = ctx.candles
    applicable = LOCATION_SETS[ctx.minutes]
    names = [f"{prefix}_{name}{suffix}" for name in applicable
             for prefix, suffix in (("dist_up", "_atr"), ("dist_dn", "_atr"), ("touch", ""))]
    atr = ctx.ind[i - 1]["atr"] if i else None
    out = {}
    close, high, low = c.close[i], c.high[i], c.low[i]
    ath, previous_ath, atl = ctx.ath[i], ctx.ath[i - 1] if i else None, ctx.atl[i]
    out["new_ath_in_candle"] = None if previous_ath is None else high > previous_ath
    if not atr or atr <= 0:
        out.update(dict.fromkeys(names))
        out.update(any_touch=None, nearest_level_dist_atr=None, nearest_set=None, level_count_near=None,
                   dist_ath_atr=None, dist_atl_atr=None)
        return out
    unit = atr * SCALE  # one ATR in scaled price units
    out["dist_ath_atr"] = (ath - close) / unit
    out["dist_atl_atr"] = (close - atl) / unit
    previous_close = c.close[i - 1] if i and c.valid[i - 1] else None
    lo, hi = low - TOUCH_BAND_ATR * unit, high + TOUCH_BAND_ATR * unit
    near_lo, near_hi = close - NEAR_BAND_ATR * unit, close + NEAR_BAND_ATR * unit
    decision = c.end_ms[i]
    nearest = touched_best = None  # (distance, set rank, price, name)
    near_prices, any_touch, sets_seen = set(), False, 0
    for rank, name in enumerate(SET_ORDER):
        if name not in applicable:
            continue
        pairs = _set_prices(ctx, name, decision, close, min(lo, near_lo), max(hi, near_hi))
        if pairs is None:
            out.update({f"dist_up_{name}_atr": None, f"dist_dn_{name}_atr": None, f"touch_{name}": None})
            continue
        sets_seen += 1
        prices = sorted({price for price, _ in pairs})
        above = [price for price in prices if price >= close]
        below = [price for price in prices if price <= close]
        out[f"dist_up_{name}_atr"] = (above[0] - close) / unit if above else None
        out[f"dist_dn_{name}_atr"] = (close - below[-1]) / unit if below else None
        touched = [price for price in prices if lo <= price <= hi]
        out[f"touch_{name}"] = bool(touched)
        near_prices.update(price for price in prices if near_lo <= price <= near_hi)
        for price in (above[:1] + below[-1:]):
            candidate = (abs(price - close), rank, price, name)
            nearest = candidate if nearest is None or candidate < nearest else nearest
        if touched:
            any_touch = True
            best = min(touched, key=lambda price: (abs(price - close), price))
            out[f"approach_side_{name}"] = _side(previous_close, best)
            candidate = (abs(best - close), rank, best, name)
            touched_best = candidate if touched_best is None or candidate < touched_best else touched_best
    out["any_touch"] = any_touch if sets_seen else None
    out["nearest_level_dist_atr"] = nearest[0] / unit if nearest else None
    out["nearest_set"] = nearest[3] if nearest else None
    out["level_count_near"] = len(near_prices) if sets_seen else None
    if touched_best is not None:
        out["touch_approach_side"] = _side(previous_close, touched_best[2])
    return out


# ---------------------------------------------------------------- sweep (sweep-v1)


def _sweep_applies(minutes: int, ref: str) -> bool:
    return ref in ("prior20", "swing_tf") or ref in LOCATION_SETS[minutes]


def sweep(ctx, i: int) -> dict:
    c = ctx.candles
    refs = [ref for ref in SWEEP_REFS if _sweep_applies(ctx.minutes, ref)]
    atr = ctx.ind[i - 1]["atr"] if i else None
    if not atr or atr <= 0:
        out = {}
        for ref in refs:
            out[f"sweep_up_{ref}"] = out[f"sweep_dn_{ref}"] = None
        out.update(sweep_any_up=None, sweep_any_dn=None, sweep_both=None, sweep_dir=None)
        return out
    unit = atr * SCALE
    high, low, close, decision = c.high[i], c.low[i], c.close[i], c.end_ms[i]
    out, depths, seen = {}, [], 0
    for ref in refs:
        if ref == "prior20":
            if i < PRIOR_N or not all(c.valid[i - PRIOR_N:i]):
                out["sweep_up_prior20"] = out["sweep_dn_prior20"] = None
                continue
            highs, lows = [max(c.high[i - PRIOR_N:i])], [min(c.low[i - PRIOR_N:i])]
        else:
            index = ctx.level_index.get(ref)
            if index is None:
                out[f"sweep_up_{ref}"] = out[f"sweep_dn_{ref}"] = None
                continue
            usable = index.usable(decision)
            highs = [level.price for level in usable if level.side_hint == "high"]
            lows = [level.price for level in usable if level.side_hint == "low"]
        seen += 1
        up = [high - level for level in highs if high > level + SWEEP_PIERCE_ATR * unit and close < level]
        dn = [level - low for level in lows if low < level - SWEEP_PIERCE_ATR * unit and close > level]
        out[f"sweep_up_{ref}"], out[f"sweep_dn_{ref}"] = bool(up), bool(dn)
        depths.extend(up + dn)
    if not seen:
        out.update(sweep_any_up=None, sweep_any_dn=None, sweep_both=None, sweep_dir=None)
        return out
    any_up = any(out.get(f"sweep_up_{ref}") for ref in refs)
    any_dn = any(out.get(f"sweep_dn_{ref}") for ref in refs)
    out.update(sweep_any_up=any_up, sweep_any_dn=any_dn, sweep_both=any_up and any_dn,
               sweep_dir=0 if any_up == any_dn else (-1 if any_up else 1))
    if depths:  # not applicable (empty, not missing) when nothing was swept
        out["sweep_depth_atr"] = max(depths) / unit
    return out


# ---------------------------------------------------------------- htf (htf-v1)


def htf(ctx, i: int) -> dict:
    out = {}
    decision = ctx.candles.end_ms[i]
    for higher in HTF_MAP[ctx.minutes]:
        label = HTF_LABEL[higher]
        names = (f"trend_dir_{label}", f"adx_{label}", f"extension_{label}_atr", f"ema_gap_{label}_atr")
        state = ctx.htf_state.get(higher)
        k = last_closed_index(state.candles, decision) if state is not None else None
        if k is None or not state.candles.valid[k]:
            out.update(dict.fromkeys(names))
            continue
        ind = state.ind[k]
        fast, slow, atr = ind["ema_fast"], ind["ema_slow"], ind["atr"]
        both = fast is not None and slow is not None
        usable_atr = atr is not None and atr > 0
        out[names[0]] = ((fast > slow) - (fast < slow)) if both else None
        out[names[1]] = ind["adx"]
        out[names[2]] = ((state.candles.close[k] / SCALE - fast) / atr) if fast is not None and usable_atr else None
        out[names[3]] = ((fast - slow) / atr) if both and usable_atr else None
    return out


# ---------------------------------------------------------------- columns

_SET_DOCS = {"round": "round-number grid", "prev_day": "previous UTC day high/low",
             "prev_week": "previous Monday week high/low", "prev_month": "previous UTC month high/low",
             "swing_tf": "fractal swings of the row timeframe (n=5)", "swing4h": "4h fractal swings (n=5, 90 d)",
             "swing1d": "daily fractal swings (n=5, 180 d)", "swing1w": "weekly fractal swings (n=3, 365 d)"}
LOCATION_COLUMNS = tuple(
    item for name in SET_ORDER for item in (
        (f"dist_up_{name}_atr", f"(nearest {_SET_DOCS[name]} level >= close - close) / atr_prev"),
        (f"dist_dn_{name}_atr", f"(close - nearest {_SET_DOCS[name]} level <= close) / atr_prev"),
        (f"touch_{name}", f"a {name} level lies in [low - 0.25 atr_prev, high + 0.25 atr_prev]"),
        (f"approach_side_{name}", f"nearest touched {name} level: +1 close_(i-1) below (resistance), -1 above"),
    )) + (
    ("any_touch", "any applicable set touched"),
    ("nearest_level_dist_atr", "distance of the nearest usable level of any set to close / atr_prev"),
    ("nearest_set", "set of that level (ties: round, prev_day, prev_week, prev_month, swing_tf, swing4h, ...)"),
    ("level_count_near", "distinct usable levels within 0.5 atr_prev of close"),
    ("touch_approach_side", "approach side of the nearest touched level of any set"),
    ("dist_ath_atr", "(highest high since the series start, through i - close) / atr_prev"),
    ("dist_atl_atr", "(close - lowest low since the series start, through i) / atr_prev"),
    ("new_ath_in_candle", "high_i above every earlier high of the series"),
)
SWEEP_COLUMNS = tuple(
    item for ref in SWEEP_REFS for item in (
        (f"sweep_up_{ref}", f"high > a {ref} high + 0.1 atr_prev and close < that high"),
        (f"sweep_dn_{ref}", f"low < a {ref} low - 0.1 atr_prev and close > that low"),
    )) + (
    ("sweep_any_up", "any reference high swept"), ("sweep_any_dn", "any reference low swept"),
    ("sweep_both", "highs and lows swept in the same candle (outside bar)"),
    ("sweep_dir", "-1 swept up only, +1 swept down only, 0 both or none"),
    ("sweep_depth_atr", "largest piercing depth / atr_prev (empty when nothing was swept)"),
)
HTF_COLUMNS = tuple(
    item for higher in (60, 240, 1440, 10080) for label in (HTF_LABEL[higher],) for item in (
        (f"trend_dir_{label}", f"sign(EMA20 - EMA50) of the last closed {label} candle, through its close"),
        (f"adx_{label}", f"ADX14 of the last closed {label} candle"),
        (f"extension_{label}_atr", f"(close - EMA20) / ATR14 of the last closed {label} candle"),
        (f"ema_gap_{label}_atr", f"(EMA20 - EMA50) / ATR14 of the last closed {label} candle"),
    ))
