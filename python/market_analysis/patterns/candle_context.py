"""candle-table groups prev (prev-v1), ctx2 (ctx2-v1) and sigma_seas (ss-v1); see docs/pattern-study.md.

None of these groups changes a row's ``status``. Like candle_location, this module reads a
``TableContext`` duck-typed and is wired in by candle_table.

- prev: candle i-1 described with the geometry rules (exact integers), the confirmation of candle i
  against i-1's extremes, and i-1's flush direction. Known at the end of candle i.
- ctx2: trend and exhaustion context through candle i-1 (the open-time rule for daily trend):
  price-vs-EMA trend signs, their agreement with ``trend_dir``, the swing structure of the last two
  confirmed same-tf fractal swings (n = 5, the lv2 confirmation lag), the dk1 daily trend before the
  open, a prior-20 reclaim on candle i-1, and the climax (volume and return) of candles i-6..i-1.
- sigma_seas (intraday only): a time-of-day factor for the NEXT candle's UTC slot from the trailing
  60 days of realised |ret_1_sigma| known at the end of candle i, so outcome units can be
  deseasonalised. The ewma-3d-5m-v1 sigma itself is unchanged.
"""
from __future__ import annotations

from collections import deque
from math import log

from .. import data_lake
from ..daily_lake import DAY_MS
from . import candle_derivatives as dv
from .candle_levels2 import AVWAP_N, _swings
from .candle_location import htf_state, last_closed_index

SCALE = 10 ** data_lake.SCALE_DIGITS
MINUTE = data_lake.MINUTE_MS
DOJI_B10 = 10  # body * 10 <= range, as geometry doji_b10
PRIOR_N = 20
CLIMAX_N = 6
SWING_TREND_N = AVWAP_N  # same-tf fractal swings, n = 5, as the lv2 AVWAP anchors
SEAS_DAYS = 60
SEAS_MIN_OBS = 30
INTRADAY = (15, 60, 240)


def _sign(value) -> int:
    return (value > 0) - (value < 0)


def doji_subtype(o: int, h: int, l: int, c: int) -> str | None:
    """geometry's doji_subtype rule (exact integers); None unless body_ratio <= 0.10."""
    span, body = h - l, abs(c - o)
    if span <= 0 or DOJI_B10 * body > span:
        return None
    upper, lower = h - max(o, c), min(o, c) - l
    if 10 * lower >= 6 * span and 10 * upper <= span:
        return "dragonfly"
    if 10 * upper >= 6 * span and 10 * lower <= span:
        return "gravestone"
    if 10 * upper >= 3 * span and 10 * lower >= 3 * span:
        return "long_legged"
    return "standard"


# ---------------------------------------------------------------- preparation


def _last_two_swings(candles, n: int) -> tuple[list, list]:
    """Per row i: (previous, last) swing high prices and swing low prices confirmed by the end of i-1
    (confirm index j + n <= i - 1, the candle_levels2 lag); None while fewer than two exist."""
    highs, lows = [None] * len(candles), [None] * len(candles)
    confirmations = _swings(candles, n)
    hi, lo = [], []
    for i in range(len(candles)):
        highs[i] = tuple(hi[-2:]) if len(hi) >= 2 else None
        lows[i] = tuple(lo[-2:]) if len(lo) >= 2 else None
        for side, price in confirmations.get(i, ()):
            (hi if side == "high" else lo).append(price)
    return highs, lows


def seasonal_factors(candles, sigma: list, days: int = SEAS_DAYS, minimum: int = SEAS_MIN_OBS) -> list:
    """Per row i: the slot factor of candle i+1 (None if unavailable).

    Observations are |ret_1_sigma| of rows k <= i-1 (outcome candle k+1 valid, sigma_k present), keyed
    by the UTC time-of-day slot of candle k+1, kept while candle k+1 ended in (end_i - days, end_i].
    factor = mean of the slot of candle i+1 (>= ``minimum`` observations) / mean of all observations
    in the window."""
    step = candles.minutes * MINUTE
    slots = DAY_MS // step
    span = days * DAY_MS
    sums, counts = [0.0] * slots, [0] * slots
    every, total = deque(), 0.0
    out = [None] * len(candles)
    for i in range(len(candles)):
        end = candles.end_ms[i]
        k = i - 1
        if k >= 0 and candles.valid[k] and candles.valid[i] and sigma[k]:
            value = abs(log(candles.close[i] / candles.open[i])) / sigma[k]
            slot = (candles.open_ms(i) % DAY_MS) // step
            sums[slot] += value
            counts[slot] += 1
            every.append((end, slot, value))
            total += value
        lower = end - span
        while every and every[0][0] <= lower:
            _, slot, value = every.popleft()
            sums[slot] -= value
            counts[slot] -= 1
            total -= value
        slot = ((candles.open_ms(i) + step) % DAY_MS) // step
        if counts[slot] >= minimum and total > 0:
            out[i] = (sums[slot] / counts[slot]) / (total / len(every))
    return out


def prepare(ctx, rel_volume: list) -> None:
    """Attach swing pairs, the dk1 daily state, the climax volume series and the seasonal factors
    (called by TableContext.__post_init__). ``rel_volume[j]``: rel_volume_slot (intraday) or
    rel_volume_prior20 (daily/weekly) of candle j."""
    c = ctx.candles
    ctx.swing_pairs = _last_two_swings(c, SWING_TREND_N)
    days = getattr(ctx, "history_days", None)
    ctx.daily_state = htf_state(days) if days is not None else None
    ctx.climax_volume = rel_volume
    ctx.seas_factor = seasonal_factors(c, ctx.sigma) if ctx.minutes in INTRADAY else None


# ---------------------------------------------------------------- prev (prev-v1)


def prev(ctx, i: int) -> dict:
    c = ctx.candles
    names = ("prev_body_ratio", "prev_range_atr", "prev_doji_b10", "prev_high", "prev_low", "confirm_dir",
             "flush_prev_dir")
    if not i or not c.valid[i - 1]:
        return dict.fromkeys(names)
    o, h, l, cl = c.open[i - 1], c.high[i - 1], c.low[i - 1], c.close[i - 1]
    span = h - l
    atr_before = ctx.ind[i - 2]["atr"] if i >= 2 else None  # geometry's atr_prev of candle i-1
    atr = ctx.ind[i - 1]["atr"]                            # atr_prev of candle i
    out = {"prev_high": h / SCALE, "prev_low": l / SCALE,
           "prev_body_ratio": abs(cl - o) / span if span > 0 else None,
           "prev_range_atr": span / SCALE / atr_before if atr_before and atr_before > 0 else None,
           "prev_doji_b10": DOJI_B10 * abs(cl - o) <= span if span > 0 else None}
    subtype = doji_subtype(o, h, l, cl)
    if subtype is not None:
        out["prev_doji_subtype"] = subtype
    close = c.close[i]
    direction = 1 if close > h else -1 if close < l else 0
    out["confirm_dir"] = direction
    if direction:
        excess = close - h if direction > 0 else l - close
        out["confirm_excess_atr"] = excess / SCALE / atr if atr and atr > 0 else None
    out["flush_prev_dir"] = dv.flush(ctx, i - 1)["flush_dir"]
    return out


# ---------------------------------------------------------------- ctx2 (ctx2-v1)


def _prior20_reclaim(c, i: int):
    """+1: candle i-1 traded below the low of candles i-21..i-2 and closed above it; -1 the high mirror;
    0 neither or both. None unless candles i-21..i-1 are valid."""
    j = i - 1
    if j < PRIOR_N or not all(c.valid[j - PRIOR_N:j + 1]):
        return None
    low, high = min(c.low[j - PRIOR_N:j]), max(c.high[j - PRIOR_N:j])
    up = c.low[j] < low and c.close[j] > low
    down = c.high[j] > high and c.close[j] < high
    return 0 if up == down else (1 if up else -1)


def _climax(ctx, i: int) -> dict:
    c = ctx.candles
    out = dict.fromkeys(("climax_vol_max6", "climax_age6", "climax_ret_sigma6"))
    if i < CLIMAX_N:
        return out
    window = range(i - CLIMAX_N, i)  # k = i - j in 1..6
    volumes = [ctx.climax_volume[j] for j in window]
    if None not in volumes:
        best = max(range(CLIMAX_N), key=lambda t: (volumes[t], t))  # ties: the most recent
        out["climax_vol_max6"], out["climax_age6"] = volumes[best], CLIMAX_N - best
    if all(c.valid[j] and ctx.sigma[j] for j in window):
        rets = [log(c.close[j] / c.open[j]) / ctx.sigma[j] for j in window]
        best = max(range(CLIMAX_N), key=lambda t: (abs(rets[t]), t))
        out["climax_ret_sigma6"] = rets[best]
    return out


def ctx2(ctx, i: int) -> dict:
    c = ctx.candles
    prev_ind = ctx.ind[i - 1] if i else {}
    fast, slow = prev_ind.get("ema_fast"), prev_ind.get("ema_slow")
    close_prev = c.close[i - 1] / SCALE if i and c.valid[i - 1] else None
    px20 = _sign(close_prev - fast) if close_prev is not None and fast is not None else None
    px50 = _sign(close_prev - slow) if close_prev is not None and slow is not None else None
    trend = _sign(fast - slow) if fast is not None and slow is not None else None
    agree = None if None in (trend, px20, px50) else (trend != 0 and trend == px20 == px50)
    out = {"trend_px20_prev": px20, "trend_px50_prev": px50, "trend_agree": agree}
    highs, lows = ctx.swing_pairs[0][i], ctx.swing_pairs[1][i]
    if highs is None or lows is None:
        out["swing_trend_dir"] = None
    else:
        rising, falling = highs[1] > highs[0] and lows[1] > lows[0], highs[1] < highs[0] and lows[1] < lows[0]
        out["swing_trend_dir"] = 1 if rising else -1 if falling else 0
    state = ctx.daily_state
    k = last_closed_index(state.candles, c.open_ms(i)) if state is not None else None
    if k is None or not state.candles.valid[k] or state.ind[k]["ema_fast"] is None or state.ind[k]["ema_slow"] is None:
        out["htf_trend_dir"] = None
    else:
        out["htf_trend_dir"] = _sign(state.ind[k]["ema_fast"] - state.ind[k]["ema_slow"])
    out["prior20_reclaim_dir"] = _prior20_reclaim(c, i)
    out.update(_climax(ctx, i))
    return out


PREV_COLUMNS = (
    ("prev_body_ratio", "body / range of candle i-1 (empty if its range is 0)"),
    ("prev_range_atr", "range of candle i-1 / its own atr_prev (Wilder ATR14 through i-2)"),
    ("prev_doji_b10", "body_ratio of candle i-1 <= 0.10 (exact integers, geometry rule)"),
    ("prev_doji_subtype", "only when prev_doji_b10: dragonfly | gravestone | long_legged | standard (geometry rule)"),
    ("prev_high", "high of candle i-1"),
    ("prev_low", "low of candle i-1"),
    ("confirm_dir", "+1 close_i > high_(i-1), -1 close_i < low_(i-1), else 0; empty if i-1 is invalid"),
    ("confirm_excess_atr", "only when confirm_dir != 0: distance of close_i beyond that extreme / atr_prev"),
    ("flush_prev_dir", "flush_dir of candle i-1 (flush-v1 rule, known at the end of i-1)"),
)
CTX2_COLUMNS = (
    ("trend_px20_prev", "sign(close_(i-1) - ema20_prev)"),
    ("trend_px50_prev", "sign(close_(i-1) - ema50_prev)"),
    ("trend_agree", "trend_dir, trend_px20_prev and trend_px50_prev all equal and non-zero"),
    ("swing_trend_dir", "+1 last two same-tf swing highs AND lows rising, -1 both falling, else 0 "
                        "(fractal n=5 confirmed by the end of i-1)"),
    ("htf_trend_dir", "sign(EMA20 - EMA50) of the dk1 daily closes of the days ended at or before the open"),
    ("prior20_reclaim_dir", "+1 candle i-1 traded below the low of i-21..i-2 and closed above it, -1 the high "
                            "mirror, 0 neither or both"),
    ("climax_vol_max6", "max relative volume of candles i-6..i-1 (intraday rel_volume_slot, else "
                        "rel_volume_prior20); empty if any is missing"),
    ("climax_age6", "k in 1..6 of that candle (i-k; ties: the most recent)"),
    ("climax_ret_sigma6", "ln(close/open) of the candle in i-6..i-1 with the largest |return|, in one-candle "
                          "sigma at that candle's own end"),
)
SIGMA_SEAS_COLUMNS = (
    ("sigma_seas_factor", "intraday: mean |ret_1_sigma| of the UTC slot of candle i+1 over rows whose outcome "
                          "candle ended in the 60 days through end of i (>= 30) / mean of all slots in that window"),
    ("ret_1_sigma_seas", "intraday: ret_1_sigma / sigma_seas_factor"),
    ("ln_range1_sigma_seas", "intraday: ln(range1_sigma / sigma_seas_factor)"),
)
