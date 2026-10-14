"""candle-table-v1 groups location (loc-v1), sweep (sweep-v1) and htf (htf-v1); see docs/pattern-study.md.

Levels come from ``benchmark/levels.py`` (``PATTERN_LEVEL_SETS``). A level is usable for candle i iff
it was valid when the candle opened: ``valid_from_ms <= open_ms(i) < valid_to_ms``, so a period
level never contains the candle's own high/low. Higher-timeframe state likewise comes from the
last higher candle closed at or before the row candle's open. A swing is known only ``n`` candles after its
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
from ..daily_lake import DAY_MS
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
    """Levels of one set usable at ``known_ms`` (the row candle's open: a level is usable for candle i
    iff it was valid when the candle opened), answered for non-decreasing times with a sweep cursor
    (an earlier time restarts the sweep, so answers depend only on the time asked)."""

    def __init__(self, levels):
        self.levels = sorted(levels, key=lambda level: (level.valid_from_ms, level.price, level.side_hint))
        self._starts = [level.valid_from_ms for level in self.levels]
        self._time, self._next, self._active = None, 0, []

    def usable(self, known_ms: int) -> list:
        if self._time is None or known_ms < self._time:
            self._next, self._active = 0, []
        stop = bisect_right(self._starts, known_ms)
        self._active.extend(self.levels[self._next:stop])
        self._next = max(self._next, stop)
        self._active = [level for level in self._active if level.valid_to_ms > known_ms]
        self._time = known_ms
        return self._active


@dataclass(frozen=True)
class HtfState:
    candles: CandleSeries
    ind: list  # inclusive indicators per candle (CandleFeatureHistory.indicators)


def htf_state(candles: CandleSeries) -> HtfState:
    history = CandleFeatureHistory(candles, **_HTF_PARAMS)
    return HtfState(candles, [history.indicators(k) for k in range(len(candles))])


def last_closed_index(candles: CandleSeries, at_ms: int) -> int | None:
    """Index of the last candle with ``end_ms <= at_ms`` (never one still forming), else None."""
    step = candles.minutes * MINUTE
    k = (at_ms - candles.start_ms) // step - 1
    return k if 0 <= k < len(candles) else None


def shared_levels(*, bars: BarSeries | None = None, days: CandleSeries | None = None,
                  weeks: CandleSeries | None = None, candles_240: CandleSeries | None = None) -> dict:
    """Level sets of one symbol that do not depend on the row's timeframe.

    prev_day comes from the 1m ``bars``. prev_week / prev_month come from the dk1 ``days`` when given
    (complete, fully valid Monday weeks / calendar months; the same lists serve intraday, daily and
    weekly rows), else from ``bars``."""
    out = {}
    if bars is not None:
        out["prev_day"] = lv.prev_period_levels(bars, "day")
        if days is None:
            out["prev_week"] = lv.prev_period_levels(bars, "week")
            out["prev_month"] = lv.prev_month_levels(bars)
    if candles_240 is not None:
        out["swing4h"] = lv.swing_levels(candles_240)
    if days is not None:
        out["swing1d"] = lv.pattern_swing_levels(days, "swing1d")
        out["prev_week"] = lv.prev_period_levels_daily(days, "week")
        out["prev_month"] = lv.prev_period_levels_daily(days, "month")
    if weeks is not None:
        out["swing1w"] = lv.pattern_swing_levels(weeks, "swing1w")
    return out


def context_levels(minutes: int, candles: CandleSeries, shared: dict) -> dict:
    """The applicable non-round sets of a row timeframe (round levels are computed per row)."""
    out = {"swing_tf": lv.pattern_swing_levels(candles, "swing_tf")}
    for name in LOCATION_SETS[minutes]:
        if name not in ("round", "swing_tf") and name in shared:
            out[name] = shared[name]
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


def _max(a, b):
    return b if a is None else a if b is None else max(a, b)


def _min(a, b):
    return b if a is None else a if b is None else min(a, b)


def _day_prefix(days: CandleSeries) -> tuple[list, list]:
    """hi[n], lo[n]: extremes of the valid dk1 days 0..n-1 (None while there is none)."""
    hi, lo = [None], [None]
    for d in range(len(days)):
        ok = days.valid[d]
        hi.append(_max(hi[-1], days.high[d] if ok else None))
        lo.append(_min(lo[-1], days.low[d] if ok else None))
    return hi, lo


def _days_before(days: CandleSeries, t: int, *, ending: bool) -> int:
    """Number of leading dk1 days that end at or before t (ending) or start before t (not ending)."""
    offset = t - days.start_ms
    n = offset // DAY_MS if ending else -(-offset // DAY_MS)
    return min(max(n, 0), len(days))


def running_extremes(candles: CandleSeries, history_days: CandleSeries | None = None) -> tuple:
    """(ath, atl, ath_before, atl_before): extremes through candle i and strictly before it.

    Without ``history_days`` the series alone is used. With the dk1 daily candles (since 2020-01-01):
    intraday rows combine the days ending at or before the UTC day start of the candle's open with
    the series' valid candles of that day through i; daily/weekly rows use the days that start
    before end_ms(i) (``*_before``: before open_ms(i)). Invalid days/candles are skipped."""
    n = len(candles)
    ath, atl, ath_b, atl_b = [None] * n, [None] * n, [None] * n, [None] * n
    if history_days is None:
        hi = lo = None
        for j in range(n):
            ath_b[j], atl_b[j] = hi, lo
            if candles.valid[j]:
                hi, lo = _max(hi, candles.high[j]), _min(lo, candles.low[j])
            ath[j], atl[j] = hi, lo
        return ath, atl, ath_b, atl_b
    pre_hi, pre_lo = _day_prefix(history_days)
    if candles.minutes in (1440, 10080):
        for j in range(n):
            k = _days_before(history_days, candles.end_ms[j], ending=False)
            kb = _days_before(history_days, candles.open_ms(j), ending=False)
            ath[j], atl[j], ath_b[j], atl_b[j] = pre_hi[k], pre_lo[k], pre_hi[kb], pre_lo[kb]
        return ath, atl, ath_b, atl_b
    day, day_hi, day_lo = None, None, None
    for j in range(n):
        start = candles.open_ms(j) // DAY_MS * DAY_MS
        if start != day:
            day, day_hi, day_lo = start, None, None
        k = _days_before(history_days, start, ending=True)
        ath_b[j], atl_b[j] = _max(pre_hi[k], day_hi), _min(pre_lo[k], day_lo)
        if candles.valid[j]:
            day_hi, day_lo = _max(day_hi, candles.high[j]), _min(day_lo, candles.low[j])
        ath[j], atl[j] = _max(pre_hi[k], day_hi), _min(pre_lo[k], day_lo)
    return ath, atl, ath_b, atl_b


def prepare(ctx) -> None:
    """Attach the indexes the three groups read (called by TableContext.__post_init__)."""
    ctx.level_index = {name: LevelIndex(levels) for name, levels in ctx.levels.items()}
    ctx.htf_state = {higher: htf_state(candles) for higher, candles in ctx.htf.items()}
    ctx.ath, ctx.atl, ctx.ath_before, ctx.atl_before = running_extremes(
        ctx.candles, getattr(ctx, "history_days", None))


# ---------------------------------------------------------------- per-row helpers


def _round_prices(symbol: str, close: int, lo: float, hi: float) -> list:
    """Grid prices covering [lo, hi] plus the nearest grid level on each side of close."""
    spacing = lv.ROUND_SPACING[symbol]
    first = min(int(lo) // spacing, close // spacing)
    last = max(-(-int(hi) // spacing), -(-close // spacing))
    return [m * spacing for m in range(first, last + 1) if m * spacing > 0]


def _set_prices(ctx, name: str, known_ms: int, close: int, lo: float, hi: float):
    """Usable (price, side) pairs of a set, or None when the set's source is unavailable."""
    if name == "round":
        return [(price, "grid") for price in _round_prices(ctx.symbol, close, lo, hi)]
    index = ctx.level_index.get(name)
    if index is None:
        return None
    return [(level.price, level.side_hint) for level in index.usable(known_ms)]


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
    ath, previous_ath, atl = ctx.ath[i], ctx.ath_before[i], ctx.atl[i]
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
    known_ms = c.open_ms(i)  # levels valid when the candle opened
    nearest = touched_best = None  # (distance, set rank, price, name)
    near_prices, any_touch, sets_seen = set(), False, 0
    for rank, name in enumerate(SET_ORDER):
        if name not in applicable:
            continue
        pairs = _set_prices(ctx, name, known_ms, close, min(lo, near_lo), max(hi, near_hi))
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
    high, low, close, known_ms = c.high[i], c.low[i], c.close[i], c.open_ms(i)
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
            usable = index.usable(known_ms)
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
    known_ms = ctx.candles.open_ms(i)  # never the row's own candle
    for higher in HTF_MAP[ctx.minutes]:
        label = HTF_LABEL[higher]
        names = (f"trend_dir_{label}", f"adx_{label}", f"extension_{label}_atr", f"ema_gap_{label}_atr")
        state = ctx.htf_state.get(higher)
        k = last_closed_index(state.candles, known_ms) if state is not None else None
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
    ("dist_ath_atr", "(highest high since 2020-01-01 (dk1 start, not necessarily all-time), through i - close) "
                     "/ atr_prev"),
    ("dist_atl_atr", "(close - lowest low since 2020-01-01 (dk1 start), through i) / atr_prev"),
    ("new_ath_in_candle", "high_i above the highest high since 2020-01-01 before candle i"),
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
_AT_OPEN = " at or before the row candle's open"
HTF_COLUMNS = tuple(
    item for higher in (60, 240, 1440, 10080) for label in (HTF_LABEL[higher],) for item in (
        (f"trend_dir_{label}", f"sign(EMA20 - EMA50) of the last closed {label} candle{_AT_OPEN}, through its close"),
        (f"adx_{label}", f"ADX14 of the last closed {label} candle{_AT_OPEN}"),
        (f"extension_{label}_atr", f"(close - EMA20) / ATR14 of the last closed {label} candle{_AT_OPEN}"),
        (f"ema_gap_{label}_atr", f"(EMA20 - EMA50) / ATR14 of the last closed {label} candle{_AT_OPEN}"),
    ))
